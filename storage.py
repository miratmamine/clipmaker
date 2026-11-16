"""Stockage des clips dans Supabase Storage.

Le disque d'un runner GitHub Actions (comme celui d'un conteneur) est éphémère :
chaque MP4 est donc envoyé dans un bucket privé Supabase, puis servi via une
URL signée à durée limitée. Toutes les opérations utilisent la clé
``service_role`` (côté serveur uniquement) ; le bucket n'a aucune policy
publique.
"""

from __future__ import annotations

import os
import re
import unicodedata
from pathlib import Path
from urllib.parse import quote

import httpx

DEFAULT_BUCKET = "clips"
DEFAULT_EXPIRES = 3600


class StorageError(Exception):
    """Erreur pendant une opération sur Supabase Storage."""


def configured() -> bool:
    return bool(
        os.getenv("SUPABASE_URL", "").strip()
        and (
            os.getenv("SUPABASE_SERVICE_KEY", "").strip()
            or os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()
            or os.getenv("SUPABASE_KEY", "").strip()
        )
    )


def _base_url() -> str:
    url = os.getenv("SUPABASE_URL", "").strip().rstrip("/")
    if not url:
        raise StorageError(
            "SUPABASE_URL est vide : impossible d'envoyer les clips dans le cloud."
        )
    return url


def _service_key() -> str:
    key = (
        os.getenv("SUPABASE_SERVICE_KEY", "").strip()
        or os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()
        or os.getenv("SUPABASE_KEY", "").strip()
    )
    if not key:
        raise StorageError(
            "SUPABASE_SERVICE_KEY est vide : renseignez la clé service_role "
            "du projet Supabase."
        )
    return key


def bucket() -> str:
    return os.getenv("SUPABASE_BUCKET", "").strip() or DEFAULT_BUCKET


def _headers(content_type: str | None = None) -> dict[str, str]:
    headers = {
        "apikey": _service_key(),
        "Authorization": f"Bearer {_service_key()}",
    }
    if content_type:
        headers["Content-Type"] = content_type
    return headers


def _object_url(key: str) -> str:
    return (
        f"{_base_url()}/storage/v1/object/"
        f"{quote(bucket(), safe='')}/{quote(key, safe='/')}"
    )


def _detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return (response.text or "").strip()[:300] or "réponse vide"
    if isinstance(payload, dict):
        message = payload.get("message") or payload.get("error")
        if message:
            return str(message)[:300]
    return str(payload)[:300]


def safe_key(job_id: str, index: int, filename: str) -> str:
    """Construit une clé de stockage ASCII : ``<job>/01_titre.mp4``."""
    stem = Path(filename).stem
    stem = re.sub(r"^\d{2,}_", "", stem)
    normalized = unicodedata.normalize("NFKD", stem)
    ascii_stem = normalized.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", ascii_stem).strip("._-")
    slug = re.sub(r"_+", "_", slug)[:80].rstrip("._-") or "clip"
    job = re.sub(r"[^A-Za-z0-9-]+", "", str(job_id))[:40] or "job"
    return f"{job}/{int(index):02d}_{slug}.mp4"


def upload_file(path: str | Path, key: str) -> str:
    """Envoie un fichier dans le bucket privé (écrase si la clé existe)."""
    file = Path(path)
    if not file.is_file():
        raise StorageError(f"Fichier à envoyer introuvable : {str(file)!r}")

    headers = _headers("video/mp4")
    headers["x-upsert"] = "true"

    try:
        payload = file.read_bytes()
    except OSError as exc:
        raise StorageError(f"Lecture impossible de {str(file)!r} : {exc}") from exc

    try:
        response = httpx.post(
            _object_url(key),
            headers=headers,
            content=payload,
            timeout=httpx.Timeout(300.0, connect=30.0),
        )
    except httpx.HTTPError as exc:
        raise StorageError(f"Envoi vers Supabase Storage impossible : {exc}") from exc

    if response.status_code not in (200, 201):
        raise StorageError(
            f"Supabase Storage a refusé {file.name!r} "
            f"(HTTP {response.status_code}) : {_detail(response)}"
        )
    return key


def signed_url(key: str, expires: int = DEFAULT_EXPIRES) -> str:
    """Retourne une URL signée valable ``expires`` secondes."""
    if not key:
        raise StorageError("Clé de stockage vide : impossible de signer l'URL.")

    url = (
        f"{_base_url()}/storage/v1/object/sign/"
        f"{quote(bucket(), safe='')}/{quote(key, safe='/')}"
    )
    try:
        response = httpx.post(
            url,
            headers=_headers("application/json"),
            json={"expiresIn": max(60, min(int(expires), 7 * 24 * 3600))},
            timeout=httpx.Timeout(30.0, connect=15.0),
        )
    except httpx.HTTPError as exc:
        raise StorageError(f"Signature de l'URL impossible : {exc}") from exc

    if response.status_code != 200:
        raise StorageError(
            f"Supabase Storage n'a pas signé l'URL (HTTP {response.status_code}) : "
            f"{_detail(response)}"
        )

    try:
        payload = response.json()
    except ValueError as exc:
        raise StorageError("Réponse de signature illisible.") from exc

    signed = payload.get("signedURL") or payload.get("signedUrl") or ""
    if not signed:
        raise StorageError("Supabase n'a renvoyé aucune URL signée.")

    if signed.startswith("http://") or signed.startswith("https://"):
        return signed
    if signed.startswith("/storage/v1"):
        return f"{_base_url()}{signed}"
    if signed.startswith("/"):
        return f"{_base_url()}/storage/v1{signed}"
    return f"{_base_url()}/storage/v1/{signed}"


def delete_file(key: str) -> bool:
    """Supprime un objet. Retourne False s'il était déjà absent."""
    if not key:
        return False

    url = (
        f"{_base_url()}/storage/v1/object/"
        f"{quote(bucket(), safe='')}"
    )
    try:
        response = httpx.request(
            "DELETE",
            url,
            headers=_headers("application/json"),
            json={"prefixes": [key]},
            timeout=httpx.Timeout(60.0, connect=15.0),
        )
    except httpx.HTTPError as exc:
        raise StorageError(f"Suppression dans Supabase Storage impossible : {exc}") from exc

    if response.status_code in (200, 204):
        return True
    if response.status_code == 404:
        return False

    raise StorageError(
        f"Supabase Storage a refusé la suppression (HTTP {response.status_code}) : "
        f"{_detail(response)}"
    )
