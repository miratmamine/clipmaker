"""Téléchargement des vidéos sources."""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import yt_dlp

WORK_DIR = "work"
MAX_HEIGHT = 1080
VIDEO_SUFFIXES = {".mp4", ".mkv", ".webm", ".mov", ".m4v"}

# Clients YouTube essayés dans cet ordre si YouTube exige une vérification
# anti-robot (fréquent depuis les IP de datacenter, ex. GitHub Actions).
FALLBACK_CLIENTS = ("tv_simply", "android_vr", "web_embedded")

YOUTUBE_HOSTS = {
    "youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
    "youtube-nocookie.com",
}


class DownloadError(Exception):
    """Erreur de téléchargement d'une vidéo."""


class _SilentLogger:
    """Empêche yt-dlp d'afficher ses propres erreurs : on les traduit nous-mêmes."""

    def debug(self, message: str) -> None:
        pass

    def warning(self, message: str) -> None:
        pass

    def error(self, message: str) -> None:
        pass


def _validate_url(url: str) -> str:
    """Vérifie que l'URL est bien un lien YouTube et retourne sa forme nettoyée."""
    url = url.strip()
    if not url:
        raise DownloadError("L'URL est vide.")

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise DownloadError(
            f"Lien invalide : {url!r}. Une URL YouTube complète est attendue "
            "(ex. https://www.youtube.com/watch?v=...)."
        )

    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if host not in YOUTUBE_HOSTS:
        raise DownloadError(
            f"Lien invalide : {url!r} ne pointe pas vers YouTube."
        )

    return url


def _friendly_error(exc: Exception, url: str) -> str:
    """Traduit une erreur yt-dlp en message compréhensible."""
    message = str(exc).lower()

    if "not a bot" in message or "confirm you" in message:
        return (
            "YouTube demande une vérification anti-robot, fréquente depuis une "
            "adresse de serveur. Réessaie plus tard, envoie la vidéo depuis le "
            "téléphone (formulaire « Envoyer une vidéo »), ou lance le "
            f"traitement depuis le PC : python main.py {url}"
        )
    if "private" in message:
        return "La vidéo est privée et ne peut pas être téléchargée."
    if "sign in" in message or "login" in message or "members-only" in message:
        return "La vidéo nécessite une connexion (contenu réservé aux membres)."
    if "age" in message and "confirm" in message:
        return "La vidéo est soumise à une restriction d'âge."
    if "unavailable" in message or "removed" in message or "does not exist" in message:
        return "La vidéo est indisponible ou a été supprimée."
    if "not a valid url" in message or "unsupported url" in message:
        return f"Lien invalide : {url!r}."
    if "timed out" in message or "network" in message or "connection" in message:
        return "Erreur réseau pendant le téléchargement. Vérifiez votre connexion."

    return f"Échec du téléchargement de {url!r} : {exc}"


