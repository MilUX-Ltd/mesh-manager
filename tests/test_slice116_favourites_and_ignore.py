#!/usr/bin/env python3
"""Spec 116: favourites and ignore lists on the gateway radio (1.6.0).

Decided 2 October 2026: managed devices become favourites automatically (D1) and stay so when no longer managed
(D2); a write the radio does not confirm is shown as asked until the radio's node database shows it (D3); Ignore
is refused for a managed device, a favourite and the box's own radio, and ignored nodes are listed with Stop
ignoring (D4); a radio whose firmware cannot keep the lists shows no controls and says why once (D5).

The contract the tests hold:
  bridge  nodes and links carry, per node, favorite, ignored and list_asked (the lists with a request pending), and
          links carries lists: {supported, firmware, min}; op_node_list_set(id, list: favorite|ignored, on: on|off)
          sends the library's setFavorite, removeFavorite, setIgnored or removeIgnored and answers state "asked",
          never confirmed; _node_lists_pass() confirms requests against the radio's node database and asks for a
          favourite for every managed device that is not one.
The fake radio has no favourite calls, so the tests stand in for them and record what the bridge asked. Node ids
are from the synthetic block (LESSONS 42). Every block is guarded.
"""
import html as html_lib, http.client, json, os, re, sys, tempfile, threading, time
sys.path.insert(0, os.path.dirname(__file__))
from _common import ROOT, check, check_true, finish  # noqa: E402
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "tests"))
import fakegw_lib  # noqa: E402
fakegw_lib.install()
import fakebridge_lib  # noqa: E402
from fakebridge_lib import start_fake_bridge  # noqa: E402
from mesh_manager import bridge as B, catalogue as C, web as W  # noqa: E402

FAV, IGN, PLAIN, MANAGED, OWN, WAITING = "!ee000031", "!ee000032", "!ee000033", "!ee000034", "!00000001", "!ee000035"
SERIAL = "/dev/serial/by-id/usb-fake-test-radio-if00"


def bridge(firmware=None):
    st = tempfile.mkdtemp()
    br = B.Bridge({"SERIAL": SERIAL}, socket_path=os.path.join(st, "b.sock"), state_dir=st, observe=True)
    if firmware:
        br.interface.localNode.device_firmware = firmware
    db = {FAV: {"num": 0xee000031, "user": {"id": FAV, "longName": "Lead"}, "isFavorite": True},
          IGN: {"num": 0xee000032, "user": {"id": IGN, "longName": "Noisy"}, "isIgnored": True},
          PLAIN: {"num": 0xee000033, "user": {"id": PLAIN, "longName": "Stranger"}},
          MANAGED: {"num": 0xee000034, "user": {"id": MANAGED, "longName": "Trainee 12"}}}
    br.interface.nodes = db
    br.mesh_nodes = lambda: [{"id": k, "name": v["user"]["longName"], "heard_here": True, "heard": None} for k, v in db.items()]
    br._register_load = lambda: {MANAGED: {"managed": True, "label": "Trainee 12"}, FAV: {"managed": False}}
    calls = []
    node = br.interface.localNode
    for name, field in (("setFavorite", "set_favorite_node"), ("removeFavorite", "remove_favorite_node"),
                        ("setIgnored", "set_ignored_node"), ("removeIgnored", "remove_ignored_node")):
        setattr(node, name, (lambda f: (lambda nid: calls.append((f, nid))))(field))
    return br, db, calls


def rows(br):
    try:
        return {n.get("id"): n for n in br.op_nodes().get("nodes", [])}
    except Exception as ex:  # noqa: BLE001
        return {"error": f"{type(ex).__name__}: {ex}"}


br, db, calls = bridge()

# ---- AC1 the flags are read --------------------------------------------------------------------------------
r = rows(br)
check("AC1 a favourite carries favorite", (r.get(FAV) or {}).get("favorite"), True)
check("AC1 an ignored node carries ignored", (r.get(IGN) or {}).get("ignored"), True)
check("AC1 any other node carries both false", ((r.get(PLAIN) or {}).get("favorite"), (r.get(PLAIN) or {}).get("ignored")), (False, False))
lk = br.op_links() if hasattr(br, "op_links") else {}
check_true("AC1 links carry the nodes' flags and whether the radio can keep the lists", any(n.get("id") == FAV and n.get("favorite") for n in lk.get("nodes", []))
           and (lk.get("lists") or {}).get("supported") is True and bool((lk.get("lists") or {}).get("firmware")), str(lk.get("lists")))

# ---- AC2 a request is sent and recorded, and answers asked ---------------------------------------------------
setter = getattr(br, "op_node_list_set", None)
check_true("AC2 op_node_list_set exists", callable(setter))


def ask(b, **k):
    try:
        return b.op_node_list_set(**k) if hasattr(b, "op_node_list_set") else {"error": "missing"}
    except Exception as ex:  # noqa: BLE001
        return {"error": f"raised {type(ex).__name__}: {ex}"}


