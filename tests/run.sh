#!/usr/bin/env bash
# Run every suite; the PR carries this output. Agent-reported output has no evidential
# standing, so run it yourself and paste it.
set -uo pipefail
cd "$(dirname "$0")/.."
PY="${PYTHON:-python3}"; [[ -z "${PYTHON:-}" && -x .venv/bin/python3 ]] && PY=.venv/bin/python3
rc=0; n=0; bad=0
for t in tests/test_*.py; do
    n=$((n+1))
    echo "=================== $t"
    if "$PY" "$t"; then echo "PASS $t"; else echo "FAIL $t"; bad=$((bad+1)); rc=1; fi
done
echo
echo "suites: $n  failing: $bad"
exit $rc
