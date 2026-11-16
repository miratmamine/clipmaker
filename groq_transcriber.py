"""Transcription dans le cloud via l'API gratuite de Groq.

Groq expose un endpoint compatible OpenAI
(``/openai/v1/audio/transcriptions``) qui accepte des fichiers de 25 Mo
maximum. L'audio est donc découpé en morceaux (10 minutes par défaut),
transcrit morceau par morceau, puis les timestamps sont recalés sur la vidéo
complète.

Ce module fournit la même interface que :mod:`transcriber` pour le pipeline :
``transcribe(video, json_path)`` et un helper ``slice_segments`` qui extrait les
mots d'un passage avec des temps relatifs (le pipeline n'a alors pas besoin de
relancer un appel API par clip).
"""

from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import httpx

from subtitles import SubtitleError, collect_words
from transcriber import TranscriptionError

GROQ_URL = os.getenv(
    "GROQ_URL", "https://api.groq.com/openai/v1/audio/transcriptions"
)
DEFAULT_MODEL = "whisper-large-v3-turbo"
DEFAULT_CHUNK_SECONDS = 600
DEFAULT_RPM = 20
MAX_CHUNK_BYTES = 24 * 1024 * 1024
MAX_ATTEMPTS = 5
WORK_DIR = os.getenv("CLIPMAKER_WORK_DIR", "work")

_throttle_lock = threading.Lock()
_last_request_at = 0.0


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


def _chunk_seconds() -> int:
    value = _env_int("GROQ_CHUNK_SECONDS", DEFAULT_CHUNK_SECONDS)
    return max(1, min(value, 900))


def _min_interval() -> float:
    explicit = os.getenv("GROQ_MIN_INTERVAL", "").strip()
    if explicit:
        return max(0.0, _env_float("GROQ_MIN_INTERVAL", 0.0))
    rpm = max(1, _env_int("GROQ_RPM", DEFAULT_RPM))
    return 60.0 / rpm


def _throttle() -> None:
    """Espace les appels pour rester sous la limite de requêtes/minute."""
    interval = _min_interval()
    if interval <= 0:
        return

    global _last_request_at
    with _throttle_lock:
        wait = _last_request_at + interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_request_at = time.monotonic()


def _api_key() -> str:
    key = os.getenv("GROQ_API_KEY", "").strip()
    if not key:
        raise TranscriptionError(
            "Clé Groq manquante : renseignez GROQ_API_KEY dans les secrets du "
            "service (https://console.groq.com/keys)."
        )
    return key


def _model() -> str:
    return os.getenv("GROQ_WHISPER_MODEL", "").strip() or DEFAULT_MODEL


def _number(value: object, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(number):
        return default
    return number


def _retry_after(response: httpx.Response) -> float:
    raw = response.headers.get("retry-after", "").strip()
    if not raw:
        return 0.0
    try:
        return max(0.0, min(float(raw), 120.0))
    except ValueError:
        return 0.0


def _error_detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return (response.text or "").strip()[:300] or "réponse vide"

    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])[:300]
        if error:
            return str(error)[:300]
        if payload.get("message"):
            return str(payload["message"])[:300]
    return str(payload)[:300]


def _request_chunk(path: Path, prompt: str = "") -> dict:
    """Envoie un morceau audio à Groq et retourne la réponse verbose_json."""
    key = _api_key()
    headers = {"Authorization": f"Bearer {key}"}
    data: dict[str, object] = {
        "model": _model(),
        "response_format": "verbose_json",
        "timestamp_granularities[]": ["word", "segment"],
        "temperature": "0",
    }
    language = os.getenv("GROQ_LANGUAGE", "").strip()
    if language:
        data["language"] = language
    if prompt:
        data["prompt"] = prompt[-200:]

    last_error = ""
    for attempt in range(MAX_ATTEMPTS):
        _throttle()
        try:
            with path.open("rb") as file:
                response = httpx.post(
                    GROQ_URL,
                    headers=headers,
                    data=data,
                    files={"file": (path.name, file, "audio/mpeg")},
                    timeout=httpx.Timeout(300.0, connect=30.0),
                )
        except httpx.HTTPError as exc:
            last_error = f"erreur réseau : {exc}"
            time.sleep(min(20.0, 2.0**attempt))
            continue

        if response.status_code == 200:
            try:
                payload = response.json()
            except ValueError as exc:
                raise TranscriptionError(
                    "Groq a renvoyé une réponse illisible (JSON invalide)."
                ) from exc
            if not isinstance(payload, dict):
                raise TranscriptionError("Groq a renvoyé une réponse inattendue.")
            return payload

        if response.status_code == 429:
            wait = _retry_after(response) or min(60.0, 5.0 * (attempt + 1))
            last_error = "limite de débit atteinte (HTTP 429)"
            print(f"      Groq : {last_error}, nouvelle tentative dans {wait:.0f}s...")
            time.sleep(wait)
            continue

        if response.status_code >= 500:
            last_error = f"erreur serveur HTTP {response.status_code}"
            time.sleep(min(20.0, 2.0**attempt))
            continue

        detail = _error_detail(response)
        granularities = data.get("timestamp_granularities[]")
        if (
            response.status_code == 400
            and isinstance(granularities, list)
            and "word" in granularities
            and "timestamp" in detail.lower()
        ):
            # Certains modèles Groq n'acceptent que les timestamps de segment.
            data["timestamp_granularities[]"] = ["segment"]
            last_error = f"timestamps par mot refusés ({detail})"
            print("      Groq : repli sur les timestamps de segment.")
            continue

        raise TranscriptionError(
            f"Groq a refusé l'audio (HTTP {response.status_code}) : {detail}"
        )

    raise TranscriptionError(
        f"Échec de la transcription Groq après {MAX_ATTEMPTS} tentatives : "
        f"{last_error or 'raison inconnue'}"
    )


