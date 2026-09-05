#!/usr/bin/env python3
"""Spec 042: who hears whom."""
import http.client, os, sys, tempfile, threading, time
sys.path.insert(0, os.path.dirname(__file__))
from _common import ROOT, check, check_true, finish  # noqa: E402
sys.path.insert(0, os.path.join(ROOT, "src"))
import fakegw_lib  # noqa: E402
fakegw_lib.install()
from meshtastic.protobuf import mesh_pb2  # noqa: E402
from mesh_manager import bridge as B, catalogue as C, web as W  # noqa: E402
import fakebridge_lib as FB  # noqa: E402

state = tempfile.mkdtemp()
br = B.Bridge({"SERIAL": ""}, socket_path=os.path.join(state, "b.sock"), state_dir=state, observe=True, gps_reader=False)
br.interface.nodes["!aa000001"] = {"user": {"longName": "Alpha"}, "lastHeard": int(time.time())}
br.interface.nodes["!bb000002"] = {"user": {"longName": "Bravo"}, "lastHeard": int(time.time())}
ni = mesh_pb2.NeighborInfo(node_id=0xaa000001)
n1 = ni.neighbors.add(); n1.node_id = 0xbb000002; n1.snr = 7.5
n2 = ni.neighbors.add(); n2.node_id = 0x00000001; n2.snr = 11.0
br._on_receive({"fromId": "!aa000001", "toId": "^all", "rxSnr": 5.0, "hopStart": 3, "hopLimit": 3,
                "decoded": {"portnum": "NEIGHBORINFO_APP", "payload": ni.SerializeToString()}}, br.interface)
rows = br.history.query("neighbors")
check("AC1 one edge per neighbour with snr", sorted((r.get("neighbor"), r.get("snr")) for r in rows), [("!00000001", 11.0), ("!bb000002", 7.5)])
out = br.op_neighbors(hours=24)
edges = out.get("edges", [])
check_true("AC2 edges carry names on both ends", any(e.get("from_name") == "Alpha" and e.get("to_name") == "Bravo" for e in edges))
check_true("AC5 neighbors is a read", (C.by_id("neighbors") or {}).get("risk") == "read")

# ---- the page -------------------------------------------------------------------------------------------
fb = FB.start_fake_bridge()
srv = W.make_server(bind="127.0.0.1", port=0, socket_path=fb.path, etc_dir=tempfile.mkdtemp(), config={"AUTH": "off"}, state_dir=tempfile.mkdtemp())
port = srv.server_address[1]; threading.Thread(target=srv.serve_forever, daemon=True).start(); time.sleep(0.3)
def get(p):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10); c.request("GET", p); r = c.getresponse(); b = r.read().decode(); c.close(); return r.status, b
st, body = get("/graph")
check("AC3 the page answers", st, 200)
check_true("AC5 on the More menu", ("/graph", "Graph") in W.NAV_MORE)
check_true("AC3 an SVG with a node per id and an edge per pair", "<svg" in body and "data-edge=" in body and "data-node='!aa000001'" in body and "data-node='!bb000002'" in body)
FB.NEIGHBORS = {"edges": []}
st, body = get("/graph")
check_true("AC4 with no edges it says the module is off and how to turn it on", "neighbour" in body.lower() and "node_set" in body)
finish()
