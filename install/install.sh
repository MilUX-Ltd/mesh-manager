#!/usr/bin/env bash
# install.sh - install Mesh Manager on THIS box from a release tarball (Spec 001, slice 1).
#
#   sudo ./install.sh <mesh-manager-<ver>-<arch>.tgz> [--serial <by-id path>]
#        [--filter-group <group>] [--region EU_868|US] [--channel <name>]
#        [--password <operator password>] [--no-auth] [--bind <addr>] [--port <n>] [--dry-run]
#        [--mode tak-server|server|hub]   (server: a box with no TAK Server beside it, Spec 050; hub: a site with no radio
#        that other Mesh Managers join, Spec 052) [--peer-bind <addr>] [--peer-port <n>] [--site-name <name>] [--site-address <host>]
#        [--tls-route <host>]   (Spec 057: Caddy fronts the screen at https://<host>; the firewall is yours to open, 80 and 443)
#
# What it does, in order: verify the tarball against its .sha256; unpack to /opt/mesh-manager;
# build the venv from the bundled wheels with no network; offline is only proven offline;
# apply the site-package patch; prove the patched import; create the TAK input WITH its filter
# group if absent (LESSONS 16; an existing input is left alone); write /etc/mesh-manager/config;
# create the heartbeat directory (its path is the health contract and keeps its old name);
# install and start mesh-manager-bridge.service; restart TAK Server once only if the input was
# new; say what is closed. Idempotent: a box already at this release reports nothing to change.
#
# Adopts a box running the earlier vendored gateway: with no --serial/--filter-group it reads
# /etc/vantage-mesh.conf, stops and disables tak-meshtastic-gateway, keeps that unit file as
# the rollback, and never touches the radio's channel. Nothing here runs any firewall
# tool: the screen, when it arrives, binds loopback until the operator
# opens it.
#
# MESH_MANAGER_ROOT=<dir> relocates every absolute path (the suite's fake root); --dry-run
# prints each action instead of taking it. Together they make the adopt logic testable off a
# box. Neither is for production use.
set -euo pipefail

ROOT="${MESH_MANAGER_ROOT:-}"
DRY=0; ROUTE_HOST_ARG=""; ROUTE_HOST=""; TARBALL=""; SERIAL=""; REGION=""; CHANNEL=""; FILTER_GROUP=""; PASSWORD=""; BIND_ARG=""; PORT_ARG=""; AUTH_ARG=""; MAP_LAT_ARG=""; MAP_LON_ARG=""; TILES_ARG=""; MBTILES_ARG=""; GPS_ARG=""; CLEAR_POS=0; TOKEN_FILE=""; UPDATE_MODE_ARG=""; MODE_ARG=""; PEER_BIND_ARG=""; PEER_PORT_ARG=""; SITE_NAME_ARG=""; SITE_ADDRESS_ARG=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --serial)        SERIAL="${2:-}"; shift 2 ;;
        --filter-group)  FILTER_GROUP="${2:-}"; shift 2 ;;
        --region)        REGION="${2:-}"; shift 2 ;;
        --channel)       CHANNEL="${2:-}"; shift 2 ;;
        --password)      PASSWORD="${2:-}"; AUTH_ARG="on"; shift 2 ;;
        --no-auth)       AUTH_ARG="off"; shift ;;
        --bind)          BIND_ARG="${2:-}"; shift 2 ;;
        --port)          PORT_ARG="${2:-}"; shift 2 ;;
        --map-lat)       MAP_LAT_ARG="${2:-}"; shift 2 ;;
        --map-lon)       MAP_LON_ARG="${2:-}"; shift 2 ;;
        --tiles)         TILES_ARG="${2:-}"; shift 2 ;;
        --mbtiles-dir)   MBTILES_ARG="${2:-}"; shift 2 ;;
        --gps)           GPS_ARG="${2:-}"; shift 2 ;;
        --no-map-position) CLEAR_POS=1; shift ;;
        --github-token-file) TOKEN_FILE="${2:-}"; shift 2 ;;
        --update-mode)   UPDATE_MODE_ARG="${2:-}"; shift 2 ;;
        --mode)          MODE_ARG="${2:-}"; shift 2 ;;
        --peer-bind)     PEER_BIND_ARG="${2:-}"; shift 2 ;;
        --peer-port)     PEER_PORT_ARG="${2:-}"; shift 2 ;;
        --site-name)     SITE_NAME_ARG="${2:-}"; shift 2 ;;
        --site-address)  SITE_ADDRESS_ARG="${2:-}"; shift 2 ;;
        --tls-route)     ROUTE_HOST_ARG="${2:-}"; shift 2 ;;
        --dry-run)       DRY=1; shift ;;
        -h|--help)       sed -n '2,22p' "$0"; exit 0 ;;
        -*)              echo "ERR unknown option: $1" >&2; exit 2 ;;
        *)               [[ -z "$TARBALL" ]] && TARBALL="$1" || { echo "ERR one tarball only" >&2; exit 2; }; shift ;;
    esac
done
# the interpreter for the venv: each cut is built for one Python (release/PYTHON in the tarball; 3.12 for Ubuntu 24.04,
# 3.14 for 26.04, named -py314). The box needs that python and its venv module; MESH_MANAGER_PYTHON names one outright.
PYT="3.12"
if [[ -n "$TARBALL" && -f "$TARBALL" ]]; then
    _t=$(tar -xzOf "$TARBALL" release/PYTHON 2>/dev/null | tr -d '[:space:]'); [[ "$_t" =~ ^3\.[0-9]+$ ]] && PYT="$_t"
