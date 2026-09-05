"""Spec 058: the desktop mode. One command on a laptop: the bridge and the screen in one process tree, no systemd,
the browser opened on the screen, application directories for the platform, the demo mesh when there is no radio.
Part of Mesh Manager, GPL-3.0-or-later."""
import argparse, glob, json, os, platform, signal, socket, subprocess, sys, threading, time, urllib.request, webbrowser

from . import __version__

APP = "Mesh Manager"


def app_dirs(system=None, home=None, env=None):
    """The application directories for a platform: root, config, etc, state, socket. No dependency: three rules."""
    system = system or platform.system(); home = home or os.path.expanduser("~"); env = os.environ if env is None else env
    j = (lambda *p: "\\".join(p)) if system == "Windows" else os.path.join   # the platform's separator, whatever this suite runs on
    if system == "Darwin":
        root = j(home, "Library", "Application Support", APP)
    elif system == "Windows":
        root = j(env.get("LOCALAPPDATA") or j(home, "AppData", "Local"), APP)
    else:
        root = j(env.get("XDG_DATA_HOME") or j(home, ".local", "share"), "mesh-manager")
    return {"root": root, "config": j(root, "config"), "etc": j(root, "etc"), "state": j(root, "state"), "socket": j(root, "bridge.sock")}


def first_config(dirs, serial=None):
    """Write the first config for a laptop: a desktop on loopback, no sign-in on its own loopback, the radio if known."""
    os.makedirs(dirs["root"], exist_ok=True); os.makedirs(dirs["etc"], exist_ok=True); os.makedirs(dirs["state"], exist_ok=True)
    if os.path.exists(dirs["config"]):
        return False
    lines = ["# written by mesh-manager-desktop (Spec 058); one line per setting, as on a box", "MODE=desktop", "BIND=127.0.0.1", "PORT=8093", "AUTH=off",
             "REGION=EU_868", "CHANNEL=", "UPDATE_MODE=off"]
    if serial:
        lines.append(f"SERIAL={serial}")
    with open(dirs["config"], "w") as fh:
        fh.write("\n".join(lines) + "\n")
    return True


def find_radio(system=None, candidates=None, wanted=None):
    """The radio's port: the one asked for, else the first that looks like a Meshtastic device on this platform."""
    if wanted:
        return wanted
    system = system or platform.system()
    if candidates is None:
        if system == "Darwin":
            candidates = sorted(glob.glob("/dev/cu.usbmodem*") + glob.glob("/dev/cu.usbserial*") + glob.glob("/dev/cu.SLAB*") + glob.glob("/dev/cu.wchusbserial*"))
        elif system == "Linux":
            candidates = sorted(glob.glob("/dev/serial/by-id/*")) or sorted(glob.glob("/dev/ttyACM*") + glob.glob("/dev/ttyUSB*"))
        else:
            try:
                from serial.tools import list_ports  # pyserial travels with meshtastic
                candidates = [p.device for p in list_ports.comports()]
            except Exception:  # noqa: BLE001
                candidates = []
    looks = ("usbmodem", "usbserial", "SLAB", "wchusbserial", "ttyACM", "ttyUSB", "by-id", "COM")
    for c in candidates:
        base = os.path.basename(str(c))
        if any(w in base for w in looks) and "Bluetooth" not in base and "debug-console" not in base:
            return str(c)
    return None


def heartbeat_age(path):
    """Seconds since the bridge last touched its heartbeat, or None when there is none yet."""
    try:
        return max(0.0, time.time() - os.path.getmtime(path))
    except OSError:
        return None


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def _wait_health(url, secs=60):
    t0 = time.time()
    while time.time() - t0 < secs:
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                d = json.loads(r.read().decode())
                if d.get("bridge"):
                    return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.5)
    return False


def main(argv=None):
    ap = argparse.ArgumentParser(prog="mesh-manager-desktop", description="Mesh Manager on this computer: the bridge and the screen together, no server, no TAK.")
    ap.add_argument("--root", help="the application directory (default: the platform's, e.g. ~/Library/Application Support/Mesh Manager)")
    ap.add_argument("--serial", help="the radio's port (default: the first USB serial device that looks like one)")
    ap.add_argument("--demo", action="store_true", help="run the demo mesh instead of a radio")
    ap.add_argument("--port", type=int, help="the screen's port (default: the config's, 8093; 0 picks a free one)")
    ap.add_argument("--no-browser", action="store_true", help="do not open the browser on the screen")
    a = ap.parse_args(argv)
    dirs = app_dirs()
    if a.root:
        dirs = {"root": a.root, "config": os.path.join(a.root, "config"), "etc": os.path.join(a.root, "etc"), "state": os.path.join(a.root, "state"), "socket": os.path.join(a.root, "bridge.sock")}
    radio = None if a.demo else find_radio(wanted=a.serial)
    first_config(dirs, serial=radio)
    demo = a.demo or radio is None
    port = a.port if a.port is not None else None
    if port == 0:
        port = free_port()   # --port 0: a free one, named in the line printed below
    try:
        os.remove(dirs["socket"])
    except OSError:
        pass
    env = dict(os.environ); env["MODE"] = "desktop"
    py = sys.executable
    if demo:
        bridge = subprocess.Popen([py, "-m", "mesh_manager.demo", dirs["socket"]], env=env)
        why = "the demo mesh (no radio found)" if not a.demo else "the demo mesh"
    else:
        bridge = subprocess.Popen([py, "-m", "mesh_manager.bridge", "--config", dirs["config"], "--socket", dirs["socket"], "--state-dir", dirs["state"], "--serial", radio], env=env)
        why = f"the radio on {radio}"
    web_cmd = [py, "-m", "mesh_manager.web", "--config", dirs["config"], "--socket", dirs["socket"], "--etc", dirs["etc"], "--state-dir", dirs["state"]]
    if port:
        web_cmd += ["--port", str(port)]
    web = subprocess.Popen(web_cmd, env=env)
    shown = port or 8093
    url = f"http://127.0.0.1:{shown}/"
    stop = threading.Event()

    def halt(*_):
        stop.set()
    signal.signal(signal.SIGINT, halt); signal.signal(signal.SIGTERM, halt)
    up = _wait_health(url + "healthz", 60)
    print(f"Mesh Manager {__version__} on this computer: the screen is {url} with {why}; files under {dirs['root']}; Ctrl-C stops it", flush=True)
    if not up:
        print("the screen has not answered yet; it is still starting, or the log above says why", flush=True)
    if up and not a.no_browser:
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass
    last_warn = 0.0
    while not stop.is_set():
        if bridge.poll() is not None or web.poll() is not None:
            print("a part stopped on its own (bridge rc %s, screen rc %s); stopping" % (bridge.poll(), web.poll()), flush=True); break
        age = heartbeat_age(os.path.join(dirs["state"], "heartbeat.json"))
        if not demo and age is not None and age > 120 and time.time() - last_warn > 60:
            print(f"the bridge's heartbeat is {int(age)} s old; the radio may be gone", flush=True); last_warn = time.time()
        stop.wait(1.0)
    for pr in (web, bridge):
        if pr.poll() is None:
            pr.terminate()
    t0 = time.time()
    for pr in (web, bridge):
        try:
            pr.wait(timeout=max(0.5, 8 - (time.time() - t0)))
        except subprocess.TimeoutExpired:
            pr.kill()
    try:
        os.remove(dirs["socket"])
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
