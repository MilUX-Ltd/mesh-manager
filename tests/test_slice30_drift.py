#!/usr/bin/env python3
"""Spec 028: config drift."""
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
br._emit = lambda kind, **kw: None
r = br.op_profile_set(tx_power=20, position_broadcast_secs=900, region="eu_868")
check("AC1 the profile keeps its fields, region upper-cased, unset fields unenforced", (r["written"]["tx_power"], r["written"]["region"], br.op_profile()["role"]), (20, "EU_868", None))
check("AC1 nonsense refused", ("error" in br.op_profile_set(tx_power=40), "error" in br.op_profile_set(position_broadcast_secs=10)), (True, True))
# a read over the air leaves a snapshot
snap = {"id": "!aa000001", "long_name": "Tracker9", "short_name": "TR9", "region": "US", "modem_preset": "SHORT_FAST", "role": "TRACKER", "tx_power": 27, "position_broadcast_secs": 900, "managed": True, "read_at": B.utc(time.time()), "channels": [{"index": 0, "name": "MILUX-TAK", "role": "PRIMARY"}]}
br._register_note(snap, where="air", snapshot=br._snap_fields(snap))
reg = br._register_load()
check("AC1 the read left a snapshot in the register", (reg["!aa000001"]["snapshot"]["tx_power"], reg["!aa000001"]["snapshot"]["region"], reg["!aa000001"]["snapshot"]["channel0"]), (27, "US", "MILUX-TAK"))
# a device in line, and one never read
snap2 = dict(snap, id="!bb000002", long_name="Tracker2", region="EU_868", tx_power=20)
br._register_note(snap2, where="bench", snapshot=br._snap_fields(snap2))
reg = br._register_load(); reg["!cc000003"] = {"label": "Spare"}; br._register_save(reg)
d = br.op_drift()
by = {x["id"]: x for x in d["devices"]}
check("AC2 power and region drift on the first", sorted((x["field"], x["is"], x["should"]) for x in by["!aa000001"]["diffs"]), [("region", "US", "EU_868"), ("tx_power", 27, 20)])
check("AC2 in line, unread, and the counts", (by["!bb000002"]["state"], by["!cc000003"]["state"], d["counts"]), ("in line", "unread", {"in_line": 1, "drifted": 1, "unread": 1}))
check("AC2 enforced fields listed", d["enforced"], ["tx_power", "position_broadcast_secs", "region"])
# the fix
calls = []
br.op_node_set = lambda id=None, **kw: calls.append(("set", id, kw)) or {"written": list(kw), "confirmed": True, "read_back": dict(kw)}
br.op_node_set_region = lambda id=None, confirm=None, **kw: calls.append(("region", id, kw)) or {"written": list(kw), "confirmed": True, "read_back": dict(kw)}
r = br.op_drift_fix(id="!aa000001")
check("AC3 safe writes only the power, and says what it skipped", (calls, r.get("skipped"), r.get("confirmed")), ([("set", "!aa000001", {"tx_power": 20})], ["region"], True))
check("AC3 the snapshot took the read-back", br._register_load()["!aa000001"]["snapshot"]["tx_power"], 20)
calls.clear()
r = br.op_drift_fix(id="!aa000001", scope="all")
check("AC3 all without the confirm is refused", ("error" in r, calls), (True, []))
r = br.op_drift_fix(id="!aa000001", scope="all", confirm="!aa000001")
check("AC3 with the confirm the region is written", ([c[0] for c in calls], calls[-1][2] if calls else None), (["region"], {"region": "EU_868"}))
check("AC3 an unknown device is refused; an unmanaged one too", ("error" in br.op_drift_fix(id="!ffffffff"), "error" in br.op_drift_fix(id="!cc000003")), (True, True))
check("AC3 a device in line has nothing to fix", br.op_drift_fix(id="!bb000002").get("nothing"), True)

fb = start_fake_bridge()
srv = W.make_server(bind="127.0.0.1", port=0, socket_path=fb.path, etc_dir=tempfile.mkdtemp(), config={"AUTH": "off"})
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start(); time.sleep(0.3)
def get(p):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10); c.request("GET", p); r = c.getresponse(); b = r.read().decode(); c.close(); return r.status, b
s, page = get("/register")
check_true("AC4 the Register page carries the profile form and the drift table", s == 200 and "data-action='profile_set'" in page and "1 in line" in page and "should be 20" in page and "data-action='drift_fix'" in page and "never read" in page)
s, frag = get("/fragment/drift")
check_true("AC4 the fragment renders it", s == 200 and "<h2>Drift</h2>" in frag and "Recce lead" in frag)
srv.shutdown()
finish()
