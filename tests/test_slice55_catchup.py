#!/usr/bin/env python3
"""Spec 055: catch-up after a reconnection. Three bridges on the fake gateway; the history store; the screen."""
import http.client, json, os, queue, sys, tempfile, threading, time
sys.path.insert(0, os.path.dirname(__file__))
from _common import ROOT, check, check_true, finish, read, skip  # noqa: E402
sys.path.insert(0, os.path.join(ROOT, "src"))
import fakegw_lib  # noqa: E402
fakegw_lib.install()
import fakebridge_lib  # noqa: E402
from fakebridge_lib import start_fake_bridge  # noqa: E402
from mesh_manager import bridge as B, history as H, web as W  # noqa: E402

def wait_for(pred, secs=6.0):
    t0 = time.time()
    while time.time() - t0 < secs:
        try:
            if pred(): return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.2)
    return False

# AC1: the store
hd = tempfile.mkdtemp(); h = H.History(hd, days=30)
T1, T2 = H.utc(time.time() - 120), H.utc(time.time() - 60)
h.message("!aa000001", "hello", name="Tracker9", dest="^all", channel=0, ts=T1)
h.message("!ee000099", "far hello", name="Far", dest="^all", channel=0, ts=T2, origin="cd" * 32, origin_name="Edge", channel_name="MILUX-TAK")
rows = h.query("messages", origin="cd" * 32)
check("AC1 the store keeps and filters by origin", [(r.get("text"), r.get("origin_name"), r.get("channel_name")) for r in rows], [("far hello", "Edge", "MILUX-TAK")])
check("AC1 the newest time held per origin", (h.newest_message("cd" * 32), h.newest_message("ff" * 32)), (T2, None))
check("AC1 the same message offered twice is held once", (h.has_message("cd" * 32, T2, "!ee000099", "far hello"), h.has_message("cd" * 32, T2, "!ee000099", "other")), (True, False))
h2 = H.History(hd, days=30)   # the store opens again with the columns in place
check("AC1 a store opened again still answers origin rows", len(h2.query("messages", origin="cd" * 32)), 1)

hub_state = tempfile.mkdtemp(); site_state = tempfile.mkdtemp(); site2_state = tempfile.mkdtemp()
hub = B.Bridge({"SERIAL": "", "MODE": "hub", "PEER_BIND": "127.0.0.1", "PEER_PORT": 0, "SITE_NAME": "Hub", "SITE_ADDRESS": "127.0.0.1"}, socket_path=os.path.join(hub_state, "b.sock"), state_dir=hub_state)
site = B.Bridge({"SERIAL": "", "MODE": "server", "SITE_NAME": "Edge"}, socket_path=os.path.join(site_state, "b.sock"), state_dir=site_state)
hub_id, site_id = hub.op_status()["site"]["id"], site.op_status()["site"]["id"]
inv = hub.op_peer_invite(); j = site.op_peer_join(invite=inv["invite"])
check_true("setup: paired", j.get("joined") is True and wait_for(lambda: any(p["state"] == "connected" for p in hub.op_peers()["peers"])), repr(j))
site.op_peer_sharing_set(site=hub_id, cls="messages", out="on", channels="0")
hub_events = queue.Queue(maxsize=2000); hub._subs.append(hub_events)
def drain(q, kind=None):
    out = []
    while True:
        try:
            ev = json.loads(q.get_nowait())
        except queue.Empty:
            return out
        if kind is None or ev.get("kind") == kind:
            out.append(ev)
def hear_text(br, text, fr="!aa000001"):
    br._on_receive({"fromId": fr, "toId": "^all", "rxSnr": 7.0, "hopStart": 3, "hopLimit": 3, "channel": 0, "decoded": {"portnum": "TEXT_MESSAGE_APP", "payload": text.encode(), "text": text}}, None)

hear_text(site, "first word")
ok = wait_for(lambda: any(r.get("origin") == site_id and r.get("text") == "first word" for r in hub.op_history(kind="messages", limit=50).get("rows", [])))
check_true("AC2 a remote message is in the hub's history with its origin", ok, repr([(r.get("text"), r.get("origin")) for r in hub.op_history(kind="messages", limit=5).get("rows", [])]))
n_before = len([r for r in hub.op_history(kind="messages", limit=500).get("rows", []) if r.get("origin") == site_id])
link = next(iter(site.peering.connected().values()))
hub.peer_item(next(iter(hub.peering.connected().values())), {"class": "messages", "origin": site_id, "origin_name": "Edge", "path": [site_id], "ts": "x",
                                                            "data": {"from": "!aa000001", "name": "Tracker9", "to": "^all", "channel": 0, "channel_name": "MILUX-TAK", "text": "first word",
                                                                     "ts": [r for r in hub.op_history(kind="messages", limit=50).get("rows", []) if r.get("text") == "first word"][0]["ts"]}})
time.sleep(0.5)
check("AC2 the same message offered again is held once", len([r for r in hub.op_history(kind="messages", limit=500).get("rows", []) if r.get("origin") == site_id]), n_before)

# AC3: a gap. The hub is stopped, so nothing can arrive live; it comes back on the same port with the same identity.
hub_port = hub.peering.port
hub.stop(); time.sleep(0.5)
check_true("setup: the hub is down", wait_for(lambda: not site.peering.connected(), 5))
for w in ("gap one", "gap two", "gap three"):
    hear_text(site, w); time.sleep(0.15)
