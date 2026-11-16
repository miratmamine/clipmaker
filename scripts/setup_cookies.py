"""Enregistre les cookies YouTube dans les secrets GitHub Actions.

C'est ce qui permet de télécharger une vidéo depuis le runner GitHub (adresse
IP de datacenter) : YouTube refuse sinon avec « Sign in to confirm you're not a
bot ».

Mode d'emploi :

1. Se connecter à YouTube dans un navigateur (compte jetable conseillé).
2. Exporter les cookies YouTube au format Netscape (extension « Get cookies.txt
   LOCALLY », ou ``yt-dlp --cookies-from-browser firefox``).
3. Lancer :

    python scripts/setup_cookies.py cookies.txt

Le script vérifie le fichier, puis envoie son contenu (encodé) dans le secret
``YOUTUBE_COOKIES`` du dépôt. Aucun cookie n'est écrit dans le dépôt git.

Renouvellement : les cookies expirent (souvent 1 à 3 mois). Relancer la même
commande avec un fichier frais.
"""

from __future__ import annotations

import argparse
import base64
import sys
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT / "scripts"))

import setup_github  # noqa: E402


def validate(path: Path) -> str:
    """Vérifie le format Netscape et retourne le contenu normalisé."""
    if not path.is_file():
        raise SystemExit(f"Fichier introuvable : {path}")
    text = path.read_text(encoding="utf-8", errors="replace").strip()
    if not text:
        raise SystemExit("Le fichier de cookies est vide.")

    lines = [
        line
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    youtube = [line for line in lines if "youtube.com" in line]
    if not youtube:
        raise SystemExit(
            "Aucun cookie YouTube dans ce fichier : exporte les cookies de "
            "youtube.com (format Netscape, une ligne par cookie)."
        )
    columns = youtube[0].split("\t")
    if len(columns) < 7:
        raise SystemExit(
            "Format inattendu : les cookies doivent être au format Netscape "
            "(colonnes séparées par des tabulations)."
        )
    return text + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Enregistre les cookies YouTube dans les secrets GitHub."
    )
    parser.add_argument("cookies", help="fichier cookies.txt (format Netscape)")
    parser.add_argument("--repo", default="clipmaker", help="nom du dépôt")
    args = parser.parse_args(argv)

    path = Path(args.cookies).expanduser()
    content = validate(path)
    age_days = (time.time() - path.stat().st_mtime) / 86400
    print(f"Cookies : {len(content.splitlines())} lignes, {len(content)} octets")
    if age_days > 30:
        print(
            f"Attention : ce fichier date de {age_days:.0f} jours ; les cookies "
            "expirent souvent en 1 à 3 mois."
        )

    token = setup_github._token()
    encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
    if len(encoded) > 45_000:
        raise SystemExit(
            "Fichier trop volumineux pour un secret GitHub (48 Ko max). "
            "Exporte uniquement les cookies de youtube.com."
        )

    with setup_github.httpx.Client(timeout=setup_github.TIMEOUT) as client:
        login = setup_github._whoami(client, token)
        full_name = f"{login}/{args.repo}"
        print(f"Dépôt : {full_name}")
        count = setup_github._set_secrets(
            client, token, full_name, {"YOUTUBE_COOKIES": encoded}
        )

    if count == 0:
        raise SystemExit("Le secret n'a pas été enregistré.")

    print("")
    print("Cookies enregistrés. Relance un traitement depuis le téléphone :")
    print("  - s'il démarre, YouTube accepte de nouveau le runner ;")
    print("  - sinon, utilise « Envoyer une vidéo » ou le worker du PC.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
