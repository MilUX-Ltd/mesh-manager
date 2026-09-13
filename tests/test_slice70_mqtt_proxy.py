#!/usr/bin/env python3
"""Spec 070: the box carries the radio's MQTT. The proxy relays bytes both ways and decides nothing.

Every check here runs against fakes: no broker, no radio, no network. paho-mqtt is not imported by
the suite, and mqttproxy must import without it so a box that has not taken the new dependency yet
still starts.
"""
import os, sys, time
sys.path.insert(0, os.path.dirname(__file__))
from _common import ROOT, check, check_true, finish, read  # noqa: E402
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "tests"))
from mesh_manager import mqttproxy as MP  # noqa: E402
from meshtastic.protobuf import localonly_pb2  # noqa: E402
from fakegw_lib import FakeIface  # noqa: E402


class FakeClient:
    """Enough of paho's surface for the proxy to drive, and a record of what it was told."""
    instances = []

    def __init__(self, client_id="", protocol=None, **kw):
        self.client_id, self.kw = client_id, kw
        self.published, self.subscribed = [], []
        self.creds, self.tls, self.connected_to = None, False, None
        self.looping, self.refuse = False, False
        self.on_connect = self.on_message = self.on_disconnect = None
        FakeClient.instances.append(self)

    def username_pw_set(self, u, p): self.creds = (u, p)
    def tls_set(self, *a, **k): self.tls = True
    def connect(self, host, port, keepalive=60):
        if self.refuse:
            raise OSError("connection refused")
        self.connected_to = (host, port)
    def subscribe(self, topic, qos=0): self.subscribed.append(topic)
    def publish(self, topic, payload=None, qos=0, retain=False):
        self.published.append((topic, payload, retain))
    def loop_start(self): self.looping = True
    def loop_stop(self): self.looping = False
    def disconnect(self): self.connected_to = None

    # helpers the tests use to play the broker
    def say_connected(self):
        if self.on_connect: self.on_connect(self, None, {}, 0)
    def say_dropped(self, rc=1):
        if self.on_disconnect: self.on_disconnect(self, None, rc)
    def deliver(self, topic, payload):
        if self.on_message: self.on_message(self, None, type("M", (), {"topic": topic, "payload": payload})())


class Msg:
    """A MqttClientProxyMessage as the library hands it over."""
    def __init__(self, topic, data=b"", retained=False):
        self.topic, self.data, self.retained = topic, data, retained
    def WhichOneof(self, _): return "data"


def chan(index, name, role, downlink):
    c = type("C", (), {})()
    c.index, c.role = index, role
    c.settings = type("S", (), {})()
    c.settings.name, c.settings.downlink_enabled = name, downlink
    return c


CHANNELS = [chan(0, "LongFast", 1, False), chan(1, "MilUXPriv", 2, True), chan(2, "", 0, False)]


def settings(**over):
    m = localonly_pb2.LocalModuleConfig().mqtt
    m.enabled = True
    m.address = "tak.milux.co.uk"
    m.username = "matt"
    m.password = "secret-not-for-the-screen"
    m.root = "milux"
    m.tls_enabled = True
    m.proxy_to_client_enabled = True
    s = MP.settings_from(m)
    s.update(over)
    return s


# AC1: subscriptions follow the channels that will accept from MQTT
t = MP.topics_for(CHANNELS, "milux")
check("AC1 subscribes to the downlink channel", t, ["milux/2/e/MilUXPriv/#"])
check_true("AC1 no topic for a channel without downlink", "LongFast" not in " ".join(t))
check_true("AC1 no topic for an unused slot", all("//" not in x for x in t))

# AC4: the connection is made from what the radio says
FakeClient.instances = []
p = MP.Proxy(FakeIface(), settings(), client_factory=FakeClient)
p.start(); time.sleep(0.05)
c = FakeClient.instances[-1]
check("AC4 connects to the radio's broker", c.connected_to, ("tak.milux.co.uk", 8883))
check("AC4 uses the radio's credentials", c.creds, ("matt", "secret-not-for-the-screen"))
check("AC4 TLS on when the radio says so", c.tls, True)
p.stop()

FakeClient.instances = []
p2 = MP.Proxy(FakeIface(), settings(tls=False, port=1883), client_factory=FakeClient)
p2.start(); time.sleep(0.05)
c2 = FakeClient.instances[-1]
check("AC4 TLS off and port 1883 when the radio says so", (c2.tls, c2.connected_to[1]), (False, 1883))

# AC2: what the radio hands over goes to the broker unchanged
c2.say_connected()
p2.on_radio(Msg("milux/2/e/MilUXPriv/!ee000070", b"\x0a\x0bpayload", retained=True))
check("AC2 publishes the radio's topic and bytes",
      c2.published[-1], ("milux/2/e/MilUXPriv/!ee000070", b"\x0a\x0bpayload", True))

# AC3: what the broker sends goes to the radio unchanged
iface = FakeIface()
FakeClient.instances = []
p3 = MP.Proxy(iface, settings(), client_factory=FakeClient)
p3.start(); time.sleep(0.05)
c3 = FakeClient.instances[-1]; c3.say_connected()
c3.deliver("milux/2/e/MilUXPriv/!ee000071", b"\x01\x02inbound")
check("AC3 hands the broker's message to the radio",
      getattr(iface, "proxied", [])[-1:], [("milux/2/e/MilUXPriv/!ee000071", b"\x01\x02inbound")])

# AC5: it refuses to start when there is nothing to carry, and says why
check_true("AC5 no reason to refuse a good setup", MP.why_not(settings(), has_radio=True) is None)
check_true("AC5 says why with no gateway radio",
           "radio" in (MP.why_not(settings(), has_radio=False) or "").lower())
