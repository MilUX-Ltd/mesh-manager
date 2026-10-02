#!/usr/bin/env python3
"""Spec 115: geofences that do not cry wolf, and alerts stamped with the crossing's time (1.6.0).

Decided 1 October 2026: each fence chooses a number of consecutive positions on the new side (1 to 5) or a band in
metres past the line (5 to 500), new and unset fences 2 positions (D1, D3); the alert carries the time the box heard
the first position on the new side (D2); and it names that time and the rule that fired (D4).

The contract the tests hold:
  fence_depth(fence, lat, lon)                 signed metres to the boundary, positive inside
  fence_crossings(fences, state, positions)    (events, state); positions in order, each {node, name, group, lat,
                                               lon, ts}; an event {fence, fence_name, node, name, kind, ts, rule}
  the alert text   "{name} entered|left {fence} at HH:MM ({rule})", or "at the time of the check" with no store
Positions and times are synthetic and relative to now (LESSONS 42, 43, 47). Every block is guarded.
"""
import calendar, http.client, json, math, os, sys, tempfile, threading, time
sys.path.insert(0, os.path.dirname(__file__))
from _common import ROOT, check, check_true, finish  # noqa: E402
sys.path.insert(0, os.path.join(ROOT, "src"))
import fakegw_lib  # noqa: E402
fakegw_lib.install()
from fakebridge_lib import start_fake_bridge  # noqa: E402
from mesh_manager import bridge as B, catalogue as C, web as W  # noqa: E402


def utc(secs_ago):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - secs_ago))


def hhmm(iso):
    return time.strftime("%H:%M", time.localtime(calendar.timegm(time.strptime(iso, "%Y-%m-%dT%H:%M:%SZ"))))   # the box's local time, summer time included


def east(lat, lon, metres):   # a point the given distance east along the parallel
    return lon + metres / (111320.0 * math.cos(math.radians(lat)))


def north(lat, metres):
    return lat + metres / 111195.0


SQ = [[51.0, -1.0], [51.0, -0.9], [51.1, -0.9], [51.1, -1.0]]
L_SHAPE = [[0, 0], [0, 2], [1, 2], [1, 1], [2, 1], [2, 0]]  # the notch is x>1, y>1
CIRCLE = {"id": "c1", "name": "Ring", "kind": "circle", "centre": [51.0, -1.0], "radius_m": 500, "rule": "both", "enabled": True}

# ---- AC1 the depth --------------------------------------------------------------------------------------
fd = getattr(B, "fence_depth", None)
check_true("AC1 bridge.fence_depth exists", callable(fd))


def depth(f, lat, lon):
    try:
        return fd(f, lat, lon) if callable(fd) else None
    except Exception as ex:  # noqa: BLE001
        return f"raised {type(ex).__name__}"


def near(got, want, tol=1.0):
    return isinstance(got, (int, float)) and abs(got - want) <= tol


sqf = {"id": "s1", "name": "Square", "kind": "polygon", "points": SQ, "rule": "both", "enabled": True}
check_true("AC1 circle: 70 m from the centre of a 500 m circle is 430 m inside", near(depth(CIRCLE, 51.0, east(51.0, -1.0, 70)), 430), repr(depth(CIRCLE, 51.0, east(51.0, -1.0, 70))))
check_true("AC1 circle: 650 m from the centre is 150 m outside", near(depth(CIRCLE, 51.0, east(51.0, -1.0, 650)), -150), repr(depth(CIRCLE, 51.0, east(51.0, -1.0, 650))))
check_true("AC1 circle: on the line is nought", near(depth(CIRCLE, 51.0, east(51.0, -1.0, 500)), 0), "")
check_true("AC1 polygon: 100 m inside the east edge", near(depth(sqf, 51.05, east(51.05, -0.9, -100)), 100), repr(depth(sqf, 51.05, east(51.05, -0.9, -100))))
check_true("AC1 polygon: 100 m outside the east edge", near(depth(sqf, 51.05, east(51.05, -0.9, 100)), -100), repr(depth(sqf, 51.05, east(51.05, -0.9, 100))))
check_true("AC1 polygon: 100 m inside the south edge", near(depth(sqf, north(51.0, 100), -0.95), 100), repr(depth(sqf, north(51.0, 100), -0.95)))
lf = {"id": "l1", "name": "L", "kind": "polygon", "points": L_SHAPE, "rule": "both", "enabled": True}
check_true("AC1 polygon: the concave notch is outside (negative)", isinstance(depth(lf, 1.5, 1.5), (int, float)) and depth(lf, 1.5, 1.5) < 0, repr(depth(lf, 1.5, 1.5)))
check_true("AC1 polygon: the L's arm is inside (positive)", isinstance(depth(lf, 0.5, 1.5), (int, float)) and depth(lf, 0.5, 1.5) > 0, repr(depth(lf, 0.5, 1.5)))