hub = B.Bridge({"SERIAL": "", "MODE": "hub", "PEER_BIND": "127.0.0.1", "PEER_PORT": hub_port, "SITE_NAME": "Hub", "SITE_ADDRESS": "127.0.0.1"}, socket_path=os.path.join(hub_state, "b1.sock"), state_dir=hub_state)
hub_events = queue.Queue(maxsize=2000); hub._subs.append(hub_events)
def gap_rows():
    return [r for r in hub.op_history(kind="messages", limit=500).get("rows", []) if r.get("origin") == site_id and str(r.get("text", "")).startswith("gap ")]
ok = wait_for(lambda: len(gap_rows()) >= 3, 10)
rows = sorted(gap_rows(), key=lambda r: r.get("id") or 0)
check("AC3 the gap is filled once, in order", [r.get("text") for r in rows], ["gap one", "gap two", "gap three"])
time.sleep(1.5)
got = [e.get("text") for e in drain(hub_events, "text") if e.get("origin") == site_id and str(e.get("text", "")).startswith("gap ")]
check_true("AC3 and nothing arrives twice", len(got) == len(set(got)) and len(gap_rows()) == 3, repr((got, len(gap_rows()))))
check_true("AC3 the site is back on the hub", wait_for(lambda: site_id in hub.peering.connected(), 5))

# AC4: a third site catches up on what the hub holds from the first
site2 = B.Bridge({"SERIAL": "", "MODE": "server", "SITE_NAME": "Far"}, socket_path=os.path.join(site2_state, "b.sock"), state_dir=site2_state)
site2_id = site2.op_status()["site"]["id"]
inv2 = hub.op_peer_invite(); j2 = site2.op_peer_join(invite=inv2["invite"])
check_true("setup: a third site joined", j2.get("joined") is True and wait_for(lambda: site2_id in hub.peering.connected(), 10))
hub.op_peer_sharing_set(site=site2_id, cls="messages", out="on", channels="0")   # the hub lets messages out to the third site
site2.peering.drop(hub_id); time.sleep(0.3)   # and the third site's next connection asks for what it missed
ok = wait_for(lambda: any(r.get("origin") == site_id and r.get("text") == "gap two" for r in site2.op_history(kind="messages", limit=500).get("rows", [])), 10)
r2 = [r for r in site2.op_history(kind="messages", limit=500).get("rows", []) if r.get("origin") == site_id]
check_true("AC4 the third site received the first site's words from the hub, with the first site as origin", ok and all(r.get("origin_name") == "Edge" for r in r2), repr([(r.get("text"), r.get("origin_name")) for r in r2][:4]))

# AC5: a hub restarted holds the site's live waypoint and open alert again
from meshtastic.protobuf import mesh_pb2  # noqa: E402
wp = mesh_pb2.Waypoint(); wp.id = 4242; wp.latitude_i = int(51.5e7); wp.longitude_i = int(-0.12e7); wp.name = "RV Alpha"; wp.expire = int(time.time()) + 3600
site._on_receive({"fromId": "!aa000001", "toId": "^all", "rxSnr": 7.0, "hopStart": 3, "hopLimit": 3, "channel": 0, "decoded": {"portnum": "WAYPOINT_APP", "payload": wp.SerializeToString()}}, None)
a = site._alerts_load(); site._raise_alert(a, "!aa000001", "battery", "Tracker9 battery 9%"); site._alerts_save(a)
wait_for(lambda: any(w.get("wid") == 4242 for w in hub.op_waypoints()["waypoints"]))
hub.stop(); time.sleep(0.5)
hub_b = B.Bridge({"SERIAL": "", "MODE": "hub", "PEER_BIND": "127.0.0.1", "PEER_PORT": hub.peering.port, "SITE_NAME": "Hub", "SITE_ADDRESS": "127.0.0.1"}, socket_path=os.path.join(hub_state, "b2.sock"), state_dir=hub_state)
ok = wait_for(lambda: any(w.get("wid") == 4242 and w.get("origin") == site_id for w in hub_b.op_waypoints()["waypoints"]) and any(o.get("origin") == site_id and o.get("kind") == "battery" for o in hub_b.op_alerts()["open"]), 20)
check_true("AC5 a restarted hub holds the site's waypoint and open alert again", ok, repr((hub_b.op_waypoints()["waypoints"], hub_b.op_alerts()["open"])))
check_true("AC5 and the remote chat survived the restart", any(r.get("origin") == site_id for r in hub_b.op_history(kind="messages", limit=50).get("rows", [])))

# AC6: the screen
web_txt = read("src/mesh_manager/web.py") or ""
check_true("AC6 the chat's loader maps the origin fields from the history", "origin:r.origin" in web_txt and "origin_name:r.origin_name" in web_txt and "channel_name:r.channel_name" in web_txt)
fb = start_fake_bridge()
srv = W.make_server(bind="127.0.0.1", port=0, socket_path=fb.path, etc_dir=tempfile.mkdtemp(), config={"AUTH": "off"}, state_dir=tempfile.mkdtemp())
port_w = srv.server_address[1]; threading.Thread(target=srv.serve_forever, daemon=True).start(); time.sleep(0.3)
c = http.client.HTTPConnection("127.0.0.1", port_w, timeout=10); c.request("GET", "/api/history?kind=messages&limit=50"); r = c.getresponse(); body = json.loads(r.read().decode()); c.close()
check_true("AC6 /api/history rows carry the origin of a remote message", r.status == 200 and any(row.get("origin_name") == "Edge laptop" for row in body.get("rows", [])))
finish()
