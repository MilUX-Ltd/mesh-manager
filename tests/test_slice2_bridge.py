#!/usr/bin/env python3
"""Spec 002: the bridge as a package, with its socket API and watchdog. Runs against a fake
gateway class installed into sys.modules before mesh_manager.bridge is imported, so no radio
and no gateway install are needed; the real thing is proven on the kit (LG-S2)."""
import json
import os
import socket
import sys
import tempfile
import threading
import time
import types

sys.path.insert(0, os.path.dirname(__file__))
from _common import ROOT, check, check_true, finish, read  # noqa: E402

sys.path.insert(0, os.path.join(ROOT, "src"))

# ---- a fake gateway, shaped like the real one where the bridge touches it ----------------
fake_pkg = types.ModuleType("tak_meshtastic_gateway")
fake_mod = types.ModuleType("tak_meshtastic_gateway.tak_meshtastic_gateway")
fake_pkg.__version__ = "1.1.0"


class FakeSock:
    def __init__(self): self.sent = []
    def connect(self, addr): self.addr = addr
    def send(self, b): self.sent.append(b)
    def sendto(self, b, a): self.sent.append(b)
    def close(self): pass


class FakeIface:
    def __init__(self):
        self.texts, self.traces, self.positions = [], [], []
        self.nodes = {"!aa000001": {"user": {"longName": "Tracker9", "shortName": "TR9", "hwModel": "TRACKER_T1000_E"},
                                    "deviceMetrics": {"batteryLevel": 77}, "lastHeard": int(time.time()) - 5},
                      "!00000001": {"user": {"longName": "Bench", "shortName": "BNCH", "hwModel": "HELTEC_V3"}}}
        self.myInfo = types.SimpleNamespace(my_node_num=1)
        self.localNode = types.SimpleNamespace(
            channels=[types.SimpleNamespace(index=0, role=1, settings=types.SimpleNamespace(name="MILUX-TAK", psk=b"\x01" * 32)),
                      types.SimpleNamespace(index=1, role=0, settings=types.SimpleNamespace(name="", psk=b""))],
            localConfig=types.SimpleNamespace(lora=types.SimpleNamespace(region=3, modem_preset=6, tx_power=14),
                                              device=types.SimpleNamespace(role=0),
                                              position=types.SimpleNamespace(position_broadcast_secs=900)),
            getURL=lambda includeAll=True: "https://meshtastic.org/e/#SECRET-URL-WITH-KEY")
    def getMyNodeInfo(self): return {"num": 1, "user": {"id": "!00000001", "longName": "Bench", "shortName": "BNCH"}}
    def sendText(self, text, destinationId="^all", channelIndex=0, **kw): self.texts.append((text, destinationId, channelIndex)); return "pkt"
    def sendTraceRoute(self, dest, hopLimit=7, channelIndex=0): self.traces.append((dest, hopLimit))
    def sendPosition(self, destinationId="^all", wantResponse=False, channelIndex=0): self.positions.append(destinationId)
    def sendData(self, data, destinationId="^all", portNum=None, wantResponse=False, onResponse=None, channelIndex=0, hopLimit=3, **kw):
        if not hasattr(self, "data"): self.data = []
        self.data.append({"data": data, "dest": destinationId, "portNum": portNum, "wantResponse": wantResponse, "onResponse": onResponse, "hopLimit": hopLimit})
    def close(self): pass


class TAKMeshtasticGateway:
    def __init__(self, ip=None, serial_device=None, mesh_ip=None, tak_client_ip="localhost", tx_interval=30,
                 dm_port=4243, log_file=None, debug=False):
        import logging
        self.ip, self.serial_device, self.debug = ip, serial_device, debug
        self.meshtastic_devices = {"!aa000001": {"long_name": "Tracker9", "battery": 77, "meshtastic_id": "!aa000001",
                                                 "last_lat": 51.2, "last_lon": -1.5}}
        self._mesh_radio = {"!aa000001": {"heard": "2026-09-03T02:00:00Z", "snr": 12.5, "hops": 0}}
        self._hb_last = 0.0
        self.meshtastic_connected = False
        self.logger = logging.getLogger("TAK Meshtastic Gateway")
        self.logger.setLevel(logging.INFO)
        self.socket_client = FakeSock()
        self.interface = FakeIface()
        self.main_calls = 0
    def mesh_nodes(self):
        return [{"id": "!aa000001", "name": "Tracker9", "battery": 77, "lat": 51.2, "lon": -1.5,
                 "heard": "2026-09-03T02:00:00Z", "snr": 12.5, "hops": 0, "heard_here": True}]
    def heartbeat(self): pass
    def main(self):
        self.main_calls += 1
        self.chat_sock = FakeSock(); self.sa_multicast_sock = FakeSock()   # would bind in the real one
        time.sleep(0.2)


