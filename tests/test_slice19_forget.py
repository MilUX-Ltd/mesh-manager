#!/usr/bin/env python3
"""Spec 017: forgetting a node, a press that says so at once, gpsd first, the writable paths."""
import glob
import http.client
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import zipfile

sys.path.insert(0, os.path.dirname(__file__))
from _common import ROOT, check, check_true, finish  # noqa: E402
sys.path.insert(0, os.path.join(ROOT, "src"))
import fakegw_lib  # noqa: E402
fakegw_lib.install()
from fakebridge_lib import start_fake_bridge  # noqa: E402
from mesh_manager import bridge as B, catalogue as C, web as W  # noqa: E402
from meshtastic.protobuf import portnums_pb2, mesh_pb2  # noqa: E402

state = tempfile.mkdtemp()
br = B.Bridge({"SERIAL": ""}, socket_path=os.path.join(state, "b.sock"), state_dir=state, observe=True, gps_reader=False)
br.READBACK_S = 2
events = []
_orig = br._emit
br._emit = lambda kind, **f: (events.append((kind, f)), _orig(kind, **f))[1]

# ---- AC1 forget
check("AC1 node_forget is a change action", (C.by_id("node_forget") or {}).get("risk"), "change")
br.meshtastic_devices["!bb000002"] = {"long_name": "Old one", "meshtastic_id": "!bb000002"}
br._mesh_radio["!bb000002"] = {"heard": "2026-09-03T07:00:00Z", "snr": 5.0, "hops": 0}
br.links["!bb000002"] = [["2026-09-03T07:00:00Z", 5.0, 0]]; br.direct["!bb000002"] = 5.0; br.routes["!bb000002"] = {"dest": "!bb000002"}
br.interface.nodes["!bb000002"] = {"user": {"longName": "Old one"}}
br.op_register_set(id="!bb000002", label="keep me")
r = br.op_node_forget(id="!bb000002", register="keep")
check("AC1 the radio is asked to remove the node", br.interface.localNode.calls[-1] if br.interface.localNode.calls else None, ("removeNode", "!bb000002"))
check("AC1 dropped from the devices, links and routes", ("!bb000002" in br.meshtastic_devices, "!bb000002" in br.links, "!bb000002" in br.routes, "!bb000002" in br.interface.nodes), (False, False, False, False))
check("AC1 register kept on keep", br._register_load().get("!bb000002", {}).get("label"), "keep me")
check_true("AC1 the answer says it comes back if heard", "heard again" in str(r.get("note", "")))
check_true("AC1 register and node events", any(k == "register" for k, _f in events) and any(k == "node" and f.get("action") == "node_forget" for k, f in events))
br.meshtastic_devices["!bb000002"] = {"long_name": "Old one", "meshtastic_id": "!bb000002"}
r = br.op_node_forget(id="!bb000002", register="drop")
check("AC1 register dropped on drop", "!bb000002" in br._register_load(), False)
r = br.op_node_forget(id="nope")
check_true("AC1 a bad id is refused", "radio id" in str(r.get("error", "")))

# ---- AC2 position without blocking
t0 = time.time()
r = br.op_request_position(dest="!aa000001")
check_true("AC2 answers at once with asked", time.time() - t0 < 0.5 and bool(r.get("asked")))
check("AC2 never the blocking sendPosition", br.interface.positions, [])
sent = br.interface.data[-1] if br.interface.data else {}
check("AC2 a Position through sendData with a handler", (sent.get("portNum"), callable(sent.get("onResponse")), isinstance(sent.get("data"), mesh_pb2.Position)), (portnums_pb2.PortNum.POSITION_APP, True, True))
pos = mesh_pb2.Position(); pos.latitude_i = 512128000; pos.longitude_i = -15056200
sent["onResponse"]({"fromId": "!aa000001", "decoded": {"portnum": "POSITION_APP", "payload": pos.SerializeToString()}})
ev = next((f for k, f in events if k == "position"), None)
check("AC2 the answer arrives as a position event", (ev or {}).get("id"), "!aa000001")
check("AC2 ...with the fix", (round((ev or {}).get("lat", 0), 4), round((ev or {}).get("lon", 0), 4)), (51.2128, -1.5056))

