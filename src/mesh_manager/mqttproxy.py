"""Spec 070: the box carries the radio's MQTT.

A Meshtastic radio normally reaches a broker over its own wifi. The estate's gateway radios sit on
the end of a USB cable inside a box whose network is wired, so they never can. The firmware's answer
is `proxy_to_client_enabled`: the radio hands its MQTT traffic to whatever is on the other end of the
serial link. On a phone that is the Meshtastic app. Here it is the box.

**This module carries bytes and decides nothing.** Which packets may go to MQTT, what a channel
uplinks, how a position is truncated and what is encrypted are firmware decisions and stay in the
radio. Everything here is the transport the radio has asked for.

paho is imported lazily so a box that has not yet taken the dependency still starts, and so the
suites can drive this with a fake and no broker.
"""
import threading
import time

DEFAULT_PORT_TLS, DEFAULT_PORT_PLAIN = 8883, 1883
RETRY_SECS = 15.0


def _client_factory():
    """paho's client, fetched only when a proxy actually runs.

    paho 2 changed its callback signatures, so ask it for the version 1 shape the handlers below
    are written against rather than quietly reading a disconnect flag as a reason code.
    """
    from paho.mqtt import client as paho  # noqa: PLC0415  (deliberate: see the module docstring)

    def make(client_id="", **kw):
        version = getattr(paho, "CallbackAPIVersion", None)
        if version is not None:                      # paho 2.x
            return paho.Client(version.VERSION1, client_id=client_id, **kw)
        return paho.Client(client_id=client_id, **kw)   # paho 1.x
    return make


def settings_from(mqtt):
    """The broker settings as the RADIO holds them: one source of truth, the same place the phone
    apps read. `address` may carry a port; otherwise TLS decides it."""
    address = str(getattr(mqtt, "address", "") or "").strip()
    port = None
    if address.count(":") == 1:                      # host:port, never a bare IPv6 literal
        host, _, tail = address.partition(":")
        if tail.isdigit():
            address, port = host, int(tail)
    tls = bool(getattr(mqtt, "tls_enabled", False))
    return {
        "enabled": bool(getattr(mqtt, "enabled", False)),
        "proxy": bool(getattr(mqtt, "proxy_to_client_enabled", False)),
        "address": address,
        "port": port or (DEFAULT_PORT_TLS if tls else DEFAULT_PORT_PLAIN),
        "username": str(getattr(mqtt, "username", "") or ""),
        "password": str(getattr(mqtt, "password", "") or ""),
        "root": str(getattr(mqtt, "root", "") or "").strip("/") or "msh",
        "tls": tls,
    }


def topics_for(channels, root):
    """What to subscribe to: a channel the radio will not accept from MQTT is not subscribed, so
    nothing arrives that the radio would only drop."""
    root = str(root or "msh").strip("/")
    out = []
    for c in channels or []:
        if not getattr(c, "role", 0):
            continue
        s = getattr(c, "settings", None)
        if s is None or not getattr(s, "downlink_enabled", False):
            continue
        name = str(getattr(s, "name", "") or "").strip()
        if not name:                                  # the primary's default name is not a topic
            continue
        out.append(f"{root}/2/e/{name}/#")
    return out


def why_not(settings, has_radio):
    """The one sentence the screen shows when no proxy is running. None means it should run."""
    if not has_radio:
        return "no gateway radio on this box, so there is nothing to carry"
    if not settings.get("enabled"):
        return "MQTT is off on the radio"
    if not settings.get("proxy"):
        return "the radio is not set to proxy through this box (proxy_to_client_enabled is off)"
    if not settings.get("address"):
        return "the radio has no broker address"
    return None


