"""Interface web ClipMaker.

Deux modes, choisis automatiquement :

- **local** (par défaut) : les jobs vivent en mémoire, le pipeline tourne dans un
  thread du processus Flask. Lancement : ``python webapp.py``.
- **cloud** (``DATABASE_URL`` renseignée) : les jobs sont écrits dans PostgreSQL
  et traités par ``worker.py`` (GitHub Actions). Les clips sont stockés dans
  Supabase Storage et servis par URL signée. Lancement :
  ``gunicorn webapp:app``.

En mode cloud, aucun outil vidéo n'est requis (ni ffmpeg, ni yt-dlp, ni
faster-whisper) : l'interface est volontairement légère pour tourner sur un
hébergement gratuit (Render).

Si ``APP_PASSWORD`` est renseignée, l'accès est protégé par un mot de passe
(session Flask).
"""

from __future__ import annotations

import hashlib
import hmac
import io
import math
import mimetypes
import os
import re
import secrets
import shutil
import socket
import tempfile
import threading
import uuid
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import httpx
from dotenv import load_dotenv
from flask import (
    Flask,
    abort,
    jsonify,
    redirect,
    render_template_string,
    request,
    send_from_directory,
    session,
    url_for,
)

BASE_DIR = Path(__file__).resolve().parent
os.chdir(BASE_DIR)


def _load_cloud_env() -> None:
    """Charge ``.env.cloud`` pour les essais cloud sur le PC.

    Sur Render, les variables viennent du tableau de bord. Sur le PC, charger
    ce fichier automatiquement transformerait « python webapp.py » en mode
    cloud par surprise : il faut donc le demander explicitement.
    """
    flag = os.getenv("CLIPMAKER_CLOUD_ENV", "").strip().lower()
    if flag in ("1", "true", "yes", "on") or os.getenv("RENDER", "").strip():
        load_dotenv(BASE_DIR / ".env.cloud", override=False)


_load_cloud_env()

OUTPUT_DIR = BASE_DIR / "output"
APK_PATH = BASE_DIR / "ClipMaker.apk"

# Sur Render, l'interface est toujours en mode cloud : si DATABASE_URL manque
# encore (variables pas collées), l'application doit démarrer et afficher une
# erreur claire, pas importer yt-dlp/ffmpeg (absents de requirements-web.txt).
ON_RENDER = any(
    os.getenv(name, "").strip()
    for name in ("RENDER", "RENDER_SERVICE_ID", "RENDER_EXTERNAL_HOSTNAME")
)
CLOUD = bool(os.getenv("DATABASE_URL", "").strip() or ON_RENDER)
if CLOUD:
    import storage
    import store

    main = None  # type: ignore[assignment]
else:  # pragma: no cover - dépend de l'environnement
    import main

    main.WORK_DIR = BASE_DIR / "work"
    main.OUTPUT_DIR = OUTPUT_DIR
    storage = None  # type: ignore[assignment]
    store = None  # type: ignore[assignment]

app = Flask(__name__)
# Supabase Storage accepte 50 Mo maximum par fichier sur le plan gratuit : la
# vidéo est donc envoyée en morceaux de 40 Mo puis réassemblée par le worker.
# MAX_UPLOAD_MB = taille TOTALE autorisée ; MAX_CHUNK_MB = taille par requête.
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "500"))
MAX_CHUNK_MB = min(45, max(1, int(os.getenv("MAX_CHUNK_MB", "40"))))
# En mode cloud, l'interface accepte l'envoi d'une vidéo depuis le téléphone.
app.config["MAX_CONTENT_LENGTH"] = (
    (MAX_CHUNK_MB + 5) * 1_000_000 if CLOUD else 1024 * 1024
)
_app_password_value = os.getenv("APP_PASSWORD", "").strip()
_env_secret = os.getenv("SECRET_KEY", "").strip()
if _env_secret:
    _secret_key = _env_secret
elif _app_password_value:
    # Valeur stable entre les workers gunicorn quand SECRET_KEY n'est pas fourni.
    _secret_key = hashlib.sha256(
        f"clipmaker:{_app_password_value}".encode("utf-8")
    ).hexdigest()
else:
    _secret_key = secrets.token_hex(32)
app.config["SECRET_KEY"] = _secret_key
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

JOBS: dict[str, "Job"] = {}
JOBS_LOCK = threading.Lock()
RUN_LOCK = threading.Lock()
MAX_LOG_LINES = 500
PORT = int(os.getenv("PORT", "5000"))

STATUS_FR = {
    "queued": "en attente",
    "running": "en cours",
    "done": "terminé",
    "error": "erreur",
}


def _now() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _app_password() -> str:
    return os.getenv("APP_PASSWORD", "").strip()


def _fmt_date(value: object) -> str:
    if not value:
        return ""
    if isinstance(value, datetime):
        moment = value
        if moment.tzinfo is not None:
            moment = moment.astimezone()
        return moment.strftime("%d/%m %H:%M")
    return str(value)


def _fmt_score(value: object) -> str:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return str(value or "?")
    return str(int(number)) if number.is_integer() else f"{number:g}"