check_true("AC5 says why when the radio is not proxying",
           "proxy" in (MP.why_not(settings(proxy=False), has_radio=True) or "").lower())
check_true("AC5 says why when MQTT is off on the radio",
           (MP.why_not(settings(enabled=False), has_radio=True) or "") != "")
check_true("AC5 says why with no broker address",
           (MP.why_not(settings(address=""), has_radio=True) or "") != "")

# AC6: a broker that refuses leaves the bridge alone and the state explains
FakeClient.instances = []
class Refusing(FakeClient):
    def __init__(self, *a, **k):
        super().__init__(*a, **k); self.refuse = True
p4 = MP.Proxy(FakeIface(), settings(), client_factory=Refusing)
p4.start(); time.sleep(0.1)
st = p4.state()
check("AC6 not connected when the broker refuses", st.get("connected"), False)
check_true("AC6 the state says why", bool(st.get("error")), st.get("error"))
p4.stop()

# AC7: it comes back once the broker does
FakeClient.instances = []
p5 = MP.Proxy(FakeIface(), settings(), client_factory=FakeClient, retry_secs=0.05)
p5.start(); time.sleep(0.05)
c5 = FakeClient.instances[-1]; c5.say_connected()
check("AC7 connected", p5.state().get("connected"), True)
c5.say_dropped()
check("AC7 knows it dropped", p5.state().get("connected"), False)
time.sleep(0.35)
check_true("AC7 tried again without a restart", len(FakeClient.instances) > 1,
           f"{len(FakeClient.instances)} attempts")
p5.stop()

# AC8: the state is what the screen needs, and never the password
FakeClient.instances = []
p6 = MP.Proxy(FakeIface(), settings(), client_factory=FakeClient)
p6.start(); time.sleep(0.05)
c6 = FakeClient.instances[-1]; c6.say_connected()
p6.on_radio(Msg("milux/2/e/MilUXPriv/!x", b"out"))
c6.deliver("milux/2/e/MilUXPriv/!y", b"in")
st = p6.state()
for k in ("connected", "broker", "sent", "received", "last_sent", "last_received"):
    check_true(f"AC8 state carries {k}", k in st, repr(st.get(k)))
check("AC8 counts what it carried", (st.get("sent"), st.get("received")), (1, 1))
check_true("AC8 the password is nowhere in the state", "secret-not-for-the-screen" not in repr(st))
p6.stop()

# AC11: the wiring, not just the handler. The first suite drove on_radio directly and so never
# touched pypubsub, which is where the whole thing was broken on the live box: pypubsub fixes a
# topic's argument spec from its first subscriber and **kwargs registers as nothing, so the
# library's send raised inside its publishing thread and was swallowed.
try:
    from pubsub import pub as _pub
    FakeClient.instances = []
    iface2 = FakeIface()
    p7 = MP.Proxy(iface2, settings(), client_factory=FakeClient)
    p7.start(); time.sleep(0.05)
    c7 = FakeClient.instances[-1]; c7.say_connected()
    _pub.subscribe(p7.on_radio, "meshtastic.mqttclientproxymessage")
    try:
        _pub.sendMessage("meshtastic.mqttclientproxymessage",
                         proxymessage=Msg("milux/2/e/MilUXPriv/!ee000072", b"viapubsub"),
                         interface=iface2)
        check("AC11 a message sent the way the library sends it reaches the broker",
              c7.published[-1:], [("milux/2/e/MilUXPriv/!ee000072", b"viapubsub", False)])
    except Exception as e:  # noqa: BLE001  a clean red, not a crash
        check_true("AC11 a message sent the way the library sends it reaches the broker", False,
                   f"{type(e).__name__}: {str(e)[:120]}")
    p7.stop()
except ImportError:
    from _common import skip
    skip("AC11 delivery through pypubsub", "pypubsub is not installed here")

# AC9: the op writes the radio's MQTT config and reads it back
import fakegw_lib  # noqa: E402
fakegw_lib.install()
import tempfile  # noqa: E402
from mesh_manager import bridge as B  # noqa: E402

state = tempfile.mkdtemp()
br = B.Bridge({"SERIAL": "/dev/serial/by-id/usb-fake-test-radio-if00"}, socket_path=os.path.join(state, "b.sock"), state_dir=state, observe=True)
br.mqtt_client_factory = FakeClient
out = br.op_gateway_mqtt_set(address="tak.milux.co.uk", username="matt", password="pw",
                             root="milux", tls="on", enabled="on", confirm="yes")
check_true("AC9 the op reports what it wrote", "written" in out, repr(out)[:200])
check("AC9 the op confirms against the radio's own answer", out.get("confirmed"), True)
rb = out.get("read_back", {})
check("AC9 the op turns on proxy to client", rb.get("proxy_to_client_enabled"), True)
check("AC9 the address reached the radio", rb.get("address"), "tak.milux.co.uk")
check("AC9 encryption is on, never plaintext to a broker", rb.get("encryption_enabled"), True)
check_true("AC9 the op never echoes the password", "'pw'" not in repr(rb), repr(rb))
check_true("AC9 it says a password is set without saying what", rb.get("password_set") is True)
check("AC9 it refuses without confirm", "error" in br.op_gateway_mqtt_set(address="x"), True)

# AC10: the wheel has to be declared or the box will not install it (LESSONS 6)
py = read("pyproject.toml") or ""
check_true("AC10 paho-mqtt is a declared dependency", "paho-mqtt" in py)

finish()
