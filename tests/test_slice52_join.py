#!/usr/bin/env python3
"""Spec 052: joining meshes. Two bridges in one process on the fake gateway: a hub with no radio and a site with
one; pairing by the code, the link, the picture crossing; the screen on the fake bridge; the installer."""
import http.client, json, os, re, subprocess, sys, tempfile, threading, time
sys.path.insert(0, os.path.dirname(__file__))
from _common import ROOT, check, check_true, finish, read, skip  # noqa: E402
sys.path.insert(0, os.path.join(ROOT, "src"))
import fakegw_lib  # noqa: E402
fakegw_lib.install()
import fakebridge_lib  # noqa: E402
from fakebridge_lib import start_fake_bridge  # noqa: E402
from mesh_manager import bridge as B, common as CM, web as W  # noqa: E402

d = tempfile.mkdtemp()
c0 = CM.read_config(os.path.join(d, "none"))
check("AC1 defaults", (c0.get("PEER_BIND"), c0.get("PEER_PORT"), c0.get("SITE_NAME"), c0.get("SITE_ADDRESS")), ("", 8094, "", ""))
open(os.path.join(d, "config"), "w").write("SERIAL=\nMODE=hub\n")
check("AC1 MODE=hub is read back", CM.read_config(os.path.join(d, "config")).get("MODE"), "hub")

def wait_for(pred, secs=6.0):
    t0 = time.time()
    while time.time() - t0 < secs:
        try:
            if pred(): return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.2)
    return False

hub_state = tempfile.mkdtemp()
hub = B.Bridge({"SERIAL": "", "MODE": "hub", "PEER_BIND": "127.0.0.1", "PEER_PORT": 0, "SITE_NAME": "Hub", "SITE_ADDRESS": "127.0.0.1"}, socket_path=os.path.join(hub_state, "b.sock"), state_dir=hub_state)
st = hub.op_status()
check("AC2 a hub has no radio and no TAK", (st.get("mode"), st.get("radio"), st.get("tak")), ("hub", None, "off"))
check_true("AC2 status carries the site id and name", re.fullmatch(r"[0-9a-f]{64}", str((st.get("site") or {}).get("id"))) is not None and (st.get("site") or {}).get("name") == "Hub", repr(st.get("site")))
check_true("AC2 the listener is bound and its port reported", isinstance(st.get("peer_port"), int) and st.get("peer_port") > 0, repr(st.get("peer_port")))
check_true("AC2 the identity files exist, the key private", os.path.exists(os.path.join(hub_state, "site.crt")) and (os.stat(os.path.join(hub_state, "site.key")).st_mode & 0o777) == 0o600)
hub2 = B.Bridge({"SERIAL": "", "MODE": "hub", "SITE_NAME": "Hub"}, socket_path=os.path.join(hub_state, "b2.sock"), state_dir=hub_state)
check("AC2 a second start reads the same id back", hub2.op_status().get("site", {}).get("id"), st["site"]["id"])
check("AC2 no listener without PEER_BIND", hub2.op_status().get("peer_port"), None)

inv = hub.op_peer_invite()
check_true("AC3 an invite: code, expiry, text", bool(inv.get("code")) and bool(inv.get("expires")) and str(inv.get("invite", "")).startswith("127.0.0.1:") and inv.get("invite", "").count("/") == 2, repr(inv))
site_state = tempfile.mkdtemp()
site = B.Bridge({"SERIAL": "", "MODE": "server", "SITE_NAME": "Edge"}, socket_path=os.path.join(site_state, "b.sock"), state_dir=site_state)
site_id = site.op_status().get("site", {}).get("id")
j = site.op_peer_join(invite=inv["invite"])
check_true("AC3 join answers the hub's identity", j.get("joined") is True and j.get("site") == st["site"]["id"] and j.get("name") == "Hub", repr(j))
ok = wait_for(lambda: any(p.get("id") == site_id and p.get("state") == "connected" for p in hub.op_peers().get("peers", [])) and any(p.get("id") == st["site"]["id"] and p.get("state") == "connected" for p in site.op_peers().get("peers", [])))
check_true("AC3 both sides list the other as connected", ok, repr((hub.op_peers(), site.op_peers())))
check("AC3 the hub pinned the site's id", [p.get("id") for p in hub.op_peers().get("peers", [])], [site_id])

own_ids = sorted(n["id"] for n in site.op_nodes().get("nodes", []))
ok = wait_for(lambda: sorted(n["id"] for n in hub.op_nodes().get("nodes", []) if n.get("remote")) == own_ids and own_ids)
remote = [n for n in hub.op_nodes().get("nodes", []) if n.get("remote")]
check_true("AC4 the site's nodes appear on the hub, marked remote with their origin", ok and all(n.get("origin") == site_id and n.get("origin_name") == "Edge" for n in remote), repr([(n.get("id"), n.get("origin_name")) for n in remote]))
check("AC4 the hub has no nodes of its own", [n["id"] for n in hub.op_nodes().get("nodes", []) if not n.get("remote")], [])