def _require_binary(name: str) -> str:
    binary = shutil.which(name)
    if binary is None:
        raise TranscriptionError(
            f"{name} est introuvable dans le PATH (requis pour préparer l'audio)."
        )
    return binary


def _duration(video_path: str) -> float:
    """Durée de la vidéo en secondes (via ffprobe)."""
    ffprobe = _require_binary("ffprobe")
    try:
        result = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                video_path,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as exc:
        raise TranscriptionError(f"ffprobe introuvable : {exc}") from exc

    if result.returncode != 0:
        details = (result.stderr or "").strip()
        raise TranscriptionError(
            f"Impossible de lire la durée de {video_path!r} : "
            f"{details or 'erreur inconnue'}"
        )

    try:
        duration = float((result.stdout or "").strip())
    except ValueError as exc:
        raise TranscriptionError(
            f"Durée illisible pour {video_path!r} : {result.stdout!r}"
        ) from exc

    if not math.isfinite(duration) or duration <= 0:
        raise TranscriptionError(f"Durée invalide pour {video_path!r} : {duration}")

    return duration


def _extract_chunk(video: str, start: float, length: float, target: Path) -> None:
    """Extrait un morceau mono 16 kHz en MP3 64 kb/s (≈ 4,8 Mo / 10 min)."""
    ffmpeg = _require_binary("ffmpeg")
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        f"{start:.3f}",
        "-t",
        f"{length:.3f}",
        "-i",
        video,
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "libmp3lame",
        "-b:a",
        "64k",
        str(target),
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as exc:
        raise TranscriptionError(f"ffmpeg introuvable : {exc}") from exc

    if result.returncode != 0:
        details = (result.stderr or "").strip()
        raise TranscriptionError(
            f"Échec de l'extraction audio ({start:.0f}s → {start + length:.0f}s) : "
            f"{details or 'erreur inconnue'}"
        )

    if not target.is_file() or target.stat().st_size == 0:
        raise TranscriptionError(
            f"ffmpeg n'a produit aucun audio exploitable pour {start:.0f}s."
        )
    if target.stat().st_size > MAX_CHUNK_BYTES:
        raise TranscriptionError(
            "Le morceau audio dépasse la limite de 25 Mo de Groq. "
            "Réduisez GROQ_CHUNK_SECONDS."
        )


def _words_from_chunk(payload: dict, offset: float) -> list[dict]:
    words: list[dict] = []
    for item in payload.get("words") or []:
        if not isinstance(item, dict):
            continue
        text = str(item.get("word", item.get("text", ""))).strip()
        if not text:
            continue
        start = _number(item.get("start"))
        end = _number(item.get("end"), start + 0.05)
        if end <= start:
            end = start + 0.05
        words.append(
            {
                "start": round(start + offset, 3),
                "end": round(end + offset, 3),
                "word": text,
            }
        )
    words.sort(key=lambda word: (word["start"], word["end"]))
    return words


def _segments_from_chunk(payload: dict, offset: float, words: list[dict]) -> list[dict]:
    raw_segments = payload.get("segments")
    segments: list[dict] = []

    if isinstance(raw_segments, list):
        for item in raw_segments:
            if not isinstance(item, dict):
                continue
            start = _number(item.get("start"))
            end = _number(item.get("end"), start)
            if end <= start:
                continue
            segments.append(
                {
                    "start": round(start + offset, 3),
                    "end": round(end + offset, 3),
                    "text": str(item.get("text", "")).strip(),
                }
            )

    if not segments:
        text = str(payload.get("text", "")).strip()
        if words:
            segments = [
                {
                    "start": words[0]["start"],
                    "end": words[-1]["end"],
                    "text": text,
                }
            ]
        elif text:
            duration = max(0.1, _number(payload.get("duration"), 0.1))
            segments = [{"start": offset, "end": offset + duration, "text": text}]

    if words and segments:
        # Rattache chaque mot au segment qui contient son milieu (ou au plus
        # proche) pour que les sous-titres ne perdent aucun mot.
        for word in words:
            middle = (word["start"] + word["end"]) / 2
            target = None
            for segment in segments:
                if segment["start"] <= middle <= segment["end"]:
                    target = segment
                    break
            if target is None:
                target = min(
                    segments,
                    key=lambda segment: abs(
                        (segment["start"] + segment["end"]) / 2 - middle
                    ),
                )
            target.setdefault("words", []).append(word)

    return segments


