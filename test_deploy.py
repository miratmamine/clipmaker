"""Tests de la configuration de déploiement (Render + GitHub Actions).

Lancement :
    python test_deploy.py

Aucun accès réseau : les fichiers YAML et le chiffrement des secrets sont
vérifiés hors ligne.
"""

from __future__ import annotations

import base64
import sys
import tempfile
from pathlib import Path

import yaml

PROJECT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(PROJECT / "scripts"))

print("1. render.yaml")
blueprint = yaml.safe_load((PROJECT / "render.yaml").read_text(encoding="utf-8"))
service = blueprint["services"][0]
assert service["type"] == "web"
assert service["runtime"] == "python"
assert service["plan"] == "free"
assert service["region"] == "frankfurt"
assert service["healthCheckPath"] == "/healthz"
assert "requirements-web.txt" in service["buildCommand"]
assert "$PORT" in service["startCommand"]
assert "webapp:app" in service["startCommand"]

env_vars = {item["key"]: item for item in service["envVars"]}
for required in (
    "DATABASE_URL",
    "SUPABASE_URL",
    "SUPABASE_SERVICE_KEY",
    "APP_PASSWORD",
    "SECRET_KEY",
    "GITHUB_REPO",
    "GITHUB_DISPATCH_TOKEN",
):
    assert required in env_vars, required
    assert env_vars[required].get("sync") is False, required
# Render fournit PORT lui-même : le fichier .env ne doit pas l'écraser.
assert "PORT" not in env_vars
assert env_vars["MAX_UPLOAD_MB"]["value"] == "45"
print("   OK")

print("2. workflow GitHub Actions")
workflow = yaml.safe_load(
    (PROJECT / ".github" / "workflows" / "worker.yml").read_text(encoding="utf-8")
)
# PyYAML (YAML 1.1) transforme la clé « on » en booléen True.
triggers = workflow.get("on") or workflow.get(True)
assert triggers, "bloc « on » absent"
assert "repository_dispatch" in triggers
assert "clipmaker-job" in triggers["repository_dispatch"]["types"]
assert "workflow_dispatch" in triggers
assert "schedule" in triggers

job = workflow["jobs"]["clip"]
assert job["runs-on"] == "ubuntu-latest"
assert job["timeout-minutes"] >= 60
# Le contexte « runner » n'existe qu'au niveau d'une étape : GitHub rejette le
# fichier entier s'il apparaît dans jobs.<id>.env.
for name, value in job["env"].items():
    assert "${{ runner." not in str(value), name
run_scripts = "\n".join(str(step.get("run", "")) for step in job["steps"])
assert "python worker.py --once" in run_scripts
assert "-r requirements.txt" in run_scripts
worker_step = next(
    step for step in job["steps"] if "worker.py --once" in str(step.get("run", ""))
)
assert "CLIPMAKER_DATA_DIR" in worker_step["env"]
for key in (
    "DATABASE_URL",
    "SUPABASE_URL",
    "SUPABASE_SERVICE_KEY",
    "GROQ_API_KEY",
    "OPENAI_API_KEY",
):
    assert job["env"][key].startswith("${{ secrets."), key
# Sans cookies, YouTube refuse l'IP du runner : le secret doit être injecté et
# écrit dans un fichier lu par yt-dlp (via YTDLP_COOKIES_FILE).
cookies_steps = [
    step
    for step in job["steps"]
    if "YOUTUBE_COOKIES" in str(step.get("env", {}))
]
assert cookies_steps, "étape cookies YouTube absente"
assert cookies_steps[0]["env"]["YOUTUBE_COOKIES"].startswith("${{ secrets.")
assert "YTDLP_COOKIES_FILE" in str(cookies_steps[0].get("run", ""))
assert "base64 -d" in str(cookies_steps[0].get("run", ""))
assert "SOURCE_RETENTION_DAYS" in job["env"]
assert "bgutil" in run_scripts
assert "apt-get install" in run_scripts and "ffmpeg" in run_scripts
assert "ffprobe -version" in run_scripts
print("   OK")

