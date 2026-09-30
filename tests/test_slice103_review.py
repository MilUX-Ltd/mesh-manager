#!/usr/bin/env python3
"""Spec 103, from its independent review (30 September 2026): the threshold can be changed
from the screen and by the agent; this computer's own radio is not judged; a reboot counts in the last
24 hours by when the node started, not when it was noticed; one reboot reads as one; and every stage
of a flash is an ask. Committed failing; the build does not edit it."""
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
from mesh_manager import bridge as B, catalogue as C, web as W  # noqa: E402

# ---- the threshold reaches the screen and the agent ------------------------------------------
clean, err = C.validate(C.by_id("alert_set"), {"reboots_day": 5})
check("the agent and the screen may set reboots_day", (err, (clean or {}).get("reboots_day")), (None, 5))
check_true("and the catalogue refuses a value over 50", C.validate(C.by_id("alert_set"), {"reboots_day": 51})[1] is not None)
fb = start_fake_bridge()
srv = W.make_server(bind="127.0.0.1", port=0, socket_path=fb.path, etc_dir=tempfile.mkdtemp(), config={"AUTH": "off"})
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
c = http.client.HTTPConnection("127.0.0.1", port, timeout=10); c.request("GET", "/health"); page = c.getresponse().read().decode(); c.close()
check_true("the alert settings form carries the reboot threshold beside the others", "name='reboots_day'" in page)
srv.shutdown()

# ---- a bridge on a clock the test holds -------------------------------------------------------
T0 = 1_790_000_000.0
CLOCK = [T0]
_real_time = time.time
time.time = lambda: CLOCK[0]


def at(t):
    CLOCK[0] = T0 + t


state = tempfile.mkdtemp()
br = B.Bridge({"SERIAL": "/dev/serial/by-id/usb-fake-test-radio-if00", "HISTORY_DAYS": 30}, socket_path=os.path.join(state, "b.sock"), state_dir=state, observe=True, gps_reader=False)
own = (br._own() or {}).get("id")


def telemetry(fr, uptime):
    br._on_receive({"fromId": fr, "toId": "^all", "rxSnr": 8.0, "hopStart": 3, "hopLimit": 3,
                    "decoded": {"portnum": "TELEMETRY_APP", "payload": b"y" * 12, "telemetry": {"deviceMetrics": {"uptimeSeconds": uptime}}}}, None)


# ---- this computer's own radio is not judged (it resets when the port opens) -----------------
at(0); telemetry(own, 50000)
at(1800); telemetry(own, 30)
check("this radio's restarts are not counted as a node's", own in (br.op_reboots(hours=24).get("by_node") or {}), False)

# ---- by when the node started ----------------------------------------------------------------
S = "!ee000051"
at(0); telemetry(S, 500)
at(31 * 3600); telemetry(S, 30 * 3600)          # started an hour after it was last heard, 30 hours ago
check("a reboot that started more than a day ago is not in the last 24 h, though noticed now",
      (br.op_reboots(hours=24).get("by_node") or {}).get(S, {}).get("count", 0), 0)
check("and it is still recorded", len(br.op_reboots(node=S, hours=48).get("rows") or []), 1)

# ---- one reboot reads as one ------------------------------------------------------------------
br.op_alert_set(reboots_day=1)
O = "!ee000052"
at(40 * 3600); telemetry(O, 9000)
at(40 * 3600 + 1800); telemetry(O, 60)
br._judge_alerts()
op = br._alerts_load()["open"].get(f"{O}:reboot") or {}
check_true("one reboot is 'rebooted 1 time in 24 h'", "rebooted 1 time in 24 h" in op.get("text", ""), repr(op.get("text")))

# ---- every stage of a flash is an ask ---------------------------------------------------------
F = "!ee000053"
at(50 * 3600); telemetry(F, 20000)
at(50 * 3600 + 100); br._emit("flash", id=F, stage="exported")
at(50 * 3600 + 130); br._emit("flash", id=F, stage="copied")    # the device restarts from here
at(50 * 3600 + 330); br._emit("flash", id=F, stage="back")
at(50 * 3600 + 400); br._emit("flash", id=F, stage="version read")
at(50 * 3600 + 500); telemetry(F, 360)                           # started at +140
rows = br.op_reboots(node=F, hours=24).get("rows") or []
check("a node flashed from the bench restarts as asked, whichever stage was announced last", [bool(r.get("asked")) for r in rows], [True])
time.time = _real_time

finish()