fake_mod.TAKMeshtasticGateway = TAKMeshtasticGateway
fake_pkg.tak_meshtastic_gateway = fake_mod
sys.modules["tak_meshtastic_gateway"] = fake_pkg
sys.modules["tak_meshtastic_gateway.tak_meshtastic_gateway"] = fake_mod
# pubsub is the gateway's dependency; the bridge subscribes through it
try:
    from pubsub import pub
except ImportError:
    pub = None

try:
    import mesh_manager
    from mesh_manager import bridge as B
except Exception as e:  # noqa: BLE001
    print(f"FAIL mesh_manager.bridge imports                                   {type(e).__name__}: {e}")
    print("\nFAILURES: 1"); sys.exit(1)

# ---- AC1 ----------------------------------------------------------------------------------
check("AC1 version is the VERSION file", mesh_manager.__version__, (read("VERSION") or "").strip())
check_true("AC1 Bridge subclasses the gateway", issubclass(B.Bridge, TAKMeshtasticGateway))
cf = tempfile.NamedTemporaryFile("w", delete=False, suffix=".conf")
cf.write("# c\nSERIAL=/dev/serial/by-id/usb-x-if00\nREGION=EU_868\nCHANNEL=MILUX-TAK\nFILTER_GROUP=mesh\nEXTRA_ARGS=-i 192.168.88.10 -d\nBIND=127.0.0.1\nPORT=8093\n")
cf.close()
conf = B.read_config(cf.name)
check("AC1 config: serial", conf["SERIAL"], "/dev/serial/by-id/usb-x-if00")
check("AC1 config: -i from EXTRA_ARGS", conf["ip"], "192.168.88.10")
check("AC1 config: -d from EXTRA_ARGS", conf["debug"], True)
check("AC1 config: bind and port", (conf["BIND"], conf["PORT"]), ("127.0.0.1", 8093))

# ---- AC2 the watchdog decision --------------------------------------------------------------
now = 1000.0
check("AC2 pings while activity is fresh", B.watchdog_decision(now, last_activity=now - 30, radio_present=True, bootloader=False, limit=600)[0], True)
check("AC2 pings while the radio is absent", B.watchdog_decision(now, last_activity=now - 5000, radio_present=False, bootloader=False, limit=600)[0], True)
d = B.watchdog_decision(now, last_activity=now - 700, radio_present=True, bootloader=False, limit=600)
check("AC2 stops with the radio present and the loop silent", d[0], False)
check_true("AC2 ...and says why", "silent" in d[1] and "700" in d[1])
d = B.watchdog_decision(now, last_activity=now - 5000, radio_present=True, bootloader=True, limit=600)
check_true("AC2 names bootloader mode", "bootloader" in d[1])
nd = tempfile.mkdtemp(); ns = os.path.join(nd, "notify")
srv = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM); srv.bind(ns); srv.settimeout(2)
os.environ["NOTIFY_SOCKET"] = ns
B.sd_notify("READY=1"); B.sd_notify("WATCHDOG=1")
got = [srv.recv(64).decode() for _ in range(2)]
check("AC2 sd_notify datagrams", got, ["READY=1", "WATCHDOG=1"])
srv.close(); del os.environ["NOTIFY_SOCKET"]

# ---- AC3 to AC6: the server against the fake gateway --------------------------------------
sd = tempfile.mkdtemp()
sock_path = os.path.join(sd, "b.sock")
br = B.Bridge(conf, socket_path=sock_path, state_dir=sd, observe=True, silence_limit=600)
t = threading.Thread(target=br.serve_forever, daemon=True); t.start()
deadline = time.time() + 5
while not os.path.exists(sock_path) and time.time() < deadline:
    time.sleep(0.05)


def ask(op, **args):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); s.settimeout(3); s.connect(sock_path)
    s.sendall((json.dumps({"op": op, **args}) + "\n").encode())
    buf = b""
    while not buf.endswith(b"\n"):
        chunk = s.recv(65536)
        if not chunk: break
        buf += chunk
    s.close()
    return json.loads(buf.decode() or "{}")


