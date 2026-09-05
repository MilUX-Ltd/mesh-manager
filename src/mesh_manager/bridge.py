"""The bridge: owns the gateway radio, forwards the mesh into TAK (the gateway it subclasses
does that), and adds what the product needs: a local socket API, an event stream, a log ring,
the heartbeat to a configurable state directory, and liveness measured at the serial read
loop for systemd's watchdog (Spec 002)."""
import argparse
import calendar
import collections
import re
import secrets
import datetime
import json
import logging
import os
import queue
import socket
import socketserver
import subprocess
import sys
import threading
import time

from tak_meshtastic_gateway.tak_meshtastic_gateway import TAKMeshtasticGateway

try:
    from pubsub import pub
except ImportError:  # the gateway depends on it; a bench without it still imports
    pub = None

from . import __version__
from . import catalogue as C
from .common import DEFAULT_CONFIG, DEFAULT_SOCKET, DEFAULT_STATE, NODE_ICONS, read_config, utc
from .history import History
from . import peers as P

SILENCE_LIMIT = 600                              # the deployed watchdog's figure


def firmware_behind(have, pinned):
    """Spec 043: is the firmware a device reports behind the shelf's image? Dotted numbers compared in
    order, a trailing build hash ignored; None when the device's version is unknown or unreadable."""
    def nums(v):
        m = re.match(r"^\s*(\d+(?:\.\d+)*)", str(v or ""))
        return [int(x) for x in m.group(1).split(".")] if m else None
    a, b = nums(have), nums(pinned)
    if a is None or b is None:
        return None
    n = max(len(a), len(b))
    a, b = a + [0] * (n - len(a)), b + [0] * (n - len(b))
    return a < b


def point_in_polygon(lat, lon, points):
    """Spec 045: ray casting over (lat, lon) pairs; a point on the boundary counts as inside."""
    pts = [(float(p[0]), float(p[1])) for p in (points or []) if p is not None and len(p) >= 2]
    if len(pts) < 3:
        return False
    inside = False
    j = len(pts) - 1
    for i in range(len(pts)):
        yi, xi = pts[i]; yj, xj = pts[j]
        if (xi > lon) != (xj > lon):
            cross = (yj - yi) * (lon - xi) / ((xj - xi) or 1e-12) + yi
            if lat < cross:
                inside = not inside
        j = i
    return inside


def in_circle(lat, lon, clat, clon, radius_m):
    return _haversine(float(lat), float(lon), float(clat), float(clon)) <= float(radius_m)


def fence_inside(fence, lat, lon):
    if fence.get("kind") == "circle":
        c = fence.get("centre") or [None, None]
        return in_circle(lat, lon, c[0], c[1], fence.get("radius_m") or 0)
    return point_in_polygon(lat, lon, fence.get("points") or [])


def fence_transitions(fences, state, nodes):
    """Spec 045: the crossings since the last look. state holds the last known side of every
    (fence, node) pair; the first sight of a pair records the side and raises nothing. Returns
    (events, new_state), an event being {fence, fence_name, node, name, kind: enter|leave}."""
    state = dict(state or {})
    events = []
    for f in fences or []:
        if not f.get("enabled", True):
            continue
        fid = str(f.get("id") or "")
        for n in nodes or []:
            nid = n.get("id")
            if not nid or n.get("lat") is None or n.get("lon") is None:
                continue
            if f.get("group") and str(n.get("group") or "") != str(f["group"]):
                continue
            try:
                inside = fence_inside(f, float(n["lat"]), float(n["lon"]))
            except (TypeError, ValueError):
                continue
            key = f"{fid}:{nid}"
            prev = state.get(key)
            state[key] = inside
            if prev is None or prev == inside:
                continue
            kind = "enter" if inside else "leave"
            if f.get("rule", "both") in (kind, "both"):
                events.append({"fence": fid, "fence_name": f.get("name") or fid, "node": nid, "name": n.get("name") or nid, "kind": kind})
    return events, state


def key_fingerprint(key_b64):
    """Twelve hex of the sha256 of the key bytes: short enough to read out, long enough to compare."""
    import base64, hashlib
    try:
        raw = base64.b64decode(str(key_b64 or "") + "==")
    except (ValueError, TypeError):
        return None
    return hashlib.sha256(raw).hexdigest()[:12] if raw else None
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")       # the radio's debug log arrives coloured
WATCHDOG_TICK = 30
LINK_HISTORY = 200   # SNR readings kept per node (Spec 008)
SERIAL_DIR = "/dev/serial/by-id"
RECOVERY_UF2 = "double-press reset, copy the pinned firmware UF2 onto the volume that appears, wait for it to come back"
LOG_RING = 500

# Enum names from the library where it is present; a fixed table where it is not, so a
# fake gateway on a bench still reads "EU_868" and "SHORT_FAST".
try:
    from meshtastic.protobuf import channel_pb2, config_pb2

    def region_name(v): return config_pb2.Config.LoRaConfig.RegionCode.Name(int(v))
    def preset_name(v): return config_pb2.Config.LoRaConfig.ModemPreset.Name(int(v))
    def role_name(v): return config_pb2.Config.DeviceConfig.Role.Name(int(v))
    def ch_role_name(v): return channel_pb2.Channel.Role.Name(int(v))
except Exception:  # noqa: BLE001
    _REG = {0: "UNSET", 1: "US", 2: "EU_433", 3: "EU_868", 4: "CN", 5: "JP", 6: "ANZ", 7: "KR", 8: "TW", 9: "RU",
            10: "IN", 11: "NZ_865", 12: "TH", 13: "LORA_24", 14: "UA_433", 15: "UA_868"}
    _PRE = {0: "LONG_FAST", 1: "LONG_SLOW", 2: "VERY_LONG_SLOW", 3: "MEDIUM_SLOW", 4: "MEDIUM_FAST",
            5: "SHORT_SLOW", 6: "SHORT_FAST", 7: "LONG_MODERATE", 8: "SHORT_TURBO"}
    _ROLE = {0: "CLIENT", 1: "CLIENT_MUTE", 2: "ROUTER", 3: "ROUTER_CLIENT", 4: "REPEATER", 5: "TRACKER",
             6: "SENSOR", 7: "TAK", 8: "CLIENT_HIDDEN", 9: "LOST_AND_FOUND", 10: "TAK_TRACKER", 11: "ROUTER_LATE"}
    _CHR = {0: "DISABLED", 1: "PRIMARY", 2: "SECONDARY"}

    def region_name(v): return _REG.get(int(v), str(v))
    def preset_name(v): return _PRE.get(int(v), str(v))
    def role_name(v): return _ROLE.get(int(v), str(v))
    def ch_role_name(v): return _CHR.get(int(v), str(v))


def watchdog_decision(now, last_activity, radio_present, bootloader, limit=SILENCE_LIMIT):
    """Whether to tell systemd we are alive, and why. Radio absent or in bootloader mode is an
    operator action, not a hang: keep pinging and say so, because a restart would thrash. With
    the radio present and the read loop silent past the limit, stop pinging: systemd restarts
    the bridge, which is the recovery a dead serial handle needs (LESSONS 7)."""
    if bootloader:
        return True, "radio is in bootloader mode (-BOOT): waiting, not restarting; re-seat it, or re-flash from the bench"
    if not radio_present:
        return True, "no radio at its by-id path: waiting, not restarting"
    silence = now - last_activity
    if silence < limit:
        return True, f"alive: the read loop produced something {int(silence)} s ago"
    return False, f"silent for {int(silence)} s with the radio present: not pinging, so systemd restarts the bridge"


def sd_notify(msg):
    path = os.environ.get("NOTIFY_SOCKET")
    if not path:
        return False
    if path.startswith("@"):
        path = "\0" + path[1:]
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        s.sendto(msg.encode(), path)
        s.close()
        return True
    except OSError:
        return False


def decode_join_url(url):
    """What a meshtastic.org join URL carries, without the key: names, roles, region, preset.
    (info, None) or (None, error). The key never leaves this function."""
    import base64
    url = str(url or "").strip()
    if "#" not in url or not url.startswith("https://meshtastic.org/e/"):
        return None, "not a meshtastic.org join URL"
    frag = url.split("#", 1)[1].split("?", 1)[0]
    try:
        raw = base64.urlsafe_b64decode(frag + "=" * (-len(frag) % 4))
        from meshtastic.protobuf import apponly_pb2
        cs = apponly_pb2.ChannelSet()
        cs.ParseFromString(raw)
    except Exception as e:  # noqa: BLE001
        return None, f"the URL does not decode as a channel set: {type(e).__name__}"
    names = [s.name or "" for s in cs.settings]
    lora = cs.lora_config if cs.HasField("lora_config") else None
    return {"channels": names, "count": len(names),
            "region": region_name(lora.region) if lora and lora.region else None,
            "modem_preset": preset_name(lora.modem_preset) if lora else None,
            "has_keys": [bool(s.psk) for s in cs.settings]}, None


def _haversine(lat1, lon1, lat2, lon2):
    import math as _m
    p1, p2 = _m.radians(lat1), _m.radians(lat2)
    a = _m.sin((p2 - p1) / 2) ** 2 + _m.cos(p1) * _m.cos(p2) * _m.sin(_m.radians(lon2 - lon1) / 2) ** 2
    return 6371000.0 * 2 * _m.atan2(_m.sqrt(a), _m.sqrt(1 - a))


class CountingSocket:
    """Observe mode: what the gateway would have sent to TAK is counted, never sent."""
    def __init__(self):
        self.packets, self.bytes, self.last = 0, 0, None
    def connect(self, addr): pass
    def send(self, b): self.packets += 1; self.bytes += len(b); self.last = b; return len(b)
    def sendto(self, b, addr): return self.send(b)
    def close(self): pass


accept_item = P.accept_item   # Spec 052: the loop guard, pure


class NullSocket:
    """The server shape (Spec 050): no TAK Server on this box; whatever the gateway would send goes nowhere, uncounted."""
    def connect(self, addr): pass
    def send(self, b): return len(b)
    def sendto(self, b, addr): return len(b)
    def close(self): pass


class _OldCountingSocket:
    """Observe mode: what the gateway would have sent to TAK is counted, never sent."""
    def __init__(self):
        self.packets, self.bytes = 0, 0
    def connect(self, addr): pass
    def send(self, b): self.packets += 1; self.bytes += len(b); return len(b)
    def sendto(self, b, addr): return self.send(b)
    def close(self): pass


class RingHandler(logging.Handler):
    def __init__(self, bridge):
        super().__init__()
        self.bridge = bridge
        self.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s %(message)s", datefmt="%H:%M:%S"))
    def emit(self, record):
        try:
            line = self.format(record)
        except Exception:  # noqa: BLE001
            return
        line = ANSI.sub("", line)
        self.bridge.ring.append(line)
        self.bridge._emit("log", line=line)


def bootloader_mode(serial_path):
    """True when the by-id link resolves to a USB device whose product string ends -BOOT (a
    T1000-E or RAK sitting in its UF2 bootloader: it presents a serial port and answers nothing,
    LESSONS 8). Linux sysfs only; elsewhere unknown, so False."""
    try:
        real = os.path.realpath(serial_path)
        tty = os.path.basename(real)
        for cand in (f"/sys/class/tty/{tty}/device/../product", f"/sys/class/tty/{tty}/device/product"):
            if os.path.exists(cand):
                return open(cand).read().strip().upper().endswith("-BOOT")
    except OSError:
        pass
    return False


