"""Point d'entrée du pipeline ClipMaker.

Usage :
    python main.py <url_youtube> [--clips 5]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable

from cutter import CutError, cut_clip
from downloader import DownloadError, download, find_downloaded
from selector import SelectionError, select_clips
from subtitles import SubtitleError
from transcriber import (
    TranscriptionError,
    transcribe,
    transcription_path,
)

WORK_DIR = Path("work")
OUTPUT_DIR = Path("output")
TOTAL_STEPS = 5

_INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_SPACES = re.compile(r"\s+")


def _configure_console() -> None:
    """Évite un crash d'encodage sur les titres Unicode (console Windows)."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(errors="replace")
            except (ValueError, OSError):
                pass


def _display_path(path: str | os.PathLike) -> str:
    """Affiche un chemin relatif au dossier courant quand c'est possible."""
    try:
        relative = os.path.relpath(os.fspath(path))
    except ValueError:
        return os.fspath(path)
    if relative.startswith(".."):
        return os.fspath(path)
    return relative


def _load_segments(path: str | os.PathLike) -> list[dict] | None:
    """Charge un JSON de transcription, ou None s'il est absent/invalide."""
    file = Path(path)
    if not file.is_file():
        return None

    try:
        data = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None

    if not isinstance(data, list):
        return None
    for item in data:
        if not isinstance(item, dict) or not {"start", "end", "text"} <= item.keys():
            return None
    return data


def _clean_title(title: str) -> str:
    """Transforme un titre en nom de fichier lisible et sûr."""
    text = _INVALID_FILENAME_CHARS.sub(" ", title)
    text = _SPACES.sub("_", text.strip())
    text = re.sub(r"_+", "_", text).strip("._")
    text = text[:80].rstrip("._")
    return text or "clip"


def _output_path(index: int, title: str) -> Path:
    """Construit le chemin de sortie ``output/01_titre.mp4``."""
    return OUTPUT_DIR / f"{index:02d}_{_clean_title(title)}.mp4"


def _clip_cache_path(video: str, start: float, end: float) -> Path:
    """Chemin du JSON de transcription d'un passage (temps relatifs)."""
    stem = Path(video).stem[:60].rstrip(" .")
    start_ms = int(round(start * 1000))
    end_ms = int(round(end * 1000))
    return WORK_DIR / f"{stem}__clip_{start_ms}_{end_ms}.json"


def _extract_audio(video: str, start: float, end: float, target: str) -> None:
    """Extrait la piste audio d'un passage dans un WAV mono 16 kHz."""
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise RuntimeError(
            "ffmpeg est introuvable dans le PATH. Installez-le puis relancez."
        )

    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        f"{start:.3f}",
        "-t",
        f"{end - start:.3f}",
        "-i",
        video,
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "pcm_s16le",
        target,
    ]

    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError as exc:
        raise RuntimeError(f"ffmpeg introuvable : {ffmpeg}") from exc

    if result.returncode != 0:
        details = (result.stderr or "").strip()
        raise RuntimeError(
            f"Échec de l'extraction audio : {details or 'erreur inconnue'}"
        )


def _transcribe_clip(
    video: str,
    start: float,
    end: float,
    label: str,
) -> list[dict]:
    """Transcrit un passage avec des timestamps relatifs à son début."""
    cache = _clip_cache_path(video, start, end)
    cached = _load_segments(cache)
    if cached is not None:
        print(f"      {label} Transcription réutilisée : {_display_path(cache)}")
        return cached

    WORK_DIR.mkdir(parents=True, exist_ok=True)
    wav_fd, wav_name = tempfile.mkstemp(
        dir=str(WORK_DIR), prefix="clipmaker_audio_", suffix=".wav"
    )
    os.close(wav_fd)

    try:
        _extract_audio(video, start, end, wav_name)
        segments = transcribe(wav_name, json_path=cache)
    finally:
        try:
            os.unlink(wav_name)
        except OSError:
            pass

    print(f"      {label} Transcription terminée : {len(segments)} segments")
    return segments


def _show_summary(
    successes: list[tuple[dict, Path]],
    failures: list[tuple[dict, Exception]],
) -> int:
    """Affiche le bilan et retourne le code de sortie du programme."""
    print()
    print(f"[{TOTAL_STEPS}/{TOTAL_STEPS}] Résumé")
    print(
        f"      {len(successes)} clip(s) réussi(s), "
        f"{len(failures)} échoué(s)."
    )

    if successes:
        print("      Fichiers produits :")
        for clip, path in successes:
            print(
                f"        - {_display_path(path)} — {clip['title']!r} "
                f"(score {clip.get('score', '?')})"
            )

    if failures:
        print("      Clips en échec :")
        for clip, error in failures:
            print(f"        - {clip['title']!r} : {error}")

    return 0 if not failures else 1


