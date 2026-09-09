#!/usr/bin/env python3
"""Spec 044: groups, tags and node icons. Bridge half on the fake gateway; screen half on the fake bridge."""
import http.client, json, os, sys, tempfile, threading, time
sys.path.insert(0, os.path.dirname(__file__))
from _common import ROOT, check, check_true, finish  # noqa: E402
sys.path.insert(0, os.path.join(ROOT, "src"))
import fakegw_lib  # noqa: E402
fakegw_lib.install()
import fakebridge_lib  # noqa: E402
from fakebridge_lib import start_fake_bridge  # noqa: E402
from mesh_manager import bridge as B, catalogue as C, common as CM, web as W  # noqa: E402

for aid, risk in (("groups", "read"), ("group_set", "change"), ("group_delete", "change")):
    check(f"AC7 {aid} is in the catalogue as {risk}", (C.by_id(aid) or {}).get("risk"), risk)
check("AC7 parity holds", C.parity_problems(C.ACTIONS, W.api_action_routes(), [t["name"] for t in W.mcp_tools("act")]), [])
icons = getattr(CM, "NODE_ICONS", None)
check_true("AC6 the icon set is declared once, radio first", isinstance(icons, (list, tuple)) and len(icons) >= 10 and icons[0] == "radio", repr(icons))
svg = getattr(W, "NODE_ICON_SVG", {})
check("AC6 every icon name has an SVG", sorted(set(icons or []) - set(svg)), [])

state = tempfile.mkdtemp()
br = B.Bridge({"SERIAL": ""}, socket_path=os.path.join(state, "b.sock"), state_dir=state, observe=True)
A, Bn, Cn = "!aa000001", "!bb000002", "!cc000003"
for nid, name in ((A, "Tracker9"), (Bn, "Tracker2"), (Cn, "Spare")):
    br.meshtastic_devices[nid] = {"long_name": name, "meshtastic_id": nid}
r = br.op_register_set(id=A, group="Recce", tags="alpha, bravo, alpha, ", icon="person")
reg = br._register_load().get(A, {})
check("AC1 group, tags and icon stored; tags split, trimmed, deduplicated", (reg.get("group"), reg.get("tags"), reg.get("icon")), ("Recce", ["alpha", "bravo"], "person"))
r = br.op_register_set(id=Bn, icon="unicorn")
check_true("AC1 an icon outside the set is refused in words", "icon" in str(r.get("error", "")).lower(), repr(r))
r = br.op_group_set(name="Vehicles", icon="vehicle") if hasattr(br, "op_group_set") else {}
check("AC2 group_set creates a group with an icon", (r.get("confirmed"), (r.get("group") or {}).get("icon")), (True, "vehicle"))
br.op_register_set(id=Bn, group="Vehicles")
br.op_register_set(id=Cn, group="Vehicles", icon="dog")
g = br.op_groups() if hasattr(br, "op_groups") else {}
rows = {x.get("name"): x for x in g.get("groups", [])}
check("AC2 groups lists names, icons and member counts", ((rows.get("Vehicles") or {}).get("icon"), (rows.get("Vehicles") or {}).get("count"), (rows.get("Recce") or {}).get("count")), ("vehicle", 2, 1))
nodes = {n["id"]: n for n in br.op_nodes().get("nodes", [])}
check("AC3 icon resolves node, then group, then radio", ((nodes.get(Bn) or {}).get("icon"), (nodes.get(Cn) or {}).get("icon"), (nodes.get(A) or {}).get("icon")), ("vehicle", "dog", "person"))
check("AC3 rows carry group and tags", ((nodes.get(A) or {}).get("group"), (nodes.get(A) or {}).get("tags")), ("Recce", ["alpha", "bravo"]))
r = br.op_group_delete(name="Vehicles") if hasattr(br, "op_group_delete") else {}
check("AC2 group_delete removes the group and clears its members", (r.get("confirmed"), br._register_load().get(Bn, {}).get("group", ""), "Vehicles" in {x.get("name") for x in (br.op_groups() if hasattr(br, "op_groups") else {}).get("groups", [])}), (True, "", False))
check("AC3 a node with no group and no icon is a radio", (br.op_nodes()["nodes"] and {n["id"]: n for n in br.op_nodes()["nodes"]}.get(Bn, {}).get("icon")), "radio")
r = br.op_send_text(text="rally at the gate", to="group:Recce")
check("AC5 a group message goes to each member with its own id", (sorted(r.get("members", [])), len(r.get("ids", []))), ([A], 1))
r = br.op_send_text(text="hello", to="group:Nobody")
check_true("AC5 an unknown group is refused", "group" in str(r.get("error", "")).lower(), repr(r))

# screen half: the fake bridge's nodes carry a group
for n in fakebridge_lib.NODES:
    n["group"] = "Recce" if n["id"] == "!aa000001" else ""
    n["icon"] = "person" if n["id"] == "!aa000001" else "radio"
fb = start_fake_bridge()
srv = W.make_server(bind="127.0.0.1", port=0, socket_path=fb.path, etc_dir=tempfile.mkdtemp(), config={"AUTH": "off"}, state_dir=tempfile.mkdtemp())
port = srv.server_address[1]; threading.Thread(target=srv.serve_forever, daemon=True).start(); time.sleep(0.3)
def get(p):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10); c.request("GET", p); r = c.getresponse(); b = r.read().decode(); c.close(); return r.status, b
st, body = get("/nodes?group=Recce")
check_true("AC4 /nodes?group= lists members only", st == 200 and "!aa000001" in body and "data-id='!bb000002'" not in body)
st, body = get("/api/trails?hours=48&group=Recce")
rows = json.loads(body).get("rows", []) if st == 200 else None
check_true("AC4 /api/trails?group= returns members' rows only", rows is not None and all(r["node"] == "!aa000001" for r in rows), str(st))
st, body = get("/api/trails?hours=48&group=Nobody")
check("AC4 a group with no members has no rows", (st, json.loads(body).get("rows") if st == 200 else None), (200, []))
st, body = get("/export/positions.csv?group=Nobody")
check_true("AC4 exports take the group filter", st == 200 and len(body.splitlines()) == 1, str(st))
st, body = get("/map")
check_true("AC6 the map draws icons in divIcon markers and has a group select", st == 200 and "L.divIcon(" in body and "id='group-filter'" in body)
st, body = get("/nodes")
check_true("AC6 the node row offers group, tags and an icon picker", "name='group'" in body and "name='tags'" in body and "name='icon'" in body and "<datalist id='groups'>" in body)
st, body = get("/register")
check_true("AC6 the Register page has a Groups card", st == 200 and "group_set" in body and "group_delete" in body)
finish()
