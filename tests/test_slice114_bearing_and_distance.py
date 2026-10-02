#!/usr/bin/env python3
"""Spec 114: bearing and distance from the box (1.6.0).

Each node with a fix says how far it is from the box and which way, in the node list, on the node page and in
the direct-message header. Decided 1 October 2026: metres under 1 km, then kilometres (D1); degrees true with the
compass point, 047 deg NE (D2); no distances at all when the box's position is only an estimate from the nodes it
hears, said once (D3); and a fix older than Health's Silent after shows its age (D4).

The text is one rule, `web.range_text(own, node, silent_min)`, so every place shows the same words:
  under 1 km       "200 m 000° N"
  1 km to 10 km    "2.3 km 045° NE"
  10 km and over   "25 km 090° E"
  an old fix adds  " · fix 3 h ago"
Positions are made up, near the synthetic block's (LESSONS 42); times are relative to now (LESSONS 43, 47).
Every block is guarded: a missing helper is one verdict, not a traceback hiding the rest.
"""
import html as html_lib, http.client, math, os, re, sys, tempfile, threading, time
sys.path.insert(0, os.path.dirname(__file__))
from _common import ROOT, check, check_true, finish  # noqa: E402
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "tests"))
import fakegw_lib  # noqa: E402
fakegw_lib.install()
import fakebridge_lib  # noqa: E402
from fakebridge_lib import start_fake_bridge  # noqa: E402
from mesh_manager import radiopos as R, web as W  # noqa: E402

FORMAT = re.compile(r"^(\d+ m|\d+\.\d km|\d+ km) (\d{3})° (N|NE|E|SE|S|SW|W|NW)( · fix .+ ago)?$")


def utc(secs_ago):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - secs_ago))


def north(lat, metres):    # a point due north of lat, the given distance away (one degree of latitude is 111.2 km here)
    return lat + metres / 111195.0


BOX = {"id": "!00000001", "name": "TAK Gateway", "lat": 51.2, "lon": -1.5, "position_source": "gps"}

# ---- AC1 the arithmetic ----------------------------------------------------------------------------------
bd = getattr(R, "bearing_degrees", None)
check_true("AC1 radiopos.bearing_degrees exists", callable(bd))
if callable(bd):
    def near(a, b, tol=0.5):
        return abs(((a - b) + 180) % 360 - 180) <= tol
    cases = [("due north", (51.0, 0.0), (52.0, 0.0), 0.0), ("due east on the equator", (0.0, 0.0), (0.0, 1.0), 90.0),
             ("due south", (52.0, 0.0), (51.0, 0.0), 180.0), ("due west on the equator", (0.0, 1.0), (0.0, 0.0), 270.0),
             ("north-east", (0.0, 0.0), (1.0, 1.0), 45.0), ("south-east", (1.0, 0.0), (0.0, 1.0), 135.0),
             ("south-west", (1.0, 1.0), (0.0, 0.0), 225.0), ("north-west", (0.0, 1.0), (1.0, 0.0), 315.0),
             ("east across the antimeridian", (0.0, 179.5), (0.0, -179.5), 90.0),
             ("west across the antimeridian", (0.0, -179.5), (0.0, 179.5), 270.0)]
    for name, a, b, want in cases:
        got = bd(a[0], a[1], b[0], b[1])
        check_true(f"AC1 bearing {name} is {want:.0f} deg to half a degree", near(got, want) and 0 <= got < 360, f"got {got:.3f}")

rt = getattr(W, "range_text", None)
check_true("AC1 web.range_text exists", callable(rt))


def text(own, node, silent_min=30):
    try:
        return rt(own, node, silent_min) if callable(rt) else None
    except Exception as ex:  # noqa: BLE001
        return f"raised {type(ex).__name__}: {ex}"


def metres_in(t):
    m = re.match(r"^(\d+(?:\.\d)?) (m|km)", t or "")
    return None if not m else float(m.group(1)) * (1000 if m.group(2) == "km" else 1)