fi
PY="${MESH_MANAGER_PYTHON:-$(command -v "python$PYT" || command -v python3 || echo python3)}"
[[ -n "$TARBALL" ]] || { echo "ERR usage: install.sh <release.tgz> [--serial ...] [--filter-group ...] [--dry-run]" >&2; exit 2; }

log()  { printf '%s %s\n' "$(date -u '+%H:%M:%S')" "$*"; }
die()  { echo "ERR $*" >&2; exit 2; }
act()  { if (( DRY )); then echo "would: $*"; else log "$*"; fi; }   # a named action
do_()  { (( DRY )) && return 0; "$@"; }                               # run it unless dry
read_conf() {  # KEY=value lines of a config into <prefix>KEY, portably (no eval, no GNU sed)
    local file="$1" prefix="$2" k v
    while IFS='=' read -r k v; do
        case "$k" in SERIAL|REGION|CHANNEL|FILTER_GROUP|EXTRA_ARGS|BIND|PORT|AUTH|MAP_LAT|MAP_LON|MAP_TILES|MAP_MBTILES_DIR|MAP_GPS|UPDATE_REPO|UPDATE_MODE|UPDATE_CHANNEL|MODE|PEER_BIND|PEER_PORT|SITE_NAME|SITE_ADDRESS|ROUTE_HOST) printf -v "${prefix}${k}" '%s' "$v" ;; esac
    done < "$file"
}

# logical paths (what we print) and real paths (what we touch)
L_OPT=/opt/mesh-manager;            OPT="$ROOT$L_OPT"
L_ETC=/etc/mesh-manager;            ETC="$ROOT$L_ETC"
L_CONF="$L_ETC/config";             CONF="$ROOT$L_CONF"
L_OLDCONF=/etc/vantage-mesh.conf;   OLDCONF="$ROOT$L_OLDCONF"
L_STATE=/var/lib/vantage-mesh;      STATE="$ROOT$L_STATE"
L_UNITDIR=/etc/systemd/system;      UNITDIR="$ROOT$L_UNITDIR"
L_CC=/opt/tak/CoreConfig.xml;       CC="$ROOT$L_CC"
UNIT=mesh-manager-bridge; WEBUNIT=mesh-manager-web; OLDUNIT=tak-meshtastic-gateway; SVCUSER=mesh-manager
ADOPTED="$ETC/adopted-from-vantage-mesh"
MCAST_GROUP=239.2.3.1; MCAST_PORT=6970; BIND="${BIND_ARG:-127.0.0.1}"; PORT="${PORT_ARG:-8093}"
[[ "$BIND" =~ ^[0-9.]{7,15}$ ]] || die "bad --bind (an IPv4 address)"
[[ "$PORT" =~ ^[0-9]{2,5}$ ]] || die "bad --port"

if (( ! DRY )); then
    [[ $EUID -eq 0 ]] || die "run as root (sudo)"
    [[ -f "$TARBALL" ]] || die "no such tarball: $TARBALL"
    [[ -f "$TARBALL.sha256" ]] || die "no $TARBALL.sha256 beside the tarball - the cut writes one; carry both"
    want=$(awk '{print $1}' "$TARBALL.sha256"); got=$(sha256sum "$TARBALL" | awk '{print $1}')
    [[ "$want" == "$got" ]] || die "tarball hash mismatch: got $got want $want"
    # the release's compiled wheels are for Python 3.12 (Ubuntu 24.04's python3). On an older Ubuntu, install
    # python3.12 and python3.12-venv (the deadsnakes PPA on 22.04) and the installer finds it; MESH_MANAGER_PYTHON
    # names an interpreter outright.
    command -v "$PY" >/dev/null || die "python3 is missing"
    PYV=$("$PY" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || echo "?")
    [[ "$PYV" == "$PYT" ]] || die "this release is built for Python $PYT and $PY is $PYV: install python$PYT and python$PYT-venv, take the release cut for this box's Python (Ubuntu 26.04: the -py314 tarball), or set MESH_MANAGER_PYTHON"
    "$PY" -c 'import venv, ensurepip' >/dev/null 2>&1 || die "the venv module or ensurepip is missing for $PY (apt-get install python$PYT-venv)"
    command -v patch >/dev/null || die "patch is missing (apt-get install patch): the installer patches the gateway's site-packages with it"
    command -v sha256sum >/dev/null || die "sha256sum is missing (coreutils)"
else
    echo "would: verify $TARBALL against $TARBALL.sha256"
fi
# the TAK Server check moved below the mode: a box installed with --mode server has none (Spec 050)

