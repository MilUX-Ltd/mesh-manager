#!/usr/bin/env python3
"""Spec 013: the map overlay. Static files, tilesets from MBTiles, the two views, the installer."""
import http.client
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(__file__))
from _common import ROOT, check, check_true, finish  # noqa: E402
sys.path.insert(0, os.path.join(ROOT, "src"))
from fakebridge_lib import start_fake_bridge  # noqa: E402
from mesh_manager import web as W  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40


def mbtiles(path, fmt, tiles):
    c = sqlite3.connect(path)
    c.execute("create table metadata (name text, value text)")
    c.execute("create table tiles (zoom_level integer, tile_column integer, tile_row integer, tile_data blob)")
    for k, v in (("name", os.path.basename(path)[:-8]), ("format", fmt), ("bounds", "-1.85,51.02,-1.08,51.30"), ("minzoom", "1"), ("maxzoom", "1"),
                 ("attribution", "Contains OS data (c) Crown copyright 2026, OGL v3")):
        c.execute("insert into metadata values (?, ?)", (k, v))
    for z, col, row, data in tiles:
        c.execute("insert into tiles values (?, ?, ?, ?)", (z, col, row, data))
    c.commit(); c.close()


tdir = tempfile.mkdtemp()
mbtiles(os.path.join(tdir, "andover-test.mbtiles"), "png", [(1, 0, 0, PNG)])          # TMS row 0 at z=1 is XYZ y=1
mbtiles(os.path.join(tdir, "roads.mbtiles"), "pbf", [(1, 0, 0, b"\x1f\x8b")])


def serve(config):
    fb = start_fake_bridge()
    srv = W.make_server(bind="127.0.0.1", port=0, socket_path=fb.path, etc_dir=tempfile.mkdtemp(), config=config)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.3)
    return srv, srv.server_address[1]


def get(port, path):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10); c.request("GET", path); r = c.getresponse()
    body = r.read(); hd = {k.lower(): v for k, v in r.getheaders()}; c.close()
    return r.status, hd, body


srv, port = serve({"AUTH": "off", "MAP_MBTILES_DIR": tdir})
# AC1 static
st, hd, body = get(port, "/static/leaflet/leaflet.js")
check("AC1 leaflet.js served as javascript with a day's cache", (st, hd.get("content-type", "").split(";")[0], "max-age=86400" in hd.get("cache-control", ""), body[:14]), (200, "text/javascript", True, b"/* @preserve\n "))
st, hd, body = get(port, "/static/leaflet/leaflet.css")
check("AC1 leaflet.css served as css", (st, hd.get("content-type", "").split(";")[0]), (200, "text/css"))
st, hd, body = get(port, "/static/leaflet/LICENSE")
check_true("AC1 the licence is served beside it", st == 200 and b"BSD 2-Clause" in body)
check_true("AC1 NOTICE names Leaflet", "Leaflet" in open(os.path.join(ROOT, "NOTICE")).read())
st, _, _ = get(port, "/static/../PINS.json")
check_true("AC1 a path outside the static directory is refused", st in (400, 404))
st, _, _ = get(port, "/static/leaflet/../../web.py")
check_true("AC1 ...and a traversal inside it", st in (400, 404))

# AC2 the views
st, _, page = get(port, "/")
page = page.decode()
check_true("AC2 the Mesh page carries both views", "data-view='map'" in page and "data-view='plan'" in page)
m = None
import re
mm = re.search(r"data-tiles='([^']*)'", page)
tiles = json.loads(mm.group(1).replace("&quot;", '"')) if mm else {}
check("AC2 the sources with google-hybrid as the default", (tiles.get("default"), sorted(s["id"] for s in tiles.get("sources", []))[:3]), ("google-hybrid", ["andover-test", "google-hybrid", "google-roads"]))
check_true("AC2 the Map view is the default when the box has a position", "data-default-view='map'" in page)
st, _, mp = get(port, "/map")
check_true("AC2 the Map page too", st == 200 and "data-view='map'" in mp.decode() and "data-tiles=" in mp.decode())
srv.shutdown()

# AC3 tilesets and tiles
srv, port = serve({"AUTH": "off", "MAP_MBTILES_DIR": tdir})
st, _, body = get(port, "/api/tilesets")
sets = {s["id"]: s for s in json.loads(body).get("tilesets", [])}
check("AC3 the raster set with format, bounds and zooms", (sets.get("andover-test", {}).get("format"), sets.get("andover-test", {}).get("minzoom"), sets.get("andover-test", {}).get("maxzoom"), sets.get("andover-test", {}).get("drawable")), ("png", 1, 1, True))
check("AC3 the vector set is listed as not drawable", (sets.get("roads", {}).get("format"), sets.get("roads", {}).get("drawable")), ("pbf", False))
check_true("AC3 the attribution travels", "Crown copyright" in str(sets.get("andover-test", {}).get("attribution")))
st, hd, body = get(port, "/tiles/andover-test/1/0/1")
check("AC3 the tile at XYZ y=1 is TMS row 0, served as png", (st, hd.get("content-type"), body == PNG), (200, "image/png", True))
st, _, _ = get(port, "/tiles/andover-test/1/0/0")
check("AC3 a missing tile is 404", st, 404)
st, _, _ = get(port, "/tiles/nowhere/1/0/0")
check("AC3 an unknown set is 404", st, 404)
st, _, _ = get(port, "/tiles/and..over/1/0/0")
check_true("AC3 a set name with a dot is refused", st in (400, 404))
srv.shutdown()

# AC4 local default
srv, port = serve({"AUTH": "off", "MAP_MBTILES_DIR": tdir, "MAP_TILES": "local"})
st, _, page = get(port, "/")
mm = re.search(r"data-tiles='([^']*)'", page.decode())
tiles = json.loads(mm.group(1).replace("&quot;", '"')) if mm else {}
check("AC4 MAP_TILES=local makes the first raster set the default", tiles.get("default"), "andover-test")
# AC5 the script
js = page.decode()
check_true("AC5 the overlay updates from /api/links on the events and falls back on a tile error", "mmOverlay" in js and "/api/links" in js and "tileerror" in js)
check_true("AC5 no reload", "location.reload(" not in js)
srv.shutdown()

# no position: the plan view is the default and the map view says why
import fakebridge_lib as FB
saved = dict(FB.LINKS["own"])
FB.LINKS["own"].update({"lat": None, "lon": None, "position_source": None})
srv, port = serve({"AUTH": "off"})
st, _, page = get(port, "/")
check_true("AC2 no position: Plan is the default and Map says why", "data-default-view='plan'" in page.decode() and "no position" in page.decode())
srv.shutdown()
FB.LINKS["own"].update(saved)

# AC6 the installer
root = tempfile.mkdtemp()
for d in ("opt/tak", "etc/systemd/system", "dev/serial/by-id"):
    os.makedirs(os.path.join(root, d), exist_ok=True)
open(os.path.join(root, "dev/serial/by-id/usb-x-if00"), "w").close()
out = subprocess.run(["bash", os.path.join(ROOT, "install", "install.sh"), "/nonexistent.tgz", "--serial", "/dev/serial/by-id/usb-x-if00", "--filter-group", "MilUX",
                      "--tiles", "local", "--mbtiles-dir", "/opt/tak-maps", "--dry-run"], capture_output=True, text=True, env=dict(os.environ, MESH_MANAGER_ROOT=root)).stdout
check_true("AC6 the installer writes MAP_TILES and MAP_MBTILES_DIR", "MAP_TILES=local" in out and "MAP_MBTILES_DIR=/opt/tak-maps" in out)
finish()