# ---- AC2 a node with a fix, a box with a trusted position --------------------------------------------------
n200 = {"id": "!ee000011", "name": "Walker", "lat": north(51.2, 200), "lon": -1.5}
t = text(BOX, n200)
check_true("AC2 a node 200 m north reads in metres, three-figure bearing, compass point", bool(FORMAT.match(t or "")), repr(t))
check_true("AC2 and the distance agrees with metres_between to the metre", metres_in(t) is not None and abs(metres_in(t) - round(R.metres_between(51.2, -1.5, n200["lat"], -1.5))) <= 1, repr(t))
check_true("AC2 and the bearing is 000 N", (t or "").endswith("000° N"), repr(t))
n2k = {"id": "!ee000012", "name": "Out", "lat": 51.2 + 0.0147, "lon": -1.5 + 0.0235}   # about 2.3 km to the north-east
t = text(BOX, n2k)
check_true("AC2 between 1 and 10 km it reads in km to one decimal", bool(re.match(r"^\d\.\d km 04\d° NE$", t or "")), repr(t))
n25 = {"id": "!ee000013", "name": "Far", "lat": 51.2, "lon": -1.5 + 0.359}            # about 25 km due east
t = text(BOX, n25)
check_true("AC2 from 10 km it reads in whole km", bool(re.match(r"^2\d km 09\d° E$", t or "")), repr(t))
check("AC2 the box's own node carries no distance", text(BOX, dict(BOX)), "")
for src in ("gps", "radio_gps", "declared", "config"):
    check_true(f"AC2 a box position from {src} is trusted", bool(text(dict(BOX, position_source=src), n200)), "")

# ---- AC3 no fix, no box position --------------------------------------------------------------------------
check("AC3 a node with no fix carries no distance", text(BOX, {"id": "!ee000014", "name": "Dark"}), "")
check("AC3 a box with no position gives no distance", text({"id": "!00000001"}, n200), "")
check("AC3 and no own position at all gives none", text(None, n200), "")

# ---- AC4 an estimated box position --------------------------------------------------------------------------
check("AC4 a box position estimated from the nodes it hears gives no distance (D3)", text(dict(BOX, position_source="devices"), n200), "")
check("AC4 nor does a position of no stated source", text(dict(BOX, position_source=None), n200), "")

# ---- AC6 an old fix ---------------------------------------------------------------------------------------
old = dict(n200, fix_ts=utc(3 * 3600))
young = dict(n200, fix_ts=utc(5 * 60))
t_old, t_young = text(BOX, old, 30), text(BOX, young, 30)
check_true("AC6 a fix older than Silent after shows its age", bool(FORMAT.match(t_old or "")) and (t_old or "").endswith(" · fix " + W.age(old["fix_ts"])), repr(t_old))
check_true("AC6 a fix younger than it shows the distance alone", bool(FORMAT.match(t_young or "")) and "fix" not in (t_young or ""), repr(t_young))
check_true("AC6 a fix of unknown time shows the distance alone", "fix" not in (text(BOX, n200, 30) or "x"), "")

# ---- the pages: AC2, AC3, AC4, AC5 -------------------------------------------------------------------------
fakebridge_lib.STATUS.update({"mode": "server", "tak": "off"})
fb = start_fake_bridge()
srv = W.make_server(bind="127.0.0.1", port=0, socket_path=fb.path, etc_dir=tempfile.mkdtemp(), config={"AUTH": "off"}, state_dir=tempfile.mkdtemp())
threading.Thread(target=srv.serve_forever, daemon=True).start()
time.sleep(0.3)
PORT = srv.server_address[1]
SHAPE = {"own": dict(BOX)}
NODES = [dict(BOX, heard=utc(60), heard_here=True), dict(n200, heard=utc(60), heard_here=True, fix_ts=utc(60)),
         {"id": "!ee000014", "name": "Dark", "heard": utc(60), "heard_here": True}]
_orig = srv.web.client.ask
ASKED = []


def _ask(op, *a, **k):
    ASKED.append(op)
    if op == "links":
        return {"own": SHAPE["own"], "nodes": [dict(n) for n in NODES], "routes": {}}
    if op == "nodes":
        return {"nodes": [dict(n) for n in NODES], "count": len(NODES), "grouped": False}
    if op == "node":
        return {"node": next((dict(n) for n in NODES if n["id"] == k.get("id")), None)}
    return _orig(op, *a, **k)


