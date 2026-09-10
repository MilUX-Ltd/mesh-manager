#!/usr/bin/env bash
# The screen on this machine, against the demo bridge: for looking at pages, never for a box.
# PORT=8096 to run a second one; NO_AUTH=1 to skip the sign-in (release/guide-shots.sh wants that).
# The demo bridge is `mesh_manager.demo`; it was `tests/fake_bridge.py` once and that file is long gone,
# which this script went on invoking until 10 September 2026. See docs/DEMO.md for the same thing by hand.
set -euo pipefail
cd "$(dirname "$0")/.."
PY=python3; [[ -x .venv/bin/python3 ]] && PY=.venv/bin/python3
PORT="${PORT:-8095}"
ETC=$(mktemp -d); SOCK="/tmp/mesh-manager-demo-$PORT.sock"; rm -f "$SOCK"
PYTHONPATH=src "$PY" -m mesh_manager.demo "$SOCK" &
trap 'kill %1 2>/dev/null || true' EXIT
for _ in $(seq 40); do [[ -S "$SOCK" ]] && break; sleep 0.25; done
[[ -S "$SOCK" ]] || { echo "the demo bridge did not open $SOCK" >&2; exit 2; }
if [[ "${NO_AUTH:-0}" == "1" ]]; then
    echo "no sign-in: anyone who can reach 127.0.0.1:$PORT is the operator"
    PYTHONPATH=src exec "$PY" -m mesh_manager.web --config /nonexistent --socket "$SOCK" --etc "$ETC" --bind 127.0.0.1 --port "$PORT" --no-auth
fi
MESH_MANAGER_PASSWORD="demo-demo-demo" PYTHONPATH=src "$PY" -m mesh_manager.web --etc "$ETC" --write-password
echo "demo password: demo-demo-demo"
PYTHONPATH=src exec "$PY" -m mesh_manager.web --config /nonexistent --socket "$SOCK" --etc "$ETC" --bind 127.0.0.1 --port "$PORT"