class Proxy:
    """Relays the radio's MQTT over the box's own network, both ways, and reconnects on its own.

    The broker being unreachable must never touch the mesh: every failure here is caught, recorded
    in `state()` and retried.
    """

    def __init__(self, interface, settings, client_factory=None, retry_secs=RETRY_SECS, log=None):
        self.interface, self.settings = interface, dict(settings)
        self._factory = client_factory or _client_factory()
        self._retry, self._log = float(retry_secs), log
        self._stop = threading.Event()
        self._client = None
        self._lock = threading.Lock()
        self._connected = False
        self._error = None
        self._sent = self._received = 0
        self._last_sent = self._last_received = None
        self._thread = None

    # what the screen reads -------------------------------------------------
    def state(self):
        s = self.settings
        return {
            "connected": self._connected,
            "broker": f"{s.get('address', '')}:{s.get('port', '')}" if s.get("address") else "",
            "tls": bool(s.get("tls")),
            "root": s.get("root"),
            "user": s.get("username"),          # never the password
            "topics": list(getattr(self, "_topics", [])),
            "sent": self._sent,
            "received": self._received,
            "last_sent": self._last_sent,
            "last_received": self._last_received,
            "error": self._error,
        }

    # the two directions ----------------------------------------------------
    def on_radio(self, proxymessage, **_):
        """The radio wants something published. Send exactly what it gave us."""
        try:
            topic = getattr(proxymessage, "topic", "")
            which = proxymessage.WhichOneof("payload_variant") if hasattr(proxymessage, "WhichOneof") else "data"
            payload = getattr(proxymessage, "text", "") if which == "text" else getattr(proxymessage, "data", b"")
            retained = bool(getattr(proxymessage, "retained", False))
            with self._lock:
                client = self._client
            if client is None:
                return
            client.publish(topic, payload, qos=0, retain=retained)
            self._sent += 1
            self._last_sent = {"topic": topic, "bytes": len(payload or b""), "at": _utc()}
        except Exception as e:  # noqa: BLE001  the mesh must not care that a broker misbehaved
            self._error = f"publish failed: {type(e).__name__}: {e}"

    def _on_message(self, _client, _userdata, message):
        """The broker sent something. Hand it to the radio unchanged."""
        try:
            self.interface.sendMqttClientProxyMessage(message.topic, bytes(message.payload))
            self._received += 1
            self._last_received = {"topic": message.topic, "bytes": len(message.payload or b""), "at": _utc()}
        except Exception as e:  # noqa: BLE001
            self._error = f"handing a message to the radio failed: {type(e).__name__}: {e}"

    # lifecycle -------------------------------------------------------------
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="mqttproxy", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        with self._lock:
            client, self._client = self._client, None
        for call in ("loop_stop", "disconnect"):
            try:
                getattr(client, call)()
            except Exception:  # noqa: BLE001, S110
                pass
        self._connected = False

    def _run(self):
        while not self._stop.is_set():
            if not self._connected:
                self._attempt()
            self._stop.wait(self._retry)

    def _attempt(self):
        s = self.settings
        try:
            client = self._factory(client_id=f"mesh-manager-{int(time.time())}")
            if s.get("username"):
                client.username_pw_set(s["username"], s.get("password") or "")
            if s.get("tls"):
                client.tls_set()
            client.on_connect = self._on_connect
            client.on_message = self._on_message
            client.on_disconnect = self._on_disconnect
            client.connect(s["address"], int(s["port"]), keepalive=60)
            client.loop_start()
            with self._lock:
                self._client = client
            self._error = None
        except Exception as e:  # noqa: BLE001
            self._connected = False
            self._error = f"cannot reach the broker: {type(e).__name__}: {e}"

    def _on_connect(self, client, _userdata, _flags, rc, *_):
        if rc not in (0, None):
            self._error = f"the broker refused the connection (code {rc})"
            self._connected = False
            return
        self._connected, self._error = True, None
        self._topics = topics_for(self._channels(), self.settings.get("root"))
        for t in self._topics:
            try:
                client.subscribe(t, qos=0)
            except Exception as e:  # noqa: BLE001
                self._error = f"subscribe to {t} failed: {type(e).__name__}: {e}"

    def _on_disconnect(self, _client, _userdata, rc, *_):
        self._connected = False
        if rc:
            self._error = f"the broker dropped the connection (code {rc})"

    def _channels(self):
        try:
            return list(self.interface.localNode.channels)
        except Exception:  # noqa: BLE001
            return []


def _utc():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
