"""Worker cloud : réclame les jobs PostgreSQL et exécute le pipeline.

Deux modes :

- ``python worker.py`` : boucle infinie, à côté de l'interface web (conteneur
  tout-en-un, VPS, docker-compose) ;
- ``python worker.py --once`` : réclame **un** job, le traite puis quitte. C'est
  le mode utilisé par GitHub Actions : un runner démarre à chaque job envoyé
  depuis l'interface, ce qui évite de payer une machine allumée en permanence.

Dans les deux cas le worker :

1. remet en file les jobs interrompus par un arrêt brutal ;
2. réclame le plus ancien job avec ``FOR UPDATE SKIP LOCKED`` ;
3. exécute ``main._run`` avec la transcription Groq ;
4. envoie chaque clip dans Supabase Storage et l'enregistre en base ;
5. purge les vieux clips (durée et quota).
"""

from __future__ import annotations

import argparse
import io
import os
import shutil
import signal
import sys
import threading
import time
import traceback
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

# Doit précéder les imports du projet : plusieurs modules lisent leur
# configuration à l'import (store, groq_transcriber, selector).
load_dotenv(Path(__file__).with_name(".env.cloud"), override=False)

import downloader  # noqa: E402
import groq_transcriber
import main
import storage
import store
import transcriber

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DATA_DIR = BASE_DIR / "cloud-data"
POLL_SECONDS = 3.0
HEARTBEAT_SECONDS = 45
STOP = False
CURRENT_JOB_ID: str | None = None
_CONSOLE = sys.stdout

WORK_DIR = DEFAULT_DATA_DIR / "work"
OUTPUT_DIR = DEFAULT_DATA_DIR / "output"


def configure_dirs() -> tuple[Path, Path]:
    """Aligne tous les modules sur le dossier de travail cloud."""
    global WORK_DIR, OUTPUT_DIR

    raw = os.getenv("CLIPMAKER_DATA_DIR", "").strip()
    data_dir = Path(raw) if raw else DEFAULT_DATA_DIR
    WORK_DIR = data_dir / "work"
    OUTPUT_DIR = data_dir / "output"
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    main.WORK_DIR = WORK_DIR
    main.OUTPUT_DIR = OUTPUT_DIR
    downloader.WORK_DIR = str(WORK_DIR)
    transcriber.WORK_DIR = str(WORK_DIR)
    groq_transcriber.WORK_DIR = str(WORK_DIR)

    return WORK_DIR, OUTPUT_DIR


configure_dirs()


def _log(message: str) -> None:
    print(message, file=_CONSOLE, flush=True)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


def _clean_dir(path: Path) -> None:
    if not path.is_dir():
        return
    for item in path.iterdir():
        try:
            if item.is_dir():
                shutil.rmtree(item, ignore_errors=True)
            else:
                item.unlink()
        except OSError:
            pass


class _DbLog(io.TextIOBase):
    """Écrit la progression dans le journal du job (et sur la console)."""

    def __init__(self, job_id: str) -> None:
        self.job_id = job_id
        self.buffer = ""
        self.pending: list[str] = []
        self.last_flush = time.monotonic()
        self.lock = threading.Lock()

    def writable(self) -> bool:
        return True

    def write(self, text: str) -> int:
        if not isinstance(text, str):
            text = str(text)
        self.buffer += text
        while "\n" in self.buffer:
            line, self.buffer = self.buffer.split("\n", 1)
            self._add(line)
        return len(text)

    def flush(self) -> None:
        if self.buffer:
            self._add(self.buffer)
            self.buffer = ""
        self.flush_db()

    def _add(self, line: str) -> None:
        line = line.rstrip("\r")
        _log(line)
        with self.lock:
            self.pending.append(line)
            if len(self.pending) >= 20 or time.monotonic() - self.last_flush >= 2.0:
                self._flush_locked()

    def flush_db(self) -> None:
        with self.lock:
            self._flush_locked()

    def _flush_locked(self) -> None:
        if not self.pending:
            return
        text = "\n".join(self.pending)
        self.pending.clear()
        self.last_flush = time.monotonic()
        try:
            store.append_log(self.job_id, text)
        except store.StoreError as exc:
            _log(f"      (journal non enregistré : {exc})")


