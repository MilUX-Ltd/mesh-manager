#!/usr/bin/env python3
"""Spec 026: alerts that reach TAK."""
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
from mesh_manager import bridge as B, web as W  # noqa: E402

state = tempfile.mkdtemp()
br = B.Bridge({"SERIAL": "", "MAP_LAT": "51.2128", "MAP_LON": "-1.5056"}, socket_path=os.path.join(state, "b.sock"), state_dir=state, observe=True, gps_reader=False)
events = []
br._emit = lambda kind, **kw: events.append((kind, kw)) if kind != "log" else None
sock = br.socket_client
# a registered node, heard 40 minutes ago, at 9 percent: the gateway's radio record says when it was heard
old = B.utc(time.time() - 40 * 60)
br.meshtastic_devices["!aa000001"].update({"last_lat": 51.2128, "last_lon": -1.5056})
br._register_save({"!aa000001": {"label": "Recce lead", "holder": "Cpl Smith"}})
br._mesh_radio["!aa000001"] = {"heard": old, "snr": 5.0, "hops": 0}
br._battery_note("!aa000001", 9, 3.4)
events.clear(); before = sock.packets
n = br._judge_alerts()
kinds = sorted(k for k, kw in events if k == "alert" and kw.get("state") == "open" for k in [kw.get("what")])
check("AC1 silent and battery raised once each, TAK told twice", (n, kinds, sock.packets - before), (2, ["battery", "silent"], 2))
check_true("AC2 the GeoChat is a b-t-f with the text and the callsign", sock.last is not None and b"b-t-f" in sock.last and b"Mesh Manager" in sock.last and b"battery 9%" in sock.last and b"All Chat Rooms" in sock.last)
events.clear(); before = sock.packets
check("AC1 a second pass raises nothing new", (br._judge_alerts(), sock.packets - before), (0, 0))
# heard again and charged: both clear
br._mesh_radio["!aa000001"] = {"heard": B.utc(time.time()), "snr": 5.0, "hops": 0}
br._battery_note("!aa000001", 60, 3.9)
events.clear(); br._judge_alerts()
check("AC1 clears when heard and charged", sorted(kw.get("what") for k, kw in events if k == "alert" and kw.get("state") == "cleared"), ["battery", "silent"])
check("AC1 the history keeps the rows, cleared", sorted((r["kind"], r["state"]) for r in br.history.query("alerts")), [("battery", "cleared"), ("silent", "cleared")])
# unknown: once, and not again after a restart
br.meshtastic_devices["!cc000003"] = {"long_name": "Stranger", "meshtastic_id": "!cc000003", "last_lat": 51.2129, "last_lon": -1.5057, "battery": 50}
br._mesh_radio["!cc000003"] = {"heard": B.utc(time.time()), "snr": 3.0, "hops": 0}
events.clear(); br._judge_alerts()
check("AC1 an unregistered node raises unknown once", [kw.get("what") for k, kw in events if k == "alert" and kw.get("state") == "open"], ["unknown"])
br2 = B.Bridge({"SERIAL": "", "MAP_LAT": "51.2128", "MAP_LON": "-1.5056"}, socket_path=os.path.join(state, "b2.sock"), state_dir=state, observe=True, gps_reader=False)
ev2 = []; br2._emit = lambda kind, **kw: ev2.append((kind, kw)) if kind != "log" else None
br2.meshtastic_devices["!cc000003"] = dict(br.meshtastic_devices["!cc000003"])
br2._mesh_radio["!cc000003"] = {"heard": B.utc(time.time()), "snr": 3.0, "hops": 0}
br2._judge_alerts()
check("AC1 not again after a restart", [kw.get("what") for k, kw in ev2 if k == "alert" and kw.get("state") == "open" and kw.get("what") == "unknown"], [])
# fence: 500 m, a node 2 km out
check("AC3 settings persist and refuse nonsense", (br.op_alert_set(fence_m=500, battery_pct=15)["written"]["fence_m"], br.op_alert_settings()["battery_pct"], "error" in br.op_alert_set(silent_min=0), "error" in br.op_alert_set(battery_pct=95)), (500, 15, True, True))
br.meshtastic_devices["!cc000003"].update({"last_lat": 51.2308, "last_lon": -1.5056})
events.clear(); br._judge_alerts()
check("AC1 a node 2 km out raises fence", [kw.get("what") for k, kw in events if k == "alert" and kw.get("state") == "open"], ["fence"])
br.meshtastic_devices["!cc000003"].update({"last_lat": 51.2129, "last_lon": -1.5057})
events.clear(); br._judge_alerts()
check("AC1 back inside clears it", [kw.get("what") for k, kw in events if k == "alert" and kw.get("state") == "cleared"], ["fence"])
# to_tak off sends nothing; the test sends one
br.op_alert_set(to_tak="off"); br._alerts_load()
before = sock.packets
br._raise_alert(br._alerts_load(), "!aa000001", "battery", "quiet one")
check("AC2 to_tak off sends nothing", sock.packets - before, 0)
before = sock.packets
r = br.op_alert_test()
check("AC2 alert_test sends one, counted in observe mode", (r.get("sent"), sock.packets - before, b"test alert" in (sock.last or b"")), (True, 1, True))
check("AC3 status carries the open count", br.op_status().get("alerts_open") >= 1, True)

fb = start_fake_bridge()
srv = W.make_server(bind="127.0.0.1", port=0, socket_path=fb.path, etc_dir=tempfile.mkdtemp(), config={"AUTH": "off"})
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start(); time.sleep(0.3)
def get(p):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10); c.request("GET", p); r = c.getresponse(); b = r.read().decode(); c.close(); return r.status, b
s, page = get("/health")
check_true("AC3 the Health page shows the open alert, the recent one, the form and the test", s == 200 and "Tracker9 battery 9%" in page and "Tracker2 silent for 45 min" in page and "data-action='alert_set'" in page and "data-action='alert_test'" in page)
check_true("AC3 the strip shows the open count", "1 alert<" in page)
s, frag = get("/fragment/alerts")
check_true("AC3 the alerts fragment", s == 200 and "id='alerts'" in frag)
srv.shutdown()
finish()