@dataclass
class Job:
    id: str
    url: str
    clips: int
    status: str = "en attente"
    log: list[str] = field(default_factory=list)
    outputs: list[dict] = field(default_factory=list)
    error: str = ""
    exit_code: int | None = None
    started_at: str = field(default_factory=_now)
    finished_at: str = ""


class JobWriter(io.TextIOBase):
    """Redirige la sortie du pipeline vers le journal du job."""

    def __init__(self, job: Job) -> None:
        self.job = job
        self.buffer = ""

    def writable(self) -> bool:
        return True

    def write(self, text: str) -> int:
        if not isinstance(text, str):
            text = str(text)
        self.buffer += text
        while "\n" in self.buffer:
            line, self.buffer = self.buffer.split("\n", 1)
            self._emit(line)
        return len(text)

    def flush(self) -> None:
        if self.buffer:
            self._emit(self.buffer)
            self.buffer = ""

    def _emit(self, line: str) -> None:
        line = line.rstrip("\r")
        with JOBS_LOCK:
            self.job.log.append(line)
            if len(self.job.log) > MAX_LOG_LINES:
                del self.job.log[: len(self.job.log) - MAX_LOG_LINES]


def _run_job(job: Job) -> None:
    """Exécute le pipeline dans un thread et remplit le job."""
    import main  # import local : le mode cloud ne charge pas les outils vidéo

    try:
        job.status = "en cours"
        results: dict = {}
        writer = JobWriter(job)
        with redirect_stdout(writer), redirect_stderr(writer):
            code = main._run(job.url, job.clips, results=results)
        writer.flush()

        job.exit_code = code
        job.outputs = [
            {
                "name": Path(path).name,
                "title": str(clip.get("title", Path(path).stem)),
                "score": clip.get("score", "?"),
            }
            for clip, path in results.get("successes", [])
        ]
        failures = results.get("failures", [])
        job.status = "terminé" if code == 0 else "terminé avec des erreurs"
        if failures:
            job.error = f"{len(failures)} clip(s) en échec."
        elif code != 0:
            job.error = "Le traitement a échoué. Consulte le journal ci-dessous."
    except Exception as exc:  # noqa: BLE001 - on affiche l'erreur à l'utilisateur
        job.status = "erreur"
        job.error = str(exc)
        with JOBS_LOCK:
            job.log.append(f"Erreur : {exc}")
    finally:
        job.finished_at = _now()
        RUN_LOCK.release()


