#!/usr/bin/env python3
"""Spec 053: the sharing table; messages, waypoints and alerts across the link. Two bridges in one process on the
fake gateway; the screen on the fake bridge; the pure functions under node."""
import http.client, json, os, queue, re, shutil, subprocess, sys, tempfile, threading, time
sys.path.insert(0, os.path.dirname(__file__))
from _common import ROOT, check, check_true, finish, read, skip  # noqa: E402
sys.path.insert(0, os.path.join(ROOT, "src"))
import fakegw_lib  # noqa: E402
fakegw_lib.install()
import fakebridge_lib  # noqa: E402
from fakebridge_lib import start_fake_bridge  # noqa: E402
from mesh_manager import bridge as B, catalogue as C, web as W  # noqa: E402

def wait_for(pred, secs=6.0):
    t0 = time.time()
    while time.time() - t0 < secs:
        try:
            if pred(): return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.2)
    return False

hub_state = tempfile.mkdtemp(); site_state = tempfile.mkdtemp()
hub = B.Bridge({"SERIAL": "", "MODE": "hub", "PEER_BIND": "127.0.0.1", "PEER_PORT": 0, "SITE_NAME": "Hub", "SITE_ADDRESS": "127.0.0.1"}, socket_path=os.path.join(hub_state, "b.sock"), state_dir=hub_state)
site = B.Bridge({"SERIAL": "", "MODE": "server", "SITE_NAME": "Edge"}, socket_path=os.path.join(site_state, "b.sock"), state_dir=site_state)
hub_id, site_id = hub.op_status()["site"]["id"], site.op_status()["site"]["id"]
inv = hub.op_peer_invite(); j = site.op_peer_join(invite=inv["invite"])
check_true("setup: paired", j.get("joined") is True and wait_for(lambda: any(p["state"] == "connected" for p in hub.op_peers()["peers"])), repr(j))
events = queue.Queue(maxsize=1000); hub._subs.append(events)
def drain(kind=None):
    out = []
    while True:
        try:
            ev = json.loads(events.get_nowait())
        except queue.Empty:
            return out
        if kind is None or ev.get("kind") == kind:
            out.append(ev)

sh = next(p for p in site.op_peers()["peers"] if p["id"] == hub_id)["sharing"]
check("AC1 a new peer's defaults", {k: (v.get("out"), v.get("in")) for k, v in sh.items()}, {"nodes": (True, True), "messages": (False, True), "waypoints": (True, True), "alerts": (True, True)})
check("AC1 messages carry a channel list", sh["messages"].get("channels"), [])
r = site.op_peer_sharing_set(site=hub_id, cls="messages", out="on", **{"in": "on"}, channels="0")
check_true("AC1 one class written and answered", r.get("written", {}).get("out") is True and r.get("written", {}).get("channels") == [0], repr(r))
check_true("AC1 an unknown class is refused in words", "error" in site.op_peer_sharing_set(site=hub_id, cls="keys", out="on"))
check_true("AC1 an unknown site is refused in words", "error" in site.op_peer_sharing_set(site="ff" * 32, cls="nodes", out="on"))

def hear_text(br, text, to="^all", channel=0, fr="!aa000001"):
    br._on_receive({"fromId": fr, "toId": to, "rxSnr": 7.0, "hopStart": 3, "hopLimit": 3, "channel": channel, "decoded": {"portnum": "TEXT_MESSAGE_APP", "payload": text.encode(), "text": text}}, None)
drain(); hear_text(site, "at the RV, all well")
got = []
ok = wait_for(lambda: (got.extend(drain("text")) or True) and any(e.get("origin") == site_id and e.get("text") == "at the RV, all well" for e in got))
rem = [e for e in got if e.get("origin") == site_id]
check_true("AC2 a broadcast crosses as a text event with its origin, the site's name and the channel's name", ok and rem[0].get("origin_name") == "Edge" and rem[0].get("channel_name") == "MILUX-TAK" and rem[0].get("channel") == 0, repr(rem[:1]))
site.op_peer_sharing_set(site=hub_id, cls="messages", out="off"); drain(); hear_text(site, "quiet please"); time.sleep(1.5)
check("AC2 with out off nothing arrives", [e.get("text") for e in drain("text") if e.get("origin")], [])
site.op_peer_sharing_set(site=hub_id, cls="messages", out="on", channels="0"); hub.op_peer_sharing_set(site=site_id, cls="messages", **{"in": "off"}); drain(); hear_text(site, "hub not listening"); time.sleep(1.5)
check("AC2 with the hub's in off nothing arrives", [e.get("text") for e in drain("text") if e.get("origin")], [])
hub.op_peer_sharing_set(site=site_id, cls="messages", **{"in": "on"}); drain()
hear_text(site, "a private word", to="!00000001", fr="!bb000002"); time.sleep(1.5)
check("AC3 a direct message never crosses", [e.get("text") for e in drain("text") if e.get("origin")], [])

from meshtastic.protobuf import mesh_pb2  # noqa: E402
def hear_waypoint(br, wid, name, lat=51.5, lon=-0.12, expire=None):
    wp = mesh_pb2.Waypoint(); wp.id = wid; wp.latitude_i = int(lat * 1e7); wp.longitude_i = int(lon * 1e7); wp.name = name; wp.expire = expire or int(time.time()) + 3600
    br._on_receive({"fromId": "!aa000001", "toId": "^all", "rxSnr": 7.0, "hopStart": 3, "hopLimit": 3, "channel": 0, "decoded": {"portnum": "WAYPOINT_APP", "payload": wp.SerializeToString()}}, None)