# ---- AC3 gpsd first
class FakeGpsd(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.srv = socket.socket(); self.srv.bind(("127.0.0.1", 0)); self.srv.listen(2); self.port = self.srv.getsockname()[1]; self.watched = []

    def run(self):
        while True:
            c, _ = self.srv.accept()
            c.sendall(b'{"class":"VERSION","release":"3.25"}\n')
            self.watched.append(c.recv(200))
            c.sendall(b'{"class":"DEVICES","devices":[]}\n{"class":"TPV","mode":1}\n{"class":"SKY","uSat":9}\n{"class":"TPV","mode":3,"time":"2026-09-03T10:40:00.000Z","lat":51.2128,"lon":-1.5056}\n')
            time.sleep(0.5); c.close()


g = FakeGpsd(); g.start()
opened = []
br.gps_port_factory = lambda path: opened.append(path) or fakegw_lib.FakeBenchIface(path)
br.conf = {"SERIAL": "", "MAP_GPS": f"gpsd://127.0.0.1:{g.port}"}
fix = br.read_gps(timeout=3)
check("AC3 the fix from gpsd", (round((fix or {}).get("lat", 0), 4), round((fix or {}).get("lon", 0), 4), (fix or {}).get("sats"), (fix or {}).get("time")), (51.2128, -1.5056, 9, "2026-09-03T10:40:00Z"))
check_true("AC3 gpsd was asked to WATCH in JSON and the port never opened", g.watched and b"?WATCH" in g.watched[0] and b"json" in g.watched[0] and not opened)
class NoFixGpsd(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.srv = socket.socket(); self.srv.bind(("127.0.0.1", 0)); self.srv.listen(2); self.port = self.srv.getsockname()[1]

    def run(self):
        while True:
            c, _ = self.srv.accept()
            c.sendall(b'{"class":"VERSION","release":"3.25"}\n'); c.recv(200)
            c.sendall(b'{"class":"TPV","mode":1}\n{"class":"SKY","nSat":14,"uSat":0}\n{"class":"TPV","mode":1}\n')
            time.sleep(0.5); c.close()


nf = NoFixGpsd(); nf.start()
byid = tempfile.mkdtemp(); GPS = os.path.join(byid, "usb-u-blox_AG_GPS_GNSS_Receiver-if00"); open(GPS, "w").close()
br.serial_dir = byid
br.conf = {"SERIAL": ""}
br.gpsd_address = ("127.0.0.1", nf.port)
opened.clear()
fix = br.read_gps(timeout=2)
check("AC3 gpsd reachable but no fix: no fix, the port never opened, the sky recorded", (fix, opened, (br.gps_state or {}).get("reachable"), (br.gps_state or {}).get("fix"), (br.gps_state or {}).get("seen"), (br.gps_state or {}).get("used")), (None, [], True, False, 14, 0))
br.serial_dir = byid
br.conf = {"SERIAL": ""}
br.gpsd_address = ("127.0.0.1", 9)     # nothing there: fall through to the port
br.gps_port_factory = lambda path: opened.append(path) or type("P", (), {"readline": lambda s_: b"$GPGGA,073542.00,5112.76800,N,00130.33720,W,1,08,1.02,84.3,M,47.2,M,,*5C\r\n", "close": lambda s_: None})()
fix = br.read_gps(timeout=2)
check("AC3 without gpsd the port is read as before", (opened[-1:] == [GPS], round((fix or {}).get("lat", 0), 5)), (True, 51.2128))

# ---- AC4 the screen
fb = start_fake_bridge()
srv = W.make_server(bind="127.0.0.1", port=0, socket_path=fb.path, etc_dir=tempfile.mkdtemp(), config={"AUTH": "off"})
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start(); time.sleep(0.3)
c = http.client.HTTPConnection("127.0.0.1", port, timeout=10); c.request("GET", "/register"); reg = c.getresponse().read().decode(); c.close()
check_true("AC4 a Forget control on the Register row", "data-action='node_forget'" in reg and "name='id'" in reg)
c = http.client.HTTPConnection("127.0.0.1", port, timeout=10); c.request("GET", "/nodes"); nodes = c.getresponse().read().decode(); c.close()
check_true("AC4 a press says asking at once and position events are handled", "asking" in nodes and "mmPosition" in nodes and "'position'" in nodes)
srv.shutdown()

# ---- AC5 the unit and the wheel
inst = open(os.path.join(ROOT, "install", "install.sh")).read()
check_true("AC5 the screen's unit can write its own directories", "ReadWritePaths=/etc/mesh-manager /var/lib/vantage-mesh" in inst.split("Mesh Manager screen", 1)[-1][:1200])
subprocess.run([sys.executable, "-m", "pip", "wheel", ROOT, "--no-deps", "-w", tempfile.mkdtemp(), "-q"], capture_output=True, text=True)
wheels = sorted(glob.glob(os.path.join(tempfile.gettempdir(), "**", "mesh_manager-*.whl"), recursive=True), key=os.path.getmtime)
names = zipfile.ZipFile(wheels[-1]).namelist() if wheels else []
check_true("AC5 the wheel carries Leaflet's images", any(n.endswith("static/leaflet/images/layers.png") for n in names))
finish()
