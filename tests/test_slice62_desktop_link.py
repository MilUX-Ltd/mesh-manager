#!/usr/bin/env python3
"""Spec 062: a laptop is a site. No radio is not a demonstration, a radio that arrives is taken, and it joins."""
import http.client, json, os, sys, tempfile, threading, time
sys.path.insert(0, os.path.dirname(__file__))
from _common import ROOT, check, check_true, finish, read  # noqa: E402
sys.path.insert(0, os.path.join(ROOT, "src"))
import fakegw_lib  # noqa: E402
fakegw_lib.install()
from mesh_manager import bridge as B, desktop as D, macapp as M  # noqa: E402

def wait_for(pred, secs=8.0):
    t0 = time.time()
    while time.time() - t0 < secs:
        try:
            if pred(): return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.2)
    return False

# AC1: a laptop with no radio is a real site
st_dir = tempfile.mkdtemp()
b = B.Bridge({"SERIAL": "", "MODE": "desktop", "SITE_NAME": "Someone's laptop"}, socket_path=os.path.join(st_dir, "b.sock"), state_dir=st_dir)
s = b.op_status()
check("AC1 a desktop with no radio is the real bridge, with an identity", (s.get("mode"), s.get("radio_present"), bool((s.get("site") or {}).get("id")), (s.get("site") or {}).get("name")), ("desktop", False, True, "Someone's laptop"))
check_true("AC1 and it is not the demo", str(s.get("version")) != "0.1.0", repr(s.get("version")))

# AC3: it joins a hub and both ends see it
hub_dir = tempfile.mkdtemp()
hub = B.Bridge({"SERIAL": "", "MODE": "hub", "PEER_BIND": "127.0.0.1", "PEER_PORT": 0, "SITE_NAME": "A hub", "SITE_ADDRESS": "127.0.0.1"}, socket_path=os.path.join(hub_dir, "b.sock"), state_dir=hub_dir)
inv = hub.op_peer_invite()
j = b.op_peer_join(invite=inv["invite"])
check_true("AC3 the laptop joins a hub by invite", j.get("joined") is True, repr(j))
check_true("AC3 the hub has it as a peer", wait_for(lambda: any(p.get("state") == "connected" for p in hub.op_peers()["peers"])), repr(hub.op_peers()["peers"])[:200])
check_true("AC3 and the laptop has the hub", any(p.get("state") == "connected" for p in b.op_peers()["peers"]), repr(b.op_peers()["peers"])[:200])
b.stop(); hub.stop()

# AC2 and AC4: the demo is asked for, and a radio that arrives is taken
src = read("src/mesh_manager/desktop.py") or ""
check_true("AC2 a missing radio no longer means the demo", "demo = a.demo" in src and "or radio is None" not in src, "the demo must not be chosen by a missing radio")
check_true("AC4 the application watches for a radio and takes it", "def watch_for_radio" in src or "_watch_radio" in src)
check_true("AC4 the watcher restarts the bridge onto the radio it found", "swap" in src.lower() or "restart" in src.lower())

# AC5: the words
page = read("src/mesh_manager/web.py") or ""
check_true("AC5 a laptop with no radio is watching for one, not missing one", "watching for a radio" in page.lower())
lines = M.menu_lines({"connected": False, "radio": None, "nodes_heard": 0, "nodes_db": 0, "mode": "desktop", "peers": 2}, "http://127.0.0.1:8093/", None)
check_true("AC5 the menu names the sites joined", any("joined to 2" in l for l in lines), repr(lines))
lines0 = M.menu_lines({"connected": True, "radio": "/dev/cu.usbmodem1", "nodes_heard": 1, "peers": 0}, "http://127.0.0.1:8093/", "/dev/cu.usbmodem1")
check_true("AC5 and says nothing of sites when there are none", not any("joined" in l for l in lines0), repr(lines0))

# AC6: the guide
g = read("docs/GUIDE.md") or ""
check_true("AC6 the guide says a laptop can join a hub", "join a hub" in g.lower() and "laptop" in g.lower())
finish()
