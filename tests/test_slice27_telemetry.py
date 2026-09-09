#!/usr/bin/env python3
"""Spec 025: telemetry over time."""
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
from mesh_manager import web as W  # noqa: E402

# the fake's history is dated 3 Sep 2026; the page asks for a window from now, so stamp the rows recent
now = time.time()
for i, r in enumerate(FB.HISTORY["telemetry"]):
    r["ts"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - (4 - i) * 1800))
for r in FB.HISTORY["messages"]:
    r["ts"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 600))
fb = start_fake_bridge()
srv = W.make_server(bind="127.0.0.1", port=0, socket_path=fb.path, etc_dir=tempfile.mkdtemp(), config={"AUTH": "off"})
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start(); time.sleep(0.3)
def get(p):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10); c.request("GET", p); r = c.getresponse(); b = r.read().decode(); c.close(); return r.status, b
s, page = get("/node?id=!aa000001")
check_true("AC1 the node page renders the facts", s == 200 and "Tracker9" in page and "!aa000001" in page and "Positions in the window" in page)
check_true("AC1 both charts from the telemetry rows, with the 20 percent line", page.count("<svg class='chart'") == 2 and "20%" in page and "battery over time" in page and "voltage over time" in page)
check_true("AC1 the last messages", "stored before the restart" in page)
s, one = get("/node?id=!bb000002")
check_true("AC1 one reading says there is not enough for a chart", s == 200 and "Not enough readings yet" in one)
check("AC1 an unknown id is a 404", get("/node?id=!ffffffff")[0], 404)
check("AC1 a malformed id is a 404", get("/node?id=../etc")[0], 404)
s, nodes = get("/nodes")
check_true("AC2 the Nodes table links each name to its page", s == 200 and "href='/node?id=!aa000001'" in nodes)
check_true("AC2 the window select offers 24 h and 7 d", "value='24' selected" in page and "value='168'" in page)
s, week = get("/node?id=!aa000001&hours=168")
check_true("AC2 the 7 d window is honoured", s == 200 and "value='168' selected" in week)
srv.shutdown()
finish()
