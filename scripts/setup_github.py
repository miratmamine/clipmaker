"""Publie ClipMaker sur GitHub et configure les secrets du runner.

Le script s'occupe de tout, sans exposer les clés :

1. vérifie le token GitHub ;
2. crée le dépôt (s'il n'existe pas) ;
3. initialise git si besoin, commite et pousse le code ;
4. enregistre les secrets utilisés par GitHub Actions (Supabase, Groq, LLM) ;
5. écrit ``render-env.txt``, à coller dans Render (Environment → Add from .env).

Lancement :

    python scripts/setup_github.py                 # dépôt public (Actions illimitées)
    python scripts/setup_github.py --private       # dépôt privé (2000 min/mois)
    python scripts/setup_github.py --secrets       # met seulement à jour les secrets
    python scripts/setup_github.py --dry-run       # affiche les étapes sans rien faire

Le token est lu dans ``.gh_token`` (racine du projet) ou dans la variable
d'environnement ``GITHUB_TOKEN``. Token classique avec la portée ``repo`` :
https://github.com/settings/tokens/new
"""

from __future__ import annotations

import argparse
import base64
import os
import subprocess
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

PROJECT = Path(__file__).resolve().parent.parent
TOKEN_FILE = PROJECT / ".gh_token"
ENV_FILE = PROJECT / ".env.cloud"
RENDER_ENV_FILE = PROJECT / "render-env.txt"
API = "https://api.github.com"
TIMEOUT = httpx.Timeout(30.0, connect=15.0)

# Secrets lus par .github/workflows/worker.yml
WORKER_KEYS = (
    "DATABASE_URL",
    "SUPABASE_URL",
    "SUPABASE_SERVICE_KEY",
    "GROQ_API_KEY",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "OPENAI_MODEL",
)

# Variables à coller dans Render (jamais dans le dépôt)
RENDER_KEYS = (
    "DATABASE_URL",
    "SUPABASE_URL",
    "SUPABASE_SERVICE_KEY",
    "APP_PASSWORD",
    "SECRET_KEY",
)


def log(message: str = "") -> None:
    print(_redact(message), flush=True)


# Valeurs à ne jamais afficher (jeton GitHub, en-tête d'authentification).
_SECRETS: list[str] = []


def _redact(text: str) -> str:
    """Remplace les secrets connus par ``***`` avant affichage."""
    for secret in _SECRETS:
        if secret:
            text = text.replace(secret, "***")
    return text


def _token() -> str:
    token = os.getenv("GITHUB_TOKEN", "").strip()
    if not token and TOKEN_FILE.is_file():
        token = TOKEN_FILE.read_text(encoding="utf-8").strip()
    if not token:
        raise SystemExit(
            "Token GitHub introuvable.\n"
            "Crée un token classique (portée « repo ») sur "
            "https://github.com/settings/tokens/new\n"
            "puis enregistre-le :\n"
            f'  Set-Content -Path "{TOKEN_FILE}" -Value $t -NoNewline'
        )
    _SECRETS.append(token)
    return token


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "ClipMaker",
    }


def _whoami(client: httpx.Client, token: str) -> str:
    response = client.get(f"{API}/user", headers=_headers(token))
    if response.status_code != 200:
        raise SystemExit(
            f"Token GitHub refusé (HTTP {response.status_code}). "
            "Vérifie qu'il n'a pas expiré et qu'il a la portée « repo »."
        )
    login = str(response.json().get("login") or "").strip()
    if not login:
        raise SystemExit("GitHub n'a pas renvoyé de nom de compte.")
    return login


def _repo_exists(client: httpx.Client, token: str, full_name: str) -> bool:
    response = client.get(f"{API}/repos/{full_name}", headers=_headers(token))
    if response.status_code == 200:
        return True
    if response.status_code == 404:
        return False
    raise SystemExit(
        f"Vérification du dépôt impossible (HTTP {response.status_code})."
    )


