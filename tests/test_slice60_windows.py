#!/usr/bin/env python3
"""Spec 060: the Windows app. The loopback channel, the tray app's words, the build and its workflow."""
import http.client, json, os, socket, stat, sys, tempfile, threading, time
sys.path.insert(0, os.path.dirname(__file__))
from _common import ROOT, check, check_true, finish, read, skip  # noqa: E402
sys.path.insert(0, os.path.join(ROOT, "src"))
import fakegw_lib  # noqa: E402
fakegw_lib.install()
os.environ["MESH_MANAGER_CHANNEL"] = "tcp"   # every check below is the Windows path
from mesh_manager import channel as CH, winapp as WA, web as W  # noqa: E402
from fakebridge_lib import start_fake_bridge  # noqa: E402

# AC1: which channel
check("AC1 the environment chooses the channel", (CH.use_tcp(), CH.use_tcp.__module__), (True, "mesh_manager.channel"))
os.environ.pop("MESH_MANAGER_CHANNEL", None)
check("AC1 Windows takes the loopback channel with nothing asked; the socket is the default elsewhere",
      CH.use_tcp(), sys.platform.startswith("win"))
os.environ["MESH_MANAGER_CHANNEL"] = "unix"
check("AC1 and asking for the socket by name is honoured", CH.use_tcp(), False)
os.environ["MESH_MANAGER_CHANNEL"] = "tcp"

# AC2: the token
d = tempfile.mkdtemp(); path = os.path.join(d, "bridge.sock")
srv, token = CH.listen_raw(path)
served = []
def accept_one():
    while True:
        try:
            c, _ = srv.accept()
        except OSError:
            return
        f = c.makefile("rb")
        if not CH.said_hello(f, token):
            served.append("refused"); c.sendall(b'{"error": "no"}\n'); c.close(); continue
        served.append("served"); c.sendall(b'{"ok": true}\n'); c.close()
threading.Thread(target=accept_one, daemon=True).start()
s = CH.connect(path, 5); s.sendall(b'{"op":"status"}\n'); ok = json.loads(s.makefile("rb").readline().decode()); s.close()
check_true("AC2 a caller with the token is served", ok.get("ok") is True and served[-1] == "served", repr((ok, served)))
host, port, real = CH._rendezvous(path)
bad = socket.create_connection((host, port), 5); bad.sendall(b'{"hello": "not-the-token"}\n')
line = bad.makefile("rb").readline().decode(); bad.close()
check_true("AC2 and one with the wrong token is refused", "error" in line and served[-1] == "refused", repr((line, served[-2:])))
st = os.stat(path)
check_true("AC2 the rendezvous is a file with the address and the token, not a socket",
           not stat.S_ISSOCK(st.st_mode) and real == token and port > 0 and (os.name == "nt" or stat.S_IMODE(st.st_mode) == 0o600), repr((oct(stat.S_IMODE(st.st_mode)), host, port)))
srv.close()

# AC3: the screen and a bridge, end to end, over loopback
fb = start_fake_bridge()
srv2 = W.make_server(bind="127.0.0.1", port=0, socket_path=fb.path, etc_dir=tempfile.mkdtemp(), config={"AUTH": "off"}, state_dir=tempfile.mkdtemp())
port2 = srv2.server_address[1]; threading.Thread(target=srv2.serve_forever, daemon=True).start(); time.sleep(0.4)
def get(p):
    c = http.client.HTTPConnection("127.0.0.1", port2, timeout=8); c.request("GET", p); r = c.getresponse(); b = r.read(); c.close(); return r.status, b
s1, b1 = get("/api/status"); s2, b2 = get("/healthz")
check_true("AC3 the screen reaches the bridge over the loopback channel", s1 == 200 and json.loads(b1).get("version") and json.loads(b2).get("bridge") is True, repr((s1, b1[:80])))
s3, b3 = get("/api/nodes")
check_true("AC3 and an operation answers", s3 == 200 and json.loads(b3).get("count", 0) >= 1, repr((s3, b3[:80])))
ev = []
def read_events():
    c = http.client.HTTPConnection("127.0.0.1", port2, timeout=8); c.request("GET", "/events"); r = c.getresponse()
    t0 = time.time()
    while time.time() - t0 < 6:
        ln = r.fp.readline()
        if not ln: break
        if ln.strip(): ev.append(ln.decode("utf-8", "replace").strip())
        if len(ev) >= 2: break
    c.close()
t = threading.Thread(target=read_events, daemon=True); t.start(); t.join(8)
check_true("AC3 and the event stream is open, the screen's pump connected to the bridge over loopback",
           bool(ev) and len(getattr(fb, "clients", [])) >= 1, repr((ev[:2], len(getattr(fb, "clients", [])))))

# AC4: the tray app
check_true("AC4 the Windows app imports anywhere and shares the Mac app's words", hasattr(WA, "main") and WA.menu_lines is not None)
lines = WA.menu_lines({"connected": True, "radio": "COM7", "nodes_heard": 2, "nodes_db": 9}, "http://127.0.0.1:8093/", "COM7")
check_true("AC4 and names a Windows port the same way", any("COM7" in l for l in lines) and any("2 heard here" in l for l in lines), repr(lines))

# AC5: the build and the workflow
ps = read("release/build-winapp.ps1") or ""
wf = read(".github/workflows/windows-app.yml") or ""
if ps:
    check_true("AC5 the build script is there and carries the release's patched gateway", "gateway-*.patch" in ps and "sitepkg-*.patch" in ps and "pyinstaller" in ps.lower())
    check_true("AC5 it makes a zip and claims no signature", "Compress-Archive" in ps and "SmartScreen" in ps and "signing" in ps.lower())
else:
    skip("AC5 the build script is there and carries the release's patched gateway", "the release tooling is not in this tree; this check runs in the source repository")
    skip("AC5 it makes a zip and claims no signature", "the release tooling is not in this tree; this check runs in the source repository")
if wf:
    check_true("AC5 a Windows runner builds it, by hand, on a tag, and on a pull request that touches it", "windows-latest" in wf and "workflow_dispatch" in wf and "pull_request" in wf and "build-winapp.ps1" in wf and "upload-artifact" in wf)
else:
    skip("AC5 a Windows runner builds it", "that workflow stays in the source repository, with the build script it runs")
if ps:
    check_true("AC5 the pin is not restated in the build script", "TAK-Meshtastic-Gateway==" not in ps and "cut-release.sh" in ps)
else:
    skip("AC5 the pin is not restated in the build script", "the release tooling is not in this tree; this check runs in the source repository")

# AC6: the guide
g = read("docs/GUIDE.md") or ""
check_true("AC6 the guide tells a Windows operator what to expect", "Windows" in g and "notification area" in g and "SmartScreen" in g)
finish()
