"""Test manuel de ``cutter.cut_clip``.

Prend la première vidéo trouvée dans ``./work/`` et le premier extrait d'un
JSON de sélection présent dans le même dossier. Si aucun JSON exploitable
n'est trouvé, utilise le passage 60 s -> 100 s.

Exemples :
    python test_cutter.py
    python test_cutter.py --json work/selection.json --index 1
    python test_cutter.py --start 10 --end 45
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from cutter import CutError, cut_clip

BASE_DIR = Path(__file__).resolve().parent
WORK_DIR = BASE_DIR / "work"
DEFAULT_OUTPUT = WORK_DIR / "test_cut.mp4"
DEFAULT_START = 60.0
DEFAULT_END = 100.0
VIDEO_SUFFIXES = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v"}


def find_video(explicit: Path | None) -> Path:
    """Retourne la vidéo à découper."""
    if explicit is not None:
        if not explicit.is_file():
            raise SystemExit(f"Vidéo introuvable : {explicit}")
        return explicit

    if not WORK_DIR.is_dir():
        raise SystemExit(
            f"Dossier {WORK_DIR} introuvable. Téléchargez d'abord une vidéo "
            "(python main.py <url>)."
        )

    videos = [
        path
        for path in WORK_DIR.iterdir()
        if path.is_file()
        and path.suffix.lower() in VIDEO_SUFFIXES
        and not path.name.startswith("test_cut")
    ]
    if not videos:
        raise SystemExit(
            f"Aucune vidéo trouvée dans {WORK_DIR}. Téléchargez-en une avec "
            "python main.py <url> ou passez --video."
        )

    return max(videos, key=lambda path: path.stat().st_mtime)


def _load_json(path: Path) -> Any:
    """Charge un JSON, ou retourne None s'il est illisible."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _has_bounds(item: dict) -> bool:
    """Vérifie qu'un élément contient des bornes start/end exploitables."""
    try:
        return float(item["end"]) > float(item["start"]) >= 0
    except (KeyError, TypeError, ValueError):
        return False


def _clips_from(data: Any) -> list[dict]:
    """Extrait une liste d'extraits d'un JSON (liste brute ou objet avec clips)."""
    if isinstance(data, dict):
        if isinstance(data.get("clips"), list):
            data = data["clips"]
        elif "start" in data and "end" in data:
            data = [data]
        else:
            return []

    if not isinstance(data, list):
        return []

    return [item for item in data if isinstance(item, dict) and _has_bounds(item)]


def find_clip(
    index: int, explicit: Path | None
) -> tuple[float, float, Path] | None:
    """Cherche un passage dans un JSON de sélection.

    En recherche automatique, seuls les JSON contenant des extraits avec
    ``title`` ou ``score`` sont retenus, pour ignorer les transcriptions.
    """
    if explicit is not None:
        if not explicit.is_file():
            raise SystemExit(f"Fichier JSON introuvable : {explicit}")
        candidates = [explicit]
        require_metadata = False
    else:
        if not WORK_DIR.is_dir():
            return None
        candidates = sorted(
            WORK_DIR.glob("*.json"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        require_metadata = True

    for path in candidates:
        clips = _clips_from(_load_json(path))
        if require_metadata:
            clips = [
                clip for clip in clips if "title" in clip or "score" in clip
            ]
        if not clips:
            continue

        if index >= len(clips):
            raise SystemExit(
                f"{path.name} ne contient que {len(clips)} extrait(s) ; "
                f"index {index} invalide."
            )

        clip = clips[index]
        print(
            f"Passage lu depuis {path.name} : "
            f"{clip.get('title', 'sans titre')!r} "
            f"[{clip['start']}, {clip['end']}]"
        )
        return float(clip["start"]), float(clip["end"]), path

    return None


def probe(output: Path) -> tuple[float, str]:
    """Retourne la durée et la résolution du fichier via ffprobe."""
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        raise SystemExit("ffprobe est introuvable dans le PATH.")

    command = [
        ffprobe,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height:format=duration",
        "-of",
        "json",
        str(output),
    ]

    try:
        result = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        info = json.loads(result.stdout)
        stream = info["streams"][0]
        duration = float(info["format"]["duration"])
        resolution = f"{int(stream['width'])}x{int(stream['height'])}"
    except (
        subprocess.CalledProcessError,
        KeyError,
        IndexError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
    ) as exc:
        raise SystemExit(f"Impossible de lire le fichier produit avec ffprobe : {exc}") from exc

    return duration, resolution


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse les arguments de ligne de commande."""
    parser = argparse.ArgumentParser(
        description="Découpe un extrait vertical 1080x1920 et vérifie le résultat."
    )
    parser.add_argument("--video", type=Path, help="vidéo source (défaut : ./work/)")
    parser.add_argument(
        "--json",
        type=Path,
        help="JSON de sélection (défaut : recherche dans ./work/)",
    )
    parser.add_argument(
        "--index",
        type=int,
        default=0,
        help="index de l'extrait dans le JSON (défaut : 0)",
    )
    parser.add_argument("--start", type=float, help="début imposé, en secondes")
    parser.add_argument("--end", type=float, help="fin imposée, en secondes")
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"fichier de sortie (défaut : {DEFAULT_OUTPUT})",
    )
    args = parser.parse_args(argv)

    if (args.start is None) != (args.end is None):
        parser.error("--start et --end doivent être fournis ensemble.")
    if args.index < 0:
        parser.error("--index doit être positif ou nul.")

    return args


def _configure_console() -> None:
    """Évite un crash d'encodage sur les titres Unicode (console Windows)."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(errors="replace")
            except (ValueError, OSError):
                pass


def main(argv: list[str] | None = None) -> int:
    """Exécute le test de découpe et affiche les métadonnées du fichier produit."""
    _configure_console()
    args = parse_args(argv)
    video = find_video(args.video)

    if args.start is not None:
        start, end = args.start, args.end
        print(f"Passage imposé : {start} s -> {end} s")
    else:
        clip = find_clip(args.index, args.json)
        if clip is None:
            start, end = DEFAULT_START, DEFAULT_END
            print(
                "Aucun JSON de sélection trouvé : utilisation du passage "
                f"par défaut {start:.0f} s -> {end:.0f} s."
            )
        else:
            start, end, _ = clip

    output = args.output
    if output.resolve() == video.resolve():
        raise SystemExit("Le fichier de sortie doit être différent de la vidéo source.")

    print(f"Vidéo source : {video}")
    print(f"Extrait      : {start} s -> {end} s")
    print(f"Sortie       : {output}")

    try:
        result = cut_clip(str(video), start, end, str(output))
    except CutError as exc:
        print(f"Erreur : {exc}", file=sys.stderr)
        return 1

    duration, resolution = probe(Path(result))
    print(f"Durée        : {duration:.2f} s")
    print(f"Résolution   : {resolution}")
    print("Fichier produit lisible par ffprobe.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
