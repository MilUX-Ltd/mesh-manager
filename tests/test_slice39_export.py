#!/usr/bin/env python3
"""Spec 037: the history as a file."""
import http.client, os, sys, tempfile, threading, time
import xml.etree.ElementTree as ET
sys.path.insert(0, os.path.dirname(__file__))
from _common import ROOT, check, check_true, finish  # noqa: E402
sys.path.insert(0, os.path.join(ROOT, "src"))
from fakebridge_lib import start_fake_bridge, HISTORY  # noqa: E402
import fakebridge_lib as _fb  # noqa: E402
from mesh_manager import web as W  # noqa: E402

fb = start_fake_bridge()
srv = W.make_server(bind="127.0.0.1", port=0, socket_path=fb.path, etc_dir=tempfile.mkdtemp(), config={"AUTH": "off"}, state_dir=tempfile.mkdtemp())
port = srv.server_address[1]; threading.Thread(target=srv.serve_forever, daemon=True).start(); time.sleep(0.3)

def get(path):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10); c.request("GET", path); r = c.getresponse()
    body = r.read(); hdr = dict(r.getheaders()); c.close(); return r.status, body, hdr

# ---- AC1 GPX ----------------------------------------------------------------------------------------
st, body, hdr = get("/export/positions.gpx?hours=48")
check("AC1 GPX answers", st, 200)
check_true("AC1 served as an attachment named for the kind", "attachment" in hdr.get("Content-Disposition", "") and "positions" in hdr.get("Content-Disposition", ""))
root = ET.fromstring(body)
ns = {"g": root.tag.split("}")[0].strip("{")} if "}" in root.tag else {}
trks = root.findall("g:trk", ns) if ns else root.findall("trk")
check("AC1 one track per node with points", (len(trks), len(trks[0].findall(".//g:trkpt", ns) if ns else trks[0].findall(".//trkpt")) if trks else 0), (1, 2))
check_true("AC1 timestamps are carried", ("<time>%s</time>" % _fb.at("2026-09-03T21:50:00Z")).encode() in body)   # the fixture is shifted onto now

# ---- AC2 KML ---------------------------------------------------------------------------------------
st, body, hdr = get("/export/positions.kml?hours=48")
check("AC2 KML answers and parses", (st, ET.fromstring(body) is not None), (200, True))
check_true("AC2 one placemark per node, coordinates to six decimals", body.count(b"<Placemark>") == 1 and b"-1.500000,51.200000,0" in body)

# ---- AC3 CSV ----------------------------------------------------------------------------------------
st, body, hdr = get("/export/messages.csv?hours=48")
lines = body.decode().splitlines()
check("AC3 CSV with a header and the row", (st, lines[0].split(",")[:3], "stored before the restart" in lines[1]), (200, ["ts", "node", "name"], True))
check_true("AC3 text/csv", "csv" in hdr.get("Content-Type", ""))

# ---- AC4 refusals and filters -----------------------------------------------------------------------
check("AC4 an unknown kind is 404", get("/export/nonsense.csv")[0], 404)
check("AC4 an unknown format is 404", get("/export/positions.pdf")[0], 404)
st, body, _ = get("/export/positions.csv?hours=48&node=!bb000002")
check("AC4 the node filter applies", (st, len(body.decode().splitlines())), (200, 1))

# ---- AC5 the page ---------------------------------------------------------------------------------------
st, body, _ = get("/health")
check_true("AC5 Health offers Export", st == 200 and b"/export/" in body and b"GPX" in body)
finish()