hear_waypoint(site, 4242, "RV Alpha")
ok = wait_for(lambda: any(w.get("wid") == 4242 and w.get("origin") == site_id for w in hub.op_waypoints()["waypoints"]))
w = [w for w in hub.op_waypoints()["waypoints"] if w.get("wid") == 4242]
check_true("AC4 a waypoint crosses with its origin", ok and w and w[0].get("origin_name") == "Edge" and w[0].get("name") == "RV Alpha", repr(w))
hear_waypoint(site, 4242, "RV Alpha", lat=0.0, lon=0.0, expire=1)   # withdrawn
check_true("AC4 a withdrawn waypoint leaves", wait_for(lambda: not any(w.get("wid") == 4242 for w in hub.op_waypoints()["waypoints"])))

a = site._alerts_load(); site._raise_alert(a, "!aa000001", "battery", "Tracker9 battery 9%"); site._alerts_save(a)
ok = wait_for(lambda: any(o.get("origin") == site_id and o.get("kind") == "battery" for o in hub.op_alerts()["open"]))
o = [o for o in hub.op_alerts()["open"] if o.get("origin") == site_id]
check_true("AC5 an alert crosses with its origin", ok and o and o[0].get("origin_name") == "Edge" and "Tracker9" in o[0].get("text", ""), repr(o))
a = site._alerts_load(); site._clear_alert(a, "!aa000001", "battery"); site._alerts_save(a)
check_true("AC5 a cleared alert leaves", wait_for(lambda: not any(oo.get("origin") == site_id for oo in hub.op_alerts()["open"])))

check_true("AC8 the catalogue names peer_sharing_set", any(x["id"] == "peer_sharing_set" for x in C.ACTIONS))
role = read("agents/mesh-manager-agent.md") or ""
check_true("AC8 the role names it at its floor", "`peer_sharing_set`" in role)

fakebridge_lib.STATUS.update({"mode": "hub", "tak": "off", "peers": 1, "peer_port": 8094, "site": {"id": "ab" * 32, "name": "Dev hub"}})
fb = start_fake_bridge()
srv = W.make_server(bind="127.0.0.1", port=0, socket_path=fb.path, etc_dir=tempfile.mkdtemp(), config={"AUTH": "off"}, state_dir=tempfile.mkdtemp())
port_w = srv.server_address[1]; threading.Thread(target=srv.serve_forever, daemon=True).start(); time.sleep(0.3)
def get(p):
    c = http.client.HTTPConnection("127.0.0.1", port_w, timeout=10); c.request("GET", p); r = c.getresponse(); b = r.read().decode(); c.close(); return r.status, b
s1, conns = get("/connections"); s2, health = get("/health"); s3, wps = get("/api/waypoints"); s4, msgs_page = get("/messages")
check_true("AC7 a sharing form per peer with the four classes and Air held", s1 == 200 and "data-action='peer_sharing_set'" in conns and "name='site'" in conns and all(c in conns for c in ("nodes", "messages", "waypoints", "alerts")) and "Air: no radio here" in conns)  # a hub: Spec 054 puts the air controls on radio sites only
check_true("AC7 Health shows where a remote alert came from", s2 == 200 and "via Edge laptop" in health)
check_true("AC7 waypoints carry their origin", s3 == 200 and any(w.get("origin_name") == "Edge laptop" for w in json.loads(wps).get("waypoints", [])))

m = re.search(r"/\* chat:pure:start \*/([\s\S]*?)/\* chat:pure:end \*/", msgs_page)
node = shutil.which("node")
if not node:
    skip("AC6 the pure functions under node", "node is not installed here")
elif m:
    js = m.group(1) + r"""
var own='!00000001';
var msgs=[{from:'!aa000001',name:'Tracker9',to:'^all',channel:0,ts:'2026-01-01T10:00:00Z',text:'hi'},
          {from:'!ee000099',name:'Tracker1',to:'^all',channel:0,ts:'2026-01-01T10:01:00Z',text:'far away',origin:'cd'.repeat(32),origin_name:'Edge laptop',channel_name:'MILUX-TAK'}];
var channels=[{index:0,name:'MESH',role:'PRIMARY'}];
var chats=chatsFrom(msgs,own,channels,[],{});
var far=chats.filter(function(c){return c.key.indexOf('@')>0;})[0]||{};
console.log(JSON.stringify({keys:msgs.map(function(m){return chatKey(m,own);}), farName:far.name, farRemote:!!far.remote, isRemote:[isRemoteChat(far.key||''), isRemoteChat('ch:0')]}));
"""
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
        fh.write(js); path = fh.name
    try:
        out = subprocess.run([node, path], capture_output=True, text=True, timeout=30)
        got = json.loads(out.stdout.strip() or "{}") if out.returncode == 0 else {"error": out.stderr.strip()[:300]}
    except (OSError, ValueError, subprocess.SubprocessError) as ex:
        got = {"error": str(ex)[:300]}
    finally:
        os.unlink(path)
    if "error" in got:
        check("AC6 node ran the functions", got["error"], "ran")
    else:
        check("AC6 a remote message keys its own chat", got.get("keys"), ["ch:0", "ch:0@" + "cd" * 6])
        check("AC6 the remote chat is named for its channel and site, and marked", (got.get("farName"), got.get("farRemote")), ("MILUX-TAK via Edge laptop", True))
        check("AC6 isRemoteChat", got.get("isRemote"), [True, False])
else:
    check("AC6 the pure block is on the page", bool(m), True)
finish()
