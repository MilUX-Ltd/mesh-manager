#!/usr/bin/env python3
"""Spec 107: the menu rail, the identity block and the page map (1.5.0).

In 1.2.2 the pages hang off a header bar of five and a More menu of twelve in three groups, one of them
headed "The box" even on a laptop. Decision D1 (Matt, 30 September 2026) replaces that with a rail of
eight, the same on every kind of computer, with most of today's pages as tabs under one of the eight, and
an identity block above it that says which computer this is.

The contract is observable: the rendered HTML of every screen route through the web layer, against the
fake bridge, in the three shapes (laptop, box, hub). The hooks the spec fixes are few and stated there:
the rail is a nav labelled Menu, the current item carries aria-current="page", the identity block carries
data-identity, and tabs are links inside main.

Every block is guarded: a missing helper or a missing rail is one verdict per check, never a traceback.
"""
import html as H, http.client, os, re, socket, sys, tempfile, threading, time
sys.path.insert(0, os.path.dirname(__file__))
from _common import ROOT, check, check_true, finish  # noqa: E402
sys.path.insert(0, os.path.join(ROOT, "src"))
try:
    import fakebridge_lib as fb  # noqa: E402
    from mesh_manager import web as W  # noqa: E402
except Exception as ex:  # noqa: BLE001
    print(f"FAIL imports                                                         {type(ex).__name__}: {ex}")
    print("\nFAILURES: 1"); sys.exit(1)

EIGHT = ["Home", "Map", "Nodes", "Devices and channels", "Messages", "Connect", "Health", "This computer"]

# D1: where every 1.2.2 page goes. /map/full stays a bare page (a wall screen has no menu); /help is the
# link at the foot of the rail and belongs to no section.
PAGE_MAP = {"/": "Home", "/map": "Map",
            "/nodes": "Nodes", "/node?id=!aa000001": "Nodes", "/graph": "Nodes",
            "/register": "Devices and channels", "/bench": "Devices and channels", "/channels": "Devices and channels",
            "/messages": "Messages",
            "/connections": "Connect", "/activity": "Connect",
            "/health": "Health", "/log": "Health", "/packets": "Health",
            "/radio": "This computer", "/settings": "This computer", "/about": "This computer"}
ROUTES = list(PAGE_MAP) + ["/help"]

# D1 tab names. "A node" is reached from a row, not from a tab strip, so it is not asked for here.
TABS = {"Map": ["Map", "Full screen"],
        "Nodes": ["All nodes", "Neighbours"],
        "Devices and channels": ["My devices", "Add a device", "Bench", "Channels"],
        "Messages": ["Chats", "Quick messages"],
        "Connect": ["Other Mesh Managers", "MQTT", "TAK", "AI agents"],
        "Health": ["Alerts and airtime", "Log", "Packets"],
        "This computer": ["This radio", "Where it is", "Updates", "About"]}

HOST = socket.gethostname()
# shape, the screen's config, the status the bridge reports, the name the identity block must show, its kind
SHAPES = [("laptop", {"AUTH": "off", "MODE": "desktop"},
           {"mode": "desktop", "site": None, "radio_present": True}, HOST, "Laptop"),
          ("box", {"AUTH": "off", "MODE": "tak-server"},
           {"mode": "tak-server", "site": {"id": "ab" * 32, "name": "Edge kit"}, "radio_present": True}, "Edge kit", "Box"),
          ("hub", {"AUTH": "off", "MODE": "hub"},
           {"mode": "hub", "site": {"id": "cd" * 32, "name": "Dev hub"}, "radio": None, "radio_present": False}, "Dev hub", "Hub")]
BASE_STATUS = dict(fb.STATUS)


def serve(cfg):
    b = fb.start_fake_bridge()
    s = W.make_server(bind="127.0.0.1", port=0, socket_path=b.path, etc_dir=tempfile.mkdtemp(),
                      config=cfg, state_dir=tempfile.mkdtemp())
    threading.Thread(target=s.serve_forever, daemon=True).start()
    time.sleep(0.3)
    return s.server_address[1]


def get(port, path, hops=2):
    """GET, following a redirect or two on the same screen (a 1.2.2 page may become a redirect to its tab)."""
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    c.request("GET", path)
    r = c.getresponse(); body = r.read().decode("utf-8", "replace"); loc = r.getheader("Location") or ""
    c.close()
    if r.status in (301, 302, 303, 307, 308) and hops and loc.startswith("/"):
        return get(port, loc, hops - 1)
    return r.status, body


