#!/usr/bin/env python3
"""Spec 024: mesh health."""
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
from mesh_manager import bridge as B, web as W  # noqa: E402

state = tempfile.mkdtemp()
br = B.Bridge({"SERIAL": ""}, socket_path=os.path.join(state, "b.sock"), state_dir=state, observe=True, gps_reader=False)
own = (br._own() or {}).get("id")
def pkt(fr, port="POSITION_APP", **extra):
    d = {"portnum": port, "payload": b"x" * 10}; d.update(extra)
    return {"fromId": fr, "toId": "^all", "rxSnr": 5.0, "hopStart": 3, "hopLimit": 3, "decoded": d}
for _ in range(6):
    br._on_receive(pkt("!aa000001"), None)
for _ in range(2):
    br._on_receive(pkt("!bb000002"), None)
br._on_receive(pkt("!aa000001", "TELEMETRY_APP", telemetry={"deviceMetrics": {"batteryLevel": 64, "voltage": 3.9, "channelUtilization": 11.0, "airUtilTx": 0.5}}), None)
br._on_receive(pkt(own, "TELEMETRY_APP", telemetry={"deviceMetrics": {"batteryLevel": 101, "voltage": 4.4, "channelUtilization": 12.5, "airUtilTx": 0.8}}), None)
h = br.op_health(hours=2)
by = {d["id"]: d for d in h["nodes"]}
check("AC1 packets per node and per hour", (by["!aa000001"]["packets"], by["!aa000001"]["per_hour"], by["!bb000002"]["packets"], h["packets_per_hour"]), (7, 3.5, 2, 5.0))
check("AC1 each node's last utilisation, air time and battery", (by["!aa000001"]["chutil"], by["!aa000001"]["airutil"], by["!aa000001"]["battery"]), (11.0, 0.5, 64))
check("AC1 the gateway's own figures and the verdict", (h["chutil"], h["airutil"], h["verdict"], by[own]["own"]), (12.5, 0.8, "normal", True))
check("AC1 the hourly means", (len(h["hourly"]), h["hourly"][0]["chutil"]), (1, 12.5))
check("AC1 the thresholds", [B.Bridge._verdict(v) for v in (9, 24, 39, 40, None)], ["quiet", "normal", "busy", "saturated", "unknown"])
check("AC1 nodes heard leaves the box out", h["nodes_heard"], 2)
check("AC2 the region on the fake (EU_868) carries the 10 percent budget and the share", (h["region"], h["budget_pct"], h["air_share"]), ("EU_868", 10.0, 8.0))
class _L:  # a US radio
    region = 1
_orig_lora = br._lora
br._lora = lambda: _L()
try:
    import mesh_manager.bridge as BM
    old = BM.region_name
    BM.region_name = lambda v: "US"
    h2 = br.op_health(hours=2)
    check("AC2 US carries no budget", (h2["region"], h2["budget_pct"], h2["air_share"]), ("US", None, None))
finally:
    BM.region_name = old
    br._lora = _orig_lora
check("AC3 hours outside 1 to 168 refused", ("error" in br.op_health(hours=0), "error" in br.op_health(hours=999)), (True, True))
check("AC3 status carries the gateway's utilisation and verdict", (br.op_status().get("chutil"), br.op_status().get("verdict")), (12.5, "normal"))

fb = start_fake_bridge()
srv = W.make_server(bind="127.0.0.1", port=0, socket_path=fb.path, etc_dir=tempfile.mkdtemp(), config={"AUTH": "off"})
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start(); time.sleep(0.3)
def get(p):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10); c.request("GET", p); r = c.getresponse(); b = r.read().decode(); c.close(); return r.status, b
s, page = get("/health")
check_true("AC3 the Health page renders the cards, the chart and the table", s == 200 and "Channel utilisation" in page and "12.5%" in page and "of a 10% budget" in page and "<svg class='chart'" in page and "Tracker9" in page and "this radio" in page)
s, frag = get("/fragment/health")
check_true("AC3 the fragment renders the same cards", s == 200 and "Packets per hour" in frag and "<h2>Per node</h2>" in frag)
s, home = get("/")
# 5 Sep 2026 UX reviews: the card says what the number is, channel utilisation, the name the Health page uses
check_true("AC3 the overview carries the channel utilisation card", s == 200 and "Channel utilisation" in home and "href='/health'" in home)
srv.shutdown()
finish()
