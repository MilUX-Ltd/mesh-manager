#!/usr/bin/env python3
"""Spec 008: the map and the link bar. The bridge half runs on the fake gateway, the screen half
on the fake bridge."""
import http.client
import json
import os
import re
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(__file__))
from _common import read, ROOT, check, check_true, finish  # noqa: E402
sys.path.insert(0, os.path.join(ROOT, "src"))
import fakegw_lib  # noqa: E402
fakegw_lib.install()
from fakebridge_lib import start_fake_bridge, LINKS  # noqa: E402
from mesh_manager import bridge as B, catalogue as C, web as W  # noqa: E402

# ---- AC1 the catalogue
links, route = C.by_id("links"), C.by_id("route")
check_true("AC1 links is a read action", bool(links) and links["risk"] == "read")
check_true("AC1 route is a read action taking a node id", bool(route) and route["risk"] == "read" and any(i["type"] == "node" for i in route["inputs"]))
check("AC1 parity across routes, forms and tools holds", C.parity_problems(C.ACTIONS, W.api_action_routes(), [t["name"] for t in W.mcp_tools("act")]), [])

# ---- the bridge half
tmp = tempfile.mkdtemp()
br = B.Bridge({"SERIAL": "", "MAP_LAT": "51.2", "MAP_LON": "-1.5"}, socket_path=os.path.join(tmp, "b.sock"), state_dir=tmp, observe=True)
events = []
_orig_emit = br._emit
br._emit = lambda kind, **f: (events.append((kind, f)), _orig_emit(kind, **f))[1]


def pkt(fr, snr, hops, port="POSITION_APP"):
    return {"fromId": fr, "toId": "^all", "rxSnr": snr, "hopStart": 3, "hopLimit": 3 - hops, "decoded": {"portnum": port}}


for p in (pkt("!aa000001", 12.5, 0), pkt("!aa000001", 3.0, 1), pkt("!aa000001", 8.0, 0)):
    br._on_receive(p, None)
L = br.op_links() if hasattr(br, "op_links") else {}
n = next((x for x in L.get("nodes", []) if x.get("id") == "!aa000001"), {})
check("AC2 three packets leave a history of three", len(n.get("history") or []), 3)
check("AC2 direct_snr is the last packet that came with no hops", n.get("direct_snr"), 8.0)
check("AC2 history rows are (ts, snr, hops)", ((n.get("history") or []) + [[None, None, None]] * 2)[1][1:], [3.0, 1])
for i in range(260):
    br._on_receive(pkt("!aa000001", 5.0, 0), None)
n = next((x for x in br.op_links().get("nodes", []) if x.get("id") == "!aa000001"), {}) if hasattr(br, "op_links") else {}
check("AC2 the history never grows past 200", len(n.get("history") or []), 200)
br.meshtastic_devices["!dd000004"] = {"long_name": "Old", "meshtastic_id": "!dd000004"}
d = next((x for x in br.op_links().get("nodes", []) if x.get("id") == "!dd000004"), None) if hasattr(br, "op_links") else None
check("AC2 a database-only node has no history and no direct link", (d or {}).get("heard_here"), False)
check("AC2 ...and an empty history", (d or {}).get("history"), [])

# AC3 traceroute without blocking, the answer as a route
t0 = time.time()
r = br.op_traceroute(dest="!aa000001")
took = time.time() - t0
check_true("AC3 traceroute answers at once", took < 0.5 and bool(r.get("asked")))
check("AC3 the library's blocking sendTraceRoute is never used", br.interface.traces, [])
sent = br.interface.data[-1] if br.interface.data else {}
from meshtastic.protobuf import portnums_pb2
check("AC3 sendData carried the traceroute port with a response handler", (sent.get("portNum"), sent.get("wantResponse"), callable(sent.get("onResponse"))), (portnums_pb2.PortNum.TRACEROUTE_APP, True, True))
resp = {"from": 0xaa000001, "to": 1, "fromId": "!aa000001", "toId": "!00000001", "hopStart": 3, "hopLimit": 2, "rxSnr": 7.25,
        "decoded": {"portnum": "TRACEROUTE_APP", "requestId": 1,
                    "traceroute": {"route": [0xbb000002], "snrTowards": [12, 29], "routeBack": [0xbb000002], "snrBack": [10, -128]}}}
if callable(sent.get("onResponse")):
    sent["onResponse"](resp)
rt = br.op_route(id="!aa000001").get("route") if hasattr(br, "op_route") else None
check("AC3 the route's towards hops carry ids, names and quarter-dB figures", [(h.get("id"), h.get("name"), h.get("snr")) for h in (rt or {}).get("towards", [])],
      [("!bb000002", "!bb000002", 3.0), ("!aa000001", "Tracker9", 7.25)])
check("AC3 the back hops end here, -128 as null", [(h.get("id"), h.get("snr")) for h in (rt or {}).get("back", [])], [("!bb000002", 2.5), ("!00000001", None)])
check("AC3 hops counted", (rt or {}).get("hops"), 1)
check_true("AC3 a route event was emitted for the node", any(k == "route" and f.get("dest") == "!aa000001" for k, f in events))
check_true("AC3 op_links carries the route", "!aa000001" in (br.op_links().get("routes") or {}))

