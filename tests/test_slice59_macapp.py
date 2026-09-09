#!/usr/bin/env python3
"""Spec 059: the macOS app. One process when frozen, the menu-bar words, the build script."""
import http.client, json, os, signal, subprocess, sys, tempfile, time
sys.path.insert(0, os.path.dirname(__file__))
from _common import ROOT, check, check_true, finish, read, skip  # noqa: E402
sys.path.insert(0, os.path.join(ROOT, "src"))
import fakegw_lib  # noqa: E402
fakegw_lib.install()
from mesh_manager import desktop as D, macapp as M  # noqa: E402

# AC1: the bridge and the screen as threads of one process
root = tempfile.mkdtemp()
dirs = {"root": root, "config": os.path.join(root, "config"), "etc": os.path.join(root, "etc"), "state": os.path.join(root, "state"), "socket": os.path.join(root, "bridge.sock")}
D.first_config(dirs)
h = D.serve_in_process(dirs, demo=True, port=0)
ok = False
for _ in range(60):
    try:
        c = http.client.HTTPConnection("127.0.0.1", h.port, timeout=3); c.request("GET", "/healthz"); r = c.getresponse(); d = json.loads(r.read().decode()); c.close()
        if d.get("bridge"):
            ok = True; break
    except Exception:  # noqa: BLE001
        pass
    time.sleep(0.5)
check_true("AC1 one process serves the screen with the bridge behind it", ok and h.port > 0, repr(h.port))
c = http.client.HTTPConnection("127.0.0.1", h.port, timeout=3); c.request("GET", "/api/status"); st = json.loads(c.getresponse().read().decode()); c.close()
check("AC1 and it is a desktop with TAK off", (st.get("mode"), st.get("tak")), ("desktop", "off"))
h.stop()
time.sleep(1.0)
down = False
try:
    c = http.client.HTTPConnection("127.0.0.1", h.port, timeout=2); c.request("GET", "/healthz"); c.getresponse(); c.close()
except Exception:  # noqa: BLE001
    down = True
check_true("AC1 stopping it stops both parts", down and not os.path.exists(dirs["socket"]))

# AC2: the same from the command line
root2 = tempfile.mkdtemp()
env = {**os.environ, "PYTHONPATH": os.path.join(ROOT, "src") + os.pathsep + os.path.join(ROOT, "tests")}
p = subprocess.Popen([sys.executable, "-m", "mesh_manager.desktop", "--in-process", "--demo", "--no-browser", "--port", "0", "--root", root2],
                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
port, t0, lines = None, time.time(), []
while time.time() - t0 < 40 and port is None:
    ln = p.stdout.readline()
    if not ln:
        if p.poll() is not None: break
        continue
    lines.append(ln.rstrip())
    if "http://127.0.0.1:" in ln:
        port = int(ln.split("http://127.0.0.1:")[1].split("/")[0].split()[0].strip(" ,;)"))
answered = False
if port:
    for _ in range(40):
        try:
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=3); c.request("GET", "/healthz"); answered = json.loads(c.getresponse().read().decode()).get("bridge") is True; c.close()
            if answered: break
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.5)
check_true("AC2 --in-process runs from the command line too", answered, repr(lines[-4:]))
p.send_signal(signal.SIGINT); t1 = time.time()
try:
    rc = p.wait(timeout=12)
except subprocess.TimeoutExpired:
    p.kill(); rc = "killed"
check_true("AC2 and stops on an interrupt", rc == 0 and time.time() - t1 < 12, repr((rc, round(time.time() - t1, 1))))

# AC3: the menu-bar words
radio_lines = M.menu_lines({"connected": True, "radio": "/dev/cu.usbmodem1101", "nodes_heard": 3, "nodes_db": 12, "mode": "desktop"}, "http://127.0.0.1:8093/", "/dev/cu.usbmodem1101")
none_lines = M.menu_lines({"connected": False, "radio": None, "nodes_heard": 0, "nodes_db": 0, "mode": "desktop"}, "http://127.0.0.1:8093/", None)
check_true("AC3 the menu says the radio and what it has heard", any("cu.usbmodem1101" in l for l in radio_lines) and any("3" in l and "heard" in l for l in radio_lines), repr(radio_lines))
check_true("AC3 and says plainly when there is no radio", any("no radio" in l.lower() for l in none_lines), repr(none_lines))
check_true("AC3 the module imports where there is no menu bar", hasattr(M, "main") and hasattr(M, "menu_lines"))

# AC6 (found by the gate): the app lets go of the radio however it is asked to stop, not only by its own Quit
class _Fake:
    def __init__(self): self.stopped = False
    def stop(self): self.stopped = True
_f = _Fake(); M._stop_on_terminate(_f)
import signal as _sig
check_true("AC6 a signal handler is in place to stop it", _sig.getsignal(_sig.SIGTERM) not in (_sig.SIG_DFL, _sig.SIG_IGN, None))
check_true("AC6 and the terminate hook is asked for by name", "NSApplicationWillTerminateNotification" in (read("src/mesh_manager/macapp.py") or ""))

# AC4: the build script
sh = read("release/build-macapp.sh") or ""
if sh:
    check_true("AC4 the build script is there and executable", sh and os.access(os.path.join(ROOT, "release/build-macapp.sh"), os.X_OK))
    check_true("AC4 it collects the gateway and meshtastic whole", "--collect-all meshtastic" in sh and "--collect-all tak_meshtastic_gateway" in sh, sh[:200])
    check_true("AC4 it makes a menu-bar app, signs it ad-hoc and wraps it in a DMG", "LSUIElement" in sh and "codesign" in sh and "hdiutil" in sh)
    check_true("AC4 it refuses to run anywhere but macOS", "Darwin" in sh and "uname" in sh)
    check_true("AC4 the release's patched gateway is what it bundles", "gateway-" in sh and "sitepkg-" in sh)
else:
    skip("AC4 the build script is there and executable", "the release tooling is not in this tree; this check runs in the source repository")
    skip("AC4 it collects the gateway and meshtastic whole", "the release tooling is not in this tree; this check runs in the source repository")
    skip("AC4 it makes a menu-bar app, signs it ad-hoc and wraps it in a DMG", "the release tooling is not in this tree; this check runs in the source repository")
    skip("AC4 it refuses to run anywhere but macOS", "the release tooling is not in this tree; this check runs in the source repository")
    skip("AC4 the release's patched gateway is what it bundles", "the release tooling is not in this tree; this check runs in the source repository")

# AC5: the guide
g = read("docs/GUIDE.md") or ""
check_true("AC5 the guide says how to open the app the first time", "Mesh Manager.app" in g and ("menu bar" in g or "menu-bar" in g))
finish()
