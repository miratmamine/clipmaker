"""Génération et incrustation des sous-titres."""

from __future__ import annotations

import math
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

ASS_WIDTH = 1080
ASS_HEIGHT = 1920
FONT_NAME = "Arial Black"
FONT_SIZE = 96
OUTLINE_SIZE = 6
SHADOW_SIZE = 2
MARGIN_V = 220
MAX_WORDS_PER_CUE = 3
MIN_CUE_DURATION = 0.10


class SubtitleError(Exception):
    """Erreur pendant la génération ou l'incrustation des sous-titres."""


def _number(value: Any, label: str) -> float:
    """Convertit une valeur en nombre fini."""
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise SubtitleError(f"{label} doit être un nombre, reçu {value!r}.") from exc

    if not math.isfinite(number):
        raise SubtitleError(f"{label} doit être un nombre fini, reçu {value!r}.")
    return number


def _distribute_words(text: str, start: float, end: float) -> list[dict]:
    """Répartit les mots d'un segment sans timestamps mot à mot."""
    tokens = text.split()
    if not tokens or end <= start:
        return []

    step = (end - start) / len(tokens)
    return [
        {
            "start": start + index * step,
            "end": start + (index + 1) * step,
            "word": token,
        }
        for index, token in enumerate(tokens)
    ]


def collect_words(segments: list[dict]) -> list[dict]:
    """Aplatit des segments ou des mots en une liste ``{start, end, word}``.

    Accepte la sortie de ``transcriber.transcribe`` (segments avec ``words``),
    une liste de mots déjà à plat, ou des segments sans timestamps de mots
    (répartition uniforme du texte sur la durée du segment).
    """
    if not isinstance(segments, list):
        raise SubtitleError("Les sous-titres attendent une liste de segments ou de mots.")

    words: list[dict] = []
    for index, item in enumerate(segments):
        if not isinstance(item, dict):
            raise SubtitleError(f"Élément {index} invalide : objet attendu.")

        start = _number(item.get("start"), f"start de l'élément {index}")
        end = _number(item.get("end"), f"end de l'élément {index}")

        item_words = item.get("words")
        if isinstance(item_words, list) and item_words:
            for word_index, word in enumerate(item_words):
                if not isinstance(word, dict):
                    raise SubtitleError(
                        f"Mot {word_index} invalide dans l'élément {index}."
                    )
                text = word.get("word", word.get("text"))
                if not isinstance(text, str) or not text.strip():
                    continue
                words.append(
                    {
                        "start": _number(
                            word.get("start"),
                            f"start du mot {word_index} de l'élément {index}",
                        ),
                        "end": _number(
                            word.get("end"),
                            f"end du mot {word_index} de l'élément {index}",
                        ),
                        "word": text.strip(),
                    }
                )
            continue

        text = item.get("word", item.get("text"))
        if not isinstance(text, str) or not text.strip():
            continue

        if "word" in item:
            words.append({"start": start, "end": end, "word": text.strip()})
        else:
            words.extend(_distribute_words(text.strip(), start, end))

    words.sort(key=lambda word: (word["start"], word["end"]))
    return words


def words_for_clip(
    segments: list[dict],
    clip_start: float,
    clip_end: float,
    times_relative: bool = False,
) -> list[dict]:
    """Retourne les mots du passage, avec des temps relatifs au début du clip.

    Par défaut, les timestamps des mots sont absolus et ``clip_start`` est
    soustrait. Avec ``times_relative=True``, ils sont déjà relatifs au début du
    clip (transcription faite sur le clip) et sont conservés tels quels.
    """
    clip_start = _number(clip_start, "clip_start")
    clip_end = _number(clip_end, "clip_end")
    if clip_end <= clip_start:
        raise SubtitleError(
            f"Bornes invalides : clip_start ({clip_start}) doit être inférieur "
            f"à clip_end ({clip_end})."
        )

    if times_relative:
        lower = 0.0
        upper = clip_end - clip_start
        offset = 0.0
    else:
        lower = clip_start
        upper = clip_end
        offset = clip_start

    relative: list[dict] = []
    for word in collect_words(segments):
        if word["end"] <= lower or word["start"] >= upper:
            continue

        start = max(word["start"], lower) - offset
        end = min(word["end"], upper) - offset
        if end <= start:
            continue

        relative.append({"start": start, "end": end, "word": word["word"]})

    return relative


def group_words(words: list[dict], max_words: int = MAX_WORDS_PER_CUE) -> list[list[dict]]:
    """Regroupe les mots par paquets de 2 à 4 pour l'affichage."""
    if max_words < 2 or max_words > 4:
        raise SubtitleError("max_words doit être compris entre 2 et 4.")

    chunks = [words[index : index + max_words] for index in range(0, len(words), max_words)]
    if len(chunks) >= 2 and len(chunks[-1]) == 1:
        if len(chunks[-2]) < 4:
            chunks[-2].extend(chunks.pop())
        else:
            chunks[-1].insert(0, chunks[-2].pop())
    return chunks


def _ass_time(seconds: float) -> str:
    """Formate un temps en notation ASS (heures:minutes:secondes.centièmes)."""
    centiseconds = max(0, int(round(seconds * 100)))
    hours, remainder = divmod(centiseconds, 360_000)
    minutes, remainder = divmod(remainder, 6_000)
    whole_seconds, centiseconds = divmod(remainder, 100)
    return f"{hours}:{minutes:02d}:{whole_seconds:02d}.{centiseconds:02d}"


