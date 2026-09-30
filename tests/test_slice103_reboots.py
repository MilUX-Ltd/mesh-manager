#!/usr/bin/env python3
"""Spec 103: reboot detection per node. A node's uptime resets when it restarts; the box
notices, counts the restarts it did not ask for, raises an alert at three in a day, and says so on
the node's page and its row. These are the acceptance tests. They were committed failing, and the
build does not edit them."""
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
from mesh_manager import bridge as B, catalogue as C, history as HI, web as W  # noqa: E402

T0 = 1_790_000_000.0          # a fixed start, so every time below is exact
CLOCK = [T0]
_real_time = time.time


def at(t):
    CLOCK[0] = T0 + t


def safe(fn, *a, **k):
    """Run one call a criterion depends on; a call the code cannot yet take is a failure, not a crash."""
    try:
        return fn(*a, **k)
    except Exception as e:  # noqa: BLE001
        return f"raised {type(e).__name__}: {e}"


# ---- AC1 the rule: a node started again after the last time it was heard ---------------------
rule = getattr(HI, "rebooted", None)
check_true("AC1 history.rebooted(prev_rx, prev_uptime, rx, uptime) exists", callable(rule))
if callable(rule):
    check("AC1 a node running steadily has not rebooted", rule(1000, 500, 2800, 2300), False)
    check("AC1 uptime back near nought is a reboot", rule(1000, 5000, 2800, 60), True)
    check("AC1 a reboot is seen even when the new uptime is longer than the old", rule(1000, 100, 1000 + 3 * 86400, 86400), True)
    check("AC1 a packet that arrived late is not a reboot", rule(2800, 2300, 2900, 1900), False)
    check("AC1 the same packet heard twice is not a reboot", rule(2800, 2300, 2805, 2300), False)
    check("AC1 a node rebooting every few minutes is seen each time", rule(1000, 240, 2800, 200), True)
    check("AC1 two readings minutes apart after one boot are not a reboot", rule(1030, 30, 1090, 90), False)
    check("AC1 nothing to compare with is not a reboot", (rule(None, None, 2800, 60), rule(1000, None, 2800, 60), rule(1000, 500, 2800, None)), (False, False, False))

# ---- a bridge on a clock the test holds ------------------------------------------------------
time.time = lambda: CLOCK[0]
state = tempfile.mkdtemp()


def bridge(sock):
    b = B.Bridge({"SERIAL": "/dev/serial/by-id/usb-fake-test-radio-if00", "HISTORY_DAYS": 30}, socket_path=os.path.join(state, sock), state_dir=state, observe=True, gps_reader=False)
    b.READBACK_S = 2
    return b


def telemetry(br, fr, uptime, **extra):
    p = {"fromId": fr, "toId": "^all", "rxSnr": 8.0, "hopStart": 3, "hopLimit": 3,
         "decoded": {"portnum": "TELEMETRY_APP", "payload": b"y" * 12, "telemetry": {"deviceMetrics": {"batteryLevel": 64, "voltage": 3.91, "uptimeSeconds": uptime}}}}
    p.update(extra)
    br._on_receive(p, None)


def reboots(br, **k):
    r = safe(getattr(br, "op_reboots", lambda **_: "no op_reboots"), **k)
    return r if isinstance(r, dict) else {"rows": [], "by_node": {}, "error": r}


at(0)
br = bridge("b.sock")
N = "!aa000001"

# ---- AC2 a reboot heard is recorded once, and the record survives a restart ------------------
at(0); telemetry(br, N, 500)
at(1800); telemetry(br, N, 2300)
check("AC2 steady readings record no reboot", len(reboots(br, node=N)["rows"]), 0)
at(3600); telemetry(br, N, 60)
rows = reboots(br, node=N)["rows"]
check("AC2 uptime reset records one reboot", len(rows), 1)
check("AC2 it says when the node started, from its uptime", (rows[0].get("booted") if rows else None), HI.utc(T0 + 3540))
at(3610); telemetry(br, N, 60, viaMqtt=True)
check("AC2 the same reading carried again by a broker is not a second reboot", len(reboots(br, node=N)["rows"]), 1)
for bad in ("soon", True, -5, 1e12, None):
    at(3620); telemetry(br, N, bad)
check("AC2 an uptime that is not a number a node could report records nothing", len(reboots(br, node=N)["rows"]), 1)
br.history.close()
at(5400)
br = bridge("b2.sock")
at(5400); telemetry(br, N, 30)
check("AC2 after the box restarts it still knows the last reading, and sees the next reboot", len(reboots(br, node=N)["rows"]), 2)