# ---- the pure crossings ---------------------------------------------------------------------------------
fc = getattr(B, "fence_crossings", None)
check_true("AC2 bridge.fence_crossings exists", callable(fc))


def run(fence, seq, state=None):
    """Feed positions one at a time, as passes would; return every event and the final state."""
    evs, st = [], dict(state or {})
    for lat, lon, ago in seq:
        try:
            e, st = fc([fence], st, [{"node": "!ee000021", "name": "Walker", "group": "", "lat": lat, "lon": lon, "ts": utc(ago)}])
        except Exception as ex:  # noqa: BLE001
            return [f"raised {type(ex).__name__}: {ex}"], st
        evs += e
    return evs, st


OUT, IN = (51.2, -0.95), (51.05, -0.95)
sq2 = dict(sqf)            # no debounce stored: reads as 2 positions (D3)
if callable(fc):
    ev, st = run(sq2, [(*OUT, 600)])
    check("AC2 the first sight records the side and raises nothing", ev, [])
    ev, st2 = run(sq2, [(*IN, 540)], st)
    check("AC2 one position across the line raises nothing", ev, [])
    ev, st3 = run(sq2, [(*IN, 480)], st2)
    check("AC2 the second in a row on the new side raises one enter", [(x.get("kind"), x.get("fence")) for x in ev], [("enter", "s1")])
    check("AC2 and the crossing's time is the first position on the new side (D2)", [x.get("ts") for x in ev], [utc(540)])
    ev, _ = run(sq2, [(*OUT, 600)] + [(*(IN if i % 2 == 0 else OUT), 500 - i * 30) for i in range(10)])
    check("AC2 ten positions alternating across the line raise nothing", ev, [])
    sq1 = dict(sqf, debounce={"by": "positions", "n": 1})
    ev, _ = run(sq1, [(*OUT, 600), (*IN, 540)])
    check("AC2 a fence set to 1 position raises on one, as Spec 045 did", [x.get("kind") for x in ev], ["enter"])
    ev, _ = run(dict(sqf, debounce={"by": "positions", "n": 3}), [(*OUT, 600), (*IN, 540), (*IN, 480)])
    check("AC2 a fence set to 3 does not raise on 2", ev, [])

# ---- AC3 the metres rule --------------------------------------------------------------------------------
ring = dict(CIRCLE, debounce={"by": "metres", "m": 40})
if callable(fc):
    def at(r):            # a point r metres east of the ring's centre
        return 51.0, east(51.0, -1.0, r)
    ev, st = run(ring, [(*at(600), 900)])                       # outside, 100 m out
    ev, st = run(ring, [(*at(475), 840)], st)                   # 25 m inside: within the band
    check("AC3 25 m past the line raises nothing", ev, [])
    ev, st = run(ring, [(*at(455), 780)], st)                   # 45 m inside
    check("AC3 45 m past the line raises enter", [x.get("kind") for x in ev], ["enter"])
    check("AC3 and its time is the first position past the line, not the one that proved it", [x.get("ts") for x in ev], [utc(840)])
    ev, st = run(ring, [(*at(525), 720), (*at(490), 660), (*at(530), 600)], st)   # within the band outside and in
    check("AC3 positions within the band never flip the side", ev, [])
    ev, st = run(ring, [(*at(545), 540)], st)                   # 45 m outside
    check("AC3 45 m outside raises leave", [x.get("kind") for x in ev], ["leave"])

