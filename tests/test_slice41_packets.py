#!/usr/bin/env python3
"""Spec 039: every packet the radio heard, on a page you can filter."""
import http.client, os, sys, tempfile, threading, time
sys.path.insert(0, os.path.dirname(__file__))
from _common import ROOT, check, check_true, finish  # noqa: E402
sys.path.insert(0, os.path.join(ROOT, "src"))
import fakebridge_lib as FB  # noqa: E402
from fakebridge_lib import start_fake_bridge  # noqa: E402
from mesh_manager import web as W  # noqa: E402

FB.HISTORY["packets"] = [
    {"ts": FB.at("2026-09-03T21:50:00Z"), "node": "!aa000001", "port": "POSITION_APP", "snr": 9.5, "hops": 0, "size": 20},
    {"ts": FB.at("2026-09-03T21:51:00Z"), "node": "!bb000002", "port": "TEXT_MESSAGE_APP", "snr": 3.0, "hops": 1, "size": 12},
    {"ts": FB.at("2026-09-03T21:52:00Z"), "node": "!aa000001", "port": "TELEMETRY_APP", "snr": 9.0, "hops": 0, "size": 30},
]
fb = start_fake_bridge()
srv = W.make_server(bind="127.0.0.1", port=0, socket_path=fb.path, etc_dir=tempfile.mkdtemp(), config={"AUTH": "off"}, state_dir=tempfile.mkdtemp())
port = srv.server_address[1]; threading.Thread(target=srv.serve_forever, daemon=True).start(); time.sleep(0.3)

def get(path):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10); c.request("GET", path); r = c.getresponse(); b = r.read().decode(); c.close(); return r.status, b

st, body = get("/packets?hours=48")
check("AC1 the page answers", st, 200)
check_true("AC1 the six columns", all(h in body for h in (">When<", ">From<", ">Port<", ">SNR<", ">Hops<", ">Size<")))
_tb = body.split("<tbody")[1]
check_true("AC1 newest first", _tb.index("TELEMETRY_APP") < _tb.index("POSITION_APP"))
check_true("AC1 a labelled node shows its label", "Tracker 9 (recce)" in body or "Tracker9" in body)
check_true("AC3 per-port counts for the window", "POSITION_APP" in body and "TEXT_MESSAGE_APP" in body and "data-portcount" in body)
st, body = get("/packets?hours=48&node=!bb000002")
check_true("AC2 the node filter narrows", "TEXT_MESSAGE_APP" in body and "TELEMETRY_APP" not in body.split("<tbody")[1])
st, body = get("/packets?hours=48&port=TELEMETRY_APP")
check_true("AC2 the port filter narrows", "TELEMETRY_APP" in body.split("<tbody")[1] and "TEXT_MESSAGE_APP" not in body.split("<tbody")[1])
check_true("AC4 on the More menu and live on packet events", ("/packets", "Packets") in W.NAV_MORE and "kind==='packet'" in body)
finish()
