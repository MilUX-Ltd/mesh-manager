#!/usr/bin/env python3
"""Spec 045: geofence alerts. Pure geometry, the bridge on the fake gateway, the screen on the fake bridge."""
import http.client, json, os, sys, tempfile, threading, time
sys.path.insert(0, os.path.dirname(__file__))
from _common import ROOT, check, check_true, finish  # noqa: E402
sys.path.insert(0, os.path.join(ROOT, "src"))
import fakegw_lib  # noqa: E402
fakegw_lib.install()
from fakebridge_lib import start_fake_bridge  # noqa: E402
from mesh_manager import bridge as B, catalogue as C, web as W  # noqa: E402

for aid, risk in (("fences", "read"), ("fence_set", "change"), ("fence_delete", "change")):
    check(f"AC6 {aid} is in the catalogue as {risk}", (C.by_id(aid) or {}).get("risk"), risk)
check("AC6 parity holds", C.parity_problems(C.ACTIONS, W.api_action_routes(), [t["name"] for t in W.mcp_tools("act")]), [])

pip = getattr(B, "point_in_polygon", None); inc = getattr(B, "in_circle", None)
SQ = [[51.0, -1.0], [51.0, -0.9], [51.1, -0.9], [51.1, -1.0]]
L_SHAPE = [[0, 0], [0, 2], [1, 2], [1, 1], [2, 1], [2, 0]]  # the notch is x>1, y>1
check("AC1 inside the square", pip(51.05, -0.95, SQ) if pip else "missing", True)
check("AC1 outside the square", pip(51.2, -0.95, SQ) if pip else "missing", False)
check("AC1 in the L's notch is outside", pip(1.5, 1.5, L_SHAPE) if pip else "missing", False)
check("AC1 in the L's arm is inside", pip(0.5, 1.5, L_SHAPE) if pip else "missing", True)
check("AC1 70 m away is inside a 100 m circle", inc(51.0, -1.0, 51.0, -1.001, 100) if inc else "missing", True)
check("AC1 and outside a 50 m one", inc(51.0, -1.0, 51.0, -1.001, 50) if inc else "missing", False)

state = tempfile.mkdtemp()
br = B.Bridge({"SERIAL": ""}, socket_path=os.path.join(state, "b.sock"), state_dir=state, observe=True)
r = br.op_fence_set(name="Compound", kind="polygon", points=json.dumps(SQ), rule="both") if hasattr(br, "op_fence_set") else {}
fid = r.get("id")
check_true("AC2 a polygon fence is stored and returns an id", bool(fid) and r.get("confirmed") is True, repr(r))
r2 = br.op_fence_set(name="Bad", kind="polygon", points=json.dumps(SQ[:2]), rule="both") if hasattr(br, "op_fence_set") else {}
check_true("AC2 fewer than three points is refused in words", "three" in str(r2.get("error", "")), repr(r2))
r3 = br.op_fence_set(name="Bad", kind="polygon", points=json.dumps(SQ), rule="sideways") if hasattr(br, "op_fence_set") else {}
check_true("AC2 a bad rule is refused in words", "rule" in str(r3.get("error", "")).lower(), repr(r3))
r4 = br.op_fence_set(name="Tiny", kind="circle", lat=51.0, lon=-1.0, radius_m=5, rule="enter") if hasattr(br, "op_fence_set") else {}
check_true("AC2 a 5 m circle is refused", "radius" in str(r4.get("error", "")).lower(), repr(r4))
r5 = br.op_fence_set(name="Gate", kind="circle", lat=51.0, lon=-1.0, radius_m=100, rule="leave", group="Recce") if hasattr(br, "op_fence_set") else {}
check_true("AC2 a circle with a group is stored", bool(r5.get("id")), repr(r5))
fl = br.op_fences() if hasattr(br, "op_fences") else {}
check("AC2 fences lists both", sorted(f.get("name") for f in fl.get("fences", [])), ["Compound", "Gate"])

ft = getattr(B, "fence_transitions", None)
fences = fl.get("fences", [])
def node(lat, lon, group=""):
    return {"id": "!aa000001", "name": "Tracker9", "lat": lat, lon and "lon": lon, "group": group} if False else {"id": "!aa000001", "name": "Tracker9", "lat": lat, "lon": lon, "group": group}