# ---- AC6 the rule is named ------------------------------------------------------------------------------
if callable(fc):
    ev, _ = run(sq2, [(*OUT, 600), (*IN, 540), (*IN, 480)])
    check("AC6 the positions rule is named", [x.get("rule") for x in ev], ["2 positions inside"])
    ev, _ = run(sq2, [(*IN, 600), (*OUT, 540), (*OUT, 480)])
    check("AC6 and on leaving", [x.get("rule") for x in ev], ["2 positions outside"])
    ev, _ = run(ring, [(*at(600), 900), (*at(455), 780)])
    check("AC6 the metres rule is named", [x.get("rule") for x in ev], ["40 m past the line"])

# ---- AC7 the setting ------------------------------------------------------------------------------------
state = tempfile.mkdtemp()
br = B.Bridge({"SERIAL": "/dev/serial/by-id/usb-fake-test-radio-if00"}, socket_path=os.path.join(state, "b.sock"), state_dir=state, observe=True)
r = br.op_fence_set(name="Training area", kind="polygon", points=json.dumps(SQ), rule="both", debounce_by="positions", debounce=2)
fid = r.get("id")
check_true("AC7 a fence stores a positions debounce", bool(fid) and (next((f for f in br.op_fences().get("fences", []) if f.get("id") == fid), {}).get("debounce") == {"by": "positions", "n": 2}), repr(r))
r = br.op_fence_set(name="Road", kind="circle", lat=51.0, lon=-1.0, radius_m=500, rule="both", debounce_by="metres", debounce=40)
check_true("AC7 and a metres one", bool(r.get("id")) and (next((f for f in br.op_fences().get("fences", []) if f.get("id") == r.get("id")), {}).get("debounce") == {"by": "metres", "m": 40}), repr(r))
for by, n in (("positions", 0), ("positions", 6), ("metres", 4), ("metres", 501), ("sideways", 2)):
    rr = br.op_fence_set(name="Bad", kind="polygon", points=json.dumps(SQ), rule="both", debounce_by=by, debounce=n)
    check_true(f"AC7 debounce {by} {n} is refused in words", "debounce" in str(rr.get("error", "")).lower() and not rr.get("id"), repr(rr))
r = br.op_fence_set(name="Plain", kind="polygon", points=json.dumps(SQ), rule="both")
check_true("AC7 a fence made without one is accepted", bool(r.get("id")), repr(r))
names = [i.get("name") for i in (C.by_id("fence_set") or {}).get("inputs", [])]
check_true("AC7 the catalogue's fence_set takes debounce_by and debounce", "debounce_by" in names and "debounce" in names, str(names))
check("AC7 parity holds", C.parity_problems(C.ACTIONS, W.api_action_routes(), [t["name"] for t in W.mcp_tools("act")]), [])
for f in list(br.op_fences().get("fences", [])):
    br.op_fence_delete(id=f.get("id"))

# ---- AC4, AC5 the pass: every position in order, stamped with the crossing ------------------------------
h = getattr(br, "history", None)
check_true("setup: the bridge has a history store", bool(h and h.ok))
r = br.op_fence_set(name="Training area", kind="polygon", points=json.dumps(SQ), rule="both")   # unset: 2 positions
fid = r.get("id")
CUR = {"pos": OUT}


