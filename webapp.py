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
import os
import secrets
import socket
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

CLOUD = bool(os.getenv("DATABASE_URL", "").strip())
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
app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024
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
        "url": job.get("url", ""),
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
            "url": job.get("url", ""),
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
        ),
        status,
    )


def _cloud_home(error: str = "", status: int = 200):
    try:
        jobs = _cloud_recent_jobs()
        db_error = ""
    except store.StoreError as exc:
        jobs = []
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


@app.get("/healthz")
def healthz():
    if not CLOUD:
        response = jsonify(status="ok", mode="local")
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


@app.post("/run")
def run_job():
    url = (request.form.get("url") or "").strip()
    clips_raw = (request.form.get("clips") or "5").strip()

    if not url:
        return _cloud_home("Colle une URL YouTube.", 400) if CLOUD else _home(
            "Colle une URL YouTube.", 400
        )

    try:
        clips = int(clips_raw)
    except ValueError:
        message = "Le nombre de clips doit être un entier."
        return _cloud_home(message, 400) if CLOUD else _home(message, 400)
    if not 1 <= clips <= 10:
        message = "Le nombre de clips doit être compris entre 1 et 10."
        return _cloud_home(message, 400) if CLOUD else _home(message, 400)

    if CLOUD:
        try:
            limit = int(os.getenv("MAX_ACTIVE_JOBS", "5"))
            if store.active_count() >= max(1, limit):
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
