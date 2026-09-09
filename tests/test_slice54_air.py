#!/usr/bin/env python3
"""Spec 054: the air. A hub and a radio site in one process on the fake gateway; the fake radio's send lists are the
proof; the screen on the fake bridge; the pure functions under node."""
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
hub_events = queue.Queue(maxsize=1000); hub._subs.append(hub_events)
site_events = queue.Queue(maxsize=1000); site._subs.append(site_events)
def drain(q, kind=None):
    out = []
    while True:
        try:
            ev = json.loads(q.get_nowait())
        except queue.Empty:
            return out
        if kind is None or ev.get("kind") == kind:
            out.append(ev)
radio = site.interface   # the fake radio: .texts is (text, dest, channelIndex); .waypoints is (name, description, lat, lon, expire, wid)

sh = next(p for p in site.op_peers()["peers"] if p["id"] == hub_id)["sharing"]
check("AC1 the defaults hold the air off", (sh["messages"].get("air"), sh["messages"].get("air_channel"), sh["waypoints"].get("air")), (False, None, False))
r = site.op_peer_sharing_set(site=hub_id, cls="messages", air="on", air_channel="0")
check_true("AC1 air and its channel are written", r.get("written", {}).get("air") is True and r.get("written", {}).get("air_channel") == 0, repr(r))
r = hub.op_peer_sharing_set(site=site_id, cls="messages", air="on")
check_true("AC1 a hub refuses the air in words", "error" in r and "radio" in str(r["error"]).lower(), repr(r))

drain(hub_events); drain(site_events); n0 = len(radio.texts)
r = hub.op_peer_send_text(site=site_id, channel=0, text="hello mesh, from the hub")
check_true("AC2 peer_send_text answers a mid and the site", r.get("mid") is not None and r.get("site") == site_id and r.get("sent") is True, repr(r))
mid = r.get("mid")
ok = wait_for(lambda: len(radio.texts) > n0)
sent = radio.texts[n0:] if ok else []
check_true("AC2 the text went out on the site's radio as a broadcast on the air channel, prefixed with the hub's name", ok and sent[0][1] == "^all" and sent[0][2] == 0 and sent[0][0].startswith("[Hub] ") and "hello mesh, from the hub" in sent[0][0], repr(sent))
got = []
ok2 = wait_for(lambda: (got.extend(drain(site_events, "text")) or True) and any(e.get("aired_from") for e in got))
check_true("AC2 the site's screen sees the aired message as its own, marked with where it came from", ok2 and any(e.get("aired_from") == "Hub" and e.get("sent") for e in got), repr([e.get("text") for e in got]))
acks = []
ok3 = wait_for(lambda: (acks.extend(drain(hub_events, "ack")) or True) and any(a.get("request_id") == mid for a in acks))
a = [a for a in acks if a.get("request_id") == mid]
check_true("AC2 the hub gets a receipt: on the air at Edge", ok3 and a and a[0].get("ok") is True and a[0].get("aired_at") == "Edge", repr(a))

site.op_peer_sharing_set(site=hub_id, cls="messages", air="off"); drain(hub_events); n1 = len(radio.texts)
r = hub.op_peer_send_text(site=site_id, channel=0, text="is anyone airing this")
acks = []
ok4 = wait_for(lambda: (acks.extend(drain(hub_events, "ack")) or True) and any(x.get("request_id") == r.get("mid") for x in acks))
a = [x for x in acks if x.get("request_id") == r.get("mid")]
check_true("AC3 with the air off nothing goes out and the receipt says so", len(radio.texts) == n1 and ok4 and a and a[0].get("ok") is False and "air" in str(a[0].get("reason", "")).lower(), repr((len(radio.texts) - n1, a)))

site.op_peer_sharing_set(site=hub_id, cls="messages", air="on", air_channel="0"); drain(hub_events); time.sleep(0.3)
r = hub.op_peer_send_text(site=site_id, channel=0, text="loop check"); time.sleep(2.5)
back = [e for e in drain(hub_events, "text") if e.get("origin") == site_id and not e.get("mine") and "loop check" in str(e.get("text", ""))]
check("AC4 the aired message is not shared back to the hub", back, [])
n2 = len(radio.texts)
site.peer_item(next(iter(site.peering.connected().values())), {"class": "messages", "origin": hub_id, "origin_name": "Hub", "path": [hub_id], "ts": "2026-01-01T00:00:00Z",
                                                                  "data": {"from": hub_id, "name": "Hub", "to": "^all", "channel": 0, "text": "already aired once", "mid": 99, "aired_from": "Elsewhere"}})
time.sleep(0.8)
check("AC4 a message that already went on an air is not aired again", len(radio.texts), n2)

