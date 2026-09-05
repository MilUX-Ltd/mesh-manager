#!/usr/bin/env python3
"""Spec 036: how much of the window each node was actually heard for."""
import os, sys, tempfile, time
sys.path.insert(0, os.path.dirname(__file__))
from _common import ROOT, check, check_true, finish  # noqa: E402
sys.path.insert(0, os.path.join(ROOT, "src"))
import fakegw_lib  # noqa: E402
fakegw_lib.install()
from mesh_manager import bridge as B, catalogue as C, web as W  # noqa: E402
from mesh_manager.common import utc  # noqa: E402

state = tempfile.mkdtemp()
br = B.Bridge({"SERIAL": ""}, socket_path=os.path.join(state, "b.sock"), state_dir=state, observe=True, gps_reader=False)
br.interface.nodes["!aa000001"] = {"user": {"longName": "Half"}, "lastHeard": int(time.time())}
br.interface.nodes["!bb000002"] = {"user": {"longName": "Quiet"}, "lastHeard": int(time.time())}
now = time.time()
for h in range(24):
    if h % 2 == 0:   # every other hour
        br.history.packet("!aa000001", port="POSITION_APP", snr=5.0, hops=0, ts=utc(now - h * 3600 - 600))
for d in range(7):
    if d < 3:        # three of seven days
        br.history.packet("!aa000001", port="POSITION_APP", snr=5.0, hops=0, ts=utc(now - d * 86400 - 3600 * 2))

# ---- AC1 / AC2 -------------------------------------------------------------------------------------
out = br.op_availability(hours=24)
by = {r["id"]: r for r in out.get("nodes", [])}
check("AC1 heard in 12 of 24 hourly buckets is 50%", (by.get("!aa000001", {}).get("buckets"), by.get("!aa000001", {}).get("heard"), by.get("!aa000001", {}).get("pct")), (24, 12, 50))
check("AC2 a node with nothing in the window is 0% and still listed", by.get("!bb000002", {}).get("pct"), 0)
check_true("AC1 the bucket list is there for a histogram", len(by.get("!aa000001", {}).get("series") or []) == 24)

# ---- AC3 seven days: daily buckets ----------------------------------------------------------------
out7 = br.op_availability(hours=168)
r7 = {r["id"]: r for r in out7.get("nodes", [])}.get("!aa000001", {})
check("AC3 over 7 days the buckets are daily", (r7.get("buckets"), r7.get("heard")), (7, 3))

# ---- AC5 catalogue ----------------------------------------------------------------------------------
check_true("AC5 availability is a read", (C.by_id("availability") or {}).get("risk") == "read")

# ---- AC4 the pages ----------------------------------------------------------------------------------
reg = W.register_body({"rows": [{"id": "!aa000001", "name": "Half", "managed": False}]}, availability={"!aa000001": by["!aa000001"]})
check_true("AC4 the register shows the percentage", "50%" in reg)
node = W.node_body({"id": "!aa000001", "name": "Half", "heard_here": True}, [], [], 0, 24, availability=by["!aa000001"])
check_true("AC4 the node page shows the histogram", "heard" in node.lower() and "50%" in node)
finish()