class _Heartbeat:
    """Empêche un job actif d'être considéré comme bloqué."""

    def __init__(self, job_id: str) -> None:
        self.job_id = job_id
        self.stop_event = threading.Event()
        self.thread = threading.Thread(
            target=self._run, name=f"heartbeat-{job_id}", daemon=True
        )

    def _run(self) -> None:
        while not self.stop_event.wait(HEARTBEAT_SECONDS):
            try:
                store.touch(self.job_id)
            except store.StoreError:
                pass

    def __enter__(self) -> "_Heartbeat":
        self.thread.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.stop_event.set()
        self.thread.join(timeout=2)


def _clip_index(path: Path) -> int:
    try:
        return int(path.stem.split("_", 1)[0])
    except (ValueError, IndexError):
        return 0


def _make_on_clip(job_id: str):
    """Retourne le callback qui envoie chaque clip dans Supabase Storage."""

    def on_clip(clip: dict, path: Path) -> None:
        index = _clip_index(path)
        key = storage.safe_key(job_id, index, path.name)
        size = path.stat().st_size
        _log(f"      Envoi du clip dans Supabase Storage ({size / 1_000_000:.1f} Mo)...")
        storage.upload_file(path, key)
        store.add_clip(
            job_id=job_id,
            index=index,
            title=str(clip.get("title", path.stem)),
            score=clip.get("score"),
            filename=path.name,
            storage_key=key,
            size_bytes=size,
        )
        _log("      Clip disponible dans le cloud.")

    return on_clip


def process_job(job: dict) -> str:
    """Exécute un job réclamé et met à jour son état en base.

    Retourne le statut final enregistré : ``done`` ou ``error``.
    """
    global CURRENT_JOB_ID

    job_id = str(job["id"])
    CURRENT_JOB_ID = job_id
    results: dict = {}
    sink = _DbLog(job_id)
    exit_code: int | None = None
    failures: list = []
    successes: list = []

    _clean_dir(OUTPUT_DIR)
    try:
        with _Heartbeat(job_id), redirect_stdout(sink), redirect_stderr(sink):
            exit_code = main._run(
                job["url"],
                int(job["clips"]),
                results=results,
                transcriber=groq_transcriber.transcribe,
                clip_transcriber=lambda video, start, end, label, segments: (
                    groq_transcriber.slice_segments(segments, start, end)
                ),
                on_clip=_make_on_clip(job_id),
            )
    except Exception as exc:  # noqa: BLE001 - le worker doit rester vivant
        results.setdefault("failures", []).append(({"title": "pipeline"}, exc))
        with redirect_stdout(sink), redirect_stderr(sink):
            traceback.print_exc()
        _log(f"Job {job_id} interrompu par une erreur : {exc}")
    finally:
        sink.flush()
        failures = list(results.get("failures", []))
        successes = list(results.get("successes", []))

        if exit_code == 0 and not failures:
            status, error = "done", ""
        elif successes:
            status = "done"
            error = f"{len(failures)} clip(s) en échec."
        else:
            status = "error"
            error = "Le traitement a échoué. Consulte le journal ci-dessous."

        try:
            store.finish_job(job_id, status, error, exit_code)
        except store.StoreError as exc:
            _log(f"Impossible de finaliser le job {job_id} : {exc}")

        _clean_dir(OUTPUT_DIR)
        _clean_dir(WORK_DIR)
        CURRENT_JOB_ID = None
        _log(
            f"Job {job_id} terminé : {len(successes)} clip(s), "
            f"{len(failures)} échec(s)."
        )
        return status


def purge_storage() -> int:
    """Supprime les clips trop vieux et respecte le quota de stockage."""
    retention_days = max(1, _env_int("RETENTION_DAYS", 14))
    max_bytes = int(max(50, _env_float("MAX_STORAGE_MB", 900.0)) * 1_000_000)
    now = datetime.now(timezone.utc)

    clips = store.list_stored_clips()
    removals: list[dict] = []
    kept: list[dict] = []
    for clip in clips:
        created = clip.get("created_at")
        if created is not None:
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            age_days = (now - created).total_seconds() / 86400
        else:
            age_days = 0
        if age_days >= retention_days:
            removals.append(clip)
        else:
            kept.append(clip)

    total = sum(int(clip.get("size_bytes") or 0) for clip in kept)
    for clip in kept:  # du plus ancien au plus récent
        if total <= max_bytes:
            break
        removals.append(clip)
        total -= int(clip.get("size_bytes") or 0)

    removed = 0
    for clip in removals:
        key = clip.get("storage_key") or ""
        try:
            storage.delete_file(key)
        except storage.StorageError as exc:
            _log(f"      Purge : suppression cloud impossible pour {key} : {exc}")
            continue
        try:
            store.delete_clip(str(clip["id"]))
        except store.StoreError as exc:
            _log(f"      Purge : suppression en base impossible : {exc}")
            continue
        removed += 1

    try:
        removed_jobs = store.purge_jobs(_env_int("JOB_RETENTION_DAYS", 60))
    except store.StoreError:
        removed_jobs = 0

    if removed or removed_jobs:
        _log(
            f"Purge : {removed} clip(s) et {removed_jobs} ancien(s) job(s) supprimés."
        )
    return removed