def _escape_ass(text: str) -> str:
    """Neutralise les caractères spéciaux du format ASS."""
    text = " ".join(text.split())
    text = text.replace("\\", "\\\\").replace("{", "(").replace("}", ")")
    return text.replace("\n", "\\N")


_NO_SPACE_BEFORE = set("'’.,;:!?%…)]}»")
_NO_SPACE_AFTER = set("'’([{«")


def _join_words(words: list[dict]) -> str:
    """Assemble les mots en évitant les espaces autour des apostrophes."""
    text = ""
    for word in words:
        token = word["word"]
        if not text:
            text = token
        elif token[:1] in _NO_SPACE_BEFORE or text[-1:] in _NO_SPACE_AFTER:
            text += token
        else:
            text += " " + token
    return text


def build_ass(
    segments: list[dict],
    clip_start: float,
    clip_end: float,
    times_relative: bool = False,
) -> str:
    """Construit le contenu d'un fichier ASS pour un extrait.

    Args:
        segments: Segments ou mots issus de la transcription.
        clip_start: Début du clip dans la vidéo source.
        clip_end: Fin du clip dans la vidéo source.
        times_relative: Si True, les timestamps des mots sont déjà relatifs au
            début du clip (transcription faite directement sur le clip).

    Returns:
        Le contenu complet du fichier ASS.

    Raises:
        SubtitleError: Si aucun mot ne tombe dans le passage.
    """
    words = words_for_clip(segments, clip_start, clip_end, times_relative)
    if not words:
        raise SubtitleError("Aucun mot à sous-titrer dans ce passage.")

    header = f"""\
[Script Info]
ScriptType: v4.00+
PlayResX: {ASS_WIDTH}
PlayResY: {ASS_HEIGHT}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{FONT_NAME},{FONT_SIZE},&H00FFFFFF,&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,{OUTLINE_SIZE},{SHADOW_SIZE},2,60,60,{MARGIN_V},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    events = []
    previous_end = 0.0
    for chunk in group_words(words):
        start = max(chunk[0]["start"], previous_end)
        end = max(chunk[-1]["end"], start + MIN_CUE_DURATION)
        text = _escape_ass(_join_words(chunk))
        events.append(
            f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},Default,,0,0,0,,{text}"
        )
        previous_end = end

    return header + "\n".join(events) + "\n"


def write_ass(
    segments: list[dict],
    clip_start: float,
    clip_end: float,
    output_path: str | os.PathLike,
    times_relative: bool = False,
) -> str:
    """Écrit le fichier ASS et retourne son chemin absolu.

    ``times_relative=True`` indique que les timestamps des mots sont déjà
    relatifs au début du clip.
    """
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.write_text(
            build_ass(segments, clip_start, clip_end, times_relative),
            encoding="utf-8",
        )
    except OSError as exc:
        raise SubtitleError(f"Impossible d'écrire le fichier ASS {str(path)!r} : {exc}") from exc
    return str(path.resolve())


def add_subtitles(
    clip_path: str,
    segments: list[dict],
    output_path: str,
    start: float = 0.0,
    end: float | None = None,
    times_relative: bool = False,
) -> str:
    """Incruste des sous-titres dans un clip déjà découpé.

    Args:
        clip_path: Chemin du clip à sous-titrer.
        segments: Segments ou mots horodatés.
        output_path: Chemin du fichier de sortie.
        start: Début du clip dans la vidéo source (pour rendre les temps relatifs).
        end: Fin du clip dans la vidéo source. Par défaut, fin du dernier mot.
        times_relative: Si True, les timestamps des mots sont déjà relatifs au
            début du clip (transcription faite directement sur le clip).

    Returns:
        Le chemin absolu du clip sous-titré.
    """
    if not os.path.isfile(clip_path):
        raise SubtitleError(f"Clip introuvable : {clip_path!r}")

    if end is None:
        words = collect_words(segments)
        if not words:
            raise SubtitleError("Aucun mot à sous-titrer.")
        end = max(word["end"] for word in words)

    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise SubtitleError("ffmpeg est introuvable dans le PATH.")

    clip = Path(clip_path).resolve()
    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target = target.resolve()

    if clip == target:
        raise SubtitleError("Le fichier de sortie doit être différent du clip source.")

    ass_fd, ass_name = tempfile.mkstemp(
        dir=str(target.parent), prefix="clipmaker_subs_", suffix=".ass"
    )
    os.close(ass_fd)
    ass_path = Path(ass_name)

    try:
        write_ass(segments, start, float(end), ass_path, times_relative)
        command = [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(clip),
            "-vf",
            f"ass={ass_path.name}",
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "23",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "copy",
            "-sn",
            "-dn",
        ]
        if target.suffix.lower() in (".mp4", ".m4v"):
            command += ["-movflags", "+faststart"]
        command.append(str(target))

        subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(target.parent),
        )
    except subprocess.CalledProcessError as exc:
        details = (exc.stderr or "").strip()
        raise SubtitleError(
            f"Échec de l'incrustation avec ffmpeg : {details or 'erreur inconnue'}"
        ) from exc
    finally:
        try:
            ass_path.unlink()
        except OSError:
            pass

    if not target.is_file() or target.stat().st_size == 0:
        raise SubtitleError(f"ffmpeg n'a produit aucun fichier exploitable : {str(target)!r}")

    return str(target)
