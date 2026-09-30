#!/usr/bin/env python3
"""Spec 110: first run and Home.

Today a new laptop, box or hub opens straight onto the Mesh overview (web.py overview_body), which is a
map and a set of status cards. Nothing asks the computer's name or the person's, nothing says which
radio, region and channel it settled on, and nothing records that anybody has been through it. A laptop
with no radio is still described as showing the demo mesh (macapp.py, README.md, docs/GUIDE.md), which
Spec 062 made untrue, and the Nodes page counts low batteries against a fixed 20 rather than the
battery threshold set on Health.

This suite holds: a first run per shape, due only on a fresh install; Home in place of the overview,
with count tiles, six task cards per shape and Needs attention; and the four defects fixed.

Every block is guarded: a missing route or helper is one verdict, not a traceback hiding the rest.
"""
import http.client, json, os, re, subprocess, sys, tempfile, threading, time, types, urllib.parse
sys.path.insert(0, os.path.dirname(__file__))
from _common import ROOT, check, check_true, finish, read  # noqa: E402
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "tests"))
try:
    from mesh_manager import web as W
    from mesh_manager import connections as K
    from mesh_manager import channel as CH
    from mesh_manager import desktop as D
    import fakebridge_lib as FB
except Exception as ex:  # noqa: BLE001
    check_true("the screen and the fake bridge import", False, f"{type(ex).__name__}: {ex}")
    finish()

PW = "correct horse"
RADIO = "/dev/serial/by-id/usb-x-if00"
SEEED = "/dev/serial/by-id/usb-Seeed_T1000-E_9F3A-if00"


def ago(secs):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - secs))


# Four nodes: three heard here, one only in the radio's database. Heard 5, 60 and 2 minutes ago, batteries
# 77, 40 and 9. With the Health thresholds below (quiet after 30 min, low under 50 per cent) that is one
# quiet and two low; against the fixed 20 it would be one low.
MESH = [{"id": "!aa000001", "name": "Tracker9", "battery": 77, "snr": 9.0, "hops": 0, "heard": ago(300), "heard_here": True, "lat": 51.2, "lon": -1.5},
        {"id": "!bb000002", "name": "Tracker2", "battery": 40, "snr": 3.0, "hops": 1, "heard": ago(3600), "heard_here": True},
        {"id": "!dd000004", "name": "Tracker4", "battery": 9, "snr": 5.0, "hops": 0, "heard": ago(120), "heard_here": True},
        {"id": "!cc000003", "name": "OldTracker", "battery": None, "heard": None, "heard_here": False}]
SETTINGS = {"silent_min": 30, "battery_pct": 50, "unknown": True, "fence_m": 0, "to_tak": False}


class Proxy(FB.FakeBridge):
    """The shared fake bridge, with the answers a check needs set here and everything else forwarded."""
    def __init__(self, over=None):
        self.inner = FB.FakeBridge()
        self.over = dict(over or {})
        super().__init__()

    def _one(self, c):
        f = c.makefile("rb")
        if not CH.said_hello(f, self._token):
            c.sendall(b'{"error": "no"}\n'); c.close(); return
        try:
            req = json.loads(f.readline().decode())
        except ValueError:
            c.sendall(b'{"error": "bad json"}\n'); c.close(); return
        op = req.pop("op", None)
        if op == "events":
            c.sendall(b'{"kind": "hello"}\n'); self.clients.append(c); return
        self.calls.append((op, dict(req)))
        if op in self.over:
            v = self.over[op]
            rep = v(req) if callable(v) else v
        else:
            try:
                rep = W.BridgeClient(self.inner.path).ask(op, **req)
            except W.BridgeDown as e_:
                rep = {"error": str(e_)}
        c.sendall((json.dumps(rep) + "\n").encode()); c.close()

    def ops(self):
        return [o for o, _ in self.calls]


