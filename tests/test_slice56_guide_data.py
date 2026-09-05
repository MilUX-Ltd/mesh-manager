#!/usr/bin/env python3
"""Spec 056: the joining-meshes chapter, the never-list in code, forgetting a peer, the export's origin."""
import http.client, json, os, re, sys, tempfile, threading, time
sys.path.insert(0, os.path.dirname(__file__))
from _common import ROOT, check, check_true, finish, read, skip  # noqa: E402
sys.path.insert(0, os.path.join(ROOT, "src"))
import fakegw_lib  # noqa: E402
fakegw_lib.install()
from fakebridge_lib import start_fake_bridge  # noqa: E402
from mesh_manager import bridge as B, peers as P, web as W  # noqa: E402

def wait_for(pred, secs=6.0):
    t0 = time.time()
    while time.time() - t0 < secs:
        try:
            if pred(): return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.2)
    return False

# AC1: the chapter
g = read("docs/GUIDE.md") or ""
i = g.find("\n## Joining meshes")
check_true("AC1 the guide has a Joining meshes chapter", i > 0)
chapter = g[i:g.find("\n## ", i + 5) if g.find("\n## ", i + 5) > 0 else len(g)]
parts = re.findall(r"^### (.+)$", chapter, re.M)
check("AC1 with its six parts", len(parts), 6, )
check_true("AC1 the parts are the ones named", all(any(w in p.lower() for p in parts) for w in ("invite", "shared", "air", "gap", "never", "cost")), repr(parts))
check_true("AC1 the chapter names both screenshots", "guide/peers.png" in chapter and "guide/messages-remote.png" in chapter)
check_true("AC1 and both files exist", os.path.exists(os.path.join(ROOT, "assets/guide/peers.png")) and os.path.exists(os.path.join(ROOT, "assets/guide/messages-remote.png")))

# AC2: the review
r = read("docs/security/data-handling-review-joining-meshes.md") or ""
heads = [h.lower() for h in re.findall(r"^## (.+)$", r, re.M)]
want = ("what crosses", "what never crosses", "at rest", "in flight", "retention", "exports and the agent", "forgetting a peer", "findings")
_cutpub = read("release/cut-public.sh") or ""
if _cutpub:
    check_true("AC2 the public cut carries the review", "docs/security/data-handling-review-joining-meshes.md" in _cutpub)
else:
    skip("AC2 the public cut carries the review", "the release tooling is not in this tree; this check runs in the source repository")
check_true("AC2 the review has its eight headings", all(any(w in h for h in heads) for w in want), repr(heads))
fi = r.lower().find("## findings")
findings = [f.strip() for f in re.split(r"^(?=\d+\. )", r[fi:], flags=re.M)[1:]] if fi > 0 else []
check_true("AC2 every finding carries a status", len(findings) >= 4 and all(re.search(r"\*\*(fixed here|accepted|next)\*\*", f) for f in findings), repr([f[:60] for f in findings]))

# AC3: the never-list
check("AC3 carries_never finds a listed key at depth", (P.carries_never({"class": "nodes", "data": [{"id": "!aa000001", "psk": "AQ=="}]}), P.carries_never({"data": {"settings": {"admin_key": "x"}}}), P.carries_never({"data": {"name": "psk", "text": "my url is here"}})), (True, True, False))
hub_state = tempfile.mkdtemp(); site_state = tempfile.mkdtemp()
hub = B.Bridge({"SERIAL": "", "MODE": "hub", "PEER_BIND": "127.0.0.1", "PEER_PORT": 0, "SITE_NAME": "Hub", "SITE_ADDRESS": "127.0.0.1"}, socket_path=os.path.join(hub_state, "b.sock"), state_dir=hub_state)
site = B.Bridge({"SERIAL": "", "MODE": "server", "SITE_NAME": "Edge"}, socket_path=os.path.join(site_state, "b.sock"), state_dir=site_state)
hub_id, site_id = hub.op_status()["site"]["id"], site.op_status()["site"]["id"]
inv = hub.op_peer_invite(); j = site.op_peer_join(invite=inv["invite"])
check_true("setup: paired", j.get("joined") is True and wait_for(lambda: site_id in hub.peering.connected()), repr(j))
link = site.peering.connected()[hub_id]
before = link.sent
ok = link.send({"item": {"class": "nodes", "origin": site_id, "origin_name": "Edge", "path": [site_id], "ts": "x", "data": [{"id": "!aa000001", "name": "Leak", "channel_url": "https://meshtastic.org/e/#x"}]}})
time.sleep(0.8)
held = [n.get("name") for v in hub.remote_nodes.values() for n in v.get("nodes", []) if n.get("name") == "Leak"]
check("AC3 a shared item carrying a listed key never leaves", (ok, link.sent - before, link.refused, held), (False, 0, 1, []))
hub_link = hub.peering.connected()[site_id]
hub.peer_item(hub_link, {"class": "waypoints", "origin": site_id, "origin_name": "Edge", "path": [site_id], "ts": "x", "data": {"wid": 7, "name": "RV", "lat": 51.5, "lon": -0.1, "token": "ghp_x"}})
check("AC3 one that arrives carrying a listed key is not accepted", [w.get("wid") for w in hub.op_waypoints()["waypoints"]], [])

