#!/usr/bin/env python3
"""Spec 050: the server shape. The bridge in each mode on the fake gateway; the screen in each mode on the fake bridge; the installer's flag."""
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
check("AC1 the default mode", CM.read_config(os.path.join(d, "none")).get("MODE"), "tak-server")
open(os.path.join(d, "config"), "w").write("SERIAL=\nMODE=server\n")
check("AC1 MODE=server is read back", CM.read_config(os.path.join(d, "config")).get("MODE"), "server")

state = tempfile.mkdtemp()
br = B.Bridge({"SERIAL": "", "MODE": "server"}, socket_path=os.path.join(state, "b.sock"), state_dir=state, observe=False)
st = br.op_status()
check("AC2 status carries the mode and tak off", (st.get("mode"), st.get("tak")), ("server", "off"))
check("AC2 the socket is a null socket", type(getattr(br, "socket_client", None)).__name__, "NullSocket")
check("AC2 a TAK chat is not attempted", br._tak_chat("test"), False)
r = br.op_alert_test()
check_true("AC2 alert_test says TAK is off", r.get("sent") is False and "TAK is off" in str(r.get("note", "")), repr(r))
r = br.op_alert_set(to_tak="on")
check_true("AC2 to_tak cannot be turned on", "TAK" in str(r.get("error", "")), repr(r))
check("AC2 to_tak reads off", br.op_alert_settings().get("to_tak"), False)

state2 = tempfile.mkdtemp()
br2 = B.Bridge({"SERIAL": ""}, socket_path=os.path.join(state2, "b.sock"), state_dir=state2, observe=True)
st2 = br2.op_status()
check("AC3 the default mode reports tak-server and tak on", (st2.get("mode"), st2.get("tak")), ("tak-server", "on"))
check_true("AC3 observe still counts, it is not the null socket", type(getattr(br2, "socket_client", None)).__name__ != "NullSocket")

def screen(status_extra):
    fakebridge_lib.STATUS.update(status_extra)
    fb = start_fake_bridge()
    srv = W.make_server(bind="127.0.0.1", port=0, socket_path=fb.path, etc_dir=tempfile.mkdtemp(), config={"AUTH": "off"}, state_dir=tempfile.mkdtemp())
    port = srv.server_address[1]; threading.Thread(target=srv.serve_forever, daemon=True).start(); time.sleep(0.3)
    def get(p):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=10); c.request("GET", p); r = c.getresponse(); b = r.read().decode(); c.close(); return b
    return {p: get(p) for p in ("/", "/health", "/help", "/map")}

pages = screen({"mode": "server", "tak": "off"})
allp = "\n".join(pages.values())
check_true("AC4 the strip says Managing the mesh", "Managing the mesh" in pages["/"])
for bad in ("Bridging to TAK", "To TAK chat", "data-action='alert_test'", "sent to TAK", "forwarded to TAK as a marker", "Point it at TAK", "forwards to TAK"):
    check(f"AC4 nothing says: {bad}", bad in allp, False)
check_true("AC4 the home card is the last packet heard", "Last packet heard" in pages["/"])
check_true("AC4 help says the box runs without TAK", "runs without TAK" in pages["/help"])

pages = screen({"mode": "tak-server", "tak": "on"})
check_true("AC5 tak-server keeps its words", "Bridging to TAK" in pages["/"] and "To TAK chat" in pages["/health"] and "data-action='alert_test'" in pages["/health"] and "Point it at TAK" in pages["/help"])

inst = read("install/install.sh")
if inst is None:
    skip("AC6 the installer takes --mode", "the installer is not in this tree")
else:
    check_true("AC6 the installer takes --mode and writes MODE=", "--mode" in inst and "MODE=" in inst)
    check_true("AC6 the installer refuses a mode it does not know", re.search(r"mode must be|--mode .*tak-server.*server", inst) is not None)
    S = os.path.join(ROOT, "install", "install.sh")
    def dry(*args):
        root = tempfile.mkdtemp()  # a fake root with no /opt/tak: a box with no TAK Server
        return subprocess.run(["bash", S, "/nonexistent/mesh-manager-0.9.0-amd64.tgz", "--dry-run", "--serial", "/dev/serial/by-id/usb-x-if00", *args],
                              capture_output=True, text=True, env={**os.environ, "MESH_MANAGER_ROOT": root})
    r = dry("--mode", "server")
    check_true("AC6 a dry run with --mode server on a box without TAK Server succeeds and writes MODE=server", r.returncode == 0 and "MODE=server" in r.stdout and "no TAK input" in r.stdout, (r.returncode, r.stdout[-300:], r.stderr[-300:]))
    r = dry()
    check_true("AC6 the same box without --mode is refused, naming --mode server", r.returncode != 0 and "TAK Server is not installed" in r.stderr and "--mode server" in r.stderr, (r.returncode, r.stderr[-300:]))
    r = dry("--mode", "bogus")
    check_true("AC6 --mode bogus is refused in words", r.returncode != 0 and "must be tak-server, server or hub" in r.stderr, r.stderr[-200:])  # wording grew with Spec 052's hub

demo_sock = os.path.join(tempfile.mkdtemp(), "d.sock")
env = dict(os.environ, MODE="server", PYTHONPATH=os.path.join(ROOT, "src"))
p = subprocess.Popen([sys.executable, "-m", "mesh_manager.demo", demo_sock], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    time.sleep(1.2)
    import socket as _s
    s = _s.socket(_s.AF_UNIX, _s.SOCK_STREAM); s.settimeout(5); s.connect(demo_sock); s.sendall(b'{"op": "status"}\n'); buf = b""
    while not buf.endswith(b"\n"):
        chunk = s.recv(65536)
        if not chunk: break
        buf += chunk
    s.close()
    dst = json.loads(buf.decode() or "{}")
    check("AC7 the demo in MODE=server reports tak off", (dst.get("mode"), dst.get("tak")), ("server", "off"))
finally:
    p.terminate()
finish()