st = ask("status")
check("AC3 status carries the version", st.get("version"), mesh_manager.__version__)
check("AC3 status names the radio path", st.get("radio"), "/dev/serial/by-id/usb-x-if00")
check("AC3 status: observe flag", st.get("observe"), True)
check_true("AC3 status: own node name", st.get("own", {}).get("name") == "Bench")
check("AC3 status: region and preset names", (st.get("region"), st.get("modem_preset")), ("EU_868", "SHORT_FAST"))
nodes = ask("nodes").get("nodes", [])
check("AC3 nodes: one node, joined with the node db", [(n["id"], n["name"], n.get("hw")) for n in nodes],
      [("!aa000001", "Tracker9", "TRACKER_T1000_E")])
ch = ask("channels")
check("AC3 channels: names and roles", [(c["index"], c["name"], c["role"], c["has_key"]) for c in ch.get("channels", [])],
      [(0, "MILUX-TAK", "PRIMARY", True), (1, "", "DISABLED", False)])
check_true("AC3 channels never carry a key", "psk" not in json.dumps(ch.get("channels")) and "\\u0001" not in json.dumps(ch))
check_true("AC3 channels carry the url for the QR route only", ch.get("url", "").startswith("https://meshtastic.org/e/#"))
cfg = ask("config")
check("AC3 config: names, region, preset, power", (cfg.get("long_name"), cfg.get("region"), cfg.get("modem_preset"), cfg.get("tx_power")),
      ("Bench", "EU_868", "SHORT_FAST", 14))
br.gateway.logger.info("hello from the gateway")
time.sleep(0.1)
lg = ask("log")
check_true("AC3 log: the ring carries the line", any("hello from the gateway" in l for l in lg.get("lines", [])))
check_true("AC3 unknown op answers an error", "error" in ask("no_such_op"))
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); s.settimeout(3); s.connect(sock_path)
s.sendall(b"this is not json\n"); bad = s.recv(4096); s.close()
check_true("AC3 malformed line answers an error", b"error" in bad)
check_true("AC3 ...and the server keeps serving", "version" in ask("status"))

# AC4 events
es = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); es.settimeout(3); es.connect(sock_path)
es.sendall(b'{"op": "events"}\n')
first = es.recv(4096)   # the hello line
if pub:
    pub.sendMessage("meshtastic.receive", packet={"fromId": "!aa000001", "decoded": {"portnum": "POSITION_APP"}}, interface=br.gateway.interface)
    pub.sendMessage("meshtastic.log", line="[radio] AGC reset")
got = b""
deadline = time.time() + 2
while time.time() < deadline and got.count(b"\n") < 2:
    try:
        got += es.recv(4096)
    except socket.timeout:
        break
es.close()
evs = [json.loads(l) for l in got.decode().splitlines() if l.strip()]
check_true("AC4 a packet event arrives on the stream", any(e.get("kind") == "packet" and e.get("from") == "!aa000001" for e in evs), [e.get("kind") for e in evs])
check_true("AC4 a log event arrives on the stream", any(e.get("kind") == "log" for e in evs))
check_true("AC4 activity was stamped by the events", br.last_activity > 0)

# AC5 on-air requests, observe mode
r = ask("send_text", text="hello mesh", channel=0)
check("AC5 send_text reaches the interface", br.gateway.interface.texts, [("hello mesh", "^all", 0)])
check_true("AC5 send_text answers what was sent", r.get("sent") == "hello mesh")
ask("traceroute", dest="!aa000001")
from meshtastic.protobuf import portnums_pb2 as _pn
_sent = br.gateway.interface.data[-1] if getattr(br.gateway.interface, "data", None) else {}
check("AC5 traceroute reaches the interface (Spec 008: sendData with a handler, never the blocking call)", (_sent.get("dest"), _sent.get("portNum"), br.gateway.interface.traces), ("!aa000001", _pn.PortNum.TRACEROUTE_APP, []))
check_true("AC5 observe: the TAK socket is a counter", isinstance(br.gateway.socket_client, B.CountingSocket))
check("AC5 observe: the gateway's TAK loop was never started", br.gateway.main_calls, 0)

# AC6 the heartbeat lands in the state dir atomically
br.gateway._hb_last = 0.0
br.heartbeat()
hb = json.load(open(os.path.join(sd, "heartbeat.json")))
check("AC6 heartbeat written to the state dir", (hb.get("nodes_seen"), hb["nodes"][0]["id"]), (1, "!aa000001"))
check_true("AC6 no temp file left behind", not os.path.exists(os.path.join(sd, ".heartbeat.tmp")))
br.stop()
finish()
