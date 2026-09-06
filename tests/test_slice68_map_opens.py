#!/usr/bin/env python3
"""Spec 068: the map always opens, a box with no position still gets one, and local maps live behind a chooser."""
import os, sys, tempfile, threading, time, http.client
sys.path.insert(0, os.path.dirname(__file__))
from _common import ROOT, check, check_true, finish, read  # noqa: E402
sys.path.insert(0, os.path.join(ROOT, "src"))
import fakebridge_lib as FB  # noqa: E402
from fakebridge_lib import start_fake_bridge  # noqa: E402
from mesh_manager import web as W  # noqa: E402


def serve():
    fb = start_fake_bridge()
    srv = W.make_server(bind="127.0.0.1", port=0, socket_path=fb.path, etc_dir=tempfile.mkdtemp(),
                        config={"AUTH": "off"}, state_dir=tempfile.mkdtemp())
    threading.Thread(target=srv.serve_forever, daemon=True).start(); time.sleep(0.3)
    return srv.server_address[1]


def get(port, path):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10); c.request("GET", path)
    r = c.getresponse(); b = r.read().decode(); c.close(); return r.status, b


# AC1: with a position
st, page = get(serve(), "/")
check("the screen answers", st, 200)
check_true("AC1 the map is the default view", "data-default-view='map'" in page)

# AC1 to AC4: with no position at all, which is what a hub is
saved = dict(FB.LINKS["own"])
FB.LINKS["own"].update({"lat": None, "lon": None, "position_source": None})
try:
    st, hub = get(serve(), "/")
    check_true("AC1 a box with no position still opens on the map", "data-default-view='map'" in hub)
    check_true("AC4 and it has somewhere to say why", "id='map-empty'" in hub)
finally:
    FB.LINKS["own"].clear(); FB.LINKS["own"].update(saved)

src = read("src/mesh_manager/web.py") or ""
check_true("AC2 the map is built whether or not the box has a position",
           "if(!window.L){show('plan');return;}" in src and "if(!has||!window.L)" not in src)
check_true("AC2 and a box with nothing positioned is still given a view", "map.setView([54.0,-2.5],5)" in src)
check_true("AC3 nodes with positions are drawn without one of our own", "function drawWithoutOwn" in src and "drawWithoutOwn(J);return;" in src)
check_true("AC4 the words name where to set the box's position", "Where this box is" in src and "/settings" in src)

# AC5 and AC6: the layer list and the chooser
check_true("AC5 only the internet sources go in the list on the map", "if(s.internet){byName[s.name||s.id]=l;}" in src)
check_true("AC5 with one entry that opens the chooser", "Load a local map" in src and "openLocals" in src)
check_true("AC6 the chooser is a dialog listing the box's own sets", "id='local-maps'" in src and "local-list" in src)
check_true("AC6 and says where they go when there are none", "carries no map sets of its own yet" in src)
finish()