# ---- what this box already carries ---------------------------------------------------------
EXTRA_ARGS=""; CUR_SERIAL=""; CUR_REGION=""; CUR_CHANNEL=""; CUR_FILTER_GROUP=""; CUR_EXTRA_ARGS=""; CUR_BIND=""; CUR_PORT=""; CUR_AUTH=""; CUR_MAP_LAT=""; CUR_MAP_LON=""; CUR_MAP_TILES=""; CUR_MAP_MBTILES_DIR=""; CUR_MAP_GPS=""; CUR_UPDATE_REPO=""; CUR_UPDATE_MODE=""; CUR_UPDATE_CHANNEL=""; AUTH="on"; MAP_LAT=""; MAP_LON=""; MAP_TILES=""; MAP_MBTILES_DIR=""; MAP_GPS=""; UPDATE_REPO=""; UPDATE_MODE=""; UPDATE_CHANNEL=""; CUR_MODE=""; MODE=""; CUR_PEER_BIND=""; CUR_PEER_PORT=""; CUR_SITE_NAME=""; CUR_SITE_ADDRESS=""; PEER_BIND=""; PEER_PORT=""; SITE_NAME=""; SITE_ADDRESS=""
OLD_SERIAL=""; OLD_REGION=""; OLD_CHANNEL=""; OLD_FILTER_GROUP=""; OLD_EXTRA_ARGS=""; OLD_BIND=""; OLD_PORT=""; OLD_AUTH=""
if [[ -f "$CONF" ]]; then
    # a previous Mesh Manager install is the first source of truth; flags override
    read_conf "$CONF" CUR_
    [[ -n "$SERIAL" ]]       || SERIAL="${CUR_SERIAL:-}"
    [[ -n "$REGION" ]]       || REGION="${CUR_REGION:-}"
    [[ -n "$CHANNEL" ]]      || CHANNEL="${CUR_CHANNEL:-}"
    [[ -n "$FILTER_GROUP" ]] || FILTER_GROUP="${CUR_FILTER_GROUP:-}"
    EXTRA_ARGS="${CUR_EXTRA_ARGS:-}"
    [[ -n "$BIND_ARG" ]] || BIND="${CUR_BIND:-$BIND}"
    [[ -n "$PORT_ARG" ]] || PORT="${CUR_PORT:-$PORT}"
    AUTH="${CUR_AUTH:-on}"
    MAP_LAT="${CUR_MAP_LAT:-}"; MAP_LON="${CUR_MAP_LON:-}"
    MAP_TILES="${CUR_MAP_TILES:-}"; MAP_MBTILES_DIR="${CUR_MAP_MBTILES_DIR:-}"; MAP_GPS="${CUR_MAP_GPS:-}"
    UPDATE_REPO="${CUR_UPDATE_REPO:-}"; UPDATE_MODE="${CUR_UPDATE_MODE:-}"; UPDATE_CHANNEL="${CUR_UPDATE_CHANNEL:-}"; MODE="${CUR_MODE:-}"; PEER_BIND="${CUR_PEER_BIND:-}"; PEER_PORT="${CUR_PEER_PORT:-}"; SITE_NAME="${CUR_SITE_NAME:-}"; SITE_ADDRESS="${CUR_SITE_ADDRESS:-}"
fi
[[ -z "$UPDATE_MODE_ARG" ]] || UPDATE_MODE="$UPDATE_MODE_ARG"
UPDATE_REPO="${UPDATE_REPO:-MilUX-Ltd/mesh-manager}"; UPDATE_MODE="${UPDATE_MODE:-manual}"; UPDATE_CHANNEL="${UPDATE_CHANNEL:-prerelease}"
[[ "$UPDATE_MODE" =~ ^(manual|auto|off)$ ]] || die "--update-mode is manual, auto or off"
[[ -z "$MODE_ARG" ]] || MODE="$MODE_ARG"; MODE="${MODE:-tak-server}"
[[ "$MODE" =~ ^(tak-server|server|hub)$ ]] || die "--mode must be tak-server, server or hub"
[[ "$MODE" != tak-server || -d "$ROOT/opt/tak" ]] || die "TAK Server is not installed on this box (/opt/tak missing); a box with no TAK Server installs with --mode server, a site with no radio with --mode hub"
[[ -z "$PEER_BIND_ARG" ]] || PEER_BIND="$PEER_BIND_ARG"; [[ -z "$PEER_PORT_ARG" ]] || PEER_PORT="$PEER_PORT_ARG"
[[ -z "$SITE_NAME_ARG" ]] || SITE_NAME="$SITE_NAME_ARG"; [[ -z "$SITE_ADDRESS_ARG" ]] || SITE_ADDRESS="$SITE_ADDRESS_ARG"
ROUTE_HOST="${ROUTE_HOST:-${CUR_ROUTE_HOST:-}}"; [[ -z "${ROUTE_HOST_ARG:-}" ]] || ROUTE_HOST="$ROUTE_HOST_ARG"
[[ -z "$ROUTE_HOST" || "$ROUTE_HOST" =~ ^[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?)+$ ]] || die "--tls-route must be a hostname such as hub.example.org"
if [[ "$MODE" == hub ]]; then PEER_BIND="${PEER_BIND:-0.0.0.0}"; fi          # a hub listens, or it is nothing; the operator opens the port
[[ -z "$PEER_BIND" ]] || PEER_PORT="${PEER_PORT:-8094}"
[[ -z "$PEER_PORT" || "$PEER_PORT" =~ ^[0-9]{2,5}$ ]] || die "--peer-port must be a port number"
[[ -z "$TOKEN_FILE" || -f "$TOKEN_FILE" ]] || die "no such token file: $TOKEN_FILE"
[[ -z "$GPS_ARG" ]] || MAP_GPS="$GPS_ARG"
[[ -z "$MAP_GPS" || "$MAP_GPS" =~ ^/dev/serial/by-id/[A-Za-z0-9._:+=-]{4,180}$ ]] || die "--gps must be a /dev/serial/by-id/ path (the receiver, not the radio)"
[[ -z "$TILES_ARG" ]] || MAP_TILES="$TILES_ARG"
[[ -z "$MBTILES_ARG" ]] || MAP_MBTILES_DIR="$MBTILES_ARG"
[[ -z "$MAP_TILES" || "$MAP_TILES" =~ ^(google-hybrid|google-roads|osm|local)$ ]] || die "--tiles is one of google-hybrid, google-roads, osm, local"
[[ -z "$MAP_MBTILES_DIR" || "$MAP_MBTILES_DIR" =~ ^/[A-Za-z0-9._/-]{1,200}$ ]] || die "--mbtiles-dir must be an absolute path"
[[ -z "$AUTH_ARG" ]] || AUTH="$AUTH_ARG"
[[ -z "$MAP_LAT_ARG" ]] || MAP_LAT="$MAP_LAT_ARG"
[[ -z "$MAP_LON_ARG" ]] || MAP_LON="$MAP_LON_ARG"
if (( CLEAR_POS )); then MAP_LAT=""; MAP_LON=""; fi   # the receiver, the radio's fix or the devices place the box instead
# the box's position for the map, for a box without GPS: both or neither, decimal degrees
[[ -z "$MAP_LAT$MAP_LON" || ( "$MAP_LAT" =~ ^-?[0-9]{1,2}(\.[0-9]+)?$ && "$MAP_LON" =~ ^-?[0-9]{1,3}(\.[0-9]+)?$ ) ]] \
    || die "--map-lat and --map-lon go together, in decimal degrees (51.2100 -1.5000)"