def _load_json(path: Path) -> list[dict] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(data, list):
        return None
    for item in data:
        if not isinstance(item, dict) or not {"start", "end", "text"} <= item.keys():
            return None
    return data


def _save_json(path: Path, segments: list[dict]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(segments, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        raise TranscriptionError(
            f"Impossible d'écrire la transcription {str(path)!r} : {exc}"
        ) from exc


def transcribe(
    video_path: str, json_path: str | os.PathLike | None = None
) -> list[dict]:
    """Transcrit une vidéo via Groq et met le résultat en cache JSON.

    Args:
        video_path: Chemin de la vidéo (ou de l'audio) à transcrire.
        json_path: Chemin du cache JSON. Par défaut
            ``WORK_DIR/<nom_de_la_video>.json``.

    Returns:
        Des segments ``{"start", "end", "text", "words"}`` avec des timestamps
        absolus dans la vidéo.
    """
    source = Path(video_path)
    if not source.is_file():
        raise TranscriptionError(f"Fichier introuvable : {video_path!r}")

    try:
        Path(WORK_DIR).mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise TranscriptionError(
            f"Impossible de créer le dossier de travail {WORK_DIR!r} : {exc}"
        ) from exc

    target = (
        Path(json_path)
        if json_path is not None
        else Path(WORK_DIR) / f"{source.stem}.json"
    )
    cached = _load_json(target)
    if cached is not None:
        return cached

    duration = _duration(str(source))
    chunk_seconds = _chunk_seconds()
    total = max(1, math.ceil(duration / chunk_seconds))
    segments: list[dict] = []
    prompt = ""

    print(
        f"      Groq ({_model()}) : {duration:.0f}s d'audio en {total} morceau(x) "
        f"de {chunk_seconds}s maximum."
    )

    for index in range(total):
        start = index * chunk_seconds
        length = min(chunk_seconds, duration - start)
        if length <= 0.05:
            break

        with tempfile.TemporaryDirectory(
            dir=str(Path(WORK_DIR)), prefix="groq_chunk_"
        ) as tmp:
            chunk = Path(tmp) / f"chunk_{index:03d}.mp3"
            _extract_chunk(str(source), start, length, chunk)
            payload = _request_chunk(chunk, prompt)

        words = _words_from_chunk(payload, start)
        segments.extend(_segments_from_chunk(payload, start, words))
        text = str(payload.get("text", "")).strip()
        if text:
            prompt = text
        print(
            f"      Groq : morceau {index + 1}/{total} transcrit "
            f"({len(words)} mots)."
        )

    if not segments:
        raise TranscriptionError(
            "Groq n'a détecté aucune parole dans cette vidéo (audio vide ou muet)."
        )

    segments.sort(key=lambda segment: (segment["start"], segment["end"]))
    _save_json(target, segments)
    return segments


def slice_segments(
    segments: list[dict], start: float, end: float
) -> list[dict]:
    """Extrait d'une transcription les mots d'un passage, en temps relatifs.

    Le pipeline transcrit la vidéo une seule fois puis découpe les clips : aucun
    appel Groq supplémentaire n'est nécessaire pour les sous-titres.

    Args:
        segments: Transcription complète (timestamps absolus).
        start: Début du clip, en secondes.
        end: Fin du clip, en secondes.

    Returns:
        Une liste de mots ``{"start", "end", "word"}`` avec ``start`` à 0 pour
        le début du clip. Si les timestamps de mots manquent, retourne les
        segments recadrés (les sous-titres répartiront le texte).
    """
    try:
        start = float(start)
        end = float(end)
    except (TypeError, ValueError) as exc:
        raise TranscriptionError(f"Bornes de clip invalides : {start!r} → {end!r}") from exc

    if not (math.isfinite(start) and math.isfinite(end)) or end <= start:
        raise TranscriptionError(f"Bornes de clip invalides : {start} → {end}")

    try:
        words = collect_words(segments)
    except SubtitleError as exc:
        raise TranscriptionError(f"Transcription illisible : {exc}") from exc

    relative: list[dict] = []
    for word in words:
        if word["end"] <= start or word["start"] >= end:
            continue
        word_start = max(word["start"], start) - start
        word_end = min(word["end"], end) - start
        if word_end <= word_start:
            continue
        relative.append(
            {
                "start": round(word_start, 3),
                "end": round(word_end, 3),
                "word": word["word"],
            }
        )

    if relative:
        return relative

    # Repli : pas de timestamps mot à mot, on renvoie les segments recadrés.
    sliced: list[dict] = []
    for segment in segments:
        if not isinstance(segment, dict):
            continue
        seg_start = _number(segment.get("start"))
        seg_end = _number(segment.get("end"), seg_start)
        if seg_end <= start or seg_start >= end:
            continue
        piece_start = max(seg_start, start) - start
        piece_end = min(seg_end, end) - start
        if piece_end <= piece_start:
            continue
        sliced.append(
            {
                "start": round(piece_start, 3),
                "end": round(piece_end, 3),
                "text": str(segment.get("text", "")).strip(),
            }
        )

    return sliced