def element(src, start):
    """The whole element whose opening tag starts at src[start], nested tags of the same name included."""
    m = re.match(r"<([a-zA-Z0-9]+)", src[start:])
    if not m:
        return ""
    tag = m.group(1).lower()
    depth, i = 0, start
    for t in re.finditer(r"<(/?)%s\b[^>]*>" % tag, src[start:], re.I):
        depth += -1 if t.group(1) else 1
        if depth == 0:
            return src[start:start + t.end()]
    return src[start:]


def text(frag):
    t = re.sub(r"(?is)<script\b.*?</script>|<style\b.*?</style>|<svg\b.*?</svg>", " ", frag)
    return re.sub(r"\s+", " ", H.unescape(re.sub(r"(?s)<[^>]+>", " ", t))).strip()


def links(frag):
    out = []
    for m in re.finditer(r"<a\b([^>]*)>(.*?)</a>", frag, re.S | re.I):
        href = re.search(r"href=['\"]([^'\"]*)['\"]", m.group(1))
        name = re.sub(r"\s*\d+$", "", text(m.group(2)))      # a waiting-count badge is not part of the name
        out.append({"href": H.unescape(href.group(1)) if href else "", "name": name,
                    "current": re.search(r"aria-current=['\"]page['\"]", m.group(1)) is not None})
    return out


def rail(page):
    m = re.search(r"<nav\b[^>]*aria-label=['\"]Menu['\"][^>]*>", page)
    return element(page, m.start()) if m else None


def main_of(page):
    m = re.search(r"<main\b[^>]*>", page)
    return element(page, m.start()) if m else ""


# ---- AC3 identity(st), beside this_box() -------------------------------------------------------------
HAVE_ID = callable(getattr(W, "identity", None))
if not HAVE_ID:
    check_true("AC3 identity(st) names this computer, its kind and its radio", False, "no identity() in web.py yet")
else:
    def ident(st, desktop=False):
        W.set_shape(desktop)
        try:
            return W.identity(st)
        except Exception as ex:  # noqa: BLE001
            return {"error": f"{type(ex).__name__}: {ex}"}
    for mode, kind in (("desktop", "Laptop"), ("server", "Box"), ("tak-server", "Box"), ("hub", "Hub")):
        got = ident({"mode": mode, "site": {"name": "Edge kit"}, "radio_present": mode != "hub"}, desktop=mode == "desktop")
        check(f"AC3 mode {mode} is a {kind}", (got.get("kind"), got.get("name")), (kind, "Edge kit"))
    check("AC3 no site name: the hostname", ident({"mode": "server", "site": None, "radio_present": True}).get("name"), HOST)
    check("AC3 a blank site name is no name: the hostname",
          ident({"mode": "server", "site": {"name": "  "}, "radio_present": True}).get("name"), HOST)
    check("AC3 a radio plugged in reads radio connected",
          ident({"mode": "tak-server", "radio_present": True}).get("radio"), "radio connected")
    check("AC3 a laptop or box without one is watching for a radio",
          ident({"mode": "desktop", "radio": None, "radio_present": False}, desktop=True).get("radio"), "watching for a radio")
    check("AC3 a hub has no radio, whatever else the status says",
          ident({"mode": "hub", "radio_present": True}).get("radio"), "no radio")
    check("AC3 a bridge that did not answer: the shape decides, laptop", ident({}, desktop=True).get("kind"), "Laptop")
    check("AC3 and box", (ident(None).get("kind"), ident(None).get("name")), ("Box", HOST))

# ---- render every route in every shape ---------------------------------------------------------------
PAGES = {}
for shape, cfg, st, _, _ in SHAPES:
    fb.STATUS.clear(); fb.STATUS.update(BASE_STATUS); fb.STATUS.update(st)
    port = serve(cfg)
    PAGES[shape] = {"port": port, "status": dict(fb.STATUS)}
    for r in ROUTES:
        PAGES[shape][r] = get(port, r)
fb.STATUS.clear(); fb.STATUS.update(BASE_STATUS)

# ---- AC1 the rail of eight, in order, on every page, every shape -------------------------------------
for shape, *_ in SHAPES:
    bad = []
    for r in ROUTES:
        code, page = PAGES[shape][r]
        rl = rail(page)
        names = [l["name"] for l in links(rl)] if rl else None
        if code != 200 or names is None or names[:8] != EIGHT:
            bad.append(f"{r}: {code} {'no Menu nav' if names is None else names[:8]}")
    check_true(f"AC1 {shape}: the eight, in D1 order, on every page", not bad, "; ".join(bad[:3]) + (" ..." if len(bad) > 3 else ""))