a = ask(br, id=PLAIN, list="favorite", on="on")
check("AC2 making a favourite sends set_favorite_node for that node", calls[-1:], [("set_favorite_node", PLAIN)])
check("AC2 and answers asked, never confirmed", (a.get("state"), a.get("confirmed")), ("asked", None))
check("AC2 the request is pending on the node", (rows(br).get(PLAIN) or {}).get("list_asked"), ["favorite"])
for kw, field in ((dict(id=FAV, list="favorite", on="off"), "remove_favorite_node"),
                  (dict(id=PLAIN, list="ignored", on="on"), None),     # PLAIN now has a favourite pending: refused (D4)
                  (dict(id=IGN, list="ignored", on="off"), "remove_ignored_node")):
    n0 = len(calls)
    a = ask(br, **kw)
    if field:
        check(f"AC2 {kw['list']} {kw['on']} sends {field}", calls[n0:], [(field, kw["id"])])
    else:
        check_true("AC5 ignoring a node with a favourite pending is refused, and nothing is sent", bool(a.get("error")) and calls[n0:] == [], repr(a))
n0 = len(calls)
a = ask(br, id="!ee000099", list="favorite", on="on")
check_true("AC2 a node the radio does not hold is refused in words", "!ee000099" in str(a.get("error", "")) and calls[n0:] == [], repr(a))
a = ask(br, id=PLAIN, list="sideways", on="on")
check_true("AC2 an unknown list is refused in words", "list" in str(a.get("error", "")).lower() and calls[n0:] == [], repr(a))

# ---- AC3 confirmation from the radio's node database ---------------------------------------------------------
passer = getattr(br, "_node_lists_pass", None)
check_true("AC3 the bridge has a pass that confirms requests", callable(passer))
if callable(passer):
    db[PLAIN]["isFavorite"] = True                       # the radio now holds it
    passer()
    r = rows(br)
    check("AC3 a request the radio's database shows is confirmed and no longer pending", ((r.get(PLAIN) or {}).get("favorite"), (r.get(PLAIN) or {}).get("list_asked")), (True, []))
    path = os.path.join(br.state_dir, "node-lists.json")
    book = json.load(open(path)) if os.path.exists(path) else {}
    pend = book.get("pending") or {}
    key = f"{FAV}:favorite"
    check_true("AC3 the remove request for the favourite is still pending (the radio has not shown it)", key in pend, str(sorted(pend)))
    if key in pend:
        pend[key]["asked"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 90000))   # over a day ago
        json.dump(book, open(path, "w"))
        passer()
        book = json.load(open(path))
        check("AC3 a request a day old that the radio never took is dropped", key in (book.get("pending") or {}), False)
        check("AC3 and is reported as not taken", ((book.get("done") or {}).get(key) or {}).get("state"), "not taken")

# ---- AC4 the fleet as favourites ---------------------------------------------------------------------------
if callable(passer):
    passer()
    passer()
    check("AC4 the managed device that is not a favourite is asked for, once, over every pass so far", calls.count(("set_favorite_node", MANAGED)), 1)
    check("AC4 and no other device is made a favourite by the pass", sorted({c[1] for c in calls if c[0] == "set_favorite_node"}), sorted({PLAIN, MANAGED}))
    db[MANAGED]["isFavorite"] = True
    passer()
    br._register_load = lambda: {MANAGED: {"managed": False}}   # no longer managed
    n1 = len(calls)
    passer()
    check("AC4 a device that stops being managed is not unfavourited (D2)", [c for c in calls[n1:] if c[0] == "remove_favorite_node"], [])
    br._register_load = lambda: {MANAGED: {"managed": True, "label": "Trainee 12"}, FAV: {"managed": False}}

# ---- AC5 what is never ignored -----------------------------------------------------------------------------
for nid, why in ((MANAGED, "managed"), (FAV, "favourite"), (OWN, "own")):
    n0 = len(calls)
    if nid == FAV:
        db[FAV]["isFavorite"] = True
    a = ask(br, id=nid, list="ignored", on="on")
    check_true(f"AC5 ignoring the {why} node is refused in words, and nothing is sent", bool(a.get("error")) and calls[n0:] == [], repr(a))

# ---- AC7 a radio whose firmware cannot keep the lists ------------------------------------------------------
old, odb, ocalls = bridge(firmware="2.4.3.abcdef0")
lk = old.op_links() if hasattr(old, "op_links") else {}
check("AC7 links say an old radio cannot keep the lists", (lk.get("lists") or {}).get("supported"), False)
a = ask(old, id=PLAIN, list="favorite", on="on")
check_true("AC7 a request to an old radio is refused in words naming its firmware, and nothing is sent", "2.4.3" in str(a.get("error", "")) and ocalls == [], repr(a))
if hasattr(old, "_node_lists_pass"):
    old._node_lists_pass()
    check("AC7 and the fleet pass asks nothing of it", ocalls, [])

# ---- AC8 the catalogue ---------------------------------------------------------------------------------------
act = C.by_id("node_list_set") or {}
check("AC8 node_list_set is a change action", act.get("risk"), "change")
check_true("AC8 it takes id, list and on", {"id", "list", "on"} <= {i.get("name") for i in act.get("inputs", [])}, str([i.get("name") for i in act.get("inputs", [])]))
check("AC8 parity holds", C.parity_problems(C.ACTIONS, W.api_action_routes(), [t["name"] for t in W.mcp_tools("act")]), [])

