#!/usr/bin/env python3
"""Spec 016: display names."""
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
br.op_register_set(id="!aa000001", label="Recce lead")
n = next(x for x in br.op_nodes()["nodes"] if x["id"] == "!aa000001")
check("AC1 nodes rows carry the label", (n.get("label"), n.get("name")), ("Recce lead", "Tracker9"))
n = next(x for x in br.op_links()["nodes"] if x["id"] == "!aa000001")
check("AC1 links rows carry the label", n.get("label"), "Recce lead")
br.meshtastic_devices["!bb000002"] = {"long_name": "Meshtastic 0002", "meshtastic_id": "!bb000002"}
n = next(x for x in br.op_nodes()["nodes"] if x["id"] == "!bb000002")
check("AC1 a node without a label carries an empty one", n.get("label"), "")
check("AC1 route hop names prefer the label", br._node_name("!aa000001"), "Recce lead")

FB.NODES[0]["label"] = "Recce lead"
fb = start_fake_bridge()
srv = W.make_server(bind="127.0.0.1", port=0, socket_path=fb.path, etc_dir=tempfile.mkdtemp(), config={"AUTH": "off"})
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start(); time.sleep(0.3)


def get(path):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10); c.request("GET", path); r = c.getresponse(); b = r.read().decode(); c.close()
    return b


nodes = get("/nodes")
row = nodes[nodes.index("data-id='!aa000001'"):nodes.index("data-id='!bb000002'")]
# 5 Sep 2026 UX reviews: the node link carries the product's own tip, not a native title
check_true("AC2 the label in the name position, the radio's own name in the second line", "data-tip='This node over time' data-tip-more='Battery, voltage, hours heard and messages'>Recce lead</a></b>" in row and "Tracker9" in row)
row2 = nodes[nodes.index("data-id='!bb000002'"):nodes.index("data-id='!cc000003'")]
check_true("AC2 a node without a label shows the radio's own name", "data-tip-more='Battery, voltage, hours heard and messages'>Tracker2</a></b>" in row2)
frag = get("/fragment/map")
check_true("AC2 the plan view uses the label", ">Recce lead<" in frag)
home = get("/")
check_true("AC2 the overlay prefers the label", "n.label||n.name" in home or "n.label || n.name" in home)
check_true("AC3 a Name control on the row posts register_set", "data-action='register_set'" in row and "name='label'" in row)
srv.shutdown()
FB.NODES[0].pop("label", None)
finish()
