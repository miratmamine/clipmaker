"""Sélection des extraits les plus pertinents."""

from __future__ import annotations

import json
import math
import os
import re
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI, OpenAIError

ENV_PATH = Path(__file__).with_name(".env")
load_dotenv(ENV_PATH)

DEFAULT_MODEL = "gpt-4o-mini"
MIN_CLIP_DURATION = 30
MAX_CLIP_DURATION = 60

SYSTEM_PROMPT = """\
Tu es un monteur vidéo professionnel spécialisé dans les extraits viraux \
(TikTok, YouTube Shorts, Reels). Tu analyses une transcription horodatée et \
tu sélectionnes les passages les plus captivants.

Règles de réponse :
- Réponds UNIQUEMENT avec un tableau JSON valide.
- Aucun texte avant ou après le tableau, aucune balise Markdown.
- Chaque élément contient exactement les clés : start, end, title, score.
"""

_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


class SelectionError(Exception):
    """Erreur pendant la sélection des extraits."""


def _format_transcript(segments: list[dict]) -> str:
    """Met en forme les segments horodatés pour le prompt."""
    if not segments:
        raise SelectionError("La transcription est vide : aucun extrait à sélectionner.")

    lines = []
    for index, segment in enumerate(segments):
        try:
            start = float(segment["start"])
            end = float(segment["end"])
            text = str(segment["text"]).strip()
        except (KeyError, TypeError, ValueError) as exc:
            raise SelectionError(
                f"Segment invalide à l'index {index} : {segment!r}"
            ) from exc

        if not (math.isfinite(start) and math.isfinite(end)) or end <= start:
            raise SelectionError(
                f"Bornes invalides pour le segment {index} : {start} -> {end}"
            )

        lines.append(f"[{start:.2f}s -> {end:.2f}s] {text}")

    return "\n".join(lines)


def _build_prompt(transcript: str, n: int) -> str:
    """Construit le prompt utilisateur demandant n extraits."""
    return f"""\
Voici la transcription horodatée d'une vidéo (temps en secondes) :

<transcription>
{transcript}
</transcription>

Sélectionne {n} extraits parmi les plus intéressants de cette vidéo. Si la \
transcription est trop courte pour produire {n} extraits valides, retourne le \
maximum possible.

Contraintes :
- Chaque extrait dure entre {MIN_CLIP_DURATION} et {MAX_CLIP_DURATION} secondes \
(end - start).
- Les extraits ne se chevauchent pas.
- Commence et termine si possible sur une phrase complète.
- "title" est un titre accrocheur en français, de 80 caractères maximum.
- "score" est un entier de 1 à 10 mesurant l'intérêt et le potentiel viral.
- Trie les extraits par score décroissant.

Format de réponse strict (tableau JSON uniquement) :
[{{"start": 12.5, "end": 47.2, "title": "Titre accrocheur", "score": 9}}]
"""


def _request_clips(transcript: str, n: int) -> str:
    """Envoie la transcription à l'API LLM et retourne la réponse brute."""
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise SelectionError(
            "Clé API manquante : renseignez OPENAI_API_KEY dans "
            f"{ENV_PATH} (ou dans les variables d'environnement)."
        )

    model = os.getenv("OPENAI_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL
    base_url = os.getenv("OPENAI_BASE_URL", "").strip() or None

    try:
        client = OpenAI(api_key=api_key, base_url=base_url)
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": _build_prompt(transcript, n)},
            ],
            temperature=0.3,
        )
    except OpenAIError as exc:
        raise SelectionError(f"Erreur de l'API LLM : {exc}") from exc

    if not response.choices:
        raise SelectionError("L'API LLM n'a renvoyé aucune réponse.")

    content = response.choices[0].message.content
    if not content or not content.strip():
        raise SelectionError("L'API LLM a renvoyé une réponse vide.")

    return content


def _extract_json_array(raw: str) -> list:
    """Extrait un tableau JSON de la réponse, même entourée de texte ou de fences."""
    text = _FENCE_RE.sub("", raw.strip())

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("[")
        end = text.rfind("]")
        if start == -1 or end <= start:
            raise SelectionError(
                "Réponse du LLM invalide : aucun tableau JSON trouvé."
            )
        try:
            parsed = json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            raise SelectionError(
                f"Réponse du LLM invalide : JSON mal formé ({exc.msg}, "
                f"position {exc.pos})."
            ) from exc

    if not isinstance(parsed, list):
        raise SelectionError(
            "Réponse du LLM invalide : un tableau JSON était attendu."
        )
    if not parsed:
        raise SelectionError("Le LLM n'a sélectionné aucun extrait.")

    return parsed


def _normalize_clip(item: object, index: int) -> dict:
    """Valide et normalise un extrait renvoyé par le LLM."""
    if not isinstance(item, dict):
        raise SelectionError(
            f"Extrait {index} invalide : un objet JSON était attendu."
        )

    missing = {"start", "end", "title", "score"} - item.keys()
    if missing:
        raise SelectionError(
            f"Extrait {index} invalide : clés manquantes {sorted(missing)}."
        )

    try:
        start = float(item["start"])
        end = float(item["end"])
        score = float(item["score"])
    except (TypeError, ValueError) as exc:
        raise SelectionError(
            f"Extrait {index} invalide : start, end et score doivent être numériques."
        ) from exc

    if not all(math.isfinite(value) for value in (start, end, score)):
        raise SelectionError(f"Extrait {index} invalide : valeurs non finies.")
    if start < 0 or end <= start:
        raise SelectionError(
            f"Extrait {index} invalide : bornes incohérentes ({start} -> {end})."
        )

    title = item["title"]
    if not isinstance(title, str) or not title.strip():
        raise SelectionError(
            f"Extrait {index} invalide : le titre doit être une chaîne non vide."
        )

    score = min(10.0, max(1.0, score))
    if score.is_integer():
        score = int(score)

    return {
        "start": round(start, 3),
        "end": round(end, 3),
        "title": title.strip(),
        "score": score,
    }


def _parse_clips(raw: str, n: int) -> list[dict]:
    """Parse et valide la réponse JSON du LLM."""
    items = _extract_json_array(raw)
    clips = [_normalize_clip(item, index) for index, item in enumerate(items)]
    clips.sort(key=lambda clip: clip["score"], reverse=True)
    return clips[:n]


def select_clips(segments: list[dict], n: int = 5) -> list[dict]:
    """Sélectionne les extraits les plus intéressants à partir de la transcription.

    La transcription est envoyée à un LLM (API OpenAI par défaut, configurable
    via ``OPENAI_MODEL`` et ``OPENAI_BASE_URL``) qui doit répondre uniquement
    avec un tableau JSON de la forme
    ``[{"start": ..., "end": ..., "title": ..., "score": ...}]``.

    Args:
        segments: Segments horodatés issus de la transcription
            (``{"start", "end", "text"}``).
        n: Nombre maximum d'extraits à retenir.

    Returns:
        Une liste d'extraits ``{"start", "end", "title", "score"}`` triés par
        score décroissant (au plus ``n``).

    Raises:
        SelectionError: Si la clé API est absente, si l'appel échoue, ou si la
            réponse du LLM n'est pas un JSON exploitable.
    """
    if not isinstance(n, int) or n < 1:
        raise SelectionError(f"n doit être un entier strictement positif, reçu {n!r}.")

    transcript = _format_transcript(segments)
    raw_response = _request_clips(transcript, n)
    return _parse_clips(raw_response, n)