ev, st1 = ft(fences, {}, [node(51.2, -0.95)]) if ft else ([], {})
check("AC3 first sight: the side is recorded, nothing raised", (ev, bool(st1)), ([], True))
ev, st2 = ft(fences, st1, [node(51.05, -0.95)]) if ft else ([], {})
check("AC3 outside then inside raises enter for rule both", [(x.get("fence"), x.get("kind")) for x in ev], [(fid, "enter")])
ev, st3 = ft(fences, st2, [node(51.2, -0.95)]) if ft else ([], {})
check("AC3 inside then outside raises leave", [(x.get("fence"), x.get("kind")) for x in ev], [(fid, "leave")])
# the Gate fence has group Recce and rule leave: a Vehicles node inside then outside raises nothing
ev, s = ft([f for f in fences if f.get("name") == "Gate"], {}, [dict(node(51.0, -1.0, "Vehicles"))]) if ft else ([], {})
ev, s = ft([f for f in fences if f.get("name") == "Gate"], s, [dict(node(51.5, -1.0, "Vehicles"))]) if ft else ([], {})
check("AC4 a fence with a group ignores other groups", ev, [])
ev, s = ft([f for f in fences if f.get("name") == "Gate"], {}, [dict(node(51.0, -1.0, "Recce"))]) if ft else ([], {})
ev, s = ft([f for f in fences if f.get("name") == "Gate"], s, [dict(node(51.5, -1.0, "Recce"))]) if ft else ([], {})
check("AC4 and raises for its own group", [x.get("kind") for x in ev], ["leave"])
ev, s = ft([f for f in fences if f.get("name") == "Gate"], {}, [dict(node(51.5, -1.0, "Recce"))]) if ft else ([], {})
ev, s = ft([f for f in fences if f.get("name") == "Gate"], s, [dict(node(51.0, -1.0, "Recce"))]) if ft else ([], {})
check("AC3 rule leave does not raise on enter", ev, [])

# the alert pass end to end: node outside, then inside, then outside
seq = [[node(51.2, -0.95)], [node(51.05, -0.95)], [node(51.2, -0.95)]]
def kinds():
    return sorted((o.get("kind"), o.get("text", "")[:40]) for o in br.op_alerts().get("open", []))
try:
    for i, rows in enumerate(seq):
        br.op_nodes = (lambda rows=rows: (lambda **_: {"nodes": rows, "count": len(rows)}))()
        br._judge_alerts()
        if i == 1:
            check_true("AC3 the pass raises geofence on entering", any(k == "geofence" and "entered" in t for k, t in kinds()), str(kinds()))
    check_true("AC3 leaving raises and clears the entering alert", any(k == "geofence" and "left" in t for k, t in kinds()) and not any("entered" in t for _, t in kinds()), str(kinds()))
except Exception as ex:  # noqa: BLE001
    check("AC3 the alert pass ran", f"{type(ex).__name__}: {ex}", "ran")
r = br.op_fence_delete(id=fid) if hasattr(br, "op_fence_delete") else {}
check("AC2 fence_delete removes it", (r.get("confirmed"), [f.get("name") for f in (br.op_fences() if hasattr(br, "op_fences") else {}).get("fences", [])]), (True, ["Gate"]))

fb = start_fake_bridge()
srv = W.make_server(bind="127.0.0.1", port=0, socket_path=fb.path, etc_dir=tempfile.mkdtemp(), config={"AUTH": "off"}, state_dir=tempfile.mkdtemp())
port = srv.server_address[1]; threading.Thread(target=srv.serve_forever, daemon=True).start(); time.sleep(0.3)
def get(p):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10); c.request("GET", p); r = c.getresponse(); b = r.read().decode(); c.close(); return r.status, b
st, body = get("/map")
check_true("AC5 the map carries the Fences control, the drawing script and the save form", st == 200 and "id='fence-draw'" in body and "fence_set" in body and "/api/fences" in body and "id='fence-list'" in body)
st, body = get("/api/fences")
check_true("AC5 /api/fences answers with a fences list", st == 200 and isinstance(json.loads(body).get("fences"), list), str(st))
finish()
