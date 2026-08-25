#!/bin/sh
set -e

mkdir -p "$XDG_RUNTIME_DIR" 2>/dev/null || true
chmod 700 "$XDG_RUNTIME_DIR" 2>/dev/null || true

case "${1:-api}" in
  api)
    exec uvicorn app.main:app \
      --host "${PF_API_HOST:-0.0.0.0}" \
      --port "${PF_API_PORT:-8000}" \
      --no-access-log
    ;;
  worker)
    exec python -m app.worker
    ;;
  shell)
    exec /bin/bash
    ;;
  *)
    exec "$@"
    ;;
esac
