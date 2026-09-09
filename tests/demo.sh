#!/usr/bin/env bash
# The screen on this machine, against the fake bridge: for looking at pages, never for a box.
set -euo pipefail
cd "$(dirname "$0")/.."
PY=python3; [[ -x .venv/bin/python3 ]] && PY=.venv/bin/python3
ETC=$(mktemp -d); SOCK=/tmp/mesh-manager-fake-bridge.sock
"$PY" tests/fake_bridge.py "$SOCK" &
trap 'kill %1 2>/dev/null || true' EXIT
sleep 1
MESH_MANAGER_PASSWORD="demo-demo-demo" PYTHONPATH=src "$PY" -m mesh_manager.web --etc "$ETC" --write-password
echo "demo password: demo-demo-demo"
PYTHONPATH=src exec "$PY" -m mesh_manager.web --config /nonexistent --socket "$SOCK" --etc "$ETC" --bind 127.0.0.1 --port "${PORT:-8095}"