def _list_outputs(limit: int = 12) -> list[dict]:
    if not OUTPUT_DIR.is_dir():
        return []
    files = sorted(
        OUTPUT_DIR.glob("*.mp4"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )[:limit]
    return [
        {
            "name": path.name,
            "size_mb": round(path.stat().st_size / 1_000_000, 1),
            "url": url_for("output_file", filename=path.name),
        }
        for path in files
    ]


def _job_view(job: Job) -> dict:
    with JOBS_LOCK:
        outputs = [
            {
                **output,
                "url": url_for("output_file", filename=output["name"]),
            }
            for output in job.outputs
        ]
        return {
            "id": job.id,
            "url": job.url,
            "clips": job.clips,
            "status": job.status,
            "error": job.error,
            "exit_code": job.exit_code,
            "started_at": job.started_at,
            "finished_at": job.finished_at,
            "log_text": "\n".join(job.log[-250:]),
            "outputs": outputs,
        }


def _recent_jobs(limit: int = 8) -> list[dict]:
    with JOBS_LOCK:
        jobs = sorted(JOBS.values(), key=lambda job: job.started_at, reverse=True)
        return [
            {
                "id": job.id,
                "url": job.url,
                "clips": job.clips,
                "status": job.status,
                "started_at": job.started_at,
                "clip_count": len(job.outputs),
            }
            for job in jobs[:limit]
        ]


def _display_url(url: str) -> str:
    """Rend lisible une URL de job (les envois n'ont pas de lien YouTube)."""
    text = (url or "").strip()
    if CLOUD and storage is not None and storage.source_object(text):
        return "Vidéo envoyée depuis le téléphone"
    return text


def _cloud_job_view(job: dict, clips: list[dict]) -> dict:
    outputs = []
    for clip in clips:
        outputs.append(
            {
                "id": str(clip["id"]),
                "name": clip.get("filename", ""),
                "title": clip.get("title", ""),
                "score": _fmt_score(clip.get("score")),
                "size_mb": round((clip.get("size_bytes") or 0) / 1_000_000, 1),
                "url": url_for("clip_file", clip_id=str(clip["id"])),
            }
        )

    return {
        "id": str(job["id"]),
        "url": _display_url(str(job.get("url", ""))),
        "clips": job.get("clips", 0),
        "status": STATUS_FR.get(str(job.get("status")), str(job.get("status"))),
        "error": job.get("error", ""),
        "exit_code": job.get("exit_code"),
        "started_at": _fmt_date(job.get("started_at") or job.get("created_at")),
        "finished_at": _fmt_date(job.get("finished_at")),
        "log_text": job.get("log", "") or "",
        "outputs": outputs,
    }


def _cloud_recent_jobs(limit: int = 10) -> list[dict]:
    jobs = store.list_jobs(limit)
    return [
        {
            "id": str(job["id"]),
            "url": _display_url(str(job.get("url", ""))),
            "clips": job.get("clips", 0),
            "status": STATUS_FR.get(str(job.get("status")), str(job.get("status"))),
            "started_at": _fmt_date(job.get("started_at") or job.get("created_at")),
            "clip_count": int(job.get("clip_count") or 0),
        }
        for job in jobs
    ]


def _github_repo() -> str:
    """Normalise ``GITHUB_REPO`` en ``proprietaire/depot``."""
    raw = os.getenv("GITHUB_REPO", "").strip()
    if not raw:
        return ""
    for prefix in ("https://github.com/", "http://github.com/", "github.com/"):
        if raw.lower().startswith(prefix):
            raw = raw[len(prefix) :]
            break
    raw = raw.removesuffix(".git").strip("/")
    parts = [part for part in raw.split("/") if part]
    return "/".join(parts[:2]) if len(parts) >= 2 else ""


def dispatch_runner(job_id: str) -> str:
    """Demande à GitHub Actions de démarrer un worker pour ce job.

    Retourne un message court, prêt à être ajouté au journal du job. Un échec
    n'est pas bloquant : le job reste en file et la tâche planifiée du workflow
    le récupérera (au plus 15 minutes de retard).
    """
    repo = _github_repo()
    token = os.getenv("GITHUB_DISPATCH_TOKEN", "").strip()
    if not repo or not token:
        return (
            "Aucun runner GitHub configuré (GITHUB_REPO / GITHUB_DISPATCH_TOKEN) : "
            "le job sera pris par la tâche planifiée."
        )

    event = os.getenv("GITHUB_DISPATCH_EVENT", "").strip() or "clipmaker-job"
    try:
        response = httpx.post(
            f"https://api.github.com/repos/{repo}/dispatches",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "ClipMaker",
            },
            json={"event_type": event, "client_payload": {"job_id": job_id}},
            timeout=httpx.Timeout(10.0, connect=5.0),
        )
    except httpx.HTTPError as exc:
        return (
            f"GitHub Actions injoignable ({type(exc).__name__}) : la tâche "
            "planifiée prendra le relais."
        )

    if response.status_code in (200, 204):
        return "Runner GitHub Actions démarré pour ce traitement."
    if response.status_code in (401, 403):
        return (
            f"GitHub a refusé le jeton (HTTP {response.status_code}) : vérifie "
            "GITHUB_DISPATCH_TOKEN. La tâche planifiée prendra le relais."
        )
    if response.status_code == 404:
        return (
            f"Dépôt introuvable (HTTP 404) : vérifie GITHUB_REPO={repo}. "
            "La tâche planifiée prendra le relais."
        )
    return (
        f"GitHub a refusé le déclenchement (HTTP {response.status_code}) : la "
        "tâche planifiée prendra le relais."
    )


def _home(error: str = "", status: int = 200):
    return (
        render_template_string(
            PAGE,
            job=None,
            jobs=_recent_jobs(),
            outputs=_list_outputs(),
            busy=RUN_LOCK.locked(),
            error=error,
            apk=APK_PATH.is_file(),
            cloud=False,
            auth=bool(_app_password()),
            max_upload_mb=MAX_UPLOAD_MB,
            chunk_size_bytes=MAX_CHUNK_MB * 1_000_000,
        ),
        status,
    )


def _cloud_home(error: str = "", status: int = 200):
    try:
        jobs = _cloud_recent_jobs()
        db_error = ""
    except store.StoreError as exc:
        jobs = []
        if not os.getenv("DATABASE_URL", "").strip():
            db_error = (
                "Configuration incomplète : dans Render, ouvre Environment → "
                "Add from .env, colle le contenu de render-env.txt puis "
                "Save changes."
            )
        else:
            db_error = f"Base de données indisponible : {exc}"

    return (
        render_template_string(
            PAGE,
            job=None,
            jobs=jobs,
            outputs=[],
            busy=False,
            error=error or db_error,
            apk=APK_PATH.is_file(),
            cloud=True,
            auth=bool(_app_password()),
            max_upload_mb=MAX_UPLOAD_MB,
            chunk_size_bytes=MAX_CHUNK_MB * 1_000_000,
        ),
        status,
    )


@app.before_request
def _require_login():
    password = _app_password()
    if not password:
        return None
    if request.path in ("/login", "/healthz", "/apk"):
        return None
    if session.get("auth") is True:
        return None
    if request.method == "GET":
        return redirect(url_for("login", next=request.path))
    return ("Authentification requise.", 401)


@app.get("/login")
def login():
    if not _app_password():
        return redirect(url_for("index"))
    if session.get("auth") is True:
        return redirect(url_for("index"))
    return render_template_string(LOGIN_PAGE, error="", next=request.args.get("next", ""))


