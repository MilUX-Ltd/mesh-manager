#!/usr/bin/env python3
"""Spec 021: track trails."""
import http.client
import json
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
import fakebridge_lib as _fb  # noqa: E402
from mesh_manager import web as W  # noqa: E402

fb = start_fake_bridge()
srv = W.make_server(bind="127.0.0.1", port=0, socket_path=fb.path, etc_dir=tempfile.mkdtemp(), config={"AUTH": "off"})
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start(); time.sleep(0.3)
def get(p):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10); c.request("GET", p); r = c.getresponse(); b = r.read().decode(); c.close(); return r.status, b
s, body = get("/api/history?kind=positions&limit=2")
j = json.loads(body)
check("AC1 a read action takes query arguments: positions through the catalogue", (s, j.get("kind"), len(j.get("rows") or [])), (200, "positions", 2))
s, body = get("/api/history?kind=positions&node=!aa000001&since=" + _fb.at("2026-09-03T21:52:00Z"))   # the fixture rides the present
check("AC1 node and since filter", (s, [r["lat"] for r in json.loads(body)["rows"]]), (200, [51.2004]))
s, mp = get("/map")
check_true("AC2 the trails control with its windows", s == 200 and "id='trail-hours'" in mp and "value='24'" in mp and "option value='0'>off" in mp)
check_true("AC2 the overlay fetches positions since a window and fades by age", "kind=positions&since=" in mp and "0.25+0.75*" in mp and "trails_" in mp and "d>2000" in mp)
s, full = get("/map/full")
check_true("AC2 the map of its own has the trails too", s == 200 and "id='trail-hours'" in full)
srv.shutdown()
finish()