def _youtube_id(url: str) -> str | None:
    """Extrait l'identifiant d'une vidéo YouTube à partir de l'URL."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]

    video_id: str | None = None
    if host == "youtu.be":
        video_id = parsed.path.strip("/").split("/")[0]
    else:
        query_id = parse_qs(parsed.query).get("v")
        if query_id:
            video_id = query_id[0]
        else:
            parts = [part for part in parsed.path.split("/") if part]
            for marker in ("shorts", "embed", "live", "v"):
                if marker in parts:
                    index = parts.index(marker)
                    if index + 1 < len(parts):
                        video_id = parts[index + 1]
                        break

    if video_id and video_id.replace("-", "").replace("_", "").isalnum():
        return video_id
    return None


def _mtime(path: Path) -> float:
    """Date de modification, ou 0 si le fichier est inaccessible."""
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def find_downloaded(url: str) -> str | None:
    """Retourne une vidéo déjà téléchargée dans ./work/ pour cette URL.

    Le nom de fichier produit par yt-dlp contient l'identifiant de la vidéo
    entre crochets (``Titre [id].mp4``), ce qui permet de retrouver le fichier
    sans refaire le téléchargement.

    Args:
        url: URL YouTube déjà validée ou non.

    Returns:
        Le chemin absolu du fichier existant, ou None s'il est absent.
    """
    url = _validate_url(url)
    video_id = _youtube_id(url)
    if video_id is None:
        return None

    work = Path(WORK_DIR)
    if not work.is_dir():
        return None

    marker = f"[{video_id}]"
    candidates = [
        path
        for path in work.iterdir()
        if path.is_file()
        and path.suffix.lower() in VIDEO_SUFFIXES
        and marker in path.stem
    ]
    candidates.sort(key=_mtime, reverse=True)

    for path in candidates:
        try:
            if path.stat().st_size > 0:
                return str(path.resolve())
        except OSError:
            continue
    return None


def download(url: str) -> str:
    """Télécharge une vidéo YouTube en MP4 (1080p maximum) dans ./work/.

    Args:
        url: URL de la vidéo YouTube à télécharger.

    Returns:
        Le chemin absolu du fichier vidéo téléchargé.

    Raises:
        DownloadError: Si le lien est invalide, si la vidéo est privée,
            indisponible, ou si le téléchargement échoue.
    """
    url = _validate_url(url)
    os.makedirs(WORK_DIR, exist_ok=True)

    try:
        return _extract(url, None)
    except yt_dlp.utils.YoutubeDLError as exc:
        if not _is_bot_check(exc):
            raise DownloadError(_friendly_error(exc, url)) from exc

        # IP de datacenter : YouTube exige une vérification. On réessaie avec
        # d'autres clients, qui sont parfois moins filtrés (GitHub Actions).
        last_error: Exception = exc
        for client in FALLBACK_CLIENTS:
            print(f"      Vérification YouTube : nouvelle tentative ({client})...")
            try:
                return _extract(url, client)
            except yt_dlp.utils.YoutubeDLError as retry_exc:
                last_error = retry_exc
            except OSError as retry_exc:
                raise DownloadError(
                    f"Erreur d'écriture dans {WORK_DIR!r} : {retry_exc}"
                ) from retry_exc
        raise DownloadError(_friendly_error(last_error, url)) from last_error
    except OSError as exc:
        raise DownloadError(f"Erreur d'écriture dans {WORK_DIR!r} : {exc}") from exc


def _is_bot_check(exc: Exception) -> bool:
    """Détecte le message « Sign in to confirm you're not a bot »."""
    message = str(exc).lower()
    return "not a bot" in message or "confirm you" in message


def _extract(url: str, client: str | None) -> str:
    """Lance yt-dlp (éventuellement avec un client YouTube imposé)."""
    options = {
        "format": (
            f"bestvideo[height<={MAX_HEIGHT}][ext=mp4]+bestaudio[ext=m4a]/"
            f"bestvideo[height<={MAX_HEIGHT}]+bestaudio/"
            f"best[height<={MAX_HEIGHT}]"
        ),
        "merge_output_format": "mp4",
        "outtmpl": os.path.join(WORK_DIR, "%(title)s [%(id)s].%(ext)s"),
        "noplaylist": True,
        "windowsfilenames": True,
        "quiet": True,
        "no_warnings": True,
        "logger": _SilentLogger(),
    }
    if client:
        options["extractor_args"] = {"youtube": {"player_client": [client]}}

    # Cookies d'un compte YouTube (secret YOUTUBE_COOKIES du runner) : c'est le
    # seul moyen fiable de télécharger depuis une IP de datacenter.
    cookies = os.getenv("YTDLP_COOKIES_FILE", "").strip()
    if cookies and Path(cookies).is_file():
        options["cookiefile"] = cookies

    # Proxy résidentiel (secret YTDLP_PROXY) : YouTube bloque les adresses de
    # datacenter ; passer par une box/mobile résidentielle lève ce blocage.
    proxy = os.getenv("YTDLP_PROXY", "").strip()
    if proxy:
        options["proxy"] = proxy

    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=True)
        if info is None:
            raise DownloadError(f"Aucune vidéo trouvée à l'URL {url!r}.")
        path = _downloaded_path(ydl, info)

    return os.path.abspath(path)


def _downloaded_path(ydl: yt_dlp.YoutubeDL, info: dict) -> str:
    """Détermine le chemin du fichier réellement écrit sur le disque."""
    downloads = info.get("requested_downloads") or []
    if downloads and downloads[0].get("filepath"):
        return downloads[0]["filepath"]

    if info.get("filepath"):
        return info["filepath"]

    path = ydl.prepare_filename(info)
    if not path.endswith(".mp4"):
        path = os.path.splitext(path)[0] + ".mp4"
    return path