srv.web.client.ask = _ask


def get(path):
    c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=10)
    c.request("GET", path)
    r = c.getresponse()
    out = (r.status, r.read().decode(errors="replace"))
    c.close()
    return out


def row_of(page, nid):
    m = re.search(r"<tr\b[^>]*>(?:(?!</tr>).)*" + re.escape(nid) + r"(?:(?!</tr>).)*</tr>", page, re.S)
    return html_lib.unescape(m.group(0)) if m else ""


want = text(BOX, NODES[1])
s, page = get("/nodes")
check("AC2 the Nodes page renders", s, 200)
check_true("AC2 the walker's row carries its distance and bearing", bool(want) and want in row_of(page, n200["id"]), repr(want))
check_true("AC2 the box's own row carries none", not re.search(r"\d+ (m|km) \d{3}° ", re.sub(r"<[^>]+>", " ", row_of(page, BOX["id"]))), "")
check_true("AC3 a node with no fix carries none", not re.search(r"\d+ (m|km) \d{3}° ", re.sub(r"<[^>]+>", " ", row_of(page, "!ee000014"))), "")
check_true("AC2 with a trusted position the page does not say there are no distances", "No distances" not in page)

SHAPE["own"] = dict(BOX, position_source="devices")
s, page = get("/nodes")
check_true("AC4 with an estimated position no row carries a distance", not re.search(r"\d+ (m|km) \d{3}° (N|NE|E|SE|S|SW|W|NW)\b", re.sub(r"<[^>]+>", " ", page)), "")
check("AC4 and the page says once why there are none", page.count("No distances"), 1)
check_true("AC4 and points to Where it is", bool(re.search(r"No distances.{0,300}href=['\"]/computer/where['\"]", page, re.S)), "")
SHAPE["own"] = {"id": BOX["id"], "name": BOX["name"], "lat": None, "lon": None, "position_source": None}
s, page = get("/nodes")
check("AC3 with no box position the page says once why there are none", page.count("No distances"), 1)
SHAPE["own"] = dict(BOX)

s, page = get("/node?id=" + n200["id"])
check("AC5 the node page renders", s, 200)
check_true("AC5 the node page shows the same text as the row", bool(want) and want in html_lib.unescape(page), repr(want))

n0 = len(ASKED)
s, page = get("/messages")
check("AC5 the messages page renders", s, 200)
check_true("AC5 the page carries each node's text for the message header", bool(want) and f"data-range=\"{want}\"" in html_lib.unescape(page).replace("'", '"'), repr(want))
scr = "\n".join(re.findall(r"(?is)<script\b[^>]*>(.*?)</script>", page))
check_true("AC5 the header's script reads it from the page, not by a request of its own", "dataset.range" in scr and not re.search(r"fetch\([^)]*range", scr), "")

# ---- the bridge puts the fix's time on the node (for D4) -----------------------------------------------------
from mesh_manager import bridge as B  # noqa: E402
st = tempfile.mkdtemp()
br = B.Bridge({"SERIAL": "/dev/null"}, socket_path=os.path.join(st, "b.sock"), state_dir=st, observe=True)
when = int(time.time()) - 7200
try:
    nid = next(iter(br.interface.nodes)) if isinstance(getattr(br.interface, "nodes", None), dict) and br.interface.nodes else None
    if nid is None:
        br.interface.nodes = {"!aa000001": {"num": 1, "user": {"id": "!aa000001"}}}
        nid = "!aa000001"
    br.interface.nodes[nid].setdefault("position", {})
    br.interface.nodes[nid]["position"].update({"latitude": 51.21, "longitude": -1.49, "time": when})
    rows = {n.get("id"): n for n in br.op_nodes().get("nodes", [])}
    got = (rows.get(nid) or {}).get("fix_ts")
    check("AC6 the bridge gives a node the time of its fix, from the radio's database", got, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(when)))
except Exception as ex:  # noqa: BLE001
    check_true("AC6 the bridge gives a node the time of its fix, from the radio's database", False, f"raised {type(ex).__name__}: {ex}")

finish()