# AC5: forgetting a peer
from meshtastic.protobuf import mesh_pb2  # noqa: E402
wp = mesh_pb2.Waypoint(); wp.id = 4243; wp.latitude_i = int(51.5e7); wp.longitude_i = int(-0.12e7); wp.name = "RV Bravo"; wp.expire = int(time.time()) + 3600
site._on_receive({"fromId": "!aa000001", "toId": "^all", "rxSnr": 7.0, "hopStart": 3, "hopLimit": 3, "channel": 0, "decoded": {"portnum": "WAYPOINT_APP", "payload": wp.SerializeToString()}}, None)
a = site._alerts_load(); site._raise_alert(a, "!aa000001", "battery", "Tracker9 battery 9%"); site._alerts_save(a)
site.op_peer_sharing_set(site=hub_id, cls="messages", out="on", channels="0")
site._on_receive({"fromId": "!aa000001", "toId": "^all", "rxSnr": 7.0, "hopStart": 3, "hopLimit": 3, "channel": 0, "decoded": {"portnum": "TEXT_MESSAGE_APP", "payload": b"kept words", "text": "kept words"}}, None)
ok = wait_for(lambda: any(w.get("wid") == 4243 for w in hub.op_waypoints()["waypoints"]) and any(o.get("origin") == site_id for o in hub.op_alerts()["open"]) and any(r.get("origin") == site_id for r in hub.op_history(kind="messages", limit=20).get("rows", [])))
check_true("setup: the hub holds the site's waypoint, alert and a message", ok)
hub.op_peer_forget(site=site_id); time.sleep(0.5)
check("AC5 forgetting a peer removes its waypoints and open alerts", ([w for w in hub.op_waypoints()["waypoints"] if w.get("origin") == site_id], [o for o in hub.op_alerts()["open"] if o.get("origin") == site_id], [n for n in hub.op_nodes()["nodes"] if n.get("remote")]), ([], [], []))
check_true("AC5 and keeps its messages in the history", any(r.get("origin") == site_id and r.get("text") == "kept words" for r in hub.op_history(kind="messages", limit=20).get("rows", [])))

# AC4: the export
fb = start_fake_bridge()
srv = W.make_server(bind="127.0.0.1", port=0, socket_path=fb.path, etc_dir=tempfile.mkdtemp(), config={"AUTH": "off"}, state_dir=tempfile.mkdtemp())
port_w = srv.server_address[1]; threading.Thread(target=srv.serve_forever, daemon=True).start(); time.sleep(0.3)
c = http.client.HTTPConnection("127.0.0.1", port_w, timeout=10); c.request("GET", "/export/messages.csv?hours=48000"); rr = c.getresponse(); body = rr.read().decode(); c.close()
lines = body.splitlines(); hdr = lines[0].split(",") if lines else []
remote = [l for l in lines[1:] if "far side here" in l]
check_true("AC4 the messages export carries origin columns and a remote row shows them", rr.status == 200 and "origin" in hdr and "origin_name" in hdr and remote and "Edge laptop" in remote[0], repr((rr.status, hdr, remote[:1])))
finish()
