#!/usr/bin/env python3
"""Spec 018: battery truth."""
import http.client
import os
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(__file__))
from _common import ROOT, check, check_true, finish  # noqa: E402
sys.path.insert(0, os.path.join(ROOT, "src"))
import fakegw_lib  # noqa: E402
fakegw_lib.install()
from fakebridge_lib import start_fake_bridge  # noqa: E402
import fakebridge_lib as FB  # noqa: E402
from mesh_manager import bridge as B, web as W  # noqa: E402

state = tempfile.mkdtemp()
br = B.Bridge({"SERIAL": ""}, socket_path=os.path.join(state, "b.sock"), state_dir=state, observe=True, gps_reader=False)


def telem(fr, level, volts):
    return {"fromId": fr, "toId": "^all", "rxSnr": 9.0, "hopStart": 3, "hopLimit": 3,
            "decoded": {"portnum": "TELEMETRY_APP", "telemetry": {"deviceMetrics": {"batteryLevel": level, "voltage": volts}}}}


row = lambda nid: next((n for n in br.op_nodes()["nodes"] if n["id"] == nid), {})  # noqa: E731
check("AC1 before any telemetry: the gateway's figure", (row("!aa000001").get("battery"), row("!aa000001").get("charging")), (77, False))
br._on_receive(telem("!aa000001", 64, 3.91), None)
r = row("!aa000001")
check("AC1 telemetry puts level, voltage and a time on the row", (r.get("battery"), r.get("voltage"), bool(r.get("battery_ts"))), (64, 3.91, True))
br._on_receive(telem("!aa000001", 58, 3.85), None)
check("AC1 newest wins, never the highest", row("!aa000001").get("battery"), 58)
br.meshtastic_devices["!bb000002"] = {"long_name": "Two", "meshtastic_id": "!bb000002", "battery": 90}
br.interface.nodes["!bb000002"] = {"user": {"longName": "Two"}, "deviceMetrics": {"batteryLevel": 41, "voltage": 3.7}, "lastHeard": int(time.time())}
check("AC1 without telemetry of its own, the database's deviceMetrics", (row("!bb000002").get("battery"), row("!bb000002").get("voltage")), (41, 3.7))
br.meshtastic_devices["!cc000003"] = {"long_name": "Three", "meshtastic_id": "!cc000003", "battery": 33}
check("AC1 without either, the gateway's figure", row("!cc000003").get("battery"), 33)
br._on_receive(telem("!aa000001", 101, 4.12), None)
r = row("!aa000001")
check("AC2 101 means on charge: charging true, level unknown", (r.get("charging"), r.get("battery"), r.get("voltage")), (True, None, 4.12))
L = next((n for n in br.op_links()["nodes"] if n["id"] == "!aa000001"), {})
check("AC2 links rows carry the same", (L.get("charging"), L.get("battery")), (True, None))

# AC3 no stored radio position
br.interface.position = {"latitude": 54.1, "longitude": -0.3, "locationSource": "LOC_MANUAL"}
br.meshtastic_devices["!aa000001"]["last_lat"] = 0; br.meshtastic_devices["!aa000001"]["last_lon"] = 0
br.conf = {"SERIAL": ""}
check("AC3 a stored radio position is not a source", br.own_position(), None)
br.interface.position = {"latitude": 51.4, "longitude": -1.3, "locationSource": "LOC_INTERNAL", "time": int(time.time()) - 30}
check("AC3 the radio's own fresh GPS fix still is", (br.own_position() or {}).get("source"), "radio_gps")

# the screen
FB.NODES[1]["battery"] = None; FB.NODES[1]["charging"] = True; FB.NODES[1]["voltage"] = 4.1
FB.NODES[0]["battery"] = 58; FB.NODES[0]["battery_ts"] = "2026-09-03T12:00:00Z"
fb = start_fake_bridge()
srv = W.make_server(bind="127.0.0.1", port=0, socket_path=fb.path, etc_dir=tempfile.mkdtemp(), config={"AUTH": "off"})
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start(); time.sleep(0.3)
c = http.client.HTTPConnection("127.0.0.1", port, timeout=10); c.request("GET", "/nodes"); nodes = c.getresponse().read().decode(); c.close()
row_b = nodes[nodes.index("data-id='!bb000002'"):nodes.index("data-id='!cc000003'")]
row_a = nodes[nodes.index("data-id='!aa000001'"):nodes.index("data-id='!bb000002'")]
check_true("AC2 a charging device reads on charge with its voltage, never 101%", "on charge" in row_b and "4.1 V" in row_b and "101" not in row_b)
check_true("AC1 a level shows with its age", "58%" in row_a and "data-age" in row_a.split("58%")[1][:200])
srv.shutdown()
FB.NODES[1].update({"battery": 9}); FB.NODES[1].pop("charging", None); FB.NODES[1].pop("voltage", None)
FB.NODES[0].update({"battery": 77}); FB.NODES[0].pop("battery_ts", None)
finish()