def status(**kw):
    base = dict(FB.STATUS, mode="desktop", tak="off", radio=RADIO, radio_present=True, connected=True, region="EU_868",
                modem_preset="SHORT_FAST", primary_channel="MILUX-TAK", nodes_heard=3, chutil=12.4, verdict="normal",
                alerts_open=2, peers=0, site={"id": "ab" * 32, "name": "old-hostname"})
    base.update(kw)
    return base


def mesh_bridge(**st):
    return Proxy({"status": status(**st), "links": dict(FB.LINKS, nodes=MESH, routes={}), "nodes": {"nodes": MESH, "count": len(MESH)},
                  "alert_settings": SETTINGS,
                  "site_name_set": lambda r: {"name": r.get("name"), "confirmed": True},
                  "gateway": {"serial": RADIO, "present": True, "watching": False,
                              "candidates": [{"path": RADIO, "kind": "radio"}, {"path": SEEED, "kind": "radio"}]},
                  "gateway_set": lambda r: {"serial": r.get("path"), "confirmed": True, "note": "the bridge is restarting onto that radio"}})


def serve(config, bridge, passwd=True, first=None):
    etc = tempfile.mkdtemp()
    if passwd:
        W.write_password(os.path.join(etc, "passwd"), PW)
    if first is not None:
        json.dump(first, open(os.path.join(etc, "first-run.json"), "w"))
    srv = W.make_server("127.0.0.1", 0, bridge.path, etc, dict({"UPDATE_MODE": "off"}, **config), state_dir=tempfile.mkdtemp())
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.2)
    return srv.server_address[1], etc