site.op_peer_sharing_set(site=hub_id, cls="waypoints", air="on"); w0 = len(radio.waypoints)
site.peer_item(next(iter(site.peering.connected().values())), {"class": "waypoints", "origin": hub_id, "origin_name": "Hub", "path": [hub_id], "ts": "2026-01-01T00:00:00Z",
                                                                  "data": {"wid": 5151, "node": "!ee000099", "name": "Hub RV", "description": "meet", "lat": 51.5, "lon": -0.12, "expire": int(time.time()) + 3600, "ts": "2026-01-01T00:00:00Z", "gone": False}})
ok5 = wait_for(lambda: len(radio.waypoints) > w0)
wp = radio.waypoints[w0:] if ok5 else []
check_true("AC5 a remote waypoint with the air on reaches the radio, named for its origin", ok5 and wp and "Hub RV" in wp[0][0] and wp[0][5] == 5151, repr(wp))
site.op_peer_sharing_set(site=hub_id, cls="waypoints", air="off"); w1 = len(radio.waypoints)
site.peer_item(next(iter(site.peering.connected().values())), {"class": "waypoints", "origin": hub_id, "origin_name": "Hub", "path": [hub_id], "ts": "2026-01-01T00:00:00Z",
                                                                  "data": {"wid": 5152, "node": "!ee000099", "name": "Quiet RV", "description": "", "lat": 51.5, "lon": -0.12, "expire": int(time.time()) + 3600, "ts": "2026-01-01T00:00:00Z", "gone": False}})
time.sleep(0.8)
check("AC5 with the air off a remote waypoint stays off the radio", len(radio.waypoints), w1)

p = next(p for p in site.op_peers()["peers"] if p["id"] == hub_id)
check_true("AC6 the site counts what it aired for the peer (two messages and a waypoint)", isinstance(p.get("aired"), dict) and p["aired"].get("count") == 3 and p["aired"].get("last"), repr(p.get("aired")))

check_true("AC8 the catalogue names peer_send_text and the air inputs", any(x["id"] == "peer_send_text" for x in C.ACTIONS) and any(x["id"] == "peer_sharing_set" and {i["name"] for i in x["inputs"]} >= {"air", "air_channel"} for x in C.ACTIONS))
role = read("agents/mesh-manager-agent.md") or ""
check_true("AC8 the role names peer_send_text", "`peer_send_text`" in role)

fakebridge_lib.STATUS.update({"mode": "server", "tak": "off", "peers": 1, "peer_port": None, "site": {"id": "ab" * 32, "name": "Edge box"}})
fb = start_fake_bridge()
srv = W.make_server(bind="127.0.0.1", port=0, socket_path=fb.path, etc_dir=tempfile.mkdtemp(), config={"AUTH": "off"}, state_dir=tempfile.mkdtemp())
port_w = srv.server_address[1]; threading.Thread(target=srv.serve_forever, daemon=True).start(); time.sleep(0.3)
def get(pth):
    c = http.client.HTTPConnection("127.0.0.1", port_w, timeout=10); c.request("GET", pth); r = c.getresponse(); b = r.read().decode(); c.close(); return r.status, b
s1, conns = get("/connections"); s4, msgs_page = get("/messages")
check_true("AC7 the Sharing fold carries Air controls for messages and waypoints on a radio site", s1 == 200 and "name='air'" in conns and "name='air_channel'" in conns)
fakebridge_lib.STATUS.update({"mode": "hub"}); s1b, conns_hub = get("/connections"); fakebridge_lib.STATUS.update({"mode": "server"})
check_true("AC7 a hub shows no Air controls", s1b == 200 and "name='air'" not in conns_hub and "no radio" in conns_hub)
check_true("AC7 the remote chat's composer posts peer_send_text with a confirm naming the site", "data-action='peer_send_text'" in msgs_page and "data-confirm-remote=" in msgs_page)
m = re.search(r"/\* chat:pure:start \*/([\s\S]*?)/\* chat:pure:end \*/", msgs_page)
node = shutil.which("node")
if node and m:
    js = m.group(1) + "\nconsole.log(JSON.stringify({conf:[needsConfirm('ch:0@' + 'cd'.repeat(6)), needsConfirm('ch:0'), needsConfirm('dm:!aa000001')]}));\n"
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
        fh.write(js); path = fh.name
    try:
        out = subprocess.run([node, path], capture_output=True, text=True, timeout=30); got = json.loads(out.stdout.strip() or "{}") if out.returncode == 0 else {"error": out.stderr[:200]}
    finally:
        os.unlink(path)
    check("AC7 a remote chat confirms before sending", got.get("conf", got.get("error")), [True, True, False])
else:
    skip("AC7 needsConfirm under node", "node or the pure block is missing")
finish()