class Bridge(TAKMeshtasticGateway):
    box_mode = "tak-server"   # Spec 050: the class default, so a bridge built without __init__ (the suites do) still has a shape
    peering = None            # Spec 052: the site's identity, pins and links; None on a bridge built without __init__
    remote_nodes = {}         # Spec 052: origin site id -> {"name", "ts", "nodes": [...]}
    remote_waypoints = {}     # Spec 053: origin site id -> {wid: record}
    remote_alerts = {}        # Spec 053: origin site id -> {"node:kind": record}
    def __init__(self, conf, socket_path=DEFAULT_SOCKET, state_dir=DEFAULT_STATE, observe=False,
                 silence_limit=SILENCE_LIMIT, gps_reader=True):
        self.conf = conf
        self.socket_path = socket_path
        self.state_dir = state_dir
        self.observe = observe
        _m = str(conf.get("MODE") or "").strip().lower()
        self.box_mode = _m if _m in ("server", "hub") else "tak-server"  # Spec 050 and 052
        self.remote_nodes = {}; self.remote_waypoints = {}; self.remote_alerts = {}
        self._peers_lock = threading.RLock()
        self.silence_limit = silence_limit
        self.started = time.time()
        self.last_activity = 0.0
        self.last_forwarded = None
        self.watchdog_state = "starting"
        self.ring = collections.deque(maxlen=LOG_RING)
        self.links = {}        # node id -> deque of [ts, snr, hops] for every packet heard (Spec 008)
        self.direct = {}       # node id -> SNR of the last packet that arrived with no hops
        self.routes = {}       # node id -> the last traceroute answer
        self.history = History(self.state_dir, days=self.conf.get("HISTORY_DAYS") or 30, logger=getattr(self, "logger", None))  # Spec 020
        self._last_pos = {}    # node id -> (lat, lon) last written to the history
        self._last_rec = {}    # node id -> (lat, lon) the gateway's record held when we last looked, so the TAK path writes only when it moves
        self.batteries = {}    # node id -> {"level", "voltage", "ts"} from the telemetry heard, newest wins (Spec 018)
        try:  # the store survives a restart (Spec 019)
            with open(os.path.join(self.state_dir, "batteries.json")) as f:
                self.batteries = {k: v for k, v in json.load(f).items() if isinstance(v, dict)}
        except Exception:  # noqa: BLE001
            pass
        self.serial_dir = SERIAL_DIR          # where bench devices are listed (Spec 009)
        self.serial_factory = None            # a second serial interface for a bench device; the library's by default
        self._bench_lock = threading.Lock()   # one bench device at a time
        self.node_factory = None              # a remote Node for a radio id (Spec 011); the library's by default
        self.pins = None                      # firmware pins (Spec 010); the release's PINS.json by default
        self.gps_fix = None                   # the box's own receiver's last fix (Spec 014)
        self.gps_state = None                 # what the last read of the receiver established: reachable, fix, satellites
        self.outbox = {}                      # Spec 034: sent messages by packet id, and what the radio said became of them
        self.waypoints = {}                   # Spec 041: waypoints heard on the mesh, by id, the live ones
        self.neighbor_edges = {}              # Spec 042: (reporter, neighbour) -> {snr, ts}, from NeighborInfo
        self.gps_port_factory = None          # opens the receiver's port; pyserial by default
        self._gps_reader = gps_reader
        self.flash_hooks = {}                 # the block layer and esptool, replaceable by the suite
        self._subs = []
        self._subs_lock = threading.Lock()
        self._stop = threading.Event()
        self._server = None
        # the ticker runs before the gateway's init, because that init blocks until a radio answers
        threading.Thread(target=self._watchdog_loop, name="watchdog", daemon=True).start()
        threading.Thread(target=self._telemetry_loop, name="telemetry", daemon=True).start()
        threading.Thread(target=self._alert_loop, name="alerts", daemon=True).start()
        if self.box_mode == "hub":
            # Spec 052: a site with no radio. No gateway, no serial device, no TAK, no mesh of its own.
            self.logger = logging.getLogger("mesh-manager-hub")
            if not self.logger.handlers:
                h = logging.StreamHandler(); h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s")); self.logger.addHandler(h)
            self.logger.setLevel(logging.INFO)
            self.interface = None; self.meshtastic_devices = {}; self.meshtastic_connected = False; self.socket_client = NullSocket()
        else:
            TAKMeshtasticGateway.__init__(self, ip=conf.get("ip"), serial_device=conf.get("SERIAL") or None,
                                          debug=bool(conf.get("debug")))
        self.logger.addHandler(RingHandler(self))
        try:
            self.peering = P.Peering(self, conf)
        except Exception as ex:  # noqa: BLE001
            self.peering = None
            self.logger.error(f"peers: the site identity or listener failed: {type(ex).__name__}: {ex}")
        threading.Thread(target=self._peer_loop, name="peers", daemon=True).start()
        self._redial_pinned()
        if self.box_mode == "hub":
            self._touch(); sd_notify("READY=1")
            self.logger.info(f"mesh-manager-bridge {__version__} as a hub; site {self.peering.id[:12] if self.peering else '?'}; socket {socket_path}; state {state_dir}")
            return
        if self.box_mode == "server":
            self.socket_client = NullSocket()
        elif observe and not isinstance(self.socket_client, CountingSocket):
            self.socket_client = CountingSocket()
        # Subscribe AFTER the gateway has: pypubsub fixes a topic's argument spec from its first
        # listener, and the gateway's on_connection takes the topic itself (AUTO_TOPIC). Found
        # live on the kit, 3 Sep 2026: subscribing first made the gateway's own subscription
        # fail with ListenerMismatchError. The gateway's device-log listener is replaced by ours
        # so a device line is stamped, kept and emitted once.
        if pub:
            if hasattr(self, "on_log_message"):
                try:
                    pub.unsubscribe(self.on_log_message, "meshtastic.log")
                except Exception:  # noqa: BLE001
                    pass
            pub.subscribe(self._on_receive, "meshtastic.receive")
            pub.subscribe(self._on_log, "meshtastic.log")
            pub.subscribe(self._on_connected, "meshtastic.connection.established")
            pub.subscribe(self._on_lost, "meshtastic.connection.lost")
        self._touch()
        if getattr(self, "meshtastic_connected", False):
            sd_notify("READY=1")      # the connection was established during the gateway's init
        if self.box_mode == "server":
            self.socket_client = NullSocket()
            self.logger.info("server shape (MODE=server): no TAK Server on this box; nothing is forwarded and no TAK socket is bound")
        elif observe:
            self.socket_client = CountingSocket()
            self.logger.info("observe mode: listening only; nothing is forwarded to TAK and no TAK socket is bound")
        else:
            threading.Thread(target=self._run_gateway, name="gateway", daemon=True).start()
        self.logger.info(f"mesh-manager-bridge {__version__} on {conf.get('SERIAL')}; socket {socket_path}; state {state_dir}")
        if self._gps_reader:
            threading.Thread(target=self._gps_loop, name="gps", daemon=True).start()

    @property
    def gateway(self):
        """The bridge IS the gateway (a subclass); callers that think of the gateway inside
        the bridge, the suites among them, reach it here."""
        return self

    # ---- the gateway's own loop and radio -----------------------------------------------------
    def _run_gateway(self):
        try:
            TAKMeshtasticGateway.main(self)
        except Exception as e:  # noqa: BLE001
            self.logger.error(f"the gateway loop stopped: {type(e).__name__}: {e}")

    def connect_to_meshtastic_node(self):
        # the gateway would raise on a missing port and systemd would thrash; wait instead,
        # saying so once a minute, while the watchdog ticker keeps us alive
        path = self.conf.get("SERIAL")
        said = 0
        while path and not os.path.exists(path) and not self._stop.is_set():
            if time.time() - said > 60:
                self.logger.warning(f"no radio at {path}: waiting (plug it in, or check ls -l /dev/serial/by-id/)")
                said = time.time()
            time.sleep(2)
        if self.box_mode == "server":
            self.socket_client = NullSocket()
        elif self.observe:
            # before the radio can deliver a single packet: nothing reaches TAK in observe mode
            self.socket_client = CountingSocket()
        TAKMeshtasticGateway.connect_to_meshtastic_node(self)

    # ---- activity and events --------------------------------------------------------------------
    def _touch(self):
        self.last_activity = time.time()

    def _on_receive(self, packet, interface):
        self._touch()
        d = packet.get("decoded", {}) if isinstance(packet, dict) else {}
        if isinstance(packet, dict) and d.get("portnum") == "TEXT_MESSAGE_APP":
            fr = packet.get("fromId")
            dev = getattr(self, "meshtastic_devices", {}).get(fr, {}) if fr else {}
            _msg = {"from": fr, "name": dev.get("long_name") or fr, "to": packet.get("toId"), "channel": packet.get("channel", 0), "text": d.get("text") or ""}
            self._emit("text", **_msg)
            self._share_text(_msg)   # Spec 053
        fr = packet.get("fromId") if isinstance(packet, dict) else None
        if fr and d.get("portnum") == "TELEMETRY_APP":
            dm = (d.get("telemetry") or {}).get("deviceMetrics") or {}
            if dm.get("batteryLevel") is not None or dm.get("voltage") is not None:
                self._battery_note(fr, dm.get("batteryLevel"), dm.get("voltage"))
            em = (d.get("telemetry") or {}).get("environmentMetrics") or {}
            if em:
                self._env_note(fr, em)
        if fr and d.get("portnum") == "WAYPOINT_APP":
            self._waypoint_note(fr, d)
        if fr and d.get("portnum") == "NEIGHBORINFO_APP":
            self._neighbors_note(fr, d)
        snr = packet.get("rxSnr") if isinstance(packet, dict) else None
        hops = ((packet.get("hopStart", 0) - packet.get("hopLimit", 0))
                if isinstance(packet, dict) and "hopStart" in packet and "hopLimit" in packet else None)
        if fr and snr is not None:
            # the link store: rxSnr describes the LAST hop into this radio, so only a packet
            # that came with no hops says anything about the link between that node and us
            try:
                snr = float(snr)
                self.links.setdefault(fr, collections.deque(maxlen=LINK_HISTORY)).append([utc(time.time()), snr, hops])
                if hops == 0:
                    self.direct[fr] = snr
            except (TypeError, ValueError):
                pass
        self._emit("packet", **{"from": fr, "to": packet.get("toId") if isinstance(packet, dict) else None,
                                "port": d.get("portnum"), "snr": snr, "hops": hops})
        self._history_note(packet, d, fr, snr, hops)

    def _history_note(self, packet, d, fr, snr, hops):
        """Spec 020: what this packet adds to the history. Never raises into the receive path."""
        try:
            h = getattr(self, "history", None)
            if not h or not h.ok or not isinstance(packet, dict):
                return
            try:
                snr_f = float(snr) if snr is not None else None
            except (TypeError, ValueError):
                snr_f = None
            size = None
            try:
                size = len(d.get("payload") or b"")
            except Exception:  # noqa: BLE001
                size = None
            h.packet(fr, port=d.get("portnum"), snr=snr_f, hops=hops, size=size)
            port = d.get("portnum")
            if fr and port == "TEXT_MESSAGE_APP":
                dev = getattr(self, "meshtastic_devices", {}).get(fr, {})
                h.message(fr, d.get("text") or "", name=dev.get("long_name") or fr, dest=packet.get("toId"), channel=packet.get("channel", 0), snr=snr_f)
            if fr and port == "TELEMETRY_APP":
                dm = (d.get("telemetry") or {}).get("deviceMetrics") or {}
                if dm:
                    h.telemetry(fr, level=dm.get("batteryLevel"), voltage=dm.get("voltage"), chutil=dm.get("channelUtilization"), airutil=dm.get("airUtilTx"), uptime=dm.get("uptimeSeconds"))
            if fr:
                lat = lon = None
                pos = d.get("position") or {}
                dev = getattr(self, "meshtastic_devices", {}).get(fr) or {}
                try:
                    rec = (float(dev.get("last_lat") or 0), float(dev.get("last_lon") or 0))
                except (TypeError, ValueError):
                    rec = (0.0, 0.0)
                first = fr not in self._last_rec
                moved = rec != self._last_rec.get(fr)
                self._last_rec[fr] = rec
                if pos.get("latitude") is not None and pos.get("longitude") is not None:
                    lat, lon = float(pos["latitude"]), float(pos["longitude"])
                elif rec != (0.0, 0.0) and (first or moved) and self._last_pos.get(fr) != rec:
                    lat, lon = rec        # the TAK path: the gateway's record moved since we last looked
                if lat is not None and lon is not None and (lat or lon):
                    self._last_pos[fr] = (lat, lon)
                    h.position(fr, lat, lon, snr=snr_f, hops=hops)
        except Exception as e:  # noqa: BLE001
            if hasattr(self, "logger"):
                self.logger.debug(f"history note skipped: {type(e).__name__}: {e}")

    def op_availability(self, hours=24, **_):
        """Spec 036: how much of the window each node was actually heard for. Hourly buckets up
        to two days, daily beyond; a bucket counts if any packet from the node landed in it. A
        node with nothing in the window is 0%, listed, never dropped."""
        try:
            hours = max(1, min(int(hours or 24), 24 * 30))
        except (TypeError, ValueError):
            hours = 24
        bucket = 3600 if hours <= 48 else 86400
        nb = max(1, int(round(hours * 3600 / bucket)))
        now = time.time(); start = now - nb * bucket
        h = getattr(self, "history", None)
        rows = h.query("packets", since=utc(start), limit=5000) if h and h.ok else []
        seen = {}
        for r in rows:
            try:
                t = calendar.timegm(time.strptime(str(r.get("ts"))[:19], "%Y-%m-%dT%H:%M:%S"))
            except (TypeError, ValueError):
                continue
            i = int((t - start) // bucket)
            if 0 <= i < nb and r.get("node"):
                seen.setdefault(r["node"], set()).add(i)
        names = {}
        try:
            for n in self.mesh_nodes():
                names[n.get("id")] = n.get("name") or n.get("id")
        except Exception:  # noqa: BLE001
            pass
        for nid, rec in (getattr(self.interface, "nodes", {}) or {}).items():
            if isinstance(rec, dict):
                ln = (rec.get("user") or {}).get("longName")
                if ln or nid not in names:
                    names[nid] = ln or names.get(nid) or nid
        labels = {k: str(v.get("label") or "") for k, v in self._register_load().items()} if hasattr(self, "_register_load") else {}
        own = (self._own() or {}).get("id")
        out = []
        for nid in sorted(set(names) | set(seen)):
            if not nid or nid == own:
                continue
            got = seen.get(nid, set())
            out.append({"id": nid, "name": labels.get(nid) or names.get(nid) or nid, "buckets": nb, "heard": len(got),
                        "pct": int(round(100.0 * len(got) / nb)), "bucket_secs": bucket,
                        "series": [1 if i in got else 0 for i in range(nb)]})
        out.sort(key=lambda r: (-r["pct"], r["name"]))
        return {"hours": hours, "bucket_secs": bucket, "buckets": nb, "nodes": out}

    def op_history(self, kind="positions", node=None, since=None, limit=500, **_):
        h = getattr(self, "history", None)
        if not h or not h.ok:
            return {"error": "the history store is not available on this box", "rows": []}
        if kind not in ("positions", "telemetry", "messages", "packets", "environment", "waypoints", "neighbors"):
            return {"error": "kind must be positions, telemetry, messages, packets, environment, waypoints or neighbors"}
        rows = h.query(kind, node=node or None, since=since or None, limit=limit or 500)
        return {"kind": kind, "node": node, "since": since, "rows": rows, "count": len(rows)}

    def op_history_summary(self, **_):
        h = getattr(self, "history", None)
        return h.summary() if h else {"ok": False}

    def _on_log(self, line):
        # a device log line: proof the serial loop is alive, kept in the ring, one event
        self._touch()
        line = ANSI.sub("", str(line)).strip()
        if hasattr(self, "logger"):
            self.logger.info(line)             # the ring handler emits it as a log event
        else:
            self._emit("log", line=str(line))

    def _on_connected(self, interface, topic=pub.AUTO_TOPIC if pub else None):
        self._touch()
        sd_notify("READY=1")
        self._emit("connection", state="established")

    def _on_lost(self, interface):
        self._emit("connection", state="lost")

    def _emit(self, kind, **fields):
        ev = {"kind": kind, "ts": utc(time.time()), **fields}
        line = json.dumps(ev) + "\n"
        with self._subs_lock:
            for q in list(self._subs):
                try:
                    q.put_nowait(line)
                except queue.Full:
                    pass

    # ---- the watchdog ---------------------------------------------------------------------------
    def _watchdog_loop(self):
        last_reason = None
        while not self._stop.is_set():
            path = self.conf.get("SERIAL") or ""
            present = bool(path) and os.path.exists(path)
            if self.box_mode == "hub":
                ping, reason = True, "hub: no radio by design; alive"
            else:
                ping, reason = watchdog_decision(time.time(), self.last_activity or self.started, present,
                                                 present and bootloader_mode(path), self.silence_limit)
            self.watchdog_state = "pinging" if ping else "not pinging"
            if ping:
                sd_notify("WATCHDOG=1")
            head = reason.split(":")[0]
            if head != last_reason and hasattr(self, "logger"):
                (self.logger.warning if not ping or "waiting" in reason else self.logger.info)(f"watchdog: {reason}")
                last_reason = head
            self._stop.wait(WATCHDOG_TICK)

    # ---- the heartbeat, to wherever the state directory is ------------------------------------
    def heartbeat(self):
        now = time.time()
        if self.box_mode == "tak-server":  # Spec 050: a box without TAK forwards nothing
            self.last_forwarded = now
            self._emit("forwarded")
        if now - getattr(self, "_hb_last", 0.0) < 10:
            return
        self._hb_last = now
        try:
            if not os.path.isdir(self.state_dir):
                return
            tmp = os.path.join(self.state_dir, ".heartbeat.tmp")
            with open(tmp, "w") as fh:
                json.dump({"ts": utc(now), "nodes_seen": len(self.meshtastic_devices), "nodes": self.mesh_nodes()}, fh)
            os.replace(tmp, os.path.join(self.state_dir, "heartbeat.json"))
        except OSError:
            pass

    # ---- what the API answers ------------------------------------------------------------------
    def _own(self):
        seen = getattr(self, "owner_seen", None) or {}
        try:
            info = self.interface.getMyNodeInfo() or {}
            u = info.get("user", {})
            return {"id": u.get("id"), "name": seen.get("long_name") or u.get("longName"), "short": seen.get("short_name") or u.get("shortName")}
        except Exception:  # noqa: BLE001
            return {"id": None, "name": seen.get("long_name"), "short": seen.get("short_name")}

    def _lora(self):
        try:
            return self.interface.localNode.localConfig.lora
        except Exception:  # noqa: BLE001
            return None


    # ---- Spec 052: sites, pairing, the link, the picture -------------------------------------------
    # ADR 003's sharing table, per peer and per class (Spec 053). `air` is held off until slice 4. Direct messages,
    # keys, admin traffic and firmware are not classes: they never cross.
    SHARING_DEFAULT = {"nodes": {"out": True, "in": True}, "messages": {"out": False, "in": True, "channels": [], "air": False, "air_channel": None},
                       "waypoints": {"out": True, "in": True, "air": False}, "alerts": {"out": True, "in": True}}
    SHARING_CLASSES = ("nodes", "messages", "waypoints", "alerts")

    @classmethod
    def _sharing_defaults(cls):
        return {k: dict(v, channels=list(v.get("channels", []))) if "channels" in v else dict(v) for k, v in cls.SHARING_DEFAULT.items()}

    def _peers_path(self):
        return os.path.join(self.state_dir, "peers.json")

    def _peers_load(self):
        try:
            with open(self._peers_path()) as fh:
                d = json.load(fh)
        except (OSError, ValueError):
            d = {}
        d.setdefault("peers", {}); d.setdefault("invites", {})
        return d

    def _peers_save(self, d):
        os.makedirs(self.state_dir, exist_ok=True)
        tmp = self._peers_path() + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(d, fh, indent=1, sort_keys=True)
        os.replace(tmp, self._peers_path())

    def peer_pinned(self, peer_id):
        with self._peers_lock:
            return self._peers_load()["peers"].get(str(peer_id))

    def peer_check_code(self, code):
        """None when the code is good (and it is spent here); otherwise why not, in words."""
        code = str(code or "").strip().upper()
        with self._peers_lock:
            d = self._peers_load(); inv = d["invites"]
            for k in list(inv):
                if float(inv[k].get("expires") or 0) < time.time():
                    del inv[k]
            if not code:
                self._peers_save(d)
                return "no such site here: pair with an invite code"
            rec = inv.get(code)
            if not rec or rec.get("used"):
                self._peers_save(d)
                return "the code is wrong, expired or already used"
            rec["used"] = time.time()
            self._peers_save(d)
        return None

    def peer_pin(self, peer_id, name, cert_pem, direction):
        with self._peers_lock:
            d = self._peers_load(); rec = d["peers"].setdefault(str(peer_id), {"added": utc(time.time()), "sharing": self._sharing_defaults()})
            if name: rec["name"] = name
            if cert_pem: rec["cert"] = cert_pem
            rec["direction"] = direction; rec["last_seen"] = utc(time.time())
            self._peers_save(d)
        self._emit("peers", state="pinned", site=peer_id, name=name)

    def peer_touch(self, peer_id, name):
        with self._peers_lock:
            d = self._peers_load(); rec = d["peers"].get(str(peer_id))
            if rec:
                if name: rec["name"] = name
                rec["last_seen"] = utc(time.time()); self._peers_save(d)

    CATCHUP_WINDOW = 24 * 3600   # Spec 055: how far back a reconnection reaches
    CATCHUP_LIMIT = 200

    def peer_connected(self, link):
        self._peer_send_snapshot(link)
        self._peer_send_state(link)
        self._peer_want(link)
        self._emit("peers", state="connected", site=link.peer_id, name=link.peer_name)

    def _peer_want(self, link):
        """Spec 055: ask the peer for the messages missed since the newest remote one held here, within the window."""
        h = getattr(self, "history", None)
        floor = utc(time.time() - self.CATCHUP_WINDOW)
        newest = h.newest_message() if h and h.ok else None
        link.send({"want": {"messages": max(str(newest or floor), floor)}})

    def peer_want(self, link, want):
        """Spec 055: answer a peer's ask from the history, oldest first: this box's own broadcasts its table lets out to that
        peer, and what it holds from other sites relayed with the path. A caught-up message is history: never aired, no receipt."""
        h = getattr(self, "history", None)
        since = want.get("messages") if isinstance(want, dict) else None
        if not since or not h or not h.ok or not self.peering:
            return
        since = max(str(since), utc(time.time() - self.CATCHUP_WINDOW))
        row = self._sharing(link.peer_id).get("messages") or {}
        if not row.get("out"):
            return
        sent = 0
        for r in h.catchup(since, link.peer_id, limit=self.CATCHUP_LIMIT):
            ch = int(r.get("channel") or 0)
            if row.get("channels") and ch not in row["channels"]:
                continue
            data = {"from": r.get("node"), "name": r.get("name"), "to": r.get("dest") or "^all", "channel": ch, "text": r.get("text"), "ts": r.get("ts"),
                    "channel_name": r.get("channel_name") or (self._channel_name(ch) if not r.get("origin") else None)}
            if r.get("origin"):
                item = {"class": "messages", "origin": r["origin"], "origin_name": r.get("origin_name") or r["origin"][:12], "path": [r["origin"], self.peering.id], "ts": r.get("ts"), "data": data, "catchup": True}
            else:
                item = {"class": "messages", "origin": self.peering.id, "origin_name": self.peering.name, "path": [self.peering.id], "ts": r.get("ts"), "data": data, "catchup": True}
            link.send({"item": item}); sent += 1
        if sent:
            self.logger.info(f"peers: {link.peer_name or link.peer_id[:12]} caught up on {sent} message(s) since {since}")

    def _peer_send_state(self, link):
        """Spec 055: live waypoints and open alerts go with the picture when a link comes up, this box's own and the ones it
        holds from other sites, so a peer that restarted has them within seconds."""
        if not self.peering:
            return
        now = int(time.time()); me = self.peering.id
        def send(cls, origin, oname, data, path):
            if link.peer_id in path or origin == link.peer_id or not (self._sharing(link.peer_id).get(cls) or {}).get("out"):
                return
            link.send({"item": {"class": cls, "origin": origin, "origin_name": oname, "path": path, "ts": utc(time.time()), "data": data}})
        try:
            for w in list(getattr(self, "waypoints", {}).values()):
                if not (w.get("expire") and w["expire"] < now):
                    send("waypoints", me, self.peering.name, dict(w, gone=False), [me])
            for o in list(self._alerts_load().get("open", {}).values()):
                send("alerts", me, self.peering.name, {"state": "open", "node": o.get("node"), "kind": o.get("kind"), "text": o.get("text"), "since": o.get("since")}, [me])
        except Exception as ex:  # noqa: BLE001
            self.logger.debug(f"peers: own state not sent: {type(ex).__name__}: {ex}")
        with self._peers_lock:
            wps = [(o, dict(w)) for o, bag in self.remote_waypoints.items() for w in bag.values()]
            als = [(o, dict(x)) for o, bag in self.remote_alerts.items() for x in bag.values()]
        for o, w in wps:
            if not (w.get("expire") and w["expire"] < now):
                oname = w.pop("origin_name", None) or o[:12]; w.pop("origin", None)
                send("waypoints", o, oname, dict(w, gone=False), [o, me])
        for o, x in als:
            send("alerts", o, x.get("origin_name") or o[:12], {"state": "open", "node": x.get("node"), "kind": x.get("kind"), "text": x.get("text"), "since": x.get("since")}, [o, me])

    def _snapshot_item(self):
        own = [n for n in self.op_nodes().get("nodes", []) if not n.get("remote")] if self.interface is not None else []
        keep = ("id", "name", "short", "label", "hw", "battery", "voltage", "charging", "lat", "lon", "heard", "snr", "hops", "heard_here", "icon", "group", "role")
        rows = [{k: n.get(k) for k in keep if k in n} for n in own]
        return {"class": "nodes", "origin": self.peering.id, "origin_name": self.peering.name, "path": [self.peering.id], "ts": utc(time.time()), "data": rows}

    def _peer_send_snapshot(self, link=None):
        if not self.peering:
            return
        if self.interface is not None and self._sharing(link.peer_id if link else None).get("nodes", {}).get("out", True):
            item = self._snapshot_item()
            if link: link.send({"item": item})
            else: self.peering.broadcast({"item": item})
        if link:  # a hub (any site) passes the pictures it holds on to a newly connected peer
            with self._peers_lock:
                held = [dict(v["item"]) for v in self.remote_nodes.values() if v.get("item")]
            for it in held:
                if link.peer_id not in it.get("path", []) and it.get("origin") != link.peer_id:
                    it = dict(it); it["path"] = list(it.get("path", [])) + [self.peering.id]; link.send({"item": it})

    def _sharing(self, peer_id):
        """The peer's table with the defaults under it, class by class."""
        rec = self.peer_pinned(peer_id) if peer_id else None
        sh = self._sharing_defaults()
        stored = rec.get("sharing") if rec and isinstance(rec.get("sharing"), dict) else {}
        for k, v in stored.items():
            if k in sh and isinstance(v, dict):
                sh[k].update(v)
        return sh

    def op_peer_sharing_set(self, site="", cls="", out=None, channels=None, air=None, air_channel=None, **kw):
        """One class of one peer's table; `in` arrives in kw because it is a Python keyword. Spec 054: `air` and
        `air_channel` for messages, `air` for waypoints, at a radio site only."""
        site = str(site or "").strip().lower(); cls = str(cls or "").strip().lower(); inn = kw.get("in")
        if air not in (None, "") and self.interface is None:
            return {"error": "this site has no radio: nothing can go on the air from a hub"}
        if air not in (None, "") and cls not in ("messages", "waypoints"):
            return {"error": f"the air is for messages and waypoints, not {cls}"}
        if cls not in self.SHARING_CLASSES:
            return {"error": f"no such class: {cls or '(none)'}; the table has {', '.join(self.SHARING_CLASSES)}"}
        def onoff(v):
            if v is None or v == "": return None
            t = str(v).strip().lower()
            if t in ("1", "true", "on", "yes"): return True
            if t in ("0", "false", "off", "no"): return False
            raise ValueError(t)
        try:
            o, i, a = onoff(out), onoff(inn), onoff(air)
        except ValueError as ex:
            return {"error": f"out, in and air are on or off, not {ex}"}
        ach = None
        if air_channel not in (None, ""):
            try:
                ach = int(str(air_channel).strip())
            except ValueError:
                return {"error": "air_channel is a channel index, 0 to 7"}
            if not 0 <= ach <= 7:
                return {"error": "air_channel is a channel index, 0 to 7"}
        with self._peers_lock:
            d = self._peers_load(); rec = d["peers"].get(site)
            if not rec:
                return {"error": "no such peer here: pair it first"}
            sh = rec.setdefault("sharing", self._sharing_defaults()); row = sh.setdefault(cls, dict(self._sharing_defaults()[cls]))
            if o is not None: row["out"] = o
            if i is not None: row["in"] = i
            if a is not None: row["air"] = a
            if ach is not None and cls == "messages": row["air_channel"] = ach
            if cls == "messages" and channels is not None:
                try:
                    row["channels"] = sorted({int(x) for x in re.split(r"[,\s]+", str(channels)) if x.strip() != ""})
                except ValueError:
                    return {"error": "channels are channel indexes, 0 to 7, separated by commas"}
                if any(not 0 <= c <= 7 for c in row["channels"]):
                    return {"error": "channels are channel indexes, 0 to 7"}
            self._peers_save(d)
        self._emit("peers", state="sharing", site=site, cls=cls)
        return {"written": dict(row), "site": site, "cls": cls, "confirmed": True}

    def _channel_name(self, index):
        try:
            for c in self.op_channels().get("channels", []):
                if int(c.get("index", -1)) == int(index):
                    return c.get("name") or f"slot {index}"
        except Exception:  # noqa: BLE001
            pass
        return f"channel {index}"

    def _peer_share(self, cls, data):
        """Send one item of a class to every connected peer whose table lets it out."""
        if not self.peering:
            return
        item = {"class": cls, "origin": self.peering.id, "origin_name": self.peering.name, "path": [self.peering.id], "ts": utc(time.time()), "data": data}
        for pid, link in self.peering.connected().items():
            row = self._sharing(pid).get(cls) or {}
            if not row.get("out"):
                continue
            if cls == "messages" and row.get("channels") and int(data.get("channel") or 0) not in row["channels"]:
                continue
            link.send({"item": item})

    def _share_text(self, msg):
        """A broadcast heard or sent here goes to the peers; a direct message never does (the never-list)."""
        to = str(msg.get("to") or "^all")
        if to not in ("^all", "!ffffffff", "") or msg.get("aired_from"):
            return   # a direct message never crosses; a message that came off a link and went on this air is not sent back (Spec 054)
        d = {k: msg.get(k) for k in ("from", "name", "to", "channel", "text", "mid", "sent") if k in msg}
        d["channel"] = int(msg.get("channel") or 0); d["channel_name"] = self._channel_name(d["channel"]); d["ts"] = utc(time.time())
        self._peer_share("messages", d)

    def peer_item(self, link, item):
        if not self.peering or not P.accept_item(item, self.peering.id):
            return
        cls = str(item.get("class") or ""); origin = str(item.get("origin") or ""); oname = str(item.get("origin_name") or origin[:12])
        if cls == "receipts" and isinstance(item.get("data"), dict):   # Spec 054: a peer says whether our message went on its air
            d = item["data"]
            self._emit("ack", request_id=d.get("mid"), ok=bool(d.get("aired")), aired_at=(oname if d.get("aired") else None), reason=(None if d.get("aired") else (d.get("why") or "not aired")))
            return
        if cls not in self.SHARING_CLASSES or not origin:
            return
        if not self._sharing(link.peer_id).get(cls, {}).get("in", True):
            return
        data = item.get("data")
        if cls == "nodes":
            with self._peers_lock:
                self.remote_nodes[origin] = {"name": oname, "ts": time.time(), "via": link.peer_id, "item": item,
                                             "nodes": [dict(n) for n in (data or []) if isinstance(n, dict) and n.get("id")]}
            self._emit("peers", state="picture", site=origin, count=len(data or []))
        elif cls == "messages" and isinstance(data, dict):
            to = str(data.get("to") or "^all")
            if to not in ("^all", "!ffffffff", ""):
                return  # a direct message is never accepted either
            ts = str(data.get("ts") or utc(time.time()))
            h = getattr(self, "history", None)
            if h and h.ok:
                if h.has_message(origin, ts, data.get("from"), data.get("text")):
                    return   # Spec 055: held already; a catch-up may offer what the live link delivered
                h.message(data.get("from"), data.get("text"), name=data.get("name"), dest=to, channel=int(data.get("channel") or 0), ts=ts,
                          origin=origin, origin_name=oname, channel_name=data.get("channel_name"))
            ev = {k: v for k, v in data.items() if k not in ("kind", "origin", "origin_name")}
            ev["ts"] = ts
            self._emit("text", origin=origin, origin_name=oname, **ev)
            row = self._sharing(link.peer_id).get("messages", {})
            if self.interface is not None and not data.get("aired_from") and not item.get("catchup"):   # Spec 055: history is never aired
                if row.get("air"):
                    self._air_text(link, oname, data, row)
                else:
                    self._peer_receipt(link, data.get("mid"), False, why="that site keeps its air closed")
            if item.get("for"):
                return   # addressed to this site: not relayed on
        elif cls == "waypoints" and isinstance(data, dict) and data.get("wid") is not None:
            with self._peers_lock:
                bag = self.remote_waypoints.setdefault(origin, {})
                if data.get("gone"):
                    bag.pop(int(data["wid"]), None)
                else:
                    rec = {k: v for k, v in data.items() if k != "gone"}; rec["origin"] = origin; rec["origin_name"] = oname; bag[int(data["wid"])] = rec
            self._emit("waypoint", gone=bool(data.get("gone")), origin=origin, origin_name=oname, **{k: v for k, v in data.items() if k != "gone"})
            if self.interface is not None and not data.get("gone") and not data.get("aired_from") and self._sharing(link.peer_id).get("waypoints", {}).get("air"):
                self._air_waypoint(link, oname, data)
        elif cls == "alerts" and isinstance(data, dict):
            key = f"{data.get('node')}:{data.get('kind')}"
            with self._peers_lock:
                bag = self.remote_alerts.setdefault(origin, {})
                if data.get("state") == "cleared":
                    bag.pop(key, None)
                else:
                    bag[key] = {"node": data.get("node"), "kind": data.get("kind"), "text": data.get("text"), "since": data.get("since") or utc(time.time()), "origin": origin, "origin_name": oname}
            self._emit("alert", state=data.get("state") or "open", node=data.get("node"), what=data.get("kind"), text=data.get("text"), origin=origin, origin_name=oname, tak=False)
        else:
            return
        fwd = dict(item); fwd["path"] = list(item.get("path") or []) + [self.peering.id]
        for pid, l in self.peering.connected().items():
            if pid == link.peer_id or pid in fwd["path"] or pid == origin:
                continue
            row = self._sharing(pid).get(cls, {})
            if not row.get("out", True):
                continue
            if cls == "messages" and row.get("channels") and int((data or {}).get("channel") or 0) not in row["channels"]:
                continue
            l.send({"item": fwd})

    def _remote_rows(self, own_ids):
        rows = []
        with self._peers_lock:
            snaps = list(self.remote_nodes.items())
        for origin, v in snaps:
            for n in v.get("nodes", []):
                if str(n.get("id")) in own_ids:
                    continue
                r = dict(n); r["remote"] = True; r["origin"] = origin; r["origin_name"] = v.get("name") or origin[:12]
                r.setdefault("label", ""); r.setdefault("group", ""); r.setdefault("tags", []); r.setdefault("icon", "radio"); r.setdefault("heard_here", True)
                rows.append(r)
        return rows

    def _peer_loop(self):
        while not self._stop.is_set():
            self._stop.wait(30)
            if self._stop.is_set():
                break
            try:
                if self.peering and self.peering.connected():
                    self._peer_send_snapshot()
                with self._peers_lock:  # a picture from a peer that has been away ten minutes is stale: drop it
                    live = self.peering.connected() if self.peering else {}
                    for origin, v in list(self.remote_nodes.items()):
                        if time.time() - v.get("ts", 0) > 600 and v.get("via") not in live:
                            del self.remote_nodes[origin]
            except Exception as ex:  # noqa: BLE001
                self.logger.warning(f"peers: the loop hit {type(ex).__name__}: {ex}")


    # ---- Spec 054: the air --------------------------------------------------------------------------
    def _peer_aired_count(self, peer_id):
        with self._peers_lock:
            d = self._peers_load(); rec = d["peers"].get(str(peer_id))
            if rec is not None:
                a = rec.setdefault("aired", {"count": 0, "last": None}); a["count"] = int(a.get("count") or 0) + 1; a["last"] = utc(time.time()); self._peers_save(d)

    def _peer_receipt(self, link, mid, aired, channel=None, why=None):
        if mid is None or not self.peering:
            return
        data = {"mid": mid, "aired": bool(aired), "channel": channel, "site": self.peering.name}
        if why: data["why"] = why
        link.send({"item": {"class": "receipts", "origin": self.peering.id, "origin_name": self.peering.name, "path": [self.peering.id], "ts": utc(time.time()), "data": data}})

    def _air_text(self, link, oname, data, row):
        """A message from a peer goes onto this mesh: prefixed with the origin's name, on the air channel, never shared back."""
        text = f"[{oname}] {data.get('text') or ''}"
        while len(text.encode()) > 200:
            text = text[:-1]
        channel = row.get("air_channel") if row.get("air_channel") is not None else int(data.get("channel") or 0)
        pkt = self.interface.sendText(text, destinationId="^all", channelIndex=int(channel), wantAck=True, onResponse=self._on_ack)
        pid = getattr(pkt, "id", None)
        if pid is not None:
            self.outbox[int(pid)] = {"ts": utc(time.time()), "text": text, "to": "^all", "channel": int(channel), "ack": None}
        own0 = self._own() or {}
        self._emit("text", **{"from": own0.get("id"), "name": own0.get("name") or "this box", "to": "^all", "channel": int(channel), "text": text, "mid": pid, "sent": True, "aired_from": oname})
        try:
            if getattr(self, "history", None) and self.history.ok:
                self.history.message(own0.get("id"), text, name=own0.get("name") or "this box", dest="^all", channel=int(channel), mid=pid, aired_from=link.peer_id)
        except Exception:  # noqa: BLE001
            pass
        self._peer_aired_count(link.peer_id)
        self._peer_receipt(link, data.get("mid"), True, channel=int(channel))
        self.logger.info(f"air: a message from {oname} went out on channel {channel}")

    def _air_waypoint(self, link, oname, data):
        name = f"{data.get('name') or 'waypoint'} ({oname})"
        while len(name.encode()) > 30:
            name = name[:-1]
        desc = str(data.get("description") or "")[:100]
        try:
            self.interface.sendWaypoint(name, desc, 0, int(data.get("expire") or int(time.time()) + 3600), waypoint_id=int(data["wid"]),
                                        latitude=float(data.get("lat") or 0), longitude=float(data.get("lon") or 0), wantAck=True)
            self._peer_aired_count(link.peer_id)
            self.logger.info(f"air: a waypoint from {oname} went out")
        except Exception as ex:  # noqa: BLE001
            self.logger.warning(f"air: the waypoint from {oname} did not go out: {type(ex).__name__}: {ex}")

    def op_peer_send_text(self, site="", channel=0, text="", **_):
        """Send a message into a peer's chat over the link; it goes on the air there only if that site allows it."""
        if not self.peering:
            return {"error": "this bridge has no site identity"}
        site = str(site or "").strip().lower(); text = str(text or "")
        if not text.strip():
            return {"error": "empty message"}
        if len(text.encode()) > 180:
            return {"error": "a message across the link is 180 bytes at most: the far site adds its prefix"}
        try:
            channel = int(channel or 0)
        except (TypeError, ValueError):
            return {"error": "channel is an index, 0 to 7"}
        link = self.peering.connected().get(site)
        if not link:
            return {"error": "that site is not connected"}
        self._peer_mid = getattr(self, "_peer_mid", int(time.time()) % 100000) + 1; mid = self._peer_mid
        own0 = self._own() or {}
        data = {"from": own0.get("id") or self.peering.id, "name": self.peering.name, "to": "^all", "channel": channel, "text": text, "mid": mid, "sent": True, "ts": utc(time.time())}
        link.send({"item": {"class": "messages", "origin": self.peering.id, "origin_name": self.peering.name, "path": [self.peering.id], "ts": data["ts"], "data": data, "for": site}})
        self._emit("text", origin=site, origin_name=link.peer_name, mine=True, **data)   # shows in the remote chat as this site's own bubble
        return {"sent": True, "mid": mid, "site": site, "channel": channel}

    def op_peers(self, **_):
        if not self.peering:
            return {"error": "this bridge has no site identity"}
        with self._peers_lock:
            d = self._peers_load()
        live = self.peering.connected()
        out = []
        for pid, rec in sorted(d["peers"].items(), key=lambda kv: kv[1].get("name") or kv[0]):
            l = live.get(pid); snap = self.remote_nodes.get(pid, {})
            out.append({"id": pid, "name": rec.get("name") or pid[:12], "state": "connected" if l else "away", "direction": rec.get("direction"),
                        "since": utc(l.since) if l else None, "last_seen": utc(l.last_seen) if l else rec.get("last_seen"), "added": rec.get("added"),
                        "nodes": len(snap.get("nodes", [])), "sharing": self._sharing(pid), "aired": rec.get("aired") or {"count": 0, "last": None}, "note": self.peering.refusals.get(pid)})
        invites = [{"expires": utc(v["expires"])} for k, v in d["invites"].items() if float(v.get("expires") or 0) >= time.time() and not v.get("used")]
        addr = str(self.conf.get("SITE_ADDRESS") or "").strip()
        return {"site": {"id": self.peering.id, "short": self.peering.id[:12], "name": self.peering.name, "address": addr or None, "listening": bool(self.peering.port), "port": self.peering.port},
                "peers": out, "invites": invites, "pictures": [{"origin": o, "name": v.get("name"), "nodes": len(v.get("nodes", [])), "ts": utc(v.get("ts"))} for o, v in self.remote_nodes.items()]}

    def op_peer_invite(self, **_):
        if not self.peering:
            return {"error": "this bridge has no site identity"}
        if not self.peering.port:
            return {"error": "this site is not listening: set PEER_BIND (the installer's --peer-bind) and restart the bridge"}
        alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
        code = "".join(secrets.choice(alphabet) for _ in range(8)); exp = time.time() + P.INVITE_TTL
        with self._peers_lock:
            d = self._peers_load(); d["invites"][code] = {"expires": exp, "made": utc(time.time())}; self._peers_save(d)
        host = str(self.conf.get("SITE_ADDRESS") or "").strip() or socket.gethostname()
        invite = f"{host}:{self.peering.port}/{code}/{self.peering.id}"
        svg = None
        try:
            import pyqrcode
            svg = pyqrcode.create(invite, error="M").svg(scale=4, quiet_zone=2, xmldecl=False, svgclass="qr", lineclass=None, omithw=True)
        except Exception:  # noqa: BLE001
            svg = None
        self._emit("peers", state="invited")
        return {"invite": invite, "code": code, "expires": utc(exp), "fingerprint": self.peering.id, "qr_svg": svg, "note": f"read once, good for {P.INVITE_TTL // 60} minutes, one use"}

    def op_peer_join(self, invite="", **_):
        if not self.peering:
            return {"error": "this bridge has no site identity"}
        inv = P.parse_invite(invite)
        if not inv:
            return {"error": "an invite reads host:port/code/fingerprint, as the other site's screen shows it"}
        if inv["fingerprint"] == self.peering.id:
            return {"error": "that is this site's own invite"}
        with self._peers_lock:
            d = self._peers_load(); rec = d["peers"].setdefault(inv["fingerprint"], {"added": utc(time.time()), "sharing": self._sharing_defaults()})
            rec["address"] = f"{inv['host']}:{inv['port']}"; rec["direction"] = "out"; self._peers_save(d)
        result = []
        self.peering.dial(inv["fingerprint"], inv["host"], inv["port"], inv["code"], first_result=result)
        t0 = time.time()
        while not result and time.time() - t0 < 15:
            time.sleep(0.1)
        if not result:
            return {"error": "no answer from the site within 15 s; the link keeps trying", "site": inv["fingerprint"]}
        ok, why, ans = result[0]
        if not ok:
            if inv["code"]:  # a pairing that failed leaves nothing behind
                self.peering.stop_dial(inv["fingerprint"])
                with self._peers_lock:
                    d = self._peers_load(); d["peers"].pop(inv["fingerprint"], None); self._peers_save(d)
            return {"error": why, "site": inv["fingerprint"]}
        return {"joined": True, "site": (ans or {}).get("site"), "name": (ans or {}).get("name"), "confirmed": True}

    def op_peer_forget(self, site="", **_):
        site = str(site or "").strip().lower()
        with self._peers_lock:
            d = self._peers_load(); had = d["peers"].pop(site, None) is not None; self._peers_save(d)
            self.remote_nodes.pop(site, None); self.remote_waypoints.pop(site, None); self.remote_alerts.pop(site, None)   # Spec 056: the history keeps what it said
        if self.peering:
            self.peering.stop_dial(site)
        self._emit("peers", state="forgotten", site=site)
        return {"forgotten": had, "site": site}

    def _redial_pinned(self):
        """At start: dial every peer this site joined before (the address is remembered)."""
        if not self.peering:
            return
        with self._peers_lock:
            d = self._peers_load()
        for pid, rec in d["peers"].items():
            if rec.get("direction") == "out" and rec.get("address"):
                host, _, port = str(rec["address"]).rpartition(":")
                try:
                    self.peering.dial(pid, host, int(port))
                except ValueError:
                    pass

    def op_status(self, **_):
        path = (self.conf.get("SERIAL") or "") if self.box_mode != "hub" else None
        present = bool(path) and os.path.exists(path)
        lora = self._lora()
        chans = self.op_channels().get("channels", [])
        primary = next((c["name"] for c in chans if c["role"] == "PRIMARY"), None)
        return {"version": __version__, "uptime": int(time.time() - self.started), "radio": path,
                "radio_present": present, "bootloader": present and bootloader_mode(path),
                "connected": bool(getattr(self, "meshtastic_connected", False)),
                "last_activity": utc(self.last_activity) if self.last_activity else None,
                "last_forwarded": utc(self.last_forwarded) if self.last_forwarded else None,
                "nodes_seen": len(getattr(self, "meshtastic_devices", {})), "nodes_db": len(getattr(self.interface, "nodes", {}) or {}), "observe": self.observe,
                "mode": self.box_mode, "tak": "off" if self.box_mode in ("server", "hub") else "on",
                "site": ({"id": self.peering.id, "name": self.peering.name} if self.peering else None),
                "peers": len(self.peering.connected()) if self.peering else 0,
                "peer_port": self.peering.port if self.peering else None, "peer_bind": (self.conf.get("PEER_BIND") or None) if self.peering and self.peering.port else None,
                "own": self._own(), "region": region_name(lora.region) if lora else None,
                "chutil": self._own_chutil(), "verdict": self._verdict(self._own_chutil()),
                "alerts_open": len(self._alerts_load()["open"]),
                "modem_preset": preset_name(lora.modem_preset) if lora else None,
                "primary_channel": primary, "watchdog": self.watchdog_state, "position": self.own_position(),
                "forwarded_counter": getattr(self.socket_client, "packets", None) if self.observe else None,
                "gps": self.gps_state,
                "state_dir": self.state_dir, "socket": self.socket_path}

    def op_nodes(self, **_):
        db = getattr(self.interface, "nodes", {}) or {}
        regall = self._register_load() if hasattr(self, "_register_load") else {}
        labels = {k: str(v.get("label") or "") for k, v in regall.items()}
        groups = self._groups_load()
        out = []
        for n in (self.mesh_nodes() if self.interface is not None else []):
            rec = db.get(n.get("id"), {}) if isinstance(db, dict) else {}
            u = rec.get("user", {}) if isinstance(rec, dict) else {}
            n = dict(n)
            n["label"] = labels.get(n.get("id"), "")
            r_ = regall.get(n.get("id"), {}) if isinstance(regall.get(n.get("id")), dict) else {}
            n["group"] = str(r_.get("group") or "")
            n["tags"] = list(r_.get("tags") or [])
            n["icon_own"] = r_.get("icon") or ""
            n["icon"] = r_.get("icon") or (groups.get(n["group"]) or {}).get("icon") or "radio"
            # the battery, by trust: the telemetry this bridge heard (newest wins), the library's
            # node database, the gateway's figure. Above 100 means on external power (Spec 018).
            bat = self.batteries.get(n.get("id"))
            dm = rec.get("deviceMetrics", {}) if isinstance(rec, dict) else {}
            if bat:
                level, volts, ts = bat.get("level"), bat.get("voltage"), bat.get("ts")
            elif (dm.get("batteryLevel") is not None or dm.get("voltage") is not None) and rec.get("lastHeard") and time.time() - float(rec.get("lastHeard") or 0) <= 86400:
                # the database's figure only for a node heard in the last day, and it carries that time
                # as its age: a node heard weeks ago reads "no reading", never a weeks-old number (Spec 019)
                level, volts, ts = dm.get("batteryLevel"), dm.get("voltage"), utc(rec.get("lastHeard"))
            else:
                level, volts, ts = (n.get("battery") if n.get("battery") not in (None, 0) else None), None, None
            try:
                level = int(level) if level is not None else None
            except (TypeError, ValueError):
                level = None
            n["charging"] = bool(level is not None and level > 100)
            n["battery"] = None if (level is None or level > 100) else level
            n["voltage"] = round(float(volts), 2) if volts not in (None, 0, "0") else None
            n["battery_ts"] = ts
            n["hw"] = u.get("hwModel")
            n["short"] = u.get("shortName")
            self._identity_note(n.get("id"), u.get("publicKey"), u.get("hwModel"), u.get("role"))
            lh = rec.get("lastHeard") if isinstance(rec, dict) else None
            n["last_heard_db"] = utc(lh) if lh else None
            out.append(n)
        out.extend(self._remote_rows(set(str(n.get("id")) for n in out)))  # Spec 052: the peers' pictures
        return {"nodes": out, "count": len(out)}

    def _identity_note(self, nid, key, hw, role):
        """Spec 043: the radio's database says what a node is (hardware, role) and which public key it
        holds. Written to the register only when it changes, so the poll costs nothing; a changed key
        is kept beside the one before, for the alert pass to raise."""
        if not nid or not re.fullmatch(r"![0-9a-f]{8}", str(nid)):
            return
        cache = self.__dict__.setdefault("_ident_cache", {})
        new = (str(key or ""), str(hw or ""), str(role or ""))
        if cache.get(nid) == new:
            return
        cache[nid] = new
        reg = self._register_load()
        entry = reg.setdefault(nid, {})
        changed = False
        if hw and entry.get("hw") != hw:
            entry["hw"] = hw; changed = True
        if role and entry.get("role") != role:
            entry["role"] = role; changed = True
        if key:
            old = entry.get("public_key")
            if not old:
                entry["public_key"] = key; entry["key_since"] = utc(time.time()); changed = True
            elif old != key:
                entry.update({"key_previous": old, "public_key": key, "key_changed": utc(time.time())}); changed = True
                self.logger.warning(f"the public key of {nid} changed; if the radio was not reflashed, treat it as an impostor")
        if changed:
            self._register_save(reg)
            self._emit("register", id=nid)

    def op_inventory(self, **_):
        """Spec 043: one row per radio the register or the database knows: what it is, what it runs,
        which key it holds, whether it is behind the shelf's verified image, when that was confirmed."""
        reg = self._register_load()
        nodes = {n.get("id"): n for n in self.op_nodes().get("nodes", []) if n.get("id")}
        try:
            images = [i for i in (self.op_firmware_shelf().get("images") or []) if not str(i.get("version") or "").startswith("erase")]
        except Exception:  # noqa: BLE001
            images = []
        rows = []
        for nid in sorted(set(reg) | set(nodes)):
            r, n = reg.get(nid, {}), nodes.get(nid, {})
            hw = n.get("hw") or r.get("hw")
            fw = r.get("firmware")
            img = next((i for i in images if hw and hw in (i.get("hw") or []) and i.get("recommended")), None) or next((i for i in images if hw and hw in (i.get("hw") or [])), None)
            if fw is None:
                behind, why = None, "firmware unknown: read the device on the bench or over the air"
            elif not img:
                behind, why = None, "no image for this hardware on the shelf"
            elif img.get("state") != "verified":
                behind, why = None, f"the shelf's image for this hardware ({img.get('version')}) is not verified on this box"
            else:
                behind = firmware_behind(fw, img.get("version"))
                why = (f"behind the shelf's {img.get('version')}" if behind else ("on the shelf's version" if behind is False and firmware_behind(img.get("version"), fw) is False else f"newer than the shelf's {img.get('version')}")) if behind is not None else "firmware unreadable"
            confirmed = max([x for x in (r.get("seen_on_bench"), r.get("seen_on_air"), r.get("key_since")) if x] or [""]) or None
            rows.append({"id": nid, "name": r.get("label") or n.get("name") or r.get("name") or nid, "hw": hw, "firmware": fw, "role": n.get("role") or r.get("role"),
                         "fingerprint": key_fingerprint(r.get("public_key")), "key_since": r.get("key_since"), "key_changed": r.get("key_changed"), "key_ack": r.get("key_ack"),
                         "key_alarm": bool(r.get("key_changed") and str(r.get("key_changed")) > str(r.get("key_ack") or "")),
                         "managed": bool(r.get("managed")), "behind": behind, "behind_reason": why, "confirmed": confirmed, "heard": n.get("heard")})
        return {"rows": rows, "count": len(rows), "behind": sum(1 for x in rows if x["behind"]), "key_alarms": sum(1 for x in rows if x["key_alarm"])}

    def op_key_accept(self, id=None, **_):
        """The operator has looked at a changed key and accepts it (the radio was reflashed, or it is a
        new radio under an old id); the alarm clears and the key on file stands."""
        nid = str(id or "").strip()
        reg = self._register_load()
        if nid not in reg:
            return {"error": f"no device {nid} in the register"}
        reg[nid]["key_ack"] = utc(time.time())
        self._register_save(reg)
        a = self._alerts_load(); self._clear_alert(a, nid, "key"); self._alerts_save(a)
        self._emit("register", id=nid)
        return {"accepted": nid, "confirmed": True}

    def op_node(self, id=None, **_):
        for n in self.op_nodes()["nodes"]:
            if n.get("id") == id:
                return {"node": n}
        return {"error": f"no node {id}"}

    def op_channels(self, **_):
        chans = []
        url = None
        try:
            for c in self.interface.localNode.channels or []:
                s = c.settings
                chans.append({"index": int(c.index), "name": s.name or "", "role": ch_role_name(c.role),
                              "has_key": bool(getattr(s, "psk", b""))})
            url = self.interface.localNode.getURL(includeAll=True)
        except Exception as e:  # noqa: BLE001
            return {"channels": chans, "error": f"channel set not readable yet: {type(e).__name__}"}
        return {"channels": chans, "url": url}

    def op_config(self, **_):
        own = self._own()
        try:
            lc = self.interface.localNode.localConfig
            return {"long_name": own["name"], "short_name": own["short"], "role": role_name(lc.device.role),
                    "region": region_name(lc.lora.region), "modem_preset": preset_name(lc.lora.modem_preset),
                    "tx_power": int(lc.lora.tx_power),
                    "position_broadcast_secs": int(getattr(lc.position, "position_broadcast_secs", 0)),
                    "read_at": utc(getattr(self, "config_read_at", None) or self.started)}
        except Exception as e:  # noqa: BLE001
            return {"long_name": own["name"], "short_name": own["short"], "error": f"config not readable yet: {type(e).__name__}"}

    def op_log(self, n=200, **_):
        lines = list(self.ring)
        return {"lines": lines[-int(n):], "total": len(lines)}

    def op_send_text(self, text="", channel=0, to=None, **_):
        text = str(text or "")
        if not text.strip():
            return {"error": "empty message"}
        if len(text.encode()) > 200:
            return {"error": "a mesh message is 200 bytes at most"}
        dest = to or "^all"
        if isinstance(dest, str) and dest.startswith("group:"):
            # Spec 044: Meshtastic has no group address, so a group message is one direct message per member,
            # each with its own receipt; the screen says "n of m delivered", never "sent to the group"
            gname = dest[6:].strip()
            reg = self._register_load()
            members = sorted(nid for nid, r in reg.items() if isinstance(r, dict) and str(r.get("group") or "") == gname)
            if not members:
                return {"error": f"no device in group {gname!r}"}
            ids = [self._send_one(text, int(channel or 0), m) for m in members]
            return {"members": members, "ids": ids, "sent": text, "to": dest, "channel": int(channel or 0)}
        pid = self._send_one(text, int(channel or 0), dest)
        return {"id": pid, "sent": text, "to": dest, "channel": int(channel or 0)}

    def _send_one(self, text, channel, dest):
        # Spec 034: wantAck asks the radio to report delivery; the answer arrives on ROUTING_APP at
        # the handler, never by waiting. The id is how the answer finds the message again.
        pkt = self.interface.sendText(text, destinationId=dest, channelIndex=int(channel or 0), wantAck=True, onResponse=self._on_ack)
        pid = getattr(pkt, "id", None)
        if pid is not None:
            self.outbox[int(pid)] = {"ts": utc(time.time()), "text": text, "to": dest, "channel": int(channel or 0), "ack": None}
        own0 = self._own() or {}
        _msg = {"from": own0.get("id"), "name": own0.get("name") or "this box", "to": dest, "channel": int(channel or 0), "text": text, "mid": pid, "sent": True}
        self._emit("text", **_msg)
        self._share_text(_msg)   # Spec 053
        try:
            own = (self._own() or {})
            if getattr(self, "history", None) and self.history.ok:
                self.history.message(own.get("id"), text, name=own.get("name") or "this box", dest=dest, channel=int(channel or 0), mid=pid)
        except Exception:  # noqa: BLE001
            pass
        own = self._own()
        self._emit("text", **{"from": own.get("id"), "name": own.get("name") or "this radio", "to": dest,
                              "channel": int(channel or 0), "text": text, "sent": True})
        return pid

    def op_channel_url(self, index=0, **_):
        """A join URL for one channel slot alone, for a device joining a secondary channel from its own QR
        (5 Sep 2026 reviews: a channel created on the screen had no way to reach a handset). The screen asks
        this over the socket and turns it into a PNG; it is not a catalogue action, so the key never leaves
        the box through the API."""
        try:
            idx = int(index)
        except (TypeError, ValueError):
            return {"error": "index must be 0 to 7"}
        try:
            from meshtastic.protobuf import apponly_pb2, channel_pb2
            import base64
            chans = list(self.interface.localNode.channels or [])
            c = next((x for x in chans if int(x.index) == idx and x.role != channel_pb2.Channel.Role.DISABLED), None)
            if c is None:
                return {"error": f"no live channel in slot {idx}"}
            cs = apponly_pb2.ChannelSet()
            st = cs.settings.add(); st.CopyFrom(c.settings)
            cs.lora_config.CopyFrom(self.interface.localNode.localConfig.lora)
            url = "https://meshtastic.org/e/#" + base64.urlsafe_b64encode(cs.SerializeToString()).decode().rstrip("=")
            return {"index": idx, "name": c.settings.name or "", "url": url}
        except Exception as ex:  # noqa: BLE001
            return {"error": f"the channel could not be encoded: {type(ex).__name__}"}

    def op_traceroute(self, dest=None, **_):
        if not dest:
            return {"error": "dest is required"}
        # Never the library's sendTraceRoute: it blocks the caller for the hop-scaled timeout
        # and prints the answer. The request goes out with our own handler; the answer comes
        # back as a route record and a route event (Spec 008).
        from meshtastic.protobuf import mesh_pb2, portnums_pb2
        self.interface.sendData(mesh_pb2.RouteDiscovery(), destinationId=dest, portNum=portnums_pb2.PortNum.TRACEROUTE_APP,
                                wantResponse=True, onResponse=self._on_route, hopLimit=7)
        return {"requested": "traceroute", "dest": dest, "asked": utc(time.time())}

    # ---- links and routes (Spec 008) ---------------------------------------------------------------
    @staticmethod
    def _num_id(num):
        try:
            return f"!{int(num):08x}"
        except (TypeError, ValueError):
            return str(num)

    def _node_name(self, nid):
        label = str(self._register_load().get(nid, {}).get("label") or "")
        if label:
            return label
        own = self._own()
        if nid == own.get("id"):
            return own.get("name") or nid
        for n in self.mesh_nodes():
            if n.get("id") == nid and n.get("name"):
                return n["name"]
        try:
            u = (self.interface.nodes or {}).get(nid, {}).get("user", {})
            return u.get("longName") or nid
        except Exception:  # noqa: BLE001
            return nid

    def _on_route(self, p):
        """A traceroute answer (or the routing error that stands in for one)."""
        try:
            d = p.get("decoded", {}) if isinstance(p, dict) else {}
            dest = p.get("fromId") or self._num_id(p.get("from"))
            if d.get("portnum") == "ROUTING_APP":
                why = (d.get("routing") or {}).get("errorReason") or "no answer"
                self._emit("route", dest=dest, error=str(why))
                return
            tr = d.get("traceroute")
            if tr is None:
                from google.protobuf.json_format import MessageToDict
                from meshtastic.protobuf import mesh_pb2
                rd = mesh_pb2.RouteDiscovery()
                rd.ParseFromString(d.get("payload") or b"")
                tr = MessageToDict(rd)
            origin = p.get("toId") or self._own().get("id") or self._num_id(p.get("to"))

            def q(v):
                return None if v is None or int(v) == -128 else int(v) / 4

            def hop(nid, snr):
                return {"id": nid, "name": self._node_name(nid), "snr": snr}
            route, st = list(tr.get("route") or []), list(tr.get("snrTowards") or [])
            rb, sb = list(tr.get("routeBack") or []), list(tr.get("snrBack") or [])
            st_ok, sb_ok = len(st) == len(route) + 1, len(sb) == len(rb) + 1
            towards = [hop(self._num_id(n), q(st[i]) if st_ok else None) for i, n in enumerate(route)] + [hop(dest, q(st[-1]) if st_ok else None)]
            back = [hop(self._num_id(n), q(sb[i]) if sb_ok else None) for i, n in enumerate(rb)] + [hop(origin, q(sb[-1]) if sb_ok else None)]
            rec = {"dest": dest, "ts": utc(time.time()), "hops": len(route), "towards": towards, "back": back}
            self.routes[dest] = rec
            self._emit("route", **rec)
        except Exception as e:  # noqa: BLE001
            self.logger.warning(f"a traceroute answer could not be read: {type(e).__name__}: {e}")

    # ---- where the box is (Spec 014) ---------------------------------------------------------------
    def gps_path(self):
        """The receiver's by-id path: MAP_GPS as given, else the first by-id name that says GPS or GNSS."""
        given = str(self.conf.get("MAP_GPS") or "").strip()
        if given:
            return given
        try:
            for nm in sorted(os.listdir(self.serial_dir)):
                if re.search(r"gps|gnss", nm, re.I):
                    return os.path.join(self.serial_dir, nm)
        except OSError:
            pass
        return None

    @staticmethod
    def _nmea_deg(v, hemi):
        """ddmm.mmmm (or dddmm.mmmm) with N/S/E/W to signed decimal degrees."""
        if not v or "." not in v:
            return None
        head = v.split(".")[0]
        deg = int(head[:-2])
        minutes = float(v[len(head) - 2:])
        out = deg + minutes / 60.0
        return -out if hemi in ("S", "W") else out

    gpsd_address = ("127.0.0.1", 2947)

    def read_gpsd(self, host, port, timeout=10):
        """One fix from gpsd (which holds the receiver's port on a box that runs it): WATCH in
        JSON, the first TPV with a 2D or 3D fix, the satellite count from a SKY seen on the way.
        Returns the fix, or None; sets gps_state so a reachable gpsd with no fix is known as
        such and the port is never opened behind it."""
        import socket as _socket
        try:
            sock = _socket.create_connection((host, int(port)), timeout=3)
        except OSError:
            self.gps_state = {"reachable": False, "checked": utc(time.time()), "via": f"gpsd://{host}:{port}"}
            return None
        self.gps_state = {"reachable": True, "fix": False, "seen": None, "used": None, "checked": utc(time.time()), "via": f"gpsd://{host}:{port}"}
        sats = None
        try:
            sock.settimeout(timeout)
            sock.sendall(b'?WATCH={"enable":true,"json":true}\n')
            buf = b""
            deadline = time.time() + timeout
            while time.time() < deadline:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    try:
                        rec = json.loads(line.decode("utf-8", "ignore") or "{}")
                    except ValueError:
                        continue
                    if rec.get("class") == "SKY":
                        if rec.get("uSat") is not None:
                            sats = int(rec["uSat"])
                        self.gps_state.update({"seen": rec.get("nSat"), "used": rec.get("uSat")})
                    if rec.get("class") == "TPV" and int(rec.get("mode") or 0) >= 2 and rec.get("lat") is not None and rec.get("lon") is not None:
                        t = str(rec.get("time") or "")
                        self.gps_state.update({"fix": True})
                        return {"lat": round(float(rec["lat"]), 6), "lon": round(float(rec["lon"]), 6), "sats": sats, "quality": int(rec["mode"]),
                                "time": (t[:19] + "Z") if len(t) >= 19 else utc(time.time()), "path": f"gpsd://{host}:{port}"}
        except OSError:
            pass
        finally:
            try:
                sock.close()
            except OSError:
                pass
        return None

    def read_gps(self, timeout=30):
        """One fix: from gpsd when it answers (it holds the port on a box that runs it), else the
        receiver's port itself: open, read NMEA until a GGA (quality above 0) or an RMC (status A)
        carries a position, close. The port is never held between reads."""
        given = str(self.conf.get("MAP_GPS") or "").strip()
        if given.startswith("gpsd://"):
            host, _, port = given[len("gpsd://"):].partition(":")
            return self.read_gpsd(host or "127.0.0.1", port or 2947, timeout=min(timeout, 10))
        if not given:
            fix = self.read_gpsd(self.gpsd_address[0], self.gpsd_address[1], timeout=min(timeout, 10))
            if fix or (self.gps_state or {}).get("reachable"):
                return fix           # gpsd's word stands, fix or no fix; it holds the port, so never open it behind it
        path = self.gps_path()
        if not path:
            return None
        self.gps_state = {"reachable": True, "fix": False, "checked": utc(time.time()), "via": path}
        try:
            if self.gps_port_factory:
                port = self.gps_port_factory(path)
            else:
                import serial
                port = serial.Serial(path, 9600, timeout=1)
        except Exception as e:  # noqa: BLE001
            self.logger.warning(f"the GPS receiver at {path} could not be opened: {type(e).__name__}: {e}")
            return None
        deadline = time.time() + timeout
        try:
            while time.time() < deadline:
                raw = port.readline()
                if raw is None:
                    continue
                line = raw.decode("ascii", "ignore").strip() if isinstance(raw, (bytes, bytearray)) else str(raw).strip()
                if not line.startswith("$") or "," not in line:
                    if not line and not getattr(port, "lines", True):
                        break
                    continue
                f = line.split("*")[0].split(",")
                tag = f[0][-3:]
                try:
                    if tag == "GGA" and len(f) >= 8 and f[6] not in ("", "0"):
                        lat, lon = self._nmea_deg(f[2], f[3]), self._nmea_deg(f[4], f[5])
                        if lat is not None and lon is not None:
                            self.gps_state.update({"fix": True, "used": int(f[7] or 0)})
                            return {"lat": round(lat, 6), "lon": round(lon, 6), "sats": int(f[7] or 0), "quality": int(f[6]),
                                    "time": self._nmea_time(f[1]), "path": path}
                    if tag == "RMC" and len(f) >= 7 and f[2] == "A":
                        lat, lon = self._nmea_deg(f[3], f[4]), self._nmea_deg(f[5], f[6])
                        if lat is not None and lon is not None:
                            return {"lat": round(lat, 6), "lon": round(lon, 6), "sats": None, "quality": None, "time": self._nmea_time(f[1]), "path": path}
                except (ValueError, IndexError):
                    continue
            return None
        finally:
            try:
                port.close()
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    def _nmea_time(hhmmss):
        try:
            h, m, sec = int(hhmmss[0:2]), int(hhmmss[2:4]), int(float(hhmmss[4:]))
            return time.strftime("%Y-%m-%dT", time.gmtime()) + f"{h:02d}:{m:02d}:{sec:02d}Z"
        except (ValueError, TypeError):
            return utc(time.time())

    def _gps_loop(self):
        """A fix every five minutes when the receiver gives one; every minute of trying when not."""
        while not self._stop.is_set():
            fix = None
            try:
                if self.gps_path():
                    fix = self.read_gps(timeout=30)
            except Exception as e:  # noqa: BLE001
                self.logger.warning(f"GPS read failed: {type(e).__name__}: {e}")
            if fix:
                fix["read_at"] = utc(time.time())
                self.gps_fix = fix
            elif self.gps_fix and (self.gps_state or {}).get("reachable") and (self.gps_state or {}).get("fix") is False:
                self._gps_misses = getattr(self, "_gps_misses", 0) + 1
                if self._gps_misses >= 2:
                    self.gps_fix = None      # the receiver has lost its fix: stop placing the box by a stale one
            if fix:
                self._gps_misses = 0
            self._stop.wait(300 if fix else 60)

    def _radio_own_gps(self):
        """The gateway radio's position only when it is the radio's own GPS fix (LOC_INTERNAL) and
        recent; a stored or manually set position is not a fix and comes last."""
        try:
            pos = (self.interface.getMyNodeInfo() or {}).get("position") or {}
            if pos.get("latitude") is None or pos.get("longitude") is None:
                return None, None
            rec = {"lat": float(pos["latitude"]), "lon": float(pos["longitude"])}
            src = str(pos.get("locationSource") or pos.get("location_source") or "")
            t = pos.get("time")
            fresh = isinstance(t, (int, float)) and (time.time() - float(t)) < 3600
            if src == "LOC_INTERNAL" and fresh:
                rec["time"] = utc(float(t))
                rec["sats"] = pos.get("satsInView")
                return rec, None
            return None, rec
        except Exception:  # noqa: BLE001
            return None, None

    def own_position(self):
        """Where the box is, by trust (Matt, 3 Sep 2026: "it should use the GPS that is plugged
        in, or the GPS from one of the radios that is plugged in"): the box's own receiver's fix;
        the gateway radio's own GPS fix; the position declared at install; among the devices it
        hears (the median of their fixes, an estimate); the radio's stored or set position (the
        least trusted, 330 km off on the kit); none. The map says which."""
        fix = self.gps_fix
        if fix and fix.get("lat") is not None:
            return {"lat": fix["lat"], "lon": fix["lon"], "source": "gps", "sats": fix.get("sats"), "time": fix.get("time"), "read_at": fix.get("read_at")}
        radio_fix, radio_stored = self._radio_own_gps()
        if radio_fix:
            return dict(radio_fix, source="radio_gps")
        decl = self._declared_position()
        if decl:
            return {"lat": decl["lat"], "lon": decl["lon"], "source": "declared", "time": decl.get("set")}
        lat, lon = self.conf.get("MAP_LAT"), self.conf.get("MAP_LON")
        try:
            if lat not in (None, "") and lon not in (None, ""):
                return {"lat": float(lat), "lon": float(lon), "source": "config"}
        except (TypeError, ValueError):
            pass
        try:
            pts = [(float(n["lat"]), float(n["lon"])) for n in self.mesh_nodes() if n.get("heard_here") and n.get("lat") is not None and n.get("lon") is not None]
        except Exception:  # noqa: BLE001
            pts = []
        if pts:
            import statistics
            return {"lat": round(statistics.median(p[0] for p in pts), 6), "lon": round(statistics.median(p[1] for p in pts), 6), "source": "devices", "count": len(pts), "gps": self.gps_state}
        # a stored or manually set radio position is not a source: a radio without GPS carries
        # whatever it was last told (330 km off on the kit), and it showed for the minutes after
        # a restart before any device was heard. Nothing real means no position.
        return None

    def _position_path(self):
        return os.path.join(self.state_dir, "position.json")

    def _declared_position(self):
        try:
            d = json.load(open(self._position_path()))
            return {"lat": float(d["lat"]), "lon": float(d["lon"]), "set": d.get("set")}
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def op_box_position_set(self, lat=None, lon=None, clear=None, **_):
        """Spec 007 review, 5 Sep 2026: a box with no receiver had no way to be given a position on the
        screen, and without one the map view is off. Kept on the box; a receiver's fix still wins."""
        if str(clear or "").lower() in ("on", "yes", "true", "1"):
            try:
                os.remove(self._position_path())
            except OSError:
                pass
            self._emit("status", **self.op_status())
            return {"cleared": True, "confirmed": True}
        try:
            la, lo = float(str(lat).strip()), float(str(lon).strip())
        except (TypeError, ValueError):
            return {"error": "lat and lon must be decimal degrees, like 51.5000 and -0.1200"}
        if not (-90 <= la <= 90 and -180 <= lo <= 180):
            return {"error": "lat is -90 to 90 and lon is -180 to 180"}
        os.makedirs(self.state_dir, exist_ok=True)
        tmp = self._position_path() + ".tmp"
        with open(tmp, "w") as fh:
            json.dump({"lat": round(la, 6), "lon": round(lo, 6), "set": utc(time.time())}, fh)
        os.replace(tmp, self._position_path())
        self._emit("status", **self.op_status())
        return {"written": {"lat": round(la, 6), "lon": round(lo, 6)}, "confirmed": True, "source": self.own_position().get("source")}

    def op_links(self, **_):
        own, pos = self._own(), self.own_position() or {}
        nodes = []
        for n in self.op_nodes().get("nodes", []):
            nid = n.get("id")
            nodes.append(dict(n, direct_snr=self.direct.get(nid), history=[list(x) for x in self.links.get(nid, [])]))
        return {"own": {"id": own.get("id"), "name": own.get("name"), "lat": pos.get("lat"), "lon": pos.get("lon"), "position_source": pos.get("source"),
                        "sats": pos.get("sats"), "time": pos.get("time"), "count": pos.get("count"), "gps": self.gps_state},
                "nodes": nodes, "routes": dict(self.routes)}

    def op_route(self, id=None, **_):
        return {"route": self.routes.get(str(id or ""))}

    # ---- the register (Spec 009) --------------------------------------------------------------------
    def _register_path(self):
        return os.path.join(self.state_dir, "register.json")

    def _register_load(self):
        try:
            return json.load(open(self._register_path()))
        except (OSError, ValueError):
            return {}

    def _register_save(self, reg):
        os.makedirs(self.state_dir, exist_ok=True)
        tmp = self._register_path() + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(reg, fh, indent=1, sort_keys=True)
        try:
            os.chmod(tmp, 0o640)
        except OSError:
            pass
        os.replace(tmp, self._register_path())

    def op_register(self, **_):
        reg = self._register_load()
        groups = self._groups_load()
        rows, seen = [], set()
        for n in self.op_nodes().get("nodes", []):
            r = reg.get(n.get("id"), {})
            seen.add(n.get("id"))
            rows.append(dict(n, label=r.get("label", ""), holder=r.get("holder", ""), note=r.get("note", ""), hw=n.get("hw") or r.get("hw"),
                             firmware=r.get("firmware"), role=r.get("role"), managed=bool(r.get("managed")), managed_at=r.get("managed_at"),
                             onboarded_at=r.get("onboarded_at"), export_at=r.get("export_at")))
        for nid, r in reg.items():
            if nid in seen:
                continue
            # onboarded on the bench, never yet heard on the air: in the register, not in the radio's database
            rows.append({"id": nid, "name": r.get("name") or nid, "heard_here": False, "heard": None, "battery": None, "snr": None, "hops": None,
                         "group": str(r.get("group") or ""), "tags": list(r.get("tags") or []), "icon": r.get("icon") or (groups.get(str(r.get("group") or "")) or {}).get("icon") or "radio",
                         "label": r.get("label", ""), "holder": r.get("holder", ""), "note": r.get("note", ""), "hw": r.get("hw"), "firmware": r.get("firmware"),
                         "role": r.get("role"), "managed": bool(r.get("managed")), "managed_at": r.get("managed_at"), "onboarded_at": r.get("onboarded_at"),
                         "export_at": r.get("export_at"), "bench_only": True})
        return {"rows": rows, "count": len(rows)}

    def op_register_set(self, id=None, label=None, holder=None, note=None, group=None, tags=None, icon=None, **_):
        nid = str(id or "").strip()
        if not re.fullmatch(r"![0-9a-f]{8}", nid):
            return {"error": "id must be a radio id, !hex"}
        if icon is not None and str(icon) not in ("", "inherit") and str(icon) not in NODE_ICONS:
            return {"error": "icon must be one of: " + ", ".join(NODE_ICONS) + ", or inherit for the group's"}
        reg = self._register_load()
        entry = reg.setdefault(nid, {})
        for k, v in (("label", label), ("holder", holder), ("note", note)):
            if v is not None:
                entry[k] = str(v)[:200]
        if group is not None:
            entry["group"] = str(group).strip()[:40]
        if tags is not None:
            raw = tags if isinstance(tags, list) else str(tags).split(",")
            seen, clean = set(), []
            for t in raw:
                t = str(t).strip()[:24]
                if t and t.lower() not in seen:
                    seen.add(t.lower()); clean.append(t)
            entry["tags"] = clean[:10]
        if icon is not None:
            entry["icon"] = "" if str(icon) in ("", "inherit") else str(icon)
        entry["updated"] = utc(time.time())
        self._register_save(reg)
        self._emit("register", id=nid)
        return {"written": {"id": nid, "label": entry.get("label", ""), "holder": entry.get("holder", ""), "note": entry.get("note", ""),
                            "group": entry.get("group", ""), "tags": entry.get("tags", []), "icon": entry.get("icon", "")}, "confirmed": True}

    # ---- groups (Spec 044): a name and an icon, kept on the box; membership lives on each register entry
    def _groups_path(self):
        return os.path.join(self.state_dir, "groups.json")

    def _groups_load(self):
        try:
            d = json.load(open(self._groups_path()))
            return {str(k): v for k, v in d.items() if isinstance(v, dict)}
        except (OSError, ValueError, AttributeError):
            return {}

    def _groups_save(self, g):
        os.makedirs(self.state_dir, exist_ok=True)
        tmp = self._groups_path() + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(g, fh, indent=1, sort_keys=True)
        os.replace(tmp, self._groups_path())

    def op_groups(self, **_):
        g = self._groups_load()
        reg = self._register_load()
        counts = {}
        for r in reg.values():
            if isinstance(r, dict) and r.get("group"):
                counts[str(r["group"])] = counts.get(str(r["group"]), 0) + 1
        names = sorted(set(g) | set(counts), key=str.lower)
        return {"groups": [{"name": n, "icon": (g.get(n) or {}).get("icon") or "radio", "count": counts.get(n, 0), "declared": n in g} for n in names], "icons": list(NODE_ICONS)}

    def op_group_set(self, name=None, icon=None, **_):
        name = str(name or "").strip()[:40]
        if not name:
            return {"error": "name is required"}
        icon = str(icon or "radio")
        if icon not in NODE_ICONS:
            return {"error": "icon must be one of: " + ", ".join(NODE_ICONS)}
        g = self._groups_load()
        g[name] = {"icon": icon, "created": (g.get(name) or {}).get("created") or utc(time.time())}
        self._groups_save(g)
        self._emit("register", group=name)
        return {"group": {"name": name, "icon": icon}, "confirmed": True}

    def op_group_delete(self, name=None, **_):
        name = str(name or "").strip()
        g = self._groups_load()
        reg = self._register_load()
        cleared = [nid for nid, r in reg.items() if isinstance(r, dict) and str(r.get("group") or "") == name]
        if name not in g and not cleared:
            return {"error": f"no group {name!r}"}
        g.pop(name, None)
        for nid in cleared:
            reg[nid]["group"] = ""
        self._groups_save(g)
        if cleared:
            self._register_save(reg)
        self._emit("register", group=name)
        return {"removed": name, "cleared": cleared, "confirmed": True}

    def _register_note(self, snap, where="bench", **more):
        """What a read of the device itself established: hardware, firmware, role, managed."""
        nid = snap.get("id")
        if not nid:
            return {}
        reg = self._register_load()
        entry = reg.setdefault(nid, {})
        entry.update({"name": snap.get("long_name") or entry.get("name"), "hw": snap.get("hw") or entry.get("hw"), "firmware": snap.get("firmware") or entry.get("firmware"),
                      "role": snap.get("role") or entry.get("role"), "managed": bool(snap.get("managed")), f"seen_on_{where}": utc(time.time())})
        if snap.get("managed") and not entry.get("managed_at"):
            entry["managed_at"] = utc(time.time())
        if not snap.get("managed"):
            entry.pop("managed_at", None)
        entry.update({k: v for k, v in more.items() if v is not None})
        self._register_save(reg)
        self._emit("register", id=nid)
        return entry

    # ---- over the air (Spec 011) ----------------------------------------------------------------------
    @staticmethod
    def _channel_row(index, ch):
        from meshtastic.protobuf import channel_pb2
        return {"index": index, "name": ch.settings.name, "role": channel_pb2.Channel.Role.Name(ch.role), "has_key": bool(ch.settings.psk), "_psk": bytes(ch.settings.psk)}

    def _admin_many(self, node, specs, timeout=None):
        """Several admin round trips to one node at once: (name, build, extract) each; every
        answer waited for within one window. A name missing from the result never answered."""
        from meshtastic.protobuf import admin_pb2
        out, waits = {}, []
        for name, build, extract in specs:
            done = threading.Event()
            def on_resp(pk, name=name, extract=extract, done=done):
                try:
                    out[name] = extract(pk["decoded"]["admin"]["raw"])
                except Exception as e:  # noqa: BLE001
                    out[name] = e
                done.set()
            p = admin_pb2.AdminMessage()
            build(p)
            node._sendAdmin(p, wantResponse=True, onResponse=on_resp)
            waits.append(done)
        deadline = time.time() + (timeout or self.READBACK_S)
        for done in waits:
            done.wait(max(0.0, deadline - time.time()))
        return {k: v for k, v in out.items() if not isinstance(v, Exception)}

    @staticmethod
    def _spec_section(section):
        from meshtastic.protobuf import admin_pb2
        def build(p): p.get_config_request = admin_pb2.AdminMessage.ConfigType.Value(section.upper() + "_CONFIG")
        def extract(raw): return getattr(raw.get_config_response, section)
        return section, build, extract

    def _spec_owner(self):
        def build(p): p.get_owner_request = True
        def extract(raw): return raw.get_owner_response
        return "owner", build, extract

    def _spec_metadata(self):
        def build(p): p.get_device_metadata_request = True
        def extract(raw): return raw.get_device_metadata_response.firmware_version
        return "metadata", build, extract

    def _spec_channel(self, index):
        def build(p): p.get_channel_request = index + 1
        def extract(raw, index=index): return self._channel_row(index, raw.get_channel_response)
        return f"ch{index}", build, extract

    def _remote(self, nid, write=False):
        nid = str(nid or "").strip()
        if not re.fullmatch(r"![0-9a-f]{8}", nid):
            return None, None, "id must be a radio id, !hex"
        if write and not self._register_load().get(nid, {}).get("managed"):
            return None, nid, f"{nid} is not managed: bring it to the bench first"
        try:
            if self.node_factory:
                return self.node_factory(nid), nid, None
            import meshtastic.node
            return meshtastic.node.Node(self.interface, nid, timeout=self.READBACK_S), nid, None
        except Exception as e:  # noqa: BLE001
            return None, nid, f"could not address {nid}: {type(e).__name__}: {e}"

    def _remote_prepare(self, node, nid):
        """The admin session passkey (firmware 2.5's replay guard) before the first write; the
        library keeps it on the interface's node record and carries it on every admin message."""
        try:
            node.ensureSessionKey()
        except Exception as e:  # noqa: BLE001
            return f"could not ask {nid} for a session key: {type(e).__name__}: {e}"
        try:
            num = int(nid[1:], 16)
            recs = getattr(self.interface, "nodesByNum", None)
            deadline = time.time() + self.READBACK_S
            while recs is not None and time.time() < deadline:
                rec = recs.get(num) or {}
                if rec.get("adminSessionPassKey"):
                    break
                time.sleep(0.2)
        except Exception:  # noqa: BLE001
            pass
        return None

    def _remote_snapshot(self, node, nid, channels=3):
        got = self._admin_many(node, [self._spec_owner(), self._spec_section("lora"), self._spec_section("device"), self._spec_section("position"),
                                      self._spec_section("security"), self._spec_metadata()] + [self._spec_channel(i) for i in range(channels)])
        if "owner" not in got:
            return {"error": f"{nid} did not answer over the air within {self.READBACK_S} s: it may be asleep, out of range, or not carrying this radio's key"}, got
        ours = self._own_public_key()
        keys = [bytes(k) for k in got["security"].admin_key] if "security" in got else None
        lora, device, position = got.get("lora"), got.get("device"), got.get("position")
        snap = {"id": nid, "long_name": got["owner"].long_name, "short_name": got["owner"].short_name,
                "region": region_name(lora.region) if lora is not None else None, "modem_preset": preset_name(lora.modem_preset) if lora is not None else None,
                "tx_power": int(lora.tx_power) if lora is not None else None, "role": role_name(device.role) if device is not None else None,
                "position_broadcast_secs": int(position.position_broadcast_secs) if position is not None else None,
                "managed": bool(ours) and keys is not None and ours in keys, "admin_keys": len(keys) if keys is not None else None,
                "firmware": got.get("metadata") if isinstance(got.get("metadata"), str) else None, "read_at": utc(time.time()),
                "channels": [self._public(got[f"ch{i}"]) for i in range(channels) if f"ch{i}" in got and got[f"ch{i}"]["role"] != "DISABLED"],
                "missing": sorted(k for k in ("lora", "device", "position", "security", "metadata") if k not in got)}
        return snap, got

    def op_node_read(self, id=None, **_):
        node, nid, err = self._remote(id)
        if err:
            return {"error": err}
        snap, _got = self._remote_snapshot(node, nid)
        if snap.get("error"):
            return snap
        self._register_note(snap, where="air", snapshot=self._snap_fields(snap))
        self._emit("node", id=nid, action="node_read")
        return snap

    def _confirm_device(self, confirm, nid, why):
        if str(confirm or "").strip() != nid:
            return {"error": f"{why} Confirm by naming the device ({nid}) in 'confirm'."}
        return None

    def op_node_set(self, id=None, long_name=None, short_name=None, tx_power=None, position_broadcast_secs=None, **_):
        node, nid, err = self._remote(id, write=True)
        if err:
            return {"error": err}
        if long_name is None and short_name is None and tx_power is None and position_broadcast_secs is None:
            return {"error": "nothing to write"}
        err = self._remote_prepare(node, nid)
        if err:
            return {"error": err}
        sent, written = utc(time.time()), []
        try:
            if long_name is not None or short_name is not None:
                node.setOwner(long_name=str(long_name).strip() if long_name is not None else None, short_name=str(short_name).strip() if short_name is not None else None)
                written += [k for k, v in (("long_name", long_name), ("short_name", short_name)) if v is not None]
            need = [self._spec_section(sec) for sec, v in (("lora", tx_power), ("position", position_broadcast_secs)) if v is not None]
            if need:
                before = self._admin_many(node, need)
                for sec, val, field in (("lora", tx_power, "tx_power"), ("position", position_broadcast_secs, "position_broadcast_secs")):
                    if val is None:
                        continue
                    if sec not in before:
                        return {"written": written, "sent": sent, "confirmed": False, "read_back": {}, "unconfirmed": f"{nid} did not answer the read of its {sec} config before the write"}
                    getattr(node.localConfig, sec).CopyFrom(before[sec])
                    setattr(getattr(node.localConfig, sec), field, int(val))
                    node.writeConfig(sec)
                    written.append(field)
            after = self._admin_many(node, [self._spec_owner()] + need)
            rb = {}
            if "owner" in after:
                rb["long_name"], rb["short_name"] = after["owner"].long_name, after["owner"].short_name
            if "lora" in after:
                rb["tx_power"] = int(after["lora"].tx_power)
            if "position" in after:
                rb["position_broadcast_secs"] = int(after["position"].position_broadcast_secs)
            wanted = {k: v for k, v in (("long_name", long_name), ("short_name", short_name), ("tx_power", tx_power), ("position_broadcast_secs", position_broadcast_secs)) if v is not None}
            missing = [k for k in wanted if k not in rb]
            ok = not missing and all(str(rb[k]).strip() == str(v).strip() for k, v in wanted.items())
            why = None if ok else (f"{nid} did not answer within {self.READBACK_S} s" if missing else "the device's own answers differ from what was written")
            self._emit("node", id=nid, action="node_set", confirmed=ok)
            return {"written": written, "sent": sent, "confirmed": ok, "read_back": {k: rb[k] for k in wanted if k in rb}, "unconfirmed": why}
        except Exception as e:  # noqa: BLE001
            return {"error": f"over the air to {nid} failed: {type(e).__name__}: {e}"}

    def op_node_set_region(self, id=None, region=None, modem_preset=None, role=None, confirm=None, **_):
        node, nid, err = self._remote(id, write=True)
        if err:
            return {"error": err}
        if region is None and modem_preset is None and role is None:
            return {"error": "nothing to write"}
        bad = self._confirm_device(confirm, nid, "Changing the region, preset or role moves that device to another band or role; it may be unreachable over the air afterwards and reboots.")
        if bad:
            return bad
        err = self._remote_prepare(node, nid)
        if err:
            return {"error": err}
        sent, written = utc(time.time()), []
        try:
            need = [self._spec_section("lora")] if (region is not None or modem_preset is not None) else []
            if role is not None:
                need.append(self._spec_section("device"))
            before = self._admin_many(node, need)
            if any(sec not in before for sec, _b, _e in need):
                return {"written": [], "sent": sent, "confirmed": False, "read_back": {}, "unconfirmed": f"{nid} did not answer the read before the write"}
            if "lora" in before:
                node.localConfig.lora.CopyFrom(before["lora"])
                if region is not None:
                    node.localConfig.lora.region = self._enum_value("region", region); written.append("region")
                if modem_preset is not None:
                    node.localConfig.lora.modem_preset = self._enum_value("modem_preset", modem_preset); written.append("modem_preset")
                node.writeConfig("lora")
            if "device" in before:
                node.localConfig.device.CopyFrom(before["device"])
                node.localConfig.device.role = self._enum_value("role", role); written.append("role")
                node.writeConfig("device")
            after = self._admin_many(node, need)
            rb = {}
            if "lora" in after:
                rb["region"], rb["modem_preset"] = region_name(after["lora"].region), preset_name(after["lora"].modem_preset)
            if "device" in after:
                rb["role"] = role_name(after["device"].role)
            wanted = {k: v for k, v in (("region", region), ("modem_preset", modem_preset), ("role", role)) if v is not None}
            missing = [k for k in wanted if k not in rb]
            ok = not missing and all(rb[k] == v for k, v in wanted.items())
            why = None if ok else (f"{nid} did not answer within {self.READBACK_S} s (it may have rebooted onto the new setting)" if missing else "the device's own answers differ from what was written")
            self._emit("node", id=nid, action="node_set_region", confirmed=ok)
            return {"written": written, "sent": sent, "confirmed": ok, "read_back": {k: rb[k] for k in wanted if k in rb}, "unconfirmed": why}
        except Exception as e:  # noqa: BLE001
            return {"error": f"over the air to {nid} failed: {type(e).__name__}: {e}"}

    def op_node_channel_push(self, id=None, index=None, confirm=None, **_):
        node, nid, err = self._remote(id, write=True)
        if err:
            return {"error": err}
        try:
            index = int(index)
        except (TypeError, ValueError):
            return {"error": "index must be 0 to 7"}
        if not 0 <= index <= 7:
            return {"error": "index must be 0 to 7"}
        if index == 0:
            bad = self._confirm_device(confirm, nid, "Slot 0 replaces the device's primary channel; if the keys differ it will not hear this mesh afterwards.")
            if bad:
                return bad
        err = self._remote_prepare(node, nid)
        if err:
            return {"error": err}
        sent = utc(time.time())
        try:
            from meshtastic.protobuf import admin_pb2
            src = self.interface.localNode.channels[index]
            p = admin_pb2.AdminMessage()
            p.set_channel.CopyFrom(src)
            p.set_channel.index = index
            node._sendAdmin(p, wantResponse=False)
            after = self._admin_many(node, [self._spec_channel(index)])
            row = after.get(f"ch{index}")
            ok = bool(row) and row["name"] == src.settings.name and row["_psk"] == bytes(src.settings.psk) and row["role"] == self._channel_row(index, src)["role"]
            why = None if ok else (f"{nid} did not answer within {self.READBACK_S} s" if not row else "the device's own answer differs from what was written")
            self._emit("node", id=nid, action="node_channel_push", index=index, confirmed=ok)
            return {"written": ["channel"], "sent": sent, "confirmed": ok, "read_back": self._public(row) if row else {}, "unconfirmed": why}
        except Exception as e:  # noqa: BLE001
            return {"error": f"over the air to {nid} failed: {type(e).__name__}: {e}"}

    def op_node_reboot(self, id=None, confirm=None, **_):
        node, nid, err = self._remote(id, write=True)
        if err:
            return {"error": err}
        bad = self._confirm_device(confirm, nid, "The device reboots in ten seconds and is off the mesh while it does.")
        if bad:
            return bad
        err = self._remote_prepare(node, nid)
        if err:
            return {"error": err}
        try:
            node.reboot(10)
        except Exception as e:  # noqa: BLE001
            return {"error": f"over the air to {nid} failed: {type(e).__name__}: {e}"}
        self._emit("node", id=nid, action="node_reboot")
        return {"asked": utc(time.time()), "id": nid, "secs": 10, "note": "asked; watch for it to be heard again"}

    # ---- restore and firmware (Spec 010) ---------------------------------------------------------------
    def _exports_dir(self):
        return os.path.join(self.state_dir, "exports")

    def op_bench_exports(self, id=None, **_):
        nid = str(id or "").strip()
        if not re.fullmatch(r"![0-9a-f]{8}", nid):
            return {"error": "id must be a radio id, !hex"}
        d = os.path.join(self._exports_dir(), nid)
        out = []
        try:
            for fn in sorted(os.listdir(d), reverse=True):
                if fn.endswith(".json"):
                    fp = os.path.join(d, fn)
                    out.append({"path": fp, "when": fn[:-5].replace("-", ":", 3).replace(":", "-", 2) if False else fn[:-5], "bytes": os.path.getsize(fp)})
        except OSError:
            pass
        return {"id": nid, "exports": out}

    def op_bench_restore(self, path=None, export=None, confirm=None, **_):
        exp = os.path.realpath(str(export or ""))
        root = os.path.realpath(self._exports_dir())
        if not exp.startswith(root + os.sep) or not exp.endswith(".json") or not os.path.exists(exp):
            return {"error": f"export must be one of this box's exports under {root}/"}
        try:
            doc = json.load(open(exp))
        except (OSError, ValueError) as e:
            return {"error": f"could not read the export: {type(e).__name__}: {e}"}
        with self._bench_lock:
            iface, err = self._bench_open(path)
            if err:
                return {"error": err}
            try:
                from google.protobuf.json_format import ParseDict
                node = iface.localNode
                u = ((iface.getMyNodeInfo() or {}).get("user") or {})
                nid = u.get("id")
                if doc.get("id") != nid and str(confirm or "").strip() != nid:
                    return {"error": f"the export was made from {doc.get('id')} and this device is {nid}; to clone it anyway, confirm by naming this device ({nid})"}
                sent, written = utc(time.time()), []
                owner = doc.get("owner") or {}
                if owner.get("long_name") or owner.get("short_name"):
                    node.setOwner(long_name=owner.get("long_name") or None, short_name=owner.get("short_name") or None)
                    written.append("owner")
                cfg = doc.get("config") or {}
                for sec in ("lora", "device", "position"):
                    if sec in cfg:
                        ParseDict(cfg[sec], getattr(node.localConfig, sec), ignore_unknown_fields=True)
                        node.writeConfig(sec)
                        written.append(sec)
                want_ch = {}
                for c in doc.get("channels") or []:
                    i = int(c.get("index", 0))
                    if not 0 <= i <= 7:
                        continue
                    ch = node.channels[i]
                    ch.settings.name = str(c.get("name") or "")
                    ch.settings.psk = bytes.fromhex(c.get("psk") or "")
                    ch.role = {"DISABLED": 0, "PRIMARY": 1, "SECONDARY": 2}.get(c.get("role"), 0)
                    ch.index = i
                    node.writeChannel(i)
                    want_ch[i] = (ch.settings.name, bytes(ch.settings.psk), c.get("role"))
                if want_ch:
                    written.append("channels")
                snap, raw = self._bench_snapshot(iface)
                ok = True
                if owner.get("long_name") and snap["long_name"] != owner["long_name"]:
                    ok = False
                if owner.get("short_name") and snap["short_name"] != owner["short_name"]:
                    ok = False
                for sec in ("lora", "device", "position"):
                    if sec in cfg:
                        from google.protobuf.json_format import MessageToDict
                        if MessageToDict(raw[sec]) != MessageToDict(ParseDict(cfg[sec], type(raw[sec])(), ignore_unknown_fields=True)):
                            ok = False
                for i, (name, psk, role) in want_ch.items():
                    row = raw["channels"][i]
                    if row["name"] != name or bytes(row.get("_psk") or b"") != psk or row["role"] != role:
                        ok = False
                self._register_note(snap, where="bench", snapshot=self._snap_fields(snap))
                self._emit("bench", action="restore", id=nid, confirmed=ok)
                return {"written": written, "sent": sent, "confirmed": ok,
                        "read_back": {"long_name": snap["long_name"], "short_name": snap["short_name"], "region": snap["region"], "modem_preset": snap["modem_preset"], "role": snap["role"], "channels": len(snap["channels"])},
                        "unconfirmed": None if ok else "the device's own answers differ from the export"}
            except Exception as e:  # noqa: BLE001
                return {"error": f"restore failed: {type(e).__name__}: {e}"}
            finally:
                iface.close()

    def _pins(self):
        if self.pins is not None:
            return self.pins
        here = os.path.dirname(os.path.abspath(__file__))
        for cand in (os.path.join(here, "PINS.json"), os.path.join(here, os.pardir, os.pardir, "firmware", "PINS.json")):
            try:
                self.pins = json.load(open(cand))
                return self.pins
            except (OSError, ValueError):
                continue
        self.pins = {"images": []}
        return self.pins

    def _firmware_dir(self):
        return os.path.join(self.state_dir, "firmware")

    def op_firmware_shelf(self, **_):
        import hashlib
        d = self._firmware_dir()
        out = []
        for img in self._pins().get("images", []):
            fp = os.path.join(d, img["file"])
            rec = {k: img.get(k) for k in ("id", "hw", "version", "method", "file", "recommended", "note", "offset", "chip")}
            rec["path"] = fp
            if not os.path.exists(fp):
                rec["state"] = "missing"
            else:
                h = hashlib.sha256()
                with open(fp, "rb") as fh:
                    for chunk in iter(lambda: fh.read(1 << 20), b""):
                        h.update(chunk)
                rec["state"] = "verified" if h.hexdigest() == img.get("sha256") else "wrong"
                rec["bytes"] = os.path.getsize(fp)
            out.append(rec)
        return {"dir": d, "images": out}

    # the block layer and esptool, each replaceable by the suite through flash_hooks
    def _hook(self, name):
        return self.flash_hooks.get(name) or getattr(self, "_default_" + name)

    @staticmethod
    def _default_touch(path):
        import serial
        s = serial.Serial(path, 1200)
        s.setDTR(False)
        s.close()

    @staticmethod
    def _default_wait_volume(label, timeout):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                out = subprocess.run(["lsblk", "-rno", "NAME,LABEL,RM"], capture_output=True, text=True, timeout=10).stdout
                for ln in out.splitlines():
                    parts = ln.split()
                    if len(parts) >= 2 and parts[1].upper().replace("-", "") == label.upper().replace("-", ""):
                        return "/dev/" + parts[0]
            except (OSError, subprocess.SubprocessError):
                pass
            time.sleep(1)
        return None

    @staticmethod
    def _default_mount(dev):
        out = subprocess.run(["udisksctl", "mount", "-b", dev, "--no-user-interaction"], capture_output=True, text=True, timeout=30)
        m = re.search(r" at (\S+)", out.stdout)
        if out.returncode != 0 or not m:
            raise RuntimeError(f"udisksctl mount: {out.stderr.strip() or out.stdout.strip()}")
        return m.group(1).rstrip(".")

    @staticmethod
    def _default_copy(src, mnt):
        import shutil
        dst = os.path.join(mnt, os.path.basename(src))
        shutil.copyfile(src, dst)
        fd = os.open(dst, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    @staticmethod
    def _default_unmount(dev):
        subprocess.run(["udisksctl", "unmount", "-b", dev, "--no-user-interaction"], capture_output=True, text=True, timeout=30)

    @staticmethod
    def _default_wait_port(path, timeout):
        deadline = time.time() + timeout
        time.sleep(2)
        while time.time() < deadline:
            if os.path.exists(path):
                return True
            time.sleep(1)
        return False

    @staticmethod
    def _default_has_esptool():
        try:
            import esptool  # noqa: F401
            return True
        except ImportError:
            return False

    @staticmethod
    def _default_esptool(args):
        out = subprocess.run([sys.executable, "-m", "esptool"] + list(args), capture_output=True, text=True, timeout=600)
        return out.returncode, (out.stdout + out.stderr)[-2000:]

    def op_bench_flash(self, path=None, image=None, confirm=None, **_):
        pin = next((i for i in self._pins().get("images", []) if i.get("id") == str(image or "")), None)
        if not pin:
            return {"error": f"no such pin: {image}; see firmware_shelf"}
        shelf = {i["id"]: i for i in self.op_firmware_shelf().get("images", [])}.get(pin["id"], {})
        with self._bench_lock:
            iface, err = self._bench_open(path)
            if err:
                return {"error": err}
            stages, sent = [], utc(time.time())
            nid, hw = None, None
            try:
                u = ((iface.getMyNodeInfo() or {}).get("user") or {})
                nid, hw = u.get("id"), u.get("hwModel")
                if str(confirm or "").strip() != nid:
                    return {"error": f"a flash needs confirm = the device's own id ({nid}); it reboots the device and a factory image loses every setting"}
                if hw not in (pin.get("hw") or []):
                    return {"error": f"the pin {pin['id']} is for {', '.join(pin.get('hw') or [])} and this device is {hw}"}
                if shelf.get("state") != "verified":
                    return {"error": f"the image for {pin['id']} is {shelf.get('state', 'missing')} on this box: put the file at {shelf.get('path')} with sha256 {pin.get('sha256')}"}
                if pin["method"] == "esptool" and not self._hook("has_esptool")():
                    return {"error": "esptool is not on this box; the release carries it as wheels, reinstall from a cut that has them"}
                snap, raw = self._bench_snapshot(iface)
                export = self._export(snap, raw)
                self._register_note(snap, where="bench", export_at=utc(time.time()), snapshot=self._snap_fields(snap))
                stages.append("exported")
                self._emit("flash", id=nid, stage="exported", image=pin["id"])
            except Exception as e:  # noqa: BLE001
                return {"error": f"could not read and export the device before flashing: {type(e).__name__}: {e}"}
            finally:
                iface.close()
            recovery = "put the device into its bootloader by hand (double-press reset), flash the factory-erase image from the shelf, then the pinned image"
            fp = shelf["path"]
            try:
                if pin["method"] == "uf2":
                    self._hook("touch")(path)
                    dev = self._hook("wait_volume")(pin.get("volume") or "T1000-E", 60)
                    if not dev:
                        return {"stages": stages, "sent": sent, "confirmed": False, "export": export, "unconfirmed": f"the device did not present its bootloader volume within 60 s; {recovery} (factory-erase-S140 on the shelf)"}
                    stages.append("in bootloader"); self._emit("flash", id=nid, stage="in bootloader")
                    mnt = self._hook("mount")(dev)
                    try:
                        self._hook("copy")(fp, mnt)
                    finally:
                        self._hook("unmount")(dev)
                    stages.append("copied"); self._emit("flash", id=nid, stage="copied")
                else:
                    rc, log = self._hook("esptool")(["--chip", pin.get("chip", "esp32s3"), "--port", path, "--baud", "921600", "write_flash", pin.get("offset", "0x10000"), fp])
                    if rc != 0:
                        return {"stages": stages, "sent": sent, "confirmed": False, "export": export, "unconfirmed": f"esptool exited {rc}: {log[-400:]}; the device may not boot: {recovery.replace('factory-erase image', 'factory image')}"}
                    stages.append("copied"); self._emit("flash", id=nid, stage="copied")
                if not self._hook("wait_port")(path, 90):
                    return {"stages": stages, "sent": sent, "confirmed": False, "export": export, "unconfirmed": f"the device did not come back on {path} within 90 s; {recovery}"}
                stages.append("back"); self._emit("flash", id=nid, stage="back")
                iface2, err = self._bench_open(path)
                if err:
                    return {"stages": stages, "sent": sent, "confirmed": False, "export": export, "unconfirmed": f"the device is back but could not be opened: {err}"}
                try:
                    version = self._read_metadata(node=iface2.localNode)
                    snap2, _r = self._bench_snapshot(iface2)
                    self._register_note(snap2, where="bench")
                finally:
                    iface2.close()
                stages.append("version read"); self._emit("flash", id=nid, stage="version read", version=version)
                ok = bool(version) and (version.startswith(pin["version"]) or pin["version"].startswith("erase"))
                return {"stages": stages, "sent": sent, "confirmed": ok, "export": export, "version": version,
                        "unconfirmed": None if ok else f"the device reports firmware {version!r}, not {pin['version']}"}
            except Exception as e:  # noqa: BLE001
                return {"stages": stages, "sent": sent, "confirmed": False, "export": export, "unconfirmed": f"{type(e).__name__}: {e}; {recovery}"}

    # ---- the bench (Spec 009) -------------------------------------------------------------------------
    bootloader_check = staticmethod(bootloader_mode)

    def _gateway_real(self):
        return os.path.realpath(self.conf["SERIAL"]) if self.conf.get("SERIAL") else ""

    def op_bench_devices(self, **_):
        try:
            names = sorted(os.listdir(self.serial_dir))
        except OSError:
            names = []
        gw = self._gateway_real()
        gps = self.gps_path()
        gps_real = os.path.realpath(gps) if gps else ""
        out = []
        for nm in names:
            path = os.path.join(self.serial_dir, nm)
            if gw and os.path.realpath(path) == gw:
                continue
            if gps_real and os.path.realpath(path) == gps_real:
                continue
            boot = bool(self.bootloader_check(path))
            rec = {"path": path, "tty": os.path.basename(os.path.realpath(path)), "bootloader": boot}
            if boot:
                rec["recovery"] = RECOVERY_UF2
            if re.search(r"gps|gnss", nm, re.I):
                rec["kind"] = "gps"      # the box's own receiver on the same bus: not a radio, never opened
            out.append(rec)
        return {"gateway": self.conf.get("SERIAL") or "", "devices": out}

    def _bench_open(self, path):
        path = str(path or "")
        if not path.startswith(self.serial_dir.rstrip("/") + "/") or "/.." in path:
            return None, f"path must be under {self.serial_dir}/"
        gw = self._gateway_real()
        if gw and os.path.realpath(path) == gw:
            return None, "that is the gateway radio; it is set from the Radio page"
        if not os.path.exists(path):
            return None, f"nothing at {path}: is it plugged in? (ls -l {self.serial_dir}/)"
        if self.bootloader_check(path):
            return None, f"the device at {path} is in bootloader mode and answers nothing: {RECOVERY_UF2}"
        factory = self.serial_factory
        if factory is None:
            from meshtastic.serial_interface import SerialInterface as factory
        try:
            return factory(path), None
        except Exception as e:  # noqa: BLE001
            return None, f"could not open {path}: {type(e).__name__}: {e}"

    def _own_public_key(self):
        try:
            key = bytes(self.interface.localNode.localConfig.security.public_key or b"")
            if key:
                return key
        except Exception:  # noqa: BLE001
            pass
        try:
            import base64
            pk = ((self.interface.getMyNodeInfo() or {}).get("user") or {}).get("publicKey")
            return base64.b64decode(pk) if pk else b""
        except Exception:  # noqa: BLE001
            return b""

    def _read_metadata(self, node=None):
        def build(p): p.get_device_metadata_request = True
        def extract(raw): return raw.get_device_metadata_response.firmware_version
        try:
            return self._admin_query(build, extract, node=node)
        except Exception:  # noqa: BLE001
            return None

    def _bench_position(self, iface):
        """Spec 033: what the device on the cable says about its own receiver. Nothing is asked
        of the mesh. Three states are told apart because they mean different things to whoever is
        holding it: a fix, a receiver with no fix (indoors, the expected answer), and position
        switched off in the device's own config, which is a setting and not a fault."""
        try:
            cfg = self._read_section("position", node=iface.localNode)
            secs = int(getattr(cfg, "position_broadcast_secs", 0) or 0)
            mode = int(getattr(cfg, "gps_mode", 0) or 0)
        except Exception:  # noqa: BLE001
            secs, mode = 0, 1
        enabled = not (secs == 0 and mode in (0, 2))
        out = {"enabled": enabled, "fix": False, "lat": None, "lon": None, "alt": None, "sats": None, "time": None}
        if not enabled:
            out["state"] = "position is switched off on the device"
            return out
        try:
            pos = (iface.getMyNodeInfo() or {}).get("position") or {}
        except Exception:  # noqa: BLE001
            pos = {}
        lat = pos.get("latitude")
        lon = pos.get("longitude")
        if lat is None and pos.get("latitudeI") is not None:
            lat, lon = pos.get("latitudeI") / 1e7, (pos.get("longitudeI") or 0) / 1e7
        if lat is None or lon is None or (lat == 0 and lon == 0):
            out["state"] = "a receiver, but no fix"
            return out
        t = pos.get("time")
        try:
            from .mgrs import mgrs as _mgrs
            grid = _mgrs(float(lat), float(lon))
        except Exception:  # noqa: BLE001
            grid = None
        out.update({"fix": True, "lat": round(float(lat), 6), "lon": round(float(lon), 6),
                    "alt": pos.get("altitude"), "sats": pos.get("satsInView"), "mgrs": grid,
                    "time": utc(t) if t else None, "state": "a fix"})
        return out

    def _bench_snapshot(self, iface):
        """What the device itself says: every figure from its own admin answers."""
        node = iface.localNode
        u = ((iface.getMyNodeInfo() or {}).get("user") or {})
        owner = self._read_owner(node=node)
        lora = self._read_section("lora", node=node)
        device = self._read_section("device", node=node)
        sec = self._read_section("security", node=node)
        chans = [self._read_channel(i, node=node) for i in range(8)]
        ours = self._own_public_key()
        keys = [bytes(k) for k in sec.admin_key]
        position = self._bench_position(iface)
        snap = {"id": u.get("id"), "long_name": owner["long_name"], "short_name": owner["short_name"], "hw": u.get("hwModel"),
                "position": position,
                "firmware": self._read_metadata(node=node), "region": region_name(lora.region), "modem_preset": preset_name(lora.modem_preset),
                "role": role_name(device.role), "managed": bool(ours) and ours in keys, "admin_keys": len(keys),
                "channels": [self._public(c) for c in chans if c["role"] != "DISABLED"]}
        return snap, {"owner": owner, "lora": lora, "device": device, "security": sec, "channels": chans}

    def _export(self, snap, raw):
        """The device's owner, config and channels, keys included, under the state directory at 0600."""
        from google.protobuf.json_format import MessageToDict
        d = os.path.join(self.state_dir, "exports", str(snap.get("id") or "unknown"))
        os.makedirs(d, exist_ok=True)
        try:
            os.chmod(d, 0o700)
        except OSError:
            pass
        fn = os.path.join(d, utc(time.time()).replace(":", "-") + ".json")
        doc = {"exported": utc(time.time()), "id": snap.get("id"), "hw": snap.get("hw"), "firmware": snap.get("firmware"),
               "position": snap.get("position"), "owner": raw["owner"],
               "config": {k: MessageToDict(raw[k]) for k in ("lora", "device", "security")},
               "channels": [{"index": c["index"], "name": c["name"], "role": c["role"], "psk": bytes(c.get("_psk") or b"").hex()} for c in raw["channels"]]}
        fd = os.open(fn, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(doc, fh, indent=1)
        return fn

    def op_bench_read(self, path=None, **_):
        with self._bench_lock:
            iface, err = self._bench_open(path)
            if err:
                return {"error": err}
            try:
                snap, _raw = self._bench_snapshot(iface)
                snap["path"] = path
                self._register_note(snap)
                return snap
            except Exception as e:  # noqa: BLE001
                return {"error": f"could not read the device: {type(e).__name__}: {e}"}
            finally:
                iface.close()

    def op_bench_export(self, path=None, **_):
        with self._bench_lock:
            iface, err = self._bench_open(path)
            if err:
                return {"error": err}
            try:
                snap, raw = self._bench_snapshot(iface)
                fn = self._export(snap, raw)
                self._register_note(snap, export_at=utc(time.time()))
                return {"export": fn, "bytes": os.path.getsize(fn), "id": snap.get("id")}
            except Exception as e:  # noqa: BLE001
                return {"error": f"could not export: {type(e).__name__}: {e}"}
            finally:
                iface.close()

    def op_bench_onboard(self, path=None, long_name=None, short_name=None, role=None, label=None, holder=None, **_):
        long_name, short_name, role = str(long_name or "").strip(), str(short_name or "").strip(), str(role or "").strip()
        _lh = {k: str(v).strip()[:80] for k, v in (("label", label), ("holder", holder)) if v and str(v).strip()}
        if not long_name or len(long_name.encode()) > 39:
            return {"error": "long_name is required, 39 bytes at most"}
        if not short_name or len(short_name.encode()) > 4:
            return {"error": "short_name is required, 4 bytes at most"}
        try:
            role_v = self._enum_value("role", role)
        except Exception as e:  # noqa: BLE001
            return {"error": f"bad role: {e}"}
        ours = self._own_public_key()
        if not ours:
            return {"error": "this radio's public key is not readable, so it cannot manage anything yet"}
        with self._bench_lock:
            iface, err = self._bench_open(path)
            if err:
                return {"error": err}
            try:
                node, gw = iface.localNode, self.interface.localNode
                sec = self._read_section("security", node=node)
                keys = [bytes(k) for k in sec.admin_key]
                if ours not in keys and len(keys) >= 3:
                    return {"error": f"the device already holds three admin keys and none is this radio's ({len(keys)} of 3); remove one on the device before onboarding"}
                sent = utc(time.time())
                written = []
                node.setOwner(long_name=long_name, short_name=short_name)
                written += ["long_name", "short_name"]
                node.localConfig.device.role = role_v
                node.writeConfig("device")
                written.append("role")
                ch = node.channels[0]
                ch.settings.name = gw.channels[0].settings.name
                ch.settings.psk = gw.channels[0].settings.psk
                ch.role = 1
                node.writeChannel(0)
                written.append("channel0")
                node.localConfig.lora.region = gw.localConfig.lora.region
                node.localConfig.lora.modem_preset = gw.localConfig.lora.modem_preset
                node.writeConfig("lora")
                written.append("lora")
                node.localConfig.security.CopyFrom(sec)
                if ours not in keys:
                    node.localConfig.security.admin_key.append(ours)
                node.writeConfig("security")
                written.append("admin_key")
                snap, raw = self._bench_snapshot(iface)
                ch0 = raw["channels"][0]
                ok = (snap["long_name"] == long_name and snap["short_name"] == short_name and snap["role"] == role
                      and ch0["name"] == gw.channels[0].settings.name and bytes(ch0.get("_psk") or b"") == bytes(gw.channels[0].settings.psk)
                      and int(raw["lora"].region) == int(gw.localConfig.lora.region) and int(raw["lora"].modem_preset) == int(gw.localConfig.lora.modem_preset)
                      and snap["managed"])
                fn = self._export(snap, raw)
                reg = self._register_load().get(snap.get("id") or "", {})
                entry = self._register_note(snap, export_at=utc(time.time()), onboarded_at=utc(time.time()) if ok else None,
                                            label=reg.get("label") or long_name)
                self._emit("bench", action="onboard", id=snap.get("id"), confirmed=ok)
                return {"written": written, "sent": sent, "confirmed": ok,
                        "read_back": {"long_name": snap["long_name"], "short_name": snap["short_name"], "role": snap["role"], "channel0": ch0["name"],
                                      "region": snap["region"], "modem_preset": snap["modem_preset"], "managed": snap["managed"], "admin_keys": snap["admin_keys"]},
                        "export": fn, "register": {"id": snap.get("id"), "managed": bool(entry.get("managed")), "label": entry.get("label", "")},
                        "unconfirmed": None if ok else "the device's own answers differ from what was written"}
            except Exception as e:  # noqa: BLE001
                return {"error": f"onboarding failed: {type(e).__name__}: {e}"}
            finally:
                iface.close()

    def op_request_position(self, dest=None, **_):
        if not dest:
            return {"error": "dest is required"}
        # never sendPosition(wantResponse=True): it blocks in waitForPosition (the traceroute
        # fault again, LESSONS 20); the answer comes to a handler and out as a position event
        from meshtastic.protobuf import mesh_pb2, portnums_pb2
        self.interface.sendData(mesh_pb2.Position(), destinationId=dest, portNum=portnums_pb2.PortNum.POSITION_APP,
                                wantResponse=True, onResponse=self._on_position_answer)
        return {"requested": "position", "dest": dest, "asked": utc(time.time())}

    def _on_position_answer(self, p):
        try:
            from meshtastic.protobuf import mesh_pb2
            d = p.get("decoded", {}) if isinstance(p, dict) else {}
            pos = mesh_pb2.Position()
            pos.ParseFromString(d.get("payload") or b"")
            nid = p.get("fromId") or self._num_id(p.get("from"))
            if pos.latitude_i and pos.longitude_i:
                self._emit("position", id=nid, lat=pos.latitude_i * 1e-7, lon=pos.longitude_i * 1e-7, ts=utc(time.time()))
            else:
                self._emit("position", id=nid, lat=None, lon=None, ts=utc(time.time()), note="answered without a fix")
        except Exception as e:  # noqa: BLE001
            self.logger.warning(f"a position answer could not be read: {type(e).__name__}: {e}")

    # ---- batteries (Spec 019): the store, the ask, the automatic pass
    def _battery_save(self):
        try:
            os.makedirs(self.state_dir, exist_ok=True)
            tmp = os.path.join(self.state_dir, ".batteries.tmp")
            with open(tmp, "w") as f:
                json.dump(self.batteries, f)
            os.replace(tmp, os.path.join(self.state_dir, "batteries.json"))
        except Exception as e:  # noqa: BLE001
            self.logger.debug(f"batteries.json not written: {type(e).__name__}: {e}")

    def _battery_note(self, nid, level, volts):
        """A reading from the air: newest wins, on disk, and out as a telemetry event."""
        if not nid or (level is None and volts is None):
            return
        try:
            level = int(level) if level is not None else None
        except (TypeError, ValueError):
            level = None
        volts = round(float(volts), 2) if volts not in (None, 0, "0") else None
        prev = self.batteries.get(nid) or {}
        now = time.time()
        self.batteries[nid] = {"level": level, "voltage": volts, "ts": utc(now)}
        if len(self.batteries) > 500:
            for k in sorted(self.batteries, key=lambda k: self.batteries[k].get("ts") or "")[: len(self.batteries) - 500]:
                self.batteries.pop(k, None)
        self._battery_save()
        same = False
        try:  # the same reading twice within two seconds (the answer handler and the receive path both see an answer)
            import datetime as _dt
            then = _dt.datetime.strptime(str(prev.get("ts")), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=_dt.timezone.utc).timestamp()
            same = prev.get("level") == level and prev.get("voltage") == volts and now - then < 2
        except Exception:  # noqa: BLE001
            same = False
        if not same:
            self._emit("telemetry", id=nid, battery=(None if level is None or level > 100 else level),
                       charging=bool(level is not None and level > 100), voltage=volts, ts=self.batteries[nid]["ts"])

    def op_request_telemetry(self, dest=None, **_):
        if not dest:
            return {"error": "dest is required"}
        # never sendTelemetry(wantResponse=True): it blocks in waitForTelemetry (LESSONS 20)
        from meshtastic.protobuf import portnums_pb2, telemetry_pb2
        self.interface.sendData(telemetry_pb2.Telemetry(), destinationId=dest, portNum=portnums_pb2.PortNum.TELEMETRY_APP,
                                wantResponse=True, onResponse=self._on_telemetry_answer)
        return {"requested": "telemetry", "dest": dest, "asked": utc(time.time())}

    def op_request_nodeinfo(self, dest=None, **_):
        """Spec 032: ask one node what it calls itself. A device renamed over the air keeps its
        old name on the screen until it next broadcasts, which on a quiet mesh is an hour, and
        the only way out was Forget, which throws away everything the box has heard from it to
        fix a label. Sending our own User is what the protocol expects for this exchange, and
        the node learns the gateway's name at the same time."""
        if not dest:
            return {"error": "dest is required"}
        from meshtastic.protobuf import mesh_pb2, portnums_pb2
        own = self._own() or {}
        me = mesh_pb2.User(id=str(own.get("id") or ""), long_name=str(own.get("name") or ""), short_name=str(own.get("short") or ""))
        self.interface.sendData(me, destinationId=dest, portNum=portnums_pb2.PortNum.NODEINFO_APP,
                                wantResponse=True, onResponse=self._on_nodeinfo_answer)
        return {"requested": "nodeinfo", "dest": dest, "asked": utc(time.time())}

    def _on_nodeinfo_answer(self, p):
        try:
            from meshtastic.protobuf import mesh_pb2
            d = p.get("decoded", {}) if isinstance(p, dict) else {}
            u = mesh_pb2.User()
            u.ParseFromString(d.get("payload") or b"")
            nid = p.get("fromId") or self._num_id(p.get("from")) or (u.id or None)
            if not nid:
                return
            nodes = getattr(self.interface, "nodes", None)
            if nodes is None:
                return
            rec = nodes.setdefault(nid, {})
            user = rec.setdefault("user", {})
            if u.long_name:
                user["longName"] = u.long_name
            if u.short_name:
                user["shortName"] = u.short_name
            if u.hw_model:
                user["hwModel"] = mesh_pb2.HardwareModel.Name(u.hw_model)
            self._emit("nodeinfo", id=nid, name=user.get("longName"), short=user.get("shortName"),
                       hw=user.get("hwModel"), ts=utc(time.time()))
        except Exception as e:  # noqa: BLE001
            self.logger.warning(f"a nodeinfo answer could not be read: {type(e).__name__}: {e}")

    # ---- Spec 041: waypoints across the bridge ---------------------------------------------------
    def _waypoint_note(self, fr, d):
        try:
            from meshtastic.protobuf import mesh_pb2
            wp = mesh_pb2.Waypoint()
            if d.get("payload"):
                wp.ParseFromString(d.get("payload"))
            else:
                w = d.get("waypoint") or {}
                wp.id = int(w.get("id") or 0); wp.latitude_i = int(w.get("latitudeI") or 0); wp.longitude_i = int(w.get("longitudeI") or 0)
                wp.expire = int(w.get("expire") or 0); wp.name = str(w.get("name") or ""); wp.description = str(w.get("description") or "")
            wid = int(wp.id)
            if not wid:
                return
            lat, lon = wp.latitude_i / 1e7, wp.longitude_i / 1e7
            now = int(time.time())
            gone = (wp.expire and int(wp.expire) < now) or (wp.latitude_i == 0 and wp.longitude_i == 0)
            rec = {"wid": wid, "node": fr, "name": wp.name or f"waypoint {wid}", "description": wp.description or "", "lat": round(lat, 6), "lon": round(lon, 6),
                   "expire": int(wp.expire) if wp.expire else None, "ts": utc(time.time())}
            if gone:
                self.waypoints.pop(wid, None)
            else:
                self.waypoints[wid] = rec
            h = getattr(self, "history", None)
            if h and h.ok:
                h.waypoint(fr, wid, name=rec["name"], description=rec["description"], lat=rec["lat"], lon=rec["lon"], expire=rec["expire"], gone=1 if gone else 0)
            self._emit("waypoint", gone=bool(gone), **rec)
            self._peer_share("waypoints", dict(rec, gone=bool(gone)))   # Spec 053
            if not gone:
                self._tak_waypoint(rec)
        except Exception as e:  # noqa: BLE001
            self.logger.warning(f"a waypoint could not be read: {type(e).__name__}: {e}")

    def _tak_waypoint(self, rec):
        """A Meshtastic waypoint as a TAK spot marker (b-m-p-s-m), on the socket the bridge forwards CoT on."""
        import datetime as _dt
        from xml.etree.ElementTree import Element, SubElement, tostring
        sock = getattr(self, "socket_client", None)
        if sock is None:
            return False
        now = _dt.datetime.utcnow()
        stale = _dt.datetime.utcfromtimestamp(rec["expire"]) if rec.get("expire") else now + _dt.timedelta(days=1)
        ev = Element("event", {"version": "2.0", "uid": f"MESH-WP-{rec['wid']}", "type": "b-m-p-s-m", "how": "h-g-i-g-o",
                               "time": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "start": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "stale": stale.strftime("%Y-%m-%dT%H:%M:%SZ")})
        SubElement(ev, "point", {"lat": str(rec["lat"]), "lon": str(rec["lon"]), "hae": "0.0", "ce": "9999999.0", "le": "9999999.0"})
        det = SubElement(ev, "detail")
        SubElement(det, "contact", {"callsign": str(rec.get("name") or "")})
        rem = SubElement(det, "remarks"); rem.text = str(rec.get("description") or "") + f" (via mesh, from {rec.get('node') or '?'})"
        SubElement(det, "color", {"argb": "-16776961"})
        try:
            sock.send(tostring(ev))
            return True
        except Exception as e:  # noqa: BLE001
            self.logger.warning(f"the waypoint did not reach TAK: {type(e).__name__}: {e}")
            return False

    def op_waypoints(self, **_):
        now = int(time.time())
        live = [w for w in self.waypoints.values() if not (w.get("expire") and w["expire"] < now)]
        with self._peers_lock:  # Spec 053: the peers' waypoints, each with its origin
            for bag in self.remote_waypoints.values():
                live.extend(dict(w) for w in bag.values() if not (w.get("expire") and w["expire"] < now))
        return {"waypoints": sorted(live, key=lambda w: w.get("ts") or "", reverse=True), "count": len(live)}

    def op_waypoint_send(self, name="", description="", lat=None, lon=None, expire_min=60, **_):
        import random
        name = str(name or "").strip()
        if not name or len(name.encode()) > 30:
            return {"error": "a waypoint name is 1 to 30 bytes"}
        description = str(description or "").strip()
        if len(description.encode()) > 100:
            return {"error": "a waypoint description is 100 bytes at most"}
        try:
            lat, lon = float(lat), float(lon)
        except (TypeError, ValueError):
            return {"error": "lat and lon are required, as decimal degrees"}
        if not (-90 <= lat <= 90 and -180 <= lon <= 180) or (lat == 0 and lon == 0):
            return {"error": "lat and lon must be a real position"}
        try:
            mins = max(1, min(int(expire_min or 60), 7 * 24 * 60))
        except (TypeError, ValueError):
            mins = 60
        expire = int(time.time()) + mins * 60
        wid = random.randint(1, 2**31 - 1)
        pkt = self.interface.sendWaypoint(name, description, 0, expire, waypoint_id=wid, latitude=lat, longitude=lon, wantAck=True)
        own = (self._own() or {}).get("id")
        rec = {"wid": wid, "node": own, "name": name, "description": description, "lat": round(lat, 6), "lon": round(lon, 6), "expire": expire, "ts": utc(time.time())}
        self.waypoints[wid] = rec
        h = getattr(self, "history", None)
        if h and h.ok:
            h.waypoint(own, wid, name=name, description=description, lat=rec["lat"], lon=rec["lon"], expire=expire, gone=0)
        self._emit("waypoint", gone=False, **rec)
        self._tak_waypoint(rec)
        return {"sent": True, "wid": wid, "name": name, "expire": expire, "id": getattr(pkt, "id", None), "asked": utc(time.time())}

    # ---- Spec 042: the mesh as a graph -----------------------------------------------------------
    def _neighbors_note(self, fr, d):
        try:
            from meshtastic.protobuf import mesh_pb2
            ni = mesh_pb2.NeighborInfo()
            if d.get("payload"):
                ni.ParseFromString(d.get("payload"))
            else:
                for n in (d.get("neighborinfo") or {}).get("neighbors") or []:
                    x = ni.neighbors.add(); x.node_id = int(n.get("nodeId") or 0); x.snr = float(n.get("snr") or 0)
            now = utc(time.time()); h = getattr(self, "history", None); n_edges = 0
            for n in ni.neighbors:
                if not n.node_id:
                    continue
                nid = f"!{int(n.node_id):08x}"
                snr = round(float(n.snr), 2) if n.snr is not None else None
                self.neighbor_edges[(fr, nid)] = {"snr": snr, "ts": now}
                if h and h.ok:
                    h.neighbor(fr, nid, snr=snr)
                n_edges += 1
            self._emit("neighbors", id=fr, edges=n_edges, ts=now)
        except Exception as e:  # noqa: BLE001
            self.logger.warning(f"a neighbour report could not be read: {type(e).__name__}: {e}")

    def op_neighbors(self, hours=24, **_):
        try:
            hours = max(1, min(int(hours or 24), 24 * 30))
        except (TypeError, ValueError):
            hours = 24
        since = utc(time.time() - hours * 3600)
        if not self.neighbor_edges:
            h = getattr(self, "history", None)
            for r in (h.query("neighbors", since=since, limit=5000) if h and h.ok else []):
                self.neighbor_edges[(r.get("node"), r.get("neighbor"))] = {"snr": r.get("snr"), "ts": r.get("ts")}
        names = {}
        for n in (self.mesh_nodes() if hasattr(self, "mesh_nodes") else []):
            names[n.get("id")] = n.get("name") or n.get("id")
        for nid, rec in (getattr(self.interface, "nodes", {}) or {}).items():
            if isinstance(rec, dict):
                ln = (rec.get("user") or {}).get("longName")
                if ln or nid not in names:
                    names[nid] = ln or names.get(nid) or nid
        own = self._own() or {}
        if own.get("id"):
            names[own["id"]] = own.get("name") or "this box"
        labels = {k: str(v.get("label") or "") for k, v in self._register_load().items()} if hasattr(self, "_register_load") else {}
        edges = []
        for (a, b), rec in self.neighbor_edges.items():
            if (rec.get("ts") or "") < since:
                continue
            edges.append({"from": a, "from_name": labels.get(a) or names.get(a) or a, "to": b, "to_name": labels.get(b) or names.get(b) or b, "snr": rec.get("snr"), "ts": rec.get("ts")})
        edges.sort(key=lambda x: x.get("ts") or "", reverse=True)
        return {"hours": hours, "edges": edges, "own": own.get("id"), "count": len(edges)}

    def _env_note(self, nid, em):
        """Spec 035: what a sensor node says about the air around it, kept and told."""
        if not nid or not em:
            return
        def f(k):
            v = em.get(k)
            try:
                return round(float(v), 2) if v is not None else None
            except (TypeError, ValueError):
                return None
        rec = {"temperature": f("temperature"), "humidity": f("relativeHumidity"), "pressure": f("barometricPressure"), "gas": f("gasResistance"),
               "lux": f("lux"), "iaq": f("iaq"), "wind_dir": f("windDirection"), "wind_speed": f("windSpeed")}
        if all(v is None for v in rec.values()):
            return
        h = getattr(self, "history", None)
        if h and h.ok:
            h.environment(nid, **rec)
        self._emit("environment", id=nid, ts=utc(time.time()), **rec)

    def _on_ack(self, p):
        """Spec 034: the radio's word on a sent message. NONE is delivered; anything else is the
        reason in the radio's own words. An id nobody sent is logged and dropped."""
        try:
            d = p.get("decoded", {}) if isinstance(p, dict) else {}
            pid = d.get("requestId")
            if pid is None:
                return
            pid = int(pid)
            rec = self.outbox.get(pid)
            if rec is None:
                self.logger.debug(f"an ack for packet {pid} that this bridge did not send")
                return
            why = str((d.get("routing") or {}).get("errorReason") or "NONE")
            ok = why == "NONE"
            rec["ack"] = "delivered" if ok else why
            h = getattr(self, "history", None)
            if h and h.ok:
                h.set_ack(pid, rec["ack"])
            self._emit("ack", request_id=pid, ok=ok, reason=None if ok else why, to=rec.get("to"), text=rec.get("text"))
        except Exception as e:  # noqa: BLE001
            self.logger.warning(f"an ack could not be read: {type(e).__name__}: {e}")

    def _on_telemetry_answer(self, p):
        try:
            from meshtastic.protobuf import telemetry_pb2
            d = p.get("decoded", {}) if isinstance(p, dict) else {}
            t = telemetry_pb2.Telemetry()
            t.ParseFromString(d.get("payload") or b"")
            nid = p.get("fromId") or self._num_id(p.get("from"))
            if t.HasField("device_metrics"):
                dm = t.device_metrics
                level = dm.battery_level if dm.HasField("battery_level") else None
                volts = dm.voltage if dm.HasField("voltage") else None
                self._battery_note(nid, level, volts)
            elif t.HasField("environment_metrics"):
                em = t.environment_metrics
                self._env_note(nid, {k: getattr(em, f) for k, f in (("temperature", "temperature"), ("relativeHumidity", "relative_humidity"),
                                                                    ("barometricPressure", "barometric_pressure"), ("gasResistance", "gas_resistance"),
                                                                    ("lux", "lux"), ("iaq", "iaq"), ("windDirection", "wind_direction"), ("windSpeed", "wind_speed"))
                                     if em.HasField(f)})
            else:
                self._emit("telemetry", id=nid, battery=None, charging=False, voltage=None, ts=utc(time.time()), note="answered without device metrics")
        except Exception as e:  # noqa: BLE001
            self.logger.warning(f"a telemetry answer could not be read: {type(e).__name__}: {e}")

    def _telemetry_pass(self, stagger=3.0):
        """Ask every node heard in the last day for its metrics; never the box itself."""
        own = (self._own() or {}).get("id")
        asked = []
        now = time.time()
        for nid, rec in list((getattr(self.interface, "nodes", None) or {}).items()):
            lh = rec.get("lastHeard") if isinstance(rec, dict) else None
            if nid == own or not lh or now - float(lh) > 86400:
                continue
            try:
                self.op_request_telemetry(dest=nid)
                asked.append(nid)
            except Exception as e:  # noqa: BLE001
                self.logger.debug(f"telemetry not asked of {nid}: {type(e).__name__}: {e}")
            if stagger:
                time.sleep(stagger)
        return asked

    def _telemetry_loop(self):
        try:
            secs = int(self.conf.get("TELEMETRY_ASK_SECS") or 0)
        except (TypeError, ValueError):
            secs = 0
        if secs <= 0:
            return
        if self._stop.wait(120):
            return
        while not self._stop.is_set():
            try:
                if getattr(self.interface, "nodes", None):
                    asked = self._telemetry_pass()
                    if asked:
                        self.logger.info(f"asked {len(asked)} node(s) for their batteries")
            except Exception as e:  # noqa: BLE001
                self.logger.warning(f"the telemetry pass failed: {type(e).__name__}: {e}")
            if self._stop.wait(max(60, secs)):
                return

    def op_nodes_forget_stale(self, days=7, **_):
        try:
            days = int(days or 7)
        except (TypeError, ValueError):
            return {"error": "days must be a number"}
        if days < 1:
            return {"error": "days must be at least 1"}
        own = (self._own() or {}).get("id")
        cutoff = time.time() - days * 86400
        forgotten, kept, errors = [], [], []
        for nid, rec in list((getattr(self.interface, "nodes", None) or {}).items()):
            if nid == own or not re.fullmatch(r"![0-9a-f]{8}", str(nid)):
                continue
            lh = rec.get("lastHeard") if isinstance(rec, dict) else None
            if lh and float(lh) >= cutoff:
                kept.append(nid)
                continue
            r = self.op_node_forget(id=nid, register="keep")
            if "forgotten" in r:
                forgotten.append(nid)
            else:
                errors.append(f"{nid}: {r.get('error')}")
        return {"forgotten": forgotten, "kept": kept, "errors": errors, "days": days,
                "note": f"{len(forgotten)} forgotten (not heard for {days} days), {len(kept)} kept; each comes back if heard again"}

    # ---- survey mode (Spec 022): ask one node for its position on an interval while the operator walks
    def op_survey_start(self, dest=None, interval=15, minutes=10, **_):
        dest = str(dest or "").strip()
        if not re.fullmatch(r"![0-9a-f]{8}", dest):
            return {"error": "dest must be a radio id, !hex"}
        try:
            interval = int(interval or 15); minutes = int(minutes or 10)
        except (TypeError, ValueError):
            return {"error": "interval and minutes must be numbers"}
        if not 5 <= interval <= 120:
            return {"error": "interval must be 5 to 120 seconds"}
        if not 1 <= minutes <= 120:
            return {"error": "minutes must be 1 to 120"}
        sv = getattr(self, "_survey", None)
        if sv and sv.get("running"):
            return {"error": f"a survey of {sv.get('dest')} is already running; stop it first"}
        now = time.time()
        self._survey = {"running": True, "dest": dest, "interval": interval, "minutes": minutes, "started": utc(now), "ends": utc(now + minutes * 60),
                        "asked": 0, "stop": threading.Event(), "_t0": now}
        self._emit("survey", state="started", dest=dest, interval=interval, minutes=minutes)
        threading.Thread(target=self._survey_loop, name="survey", daemon=True).start()
        return {"started": True, "dest": dest, "interval": interval, "minutes": minutes, "ends": self._survey["ends"]}

    def _survey_loop(self):
        sv = self._survey
        try:
            while not sv["stop"].is_set() and time.time() < sv["_t0"] + sv["minutes"] * 60:
                try:
                    self.op_request_position(dest=sv["dest"])
                    sv["asked"] += 1
                    self._emit("survey", state="asked", dest=sv["dest"], asked=sv["asked"])
                except Exception as e:  # noqa: BLE001
                    self.logger.warning(f"survey ask failed: {type(e).__name__}: {e}")
                if sv["stop"].wait(sv["interval"]):
                    break
        finally:
            sv["running"] = False
            sv["ended"] = utc(time.time())
            self._emit("survey", state="ended", dest=sv["dest"], asked=sv["asked"], answers=self._survey_answers(sv))

    def _survey_answers(self, sv):
        h = getattr(self, "history", None)
        if not h or not h.ok:
            return None
        return len(h.query("positions", node=sv["dest"], since=sv["started"], limit=5000))

    def op_survey_stop(self, **_):
        sv = getattr(self, "_survey", None)
        if not sv or not sv.get("running"):
            return {"stopped": False, "note": "no survey is running"}
        sv["stop"].set()
        return {"stopped": True, "dest": sv.get("dest"), "asked": sv.get("asked")}

    def op_survey_status(self, **_):
        sv = getattr(self, "_survey", None)
        if not sv:
            return {"running": False}
        return {"running": bool(sv.get("running")), "dest": sv.get("dest"), "interval": sv.get("interval"), "minutes": sv.get("minutes"),
                "started": sv.get("started"), "ends": sv.get("ends"), "ended": sv.get("ended"), "asked": sv.get("asked"), "answers": self._survey_answers(sv)}

    def _own_chutil(self):
        try:
            own = (self._own() or {}).get("id")
            dm = ((getattr(self.interface, "nodes", {}) or {}).get(own) or {}).get("deviceMetrics") or {}
            v = dm.get("channelUtilization")
            if v is None:
                h = getattr(self, "history", None)
                if h and h.ok and own:
                    rows = h.query("telemetry", node=own, limit=1)
                    v = rows[-1].get("chutil") if rows else None
            return round(float(v), 1) if v is not None else None
        except Exception:  # noqa: BLE001
            return None

    # ---- config drift (Spec 028) --------------------------------------------------------------
    PROFILE_FIELDS = ("role", "tx_power", "position_broadcast_secs", "region", "modem_preset")

    @staticmethod
    def _snap_fields(snap):
        if not isinstance(snap, dict) or snap.get("error"):
            return None
        ch0 = next((c.get("name") for c in (snap.get("channels") or []) if int(c.get("index", -1)) == 0), None)
        return {"role": snap.get("role"), "tx_power": snap.get("tx_power"), "position_broadcast_secs": snap.get("position_broadcast_secs"),
                "region": snap.get("region"), "modem_preset": snap.get("modem_preset"), "channel0": ch0, "read_at": snap.get("read_at") or utc(time.time())}

    def _profile_path(self):
        return os.path.join(self.state_dir, "profile.json")

    def op_profile(self, **_):
        try:
            d = json.load(open(self._profile_path()))
        except (OSError, ValueError):
            d = {}
        return {k: d.get(k) for k in self.PROFILE_FIELDS}

    def op_profile_set(self, role=None, tx_power=None, position_broadcast_secs=None, region=None, modem_preset=None, **_):
        prof = self.op_profile()
        def clean(v):
            return None if v is None or str(v).strip() == "" or str(v).strip().lower() in ("none", "any") else v
        try:
            if clean(tx_power) is not None:
                v = int(tx_power)
                if not 0 <= v <= 30: return {"error": "tx_power must be 0 to 30 dBm"}
                prof["tx_power"] = v
            elif tx_power is not None: prof["tx_power"] = None
            if clean(position_broadcast_secs) is not None:
                v = int(position_broadcast_secs)
                if not 32 <= v <= 86400: return {"error": "position_broadcast_secs must be 32 to 86400"}
                prof["position_broadcast_secs"] = v
            elif position_broadcast_secs is not None: prof["position_broadcast_secs"] = None
        except (TypeError, ValueError):
            return {"error": "power and interval must be numbers"}
        for k, v in (("role", role), ("region", region), ("modem_preset", modem_preset)):
            if clean(v) is not None:
                prof[k] = str(v).strip().upper()
            elif v is not None:
                prof[k] = None
        try:
            os.makedirs(self.state_dir, exist_ok=True)
            tmp = self._profile_path() + ".tmp"
            with open(tmp, "w") as f:
                json.dump(prof, f)
            os.replace(tmp, self._profile_path())
        except Exception as e:  # noqa: BLE001
            return {"error": f"profile not written: {type(e).__name__}: {e}"}
        self._emit("node", id=None, action="profile_set")
        return {"written": prof, "confirmed": True}

    def _drift_of(self, prof, snap):
        diffs = []
        for k in self.PROFILE_FIELDS:
            want = prof.get(k)
            if want is None:
                continue
            have = (snap or {}).get(k)
            if have is None:
                continue
            if str(have).strip().upper() != str(want).strip().upper():
                diffs.append({"field": k, "is": have, "should": want})
        return diffs

    def op_drift(self, **_):
        prof = self.op_profile()
        reg = self._register_load()
        enforced = [k for k in self.PROFILE_FIELDS if prof.get(k) is not None]
        rows, counts = [], {"in_line": 0, "drifted": 0, "unread": 0}
        for nid, entry in sorted(reg.items(), key=lambda kv: str(kv[1].get("label") or kv[1].get("name") or kv[0]).lower()):
            name = str(entry.get("label") or entry.get("name") or nid)
            snap = entry.get("snapshot")
            if not snap:
                rows.append({"id": nid, "name": name, "state": "unread", "diffs": [], "managed": bool(entry.get("managed"))}); counts["unread"] += 1; continue
            diffs = self._drift_of(prof, snap)
            state = "drifted" if diffs else "in line"
            counts["drifted" if diffs else "in_line"] += 1
            rows.append({"id": nid, "name": name, "state": state, "diffs": diffs, "read_at": snap.get("read_at"), "managed": bool(entry.get("managed"))})
        return {"profile": prof, "enforced": enforced, "devices": rows, "counts": counts}

    def op_drift_fix(self, id=None, scope="safe", confirm=None, **_):
        nid = str(id or "").strip()
        reg = self._register_load()
        if nid not in reg:
            return {"error": f"{nid} is not in the register"}
        entry = reg[nid]
        if not entry.get("managed"):
            return {"error": f"{nid} is not managed: bring it to the bench first"}
        prof = self.op_profile()
        diffs = self._drift_of(prof, entry.get("snapshot") or {})
        if not diffs:
            return {"nothing": True, "note": f"{nid} is in line with the profile"}
        safe = {d["field"]: d["should"] for d in diffs if d["field"] in ("tx_power", "position_broadcast_secs")}
        hard = {d["field"]: d["should"] for d in diffs if d["field"] in ("region", "modem_preset", "role")}
        out = {"id": nid, "safe": None, "hard": None, "skipped": []}
        if safe:
            out["safe"] = self.op_node_set(id=nid, **safe)
            self._snapshot_from_readback(nid, (out["safe"] or {}).get("read_back"))
        if hard:
            if scope != "all":
                out["skipped"] = sorted(hard)
                out["note"] = "region, preset or role also differ; a fix with scope all and the confirm naming the device changes them (the device moves band)"
            else:
                need = self._confirm_device(confirm, nid, "Changing a device's region, preset or role moves it to another band.")
                if need:
                    return need
                out["hard"] = self.op_node_set_region(id=nid, confirm=confirm, **hard)
                self._snapshot_from_readback(nid, (out["hard"] or {}).get("read_back"))
        out["confirmed"] = all(bool((x or {}).get("confirmed")) for x in (out["safe"], out["hard"]) if x is not None)
        return out

    def _snapshot_from_readback(self, nid, rb):
        if not isinstance(rb, dict):
            return
        reg = self._register_load()
        entry = reg.get(nid)
        if not entry:
            return
        snap = dict(entry.get("snapshot") or {})
        for k in self.PROFILE_FIELDS:
            if rb.get(k) is not None:
                snap[k] = rb[k]
        snap["read_at"] = utc(time.time())
        entry["snapshot"] = snap
        self._register_save(reg)

    # ---- the key rotation checklist (Spec 027) ----------------------------------------------
    def _rotation_path(self):
        return os.path.join(self.state_dir, "rotation.json")

    def _rotation_load(self):
        try:
            return json.load(open(self._rotation_path()))
        except (OSError, ValueError):
            return None

    def _rotation_mark(self, index, name=None, source="screen", note=None):
        """Record a rotation and the devices expected back: the register plus anyone heard here in the last seven days."""
        own = (self._own() or {}).get("id")
        reg = self._register_load() if hasattr(self, "_register_load") else {}
        expected = {}
        for nid, r in reg.items():
            expected[nid] = str(r.get("label") or r.get("name") or nid)
        h = getattr(self, "history", None)
        if h and h.ok:
            for r in h.query("packets", since=utc(time.time() - 7 * 86400), limit=5000):
                nid = r.get("node")
                if nid and nid != own and re.fullmatch(r"![0-9a-f]{8}", str(nid)):
                    expected.setdefault(nid, nid)
        for n in self.mesh_nodes():
            nid = n.get("id")
            if nid and nid != own and n.get("heard_here", True):
                expected.setdefault(nid, str(n.get("name") or nid))
        for nid in list(expected):
            db = ((getattr(self.interface, "nodes", {}) or {}).get(nid) or {}).get("user") or {}
            if expected[nid] == nid and db.get("longName"):
                expected[nid] = db["longName"]
        expected.pop(own, None)
        rec = {"ts": utc(time.time()), "index": int(index), "name": name, "source": source, "note": note, "expected": expected}
        try:
            os.makedirs(self.state_dir, exist_ok=True)
            tmp = self._rotation_path() + ".tmp"
            with open(tmp, "w") as f:
                json.dump(rec, f)
            os.replace(tmp, self._rotation_path())
        except Exception as e:  # noqa: BLE001
            self.logger.warning(f"rotation.json not written: {type(e).__name__}: {e}")
        self._emit("rotation", state="marked", index=int(index), name=name, expected=len(expected))
        return rec

    def op_rotation_mark(self, index=0, note=None, **_):
        try:
            index = int(index if index is not None and index != "" else 0)
        except (TypeError, ValueError):
            return {"error": "index must be a number"}
        if not 0 <= index <= 7:
            return {"error": "index must be a slot, 0 to 7"}
        name = None
        try:
            chans = list(self.interface.localNode.channels or [])
            name = chans[index].settings.name or None
        except Exception:  # noqa: BLE001
            name = None
        rec = self._rotation_mark(index, name=name, source="marked by hand", note=note)
        return {"marked": rec["ts"], "index": index, "name": name, "expected": len(rec["expected"]), "confirmed": True}

    def op_rotation_status(self, **_):
        rec = self._rotation_load()
        if not rec:
            return {"rotation": None, "back": [], "waiting": [], "note": "no rotation marked on this box"}
        ts = rec["ts"]
        first = {}
        h = getattr(self, "history", None)
        if h and h.ok:
            for r in h.query("packets", since=ts, limit=5000):
                nid = r.get("node")
                if nid and nid not in first:
                    first[nid] = r["ts"]
        for n in self.mesh_nodes():
            nid = n.get("id"); hd = n.get("heard")
            if nid and hd and hd >= ts and nid not in first:
                first[nid] = hd
        back, waiting = [], []
        for nid, name in sorted((rec.get("expected") or {}).items(), key=lambda kv: kv[1].lower()):
            if nid in first:
                back.append({"id": nid, "name": name, "heard": first[nid]})
            else:
                waiting.append({"id": nid, "name": name})
        return {"rotation": {k: rec.get(k) for k in ("ts", "index", "name", "source", "note")}, "back": back, "waiting": waiting,
                "counts": {"expected": len(rec.get("expected") or {}), "back": len(back), "waiting": len(waiting)}}

    # ---- alerts (Spec 026) ----------------------------------------------------------------
    ALERT_DEFAULTS = {"silent_min": 30, "battery_pct": 20, "unknown": True, "fence_m": 0, "to_tak": True}

    def _alerts_path(self):
        return os.path.join(self.state_dir, "alerts.json")

    def _alerts_load(self):
        try:
            d = json.load(open(self._alerts_path()))
        except (OSError, ValueError):
            d = {}
        st = dict(self.ALERT_DEFAULTS); st.update({k: v for k, v in (d.get("settings") or {}).items() if k in self.ALERT_DEFAULTS})
        return {"settings": st, "open": d.get("open") or {}, "alerted_unknown": d.get("alerted_unknown") or [], "fence_state": d.get("fence_state") or {}}

    # ---- geofences (Spec 045): drawn areas, kept on the box; crossings become alerts
    def _fences_path(self):
        return os.path.join(self.state_dir, "fences.json")

    def _fences_load(self):
        try:
            d = json.load(open(self._fences_path()))
            return [f for f in d if isinstance(f, dict) and f.get("id")]
        except (OSError, ValueError, TypeError):
            return []

    def _fences_save(self, fences):
        os.makedirs(self.state_dir, exist_ok=True)
        tmp = self._fences_path() + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(fences, fh, indent=1)
        os.replace(tmp, self._fences_path())

    def op_fences(self, **_):
        return {"fences": self._fences_load()}

    def op_fence_set(self, id=None, name=None, kind=None, points=None, lat=None, lon=None, radius_m=None, rule=None, group=None, enabled=None, **_):
        fences = self._fences_load()
        fid = str(id or "").strip()
        cur = next((f for f in fences if f["id"] == fid), None) if fid else None
        if fid and cur is None:
            return {"error": f"no fence {fid}"}
        f = dict(cur or {})
        if name is not None or not f.get("name"):
            nm = str(name or "").strip()[:40]
            if not nm:
                return {"error": "name is required"}
            f["name"] = nm
        kind = str(kind or f.get("kind") or "polygon")
        if kind not in ("polygon", "circle"):
            return {"error": "kind must be polygon or circle"}
        f["kind"] = kind
        rule = str(rule or f.get("rule") or "both")
        if rule not in ("enter", "leave", "both"):
            return {"error": "rule must be enter, leave or both"}
        f["rule"] = rule
        if kind == "polygon" and (points is not None or not f.get("points")):
            try:
                pts = json.loads(points) if isinstance(points, str) else (points or [])
                pts = [[float(p[0]), float(p[1])] for p in pts]
            except (TypeError, ValueError, IndexError):
                return {"error": "points must be a list of [lat, lon] pairs"}
            if len(pts) < 3:
                return {"error": "a fence needs at least three points"}
            if len(pts) > 200:
                return {"error": "two hundred points at most"}
            if any(not (-90 <= p[0] <= 90 and -180 <= p[1] <= 180) for p in pts):
                return {"error": "a point is off the earth"}
            f["points"] = pts; f.pop("centre", None); f.pop("radius_m", None)
        if kind == "circle" and (lat is not None or lon is not None or radius_m is not None or not f.get("centre")):
            try:
                c = [float(str(lat).strip()), float(str(lon).strip())]; r = float(radius_m)
            except (TypeError, ValueError):
                return {"error": "a circle needs lat, lon and radius_m"}
            if not (10 <= r <= 100000):
                return {"error": "radius_m must be 10 to 100000 metres"}
            f["centre"] = c; f["radius_m"] = r; f.pop("points", None)
        if group is not None:
            f["group"] = str(group).strip()[:40]
        if enabled is not None:
            f["enabled"] = str(enabled).lower() not in ("off", "no", "false", "0")
        f.setdefault("enabled", True)
        if cur is None:
            import secrets as _secrets
            f["id"] = _secrets.token_hex(4); f["created"] = utc(time.time())
            fences.append(f)
        else:
            fences[fences.index(cur)] = f
        f["updated"] = utc(time.time())
        self._fences_save(fences)
        self._emit("fence", id=f["id"])
        return {"id": f["id"], "fence": {k: v for k, v in f.items()}, "confirmed": True}

    def op_fence_delete(self, id=None, **_):
        fid = str(id or "").strip()
        fences = self._fences_load()
        keep = [f for f in fences if f["id"] != fid]
        if len(keep) == len(fences):
            return {"error": f"no fence {fid}"}
        self._fences_save(keep)
        a = self._alerts_load()
        a["fence_state"] = {k: v for k, v in a.get("fence_state", {}).items() if not k.startswith(fid + ":")}
        self._alerts_save(a)
        self._emit("fence", id=fid, removed=True)
        return {"removed": fid, "confirmed": True}

    def _alerts_save(self, a):
        try:
            os.makedirs(self.state_dir, exist_ok=True)
            tmp = self._alerts_path() + ".tmp"
            with open(tmp, "w") as f:
                json.dump(a, f)
            os.replace(tmp, self._alerts_path())
        except Exception as e:  # noqa: BLE001
            self.logger.debug(f"alerts.json not written: {type(e).__name__}: {e}")

    def op_alert_settings(self, **_):
        st = dict(self._alerts_load()["settings"])
        if self.box_mode in ("server", "hub"):
            st["to_tak"] = False  # Spec 050: no TAK on this box, whatever the file says
        return st

    def op_alert_set(self, silent_min=None, battery_pct=None, unknown=None, fence_m=None, to_tak=None, **_):
        if self.box_mode in ("server", "hub") and to_tak is not None and to_tak != "" and str(to_tak).strip().lower() in ("1", "true", "on", "yes"):
            return {"error": "TAK is off on this box (MODE=server): alerts stay on this screen"}
        a = self._alerts_load(); st = a["settings"]
        try:
            if silent_min is not None and silent_min != "":
                v = int(silent_min)
                if not 1 <= v <= 1440: return {"error": "silent_min must be 1 to 1440 minutes"}
                st["silent_min"] = v
            if battery_pct is not None and battery_pct != "":
                v = int(battery_pct)
                if not 1 <= v <= 90: return {"error": "battery_pct must be 1 to 90"}
                st["battery_pct"] = v
            if fence_m is not None and fence_m != "":
                v = int(fence_m)
                if not 0 <= v <= 100000: return {"error": "fence_m must be 0 (off) to 100000 metres"}
                st["fence_m"] = v
        except (TypeError, ValueError):
            return {"error": "thresholds must be numbers"}
        for k, v in (("unknown", unknown), ("to_tak", to_tak)):
            if v is not None and v != "":
                st[k] = str(v).strip().lower() in ("1", "true", "on", "yes")
        self._alerts_save(a)
        self._emit("alert", state="settings", settings=st)
        return {"written": st, "confirmed": True}

    def _tak_chat(self, text, callsign="Mesh Manager"):
        """One GeoChat to All Chat Rooms on the TAK Server, on the socket the bridge forwards CoT on."""
        if self.box_mode in ("server", "hub"):
            return False  # Spec 050 and 052: no TAK Server to chat to
        import datetime as _dt, uuid as _uuid
        from xml.etree.ElementTree import Element, SubElement, tostring
        sock = getattr(self, "socket_client", None)
        if sock is None:
            return False
        own = self.own_position() or {}
        now = _dt.datetime.utcnow(); room = "All Chat Rooms"; sender = "MESH-MANAGER-" + str((self._own() or {}).get("id") or "box").strip("!")
        mid = str(_uuid.uuid4())
        ev = Element("event", {"how": "h-g-i-g-o", "type": "b-t-f", "version": "2.0", "uid": f"GeoChat.{sender}.{room}.{mid}",
                               "start": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "time": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                               "stale": (now + _dt.timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")})
        SubElement(ev, "point", {"ce": "9999999.0", "le": "9999999.0", "hae": "0.0", "lat": str(own.get("lat") or 0.0), "lon": str(own.get("lon") or 0.0)})
        det = SubElement(ev, "detail")
        chat = SubElement(det, "__chat", {"chatroom": room, "groupOwner": "false", "id": room, "messageId": mid, "parent": "RootContactGroup", "senderCallsign": callsign})
        SubElement(chat, "chatgrp", {"id": room, "uid0": sender, "uid1": room})
        SubElement(det, "link", {"relation": "p-p", "type": "a-f-G-U-C", "uid": sender})
        rem = SubElement(det, "remarks", {"source": f"BAO.F.ATAK.{sender}", "time": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "to": room})
        rem.text = str(text)
        try:
            sock.send(tostring(ev))
            return True
        except Exception as e:  # noqa: BLE001
            self.logger.warning(f"the alert did not reach TAK: {type(e).__name__}: {e}")
            return False

    def _raise_alert(self, a, node, kind, text):
        key = f"{node}:{kind}"
        if key in a["open"]:
            return False
        a["open"][key] = {"node": node, "kind": kind, "text": text, "since": utc(time.time())}
        h = getattr(self, "history", None)
        if h and h.ok:
            h.alert(node, kind, text)
        sent = self._tak_chat(f"[Mesh Manager] {text}") if a["settings"].get("to_tak") else False
        self._emit("alert", state="open", node=node, what=kind, text=text, tak=bool(sent))
        self._peer_share("alerts", {"state": "open", "node": node, "kind": kind, "text": text, "since": a["open"][key]["since"]})   # Spec 053
        self.logger.warning(f"alert: {text}" + (" (told TAK)" if sent else ""))
        return True

    def _clear_alert(self, a, node, kind):
        key = f"{node}:{kind}"
        if key not in a["open"]:
            return False
        a["open"].pop(key, None)
        h = getattr(self, "history", None)
        if h and h.ok:
            h.alert_clear(node, kind)
        self._emit("alert", state="cleared", node=node, what=kind)
        self._peer_share("alerts", {"state": "cleared", "node": node, "kind": kind})   # Spec 053
        return True

    def _judge_alerts(self):
        """One pass over the conditions; returns the number raised."""
        a = self._alerts_load(); st = a["settings"]; raised = 0
        reg = self._register_load() if hasattr(self, "_register_load") else {}
        registered = {k for k, v in reg.items() if v.get("label") or v.get("holder") or v.get("managed")}
        own = (self._own() or {}).get("id")
        ownpos = self.own_position() or {}
        now = time.time()
        for n in self.mesh_nodes():
            nid = n.get("id")
            if not nid or nid == own or not n.get("heard_here", True):
                continue
            name = str(reg.get(nid, {}).get("label") or n.get("name") or nid)
            heard = n.get("heard")
            # silent
            if nid in registered and heard:
                try:
                    import calendar as _cal
                    age_min = (now - _cal.timegm(time.strptime(heard, "%Y-%m-%dT%H:%M:%SZ"))) / 60
                except Exception:  # noqa: BLE001
                    age_min = 0
                if age_min > st["silent_min"]:
                    raised += self._raise_alert(a, nid, "silent", f"{name} silent for {int(age_min)} min (last heard {heard[11:16]}Z)")
                else:
                    self._clear_alert(a, nid, "silent")
            # battery
            b = (self.batteries.get(nid) or {}).get("level")
            if b is not None:
                if 0 <= int(b) < st["battery_pct"]:
                    raised += self._raise_alert(a, nid, "battery", f"{name} battery {int(b)}%")
                elif int(b) > 100 or int(b) >= st["battery_pct"] + 5:
                    self._clear_alert(a, nid, "battery")
            # unknown
            if st.get("unknown") and nid not in reg and nid not in a["alerted_unknown"]:
                a["alerted_unknown"].append(nid)
                raised += self._raise_alert(a, nid, "unknown", f"unknown node {name} ({nid}) heard on the channel, {n.get('hw') or 'hardware unknown'}")
            # fence
            if st.get("fence_m") and ownpos.get("lat") is not None and n.get("lat") is not None and n.get("lon") is not None:
                d = _haversine(ownpos["lat"], ownpos["lon"], float(n["lat"]), float(n["lon"]))
                if d > st["fence_m"]:
                    raised += self._raise_alert(a, nid, "fence", f"{name} is {int(d)} m from the box, outside the {st['fence_m']} m fence")
                else:
                    self._clear_alert(a, nid, "fence")
        # Spec 045: crossings of the drawn fences, from where every node is now
        fences = [f for f in self._fences_load() if f.get("enabled", True)]
        if fences:
            try:
                rows = [dict(n, name=str(reg.get(n.get("id"), {}).get("label") or n.get("name") or n.get("id"))) for n in self.op_nodes().get("nodes", []) if n.get("id") != own]
                events, a["fence_state"] = fence_transitions(fences, a.get("fence_state") or {}, rows)
                for ev in events:
                    self._clear_alert(a, ev["node"], "geofence")
                    raised += self._raise_alert(a, ev["node"], "geofence", f"{ev['name']} {'entered' if ev['kind'] == 'enter' else 'left'} {ev['fence_name']}")
            except Exception as ex:  # noqa: BLE001
                self.logger.warning(f"the fence check failed: {type(ex).__name__}: {ex}")
        # Spec 043: a public key that is not the one on file, until the operator accepts it
        for nid, r in reg.items():
            if not isinstance(r, dict):
                continue
            if r.get("key_changed") and str(r["key_changed"]) > str(r.get("key_ack") or ""):
                nm = str(r.get("label") or r.get("name") or nid)
                raised += self._raise_alert(a, nid, "key", f"{nm} public key changed {str(r['key_changed'])[:16]}Z; if the radio was not reflashed, treat it as an impostor")
            else:
                self._clear_alert(a, nid, "key")
        self._alerts_save(a)
        return raised

    def _alert_loop(self):
        if self._stop.wait(120):
            return
        while not self._stop.is_set():
            try:
                self._judge_alerts()
            except Exception as e:  # noqa: BLE001
                self.logger.warning(f"the alert pass failed: {type(e).__name__}: {e}")
            if self._stop.wait(60):
                return

    def op_alerts(self, limit=50, **_):
        a = self._alerts_load()
        h = getattr(self, "history", None)
        recent = h.query("alerts", limit=int(limit or 50)) if h and h.ok else []
        open_ = list(a["open"].values())
        with self._peers_lock:  # Spec 053: the peers' open alerts, each with its origin
            for bag in self.remote_alerts.values():
                open_.extend(dict(o) for o in bag.values())
        return {"open": sorted(open_, key=lambda x: x.get("since") or "", reverse=True), "recent": recent, "settings": a["settings"]}

    def op_alert_test(self, **_):
        if self.box_mode in ("server", "hub"):
            self._emit("alert", state="test", tak=False)
            return {"sent": False, "observe": bool(self.observe), "mode": "server", "note": "TAK is off on this box (MODE=server)"}
        sent = self._tak_chat("[Mesh Manager] test alert: the box can reach TAK chat")
        self._emit("alert", state="test", tak=bool(sent))
        return {"sent": bool(sent), "observe": bool(self.observe), "note": "counted, not sent" if self.observe else ("a GeoChat to All Chat Rooms" if sent else "the TAK socket is not open")}

    # ---- mesh health (Spec 024)
    @staticmethod
    def _verdict(chutil):
        if chutil is None:
            return "unknown"
        c = float(chutil)
        return "quiet" if c < 10 else "normal" if c < 25 else "busy" if c < 40 else "saturated"

    def op_health(self, hours=24, **_):
        try:
            hours = 24 if hours is None or hours == "" else int(hours)
        except (TypeError, ValueError):
            return {"error": "hours must be a number"}
        if not 1 <= hours <= 168:
            return {"error": "hours must be 1 to 168"}
        h = getattr(self, "history", None)
        if not h or not h.ok:
            return {"error": "the history store is not available on this box"}
        now = time.time()
        since = utc(now - hours * 3600)
        packets = h.query("packets", since=since, limit=5000)
        telem = h.query("telemetry", since=since, limit=5000)
        own = (self._own() or {}).get("id")
        per = {}
        for r in packets:
            n = r.get("node")
            if not n:
                continue
            d = per.setdefault(n, {"id": n, "packets": 0, "chutil": None, "airutil": None, "battery": None, "last_telemetry": None})
            d["packets"] += 1
        for r in telem:
            n = r.get("node")
            if not n:
                continue
            d = per.setdefault(n, {"id": n, "packets": 0, "chutil": None, "airutil": None, "battery": None, "last_telemetry": None})
            d["chutil"], d["airutil"], d["battery"], d["last_telemetry"] = r.get("chutil"), r.get("airutil"), r.get("level"), r.get("ts")
        labels = {k: str(v.get("label") or "") for k, v in self._register_load().items()} if hasattr(self, "_register_load") else {}
        db = getattr(self.interface, "nodes", None) or {}
        for n, d in per.items():
            rec = db.get(n) if isinstance(db, dict) else None
            d["name"] = labels.get(n) or ((rec or {}).get("user") or {}).get("longName") or n
            d["per_hour"] = round(d["packets"] / hours, 1)
            d["own"] = (n == own)
        nodes = sorted(per.values(), key=lambda d: -d["packets"])
        # the gateway's own figures: its telemetry rows first, the library's record as the fallback
        own_rows = [r for r in telem if r.get("node") == own]
        own_ch = own_air = None
        if own_rows:
            own_ch, own_air = own_rows[-1].get("chutil"), own_rows[-1].get("airutil")
        else:
            dm = ((db.get(own) if isinstance(db, dict) and own else None) or {}).get("deviceMetrics") or {}
            own_ch, own_air = dm.get("channelUtilization"), dm.get("airUtilTx")
        # hourly means of the gateway's channel utilisation for the chart
        buckets = {}
        for r in own_rows:
            key = r["ts"][:13]
            buckets.setdefault(key, []).append(float(r.get("chutil") or 0))
        hourly = [{"hour": k + ":00Z", "chutil": round(sum(v) / len(v), 1)} for k, v in sorted(buckets.items())]
        region = None
        try:
            lora = self._lora()
            region = region_name(lora.region) if lora is not None else None
        except Exception:  # noqa: BLE001
            region = None
        budget = 10.0 if region == "EU_868" else None
        return {"hours": hours, "since": since, "region": region, "budget_pct": budget,
                "chutil": own_ch, "airutil": own_air, "verdict": self._verdict(own_ch),
                "air_share": (round(float(own_air) / budget * 100, 1) if (own_air is not None and budget) else None),
                "packets": len(packets), "packets_per_hour": round(len(packets) / hours, 1),
                "nodes_heard": len([d for d in nodes if not d["own"]]), "nodes": nodes, "hourly": hourly}

    def op_node_forget(self, id=None, register="keep", **_):
        nid = str(id or "").strip()
        if not re.fullmatch(r"![0-9a-f]{8}", nid):
            return {"error": "id must be a radio id, !hex"}
        try:
            self.interface.localNode.removeNode(nid)
        except Exception as e:  # noqa: BLE001
            return {"error": f"the radio could not be asked to forget {nid}: {type(e).__name__}: {e}"}
        for key in [k for k, d in list(getattr(self, "meshtastic_devices", {}).items()) if k == nid or str((d or {}).get("meshtastic_id") or "") == nid]:
            self.meshtastic_devices.pop(key, None)
        for store in (getattr(self, "_mesh_radio", {}), self.links, self.direct, self.routes):
            store.pop(nid, None)
        try:
            nodes = getattr(self.interface, "nodes", None)
            if isinstance(nodes, dict):
                nodes.pop(nid, None)
            by_num = getattr(self.interface, "nodesByNum", None)
            if isinstance(by_num, dict):
                by_num.pop(int(nid[1:], 16), None)
        except Exception:  # noqa: BLE001
            pass
        dropped = False
        if str(register or "keep") == "drop":
            reg = self._register_load()
            if reg.pop(nid, None) is not None:
                self._register_save(reg)
                dropped = True
        self._emit("register", id=nid)
        self._emit("node", id=nid, action="node_forget")
        return {"forgotten": nid, "register": "dropped" if dropped else "kept", "note": "removed from this radio's database and the box's lists; it comes back if it is heard again"}

    # ---- writes to this radio (Spec 006): every one read back, the risky ones confirmed ---------
    def _enum_value(self, kind, name):
        try:
            from meshtastic.protobuf import config_pb2
            table = {"region": config_pb2.Config.LoRaConfig.RegionCode, "modem_preset": config_pb2.Config.LoRaConfig.ModemPreset,
                     "role": config_pb2.Config.DeviceConfig.Role}[kind]
            return table.Value(name)
        except Exception:  # noqa: BLE001
            inv = {"region": {v: k for k, v in _REG.items()} if "_REG" in globals() else {},
                   "modem_preset": {v: k for k, v in _PRE.items()} if "_PRE" in globals() else {},
                   "role": {v: k for k, v in _ROLE.items()} if "_ROLE" in globals() else {}}[kind]
            if name not in inv:
                raise ValueError(f"unknown {kind} {name}")
            return inv[name]

    def _confirm_needed(self, confirm, why):
        own = self._own().get("id") or "?"
        if confirm != own:
            return {"error": f"{why} Confirm by naming this radio ({own}) in 'confirm'.", "needs_confirm": own}
        return None

    READBACK_S = 30

    def _admin_query(self, build, extract, timeout=None, node=None):
        """One admin round trip to this radio: build(p) fills the request, extract(raw) reads the
        answer. Returns what the RADIO answered, never the library's cache (which is what the
        bridge just wrote). Raises TimeoutError when the radio does not answer in time."""
        from meshtastic.protobuf import admin_pb2
        node = node or self.interface.localNode
        done = threading.Event()
        out = {}

        def on_resp(p):
            try:
                out["value"] = extract(p["decoded"]["admin"]["raw"])
            except Exception as e:  # noqa: BLE001
                out["error"] = f"{type(e).__name__}: {e}"
            done.set()
        p = admin_pb2.AdminMessage()
        build(p)
        node._sendAdmin(p, wantResponse=True, onResponse=on_resp)
        if not done.wait(timeout or self.READBACK_S):
            raise TimeoutError(f"no answer from the radio in {timeout or self.READBACK_S} s")
        if "error" in out:
            raise RuntimeError(out["error"])
        self.config_read_at = time.time()   # every admin answer is a fresh read from the radio
        return out["value"]

    def _read_channel(self, index, node=None):
        """The channel in a slot, as the radio holds it now."""
        def build(p): p.get_channel_request = int(index) + 1          # 1-based on the wire
        def extract(raw):
            c = raw.get_channel_response
            return {"index": int(c.index), "name": c.settings.name or "", "role": ch_role_name(c.role),
                    "has_key": bool(c.settings.psk), "_psk": bytes(c.settings.psk)}
        return self._admin_query(build, extract, node=node)

    def _read_section(self, section, node=None):
        """One config section (lora, device, position ...) as the radio holds it now."""
        from meshtastic.protobuf import admin_pb2
        def build(p): p.get_config_request = admin_pb2.AdminMessage.ConfigType.Value(section.upper() + "_CONFIG")
        def extract(raw): return getattr(raw.get_config_response, section)
        return self._admin_query(build, extract, node=node)

    def _read_owner(self, node=None):
        def build(p): p.get_owner_request = True
        def extract(raw):
            u = raw.get_owner_response
            return {"long_name": u.long_name, "short_name": u.short_name}
        got = self._admin_query(build, extract, node=node)
        if node is None:
            self.owner_seen = dict(got)   # the radio's own word, kept: the library's cache lags until the next NodeInfo
        return got

    def _readback(self, fn):
        """Run a read-back from the radio; it may never answer. Returns (arrived, sent_ts, why, value)."""
        sent = utc(time.time())
        try:
            return True, sent, None, fn()
        except Exception as e:  # noqa: BLE001
            return False, sent, f"{type(e).__name__}: {e}", None

    def _adopt_slot(self, index, row):
        """Keep the library's channel cache honest for the slot the radio just answered for."""
        try:
            ch = self.interface.localNode.channels[index]
            ch.settings.name = row["name"]
            ch.settings.psk = row["_psk"]
            ch.role = {"DISABLED": 0, "PRIMARY": 1, "SECONDARY": 2}.get(row["role"], ch.role)
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def _public(row):
        return {k: v for k, v in (row or {}).items() if not k.startswith("_")}

    def _write_reply(self, written, read_back, arrived, sent, why, confirmed):
        r = {"written": written, "read_back": read_back, "confirmed": bool(arrived and confirmed), "sent": sent}
        if not arrived:
            r["unconfirmed"] = f"no read-back from the radio yet: {why}"
        elif not confirmed:
            r["unconfirmed"] = "the radio read back something other than what was written"
        return r

    def op_channel_decode(self, url="", **_):
        info, err = decode_join_url(url)
        return info if not err else {"error": err}

    def op_channel_create(self, name="", index=None, **_):
        node = self.interface.localNode
        chans = list(node.channels or [])
        name = str(name).strip()
        if not name or len(name.encode()) > 11:
            return {"error": "a channel name is 1 to 11 bytes"}
        if index in (None, ""):
            free = [c.index for c in chans if int(c.index) >= 1 and int(c.role) == 0]
            if not free:
                return {"error": "no free channel slot on this radio (slots 1 to 7 are all in use)"}
            index = free[0]
        index = int(index)
        if index < 1 or index > 7 or int(chans[index].role) != 0:
            return {"error": f"slot {index} is not free (or is the primary)"}
        ch = chans[index]
        ch.settings.name = name
        ch.settings.psk = os.urandom(32)
        ch.role = 2
        node.writeChannel(index)
        arrived, sent, why, row = self._readback(lambda: self._read_channel(index))
        if arrived: self._adopt_slot(index, row)
        ok = bool(arrived) and row.get("name") == name and row.get("role") == "SECONDARY" and row.get("_psk") == bytes(ch.settings.psk)
        r = self._write_reply({"index": index, "name": name, "role": "SECONDARY"}, self._public(row), arrived, sent, why, ok)
        r["index"] = index
        self._emit("write", action="channel_create", index=index, confirmed=r["confirmed"])
        return r

    def op_channel_rotate(self, index=None, confirm=None, **_):
        node = self.interface.localNode
        index = int(index)
        chans = list(node.channels or [])
        if int(chans[index].role) == 0:
            return {"error": f"slot {index} is disabled; create a channel there first"}
        if index == 0:
            need = self._confirm_needed(confirm, "Rotating the key on the primary channel drops every device that has not scanned the new QR.")
            if need:
                return need
        new = os.urandom(32)
        chans[index].settings.psk = new
        node.writeChannel(index)
        arrived, sent, why, row = self._readback(lambda: self._read_channel(index))
        if arrived: self._adopt_slot(index, row)
        ok = bool(arrived) and row.get("_psk") == new
        r = self._write_reply({"index": index, "key": "rotated"}, self._public(row), arrived, sent, why, ok)
        self._emit("write", action="channel_rotate", index=index, confirmed=r["confirmed"])
        if r.get("confirmed"):
            try:
                self._rotation_mark(index, name=(row or {}).get("name"), source="rotated from the screen")
            except Exception as e:  # noqa: BLE001
                self.logger.warning(f"rotation not marked: {type(e).__name__}: {e}")
        return r

    def op_channel_adopt(self, url="", mode="add", confirm=None, **_):
        info, err = decode_join_url(url)
        if err:
            return {"error": err}
        node = self.interface.localNode
        if mode == "replace":
            need = self._confirm_needed(confirm, f"Replacing takes this radio's channels ({', '.join(info['channels']) or 'unnamed'}) and region {info.get('region') or '?'} from the URL; devices on the old channels will not hear it.")
            if need:
                need["region"] = info.get("region")
                return need
            node.setURL(url)
        else:
            node.setURL(url, addOnly=True)
        def rb():
            rows = []
            for i in range(8):
                row = self._read_channel(i)
                self._adopt_slot(i, row)
                rows.append(self._public(row))
            return rows
        arrived, sent, why, rows = self._readback(rb)
        names = {c.get("name") for c in (rows or [])}
        ok = bool(arrived) and all((n in names) for n in info["channels"] if n)
        r = self._write_reply({"mode": mode, "channels": info["channels"], "region": info.get("region"), "modem_preset": info.get("modem_preset")}, rows, arrived, sent, why, ok)
        r.update({"mode": mode, "region": info.get("region")})
        self._emit("write", action="channel_adopt", mode=mode, confirmed=r["confirmed"])
        return r

    def op_channel_delete(self, index=None, **_):
        node = self.interface.localNode
        index = int(index)
        if index < 1 or index > 7:
            return {"error": "slots 1 to 7 can be deleted; the primary cannot"}
        node.deleteChannel(index)
        arrived, sent, why, row = self._readback(lambda: self._read_channel(index))
        if arrived: self._adopt_slot(index, row)
        r = self._write_reply({"index": index, "role": "DISABLED"}, self._public(row), arrived, sent, why, bool(arrived) and row.get("role") == "DISABLED")
        self._emit("write", action="channel_delete", index=index, confirmed=r["confirmed"])
        return r

    def op_radio_set(self, long_name=None, short_name=None, tx_power=None, position_broadcast_secs=None, **_):
        node = self.interface.localNode
        cfg = node.localConfig
        written, sections = {}, []
        if long_name is not None or short_name is not None:
            node.setOwner(long_name or None, short_name or None)
            if long_name is not None: written["long_name"] = long_name
            if short_name is not None: written["short_name"] = short_name
        if tx_power is not None:
            cfg.lora.tx_power = int(tx_power); written["tx_power"] = int(tx_power); sections.append("lora")
        if position_broadcast_secs is not None:
            cfg.position.position_broadcast_secs = int(position_broadcast_secs); written["position_broadcast_secs"] = int(position_broadcast_secs)
            if "position" not in sections: sections.append("position")
        if not written:
            return {"error": "nothing to write: give at least one setting"}
        for sec in sections:
            node.writeConfig(sec)
        def rb():
            got = {}
            if "long_name" in written or "short_name" in written:
                o = self._read_owner()
                if "long_name" in written: got["long_name"] = o["long_name"]
                if "short_name" in written: got["short_name"] = o["short_name"]
            if "lora" in sections:
                got["tx_power"] = int(self._read_section("lora").tx_power)
            if "position" in sections:
                pos = self._read_section("position")
                if "position_broadcast_secs" in written: got["position_broadcast_secs"] = int(pos.position_broadcast_secs)
            return got
        arrived, sent, why, read_back = self._readback(rb)
        read_back = read_back or {}
        r = self._write_reply(written, read_back, arrived, sent, why, read_back == written)
        self._emit("write", action="radio_set", fields=sorted(written), confirmed=r["confirmed"])
        return r

    def op_radio_set_region(self, region=None, modem_preset=None, role=None, confirm=None, **_):
        need = self._confirm_needed(confirm, "Changing the region, preset or role moves this radio to another band or role; a fleet on the old setting will not hear it, and the radio reboots.")
        if need:
            return need
        node = self.interface.localNode
        cfg = node.localConfig
        written, sections = {}, []
        try:
            if region is not None:
                cfg.lora.region = self._enum_value("region", region); written["region"] = region; sections.append("lora")
            if modem_preset is not None:
                cfg.lora.modem_preset = self._enum_value("modem_preset", modem_preset); written["modem_preset"] = modem_preset
                if "lora" not in sections: sections.append("lora")
            if role is not None:
                cfg.device.role = self._enum_value("role", role); written["role"] = role; sections.append("device")
        except ValueError as e:
            return {"error": str(e)}
        if not written:
            return {"error": "nothing to write: give a region, a preset or a role"}
        for sec in sections:
            node.writeConfig(sec)
        def rb():
            got = {}
            if "lora" in sections:
                lora = self._read_section("lora")
                if "region" in written: got["region"] = region_name(lora.region)
                if "modem_preset" in written: got["modem_preset"] = preset_name(lora.modem_preset)
            if "device" in sections:
                got["role"] = role_name(self._read_section("device").role)
            return got
        arrived, sent, why, read_back = self._readback(rb)
        read_back = read_back or {}
        r = self._write_reply(written, read_back, arrived, sent, why, read_back == written)
        self._emit("write", action="radio_set_region", fields=sorted(written), confirmed=r["confirmed"])
        return r

    # ---- the socket server ----------------------------------------------------------------------
    def serve_forever(self):
        bridge = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                line = self.rfile.readline(65536)
                try:
                    req = json.loads(line.decode("utf-8", "replace"))
                    if not isinstance(req, dict):
                        raise ValueError("not an object")
                except Exception as e:  # noqa: BLE001
                    self.wfile.write((json.dumps({"error": f"bad request: {type(e).__name__}"}) + "\n").encode())
                    return
                op = str(req.pop("op", ""))
                if op == "events":
                    q = queue.Queue(maxsize=1000)
                    with bridge._subs_lock:
                        bridge._subs.append(q)
                    try:
                        self.wfile.write((json.dumps({"kind": "hello", "version": __version__}) + "\n").encode())
                        self.wfile.flush()
                        while not bridge._stop.is_set():
                            try:
                                ln = q.get(timeout=15)
                            except queue.Empty:
                                ln = json.dumps({"kind": "ping", "ts": utc(time.time())}) + "\n"
                            self.wfile.write(ln.encode())
                            self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError, OSError):
                        pass
                    finally:
                        with bridge._subs_lock:
                            if q in bridge._subs:
                                bridge._subs.remove(q)
                    return
                fn = getattr(bridge, "op_" + op, None) if op.isidentifier() else None
                if not fn:
                    rep = {"error": f"unknown op {op!r}"}
                else:
                    action = C.by_id(op)
                    if action and action.get("op") == op:
                        clean, err = C.validate(action, req, None)
                        if err:
                            self.wfile.write((json.dumps({"error": err}) + "\n").encode())
                            return
                        req = clean
                    try:
                        rep = fn(**req)
                    except TypeError as e:
                        rep = {"error": f"bad arguments for {op}: {e}"}
                    except Exception as e:  # noqa: BLE001
                        rep = {"error": f"{op} failed: {type(e).__name__}: {e}"}
                self.wfile.write((json.dumps(rep, default=str) + "\n").encode())

        class Server(socketserver.ThreadingUnixStreamServer):
            daemon_threads = True
            allow_reuse_address = True

        d = os.path.dirname(self.socket_path)
        if d and not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
        if os.path.exists(self.socket_path):
            os.unlink(self.socket_path)
        self._server = Server(self.socket_path, Handler)
        try:
            os.chmod(self.socket_path, 0o660)
        except OSError:
            pass
        self._server.serve_forever(poll_interval=0.5)

    def stop(self):
        self._stop.set()
        if self.peering:
            self.peering.stop()
        if self._server:
            self._server.shutdown()
            self._server.server_close()
        try:
            os.unlink(self.socket_path)
        except OSError:
            pass


def main(argv=None):
    ap = argparse.ArgumentParser(prog="mesh-manager-bridge", description="Mesh Manager's bridge: owns the radio, forwards the mesh into TAK, answers on a local socket.")
    ap.add_argument("--config", default=DEFAULT_CONFIG)
    ap.add_argument("--socket", default=DEFAULT_SOCKET)
    ap.add_argument("--state-dir", default=DEFAULT_STATE)
    ap.add_argument("--observe", action="store_true", help="listen only: forward nothing to TAK, bind no TAK socket (rehearsals)")
    ap.add_argument("--silence-limit", type=int, default=SILENCE_LIMIT)
    ap.add_argument("--serial", help="override the config's SERIAL (a /dev/serial/by-id/ path)")
    a = ap.parse_args(argv)
    conf = read_config(a.config)
    if a.serial:
        conf["SERIAL"] = a.serial
    if not conf.get("SERIAL") and conf.get("MODE") != "hub":
        print("ERR no SERIAL in the config and no --serial given (a site with no radio is MODE=hub)", file=sys.stderr)
        return 2
    b = Bridge(conf, socket_path=a.socket, state_dir=a.state_dir, observe=a.observe, silence_limit=a.silence_limit)
    try:
        b.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        b.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
