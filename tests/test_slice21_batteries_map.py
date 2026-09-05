#!/usr/bin/env python3
"""Spec 019: batteries that are current, rings that follow the zoom, a map of its own."""
import http.client
import json
import os
import re
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
br = B.Bridge({"SERIAL": ""}, socket_path=os.path.join(state, "b.sock"), state_dir=state, observe=True, gps_reader=False)
row = lambda nid: next((n for n in br.op_nodes()["nodes"] if n["id"] == nid), {})  # noqa: E731
now = int(time.time())

# AC1 the database is a fallback only for a node heard in the last day
br.meshtastic_devices["!bb000002"] = {"long_name": "Two", "meshtastic_id": "!bb000002", "battery": 0}
br.interface.nodes["!bb000002"] = {"user": {"longName": "Two"}, "deviceMetrics": {"batteryLevel": 41, "voltage": 3.7}, "lastHeard": now - 3 * 86400}
check("AC1 a figure from a node heard three days ago is not shown", (row("!bb000002").get("battery"), row("!bb000002").get("voltage")), (None, None))
br.interface.nodes["!bb000002"]["lastHeard"] = now - 3600
r = row("!bb000002")
check("AC1 heard an hour ago: shown, with that time as its age", (r.get("battery"), r.get("voltage"), r.get("battery_ts") == B.utc(now - 3600)), (41, 3.7, True))

# AC2 the store survives a restart
emitted = []
br._emit = lambda kind, **kw: emitted.append((kind, kw))
br._battery_note("!aa000001", 64, 3.91)
check_true("AC2 the battery is on disk", os.path.exists(os.path.join(state, "batteries.json")) and json.load(open(os.path.join(state, "batteries.json")))["!aa000001"]["level"] == 64)
br2 = B.Bridge({"SERIAL": ""}, socket_path=os.path.join(state, "b2.sock"), state_dir=state, observe=True, gps_reader=False)
check("AC2 a new bridge on the same state directory starts with it", br2.batteries.get("!aa000001", {}).get("level"), 64)
check("AC2 noting a battery emits a telemetry event", [k for k, _ in emitted if k != "log"], ["telemetry"])

# AC3 ask for telemetry, the answer to a handler
from meshtastic.protobuf import portnums_pb2, telemetry_pb2  # noqa: E402
emitted.clear()
out = br.op_request_telemetry(dest="!aa000001")
sent = br.interface.data[-1]
check("AC3 the request is a TELEMETRY_APP sendData with wantResponse and a handler", (out.get("requested"), sent["portNum"] == portnums_pb2.PortNum.TELEMETRY_APP, sent["wantResponse"], callable(sent["onResponse"])), ("telemetry", True, True, True))
t = telemetry_pb2.Telemetry(); t.device_metrics.battery_level = 58; t.device_metrics.voltage = 3.85
sent["onResponse"]({"fromId": "!aa000001", "decoded": {"payload": t.SerializeToString()}})
check("AC3 the answer updates the store and the row", (br.batteries["!aa000001"]["level"], row("!aa000001").get("battery"), row("!aa000001").get("voltage")), (58, 58, 3.85))
check("AC3 and goes out as a telemetry event", [(k, v.get("battery"), v.get("voltage")) for k, v in emitted if k != "log"], [("telemetry", 58, 3.85)])
t2 = telemetry_pb2.Telemetry(); t2.device_metrics.battery_level = 101; t2.device_metrics.voltage = 4.1
sent["onResponse"]({"fromId": "!aa000001", "decoded": {"payload": t2.SerializeToString()}})
tel = [v for k, v in emitted if k == "telemetry"]
check("AC3 an answer above 100 is on charge", (tel[-1].get("charging"), tel[-1].get("battery")), (True, None))
own_id = (br._own() or {}).get("id")
br.interface.nodes["!aa000001"] = {"user": {"longName": "One"}, "lastHeard": now - 60}
br.interface.nodes["!cc000003"] = {"user": {"longName": "Three"}, "lastHeard": now - 3 * 86400}
if own_id:
    br.interface.nodes[own_id] = {"user": {"longName": "me"}, "lastHeard": now}
before = len(br.interface.data)
asked = br._telemetry_pass(stagger=0)
check("AC3 the automatic pass asks the node heard today, not the stale one or the box", (sorted(asked), len(br.interface.data) - before), (["!aa000001", "!bb000002"], 2))

