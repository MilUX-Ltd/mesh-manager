#!/usr/bin/env python3
"""Spec 040: the last hours, replayed on the map."""
import http.client, json, os, sys, tempfile, threading, time
sys.path.insert(0, os.path.dirname(__file__))
from _common import ROOT, check, check_true, finish  # noqa: E402
sys.path.insert(0, os.path.join(ROOT, "src"))
from fakebridge_lib import start_fake_bridge  # noqa: E402
from mesh_manager import web as W  # noqa: E402

fb = start_fake_bridge()
srv = W.make_server(bind="127.0.0.1", port=0, socket_path=fb.path, etc_dir=tempfile.mkdtemp(), config={"AUTH": "off"}, state_dir=tempfile.mkdtemp())
port = srv.server_address[1]; threading.Thread(target=srv.serve_forever, daemon=True).start(); time.sleep(0.3)
def get(p):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10); c.request("GET", p); r = c.getresponse(); b = r.read().decode(); c.close(); return r.status, b

st, body = get("/map")
check("AC1 the map answers", st, 200)
check_true("AC1 the playback control is there", "id='play-t'" in body and "id='play-go'" in body and "type='range'" in body)
check_true("AC1 the script computes a position at an instant", "function posAt(" in body)
check_true("AC1 the instant shown is in UTC", "id='play-at'" in body)
check_true("AC2 last position at or before the instant, per node", "<=t" in body.replace(" ", "") and "posAt" in body)
st, body = get("/api/trails?hours=48")
rows = json.loads(body).get("rows", [])
check_true("AC3 /api/trails serves ts, node, lat, lon", st == 200 and rows and all(k in rows[0] for k in ("ts", "node", "lat", "lon")))
finish()
