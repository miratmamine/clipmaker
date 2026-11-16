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
import secrets
import unicodedata
from pathlib import Path
from urllib.parse import quote

import httpx

DEFAULT_BUCKET = "clips"
DEFAULT_EXPIRES = 3600
# Préfixe des vidéos envoyées depuis le téléphone (supprimées après usage).
SOURCE_PREFIX = "sources"
# Schéma utilisé dans jobs.url pour désigner un objet du bucket au lieu d'un lien.
UPLOAD_SCHEME = "upload://"
VIDEO_SUFFIXES = {".mp4", ".mkv", ".webm", ".mov", ".m4v"}


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


def _slug(text: str, limit: int = 80) -> str:
    """Réduit un texte à un identifiant ASCII sans espace ni accent."""
    normalized = unicodedata.normalize("NFKD", text)
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", ascii_text).strip("._-")
    return re.sub(r"_+", "_", slug)[:limit].rstrip("._-")


def safe_key(job_id: str, index: int, filename: str) -> str:
    """Construit une clé de stockage ASCII : ``<job>/01_titre.mp4``."""
    stem = _slug(re.sub(r"^\d{2,}_", "", Path(filename).stem)) or "clip"
    job = re.sub(r"[^A-Za-z0-9-]+", "", str(job_id))[:40] or "job"
    return f"{job}/{int(index):02d}_{stem}.mp4"


def source_key(filename: str) -> str:
    """Clé d'une vidéo envoyée depuis le téléphone (jeton aléatoire)."""
    suffix = Path(filename).suffix.lower()
    if suffix not in VIDEO_SUFFIXES:
        suffix = ".mp4"
    stem = _slug(Path(filename).stem, 60) or "video"
    return f"{SOURCE_PREFIX}/{secrets.token_hex(8)}/{stem}{suffix}"


def source_object(url: str) -> str:
    """Retourne la clé Storage d'une URL ``upload://…`` (sinon chaîne vide)."""
    text = (url or "").strip()
    if not text.lower().startswith(UPLOAD_SCHEME):
        return ""
    return text[len(UPLOAD_SCHEME) :].strip()


def upload_file(
    path: str | Path, key: str, content_type: str = "video/mp4"
) -> str:
    """Envoie un fichier dans le bucket privé (écrase si la clé existe)."""
    file = Path(path)
    if not file.is_file():
        raise StorageError(f"Fichier à envoyer introuvable : {str(file)!r}")

    headers = _headers(content_type)
    headers["x-upsert"] = "true"

    try:
        size = file.stat().st_size
    except OSError as exc:
        raise StorageError(f"Lecture impossible de {str(file)!r} : {exc}") from exc
    if size == 0:
        raise StorageError(f"Le fichier {file.name!r} est vide.")

    # Envoi en flux : un MP4 de plusieurs centaines de Mo ne passe pas en RAM
    # sur un hébergement gratuit (512 Mo).
    try:
        with file.open("rb") as handle:
            response = httpx.post(
                _object_url(key),
                headers={**headers, "Content-Length": str(size)},
                content=handle,
                timeout=httpx.Timeout(900.0, connect=30.0),
            )
    except (httpx.HTTPError, OSError) as exc:
        raise StorageError(f"Envoi vers Supabase Storage impossible : {exc}") from exc

    if response.status_code not in (200, 201):
        raise StorageError(
            f"Supabase Storage a refusé {file.name!r} "
            f"(HTTP {response.status_code}) : {_detail(response)}"
        )
    return key


def download_file(key: str, dest: str | Path) -> Path:
    """Télécharge un objet du bucket vers ``dest`` et retourne son chemin."""
    if not key:
        raise StorageError("Clé de stockage vide : impossible de récupérer la vidéo.")

    target = Path(dest)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with httpx.stream(
            "GET",
            _object_url(key),
            headers=_headers(),
            timeout=httpx.Timeout(900.0, connect=30.0),
            follow_redirects=True,
        ) as response:
            if response.status_code != 200:
                response.read()
                raise StorageError(
                    f"Supabase Storage n'a pas renvoyé {key!r} "
                    f"(HTTP {response.status_code}) : {_detail(response)}"
                )
            with target.open("wb") as handle:
                for chunk in response.iter_bytes(1 << 20):
                    handle.write(chunk)
    except httpx.HTTPError as exc:
        raise StorageError(f"Téléchargement depuis Supabase impossible : {exc}") from exc
    except OSError as exc:
        raise StorageError(f"Écriture impossible dans {str(target)!r} : {exc}") from exc

    if target.stat().st_size == 0:
        raise StorageError(f"Le fichier reçu pour {key!r} est vide.")
    return target


def list_objects(prefix: str, limit: int = 1000) -> list[dict]:
    """Liste les objets sous ``prefix`` (nom relatif + date de création)."""
    url = f"{_base_url()}/storage/v1/object/list/{quote(bucket(), safe='')}"
    try:
        response = httpx.post(
            url,
            headers=_headers("application/json"),
            json={
                "prefix": prefix,
                "limit": int(limit),
                "offset": 0,
                "sortBy": {"column": "created_at", "order": "asc"},
            },
            timeout=httpx.Timeout(60.0, connect=15.0),
        )
    except httpx.HTTPError as exc:
        raise StorageError(f"Liste du stockage impossible : {exc}") from exc

    if response.status_code != 200:
        raise StorageError(
            f"Supabase Storage a refusé la liste (HTTP {response.status_code}) : "
            f"{_detail(response)}"
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise StorageError("Réponse de liste illisible.") from exc
    return payload if isinstance(payload, list) else []



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
