#!/usr/bin/env python3
"""Spec 043: fleet inventory. Bridge half on the fake gateway; screen half on the fake bridge."""
import base64, hashlib, http.client, json, os, sys, tempfile, threading, time
sys.path.insert(0, os.path.dirname(__file__))
from _common import ROOT, check, check_true, finish  # noqa: E402
sys.path.insert(0, os.path.join(ROOT, "src"))
import fakegw_lib  # noqa: E402
fakegw_lib.install()
from fakebridge_lib import start_fake_bridge  # noqa: E402
from mesh_manager import bridge as B, catalogue as C, web as W  # noqa: E402

a = C.by_id("inventory") or {}; k = C.by_id("key_accept") or {}
check("AC1 inventory is a read with no inputs", (a.get("risk"), a.get("inputs")), ("read", []))
check("AC1 key_accept is a change taking id", (k.get("risk"), [i["name"] for i in k.get("inputs", [])]), ("change", ["id"]))
check("AC1 parity holds", C.parity_problems(C.ACTIONS, W.api_action_routes(), [t["name"] for t in W.mcp_tools("act")]), [])

state = tempfile.mkdtemp()
br = B.Bridge({"SERIAL": ""}, socket_path=os.path.join(state, "b.sock"), state_dir=state, observe=True)
NID = "!aa000001"
K1 = base64.b64encode(b"\x01" * 32).decode(); K2 = base64.b64encode(b"\x02" * 32).decode()
br.meshtastic_devices[NID] = {"long_name": "Tracker9", "meshtastic_id": NID}
def db(key):
    return {NID: {"user": {"id": NID, "longName": "Tracker9", "hwModel": "TRACKER_T1000_E", "publicKey": key, "role": "TRACKER"}, "lastHeard": time.time()}}
br.interface.nodes = db(K1); br.op_nodes()
reg = br._register_load().get(NID, {})
check("AC2 key, hardware and role recorded from the database", (reg.get("public_key"), reg.get("hw"), reg.get("role")), (K1, "TRACKER_T1000_E", "TRACKER"))
check_true("AC2 key_since set the first time", bool(reg.get("key_since")))
since = reg.get("key_since")
br.interface.nodes = db(""); br.op_nodes()
reg = br._register_load().get(NID, {})
check("AC3 an empty key changes nothing", (reg.get("public_key"), reg.get("key_changed")), (K1, None))
br.interface.nodes = db(K1); br.op_nodes()
check("AC3 the same key changes nothing", br._register_load().get(NID, {}).get("key_changed"), None)
br.interface.nodes = db(K2); br.op_nodes()
reg = br._register_load().get(NID, {})
check("AC3 a different key: changed, previous kept, new kept, since kept", (bool(reg.get("key_changed")), reg.get("key_previous"), reg.get("public_key"), reg.get("key_since")), (True, K1, K2, since))

fb_ = getattr(B, "firmware_behind", None)
check("AC4 2.7.4 is behind 2.7.11", fb_("2.7.4.c1f4f9c", "2.7.11.ab12") if fb_ else "missing", True)
check("AC4 2.7.11 is not behind 2.7.11", fb_("2.7.11", "2.7.11.ab12") if fb_ else "missing", False)
check("AC4 unknown firmware is None", fb_(None, "2.7.11") if fb_ else "missing", None)

inv = br.op_inventory() if hasattr(br, "op_inventory") else {}
row = next((r for r in inv.get("rows", []) if r.get("id") == NID), {})
check("AC5 fingerprint is 12 hex of sha256 of the key", row.get("fingerprint"), hashlib.sha256(base64.b64decode(K2)).hexdigest()[:12])
check_true("AC5 behind is tri-state with a reason in words", row.get("behind") in (True, False, None) and isinstance(row.get("behind_reason"), str) and bool(row.get("behind_reason")), repr(row.get("behind_reason")))

def kinds():
    return [o.get("kind") for o in br.op_alerts().get("open", [])]
try:
    br._judge_alerts()
    check_true("AC6 the alert pass raises kind key for the changed key", "key" in kinds(), str(kinds()))
    r = br.op_key_accept(id=NID) if hasattr(br, "op_key_accept") else {}
    check("AC6 key_accept confirms", r.get("confirmed"), True)
    br._judge_alerts()
    check("AC6 accepted: no open key alert", "key" in kinds(), False)
except Exception as ex:  # noqa: BLE001
    check("AC6 the alert pass ran", f"{type(ex).__name__}: {ex}", "ran")

fb = start_fake_bridge()
srv = W.make_server(bind="127.0.0.1", port=0, socket_path=fb.path, etc_dir=tempfile.mkdtemp(), config={"AUTH": "off"}, state_dir=tempfile.mkdtemp())
port = srv.server_address[1]; threading.Thread(target=srv.serve_forever, daemon=True).start(); time.sleep(0.3)
def get(p):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10); c.request("GET", p); r = c.getresponse(); b = r.read().decode(); c.close(); return r.status, b
st, body = get("/register")
check_true("AC7 the Register page has hardware, firmware and key columns", st == 200 and ">Hardware</th>" in body and ">Firmware</th>" in body and ">Key</th>" in body)
check_true("AC7 behind and changed rows are marked", "behind" in body and "key-changed" in body, "")
st, body = get("/export/inventory.csv")
check_true("AC7 /export/inventory.csv serves the table with a header", st == 200 and body.splitlines() and body.splitlines()[0].startswith("id,name,hw,firmware,fingerprint"), body[:80])
finish()
