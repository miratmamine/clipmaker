#!/usr/bin/env bash
# Lance le worker en arrière-plan et gunicorn au premier plan.
# Conçu pour le conteneur « tout-en-un » (VPS, docker-compose, PC allumé).
# Sur Render + GitHub Actions, l'interface et le worker tournent séparément :
# voir render.yaml et .github/workflows/worker.yml.
set -uo pipefail

PORT="${PORT:-7860}"
WEB_WORKERS="${WEB_CONCURRENCY:-2}"
WEB_THREADS="${WEB_THREADS:-4}"

python worker.py &
worker_pid=$!

gunicorn \
  --bind "0.0.0.0:${PORT}" \
  --workers "${WEB_WORKERS}" \
  --threads "${WEB_THREADS}" \
  --timeout 120 \
  --access-logfile - \
  --error-logfile - \
  webapp:app &
web_pid=$!

shutdown() {
  kill -TERM "${worker_pid}" "${web_pid}" 2>/dev/null || true
  wait 2>/dev/null || true
  exit 0
}

trap shutdown TERM INT

# Si l'un des deux processus s'arrête, on arrête l'autre.
wait -n "${worker_pid}" "${web_pid}"
status=$?
kill -TERM "${worker_pid}" "${web_pid}" 2>/dev/null || true
wait 2>/dev/null || true
exit "${status}"