def _run(
    url: str,
    clips_count: int,
    results: dict | None = None,
    transcriber: Callable[[str, str], list[dict]] | None = None,
    clip_transcriber: Callable[[str, float, float, str, list[dict]], list[dict]]
    | None = None,
    on_clip: Callable[[dict, Path], None] | None = None,
) -> int:
    """Exécute le pipeline complet et retourne un code de sortie.

    ``results`` permet à un appelant (interface web, worker cloud) de récupérer
    la liste des clips produits et des échecs sous forme de dictionnaire.

    Les backends de transcription sont injectables pour réutiliser le même
    pipeline en local (faster-whisper) ou dans le cloud (Groq) :

    - ``transcriber(video, json_path)`` transcrit la vidéo complète ;
    - ``clip_transcriber(video, start, end, label, segments)`` retourne les mots
      du passage avec des timestamps relatifs à son début ;
    - ``on_clip(clip, path)`` est appelé après chaque clip réussi (par exemple
      pour l'envoyer dans un stockage objet). Une exception y est traitée comme
      un échec du clip.
    """
    _configure_console()

    if results is None:
        results = {}
    results.setdefault("successes", [])
    results.setdefault("failures", [])

    transcribe_fn = transcriber if transcriber is not None else transcribe

    # 1. Téléchargement (ou réutilisation du fichier déjà présent).
    print(f"[1/{TOTAL_STEPS}] Téléchargement de la vidéo...")
    video: str | None = None
    try:
        video = find_downloaded(url)
    except (DownloadError, OSError):
        video = None

    if video is not None:
        print(f"      Vidéo déjà téléchargée, réutilisation : {_display_path(video)}")
    else:
        try:
            video = download(url)
        except DownloadError as exc:
            print(f"Erreur de téléchargement : {exc}", file=sys.stderr)
            return 1
        print(f"      Téléchargée : {_display_path(video)}")

    # 2. Transcription complète (ou réutilisation du JSON existant).
    print(f"[2/{TOTAL_STEPS}] Transcription de la vidéo...")
    transcript_file = transcription_path(video)
    segments = _load_segments(transcript_file)
    if segments is not None:
        print(
            f"      Transcription réutilisée : {_display_path(transcript_file)} "
            f"({len(segments)} segments)"
        )
    else:
        print("      Transcription en cours (cela peut prendre plusieurs minutes)...")
        try:
            segments = transcribe_fn(video, transcript_file)
        except TranscriptionError as exc:
            print(f"Erreur de transcription : {exc}", file=sys.stderr)
            return 1
        print(f"      Transcription terminée : {len(segments)} segments")

    # 3. Sélection des passages par le LLM.
    print(f"[3/{TOTAL_STEPS}] Sélection des extraits (max {clips_count})...")
    try:
        clips = select_clips(segments, n=clips_count)
    except SelectionError as exc:
        print(f"Erreur de sélection : {exc}", file=sys.stderr)
        return 1

    total = len(clips)
    print(f"      {total} extrait(s) sélectionné(s).")

    # 4. Découpe et sous-titres, un passage à la fois.
    print(f"[4/{TOTAL_STEPS}] Découpe et sous-titrage de {total} clip(s)...")
    try:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"Impossible de créer {OUTPUT_DIR} : {exc}", file=sys.stderr)
        return 1

    successes: list[tuple[dict, Path]] = []
    failures: list[tuple[dict, Exception]] = []

    for index, clip in enumerate(clips, start=1):
        label = f"[clip {index}/{total}]"
        start = float(clip["start"])
        end = float(clip["end"])
        title = str(clip["title"])
        score = clip.get("score", "?")

        print()
        print(
            f"      {label} {title!r} — score {score} — "
            f"{start:.1f}s → {end:.1f}s"
        )

        try:
            print(f"      {label} Transcription du passage (temps relatifs)...")
            if clip_transcriber is None:
                words = _transcribe_clip(video, start, end, label)
            else:
                words = clip_transcriber(video, start, end, label, segments)
            if not words:
                raise RuntimeError("Aucun mot transcrit dans ce passage.")

            # cut_clip génère le fichier ASS (temps relatifs) et l'incruste
            # pendant la même passe ffmpeg.
            print(f"      {label} Découpe et incrustation des sous-titres...")
            output = _output_path(index, title)
            result = cut_clip(
                video,
                start,
                end,
                str(output),
                words=words,
                words_relative=True,
            )
            path = Path(result)
            if on_clip is not None:
                on_clip(clip, path)
        except (CutError, TranscriptionError, SubtitleError, RuntimeError) as exc:
            print(f"      {label} ÉCHEC : {exc}", file=sys.stderr)
            failures.append((clip, exc))
            continue
        except Exception as exc:  # un clip ne doit jamais arrêter le pipeline
            print(f"      {label} ÉCHEC inattendu : {exc}", file=sys.stderr)
            failures.append((clip, exc))
            continue

        print(f"      {label} OK : {_display_path(path)}")
        successes.append((clip, path))

    # 5. Bilan final.
    results["successes"] = successes
    results["failures"] = failures
    code = _show_summary(successes, failures)
    results["exit_code"] = code
    return code


def _positive_int(value: str) -> int:
    """Valide ``--clips`` comme entier strictement positif."""
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"un entier positif est attendu, reçu {value!r}"
        ) from exc
    if number < 1:
        raise argparse.ArgumentTypeError(
            f"le nombre de clips doit être supérieur ou égal à 1, reçu {number}"
        )
    return number


def main(argv: list[str] | None = None) -> int:
    """Lance le pipeline complet : téléchargement, transcription, sélection, découpe et sous-titres."""
    _configure_console()

    parser = argparse.ArgumentParser(
        description=(
            "Télécharge une vidéo YouTube, sélectionne les meilleurs passages, "
            "les découpe en vertical 1080x1920 et incruste les sous-titres."
        )
    )
    parser.add_argument("url", help="URL de la vidéo YouTube")
    parser.add_argument(
        "--clips",
        type=_positive_int,
        default=5,
        metavar="N",
        help="nombre maximum de clips à produire (défaut : 5)",
    )
    args = parser.parse_args(argv)

    return _run(args.url, args.clips)


def cli(argv: list[str]) -> int:
    """Compatibilité : point d'entrée historique de la ligne de commande."""
    return main(argv)


if __name__ == "__main__":
    raise SystemExit(cli(sys.argv[1:]))
