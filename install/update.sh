#!/usr/bin/env bash
# update.sh - apply the release the screen staged and verified (Spec 015). Runs as root from
# mesh-manager-update.service; the screen may start that unit and nothing else. Finds the newest
# staging directory with a READY marker under the state directory, checks the tarball's sha256
# once more, and runs the STAGED installer on it with no flags, so the box keeps its config.
# --dry-run says what it would do. MESH_MANAGER_STATE relocates the state directory (the suite).
set -uo pipefail
STATE="${MESH_MANAGER_STATE:-/var/lib/vantage-mesh}"
DRY=0; [[ "${1:-}" == "--dry-run" ]] && DRY=1
LOG="$STATE/updates/last.log"
ready=$(ls -t "$STATE"/updates/*/READY 2>/dev/null | head -1 || true)
[[ -n "$ready" ]] || { echo "nothing staged under $STATE/updates"; exit 2; }
d=$(dirname "$ready"); tgz=$(head -1 "$ready"); ver=$(basename "$d")
[[ -f "$tgz" && -f "$tgz.sha256" && -f "$d/install.sh" ]] || { echo "staging for $ver is incomplete"; exit 2; }
want=$(awk '{print $1}' "$tgz.sha256"); got=$(sha256sum "$tgz" | awk '{print $1}')
[[ "$want" == "$got" ]] || { echo "sha256 mismatch for $ver: refusing"; exit 2; }
echo "sha256 ok for $ver ($tgz)"
if (( DRY )); then echo "would run: bash $d/install.sh $tgz"; exit 0; fi
mkdir -p "$(dirname "$LOG")"
{
    echo "== $(date -u +%Y-%m-%dT%H:%M:%SZ) applying $ver from $d"
    bash "$d/install.sh" "$tgz"; rc=$?
    echo "== $(date -u +%Y-%m-%dT%H:%M:%SZ) installer exited $rc"
    (( rc == 0 )) && rm -f "$ready"
    exit $rc
} > "$LOG" 2>&1