@app.post("/login")
def login_post():
    if not _app_password():
        return redirect(url_for("index"))

    password = request.form.get("password", "")
    target = request.form.get("next", "") or url_for("index")
    if not target.startswith("/") or target.startswith("//"):
        target = url_for("index")

    if hmac.compare_digest(password, _app_password()):
        session["auth"] = True
        return redirect(target)

    return (
        render_template_string(
            LOGIN_PAGE, error="Mot de passe incorrect.", next=target
        ),
        401,
    )


@app.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.errorhandler(413)
def too_large(error):  # noqa: ARG001
    message = (
        f"Morceau trop lourd : la limite est de {MAX_CHUNK_MB} Mo "
        f"par requête (la vidéo totale peut atteindre {MAX_UPLOAD_MB} Mo, "
        "elle est envoyée par morceaux)."
    )
    return _cloud_home(message, 413) if CLOUD else _home(message, 413)


@app.get("/healthz")
def healthz():
    if not CLOUD:
        response = jsonify(status="ok", mode="local")
        response.headers["Cache-Control"] = "no-store"
        return response, 200

    if not os.getenv("DATABASE_URL", "").strip():
        # Variables pas encore collées dans Render : le service doit rester
        # « live » pour afficher la page d'explication au lieu d'échouer.
        response = jsonify(status="config", mode="cloud")
        response.headers["Cache-Control"] = "no-store"
        return response, 200

    ok = store.ping()
    response = jsonify(status="ok" if ok else "error", mode="cloud")
    response.headers["Cache-Control"] = "no-store"
    return response, 200 if ok else 503


@app.get("/")
def index():
    if CLOUD:
        return _cloud_home()
    return _home()


def _parse_clips(raw: object) -> int | str:
    """Valide le nombre de clips ; retourne un message d'erreur en cas d'échec."""
    try:
        clips = int(str(raw or "5").strip())
    except ValueError:
        return "Le nombre de clips doit être un entier."
    if not 1 <= clips <= 10:
        return "Le nombre de clips doit être compris entre 1 et 10."
    return clips


def _active_jobs_full() -> bool:
    limit = int(os.getenv("MAX_ACTIVE_JOBS", "5"))
    return store.active_count() >= max(1, limit)


@app.post("/run")
def run_job():
    url = (request.form.get("url") or "").strip()
    clips = _parse_clips(request.form.get("clips"))

    if not url:
        return _cloud_home("Colle une URL YouTube.", 400) if CLOUD else _home(
            "Colle une URL YouTube.", 400
        )
    if isinstance(clips, str):
        return _cloud_home(clips, 400) if CLOUD else _home(clips, 400)

    if CLOUD:
        try:
            if _active_jobs_full():
                return _cloud_home(
                    "Trop de traitements en attente. Réessaie dans quelques minutes.",
                    429,
                )
            job = store.create_job(url, clips)
        except store.StoreError as exc:
            return _cloud_home(f"Impossible de créer le traitement : {exc}", 503)
        try:
            store.append_log(str(job["id"]), dispatch_runner(str(job["id"])))
        except store.StoreError:
            pass
        return redirect(url_for("job_page", job_id=str(job["id"])))

    if not RUN_LOCK.acquire(blocking=False):
        return _home("Un traitement est déjà en cours. Attends qu'il se termine.", 429)

    job = Job(id=uuid.uuid4().hex[:8], url=url, clips=clips)
    with JOBS_LOCK:
        JOBS[job.id] = job

    try:
        thread = threading.Thread(target=_run_job, args=(job,), daemon=True)
        thread.start()
    except Exception:
        RUN_LOCK.release()
        raise

    return redirect(url_for("job_page", job_id=job.id))


@app.post("/upload")
def upload_job():
    """Reçoit une vidéo depuis le téléphone et lance le traitement (cloud)."""
    if not CLOUD:
        abort(404)

    clips = _parse_clips(request.form.get("clips"))
    if isinstance(clips, str):
        return _cloud_home(clips, 400)

    try:
        if _active_jobs_full():
            return _cloud_home(
                "Trop de traitements en attente. Réessaie dans quelques minutes.",
                429,
            )
    except store.StoreError as exc:
        return _cloud_home(f"Base de données indisponible : {exc}", 503)

    upload = request.files.get("video")
    if upload is None or not upload.filename:
        return _cloud_home("Choisis une vidéo à envoyer.", 400)

    filename = Path(upload.filename).name
    suffix = Path(filename).suffix.lower()
    if suffix not in storage.VIDEO_SUFFIXES:
        return _cloud_home(
            "Format non reconnu : choisis un fichier MP4, MKV, WEBM ou MOV.", 400
        )

    key = storage.source_key(filename)
    temp_dir = Path(tempfile.mkdtemp(prefix="clipmaker_upload_"))
    temp_path = temp_dir / f"video{suffix}"
    size_mb = 0.0
    try:
        upload.save(str(temp_path))
        size_mb = temp_path.stat().st_size / 1_000_000
        if size_mb <= 0:
            return _cloud_home("Le fichier reçu est vide.", 400)
        content_type = mimetypes.guess_type(filename)[0] or "video/mp4"
        storage.upload_file(temp_path, key, content_type=content_type)
    except OSError as exc:
        return _cloud_home(f"Enregistrement impossible : {exc}", 500)
    except storage.StorageError as exc:
        return _cloud_home(f"Envoi impossible : {exc}", 503)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

    try:
        job = store.create_job(f"{storage.UPLOAD_SCHEME}{key}", clips)
    except store.StoreError as exc:
        try:
            storage.delete_file(key)
        except storage.StorageError:
            pass
        return _cloud_home(f"Impossible de créer le traitement : {exc}", 503)

    try:
        store.append_log(
            str(job["id"]), f"Vidéo envoyée depuis le téléphone ({size_mb:.1f} Mo)."
        )
        store.append_log(str(job["id"]), dispatch_runner(str(job["id"])))
    except store.StoreError:
        pass
    return redirect(url_for("job_page", job_id=str(job["id"])))


