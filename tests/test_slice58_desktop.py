#!/usr/bin/env python3
"""Spec 058: the desktop mode. Directories, the first config, the radio, the mode, the command."""
import http.client, json, os, signal, subprocess, sys, tempfile, time
sys.path.insert(0, os.path.dirname(__file__))
from _common import ROOT, check, check_true, finish, read  # noqa: E402
sys.path.insert(0, os.path.join(ROOT, "src"))
import fakegw_lib  # noqa: E402
fakegw_lib.install()
from mesh_manager import bridge as B, common as C, desktop as D  # noqa: E402

# AC1: the directories
h = "/somewhere/home"   # any home; a real macOS home path reads as ours to the public cut's gate
d = D.app_dirs("Darwin", h, {})
check("AC1 macOS puts it under Application Support", (d["root"], d["config"], d["etc"], d["state"], d["socket"]),
      (h + "/Library/Application Support/Mesh Manager", h + "/Library/Application Support/Mesh Manager/config", h + "/Library/Application Support/Mesh Manager/etc", h + "/Library/Application Support/Mesh Manager/state", h + "/Library/Application Support/Mesh Manager/bridge.sock"))
check("AC1 Linux follows XDG", (D.app_dirs("Linux", "/home/me", {})["root"], D.app_dirs("Linux", "/home/me", {"XDG_DATA_HOME": "/data"})["root"]), ("/home/me/.local/share/mesh-manager", "/data/mesh-manager"))
check("AC1 Windows uses LOCALAPPDATA", D.app_dirs("Windows", "C:\\Users\\me", {"LOCALAPPDATA": "C:\\Users\\me\\AppData\\Local"})["root"], "C:\\Users\\me\\AppData\\Local\\Mesh Manager")

# AC2: the first config
root = tempfile.mkdtemp(); dirs = D.app_dirs("Darwin", root, {}); dirs = {k: v.replace(root + "/Library/Application Support/Mesh Manager", root) for k, v in dirs.items()}
D.first_config(dirs, serial="/dev/cu.usbmodem1234")
txt = open(dirs["config"]).read()
check_true("AC2 the first config is a desktop on loopback with no sign-in", "MODE=desktop" in txt and "BIND=127.0.0.1" in txt and "AUTH=off" in txt and "SERIAL=/dev/cu.usbmodem1234" in txt, txt)
conf = C.read_config(dirs["config"])
check("AC4 read_config keeps the desktop mode", (conf["MODE"], conf["AUTH"], conf["BIND"]), ("desktop", "off", "127.0.0.1"))

# AC3: the radio
check("AC3 the usbmodem is picked on Darwin, --serial wins, none is None",
      (D.find_radio("Darwin", ["/dev/cu.Bluetooth-Incoming-Port", "/dev/cu.debug-console", "/dev/cu.usbmodem5B841E7C1"]), D.find_radio("Darwin", ["/dev/cu.usbmodemX"], wanted="/dev/cu.other"), D.find_radio("Darwin", ["/dev/cu.debug-console"])),
      ("/dev/cu.usbmodem5B841E7C1", "/dev/cu.other", None))

# AC4: the bridge in desktop mode
st = tempfile.mkdtemp()
br = B.Bridge({"SERIAL": "", "MODE": "desktop", "SITE_NAME": "Laptop"}, socket_path=os.path.join(st, "b.sock"), state_dir=st)
s = br.op_status()
check("AC4 the bridge is a desktop with TAK off", (br.box_mode, s.get("mode"), s.get("tak")), ("desktop", "desktop", "off"))
br.stop()

# AC6: the heartbeat and the command's name
hb = os.path.join(st, "heartbeat.json"); open(hb, "w").write("{}")
age = D.heartbeat_age(hb)
check_true("AC6 heartbeat_age reads a fresh file as young and a missing one as None", age is not None and age < 5 and D.heartbeat_age(os.path.join(st, "none.json")) is None, repr(age))
check_true("AC6 pyproject names the command", 'mesh-manager-desktop = "mesh_manager.desktop:main"' in (read("pyproject.toml") or ""))

# AC7 (0.17.1): a deep application directory still runs; the socket moves somewhere a Unix socket can live
long_root = os.path.join(tempfile.mkdtemp(), "a-very-long-application-directory-name-that-a-user-might-well-have", "and-another-level-below-it-for-good-measure", "Mesh Manager")
dl = {"root": long_root, "socket": os.path.join(long_root, "bridge.sock")}
sp = D.socket_for(dl)
check_true("AC7 a long socket path is moved to the temporary directory", sp != dl["socket"] and len(sp.encode()) <= 90 and "mesh-manager-" in sp, sp)
check("AC7 a short one stays", D.socket_for({"root": "/tmp/x", "socket": "/tmp/x/bridge.sock"}), "/tmp/x/bridge.sock")

# AC5: the command itself, with the demo bridge and no browser, under the long root
root2 = long_root
env = {**os.environ, "PYTHONPATH": os.path.join(ROOT, "src") + os.pathsep + os.path.join(ROOT, "tests")}
p = subprocess.Popen([sys.executable, "-m", "mesh_manager.desktop", "--demo", "--no-browser", "--port", "0", "--root", root2], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
port = None; t0 = time.time(); lines = []
while time.time() - t0 < 30 and port is None:
    ln = p.stdout.readline()
    if not ln:
        if p.poll() is not None: break
        continue
    lines.append(ln.rstrip())
    if "http://127.0.0.1:" in ln:
        port = int(ln.split("http://127.0.0.1:")[1].split("/")[0].split()[0].strip(" ,;)"))
check_true("AC5 the command prints the screen's address", port is not None, repr(lines[-5:]))
ok = False; status = {}
if port:
    for _ in range(40):
        try:
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=3); c.request("GET", "/healthz"); r = c.getresponse(); hz = json.loads(r.read().decode()); c.close()
            if hz.get("bridge"):
                c = http.client.HTTPConnection("127.0.0.1", port, timeout=3); c.request("GET", "/api/status"); r = c.getresponse(); status = json.loads(r.read().decode()); c.close(); ok = True; break
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.5)
check_true("AC5 the screen answers with the demo mesh, TAK off", ok and status.get("tak") == "off" and status.get("mode") in ("desktop", "server"), repr({k: status.get(k) for k in ("mode", "tak", "nodes", "connected")}))
check_true("AC5 the directories exist under --root", all(os.path.isdir(os.path.join(root2, x)) for x in ("etc", "state")) and os.path.exists(os.path.join(root2, "config")), repr(os.listdir(root2)))
p.send_signal(signal.SIGINT); t1 = time.time()
try:
    rc = p.wait(timeout=10)
except subprocess.TimeoutExpired:
    p.kill(); rc = "killed"
check_true("AC5 Ctrl-C stops it within ten seconds, the demo bridge with it", rc == 0 and time.time() - t1 < 10 and not os.path.exists(D.socket_for({"root": root2, "socket": os.path.join(root2, "bridge.sock")})), repr((rc, round(time.time() - t1, 1))))
finish()