def _create_repo(
    client: httpx.Client, token: str, name: str, private: bool
) -> str:
    response = client.post(
        f"{API}/user/repos",
        headers=_headers(token),
        json={
            "name": name,
            "private": private,
            "description": "ClipMaker : clips verticaux sous-titres depuis YouTube",
            "has_issues": False,
            "has_wiki": False,
            "auto_init": False,
        },
    )
    if response.status_code not in (200, 201):
        raise SystemExit(
            f"Création du dépôt refusée (HTTP {response.status_code}) : "
            f"{response.text[:300]}"
        )
    payload = response.json()
    return str(payload["full_name"])


def _git(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["git", *args],
        cwd=PROJECT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if check and result.returncode != 0:
        raise SystemExit(
            f"Échec de « git {_redact(' '.join(args))} » :\n"
            f"{_redact((result.stderr or result.stdout).strip()[:800])}"
        )
    return result


def _ensure_git_repo() -> None:
    if not (PROJECT / ".git").exists():
        log("→ Initialisation du dépôt git local...")
        _git("init")

    if _git("rev-parse", "--verify", "HEAD", check=False).returncode != 0:
        # Dépôt sans aucun commit : on nomme la branche principale « main ».
        _git("symbolic-ref", "HEAD", "refs/heads/main", check=False)

    if not _git("config", "user.name", check=False).stdout.strip():
        _git("config", "user.name", "ClipMaker")
    if not _git("config", "user.email", check=False).stdout.strip():
        _git("config", "user.email", "clipmaker@users.noreply.github.com")


def _commit() -> None:
    log("→ Préparation du commit...")
    _git("add", "-A")
    staged = _git("diff", "--cached", "--name-only", check=False).stdout.strip()
    if staged:
        _git("commit", "-m", "ClipMaker : interface Render + worker GitHub Actions")
        log(f"  {len(staged.splitlines())} fichier(s) commité(s)")
    else:
        log("  (aucune modification à commiter)")

    # Render et la tâche planifiée Actions attendent la branche « main ».
    _git("branch", "-M", "main", check=False)


def _remote_matches(full_name: str) -> bool:
    remote = _git("remote", "get-url", "origin", check=False).stdout.strip()
    return full_name.lower() in remote.lower()


def _auth_header(token: str) -> str:
    """En-tête HTTP d'authentification pour ``git push`` (sans URL à jeton)."""
    raw = base64.b64encode(f"x-access-token:{token}".encode("utf-8")).decode("ascii")
    return f"Authorization: Basic {raw}"


def _push(full_name: str, token: str) -> None:
    clean = f"https://github.com/{full_name}.git"
    if not _remote_matches(full_name):
        _git("remote", "remove", "origin", check=False)
        _git("remote", "add", "origin", clean)
    else:
        # Au cas où une ancienne URL contiendrait le jeton.
        _git("remote", "set-url", "origin", clean, check=False)

    log("→ Envoi du code sur GitHub...")
    header = _auth_header(token)
    _SECRETS.append(header.split("Basic ", 1)[-1])
    # Le jeton passe par un en-tête HTTP : ni dans .git/config, ni dans l'URL.
    result = _git(
        "-c",
        "credential.helper=",
        "-c",
        f"http.extraheader={header}",
        "push",
        "-u",
        "origin",
        "main",
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        if "workflow" in detail and "scope" in detail:
            raise SystemExit(
                "Le token GitHub n'a pas la portée « workflow », obligatoire "
                "pour publier .github/workflows/worker.yml.\n\n"
                "À faire (30 secondes, le token ne change pas) :\n"
                "  1. https://github.com/settings/tokens\n"
                "  2. cliquer sur le token utilisé → cocher « workflow »\n"
                "  3. Update token, puis relancer cette commande.\n\n"
                f"Détail GitHub : {detail[:300]}"
            )
        raise SystemExit(
            "L'envoi a échoué :\n"
            f"{detail[:800]}\n\n"
            "Vérifie que le token a bien la portée « repo » et qu'il n'a pas "
            "expiré (https://github.com/settings/tokens)."
        )
    log("  Code publié.")


def _public_key(client: httpx.Client, token: str, full_name: str) -> tuple[str, str]:
    response = client.get(
        f"{API}/repos/{full_name}/actions/secrets/public-key",
        headers=_headers(token),
    )
    if response.status_code != 200:
        raise SystemExit(
            "Impossible de récupérer la clé publique des secrets "
            f"(HTTP {response.status_code}) : {response.text[:200]}"
        )
    payload = response.json()
    return str(payload["key_id"]), str(payload["key"])


def _encrypt(public_key: str, value: str) -> str:
    try:
        from nacl import encoding, public
    except ImportError as exc:  # pragma: no cover - dépend de l'environnement
        raise SystemExit(
            "Le paquet PyNaCl est absent : installe-le avec\n"
            f"  {sys.executable} -m pip install pynacl"
        ) from exc

    key = public.PublicKey(public_key.encode("utf-8"), encoding.Base64Encoder())
    sealed = public.SealedBox(key)
    return base64.b64encode(sealed.encrypt(value.encode("utf-8"))).decode("utf-8")


def _set_secrets(
    client: httpx.Client, token: str, full_name: str, values: dict[str, str]
) -> int:
    key_id, public_key = _public_key(client, token, full_name)
    count = 0
    for name in WORKER_KEYS:
        value = values.get(name, "").strip()
        if not value:
            log(f"  {name} : ignoré (valeur vide)")
            continue
        response = client.put(
            f"{API}/repos/{full_name}/actions/secrets/{name}",
            headers=_headers(token),
            json={
                "encrypted_value": _encrypt(public_key, value),
                "key_id": key_id,
            },
        )
        if response.status_code not in (200, 201, 204):
            raise SystemExit(
                f"Enregistrement du secret {name} refusé "
                f"(HTTP {response.status_code}) : {response.text[:200]}"
            )
        log(f"  {name} : enregistré ({len(value)} caractères)")
        count += 1
    return count


def _write_render_env(full_name: str, token: str, values: dict[str, str]) -> None:
    lines = ["# Render → Environment → Add from .env (coller tout ce bloc)"]
    for name in RENDER_KEYS:
        value = values.get(name, "").strip()
        if value:
            lines.append(f"{name}={value}")
    lines.extend(
        [
            "SUPABASE_BUCKET=clips",
            f"GITHUB_REPO={full_name}",
            f"GITHUB_DISPATCH_TOKEN={token}",
            "GITHUB_DISPATCH_EVENT=clipmaker-job",
            "SIGNED_URL_SECONDS=3600",
            "MAX_ACTIVE_JOBS=2",
        ]
    )
    RENDER_ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log(f"→ Fichier créé : {RENDER_ENV_FILE}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Publie ClipMaker sur GitHub et configure Actions."
    )
    parser.add_argument("--repo", default="clipmaker", help="nom du dépôt")
    parser.add_argument(
        "--private",
        action="store_true",
        help="dépôt privé (2000 min Actions/mois au lieu d'illimité)",
    )
    parser.add_argument(
        "--secrets",
        action="store_true",
        help="met seulement à jour les secrets et render-env.txt",
    )
    parser.add_argument("--dry-run", action="store_true", help="simulation")
    args = parser.parse_args(argv)

    load_dotenv(ENV_FILE, override=False)
    token = _token()
    values = {name: os.getenv(name, "") for name in set(WORKER_KEYS + RENDER_KEYS)}

    with httpx.Client(timeout=TIMEOUT) as client:
        login = _whoami(client, token)
        full_name = f"{login}/{args.repo}"
        log(f"Compte GitHub : {login}")
        log(f"Dépôt cible   : {full_name} ({'privé' if args.private else 'public'})")

        if args.dry_run:
            log("(simulation) aucune modification envoyée.")
            return 0

        if not args.secrets:
            if _repo_exists(client, token, full_name):
                log("→ Le dépôt existe déjà.")
            else:
                full_name = _create_repo(client, token, args.repo, args.private)
                log(f"→ Dépôt créé : {full_name}")

            _ensure_git_repo()
            _commit()
            _push(full_name, token)

        log("→ Enregistrement des secrets GitHub Actions...")
        count = _set_secrets(client, token, full_name, values)
        if count == 0:
            raise SystemExit(
                "Aucun secret enregistré : vérifie les valeurs dans .env.cloud."
            )

    _write_render_env(full_name, token, values)
    log("")
    log("GitHub est prêt.")
    log("Étape suivante : Render → New → Blueprint → dépôt "
        f"{full_name} → Apply, puis colle render-env.txt dans Environment.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
