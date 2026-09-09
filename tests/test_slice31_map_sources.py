#!/usr/bin/env python3
"""Spec 029: map sources from TAK."""
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
from mesh_manager import web as W  # noqa: E402

# AC1 the placeholders and the refusals
check("AC1 ATAK placeholders become Leaflet's", W.atak_url_to_leaflet("https://s/{$z}/{$x}/{$y}.png")[0], "https://s/{z}/{x}/{y}.png")
check("AC1 a quadkey source is refused with the reason", ("quadkey" in (W.atak_url_to_leaflet("https://s/a{$q}.jpeg")[1] or "")), True)
check("AC1 a URL without the placeholders is refused", ("{z}, {x} and {y}" in (W.atak_url_to_leaflet("https://s/tiles.png")[1] or "")), True)
check("AC1 a non-http URL is refused", ("http" in (W.atak_url_to_leaflet("javascript:alert(1){z}{x}{y}")[1] or "")), True)
XML = """<?xml version="1.0"?><customMapSource>
  <name>Andover imagery</name><url>https://tiles.example/{$z}/{$x}/{$y}.jpg</url>
  <minZoom>6</minZoom><maxZoom>17</maxZoom><tileType>jpg</tileType></customMapSource>"""
src, err = W.parse_map_source_xml(XML)
check("AC1 a whole customMapSource parses", (err, src["name"], src["url"], src["minzoom"], src["maxzoom"], src["tile_type"]),
      (None, "Andover imagery", "https://tiles.example/{z}/{x}/{y}.jpg", 6, 17, "jpg"))
check("AC1 rubbish is refused with a reason", ("XML" in (W.parse_map_source_xml("not xml at all")[1] or "")), True)

# AC2 XML in the box's map folder
mapdir = tempfile.mkdtemp()
open(os.path.join(mapdir, "andover.xml"), "w").write(XML)
open(os.path.join(mapdir, "broken.xml"), "w").write("<customMapSource><name>Bing</name><url>https://x/a{$q}.jpg</url></customMapSource>")
disk = {d["id"]: d for d in W.disk_map_sources(mapdir)}
check("AC2 a source in the map folder is offered", (disk["tak-andover"]["name"], disk["tak-andover"]["url"], disk["tak-andover"]["where"]),
      ("Andover imagery", "https://tiles.example/{z}/{x}/{y}.jpg", "the box's map folder"))
check("AC2 a broken one carries its reason and no URL", ("quadkey" in disk["tak-broken"]["error"], "url" in disk["tak-broken"]), (True, False))
t = W.tile_sources({"MAP_MBTILES_DIR": mapdir})
check("AC2 the drawable one joins the sources, the broken one does not", ([x["id"] for x in t["sources"] if x["id"].startswith("tak-")], t["default"]), (["tak-andover"], "google-hybrid"))

# AC3 added on the screen, saved on the box
etc = tempfile.mkdtemp()
rec, err = W.map_source_add(etc, name="Exercise area", url="https://ex/{z}/{x}/{y}.png", maxzoom=16)
check("AC3 an added source is saved", (err, rec["id"], rec["maxzoom"], rec["removable"]), (None, "own-exercise-area", 16, True))
check("AC3 it survives, read back fresh", [x["name"] for x in W.saved_map_sources(etc)], ["Exercise area"])
check("AC3 a duplicate name is refused", "already there" in (W.map_source_add(etc, name="Exercise area", url="https://ex/{z}/{x}/{y}.png")[1] or ""), True)
check("AC3 a built-in name cannot be repeated (the layer control is keyed by name)", "already there" in (W.map_source_add(etc, name="Google hybrid", url="https://ex/{z}/{x}/{y}.png")[1] or ""), True)
check("AC3 a built-in cannot be removed here", "cannot be removed" in (W.map_source_remove(etc, "google-hybrid")[1] or ""), True)
check("AC3 removing the added one works", (W.map_source_remove(etc, "own-exercise-area")[0], W.saved_map_sources(etc)), ({"removed": "own-exercise-area"}, []))

# AC4 the screen
W.map_source_add(etc, name="Exercise area", url="https://ex/{z}/{x}/{y}.png")
fb = start_fake_bridge()
srv = W.make_server(bind="127.0.0.1", port=0, socket_path=fb.path, etc_dir=etc, config={"AUTH": "off", "MAP_MBTILES_DIR": mapdir})
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start(); time.sleep(0.3)
def get(p):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10); c.request("GET", p); r = c.getresponse(); b = r.read().decode(); c.close(); return r.status, b
def post(p, body):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10); c.request("POST", p, body=json.dumps(body), headers={"Content-Type": "application/json"}); r = c.getresponse(); b = r.read().decode(); c.close(); return r.status, b
s, page = get("/map")
check_true("AC4 the Map page lists the sources and carries the form", s == 200 and "Andover imagery" in page and "Exercise area" in page and "data-action='map_source_add'" in page and "customMapSource" in page)
check_true("AC4 the broken one is shown with its reason", "quadkey" in page and "data-action='map_source_remove'" in page)
check_true("AC4 Google hybrid is still the default the map opens on", "&quot;default&quot;: &quot;google-hybrid&quot;" in page or '"default": "google-hybrid"' in page.replace("&quot;", '"'))
s, body = get("/api/map_sources")
j = json.loads(body)
check("AC4 the read action answers", (s, j["default"], any(x["id"] == "tak-andover" for x in j["sources"]), len(j["added"])), (200, "google-hybrid", True, 1))
s, body = post("/api/map_source_add", {"name": "Range", "xml": XML})
check("AC4 add through the API, then remove", (s, json.loads(body)["written"]["id"], post("/api/map_source_remove", {"id": "own-range"})[0]), (200, "own-range", 200))
s, body = post("/api/map_source_add", {"name": "Bad", "url": "ftp://x/{z}/{x}/{y}.png"})
check("AC4 a bad URL is a 400 with the reason", (s, "http" in json.loads(body)["error"]), (400, True))
srv.shutdown()
finish()