# ---- AC3 a reboot this computer asked for is labelled, and not counted -----------------------------
OURS = b"\x07" * 32
br.interface.localNode.localConfig.security.public_key = OURS
M = "!cc000003"
fakegw_lib.FakeRemoteNode.device(M, OURS)
br.node_factory = lambda nid: fakegw_lib.FakeRemoteNode(br.interface, nid)
br._register_note({"id": M, "long_name": "Relay3", "managed": True, "hw": "RAK4631", "firmware": "2.6.11", "role": "ROUTER"})
at(6000); telemetry(br, M, 40000)
at(7000); r = safe(br.op_node_reboot, id=M, confirm=M)
check_true("AC3 the box asks the node to reboot", isinstance(r, dict) and bool(r.get("asked")), repr(r)[:120])
at(7200); telemetry(br, M, 180)
rows = reboots(br, node=M)["rows"]
check("AC3 the reboot this computer asked for is recorded and marked as asked", [bool(x.get("asked")) for x in rows], [True])
check("AC3 and it is not counted", reboots(br, node=M).get("by_node", {}).get(M, {}).get("count"), 0)
at(8000); r = safe(br.op_node_set_region, id=M, region="EU_868", confirm=M)
at(8400); telemetry(br, M, 300)                   # started at 8100: eighteen minutes after the last boot, a hundred seconds after the ask
check("AC3 a settings write the node reboots for is asked too", [bool(x.get("asked")) for x in reboots(br, node=M)["rows"]], [True, True])
at(8400 + 3 * 3600); telemetry(br, M, 3 * 3600 + 300)   # still up: no reboot
at(8400 + 4 * 3600); telemetry(br, M, 200)              # hours after the ask: its own reboot
check("AC3 a reboot long after an ask is the node's own and is counted", reboots(br, node=M).get("by_node", {}).get(M, {}).get("count"), 1)

# ---- AC4 three of its own in a day raise an alert, which clears when they age out ------------
st = safe(br.op_alert_settings)
check("AC4 the threshold is a setting, three by default", (st.get("reboots_day") if isinstance(st, dict) else st), 3)
check_true("AC4 a threshold outside 0 to 50 is refused", "error" in (safe(br.op_alert_set, reboots_day=51) or {}))
check_true("AC4 a threshold that is not a number is refused", "error" in (safe(br.op_alert_set, reboots_day="lots") or {}))
F = "!dd000004"
t = 40000
at(t); telemetry(br, F, 9000)
for i in range(3):
    t += 1800; at(t); telemetry(br, F, 60)
    t += 1500; at(t); telemetry(br, F, 1560)
check("AC4 three reboots of its own in 24 h are counted", reboots(br, node=F, hours=24).get("by_node", {}).get(F, {}).get("count"), 3)
safe(br._judge_alerts)
op = br._alerts_load()["open"].get(f"{F}:reboot")
check_true("AC4 they raise a reboot alert", bool(op), repr(op))
check_true("AC4 which says how many and over what", bool(op) and "rebooted 3 times in 24 h" in op.get("text", ""), repr((op or {}).get("text")))
at(t + 25 * 3600); telemetry(br, F, 25 * 3600 + 1560)
safe(br._judge_alerts)
check("AC4 the alert clears once the reboots are more than a day old", f"{F}:reboot" in br._alerts_load()["open"], False)
check_true("AC4 a threshold of 0 is accepted and turns the alert off", "error" not in (safe(br.op_alert_set, reboots_day=0) or {"error": 1}))
time.time = _real_time

# ---- AC5 the node's page and its row say it, and the agent can ask ---------------------------
check("AC5 the agent has a reboots read", (C.by_id("reboots") or {}).get("risk"), "read")
check("AC5 parity holds across the catalogue, the API and the agent's tools", C.parity_problems(C.ACTIONS, W.api_action_routes(), [t["name"] for t in W.mcp_tools("act")]), [])
fb = start_fake_bridge()
srv = W.make_server(bind="127.0.0.1", port=0, socket_path=fb.path, etc_dir=tempfile.mkdtemp(), config={"AUTH": "off"})
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()


def get(p):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10); c.request("GET", p); r = c.getresponse(); b = r.read().decode(); c.close(); return r.status, b


s, page = get("/node?id=!aa000001")
check_true("AC5 the node's page says how many times it rebooted of its own accord", s == 200 and "Rebooted 3 times in the last 24 h" in page)
check_true("AC5 and names the one this computer asked for as such", "asked for from this computer" in page)
check_true("AC5 and lists when it started each time", "2026-09-03T21:26" in page and "2026-09-03T08:02" in page)
s, quiet = get("/node?id=!bb000002")
check_true("AC5 a node that has not rebooted says so", s == 200 and "No reboots seen in the last 24 h" in quiet)
s, nodes = get("/nodes")
check_true("AC5 the node's row on the Nodes page carries the count", s == 200 and "3 reboots in 24 h" in nodes)
srv.shutdown()

finish()