# AC4 own position: the declared position stands when no receiver and no radio fix (the full order is Spec 014's suite)
br.interface.position = None
check("AC4 the declared position when nothing better", ((br.own_position() or {}).get("source"), (br.own_position() or {}).get("lat")), ("config", 51.2))
check("AC4 op_links says which", (br.op_links().get("own") or {}).get("position_source"), "config")

# ---- the screen half
fb = start_fake_bridge()
etc = tempfile.mkdtemp()
srv = W.make_server(bind="127.0.0.1", port=0, socket_path=fb.path, etc_dir=etc, config={"AUTH": "off"})
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
time.sleep(0.4)


def req(method, path, body=None, ctype="application/x-www-form-urlencoded"):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    h = {"Content-Type": ctype} if body is not None else {}
    c.request(method, path, body=body, headers=h)
    r = c.getresponse(); data = r.read().decode("utf-8", "replace"); c.close()
    return r.status, data


st, home = req("GET", "/")
st_map, mappage = req("GET", "/map")
st_frag, frag = req("GET", "/fragment/map")
check_true("AC5 the Mesh page carries the map", "<svg class='map'" in home)
check_true("AC5 /map is a page with the map", st_map == 200 and "<svg class='map'" in mappage)
check_true("AC5 /fragment/map answers the map", st_frag == 200 and frag.strip().startswith("<svg class='map'"))
check_true("AC5 the box is marked", "data-own='!00000001'" in frag)
check_true("AC5 a circle per heard node keyed by id", "data-id='!aa000001'" in frag and "data-id='!bb000002'" in frag)
check_true("AC5 no circle for a database-only node", "data-id='!cc000003'" not in frag)
check_true("AC5 a direct link carries its band and the figure", re.search(r"<line[^>]*class='link band-4'", frag) is not None and "12.5 dB" in frag)
check_true("AC5 a relayed-only node's link is dashed and unlabelled", re.search(r"<line[^>]*class='link relayed'", frag) is not None)
check_true("AC5 a node without a position sits on the outer ring", "data-pos='none'" in frag)
check_true("AC5 the legend disclaims propagation", "geometry, not propagation" in frag)
check_true("AC5 the stored route is drawn hop to hop", "class='route" in frag)

st, bar = req("GET", "/fragment/route/!aa000001")
check_true("AC6 the link bar has a segment per hop with its band", bar.count("class='hop band-") >= 2)
check_true("AC6 ...with names and figures both ways", "Tracker2" in bar and "7.25 dB" in bar and "2.5 dB" in bar and "back" in bar.lower())
check_true("AC6 an unknown SNR is shown as unknown, never a number", "?" in bar or "unknown" in bar)
st, none = req("GET", "/fragment/route/!bb000002")
check_true("AC6 a node with no route says so", "no route asked for yet" in none)

st, nodes = req("GET", "/nodes")
row_a = nodes[nodes.index("data-id='!aa000001'"):nodes.index("data-id='!bb000002'")]
row_c = nodes[nodes.index("data-id='!cc000003'"):]
check_true("AC7 a node with history carries a sparkline and the figures", "<svg class='spark'" in row_a and "best" in row_a and "worst" in row_a)
check_true("AC7 a node without history carries none", "<svg class='spark'" not in row_c.split("</tr>")[0])

for p, html in (("/", home), ("/map", mappage), ("/nodes", nodes)):
    check_true(f"AC8 {p} has no location.reload", "location.reload(" not in html)
check_true("AC8 the Mesh page patches the map on packet and route events", "mmFrag('map'" in home and "'route'" in home)

st, j = req("GET", "/api/links")
check_true("AC9 the fake bridge answers links through the API", st == 200 and "nodes" in json.loads(j))
st, j = req("GET", "/api/route?id=!aa000001")
check("AC9 ...and route", (st, (json.loads(j).get("route") or {}).get("dest")), (200, "!aa000001"))
srv.shutdown()
# 0.20.2, Matt: "the app should load to map, not plan". The plan used to stick because the automatic fall back,
# taken when a position had not arrived yet, was written to storage as though the person had chosen it.
_page = read("src/mesh_manager/web.py") or ""
check_true("0.20.2 only a deliberate click is remembered", "show(b.dataset.view,true)" in _page and "if(chose){try{localStorage.setItem('mm-view-choice'" in _page)
check_true("0.20.2 the automatic fall back does not remember", "show('plan');return;" in _page and "localStorage.setItem('mm-view'," not in _page)
check_true("0.20.2 the key written by the old behaviour is dropped on sight", "localStorage.removeItem('mm-view')" in _page)
# 0.20.2 opened on the map only when the box knew where it was. Matt, 6 Sep 2026: "app does not default to map
# view when loading. it should do, i asked for it some iterations ago." Spec 068: the map is always the default,
# and a box with no position gets a real map with words on it rather than an empty box.
check_true("Spec 068 the map is what this opens on, position or not", "data-default-view='map'" in _page)

finish()
