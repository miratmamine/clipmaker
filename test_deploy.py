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
run_scripts = "\n".join(str(step.get("run", "")) for step in job["steps"])
assert "python worker.py --once" in run_scripts
assert "-r requirements.txt" in run_scripts
for key in (
    "DATABASE_URL",
    "SUPABASE_URL",
    "SUPABASE_SERVICE_KEY",
    "GROQ_API_KEY",
    "OPENAI_API_KEY",
):
    assert job["env"][key].startswith("${{ secrets."), key
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
print("   OK")

print("\nTESTS DEPLOY OK")