def req(port, method, path, body=None, cookie=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    h = {"Cookie": cookie} if cookie else {}
    if body is not None:
        h["Content-Type"] = "application/x-www-form-urlencoded"
    c.request(method, path, body=body, headers=h)
    r = c.getresponse(); data = r.read(); hd = {k.lower(): v for k, v in r.getheaders()}
    c.close()
    return r.status, hd, data.decode("utf-8", "replace")


def login(port, name="Sgt Patel"):
    W.reset_throttle()
    _, hd, _ = req(port, "POST", "/login", urllib.parse.urlencode({"name": name, "password": PW}))
    return hd.get("set-cookie", "").split(";")[0]


def flag(etc):
    try:
        return json.load(open(os.path.join(etc, "first-run.json")))
    except (OSError, ValueError):
        return {}


def tile(page, key):
    """The count on a Home tile: the element carrying data-tile='<key>' and its data-count."""
    m = re.search(r"<[^>]*data-tile=['\"]" + key + r"['\"][^>]*>", page)
    if not m:
        return None
    c = re.search(r"data-count=['\"](-?\d+)['\"]", m.group(0))
    return int(c.group(1)) if c else None


def guarded(label, fn):
    try:
        fn()
    except Exception as ex:  # noqa: BLE001
        check_true(f"{label}: ran without an exception", False, f"{type(ex).__name__}: {ex}")


PENDING = {"state": "pending"}
DONE = {"state": "done", "name": "Matt", "done": "2026-09-30T12:00:00Z", "radio": RADIO, "region": "EU_868", "channel": "MILUX-TAK"}
LAPTOP = {"MODE": "desktop", "AUTH": "off"}


# ---- AC1 a first run is due on a fresh install only, and Home follows it ------------------------------------
def ac1():
    port, _ = serve(LAPTOP, mesh_bridge(), passwd=False, first=PENDING)
    st, hd, _ = req(port, "GET", "/")
    check("AC1 a fresh laptop opens on the first run", (st, hd.get("location")), (302, "/first-run"))
    port2, _ = serve(LAPTOP, mesh_bridge(), passwd=False, first=None)
    st, hd, _ = req(port2, "GET", "/")
    check_true("AC1 a computer with no first-run mark (an upgrade) is never sent through it",
               st == 200 and hd.get("location") is None, f"{st} {hd.get('location')}")
    port3, _ = serve(LAPTOP, mesh_bridge(), passwd=False, first=DONE)
    st, hd, _ = req(port3, "GET", "/first-run")
    check("AC1 once done, the first run goes to Home", (st, hd.get("location")), (302, "/"))
    bport, _ = serve({"MODE": "server"}, mesh_bridge(mode="server"), first=PENDING)
    st, hd, _ = req(bport, "GET", "/")
    check("AC1 on a box, sign-in still comes first", (st, hd.get("location")), (302, "/login"))
    st, hd, _ = req(bport, "GET", "/", cookie=login(bport))
    check("AC1 and then the first run", (st, hd.get("location")), (302, "/first-run"))
    # the marks: written where a fresh config is written, never on an upgrade
    root = tempfile.mkdtemp()
    dirs = {"root": root, "config": os.path.join(root, "config"), "etc": os.path.join(root, "etc"),
            "state": os.path.join(root, "state"), "socket": os.path.join(root, "bridge.sock")}
    D.first_config(dirs)
    check("AC1 a laptop's first config marks the first run as due", flag(dirs["etc"]).get("state"), "pending")
    os.remove(os.path.join(dirs["etc"], "first-run.json")) if os.path.exists(os.path.join(dirs["etc"], "first-run.json")) else None
    D.first_config(dirs)
    check_true("AC1 and a laptop that already has a config is not marked again", not os.path.exists(os.path.join(dirs["etc"], "first-run.json")), "marked on an upgrade")
    fresh = subprocess.run(["bash", os.path.join(ROOT, "install", "install.sh"), "/nonexistent/mesh-manager-1.5.0-amd64.tgz", "--dry-run"],
                           capture_output=True, text=True, timeout=120, env={**os.environ, "MESH_MANAGER_ROOT": _fake_root()})
    check_true("AC1 the installer marks the first run as due when it writes the first config",
               "first run is due" in (fresh.stdout + fresh.stderr), (fresh.stdout + fresh.stderr)[-200:])


def _fake_root():
    root = tempfile.mkdtemp()
    for d in ("etc/systemd/system", "opt", "var/lib", "opt/tak"):
        os.makedirs(os.path.join(root, d), exist_ok=True)
    open(os.path.join(root, "opt/tak/CoreConfig.xml"), "w").write(
        "<Configuration><network>\n        <input _name=\"meshtastic\" protocol=\"mcast\" port=\"6970\" group=\"239.2.3.1\">"
        "<filtergroup>mesh</filtergroup></input>\n</network></Configuration>\n")
    open(os.path.join(root, "etc/vantage-mesh.conf"), "w").write(
        "SERIAL=/dev/serial/by-id/usb-Espressif_USB_JTAG_serial_debug_unit_A4:CB:8F:EE:00:01-if00\n"
        "REGION=EU_868\nCHANNEL=MILUX-TAK\nFILTER_GROUP=mesh\nEXTRA_ARGS=\n")
    open(os.path.join(root, "etc/systemd/system/tak-meshtastic-gateway.service"), "w").write("[Unit]\nDescription=old\n")
    return root


guarded("AC1", ac1)


# ---- AC2 the laptop's first run --------------------------------------------------------------------------
def ac2():
    br = mesh_bridge()
    port, etc = serve(LAPTOP, br, passwd=False, first=PENDING)
    st, _, pg = req(port, "GET", "/first-run")
    check("AC2 the first run answers", st, 200)
    check_true("AC2 a laptop is never asked for a password", re.search(r"type=['\"]password['\"]", pg) is None, "a password field")
    for n in ("site_name", "name"):
        check_true(f"AC2 it asks for {n}", re.search(rf"<input[^>]*name=['\"]{n}['\"]", pg) is not None, f"no {n} field")
    check_true("AC2 it shows the radio it found", "usb-x-if00" in pg, "no radio shown")
    check_true("AC2 and the channel the radio is on", "MILUX-TAK" in pg, "no channel shown")
    check_true("AC2 and offers to join a hub, as a choice", re.search(r"[Jj]oin a hub", pg) is not None, "no join a hub")
    st, _, pg = req(port, "POST", "/first-run", urllib.parse.urlencode({"site_name": "Kit laptop", "name": "", "channel": "0"}))
    check_true("AC2 the person's name is required", st == 400 and flag(etc).get("state") == "pending", f"{st} {flag(etc)}")
    st, _, pg = req(port, "POST", "/first-run", urllib.parse.urlencode({"site_name": "Kit laptop", "name": "Matt", "channel": "0"}))
    check("AC2 the first run finishes", st, 200)
    check_true("AC2 and says what it decided: the radio, the region and the channel",
               all(w in pg for w in ("usb-x-if00", "EU_868", "MILUX-TAK")), "one of radio, region, channel missing")
    f = flag(etc)
    check("AC2 the mark says done", f.get("state"), "done")
    check("AC2 and keeps the person's name for attribution", f.get("name"), "Matt")
    check("AC2 and what it decided", (f.get("radio"), f.get("region"), f.get("channel")), (RADIO, "EU_868", "MILUX-TAK"))
    check_true("AC2 the computer's name goes to the bridge, which keeps it in the config",
               ("site_name_set", {"name": "Kit laptop"}) in br.calls, str([c for c in br.calls if c[0] != "status"][-4:]))
    st, hd, _ = req(port, "GET", "/")
    check_true("AC2 then Home", st == 200 and hd.get("location") is None, f"{st} {hd.get('location')}")
    none = mesh_bridge(radio=None, radio_present=False, connected=False, region=None, primary_channel=None)
    port2, _ = serve(LAPTOP, none, passwd=False, first=PENDING)
    _, _, pg2 = req(port2, "GET", "/first-run")
    check_true("AC2 with no radio it says it is watching for one, and never offers the demo",
               "watching for a radio" in pg2 and "demo" not in pg2.lower(), "no 'watching for a radio', or the demo offered")
    # the bridge end: SITE_NAME written by the bridge, one key, and the name the links use changes at once
    try:
        import fakegw_lib
        fakegw_lib.install()
        from mesh_manager.bridge import Bridge
    except Exception as ex:  # noqa: BLE001
        check_true("AC2 the bridge imports", False, f"{type(ex).__name__}: {ex}")
        return
    if not hasattr(Bridge, "op_site_name_set"):
        check_true("AC2 the bridge has op_site_name_set", False, "no op_site_name_set on the bridge yet")
        return

    class B(Bridge):
        def __init__(self, d):
            self.state_dir = d; self.box_mode = "desktop"; self.events = []
            self.conf = {"MODE": "desktop", "CONFIG_PATH": os.path.join(d, "config")}
            open(self.conf["CONFIG_PATH"], "w").write("MODE=desktop\nBIND=127.0.0.1\nPORT=8093\nAUTH=off\nREGION=EU_868\n")
            self.peering = types.SimpleNamespace(name="old-hostname", id="ab" * 32, port=None, connected=lambda: [])
            self._emit = lambda kind=None, **k: self.events.append(dict(k, kind=kind))
            self.logger = types.SimpleNamespace(warning=print, info=lambda *a, **k: None, debug=lambda *a, **k: None, error=print)

    b = B(tempfile.mkdtemp())
    r = b.op_site_name_set(name="Kit laptop")
    conf = open(b.conf["CONFIG_PATH"]).read()
    check_true("AC2 the bridge writes SITE_NAME", bool(r.get("confirmed")) and "SITE_NAME=Kit laptop" in conf, conf)
    check_true("AC2 and leaves every other line alone", all(x in conf for x in ("MODE=desktop", "BIND=127.0.0.1", "PORT=8093", "AUTH=off", "REGION=EU_868")), conf)
    check("AC2 and the site takes the name now, not at the next restart", b.peering.name, "Kit laptop")
    for bad in ("", "x" * 61, "two\nlines"):
        check_true(f"AC2 a site name {bad[:12]!r} is refused", "error" in b.op_site_name_set(name=bad), "accepted")


guarded("AC2", ac2)


# ---- AC3 the box and the hub ------------------------------------------------------------------------------
def ac3():
    br = mesh_bridge(mode="server", site={"id": "cd" * 32, "name": "edge"})
    port, etc = serve({"MODE": "server"}, br, first=PENDING)
    ck = login(port)
    st, _, pg = req(port, "GET", "/first-run", cookie=ck)
    check("AC3 a box's first run answers after sign-in", st, 200)
    check_true("AC3 it offers the gateway radios the box can see", RADIO in pg and SEEED in pg, "candidates missing")
    st, _, pg = req(port, "POST", "/first-run", urllib.parse.urlencode({"site_name": "EDGE", "radio": SEEED, "channel": "0"}), cookie=ck)
    check("AC3 a box's first run finishes", st, 200)
    check_true("AC3 a different radio is chosen through the bridge, as This radio does",
               ("gateway_set", {"path": SEEED}) in br.calls, str([c for c in br.calls if c[0] not in ("status", "links", "nodes")][-4:]))
    check("AC3 the mark records the radio chosen", flag(etc).get("radio"), SEEED)
    check("AC3 and on a box the name is the signed-in person's", flag(etc).get("name"), "Sgt Patel")
    hb = mesh_bridge(mode="hub", radio=None, radio_present=False, connected=False, region=None, primary_channel=None,
                     chutil=None, verdict="unknown", site={"id": "ab" * 32, "name": "Dev hub"})
    hport, _ = serve({"MODE": "hub"}, hb, first=PENDING)
    hck = login(hport)
    st, _, pg = req(hport, "GET", "/first-run", cookie=hck)
    check("AC3 a hub's first run answers after sign-in", st, 200)
    check_true("AC3 it says what it knows about being reachable: listening, the address, the port",
               "dev.example" in pg and "8094" in pg and re.search(r"[Ll]istening", pg) is not None, "address, port or listening missing")
    check_true("AC3 it offers to invite the first site", re.search(r"[Ii]nvite", pg) is not None, "no invite")
    check_true("AC3 a hub is never offered a radio", "gateway" not in hb.ops() and "/dev/serial" not in pg, "a radio offered on a hub")
    check_true("AC3 and nothing is invited or probed until someone asks", "peer_invite" not in hb.ops(), str(hb.ops()))


guarded("AC3", ac3)


# ---- AC4 Home: the count tiles and Needs attention ----------------------------------------------------------
def ac4():
    port, etc = serve(LAPTOP, mesh_bridge(), passwd=False, first=DONE)
    K.propose(etc, "Claude on the laptop", "send_text", {"text": "report in", "to": "!bb000002", "channel": 0},
              "Tracker2 battery is low; ask it to report")
    st, _, pg = req(port, "GET", "/")
    check("AC4 Home answers at /", st, 200)
    check("AC4 the Heard tile is the bridge's nodes_heard", tile(pg, "heard"), 3)
    check("AC4 the Quiet tile uses the silent threshold from Health", tile(pg, "quiet"), 1)
    check("AC4 the Low battery tile uses the battery threshold from Health", tile(pg, "low"), 2)
    check("AC4 the Airtime tile is the channel utilisation, whole per cent", tile(pg, "airtime"), 12)
    check_true("AC4 and carries the verdict", re.search(r"data-tile=['\"]airtime['\"][^>]*data-verdict=['\"]normal['\"]|data-verdict=['\"]normal['\"][^>]*data-tile=['\"]airtime['\"]", pg) is not None, "no verdict")
    m = re.search(r"id=['\"]needs-attention['\"](.*)", pg, re.S)
    na = m.group(1) if m else ""
    check_true("AC4 Needs attention is on Home", bool(m), "no id='needs-attention'")
    check_true("AC4 it lists the open alerts", "Tracker1 silent for 40 min" in na and "Tracker9 battery 9%" in na, "open alerts missing")
    check_true("AC4 and the agent proposals waiting", "Tracker2 battery is low; ask it to report" in na, "proposal missing")
    check_true("AC4 the old overview is gone from Home", "What is open" not in pg, "overview_body still renders at /")


guarded("AC4", ac4)


# ---- AC5 six task cards per shape; a hub never adds a device -------------------------------------------------
TASKS = {"desktop": {"map": "See where everyone is", "add-device": "Add a device", "message": "Message the mesh",
                     "join": "Join a hub", "health": "Check the mesh is healthy", "this-radio": "Look after this radio"},
         "server": {"map": "See where everyone is", "add-device": "Add a device", "message": "Message the mesh",
                    "invite-or-join": "Invite or join a site", "health": "Check the mesh is healthy", "this-radio": "Look after this radio"},
         "hub": {"map": "See where everyone is", "invite": "Invite a site", "sites": "Check the sites",
                 "groups": "Groups", "sign-in": "Who can sign in", "agents": "Agents"}}


def ac5():
    for mode, tasks in TASKS.items():
        cfg = LAPTOP if mode == "desktop" else {"MODE": mode}
        br = mesh_bridge(mode=mode, **({"radio": None, "radio_present": False} if mode == "hub" else {}))
        port, _ = serve(cfg, br, passwd=mode != "desktop", first=DONE)
        ck = None if mode == "desktop" else login(port)
        st, _, pg = req(port, "GET", "/", cookie=ck)
        got = re.findall(r"data-task=['\"]([a-z-]+)['\"]", pg)
        check(f"AC5 {mode}: six task cards, the six for this shape", sorted(got), sorted(tasks))
        missing = [t for t in tasks.values() if t not in pg]
        check(f"AC5 {mode}: each card carries its title", missing, [])
        if mode == "hub":
            check_true("AC5 hub: never offers to add a device", "add-device" not in got, str(got))


guarded("AC5", ac5)


# ---- AC6 the defects: no demo without --demo; low battery by the Health threshold -----------------------------
def ac6():
    try:
        from mesh_manager import macapp as M
    except Exception as ex:  # noqa: BLE001
        check_true("AC6 macapp imports", False, f"{type(ex).__name__}: {ex}")
        M = None
    if M:
        first = M.menu_lines({}, "http://127.0.0.1:8093/", None)[0]
        check_true("AC6 the menu with no radio says it is watching for one, not the demo",
                   "demo" not in first.lower() and "watching" in first.lower(), first)
    src = read("src/mesh_manager/macapp.py") or ""
    call = re.search(r"_run_together\(([^)]*)\)", src)
    check_true("AC6 without the menu-bar library, no radio does not start the demo",
               call is not None and "radio is None" not in call.group(1), call.group(0) if call else "no _run_together call")
    readme, guide = read("README.md") or "", read("docs/GUIDE.md") or ""
    check_true("AC6 the README no longer says a laptop with no radio shows the demo",
               re.search(r"[Ww]ith no radio[^.]*demo", readme) is None, "README still says so")
    check_true("AC6 nor does the guide", re.search(r"demo mesh when there is none|or shows the demo", guide) is None, "GUIDE still says so")
    port, _ = serve(LAPTOP, mesh_bridge(), passwd=False, first=DONE)
    st, _, pg = req(port, "GET", "/nodes")
    m = re.search(r"data-nf=['\"]low['\"][^>]*>\D*(\d+)", pg)
    check("AC6 the Nodes page counts low batteries against the Health threshold", int(m.group(1)) if m else None, 2)
    row = re.search(r"<tr[^>]*data-id=['\"]!bb000002['\"][^>]*>", pg)
    lowflag = re.search(r"data-low=['\"](\d)['\"]", row.group(0)) if row else None
    check("AC6 and marks the row of a node under it as low", lowflag.group(1) if lowflag else None, "1")


guarded("AC6", ac6)

finish()