# ---- AC2 the current item is the page's section under the page map -----------------------------------
for shape, *_ in SHAPES:
    bad = []
    for r, section in PAGE_MAP.items():
        rl = rail(PAGES[shape][r][1])
        cur = [l["name"] for l in links(rl) if l["current"]] if rl else None
        if cur != [section]:
            bad.append(f"{r}: {cur} not [{section}]")
    check_true(f"AC2 {shape}: one item marked, the page's section", not bad, "; ".join(bad[:3]) + (" ..." if len(bad) > 3 else ""))

# ---- AC4 the identity block, above the rail, on every page -------------------------------------------
for shape, _, _, name, kind in SHAPES:
    bad = []
    for r in ROUTES:
        page = PAGES[shape][r][1]
        m = re.search(r"<[a-zA-Z0-9]+\b[^>]*\bdata-identity\b[^>]*>", page)
        rl = rail(page)
        if not m:
            bad.append(f"{r}: no data-identity"); continue
        blk = text(element(page, m.start()))
        first = page.find("<a", page.find(rl)) if rl else -1
        if name not in blk or not re.search(r"\b%s\b" % kind, blk):
            bad.append(f"{r}: says {blk[:60]!r}")
        elif first < 0 or m.start() > first:
            bad.append(f"{r}: not above the rail's items")
        elif shape == "laptop" and re.search(r"\bbox\b", blk.replace(name, ""), re.I):   # a host may be called anything
            bad.append(f"{r}: a laptop called a box")
    check_true(f"AC4 {shape}: names {name!r} and {kind}, above the rail", not bad, "; ".join(bad[:3]) + (" ..." if len(bad) > 3 else ""))

# ---- AC5 each section's tabs, by D1 name, each its own address, the section still marked -------------
port = PAGES["box"]["port"]
fb.STATUS.clear(); fb.STATUS.update(BASE_STATUS); fb.STATUS.update(SHAPES[1][2])
home_rail = rail(PAGES["box"]["/"][1])
where = {l["name"]: l["href"] for l in links(home_rail)} if home_rail else {}
for section, tabs in TABS.items():
    href = where.get(section)
    if not href:
        check_true(f"AC5 {section}: its tabs", False, "no rail item to open the section from")
        continue
    code, page = get(port, href.split("#")[0])
    found = {l["name"]: l["href"] for l in links(main_of(page))}
    missing = [t for t in tabs if t not in found]
    check_true(f"AC5 {section}: tabs {', '.join(tabs)}", code == 200 and not missing, f"{code}; missing {missing}")
    bad = []
    for t in tabs:
        h = (found.get(t) or "").split("#")[0]
        if not h.startswith("/"):
            bad.append(f"{t}: no address of its own"); continue
        c2, p2 = get(port, h)
        if c2 != 200:
            bad.append(f"{t}: {h} answered {c2}"); continue
        if h.startswith("/map/full"):
            continue                                   # the wall screen is bare by design
        rl = rail(p2)
        cur = [l["name"] for l in links(rl) if l["current"]] if rl else None
        if cur != [section]:
            bad.append(f"{t}: {h} marks {cur}")
    check_true(f"AC5 {section}: every tab answers and keeps the section marked", not missing and not bad, "; ".join(bad[:3]))
fb.STATUS.clear(); fb.STATUS.update(BASE_STATUS)

# ---- AC6 Help at the foot of the rail, on every page -------------------------------------------------
for shape, *_ in SHAPES:
    bad = []
    for r in ROUTES:
        rl = rail(PAGES[shape][r][1])
        ls = links(rl) if rl else []
        names = [l["name"] for l in ls]
        if "Help" not in names[8:] or next(l["href"] for l in ls if l["name"] == "Help").split("?")[0] != "/help":
            bad.append(f"{r}: {names[8:] if rl else 'no rail'}")
    check_true(f"AC6 {shape}: Help at the foot of the rail, to /help", not bad, "; ".join(bad[:3]) + (" ..." if len(bad) > 3 else ""))
rl = rail(PAGES["box"]["/help"][1])
check_true("AC6 and on Help itself no section is marked current",
           rl is not None and not [l for l in links(rl) if l["current"] and l["name"] in EIGHT],
           "no rail" if rl is None else str([l["name"] for l in links(rl) if l["current"]]))

finish()