print("3. requirements-web.txt")
web_text = (PROJECT / "requirements-web.txt").read_text(encoding="utf-8")
web_reqs = "\n".join(
    line for line in web_text.splitlines() if not line.strip().startswith("#")
).lower()
for package in ("flask", "gunicorn", "httpx", "psycopg", "python-dotenv"):
    assert package in web_reqs, package
assert "faster-whisper" not in web_reqs
assert "yt-dlp" not in web_reqs
print("   OK")

print("4. setup_github : chiffrement des secrets")
import setup_github  # noqa: E402
from nacl import public as nacl_public  # noqa: E402

setup_github._SECRETS.append("valeur-secrete")
assert setup_github._redact("avant valeur-secrete apres") == "avant *** apres"
assert "valeur-secrete" not in setup_github._redact("valeur-secrete")

private_key = nacl_public.PrivateKey.generate()
public_key = base64.b64encode(bytes(private_key.public_key)).decode("utf-8")
encrypted = setup_github._encrypt(public_key, "valeur-secrete")
box = nacl_public.SealedBox(private_key)
assert box.decrypt(base64.b64decode(encrypted)).decode("utf-8") == "valeur-secrete"
print("   OK")

print("5. setup_github : render-env.txt")
with tempfile.TemporaryDirectory() as tmp:
    setup_github.RENDER_ENV_FILE = Path(tmp) / "render-env.txt"
    setup_github._write_render_env(
        "moi/clipmaker",
        "jeton",
        {
            "DATABASE_URL": "postgres://x",
            "SUPABASE_URL": "https://x.supabase.co",
            "SUPABASE_SERVICE_KEY": "cle",
            "APP_PASSWORD": "mdp",
            "SECRET_KEY": "secret",
            "PORT": "7860",
        },
    )
    content = setup_github.RENDER_ENV_FILE.read_text(encoding="utf-8")
    assert "PORT=" not in content
    assert "DATABASE_URL=postgres://x" in content
    assert "GITHUB_REPO=moi/clipmaker" in content
    assert "GITHUB_DISPATCH_TOKEN=jeton" in content
    assert "MAX_UPLOAD_MB=45" in content
print("   OK")

print("6. setup_cookies : validation du fichier Netscape")
import setup_cookies  # noqa: E402

with tempfile.TemporaryDirectory() as tmp:
    good = Path(tmp) / "cookies.txt"
    good.write_text(
        "# Netscape HTTP Cookie File\n"
        ".youtube.com\tTRUE\t/\tTRUE\t1767225600\tSID\tvaleur\n"
        ".google.com\tTRUE\t/\tTRUE\t1767225600\tNID\tvaleur\n",
        encoding="utf-8",
    )
    content = setup_cookies.validate(good)
    assert "SID" in content and content.endswith("\n")

    empty = Path(tmp) / "vide.txt"
    empty.write_text("", encoding="utf-8")
    try:
        setup_cookies.validate(empty)
    except SystemExit as exc:
        assert "vide" in str(exc)
    else:
        raise AssertionError("un fichier vide aurait dû être refusé")

    other = Path(tmp) / "autre.txt"
    other.write_text(
        "# Netscape HTTP Cookie File\n.example.com\tTRUE\t/\tTRUE\t1\tA\tb\n",
        encoding="utf-8",
    )
    try:
        setup_cookies.validate(other)
    except SystemExit as exc:
        assert "YouTube" in str(exc)
    else:
        raise AssertionError("un fichier sans cookie YouTube aurait dû être refusé")

    broken = Path(tmp) / "casse.txt"
    broken.write_text("youtube.com pas au format netscape\n", encoding="utf-8")
    try:
        setup_cookies.validate(broken)
    except SystemExit as exc:
        assert "Netscape" in str(exc)
    else:
        raise AssertionError("un format invalide aurait dû être refusé")
print("   OK")

print("\nTESTS DEPLOY OK")