# ---- AC6, AC7 the screen -----------------------------------------------------------------------------------
fakebridge_lib.STATUS.update({"mode": "server", "tak": "off"})
fb = start_fake_bridge()
srv = W.make_server(bind="127.0.0.1", port=0, socket_path=fb.path, etc_dir=tempfile.mkdtemp(), config={"AUTH": "off"}, state_dir=tempfile.mkdtemp())
threading.Thread(target=srv.serve_forever, daemon=True).start()
time.sleep(0.3)
PORT = srv.server_address[1]
SHAPE = {"lists": {"supported": True, "firmware": "2.6.11", "min": "2.5.0"}}
NODES = [{"id": FAV, "name": "Lead", "heard_here": True, "favorite": True, "ignored": False, "list_asked": []},
         {"id": IGN, "name": "Noisy", "heard_here": True, "favorite": False, "ignored": True, "list_asked": []},
         {"id": PLAIN, "name": "Stranger", "heard_here": True, "favorite": False, "ignored": False, "list_asked": []},
         {"id": WAITING, "name": "Waiting", "heard_here": True, "favorite": False, "ignored": False, "list_asked": ["favorite"]},
         {"id": MANAGED, "name": "Trainee 12", "heard_here": True, "favorite": False, "ignored": False, "list_asked": [], "managed": True}]
_orig = srv.web.client.ask


def _ask(op, *a, **k):
    if op == "links":
        return {"own": {"id": OWN, "name": "TAK Gateway"}, "nodes": [dict(n) for n in NODES], "routes": {}, "lists": SHAPE["lists"]}
    if op == "nodes":
        return {"nodes": [dict(n) for n in NODES], "count": len(NODES), "grouped": False, "lists": SHAPE["lists"]}
    return _orig(op, *a, **k)


srv.web.client.ask = _ask


def get(path):
    c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=10)
    c.request("GET", path)
    resp = c.getresponse()
    out = (resp.status, html_lib.unescape(resp.read().decode(errors="replace")))
    c.close()
    return out


def row_of(page, nid):
    m = re.search(r"<tr\b[^>]*>(?:(?!</tr>).)*" + re.escape(nid) + r"(?:(?!</tr>).)*</tr>", page, re.S)
    return m.group(0) if m else ""


def forms_for(page, lst, on):
    return [f for f in re.findall(r"<form\b[^>]*data-action='node_list_set'[^>]*>.*?</form>", page, re.S)
            if f"name='list' value='{lst}'" in f and f"name='on' value='{on}'" in f]


s, page = get("/nodes")
check("AC6 the Nodes page renders", s, 200)
check_true("AC6 a favourite's row shows the star", "★" in row_of(page, FAV), "")
check_true("AC6 an ignored node's row says ignored", "ignored" in re.sub(r"<[^>]+>", " ", row_of(page, IGN)).lower(), "")
check_true("AC6 a request still pending shows as asked", "asked" in re.sub(r"<[^>]+>", " ", row_of(page, WAITING)).lower(), "")
check_true("AC6 the Nodes page lists ignored nodes with Stop ignoring", any(IGN in f for f in forms_for(page, "ignored", "off")) and "Stop ignoring" in page, "")
s, pg = get("/node?id=" + PLAIN)
check("AC6 a node page renders", s, 200)
check_true("AC6 a stranger's page offers Make a favourite and Ignore", bool(forms_for(pg, "favorite", "on")) and bool(forms_for(pg, "ignored", "on")), "")
s, pg = get("/node?id=" + FAV)
check_true("AC6 a favourite's page offers Remove from favourites and no Ignore", bool(forms_for(pg, "favorite", "off")) and not forms_for(pg, "ignored", "on"), "")
s, pg = get("/node?id=" + WAITING)
check_true("AC6 a node with a favourite asked for says so and offers no Ignore", "asked" in re.sub(r"<[^>]+>", " ", pg).lower() and not forms_for(pg, "ignored", "on"), "")
s, pg = get("/node?id=" + MANAGED)
check_true("AC6 a managed device's page offers no Ignore", not forms_for(pg, "ignored", "on"), "")
s, pg = get("/node?id=" + IGN)
check_true("AC6 an ignored node's page offers Stop ignoring", bool(forms_for(pg, "ignored", "off")), "")
check_true("AC6 every list form posts", all("method='post'" in f for f in re.findall(r"<form\b[^>]*data-action='node_list_set'[^>]*>", page + pg)), "")

SHAPE["lists"] = {"supported": False, "firmware": "2.4.3", "min": "2.5.0"}
s, page = get("/nodes")
check("AC7 with an old radio the Nodes page draws no list control", len(re.findall(r"data-action='node_list_set'", page)), 0)
check("AC7 and says why once", page.count("cannot keep favourites"), 1)
s, pg = get("/node?id=" + PLAIN)
check("AC7 nor does the node page", len(re.findall(r"data-action='node_list_set'", pg)), 0)

finish()