def nodes_now(**_):
    return {"nodes": [{"id": "!ee000021", "name": "Walker", "lat": CUR["pos"][0], "lon": CUR["pos"][1], "group": ""}], "count": 1}


br.op_nodes = nodes_now


def open_geofence():
    return [o for o in br.op_alerts().get("open", []) if o.get("kind") == "geofence"]


try:
    br._judge_alerts()                                          # first look: outside, recorded, nothing raised
    check("AC4 the first pass raises nothing", open_geofence(), [])
    t1, t2 = utc(240), utc(200)                                 # heard minutes before the pass, in the same minute
    h.position("!ee000021", IN[0], IN[1], ts=t1)
    h.position("!ee000021", IN[0], IN[1], ts=t2)
    CUR["pos"] = IN
    br._judge_alerts()
    og = open_geofence()
    check("AC5 two positions heard between passes raise the alert at the next pass", len(og), 1)
    o = og[0] if og else {}
    check("AC4 the open alert's since is the first position on the new side, not the pass", o.get("since"), t1)
    check_true("AC4 the text says when it crossed and by which rule", f"at {hhmm(t1)} (2 positions inside)" in str(o.get("text")), repr(o.get("text")))
    rows = [x for x in h.query("alerts", node="!ee000021") if x.get("kind") == "geofence"]
    check("AC4 the history row carries the crossing's time", [x.get("ts") for x in rows][-1:], [t1])
    br._judge_alerts()
    check("AC4 a pass with nothing new raises nothing more", len(open_geofence()), 1)
except Exception as ex:  # noqa: BLE001
    check("AC4 the alert pass ran", f"{type(ex).__name__}: {ex}", "ran")

# ---- AC6 without the history store -------------------------------------------------------------------------
st2 = tempfile.mkdtemp()
b2 = B.Bridge({"SERIAL": "/dev/serial/by-id/usb-fake-test-radio-if00"}, socket_path=os.path.join(st2, "b.sock"), state_dir=st2, observe=True)
b2.history = None
b2.op_fence_set(name="Training area", kind="polygon", points=json.dumps(SQ), rule="both")
CUR2 = {"pos": OUT}
b2.op_nodes = lambda **_: {"nodes": [{"id": "!ee000021", "name": "Walker", "lat": CUR2["pos"][0], "lon": CUR2["pos"][1], "group": ""}], "count": 1}
try:
    b2._judge_alerts()
    CUR2["pos"] = IN
    b2._judge_alerts()
    check("AC6 no store: one check inside raises nothing (each check is one position)", [o for o in b2.op_alerts().get("open", []) if o.get("kind") == "geofence"], [])
    CUR2["pos"] = (IN[0] + 0.001, IN[1])
    b2._judge_alerts()
    og = [o for o in b2.op_alerts().get("open", []) if o.get("kind") == "geofence"]
    check_true("AC6 the second raises, and says its time is the check's", len(og) == 1 and "at the time of the check" in str(og[0].get("text")), repr(og))
except Exception as ex:  # noqa: BLE001
    check("AC6 the alert pass ran without a store", f"{type(ex).__name__}: {ex}", "ran")

# ---- AC7 the map's form ---------------------------------------------------------------------------------
fb = start_fake_bridge()
srv = W.make_server(bind="127.0.0.1", port=0, socket_path=fb.path, etc_dir=tempfile.mkdtemp(), config={"AUTH": "off"}, state_dir=tempfile.mkdtemp())
port = srv.server_address[1]; threading.Thread(target=srv.serve_forever, daemon=True).start(); time.sleep(0.3)
c = http.client.HTTPConnection("127.0.0.1", port, timeout=10); c.request("GET", "/map"); resp = c.getresponse(); body = resp.read().decode(); c.close()
check("AC7 the map renders", resp.status, 200)
check_true("AC7 the fence form offers the choice of rule", "name='debounce_by'" in body and "name='debounce'" in body and "positions" in body and "metres" in body.lower(), "")

finish()