ADOPTING=0
if [[ ( -z "$SERIAL" || -z "$FILTER_GROUP" ) && -f "$OLDCONF" ]]; then
    ADOPTING=1
    log "adopting $L_OLDCONF (the earlier gateway's config)"
    read_conf "$OLDCONF" OLD_
    [[ -n "$SERIAL" ]]       || SERIAL="${OLD_SERIAL:-}"
    [[ -n "$REGION" ]]       || REGION="${OLD_REGION:-}"
    [[ -n "$CHANNEL" ]]      || CHANNEL="${OLD_CHANNEL:-}"
    [[ -n "$FILTER_GROUP" ]] || FILTER_GROUP="${OLD_FILTER_GROUP:-}"
    [[ -n "$EXTRA_ARGS" ]]   || EXTRA_ARGS="${OLD_EXTRA_ARGS:-}"
    for k in SERIAL REGION CHANNEL FILTER_GROUP EXTRA_ARGS; do printf '  %s=%s\n' "$k" "${!k}"; done
fi
if [[ "$MODE" == hub ]]; then
    :   # a hub has no radio and no filter group
elif [[ "$MODE" == server ]]; then
    [[ -n "$SERIAL" ]] || die "give --serial </dev/serial/by-id/...> (a box without TAK Server needs no filter group)"
else
    [[ -n "$SERIAL" && -n "$FILTER_GROUP" ]] \
        || die "this box carries no mesh config to adopt: give --serial </dev/serial/by-id/...> and --filter-group <group>"
fi
[[ "$MODE" == hub && -z "$SERIAL" ]] || [[ "$SERIAL" =~ ^/dev/serial/by-id/[A-Za-z0-9._:+=-]{4,180}$ ]] || die "serial must be a /dev/serial/by-id/ path (ports shuffle; by-id does not)"
[[ "$MODE" != tak-server && -z "$FILTER_GROUP" ]] || [[ "$FILTER_GROUP" =~ ^[A-Za-z0-9_-]{1,40}$ ]] || die "bad filter group"
[[ -z "$REGION" || "$REGION" =~ ^(EU_868|US)$ ]] || die "bad region"
[[ -z "$CHANNEL" || "$CHANNEL" =~ ^[A-Za-z0-9_-]{1,11}$ ]] || die "bad channel name"
REGION="${REGION:-EU_868}"; CHANNEL="${CHANNEL:-unknown}"

# ---- idempotence: is there anything to change? --------------------------------------------
want_conf() {
    printf '# written by mesh-manager install.sh %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    printf 'SERIAL=%s\nREGION=%s\nCHANNEL=%s\nFILTER_GROUP=%s\nEXTRA_ARGS=%s\nBIND=%s\nPORT=%s\nAUTH=%s\n' \
        "$SERIAL" "$REGION" "$CHANNEL" "$FILTER_GROUP" "$EXTRA_ARGS" "$BIND" "$PORT" "$AUTH"
    [[ -z "$MAP_LAT" ]] || printf 'MAP_LAT=%s\nMAP_LON=%s\n' "$MAP_LAT" "$MAP_LON"
    [[ -z "$MAP_TILES" ]] || printf 'MAP_TILES=%s\n' "$MAP_TILES"
    [[ -z "$MAP_MBTILES_DIR" ]] || printf 'MAP_MBTILES_DIR=%s\n' "$MAP_MBTILES_DIR"
    [[ -z "$MAP_GPS" ]] || printf 'MAP_GPS=%s\n' "$MAP_GPS"
    printf 'UPDATE_REPO=%s\nUPDATE_MODE=%s\nUPDATE_CHANNEL=%s\n' "$UPDATE_REPO" "$UPDATE_MODE" "$UPDATE_CHANNEL"
    [[ "$MODE" == tak-server ]] || printf 'MODE=%s\n' "$MODE"   # the default stays implicit, so a box already installed reports nothing to change
    [[ -z "$PEER_BIND" ]] || printf 'PEER_BIND=%s\nPEER_PORT=%s\n' "$PEER_BIND" "$PEER_PORT"
    [[ -z "$SITE_NAME" ]] || printf 'SITE_NAME=%s\n' "$SITE_NAME"
    [[ -z "$SITE_ADDRESS" ]] || printf 'SITE_ADDRESS=%s\n' "$SITE_ADDRESS"
    [[ -z "$ROUTE_HOST" ]] || printf 'ROUTE_HOST=%s\n' "$ROUTE_HOST"
}