bad_state = tempfile.mkdtemp()
bad = B.Bridge({"SERIAL": "", "MODE": "server", "SITE_NAME": "Stranger"}, socket_path=os.path.join(bad_state, "b.sock"), state_dir=bad_state)
host, port, code, fp = re.match(r"([^:]+):(\d+)/([^/]+)/([0-9a-f]+)", inv["invite"]).groups()
r = bad.op_peer_join(invite=f"{host}:{port}/WRONGCODE/{fp}")
check_true("AC5 a wrong code is refused in words", "error" in r and "code" in str(r["error"]).lower(), repr(r))
r = bad.op_peer_join(invite=inv["invite"])
check_true("AC5 a code cannot be used twice", "error" in r, repr(r))
r = bad.op_peer_join(invite=f"{host}:{port}//{fp}")
check_true("AC5 an unknown site with no code is refused", "error" in r, repr(r))
check("AC5 nothing was pinned", [p.get("id") for p in hub.op_peers().get("peers", [])], [site_id])

item = {"class": "nodes", "origin": "aaaa", "path": ["aaaa", "bbbb"], "data": []}
check("AC6 accept_item drops a loop and keeps a stranger", (B.accept_item(item, "bbbb"), B.accept_item(item, "cccc")), (False, True))

r = hub.op_peer_forget(site=site_id)
check("AC9 forget drops the pin", (r.get("forgotten"), [p.get("id") for p in hub.op_peers().get("peers", [])]), (True, []))
ok = wait_for(lambda: all(p.get("state") != "connected" for p in site.op_peers().get("peers", [])), 12)
check_true("AC9 the site's link is refused after forget", ok, repr(site.op_peers()))

fakebridge_lib.STATUS.update({"mode": "hub", "tak": "off", "radio": None, "radio_present": False, "connected": False, "peers": 1, "peer_port": 8094, "site": {"id": "ab" * 32, "name": "Dev hub"}})
fakebridge_lib.NODES.append({"id": "!dd000004", "name": "Far tracker", "battery": 50, "snr": 4.0, "hops": 0, "heard": "2026-09-03T02:00:00Z", "hw": "TRACKER_T1000_E", "heard_here": True, "remote": True, "origin": "cd" * 32, "origin_name": "Edge laptop"})
fb = start_fake_bridge()
srv = W.make_server(bind="127.0.0.1", port=0, socket_path=fb.path, etc_dir=tempfile.mkdtemp(), config={"AUTH": "off"}, state_dir=tempfile.mkdtemp())
port_w = srv.server_address[1]; threading.Thread(target=srv.serve_forever, daemon=True).start(); time.sleep(0.3)
def get(p):
    c = http.client.HTTPConnection("127.0.0.1", port_w, timeout=10); c.request("GET", p); r = c.getresponse(); b = r.read().decode(); c.close(); return r.status, b
s_, home = get("/"); s2, conns = get("/connections"); s3, nodes_page = get("/nodes")
check_true("AC7 the strip reads Hub with its peers", s_ == 200 and "Hub · 1 peer" in home, home[home.find("state"):home.find("state") + 200] if "Hub" not in home else "")
check_true("AC7 Connections carries the peers section, invite, join and forget", s2 == 200 and all(x in conns for x in ("id='peers'", "data-action='peer_invite'", "data-action='peer_join'", "Forget")))
check_true("AC7 a remote node says where it came from", s3 == 200 and "via Edge laptop" in nodes_page)

inst = read("install/install.sh")
if inst is None:
    skip("AC8 the installer", "not in this tree")
else:
    S = os.path.join(ROOT, "install", "install.sh")
    def dry(*args):
        root = tempfile.mkdtemp()
        return subprocess.run(["bash", S, "/nonexistent/mesh-manager-0.11.0-amd64.tgz", "--dry-run", *args], capture_output=True, text=True, env={**os.environ, "MESH_MANAGER_ROOT": root})
    r = dry("--mode", "hub")
    check_true("AC8 --mode hub needs no radio and binds the peer port", r.returncode == 0 and "MODE=hub" in r.stdout and "PEER_BIND=0.0.0.0" in r.stdout and "PEER_PORT=8094" in r.stdout, (r.returncode, r.stdout[-400:], r.stderr[-300:]))
    r = dry("--mode", "hub", "--peer-bind", "127.0.0.1", "--site-name", "Dev hub", "--site-address", "dev.example")
    check_true("AC8 the peer bind, site name and address are written", r.returncode == 0 and "PEER_BIND=127.0.0.1" in r.stdout and "SITE_NAME=Dev hub" in r.stdout and "SITE_ADDRESS=dev.example" in r.stdout, (r.returncode, r.stdout[-400:]))
    r = dry("--mode", "server")
    check_true("AC8 a server without a radio is still refused", r.returncode != 0 and "--serial" in r.stderr, r.stderr[-200:])
    check_true("AC8 the installer names Python 3.12 and the MESH_MANAGER_PYTHON override (Ubuntu 22.04 boxes)", "python3.12" in inst and "MESH_MANAGER_PYTHON" in inst and 'PYV" == "$PYT"' in inst and "release/PYTHON" in inst and "import venv, ensurepip" in inst)
    cut = read("release/cut-release.sh") or ""
    if cut:
        check_true("AC8 the cut takes --py, stamps release/PYTHON and names a non-3.12 cut by its Python", "--py)" in cut and '> "$B/PYTHON"' in cut and 'suffix="-py${PYV/./}"' in cut)
    else:
        skip("AC8 the cut takes --py", "the release tooling is not in this tree; this check runs in the source repository")
    upd = read("src/mesh_manager/updates.py") or ""
    check_true("AC8 the updater takes the cut for this box's Python when the release carries one", "-py{sys.version_info[0]}{sys.version_info[1]}.tgz" in upd)
finish()
