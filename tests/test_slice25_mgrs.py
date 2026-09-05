#!/usr/bin/env python3
"""Spec 023: MGRS and the grid."""
import http.client
import os
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(__file__))
from _common import ROOT, check, check_true, finish  # noqa: E402
sys.path.insert(0, os.path.join(ROOT, "src"))
from mesh_manager import mgrs as MG  # noqa: E402

check("AC1 Andover is in 30U XB", MG.mgrs(51.2128, -1.5056, 0), "30U XB")
check("AC1 London is in 30U XC", MG.mgrs(51.5074, -0.1278, 0), "30U XC")
check("AC1 Sydney's zone and band", MG.mgrs(-33.8688, 151.2093, 0)[:3], "56H")
z, h, x, y = MG.to_utm(51.2128, -1.5056)
lat, lon = MG.from_utm(z, h, x, y)
check("AC1 zone 30 north, easting east of the central meridian", (z, h, 500000 < x < 700000, 5600000 < y < 5750000), (30, "N", True, True))
check_true("AC1 UTM and back within a metre", abs(lat - 51.2128) < 1e-5 and abs(lon + 1.5056) < 1.5e-5)
s5 = MG.mgrs(51.2128, -1.5056, 5); s4 = MG.mgrs(51.2128, -1.5056, 4)
check("AC2 five digits are metres, four are tens of metres", (len(s5.split()[2]), len(s5.split()[3]), len(s4.split()[2]), s5.split()[2][:4] == s4.split()[2]), (5, 5, 4, True))
check("AC2 outside the bands is none", MG.mgrs(-85, 0), None)

import fakegw_lib  # noqa: E402
fakegw_lib.install()
from fakebridge_lib import start_fake_bridge  # noqa: E402
from mesh_manager import web as W  # noqa: E402
fb = start_fake_bridge()
srv = W.make_server(bind="127.0.0.1", port=0, socket_path=fb.path, etc_dir=tempfile.mkdtemp(), config={"AUTH": "off"})
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start(); time.sleep(0.3)
def get(p):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10); c.request("GET", p); r = c.getresponse(); b = r.read().decode(); c.close(); return r.status, b
s, nodes = get("/nodes")
check_true("AC3 the node rows carry MGRS beside the degrees", s == 200 and "30U X" in nodes and "51.20000, -1.50000 · 30U" in nodes)
s, mp = get("/map")
check_true("AC3 the map bar carries the grid control; the overlay the arithmetic and the readout", s == 200 and "id='grid-on'" in mp and "function fromUtm" in mp and "map-readout" in mp and "window.mmMgrs" in mp and "step=map.getZoom()>=13?1000:10000" in mp)
check_true("AC3 the box's own MGRS in the legend", "Box position:" in mp and "· 30U" in mp)
srv.shutdown()
finish()
