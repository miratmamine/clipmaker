"""Tests du mode cloud : base PostgreSQL, Groq simulé, Storage simulé, worker, web.

Lancement :
    python test_cloud.py

Le test démarre un PostgreSQL embarqué (paquet ``pgserver``), deux faux serveurs
HTTP (Groq et Supabase Storage) et une vraie vidéo de quelques secondes
générée par ffmpeg. Aucun accès réseau externe n'est nécessaire.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PROJECT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT))

TEMP = Path(tempfile.mkdtemp(prefix="clipmaker_cloud_test_"))
os.environ["CLIPMAKER_DATA_DIR"] = str(TEMP / "data")
os.environ["GROQ_API_KEY"] = "test-groq-key"
os.environ["GROQ_MIN_INTERVAL"] = "0"
os.environ["SUPABASE_SERVICE_KEY"] = "test-service-key"
os.environ["SUPABASE_BUCKET"] = "clips"
os.environ["SECRET_KEY"] = "test-secret"
os.environ["APP_PASSWORD"] = "motdepasse"
os.environ["SIGNED_URL_SECONDS"] = "600"
os.environ["MAX_ACTIVE_JOBS"] = "10"

# --- PostgreSQL embarqué -----------------------------------------------------
import pgserver  # noqa: E402

pg_dir = TEMP / "pg"
server = pgserver.get_server(str(pg_dir))
os.environ["DATABASE_URL"] = server.get_uri()

import psycopg  # noqa: E402

schema = (PROJECT / "supabase" / "schema.sql").read_text(encoding="utf-8")
with psycopg.connect(server.get_uri(), autocommit=True) as conn:
    conn.execute(schema)

import storage  # noqa: E402
import store  # noqa: E402

print("1. Base de données")
assert store.configured()
assert store.ping()

job = store.create_job("https://youtu.be/abc", 3)
job_id = str(job["id"])
assert job["status"] == "queued"
assert store.active_count() == 1

claimed = store.claim_job()
assert claimed is not None and str(claimed["id"]) == job_id
assert store.claim_job() is None

store.append_log(job_id, "ligne 1\nligne 2")
store.touch(job_id)
current = store.get_job(job_id)
assert current is not None and current["status"] == "running"
assert "ligne 1" in current["log"] and "ligne 2" in current["log"]

clip = store.add_clip(job_id, 1, "Titre", 9, "01_titre.mp4", "job/01_titre.mp4", 1234)
assert clip["storage_key"] == "job/01_titre.mp4"
clip = store.add_clip(job_id, 1, "Titre modifié", 8, "01_titre.mp4", "job/01_titre.mp4", 4321)
assert clip["title"] == "Titre modifié" and clip["size_bytes"] == 4321
assert len(store.list_clips(job_id)) == 1
assert store.get_clip(str(clip["id"])) is not None
assert len(store.list_stored_clips()) == 1

store.finish_job(job_id, "done", "", 0)
assert store.get_job(job_id)["status"] == "done"

second = store.create_job("https://youtu.be/def", 1)
second_id = str(second["id"])
store.claim_job()
with psycopg.connect(server.get_uri(), autocommit=True) as conn:
    conn.execute(
        "update jobs set heartbeat_at = now() - interval '2 hours' where id::text = %s",
        (second_id,),
    )
assert store.requeue_stale(15) == 1
assert store.get_job(second_id)["status"] == "queued"

store.requeue_job(second_id, "note de remise en file")
assert "note de remise" in store.get_job(second_id)["log"]
store.delete_clip(str(clip["id"]))
assert store.get_clip(str(clip["id"])) is None
store.finish_job(second_id, "error", "vieux job", 1)
with psycopg.connect(server.get_uri(), autocommit=True) as conn:
    conn.execute(
        "update jobs set created_at = now() - interval '100 days' where id::text = %s",
        (second_id,),
    )
assert store.purge_jobs(60) == 1
print("   OK")

# --- Faux serveurs HTTP ------------------------------------------------------
class QuietHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # noqa: ANN002
        pass


def start_server(handler):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


class GroqHandler(QuietHandler):
    calls: list[bytes] = []

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        GroqHandler.calls.append(body)
        payload = {
            "text": "Bonjour le monde deuxième partie",
            "duration": 3.0,
            "words": [
                {"word": "Bonjour", "start": 0.1, "end": 0.4},
                {"word": "le", "start": 0.5, "end": 0.7},
                {"word": "monde", "start": 0.8, "end": 1.2},
            ],
            "segments": [
                {"start": 0.1, "end": 1.2, "text": "Bonjour le monde"}
            ],
        }
        data = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


groq_httpd, groq_url = start_server(GroqHandler)
os.environ["GROQ_URL"] = groq_url + "/openai/v1/audio/transcriptions"
os.environ["GROQ_CHUNK_SECONDS"] = "3"
os.environ["GROQ_LANGUAGE"] = ""

import groq_transcriber  # noqa: E402

print("2. Transcription Groq (serveur simulé)")
video = TEMP / "video.mp4"
subprocess.run(
    [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "testsrc=size=160x120:rate=10",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440",
        "-t",
        "7",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        str(video),
    ],
    check=True,
)
groq_transcriber.WORK_DIR = str(TEMP / "transcripts")
segments = groq_transcriber.transcribe(str(video))
assert len(GroqHandler.calls) == 3, len(GroqHandler.calls)
assert len(segments) == 3, segments
starts = [segment["start"] for segment in segments]
assert starts == [0.1, 3.1, 6.1], starts
assert segments[1]["words"][0]["start"] == 3.1
assert b'timestamp_granularities[]' in GroqHandler.calls[0]
assert b'verbose_json' in GroqHandler.calls[0]

calls_before = len(GroqHandler.calls)
assert groq_transcriber.transcribe(str(video)) == segments
assert len(GroqHandler.calls) == calls_before, "cache JSON non utilisé"

sliced = groq_transcriber.slice_segments(segments, 3.15, 4.0)
assert sliced and sliced[0]["word"] == "Bonjour"
assert 0.0 <= sliced[0]["start"] < 0.3

fallback = groq_transcriber.slice_segments(
    [{"start": 0, "end": 2, "text": "un deux trois"}, {"start": 2, "end": 4, "text": "quatre"}],
    1,
    3,
)
assert fallback and fallback[0]["start"] == 0
assert all(word["end"] <= 2 for word in fallback)
print(f"   OK ({len(segments)} segments, {len(GroqHandler.calls)} appels)")


class StorageHandler(QuietHandler):
    uploads: dict[str, bytes] = {}
    deletes: list[str] = []

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        if self.path.endswith("/object/list/clips"):
            payload = json.loads(body or b"{}")
            prefix = str(payload.get("prefix") or "").strip("/")
            entries: dict[str, dict] = {}
            for key, data in sorted(StorageHandler.uploads.items()):
                rest = key[len(prefix) :].lstrip("/") if prefix else key
                if prefix and not key.startswith(prefix):
                    continue
                if not rest:
                    continue
                name = rest.split("/", 1)[0]
                entries.setdefault(
                    name,
                    {
                        "name": name,
                        "id": None,
                        "created_at": "2026-01-01T00:00:00Z",
                        "updated_at": "2026-01-01T00:00:00Z",
                        "metadata": {"size": len(data)},
                    },
                )
            data = json.dumps(list(entries.values())).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if "/object/sign/clips/" in self.path:
            key = self.path.split("/object/sign/clips/", 1)[1]
            data = json.dumps(
                {"signedURL": f"/object/sign/clips/{key}?token=abc"}
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if "/object/clips/" in self.path:
            key = self.path.split("/object/clips/", 1)[1]
            if key == "bad.mp4":
                data = json.dumps({"message": "refusé"}).encode("utf-8")
                self.send_response(400)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            StorageHandler.uploads[key] = body
            data = b'{"Key":"ok"}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        self.send_response(404)
        self.end_headers()

    def do_DELETE(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        payload = json.loads(body or b"{}")
        for key in payload.get("prefixes", []):
            StorageHandler.deletes.append(key)
            StorageHandler.uploads.pop(key, None)
        data = b"[]"
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802
        marker = "/object/clips/"
        if marker in self.path:
            key = self.path.split(marker, 1)[1].split("?", 1)[0]
            body = StorageHandler.uploads.get(key)
            if body is None:
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()


storage_httpd, storage_url = start_server(StorageHandler)
os.environ["SUPABASE_URL"] = storage_url

print("3. Supabase Storage (serveur simulé)")
key = storage.safe_key("abc-123", 1, "01_Été à Paris.mp4")
assert key == "abc-123/01_Ete_a_Paris.mp4", key
sample = TEMP / "01_Été à Paris.mp4"
sample.write_bytes(b"video-bytes")
assert storage.upload_file(sample, key) == key
assert StorageHandler.uploads[key] == b"video-bytes"
signed = storage.signed_url(key, expires=600)
assert signed == f"{storage_url}/storage/v1/object/sign/clips/{key}?token=abc", signed
assert storage.delete_file(key)
assert key in StorageHandler.deletes
try:
    storage.upload_file(sample, "bad.mp4")
except storage.StorageError as exc:
    assert "refusé" in str(exc)
else:
    raise AssertionError("un refus Storage aurait dû lever StorageError")

source = storage.source_key("Ma Vidéo (finale).MP4")
assert source.startswith("sources/") and source.endswith("/Ma_Video_finale.mp4"), source
assert storage.source_object(f"{storage.UPLOAD_SCHEME}{source}") == source
assert storage.source_object("https://youtu.be/x") == ""
assert storage.upload_file(sample, source) == source
assert storage.source_object(
    f"{storage.UPLOAD_SCHEME}{source}"
) in StorageHandler.uploads
assert len(storage.list_objects("sources")) == 1
assert storage.list_objects(f"sources/{source.split('/')[1]}")[0]["name"].endswith(
    ".mp4"
)
restored = storage.download_file(source, TEMP / "restored.mp4")
assert restored.read_bytes() == b"video-bytes"
assert storage.delete_file(source)
assert source not in StorageHandler.uploads
print("   OK")

# --- Pipeline complet avec backends injectés (ffmpeg réel) --------------------
print("4. Pipeline main._run avec Groq simulé (découpe réelle)")
import main  # noqa: E402
import transcriber  # noqa: E402

integration = TEMP / "integration"
transcriber.WORK_DIR = str(TEMP / "transcripts")
main.WORK_DIR = integration / "work"
main.OUTPUT_DIR = integration / "output"
main.find_downloaded = lambda url: str(video)  # type: ignore[assignment]
main.select_clips = lambda segments, n=5: [  # type: ignore[assignment]
    {"start": 0.0, "end": 3.0, "title": "Intégration", "score": 9}
]
captured: list[Path] = []
results: dict = {}
integration_code = main._run(
    "https://youtu.be/integration",
    1,
    results=results,
    transcriber=groq_transcriber.transcribe,
    clip_transcriber=lambda video_path, start, end, label, segs: (
        groq_transcriber.slice_segments(segs, start, end)
    ),
    on_clip=lambda clip, path: captured.append(path),
)
assert integration_code == 0, integration_code
assert len(results["successes"]) == 1 and not results["failures"]
assert captured and captured[0].is_file() and captured[0].stat().st_size > 0
probe = subprocess.run(
    [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height",
        "-of",
        "json",
        str(captured[0]),
    ],
    capture_output=True,
    text=True,
    check=True,
)
dimensions = json.loads(probe.stdout)["streams"][0]
assert dimensions["width"] == 1080 and dimensions["height"] == 1920, dimensions
print(f"   OK ({captured[0].name})")

# --- Worker ------------------------------------------------------------------
os.environ["SIGNED_URL_SECONDS"] = "600"
import worker  # noqa: E402

print("5. Worker")
worker.configure_dirs()
uploads: list[tuple[str, str]] = []
sources: list[str | None] = []
real_upload_file = storage.upload_file
real_delete_file = storage.delete_file


def fake_upload(path, storage_key):
    uploads.append((str(path), storage_key))
    return storage_key


worker.storage.upload_file = fake_upload  # type: ignore[assignment]
worker.storage.delete_file = lambda storage_key: True  # type: ignore[assignment]


def fake_run(url, clips, results=None, **kwargs):
    print("[1/5] Téléchargement simulé")
    print("      ligne de journal worker")
    sources.append(kwargs.get("source"))
    if kwargs.get("source"):
        # Vérifie que la vidéo envoyée depuis le téléphone a bien été récupérée.
        data = Path(str(kwargs["source"])).read_bytes()
        assert data == b"contenu-video", data
    on_clip = kwargs.get("on_clip")
    successes = []
    for index in (1, 2):
        path = worker.OUTPUT_DIR / f"0{index}_Clip_{index}.mp4"
        path.write_bytes(b"x" * 2048)
        clip = {"title": f"Clip {index}", "score": 10 - index, "start": 0, "end": 30}
        if on_clip:
            on_clip(clip, path)
        successes.append((clip, path))
    if results is not None:
        results["successes"] = successes
        results["failures"] = []
        results["exit_code"] = 0
    return 0


worker.main._run = fake_run  # type: ignore[assignment]
job3 = store.create_job("https://youtu.be/worker", 2)
claimed = store.claim_job()
assert claimed is not None
worker.process_job(claimed)

finished = store.get_job(str(job3["id"]))
assert finished["status"] == "done", finished
assert "ligne de journal worker" in finished["log"]
clips = store.list_clips(str(job3["id"]))
assert len(clips) == 2, clips
assert all(clip["storage_key"].endswith(".mp4") for clip in clips)
assert len(uploads) == 2
assert not list(worker.OUTPUT_DIR.glob("*.mp4")), "les MP4 locaux devraient être nettoyés"
assert worker.CURRENT_JOB_ID is None

worker.main._run = lambda url, clips, results=None, **kwargs: 1  # type: ignore[assignment]
job4 = store.create_job("https://youtu.be/echec", 1)
failed = store.claim_job()
worker.process_job(failed)
failed_job = store.get_job(str(job4["id"]))
assert failed_job["status"] == "error", failed_job
print("   OK")

# --- Worker en mode ponctuel (GitHub Actions) --------------------------------
print("5b. Worker --once")
worker.main._run = fake_run  # type: ignore[assignment]
once_job = store.create_job("https://youtu.be/once", 1)
assert worker.run_once() == 0
once_row = store.get_job(str(once_job["id"]))
assert once_row is not None and once_row["status"] == "done", once_row
assert len(store.list_clips(str(once_job["id"]))) == 2
# File vide : le runner doit sortir sans erreur (job GitHub de quelques secondes).
assert worker.run_once() == 0
print("   OK")

print("5c. Messages YouTube (IP de datacenter)")
import downloader  # noqa: E402

assert downloader._is_bot_check(Exception("Sign in to confirm you're not a bot"))
assert not downloader._is_bot_check(Exception("Video unavailable"))
assert "vérification" in downloader._friendly_error(
    Exception("Sign in to confirm you're not a bot"), "https://youtu.be/x"
)
print("   OK")

# --- Interface web cloud -----------------------------------------------------
print("6. Interface web (mode cloud)")
import webapp  # noqa: E402

webapp.storage.signed_url = lambda storage_key, expires=600: (  # type: ignore[assignment]
    f"https://storage.test/{storage_key}?token=abc"
)
webapp.OUTPUT_DIR = TEMP / "web-output"
webapp.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
assert webapp.CLOUD

client = webapp.app.test_client()
response = client.get("/")
assert response.status_code == 302 and "/login" in response.headers["Location"]
assert client.get("/healthz").status_code == 200

bad = client.post("/login", data={"password": "faux"})
assert bad.status_code == 401
ok = client.post("/login", data={"password": "motdepasse", "next": "/"})
assert ok.status_code == 302

# Le navigateur du téléphone refuse un fichier trop lourd avant l'envoi, avec
# la limite réelle injectée par le serveur.
home = client.get("/")
assert home.status_code == 200
assert 'id="upload_info"' in home.get_data(as_text=True)
assert "trop lourd" in home.get_data(as_text=True)
assert f"var limit = {webapp.MAX_UPLOAD_MB};" in home.get_data(as_text=True)
assert f"var chunkSize = {webapp.MAX_CHUNK_MB * 1_000_000};" in home.get_data(as_text=True)

# Sur Render, avant que render-env.txt soit collé : le service doit démarrer et
# afficher une explication, pas planter sur l'import de yt-dlp.
saved_database_url = os.environ.pop("DATABASE_URL")
try:
    health = client.get("/healthz")
    assert health.status_code == 200
    assert health.json["status"] == "config", health.json

    original_list_jobs = webapp.store.list_jobs
    webapp.store.list_jobs = lambda limit=10: (_ for _ in ()).throw(  # type: ignore[assignment]
        webapp.store.StoreError("base absente")
    )
    config_page = client.get("/")
    assert config_page.status_code == 200
    assert b"render-env.txt" in config_page.data
    webapp.store.list_jobs = original_list_jobs  # type: ignore[assignment]
finally:
    os.environ["DATABASE_URL"] = saved_database_url

created = client.post(
    "/run", data={"url": "https://youtu.be/web", "clips": "1"}
)
assert created.status_code == 302, created.status_code
web_job_id = created.headers["Location"].rstrip("/").split("/")[-1]
assert store.get_job(web_job_id) is not None

remote = store.add_clip(
    web_job_id, 1, "Clip distant", 9, "remote.mp4", "web/remote.mp4", 5000
)
page = client.get(f"/job/{web_job_id}")
assert page.status_code == 200
assert b"Clip distant" in page.data and b"en attente" in page.data

redirected = client.get(f"/clip/{remote['id']}")
assert redirected.status_code == 302
assert redirected.headers["Location"] == "https://storage.test/web/remote.mp4?token=abc"

local_file = webapp.OUTPUT_DIR / "local.mp4"
local_file.write_bytes(b"fake-mp4")
store.add_clip(web_job_id, 1, "Clip local", 9, "local.mp4", "", 8)
local_clip = store.list_clips(web_job_id)[0]
served = client.get(f"/clip/{local_clip['id']}")
assert served.status_code == 200
assert served.headers["Content-Type"].startswith("video/mp4")
assert client.get("/").status_code == 200

print("6b. Déclenchement du runner GitHub Actions")
calls: list[tuple[str, dict]] = []


class _FakeResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


original_post = webapp.httpx.post


def fake_post(url, **kwargs):
    calls.append((url, kwargs))
    return _FakeResponse(204)


webapp.httpx.post = fake_post  # type: ignore[assignment]
os.environ["GITHUB_REPO"] = "https://github.com/Test/clipmaker.git"
os.environ["GITHUB_DISPATCH_TOKEN"] = "jeton-de-test"
os.environ.pop("GITHUB_DISPATCH_EVENT", None)

assert webapp._github_repo() == "Test/clipmaker"
assert "démarré" in webapp.dispatch_runner("abc")

url, kwargs = calls[-1]
assert url == "https://api.github.com/repos/Test/clipmaker/dispatches", url
assert kwargs["headers"]["Authorization"] == "Bearer jeton-de-test"
assert kwargs["json"] == {
    "event_type": "clipmaker-job",
    "client_payload": {"job_id": "abc"},
}

webapp.httpx.post = lambda url, **kwargs: _FakeResponse(401)  # type: ignore[assignment]
assert "refusé" in webapp.dispatch_runner("abc")
webapp.httpx.post = lambda url, **kwargs: _FakeResponse(404)  # type: ignore[assignment]
assert "introuvable" in webapp.dispatch_runner("abc")

webapp.httpx.post = fake_post  # type: ignore[assignment]
dispatched = client.post(
    "/run", data={"url": "https://youtu.be/dispatch", "clips": "1"}
)
assert dispatched.status_code == 302, dispatched.status_code
dispatch_id = dispatched.headers["Location"].rstrip("/").split("/")[-1]
assert "Runner GitHub Actions démarré" in (store.get_job(dispatch_id)["log"] or "")

os.environ.pop("GITHUB_REPO", None)
os.environ.pop("GITHUB_DISPATCH_TOKEN", None)
assert "Aucun runner" in webapp.dispatch_runner("abc")
webapp.httpx.post = original_post  # type: ignore[assignment]
print("   OK")

# --- Envoi depuis le téléphone ----------------------------------------------
print("6c. Envoi d'une vidéo depuis le téléphone")
# Le worker utilise ici le vrai Storage (serveur simulé) : envoi, liste, relecture.
worker.storage.upload_file = real_upload_file  # type: ignore[assignment]
worker.storage.delete_file = real_delete_file  # type: ignore[assignment]
webapp.storage.upload_file = real_upload_file  # type: ignore[assignment]
webapp.storage.delete_file = real_delete_file  # type: ignore[assignment]

uploaded = client.post(
    "/upload",
    data={
        "clips": "1",
        "video": (io.BytesIO(b"contenu-video"), "ma video (test).mp4"),
    },
    content_type="multipart/form-data",
)
assert uploaded.status_code == 302, uploaded.status_code
upload_id = uploaded.headers["Location"].rstrip("/").split("/")[-1]
upload_job = store.get_job(upload_id)
assert upload_job is not None and upload_job["status"] == "queued", upload_job
upload_key = storage.source_object(str(upload_job["url"]))
assert upload_key.startswith("sources/"), upload_key
assert StorageHandler.uploads[upload_key] == b"contenu-video"
assert "envoyée depuis le téléphone" in (upload_job["log"] or "")
page = client.get(f"/job/{upload_id}")
assert page.status_code == 200
assert "Vidéo envoyée depuis le téléphone".encode("utf-8") in page.data

# Les jobs de l'interface web sont encore en file : on les clôt pour réclamer
# celui de l'envoi (claim_job prend le plus ancien).
for stale in (web_job_id, dispatch_id):
    row = store.get_job(stale)
    if row and row["status"] in ("queued", "running"):
        store.finish_job(stale, "done", "", 0)

claimed_upload = store.claim_job()
assert claimed_upload is not None and str(claimed_upload["id"]) == upload_id
worker.main._run = fake_run  # type: ignore[assignment]
worker.process_job(claimed_upload)
assert sources[-1] and "upload_" in str(sources[-1]), sources[-1]
assert Path(str(sources[-1])).parent == worker.WORK_DIR
assert store.get_job(upload_id)["status"] == "done"
assert upload_key not in StorageHandler.uploads, "la vidéo source devrait être supprimée"
assert upload_key in StorageHandler.deletes
print("   OK")

print("6c-bis. Envoi par morceaux (vidéo jusqu'à 500 Mo)")
saved_jobs_limit = os.environ.get("MAX_ACTIVE_JOBS", "2")
os.environ["MAX_ACTIVE_JOBS"] = "99"
try:
    total = webapp.MAX_CHUNK_MB * 1_000_000 * 2 + 1234
    started = client.post(
        "/upload/start",
        data={"name": "grosse video.mp4", "size": str(total), "clips": "1"},
    )
    assert started.status_code == 200, started.data[:200]
    meta = started.json
    assert meta["chunks"] == 3, meta
    token = meta["token"]
    for index, blob in enumerate((b"morceau-0", b"morceau-1", b"morceau-2")):
        piece = client.post(
            "/upload/chunk",
            data={
                "token": token,
                "index": str(index),
                "chunk": (io.BytesIO(blob), "part.bin"),
            },
            content_type="multipart/form-data",
        )
        assert piece.status_code == 200, piece.data[:200]
    prefix = f"sources/{token}"
    assert len([k for k in StorageHandler.uploads if k.startswith(prefix)]) == 3
    finished = client.post(
        "/upload/finish",
        data={"token": token, "size": str(total), "clips": "1"},
    )
    assert finished.status_code == 200, finished.data[:200]
    chunked_job = store.get_job(str(finished.json["job_id"]))
    assert chunked_job is not None
    assert chunked_job["url"] == f"{storage.UPLOAD_SCHEME}{prefix}/"
    assert storage.source_is_chunked(chunked_job["url"])

    assembled = worker._source_from_storage(chunked_job)
    assert assembled is not None
    assert assembled.read_bytes() == b"morceau-0morceau-1morceau-2"
    worker._drop_source(chunked_job)
    assert not any(k.startswith(prefix) for k in StorageHandler.uploads)
finally:
    os.environ["MAX_ACTIVE_JOBS"] = saved_jobs_limit
print("   OK")

print("6d. Purge des vidéos envoyées orphelines")
orphan = storage.source_key("orphelin.mp4")
storage.upload_file(TEMP / "restored.mp4", orphan)
active_key = storage.source_key("actif.mp4")
storage.upload_file(TEMP / "restored.mp4", active_key)
active_job = store.create_job(f"{storage.UPLOAD_SCHEME}{active_key}", 1)

assert worker._purge_sources() == 1
assert active_key in StorageHandler.uploads, "une vidéo en attente ne doit pas être purgée"

store.finish_job(str(active_job["id"]), "done", "", 0)
assert worker._purge_sources() == 1
assert active_key not in StorageHandler.uploads
print("   OK")


assert client.get("/logout").status_code == 302
print("   OK")

# --- Nettoyage ---------------------------------------------------------------
groq_httpd.shutdown()
storage_httpd.shutdown()
server.cleanup()
shutil.rmtree(TEMP, ignore_errors=True)
print("\nTESTS CLOUD OK")