def _json_error(message: str, status: int):
    """Réponse JSON pour les étapes d'envoi par morceaux (appelées en JS)."""
    return jsonify(error=message), status


def _upload_token() -> str:
    token = str(request.form.get("token") or "").strip().lower()
    return token if re.fullmatch(r"[0-9a-f]{16}", token) else ""


@app.post("/upload/start")
def upload_start():
    """Prépare un envoi par morceaux et retourne le jeton à utiliser."""
    if not CLOUD:
        abort(404)

    name = Path(str(request.form.get("name") or "")).name
    if Path(name).suffix.lower() not in storage.VIDEO_SUFFIXES:
        return _json_error(
            "Format non reconnu : choisis un MP4, MKV, WEBM ou MOV.", 400
        )
    try:
        size = int(str(request.form.get("size") or "").strip())
    except ValueError:
        return _json_error("Taille de vidéo invalide.", 400)
    if size <= 0:
        return _json_error("Le fichier reçu est vide.", 400)
    if size > MAX_UPLOAD_MB * 1_000_000:
        return _json_error(
            f"Vidéo trop lourde : la limite est de {MAX_UPLOAD_MB} Mo.", 413
        )
    clips = _parse_clips(request.form.get("clips"))
    if isinstance(clips, str):
        return _json_error(clips, 400)

    try:
        if _active_jobs_full():
            return _json_error(
                "Trop de traitements en attente. Réessaie dans quelques minutes.",
                429,
            )
    except store.StoreError as exc:
        return _json_error(f"Base de données indisponible : {exc}", 503)

    token = storage.source_key(name).split("/")[1]
    chunk_size = MAX_CHUNK_MB * 1_000_000
    return jsonify(
        token=token,
        chunk_size=chunk_size,
        chunks=max(1, math.ceil(size / chunk_size)),
    )


@app.post("/upload/chunk")
def upload_chunk():
    """Reçoit un morceau d'au plus MAX_CHUNK_MB et le range dans le bucket."""
    if not CLOUD:
        abort(404)

    token = _upload_token()
    if not token:
        return _json_error("Jeton d'envoi invalide.", 400)
    raw_index = str(request.form.get("index") or "").strip()
    if not raw_index.isdigit() or not 0 <= int(raw_index) <= 999:
        return _json_error("Numéro de morceau invalide.", 400)
    chunk = request.files.get("chunk")
    if chunk is None:
        return _json_error("Morceau manquant.", 400)

    key = f"{storage.SOURCE_PREFIX}/{token}/part-{int(raw_index):04d}.bin"
    temp_dir = Path(tempfile.mkdtemp(prefix="clipmaker_chunk_"))
    temp_path = temp_dir / "part.bin"
    try:
        chunk.save(str(temp_path))
        if temp_path.stat().st_size == 0:
            return _json_error("Morceau vide.", 400)
        storage.upload_file(temp_path, key, content_type="application/octet-stream")
    except OSError as exc:
        return _json_error(f"Enregistrement impossible : {exc}", 500)
    except storage.StorageError as exc:
        return _json_error(f"Envoi impossible : {exc}", 503)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
    return jsonify(ok=True)


@app.post("/upload/finish")
def upload_finish():
    """Tous les morceaux sont là : crée le job et déclenche le runner."""
    if not CLOUD:
        abort(404)

    token = _upload_token()
    if not token:
        return _json_error("Jeton d'envoi invalide.", 400)
    clips = _parse_clips(request.form.get("clips"))
    if isinstance(clips, str):
        return _json_error(clips, 400)
    try:
        size = int(str(request.form.get("size") or "").strip())
    except ValueError:
        size = 0

    prefix = f"{storage.SOURCE_PREFIX}/{token}"
    try:
        parts = storage.list_objects(prefix)
    except storage.StorageError as exc:
        return _json_error(f"Stockage indisponible : {exc}", 503)
    if not parts:
        return _json_error("Aucun morceau reçu. Recommence l'envoi.", 400)

    try:
        job = store.create_job(f"{storage.UPLOAD_SCHEME}{prefix}/", clips)
    except store.StoreError as exc:
        try:
            storage.delete_folder(prefix)
        except storage.StorageError:
            pass
        return _json_error(f"Impossible de créer le traitement : {exc}", 503)

    try:
        store.append_log(
            str(job["id"]),
            f"Vidéo envoyée en {len(parts)} morceau(x) ({size / 1_000_000:.1f} Mo).",
        )
        store.append_log(str(job["id"]), dispatch_runner(str(job["id"])))
    except store.StoreError:
        pass
    return jsonify(job_id=str(job["id"]))


