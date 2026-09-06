"""What the bridge and the screen share: the config file's shape and the default paths. The
screen imports this and never the gateway."""
import datetime
import os

DEFAULT_CONFIG = "/etc/mesh-manager/config"
DEFAULT_SOCKET = "/run/mesh-manager/bridge.sock"
DEFAULT_STATE = "/var/lib/vantage-mesh"          # the health contract keeps its old name

# Spec 044: the map icons a node or a group may carry. The screen draws them; the bridge validates against
# this list, so the two cannot disagree. radio is the default.
NODE_ICONS = ("radio", "person", "vehicle", "router", "repeater", "base", "drone", "boat", "bike", "dog", "box", "medic", "flag", "star")


def utc(ts=None):
    if ts is None:
        return None
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_config(path):
    """KEY=value lines; EXTRA_ARGS carries the gateway's own flags (-i <ip>, -d)."""
    conf = {"SERIAL": "", "REGION": "", "CHANNEL": "", "FILTER_GROUP": "", "EXTRA_ARGS": "",
            "BIND": "127.0.0.1", "PORT": 8093, "AUTH": "on", "MAP_TILES": "google-hybrid", "MAP_MBTILES_DIR": "", "MAP_GPS": "", "UPDATE_REPO": "", "UPDATE_MODE": "manual", "UPDATE_CHANNEL": "prerelease", "TELEMETRY_ASK_SECS": 1800, "HISTORY_DAYS": 30, "MODE": "tak-server",
            "PEER_BIND": "", "PEER_PORT": 8094, "SITE_NAME": "", "SITE_ADDRESS": "", "ROUTE_HOST": ""}
    if path and os.path.exists(path):
        for ln in open(path):
            ln = ln.strip()
            if not ln or ln.startswith("#") or "=" not in ln:
                continue
            k, v = ln.split("=", 1)
            conf[k.strip()] = v.strip()
    mode = str(conf.get("MODE") or "").strip().lower()
    conf["MODE"] = mode if mode in ("server", "hub", "desktop") else "tak-server"  # Spec 050, 052 and 058: the box's shape
    try:
        conf["PEER_PORT"] = int(conf.get("PEER_PORT") or 8094)
    except (TypeError, ValueError):
        conf["PEER_PORT"] = 8094
    args = conf["EXTRA_ARGS"].split()
    conf["ip"] = args[args.index("-i") + 1] if "-i" in args and args.index("-i") + 1 < len(args) else None
    conf["debug"] = "-d" in args
    try:
        conf["PORT"] = int(conf["PORT"])
    except (TypeError, ValueError):
        conf["PORT"] = 8093
    return conf