tls_route() {  # Spec 057: Caddy fronts the screen at https://$ROUTE_HOST; the firewall stays the operator's
    local snip="$ROOT/etc/caddy/Caddyfile.d/mesh-manager.caddy" main="$ROOT/etc/caddy/Caddyfile" imp='import /etc/caddy/Caddyfile.d/*.caddy'
    if (( DRY )); then
        act "install caddy from the distribution's packages when it is not already present (apt-get install caddy)"
    elif ! command -v caddy >/dev/null 2>&1; then
        local cand; cand=$(apt-cache policy caddy 2>/dev/null | grep -c "Candidate: [0-9]" || true)   # not grep -q: under pipefail its early exit fails the pipe (found on the live hub)
        if [[ "$cand" != "0" && -n "$cand" ]]; then
            act "apt-get install -y caddy (the distribution's package)"; do_ apt-get install -y -q caddy >/dev/null || die "caddy did not install"
        else
            die "caddy is not in this distribution's packages; install it first (https://caddyserver.com/docs/install), then run the installer again with --tls-route $ROUTE_HOST"
        fi
    fi
    act "write $snip: $ROUTE_HOST fronting 127.0.0.1:$PORT (Caddy adds X-Forwarded-For and X-Forwarded-Proto)"
    do_ mkdir -p "$(dirname "$snip")"
    do_ sh -c "printf '%s\n' '# written by mesh-manager install.sh (Spec 057); the screen stays on loopback, Caddy holds the certificate' '$ROUTE_HOST {' '    encode zstd gzip' '    reverse_proxy 127.0.0.1:$PORT' '}' > '$snip'"
    if [[ -f "$main" ]] && grep -q 'root \* /usr/share/caddy' "$main" && grep -q '^:80' "$main" && ! grep -q '^[a-z0-9.-]*\.[a-z]' "$main"; then
        act "replace the package's placeholder Caddyfile (the welcome page on :80) with one that only imports Caddyfile.d"
        do_ sh -c "printf '%s\n' '# Mesh Manager (Spec 057): the package placeholder served a welcome page on :80; sites live in Caddyfile.d' '$imp' > '$main'"
    elif [[ ! -f "$main" ]] || ! grep -qF "$imp" "$main"; then
        act "add to $main: $imp (one line; the rest of the file is left as it is)"
        do_ sh -c "mkdir -p '$(dirname "$main")'; printf '\n# Mesh Manager (Spec 057)\n%s\n' '$imp' >> '$main'"
    fi
    act "caddy validate, then systemctl enable --now caddy and reload it"
    if (( ! DRY )); then
        caddy validate --config "$main" --adapter caddyfile >/dev/null 2>&1 || die "the Caddyfile does not validate after the route was added; see caddy validate --config $main"
        systemctl enable --now caddy >/dev/null 2>&1 || die "caddy.service would not start"
        systemctl reload caddy >/dev/null 2>&1 || systemctl restart caddy || die "caddy would not reload"
    fi
    log "the route is written; the firewall is yours and nothing here touched it: open 80/tcp (the certificate is fetched over it) and 443/tcp (the screen)"
    log "then https://$ROUTE_HOST is the screen, signed in with the operator password"
}

same_conf=0
if [[ -f "$CONF" ]] && diff -q <(grep -v '^#' "$CONF") <(want_conf | grep -v '^#') >/dev/null 2>&1; then same_conf=1; fi
# a new release is new code: the bridge must restart to run it, whatever the config says
# (found 3 Sep 2026: the screen came up at 0.2.0 over a bridge still running the night's 0.1.0)
new_release=1
if [[ -f "$OPT/release/.tarball.sha256" ]]; then
    if [[ -f "$TARBALL" ]]; then
        [[ "$(cat "$OPT/release/.tarball.sha256")" == "$(sha256sum "$TARBALL" | awk '{print $1}')" ]] && new_release=0
    else
        new_release=0    # a dry run without the tarball to hand: the recorded release stands
    fi
fi
have_web=0; [[ -f "$UNITDIR/$WEBUNIT.service" ]] && { [[ -f "$ETC/passwd" || "$AUTH" == "off" ]] && have_web=1; }
old_unit_pending=0
[[ -f "$UNITDIR/$OLDUNIT.service" && ! -f "$ADOPTED" ]] && old_unit_pending=1
if (( same_conf )) && (( ! new_release )) && (( have_web )) && [[ -z "$PASSWORD" ]] && [[ -f "$UNITDIR/$UNIT.service" && -x "$OPT/venv/bin/tak-meshtastic-gateway" ]] && (( ! old_unit_pending )); then
    log "nothing to change: this box already carries Mesh Manager with this config"
    log "${PEER_BIND:+the bridge listens for peers on $PEER_BIND:$PEER_PORT (TLS, paired sites only), which you open yourself; }${PEER_BIND:-the bridge binds no port of its own}; the screen binds $BIND:$PORT$([[ "$AUTH" == "off" ]] && echo ' with sign-in off') and nothing here opened one. Closed."
    exit 0