@app.get("/job/<job_id>")
def job_page(job_id: str):
    if CLOUD:
        try:
            job = store.get_job(job_id)
            clips = store.list_clips(job_id) if job is not None else []
        except store.StoreError as exc:
            return _cloud_home(f"Base de données indisponible : {exc}", 503)
        if job is None:
            abort(404)
        return render_template_string(
            PAGE,
            job=_cloud_job_view(job, clips),
            jobs=[],
            outputs=[],
            busy=False,
            error="",
            apk=False,
            cloud=True,
            auth=bool(_app_password()),
            max_upload_mb=MAX_UPLOAD_MB,
            chunk_size_bytes=MAX_CHUNK_MB * 1_000_000,
        )

    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if job is None:
        abort(404)
    return render_template_string(
        PAGE,
        job=_job_view(job),
        jobs=[],
        outputs=[],
        busy=False,
        error="",
        apk=False,
        cloud=False,
        auth=bool(_app_password()),
        max_upload_mb=MAX_UPLOAD_MB,
            chunk_size_bytes=MAX_CHUNK_MB * 1_000_000,
    )


@app.get("/clip/<clip_id>")
def clip_file(clip_id: str):
    """Sert un clip cloud : fichier local sinon redirection vers Supabase."""
    if not CLOUD:
        abort(404)

    try:
        clip = store.get_clip(clip_id)
    except store.StoreError:
        abort(503)
    if clip is None:
        abort(404)

    name = Path(str(clip.get("filename") or "")).name
    if name and (OUTPUT_DIR / name).is_file():
        return send_from_directory(OUTPUT_DIR, name, conditional=True)

    key = str(clip.get("storage_key") or "")
    if not key:
        abort(404)

    try:
        expires = int(os.getenv("SIGNED_URL_SECONDS", "3600"))
        target = storage.signed_url(key, expires=expires)
    except storage.StorageError:
        abort(502)
    return redirect(target)


@app.get("/apk")
def apk_file():
    if not APK_PATH.is_file():
        abort(404)
    return send_from_directory(
        BASE_DIR,
        APK_PATH.name,
        as_attachment=True,
        download_name="ClipMaker.apk",
        mimetype="application/vnd.android.package-archive",
    )


@app.get("/output/<path:filename>")
def output_file(filename: str):
    if not filename.lower().endswith(".mp4"):
        abort(404)
    return send_from_directory(OUTPUT_DIR, filename, conditional=True)


def _local_ips() -> list[str]:
    addresses: set[str] = set()
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect(("8.8.8.8", 80))
            addresses.add(sock.getsockname()[0])
        finally:
            sock.close()
    except OSError:
        pass

    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            if info[0] == socket.AF_INET:
                addresses.add(info[4][0])
    except OSError:
        pass

    return sorted(ip for ip in addresses if not ip.startswith("127."))


LOGIN_PAGE = """\
<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ClipMaker — connexion</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
    background: #0b1020;
    color: #e8ecf8;
    display: flex;
    min-height: 100vh;
    align-items: center;
    justify-content: center;
    padding: 16px;
  }
  .card {
    width: 100%;
    max-width: 360px;
    background: #141a2e;
    border: 1px solid #26304d;
    border-radius: 14px;
    padding: 24px;
  }
  h1 { margin: 0 0 4px; font-size: 1.5rem; }
  p { color: #b8c4e6; font-size: .9rem; }
  label { display: block; font-size: .9rem; margin: 16px 0 6px; color: #aeb8d8; }
  input {
    width: 100%;
    padding: 12px;
    border-radius: 10px;
    border: 1px solid #33405f;
    background: #0d1326;
    color: #fff;
    font-size: 16px;
  }
  button {
    width: 100%;
    margin-top: 16px;
    padding: 14px;
    border: 0;
    border-radius: 10px;
    background: #4f7cff;
    color: #fff;
    font-size: 1rem;
    font-weight: 700;
  }
  .error { color: #ff8b8b; }
</style>
</head>
<body>
<form class="card" method="post" action="{{ url_for('login_post') }}">
  <h1>ClipMaker</h1>
  <p>Entre le mot de passe du serveur.</p>
  {% if error %}<p class="error">{{ error }}</p>{% endif %}
  <input type="hidden" name="next" value="{{ next }}">
  <label for="password">Mot de passe</label>
  <input id="password" name="password" type="password" required autofocus>
  <button type="submit">Se connecter</button>
</form>
</body>
</html>
"""


