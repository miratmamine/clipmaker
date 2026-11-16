"""Découpe des extraits vidéo."""

from __future__ import annotations

import math
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from subtitles import SubtitleError, write_ass

OUTPUT_WIDTH = 1080
OUTPUT_HEIGHT = 1920


class CutError(Exception):
    """Erreur pendant la découpe d'un extrait vidéo."""


def _require_ffmpeg() -> str:
    """Retourne le chemin de ffmpeg ou lève une erreur explicite."""
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise CutError(
            "ffmpeg est introuvable dans le PATH. Installez-le puis relancez "
            "(voir la section Installation du README)."
        )
    return ffmpeg


def _positive_time(value: object, name: str) -> float:
    """Convertit une borne temporelle en secondes flottantes valides."""
    try:
        seconds = float(value)
    except (TypeError, ValueError) as exc:
        raise CutError(f"{name} doit être un nombre de secondes, reçu {value!r}.") from exc

    if not math.isfinite(seconds) or seconds < 0:
        raise CutError(f"{name} doit être un nombre positif, reçu {value!r}.")

    return seconds


def cut_clip(
    video: str,
    start: float,
    end: float,
    output: str,
    words: list[dict] | None = None,
    burn_subtitles: bool = True,
    words_relative: bool = False,
) -> str:
    """Découpe le passage ``start`` → ``end`` et le recadre au centre en 1080x1920.

    La vidéo est mise à l'échelle sans déformation pour couvrir le format
    vertical, puis recadrée au centre. Le résultat est encodé en H.264/AAC.

    Si ``words`` est fourni, un fichier ASS est généré avec des temps relatifs
    au début du clip, puis incrusté pendant le même passage ffmpeg.

    Args:
        video: Chemin de la vidéo source.
        start: Début de l'extrait, en secondes.
        end: Fin de l'extrait, en secondes.
        output: Chemin du fichier de sortie.
        words: Segments ou mots issus de la transcription. Accepte la sortie
            de ``transcriber.transcribe``. Par défaut les temps sont absolus.
        burn_subtitles: Si False, ignore ``words`` et ne brûle pas les sous-titres.
        words_relative: Si True, les timestamps de ``words`` sont déjà relatifs
            au début du clip (transcription faite directement sur le clip).

    Returns:
        Le chemin absolu du fichier généré.

    Raises:
        CutError: Si les paramètres sont invalides, si ffmpeg est absent ou si
            l'encodage échoue.
    """
    if not os.path.isfile(video):
        raise CutError(f"Vidéo introuvable : {video!r}")

    start = _positive_time(start, "start")
    end = _positive_time(end, "end")
    if end <= start:
        raise CutError(f"Bornes invalides : start ({start}) doit être inférieur à end ({end}).")

    if os.path.abspath(video) == os.path.abspath(output):
        raise CutError("Le fichier de sortie doit être différent de la vidéo source.")

    ffmpeg = _require_ffmpeg()
    output_path = Path(output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    workdir = output_path.parent.resolve()
    output_abs = output_path.resolve()
    video_abs = Path(video).resolve()

    duration = end - start
    video_filters = [
        (
            f"scale={OUTPUT_WIDTH}:{OUTPUT_HEIGHT}:"
            "force_original_aspect_ratio=increase:flags=lanczos"
        ),
        (
            f"crop={OUTPUT_WIDTH}:{OUTPUT_HEIGHT}:"
            f"(in_w-{OUTPUT_WIDTH})/2:(in_h-{OUTPUT_HEIGHT})/2"
        ),
        "setsar=1",
    ]

    ass_path: Path | None = None
    try:
        if words and burn_subtitles:
            ass_fd, ass_name = tempfile.mkstemp(
                dir=str(workdir), prefix="clipmaker_subs_", suffix=".ass"
            )
            os.close(ass_fd)
            ass_path = Path(ass_name)
            try:
                write_ass(words, start, end, ass_path, words_relative)
            except SubtitleError as exc:
                raise CutError(
                    f"Échec de la génération des sous-titres : {exc}"
                ) from exc
            video_filters.append(f"ass={ass_path.name}")

        command = [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            f"{start:.3f}",
            "-i",
            str(video_abs),
            "-t",
            f"{duration:.3f}",
            "-map",
            "0:v:0",
            "-map",
            "0:a?",
            "-vf",
            ",".join(video_filters),
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "23",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-sn",
            "-dn",
        ]

        if output_path.suffix.lower() in (".mp4", ".m4v"):
            command += ["-movflags", "+faststart"]

        command.append(str(output_abs))

        try:
            subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=str(workdir),
            )
        except FileNotFoundError as exc:
            raise CutError(f"ffmpeg introuvable : {ffmpeg}") from exc
        except subprocess.CalledProcessError as exc:
            details = (exc.stderr or "").strip()
            raise CutError(
                f"Échec de la découpe avec ffmpeg : {details or 'erreur inconnue'}"
            ) from exc
    finally:
        if ass_path is not None:
            try:
                ass_path.unlink()
            except OSError:
                pass

    if not output_abs.is_file() or output_abs.stat().st_size == 0:
        raise CutError(f"ffmpeg n'a produit aucun fichier exploitable : {output!r}")

    return str(output_abs)
