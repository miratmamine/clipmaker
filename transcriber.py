"""Transcription audio des vidéos sources."""

from __future__ import annotations

import json
import os
from functools import lru_cache

from faster_whisper import WhisperModel

MODEL_SIZE = "small"
WORK_DIR = "work"


class TranscriptionError(Exception):
    """Erreur pendant la transcription d'une vidéo."""


@lru_cache(maxsize=1)
def _load_model() -> WhisperModel:
    """Charge le modèle faster-whisper une seule fois par processus."""
    try:
        return WhisperModel(MODEL_SIZE, device="auto", compute_type="auto")
    except Exception as exc:
        raise TranscriptionError(
            f"Impossible de charger le modèle Whisper {MODEL_SIZE!r} : {exc}"
        ) from exc


def _segment_to_dict(segment) -> dict:
    """Convertit un segment faster-whisper en dictionnaire sérialisable."""
    data = {
        "start": round(float(segment.start), 3),
        "end": round(float(segment.end), 3),
        "text": segment.text.strip(),
    }

    words = getattr(segment, "words", None)
    if words:
        data["words"] = [
            {
                "start": round(float(word.start), 3),
                "end": round(float(word.end), 3),
                "word": word.word.strip(),
            }
            for word in words
        ]

    return data


def _json_path(video_path: str) -> str:
    """Construit le chemin du JSON de transcription dans ./work/."""
    stem = os.path.splitext(os.path.basename(video_path))[0]
    return os.path.join(WORK_DIR, f"{stem}.json")


def transcription_path(video_path: str) -> str:
    """Retourne le chemin du JSON de transcription associé à une vidéo."""
    return _json_path(video_path)


def transcribe(
    video_path: str, json_path: str | os.PathLike | None = None
) -> list[dict]:
    """Transcrit la piste audio d'une vidéo en segments horodatés.

    Les timestamps au niveau des mots sont demandés à Whisper ; lorsqu'ils sont
    disponibles, chaque segment contient en plus une clé ``words``.

    Le résultat est sauvegardé dans ``./work/<nom_de_la_video>.json`` (ou dans
    ``json_path`` si fourni).

    Args:
        video_path: Chemin de la vidéo à transcrire.
        json_path: Chemin de sauvegarde explicite du JSON. Utile pour les
            transcriptions de clips temporaires.

    Returns:
        Une liste de segments ``{"start", "end", "text"}``, avec éventuellement
        ``"words"`` pour les timestamps par mot.

    Raises:
        TranscriptionError: Si le fichier est introuvable ou si la
            transcription échoue.
    """
    if not os.path.isfile(video_path):
        raise TranscriptionError(f"Fichier introuvable : {video_path!r}")

    model = _load_model()

    try:
        raw_segments, _info = model.transcribe(
            video_path,
            word_timestamps=True,
            vad_filter=True,
        )
        segments = [_segment_to_dict(segment) for segment in raw_segments]
    except Exception as exc:
        raise TranscriptionError(
            f"Échec de la transcription de {video_path!r} : {exc}"
        ) from exc

    _save_json(video_path, segments, json_path)
    return segments


def _save_json(
    video_path: str,
    segments: list[dict],
    json_path: str | os.PathLike | None = None,
) -> str:
    """Écrit les segments au format JSON dans ./work/."""
    path = os.fspath(json_path) if json_path is not None else _json_path(video_path)
    try:
        os.makedirs(WORK_DIR, exist_ok=True)
        parent = os.path.dirname(os.path.abspath(path))
        os.makedirs(parent, exist_ok=True)
        with open(path, "w", encoding="utf-8") as file:
            json.dump(segments, file, ensure_ascii=False, indent=2)
            file.write("\n")
        return path
    except OSError as exc:
        raise TranscriptionError(
            f"Impossible d'écrire la transcription {path!r} : {exc}"
        ) from exc