PAGE = """\
<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
{% if job and job.status in ("en attente", "en cours") %}
<meta http-equiv="refresh" content="3">
{% endif %}
<title>ClipMaker</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
    background: #0b1020;
    color: #e8ecf8;
  }
  header {
    padding: 20px 16px;
    background: linear-gradient(135deg, #1b2a6b, #0b1020);
  }
  header h1 { margin: 0 0 6px; font-size: 1.6rem; }
  header p { margin: 0; color: #b8c4e6; font-size: .9rem; }
  header .right { float: right; font-size: .85rem; }
  main { padding: 16px; max-width: 760px; margin: 0 auto; }
  .card {
    background: #141a2e;
    border: 1px solid #26304d;
    border-radius: 14px;
    padding: 16px;
    margin-bottom: 16px;
  }
  h2 { margin: 0 0 10px; font-size: 1.15rem; }
  h3 { margin: 0 0 8px; font-size: 1rem; }
  label { display: block; font-size: .9rem; margin: 12px 0 6px; color: #aeb8d8; }
  input {
    width: 100%;
    padding: 12px;
    border-radius: 10px;
    border: 1px solid #33405f;
    background: #0d1326;
    color: #fff;
    font-size: 16px;
  }
  button {
    width: 100%;
    margin-top: 16px;
    padding: 14px;
    border: 0;
    border-radius: 10px;
    background: #4f7cff;
    color: #fff;
    font-size: 1rem;
    font-weight: 700;
  }
  button:disabled { opacity: .5; }
  pre {
    white-space: pre-wrap;
    word-break: break-word;
    background: #080d18;
    border-radius: 10px;
    padding: 12px;
    font-size: .78rem;
    line-height: 1.4;
    max-height: 360px;
    overflow: auto;
  }
  video { width: 100%; border-radius: 10px; background: #000; }
  a { color: #8fb0ff; }
  .status { font-weight: 700; }
  .error { color: #ff8b8b; }
  .clip { margin-bottom: 20px; }
  .muted { color: #8f9bb8; font-size: .85rem; }
  .badge {
    display: inline-block;
    padding: 3px 8px;
    border-radius: 999px;
    background: #26304d;
    color: #c8d2ee;
    font-size: .75rem;
    font-weight: 600;
  }
  .badge.cloud { background: #234a3a; color: #9ff0c4; }
</style>
</head>
<body>
<header>
  {% if auth %}<a class="right" href="{{ url_for('logout') }}">Déconnexion</a>{% endif %}
  <h1>ClipMaker {% if cloud %}<span class="badge cloud">cloud</span>{% endif %}</h1>
  <p>Génère des clips verticaux sous-titrés depuis une URL YouTube.</p>
</header>
<main>
{% if job %}
  <div class="card">
    <h2>Traitement {{ job.id }}</h2>
    <p class="muted">{{ job.url }}</p>
    <p>Statut : <span class="status">{{ job.status }}</span></p>
    <p class="muted">Démarrage : {{ job.started_at }}{% if job.finished_at %} · Fin : {{ job.finished_at }}{% endif %}</p>
    {% if job.error %}<p class="error">{{ job.error }}</p>{% endif %}
  </div>

  {% if job.outputs %}
  <div class="card">
    <h2>Clips produits</h2>
    {% for clip in job.outputs %}
    <div class="clip">
      <h3>{{ clip.title }} {% if clip.score != "?" %}<span class="muted">(score {{ clip.score }})</span>{% endif %}</h3>
      <video controls playsinline preload="metadata" src="{{ clip.url }}"></video>
      <p class="muted"><a href="{{ clip.url }}" download>Télécharger {{ clip.name }}</a></p>
    </div>
    {% endfor %}
  </div>
  {% endif %}

  <div class="card">
    <h2>Progression</h2>
    <pre>{{ job.log_text or "Démarrage du traitement..." }}</pre>
  </div>

  <p><a href="{{ url_for('index') }}">← Nouveau clip</a></p>
{% else %}
  {% if busy %}<div class="card error">Un traitement est déjà en cours.</div>{% endif %}
  {% if error %}<div class="card error">{{ error }}</div>{% endif %}

  {% if cloud %}
  <div class="card">
    <p class="muted">Le traitement tourne sur un serveur dans le cloud : tu peux fermer l'application, le PC n'a pas besoin d'être allumé. Les clips restent disponibles quelques jours.</p>
  </div>
  {% endif %}

  {% if apk %}
  <div class="card">
    <h2>Application Android</h2>
    <p class="muted">Télécharge l'APK, installe-le (autorise l'installation hors Play Store), puis saisis l'adresse de ce serveur.</p>
    <p><a href="{{ url_for('apk_file') }}">Télécharger ClipMaker.apk</a></p>
  </div>
  {% endif %}

  <div class="card">
    <h2>Nouveau clip</h2>
    <form method="post" action="{{ url_for('run_job') }}">
      <label for="url">URL YouTube</label>
      <input id="url" name="url" type="url" required
             placeholder="https://www.youtube.com/watch?v=...">
      <label for="clips">Nombre de clips (1 à 10)</label>
      <input id="clips" name="clips" type="number" min="1" max="10" value="5">
      <button type="submit" {% if busy %}disabled{% endif %}>Lancer le traitement</button>
    </form>
  </div>

  {% if cloud %}
  <div class="card">
    <h2>Ou envoyer une vidéo</h2>
    <p class="muted">Si YouTube refuse le serveur, télécharge la vidéo sur le téléphone puis envoie-la ici (MP4, MKV, WEBM ou MOV, {{ max_upload_mb }} Mo max).</p>
    <form id="upload_form" method="post" action="{{ url_for('upload_job') }}" enctype="multipart/form-data">
      <label for="video">Fichier vidéo</label>
      <input id="video" name="video" type="file" accept="video/*" required>
      <p class="muted" id="upload_info"></p>
      <label for="upload_clips">Nombre de clips (1 à 10)</label>
      <input id="upload_clips" name="clips" type="number" min="1" max="10" value="5">
      <button type="submit">Envoyer et lancer</button>
    </form>
    <script>
      // Supabase plafonne à 50 Mo par fichier : la vidéo part en morceaux de
      // {{ chunk_size_bytes / 1000000 }} Mo, réassemblés par le worker.
      (function () {
        var form = document.getElementById("upload_form");
        var input = document.getElementById("video");
        var info = document.getElementById("upload_info");
        var clips = document.getElementById("upload_clips");
        var button = form.querySelector("button[type=submit]");
        var limit = {{ max_upload_mb }};
        var chunkSize = {{ chunk_size_bytes }};

        function say(text, bad) {
          info.textContent = text;
          info.style.color = bad ? "#ff9a9a" : "";
        }
        function check() {
          var file = input.files && input.files[0];
          if (!file) { say("", false); return true; }
          var mb = file.size / 1000000;
          if (mb > limit) {
            say("Fichier : " + mb.toFixed(1) + " Mo — trop lourd (limite " + limit + " Mo).", true);
            return false;
          }
          say("Fichier : " + mb.toFixed(1) + " Mo — envoi en " + Math.ceil(file.size / chunkSize) + " morceau(x).", false);
          return true;
        }
        async function post(url, fields, piece) {
          var body = new FormData();
          for (var key in fields) body.append(key, fields[key]);
          if (piece) body.append("chunk", piece, "part.bin");
          var response = await fetch(url, { method: "POST", body: body });
          var data = {};
          try { data = await response.json(); } catch (err) {}
          if (!response.ok) throw new Error(data.error || ("Erreur " + response.status));
          return data;
        }
        input.addEventListener("change", check);
        form.addEventListener("submit", async function (event) {
          event.preventDefault();
          if (!check()) { info.scrollIntoView({ block: "center" }); return; }
          var file = input.files[0];
          button.disabled = true;
          button.textContent = "Envoi en cours… ne ferme pas la page";
          try {
            var start = await post("/upload/start", {
              name: file.name, size: file.size, clips: clips.value
            });
            for (var i = 0; i < start.chunks; i++) {
              say("Envoi du morceau " + (i + 1) + " / " + start.chunks + "…", false);
              var piece = file.slice(i * start.chunk_size, Math.min(file.size, (i + 1) * start.chunk_size));
              await post("/upload/chunk", { token: start.token, index: i }, piece);
            }
            say("Vidéo envoyée, traitement lancé…", false);
            var done = await post("/upload/finish", {
              token: start.token, size: file.size, clips: clips.value
            });
            window.location = "/job/" + done.job_id;
          } catch (err) {
            say(err.message || "Envoi interrompu. Réessaie.", true);
            button.disabled = false;
            button.textContent = "Envoyer et lancer";
          }
        });
      })();
    </script>
  </div>
  {% endif %}

  {% if jobs %}
  <div class="card">
    <h2>Derniers traitements</h2>
    {% for item in jobs %}
    <p>
      <a href="{{ url_for('job_page', job_id=item.id) }}">{{ item.started_at }} — {{ item.clips }} clip(s)</a>
      <span class="badge">{{ item.status }}</span><br>
      <span class="muted">{{ item.url }}</span>
    </p>
    {% endfor %}
  </div>
  {% endif %}

  {% if outputs %}
  <div class="card">
    <h2>Clips déjà générés</h2>
    {% for clip in outputs %}
    <p>
      <a href="{{ clip.url }}">{{ clip.name }}</a>
      <span class="muted">{{ clip.size_mb }} Mo</span>
    </p>
    {% endfor %}
  </div>
  {% endif %}
{% endif %}
</main>
</body>
</html>
"""


if __name__ == "__main__":
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    print("Interface ClipMaker démarrée." + (" (mode cloud)" if CLOUD else ""))
    print(f"Sur cet ordinateur : http://127.0.0.1:{PORT}")
    if not CLOUD:
        for ip in _local_ips():
            print(f"Sur le téléphone  : http://{ip}:{PORT}")
        print("Le téléphone doit être sur le même Wi-Fi que cet ordinateur.")
    if _app_password() and not os.getenv("SECRET_KEY", "").strip():
        print(
            "Attention : SECRET_KEY n'est pas défini, les sessions seront "
            "invalidées à chaque redémarrage."
        )
    app.run(host="0.0.0.0", port=PORT, threaded=True)