# AC4 forget the stale
br.interface.localNode.calls.clear()
br.interface.nodes["!dd000004"] = {"user": {"longName": "Old"}, "lastHeard": now - 10 * 86400}
res = br.op_nodes_forget_stale(days=7)
check("AC4 the ten-day-old node is forgotten; the ones heard today and three days ago are kept", (sorted(res.get("forgotten") or []), sorted(x for x in (res.get("kept") or []) if x in ("!aa000001", "!cc000003")), ("removeNode", "!dd000004") in br.interface.localNode.calls, ("removeNode", "!aa000001") in br.interface.localNode.calls), (["!dd000004"], ["!aa000001", "!cc000003"], True, False))

# AC5 the screen
check("AC5 ring_step", (W.ring_step(75), W.ring_step(500), W.ring_step(3500), W.ring_step(20)), (20, 100, 1000, 5))
fb = start_fake_bridge()
srv = W.make_server(bind="127.0.0.1", port=0, socket_path=fb.path, etc_dir=tempfile.mkdtemp(), config={"AUTH": "off"})
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start(); time.sleep(0.3)
def get(p):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10); c.request("GET", p); r = c.getresponse(); b = r.read().decode(); c.close(); return r.status, b
s, full = get("/map/full")
check_true("AC5 /map/full is the map with no header", s == 200 and "id='map-geo'" in full and "<header" not in full and "class='bare'" in full and "id='map-pop'" not in full)
s, mp = get("/map")
# 5 Sep 2026 UX reviews: the rings are a three-way switch, not a slider, and Pop out is an icon with a name
check_true("AC5 /map has Pop out and the rings switch", s == 200 and "id='map-pop'" in mp and "name='rings'" in mp and "zoomend" in mp and "niceStep" in mp)
check_true("AC7 the map waits for a size before it fits and refits on resize; centre on me is a one-kilometre view (0.2.12)", "function sized()" in mp and "ResizeObserver" in mp and "visibilitychange" in mp and "toBounds(1000)" in mp and "map-centre" in mp and "!fitted&&sized()" in mp)
check_true("AC6 each ring is a gold line over a deep-green halo (0.2.8)", "color:tok('--gold'),weight:2" in mp and "color:tok('--accent'),weight:4" in mp)
s, nodes = get("/nodes")
check_true("AC5 the row offers Ask for a battery", "data-action='request_telemetry'" in nodes and "mmTelemetry" in nodes)
# 4 Sep 2026: adding an air action with a node input silently put a fifth, text-labelled button
# on every row and broke the layout. The row carries icons, and only icons.
_row = nodes[nodes.index("data-id='!aa000001'"):nodes.index("data-id='!bb000002'")]
_asks = _row[_row.index("row-actions"):_row.index("</div>", _row.index("row-actions"))]
# The count is deliberate: an air action with one node input lands here automatically, so a new
# one has to be added to this list on purpose. Spec 032 added the fourth (4 Sep 2026).
_expected = ["traceroute", "request_position", "request_telemetry", "request_nodeinfo"]
check("AC5 the row's asks are exactly the air actions meant to be there, each an icon button",
      (sorted(re.findall(r"<button[^>]*class='line icon'[^>]*data-action='([a-z_]+)'", _asks)), _asks.count("class='line icon'"),
       sum(1 for b in re.findall(r"<button[^>]*class='line icon'[^>]*>(.*?)</button>", _asks, re.S) if "<svg" in b)),   # Spec 044: the fold beside them carries the icon picker's own glyphs
      (sorted(_expected), 4, 4))
# 5 Sep 2026 UX reviews: every icon button carries a hidden word the labels switch reveals, so the check is that no ask is a plain word button
check_true("AC5 no ask on the row carries a text label instead of an icon", "Start a coverage survey" not in nodes and "data-action='survey_start'" not in nodes and re.search(r"<button[^>]*>Traceroute<", _asks) is None)
check_true("AC5 the icon buttons carry an instant tooltip (0.2.11)", "data-tip='Ask for a battery'" in nodes and "data-tip-more=" in nodes and "setAttribute('role','tooltip')" in nodes and "title='Ask for a battery" not in nodes)
s, reg = get("/register")
check_true("AC5 the Register page offers Forget the stale", "data-action='nodes_forget_stale'" in reg)
srv.shutdown()
finish()