fi

# ---- 1. the release ------------------------------------------------------------------------
act "unpack $TARBALL to $L_OPT/release (its sha256 is recorded there, so a re-run with the same tarball changes nothing and a new one restarts the bridge)"
if (( ! DRY )); then
    rm -rf "$OPT/release"; mkdir -p "$OPT/release"
    sha256sum "$TARBALL" | awk '{print $1}' > "$OPT/release/.tarball.sha256"
    tar -xzf "$TARBALL" -C "$OPT/release" --strip-components=1 || die "tarball does not unpack"
    [[ -d "$OPT/release/wheels" && -f "$OPT/release/RELEASE.json" ]] || die "release layout wrong: expected wheels/ and RELEASE.json (cut it with cut-release.sh)"
fi

# ---- 2. the venv, from the bundled wheels and nothing else ---------------------------------
act "build $L_OPT/venv from the bundled wheels (--no-index; the box downloads nothing)"
if (( ! DRY )); then
    rm -rf "$OPT/venv"; "$PY" -m venv "$OPT/venv"
    "$OPT/venv/bin/pip" install --quiet --no-index --find-links "$OPT/release/wheels" TAK-Meshtastic-Gateway mesh-manager \
        || die "venv build failed - the release must carry every wheel for this architecture (re-cut)"
    [[ -x "$OPT/venv/bin/tak-meshtastic-gateway" && -x "$OPT/venv/bin/mesh-manager-bridge" && -x "$OPT/venv/bin/mesh-manager-web" ]] \
        || die "entrypoints missing after install"
    sp=$("$OPT/venv/bin/python3" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')
    for p in "$OPT"/release/patches/sitepkg-*.patch; do
        [[ -e "$p" ]] || continue
        log "  applying $(basename "$p")"
        (cd "$sp" && patch -p1 --forward --fuzz=2) < "$p" || die "site-package patch did not apply: $(basename "$p")"
    done
    "$OPT/venv/bin/python3" -c "import tak_meshtastic_gateway, mesh_manager.bridge, mesh_manager.web; from meshtastic.protobuf import atak_pb2; from tak_meshtastic_gateway.tak_meshtastic_gateway import takv2_decode" \
        || die "patched install does not import"
    log "  import proven: tak_meshtastic_gateway (patched), atak_pb2, takv2_decode, mesh_manager"
fi

# ---- 3. the TAK input, with its filter group at creation (LESSONS 16) ----------------------
INPUT_NEW=0
if [[ "$MODE" != tak-server ]]; then
    log "MODE=$MODE: no TAK Server on this box; no TAK input to create and nothing is forwarded"
elif [[ -f "$CC" ]]; then
    if python3 - "$CC" <<'PYCHK'
import re, sys
s = re.sub(r"<!--.*?-->", "", open(sys.argv[1]).read(), flags=re.S)
sys.exit(0 if re.search(r'<input[^>]*_name="meshtastic"', s) else 1)
PYCHK
    then
        log "input 'meshtastic' already present; leaving it untouched (its port and protocol cannot be modified in place)"
    else
        INPUT_NEW=1
        act "create TAK input 'meshtastic' (mcast $MCAST_GROUP:$MCAST_PORT) with filter group $FILTER_GROUP in $L_CC"
        if (( ! DRY )); then
            cp -n "$CC" "$CC.orig-mesh-manager" || true
            MESH_GROUP="$FILTER_GROUP" MESH_MCAST="$MCAST_GROUP" MESH_PORT="$MCAST_PORT" python3 - "$CC" <<'PYCC'
import os, re, sys
p = sys.argv[1]; s = open(p).read()
grp, mc, port = os.environ["MESH_GROUP"], os.environ["MESH_MCAST"], os.environ["MESH_PORT"]
blk = (f'        <input auth="anonymous" _name="meshtastic" protocol="mcast" port="{port}" group="{mc}">\n'
       f'            <filtergroup>{grp}</filtergroup>\n        </input>\n')
m = re.search(r"^[ \t]*</network>", s, re.M)
if not m: raise SystemExit("no </network> element to anchor on")
s = s[:m.start()] + blk + s[m.start():]
open(p, "w").write(s)
if f"<filtergroup>{grp}</filtergroup>" not in open(p).read(): raise SystemExit("mesh input insertion did not verify")
print("input 'meshtastic' created with filter group " + grp)
PYCC
            chown tak:tak "$CC" 2>/dev/null || true
        fi
    fi
else
    log "no $L_CC on this box; the TAK input is not created (a box without TAK Server runs the bridge blind)"
fi

# ---- 4. the service user, state, config, the password, the units ---------------------------
act "create user $SVCUSER (system account, group $SVCUSER, in dialout) if absent"
if (( ! DRY )); then
    getent group "$SVCUSER" >/dev/null || groupadd --system "$SVCUSER"
    id "$SVCUSER" >/dev/null 2>&1 || useradd --system -g "$SVCUSER" -G dialout -d /nonexistent -s /usr/sbin/nologin "$SVCUSER"
fi
act "create $L_STATE (the heartbeat directory; its name is the health contract)"
do_ mkdir -p "$STATE"
act "create $L_STATE/firmware and $L_STATE/exports (the shelf's images and the bench's exports, $SVCUSER only)"
do_ mkdir -p "$STATE/firmware" "$STATE/exports"
do_ chown "$SVCUSER:$SVCUSER" "$STATE/firmware" "$STATE/exports" 2>/dev/null || true
do_ chmod 0750 "$STATE/firmware" "$STATE/exports" 2>/dev/null || true
act "create $L_STATE/updates (the screen stages and verifies a release there; $SVCUSER only)"
do_ mkdir -p "$STATE/updates"
do_ chown "$SVCUSER:$SVCUSER" "$STATE/updates" 2>/dev/null || true
do_ chmod 0750 "$STATE/updates" 2>/dev/null || true
act "install $L_OPT/update.sh (applies a staged release as root) and mesh-manager-update.service (oneshot, root)"
do_ cp "$OPT/release/update.sh" "$OPT/update.sh" 2>/dev/null || true
do_ chmod 0755 "$OPT/update.sh" 2>/dev/null || true
if (( ! DRY )); then
    cat > "$UNITDIR/mesh-manager-update.service" <<UNITEOF
[Unit]
Description=Mesh Manager update (apply the release the screen staged)
After=network.target

[Service]
Type=oneshot
User=root
ExecStart=$L_OPT/update.sh
UNITEOF
fi
act "install the polkit rule that lets $SVCUSER start mesh-manager-update.service, and nothing else"
if [[ -d "$ROOT/etc/polkit-1/localauthority" ]]; then   # polkit 0.105 (Ubuntu 22.04): the same grant in its own form
    do_ mkdir -p "$ROOT/etc/polkit-1/localauthority/50-local.d"
    do_ bash -c "cat > '$ROOT/etc/polkit-1/localauthority/50-local.d/51-mesh-manager-update.pkla' <<'PKLA'
[Mesh Manager may start its update service]
Identity=unix-user:$SVCUSER
Action=org.freedesktop.systemd1.manage-units
ResultAny=yes
ResultInactive=yes
ResultActive=yes
PKLA"
fi
do_ mkdir -p "$ROOT/etc/polkit-1/rules.d"   # Ubuntu 22.04 has no rules.d (polkit 0.105); create it, and write the .pkla form below
do_ bash -c "cat > '$ROOT/etc/polkit-1/rules.d/51-mesh-manager-update.rules' <<'RULES'
// written by mesh-manager install.sh: the screen may start the update unit, which installs
// the release the screen staged and verified; no other unit, no other verb.
polkit.addRule(function(action, subject) {
    if (action.id == \"org.freedesktop.systemd1.manage-units\" &&
        action.lookup(\"unit\") == \"mesh-manager-update.service\" &&
        action.lookup(\"verb\") == \"start\" &&
        subject.user == \"$SVCUSER\") {
        return polkit.Result.YES;
    }
});
RULES"
if [[ -n "$TOKEN_FILE" ]]; then
    act "write $L_ETC/github.token from $TOKEN_FILE (0600, $SVCUSER): the read-only token the screen checks releases with"
    if (( ! DRY )); then
        mkdir -p "$ETC"; tr -d '[:space:]' < "$TOKEN_FILE" > "$ETC/github.token"; chown "$SVCUSER:$SVCUSER" "$ETC/github.token"; chmod 0600 "$ETC/github.token"
    fi
fi
act "install the polkit rule that lets $SVCUSER mount and unmount a removable device through udisks (a bootloader's UF2 volume), and nothing else"
do_ mkdir -p "$ROOT/etc/polkit-1/rules.d"
do_ mkdir -p "$ROOT/etc/polkit-1/rules.d"   # Ubuntu 22.04 has no rules.d (polkit 0.105); create it, and write the .pkla form below
do_ bash -c "cat > '$ROOT/etc/polkit-1/rules.d/50-mesh-manager-udisks.rules' <<'RULES'
// written by mesh-manager install.sh: the bridge flashes nRF52 devices by copying a UF2 onto the
// bootloader's volume; mounting that volume needs udisks, and this rule grants the bridge's
// user that and nothing else.
polkit.addRule(function(action, subject) {
    if (subject.user == \"$SVCUSER\" &&
        (action.id == \"org.freedesktop.udisks2.filesystem-mount\" ||
         action.id == \"org.freedesktop.udisks2.filesystem-mount-other-seat\" ||
         action.id == \"org.freedesktop.udisks2.filesystem-unmount-others\")) {
        return polkit.Result.YES;
    }
});
RULES"
act "write $L_CONF"
if (( DRY )); then want_conf | grep -v "^#" | sed "s/^/would write: /"; fi
if (( ! DRY )); then
    mkdir -p "$ETC"; want_conf > "$CONF"; chmod 0644 "$CONF"
    chown "root:$SVCUSER" "$ETC"; chmod 0770 "$ETC"   # the screen writes connections, the brief, the audit and the token here
fi
if [[ "$AUTH" == "off" ]]; then
    act "sign-in off (AUTH=off): anyone who can reach $BIND:$PORT is the operator; turn it on with --password"
elif [[ -n "$PASSWORD" || ! -f "$ETC/passwd" ]]; then
    if [[ -z "$PASSWORD" ]]; then
        PASSWORD=$("$PY" -c 'import secrets; print(secrets.token_urlsafe(12))')
        GENERATED=1
    else
        GENERATED=0
    fi
    [[ ${#PASSWORD} -ge 8 ]] || die "--password must be at least 8 characters"
    act "write the operator password hash to $L_ETC/passwd"
    if (( ! DRY )); then
        MESH_MANAGER_PASSWORD="$PASSWORD" "$OPT/venv/bin/mesh-manager-web" --etc "$ETC" --write-password >/dev/null \
            || die "could not write the password"
        chown "root:$SVCUSER" "$ETC/passwd"; chmod 0640 "$ETC/passwd"
        if (( GENERATED )); then
            log "operator password for the screen, shown ONCE (set another with --password): $PASSWORD"
        fi
    else
        (( GENERATED )) && echo "would: generate an operator password and show it once"
    fi
fi
act "write $L_ETC/web.secret (the session signing secret, owned by $SVCUSER)"
if (( ! DRY )) && [[ ! -f "$ETC/web.secret" ]]; then
    head -c 32 /dev/urandom > "$ETC/web.secret"; chown "$SVCUSER:$SVCUSER" "$ETC/web.secret"; chmod 0600 "$ETC/web.secret"
fi
act "install $UNIT.service (Type=notify, WatchdogSec=900: liveness at the serial read loop)"
if (( ! DRY )); then
    mkdir -p "$UNITDIR"
    cat > "$UNITDIR/$UNIT.service" <<UNITEOF
[Unit]
Description=Mesh Manager bridge (owns the radio, bridges the mesh into TAK)
After=network.target

[Service]
Type=notify
NotifyAccess=main
WatchdogSec=900
Group=$SVCUSER
RuntimeDirectory=mesh-manager
RuntimeDirectoryMode=0750
ExecStart=$L_OPT/venv/bin/mesh-manager-bridge --config $L_CONF
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNITEOF
fi
act "install $WEBUNIT.service (the screen, as $SVCUSER, bound to $BIND:$PORT)"
if (( ! DRY )); then
    cat > "$UNITDIR/$WEBUNIT.service" <<UNITEOF
[Unit]
Description=Mesh Manager screen
After=$UNIT.service
Wants=$UNIT.service

[Service]
Type=simple
User=$SVCUSER
Group=$SVCUSER
ExecStart=$L_OPT/venv/bin/mesh-manager-web --config $L_CONF
Restart=always
RestartSec=5
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=full
ReadWritePaths=/etc/mesh-manager /var/lib/vantage-mesh

[Install]
WantedBy=multi-user.target
UNITEOF
fi

# ---- 5. the old gateway, stopped and kept ---------------------------------------------------
if (( old_unit_pending )); then
    act "stop and disable $OLDUNIT (the earlier gateway); it held the radio until now"
    act "keeping $L_UNITDIR/$OLDUNIT.service as the rollback: restore it with systemctl disable --now $UNIT && systemctl enable --now $OLDUNIT"
    if (( ! DRY )); then
        systemctl disable --now "$OLDUNIT" 2>/dev/null || true
        mkdir -p "$ETC"; date -u +%Y-%m-%dT%H:%M:%SZ > "$ADOPTED"
    fi
fi

# ---- 6. start --------------------------------------------------------------------------------
act "systemctl daemon-reload; enable --now $UNIT and $WEBUNIT (a running screen restarts so a new bind or sign-in setting takes effect; the bridge restarts when the release is new or its radio or arguments changed)"
if (( ! DRY )); then
    systemctl daemon-reload
    if systemctl is-active --quiet "$UNIT" && { (( new_release )) || [[ "$SERIAL" != "${CUR_SERIAL:-$SERIAL}" || "$EXTRA_ARGS" != "${CUR_EXTRA_ARGS:-$EXTRA_ARGS}" ]]; }; then
        if (( new_release )); then log "a new release: restarting the bridge so it runs it (a brief mesh outage)"; else log "the bridge's radio or arguments changed: restarting it (a brief mesh outage)"; fi
        systemctl restart "$UNIT"
    fi
    systemctl enable --now "$UNIT"
    if systemctl is-active --quiet "$WEBUNIT"; then systemctl restart "$WEBUNIT"; else systemctl enable --now "$WEBUNIT"; fi
    systemctl enable "$WEBUNIT" >/dev/null 2>&1 || true
    if (( INPUT_NEW )); then
        log "restarting TAK Server to pick up the new input (brief outage)"
        systemctl restart takserver || die "takserver did not restart - read journalctl -u takserver"
    fi
    sleep 3
    systemctl is-active --quiet "$UNIT" || die "$UNIT is not active - read journalctl -u $UNIT"
    systemctl is-active --quiet "$WEBUNIT" || die "$WEBUNIT is not active - read journalctl -u $WEBUNIT"
    log "bridge running${SERIAL:+ on $SERIAL}${CHANNEL:+, channel $CHANNEL}${REGION:+, region $REGION}${FILTER_GROUP:+, filter group $FILTER_GROUP}, mode $MODE${PEER_BIND:+; peers listen on $PEER_BIND:$PEER_PORT (TLS, paired sites only; open the port yourself, the installer never does)}"
    log "the proof is a marker on a client that signed in normally, and the heartbeat updating in $L_STATE"
fi
(( INPUT_NEW )) && (( DRY )) && echo "would: restart takserver once, because the input is new"
[[ -z "$ROUTE_HOST" ]] || tls_route   # Spec 057
echo "STAGE-OK install"
log "the bridge binds no port of its own; the screen binds $BIND:$PORT and nothing here opened one${ROUTE_HOST:+; Caddy fronts it at https://$ROUTE_HOST once you open 80 and 443}. Closed."