def _install_signal_handlers() -> None:
    def handler(signum, frame):  # noqa: ANN001, ARG001
        global STOP
        STOP = True
        job_id = CURRENT_JOB_ID
        if job_id:
            try:
                store.requeue_job(
                    job_id,
                    f"Service arrêté (signal {signum}) : job remis en file.",
                )
            except Exception:  # noqa: BLE001 - on quitte quoi qu'il arrive
                pass
        _log("Worker arrêté.")
        os._exit(0)

    for name in ("SIGTERM", "SIGINT"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                signal.signal(sig, handler)
            except (OSError, ValueError):
                pass


def _require_config() -> None:
    if not store.configured():
        raise SystemExit(
            "DATABASE_URL est vide : le worker cloud ne peut pas démarrer."
        )
    if not storage.configured():
        raise SystemExit(
            "SUPABASE_URL / SUPABASE_SERVICE_KEY sont vides : impossible "
            "d'envoyer les clips dans le cloud."
        )
    if not os.getenv("GROQ_API_KEY", "").strip():
        raise SystemExit("GROQ_API_KEY est vide : la transcription cloud est impossible.")


def _prepare_runtime() -> None:
    """Nettoie les restes d'un arrêt brutal et remet les jobs bloqués en file."""
    _clean_dir(WORK_DIR)
    _clean_dir(OUTPUT_DIR)
    requeued = store.requeue_stale(_env_int("STALE_MINUTES", 15))
    if requeued:
        _log(f"  {requeued} job(s) interrompu(s) remis en file.")


def run_once() -> int:
    """Traite au plus un job puis retourne (mode GitHub Actions)."""
    _require_config()
    _log("Worker ClipMaker (mode ponctuel).")
    _log(f"  Travail : {WORK_DIR}")

    try:
        _prepare_runtime()
    except store.StoreError as exc:
        _log(f"Base de données indisponible : {exc}")
        return 1

    try:
        job = store.claim_job()
    except store.StoreError as exc:
        _log(f"Base de données indisponible : {exc}")
        return 1

    if job is None:
        _log("Aucun job en attente.")
    else:
        _log(f"Job {job['id']} réclamé : {job['url']} ({job['clips']} clip(s))")
        process_job(job)

    try:
        purge_storage()
    except store.StoreError as exc:
        _log(f"Purge impossible : {exc}")
    return 0


def run_forever() -> None:
    _require_config()

    _log("Worker ClipMaker démarré.")
    _log(f"  Travail : {WORK_DIR}")

    try:
        _prepare_runtime()
    except store.StoreError as exc:
        _log(f"  Base de données indisponible au démarrage : {exc}")

    try:
        purge_storage()
    except store.StoreError as exc:
        _log(f"  Purge impossible au démarrage : {exc}")

    while not STOP:
        try:
            job = store.claim_job()
        except store.StoreError as exc:
            _log(f"Base de données indisponible : {exc}")
            time.sleep(10)
            continue

        if job is None:
            time.sleep(_env_float("POLL_SECONDS", POLL_SECONDS))
            continue

        _log(f"Job {job['id']} réclamé : {job['url']} ({job['clips']} clip(s))")
        process_job(job)

        try:
            purge_storage()
        except store.StoreError as exc:
            _log(f"Purge impossible : {exc}")


def main_entry(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Worker ClipMaker : traite les jobs de la file PostgreSQL."
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="traite au plus un job puis quitte (GitHub Actions).",
    )
    args = parser.parse_args(argv)

    configure_dirs()
    _install_signal_handlers()

    if args.once:
        raise SystemExit(run_once())
    run_forever()


if __name__ == "__main__":
    main_entry()
