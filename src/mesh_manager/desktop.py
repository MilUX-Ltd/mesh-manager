"""Spec 058: the desktop mode. One command on a laptop: the bridge and the screen in one process tree, no systemd,
the browser opened on the screen, application directories for the platform, the demo mesh when there is no radio.
Part of Mesh Manager, GPL-3.0-or-later."""
import argparse, glob, hashlib, json, os, platform, signal, socket, subprocess, sys, tempfile, threading, time, urllib.error, urllib.request, webbrowser

from .common import read_config
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


def socket_for(dirs, limit=90):
    """The bridge's socket path. A Unix socket path is at most about 104 bytes on macOS (108 on Linux); a deep
    application directory can pass that, so a long one moves the socket to the temporary directory, named for the root."""
    p = dirs["socket"]
    if len(p.encode()) <= limit:
        return p
    tag = hashlib.sha1(dirs["root"].encode()).hexdigest()[:10]
    for base in (tempfile.gettempdir(), "/tmp"):
        q = os.path.join(base, f"mesh-manager-{tag}.sock")
        if len(q.encode()) <= limit:
            return q
    return p


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def _wait_health(url, secs=60, stop=None, procs=()):
    """True once the screen answers with the bridge behind it; False early when asked to stop or a part has died."""
    t0 = time.time()
    while time.time() - t0 < secs:
        if (stop is not None and stop.is_set()) or any(p.poll() is not None for p in procs):
            return False
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                d = json.loads(r.read().decode())
                if d.get("bridge"):
                    return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.5)
    return False


def port_state(port, timeout=1.5):
    """Spec 061 (0.20.1): who has this port. ("free", None) when nobody, ("ours", version) when a Mesh Manager
    screen answers on it, ("busy", None) when something else does. A second launch must say so rather than die."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{int(port)}/healthz", timeout=timeout) as r:
            d = json.loads(r.read().decode())
        return ("ours", str(d.get("version") or "")) if "version" in d else ("busy", None)
    except urllib.error.HTTPError:
        return ("busy", None)
    except Exception:  # noqa: BLE001  nothing listening, or nothing that answers in time
        pass
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)   # 0.20.1: probe as the screen binds, or a socket
    try:                                                       # still closing from the last run reads as busy
        s.bind(("127.0.0.1", int(port)))
        return ("free", None)
    except OSError:
        return ("busy", None)
    finally:
        s.close()


def app_log(dirs):
    """Spec 061 (0.20.1): an application has no terminal, so its own words go to a file a person can read and
    send. Kept small; the newest run is always at the end."""
    d = os.path.join(dirs["root"], "log")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, "app.log")
    try:
        if os.path.getsize(path) > 2_000_000:
            os.replace(path, path + ".1")
    except OSError:
        pass
    fh = open(path, "a", buffering=1)
    fh.write(f"\n== {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} Mesh Manager {__version__} starting\n")
    return fh, path


class Running:
    """Spec 059: the bridge and the screen running as threads of this process, and the one way to stop them."""
    def __init__(self, bridge, srv, sock, port, demo, radio=None, dirs=None):
        self.bridge, self.srv, self.sock, self.port, self.demo = bridge, srv, sock, port, demo
        self.radio, self.dirs = radio, dirs
        self.url = f"http://127.0.0.1:{port}/"
        self._stopped = False
        self._swap = threading.Lock()

    def stopped(self):
        return self._stopped

    def swap_radio(self, radio, dirs=None):
        """Spec 062: put a new bridge on the same socket, with the radio there is now (or none)."""
        dirs = dirs or self.dirs
        if self._stopped or self.demo or not dirs:
            return False
        with self._swap:
            from . import bridge as B
            from .common import read_config
            old = self.bridge
            conf = read_config(dirs["config"])
            conf["SERIAL"] = radio or ""
            try:
                if old:
                    old.stop()
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.5)
            self.bridge = B.Bridge(conf, socket_path=self.sock, state_dir=dirs["state"])
            threading.Thread(target=self.bridge.serve_forever, name="bridge", daemon=True).start()
            self.radio = radio
            return True

    def status(self):
        """What the bridge says of itself, or an empty picture while it is still starting."""
        try:
            with urllib.request.urlopen(self.url + "api/status", timeout=3) as r:
                return json.loads(r.read().decode())
        except Exception:  # noqa: BLE001
            return {}

    def stop(self):
        if self._stopped:
            return
        self._stopped = True
        for step in (lambda: self.srv.shutdown(), lambda: self.srv.server_close(), lambda: self.bridge and self.bridge.stop()):
            try:
                step()
            except Exception:  # noqa: BLE001
                pass
        try:
            os.remove(self.sock)
        except OSError:
            pass


def serve_in_process(dirs, demo=False, radio=None, port=None):
    """Spec 059: inside an application bundle there is no `python -m`, so the parts run here as threads.
    Returns a Running; the caller waits on its url and calls stop()."""
    from . import web as W
    for d in (dirs["root"], dirs["etc"], dirs["state"]):
        os.makedirs(d, exist_ok=True)
    first_config(dirs, serial=radio)
    conf = read_config(dirs["config"])
    if radio:
        conf["SERIAL"] = radio
    sock = socket_for(dirs)
    try:
        os.remove(sock)
    except OSError:
        pass
    bridge = None
    if demo:
        import runpy
        def run_demo():
            argv = sys.argv
            sys.argv = ["mesh_manager.demo", sock]
            os.environ["MODE"] = "desktop"
            try:
                runpy.run_module("mesh_manager.demo", run_name="__main__")
            except Exception:  # noqa: BLE001
                pass
            finally:
                sys.argv = argv
        threading.Thread(target=run_demo, name="demo-bridge", daemon=True).start()
    else:
        from . import bridge as B
        bridge = B.Bridge(conf, socket_path=sock, state_dir=dirs["state"])
        threading.Thread(target=bridge.serve_forever, name="bridge", daemon=True).start()
    want = int(conf.get("PORT") or 8093) if port is None else int(port)
    if want and port_state(want)[0] == "busy":   # 0.20.1: something else holds it; take one the system gives
        want = 0
    srv = W.make_server("127.0.0.1", want, sock, dirs["etc"], conf, state_dir=dirs["state"])
    threading.Thread(target=srv.serve_forever, name="screen", daemon=True).start()
    return Running(bridge, srv, sock, srv.server_address[1], demo, radio=radio, dirs=dirs)


def watch_for_radio(run, dirs, every=4.0):
    """Spec 062: a laptop that started with nothing plugged in takes a radio when one appears, and lets go when
    it is pulled out, by swapping the bridge under the screen. The screen's socket does not move, so nothing the
    person is looking at has to be reloaded."""
    def loop():
        while not run.stopped():
            time.sleep(every)
            try:
                found = find_radio()
                have = run.radio
                if found == have:
                    continue
                if found and not have:
                    print(f"a radio appeared on {found}: taking it", flush=True)
                elif have and not found:
                    print(f"the radio on {have} went away: this laptop is still a site", flush=True)
                else:
                    print(f"the radio changed to {found}: taking it", flush=True)
                run.swap_radio(found, dirs)
            except Exception as ex:  # noqa: BLE001
                print(f"the radio watcher stumbled: {type(ex).__name__}: {ex}", flush=True)
    threading.Thread(target=loop, name="radio-watch", daemon=True).start()


def main(argv=None):
    ap = argparse.ArgumentParser(prog="mesh-manager-desktop", description="Mesh Manager on this computer: the bridge and the screen together, no server, no TAK.")
    ap.add_argument("--root", help="the application directory (default: the platform's, e.g. ~/Library/Application Support/Mesh Manager)")
    ap.add_argument("--serial", help="the radio's port (default: the first USB serial device that looks like one)")
    ap.add_argument("--demo", action="store_true", help="run the demo mesh instead of a radio")
    ap.add_argument("--port", type=int, help="the screen's port (default: the config's, 8093; 0 picks a free one)")
    ap.add_argument("--no-browser", action="store_true", help="do not open the browser on the screen")
    ap.add_argument("--in-process", action="store_true", help="run the bridge and the screen as threads of this process, as the application bundle does (Spec 059)")
    a = ap.parse_args(argv)
    dirs = app_dirs()
    if a.root:
        dirs = {"root": a.root, "config": os.path.join(a.root, "config"), "etc": os.path.join(a.root, "etc"), "state": os.path.join(a.root, "state"), "socket": os.path.join(a.root, "bridge.sock")}
    radio = None if a.demo else find_radio(wanted=a.serial)
    first_config(dirs, serial=radio)
    demo = a.demo   # Spec 062: no radio is a site watching for one, not a demonstration
    if a.in_process or getattr(sys, "frozen", False):
        return _run_together(dirs, demo, radio, a.port, a.no_browser)
    port = a.port if a.port is not None else None
    if port == 0:
        port = free_port()   # --port 0: a free one, named in the line printed below
    sock = socket_for(dirs)
    try:
        os.remove(sock)
    except OSError:
        pass
    env = dict(os.environ); env["MODE"] = "desktop"
    py = sys.executable
    if demo:
        bridge = subprocess.Popen([py, "-m", "mesh_manager.demo", sock], env=env)
        why = "the demo mesh"
    else:
        cmd = [py, "-m", "mesh_manager.bridge", "--config", dirs["config"], "--socket", sock, "--state-dir", dirs["state"]]
        if radio:
            cmd += ["--serial", radio]
        bridge = subprocess.Popen(cmd, env=env)
        why = f"the radio on {radio}" if radio else "no radio yet: a site, watching for one"
    web_cmd = [py, "-m", "mesh_manager.web", "--config", dirs["config"], "--socket", sock, "--etc", dirs["etc"], "--state-dir", dirs["state"]]
    if port:
        web_cmd += ["--port", str(port)]
    web = subprocess.Popen(web_cmd, env=env)
    shown = port or 8093
    url = f"http://127.0.0.1:{shown}/"
    stop = threading.Event()

    def halt(*_):
        stop.set()
    signal.signal(signal.SIGINT, halt); signal.signal(signal.SIGTERM, halt)
    up = _wait_health(url + "healthz", 60, stop=stop, procs=(bridge, web))
    print(f"Mesh Manager {__version__} on this computer: the screen is {url} with {why}; files under {dirs['root']}; Ctrl-C stops it", flush=True)
    if not up and not stop.is_set():
        print("the screen has not answered with the bridge behind it; the log above says why" if (bridge.poll() is not None or web.poll() is not None) else "the screen has not answered yet; it is still starting", flush=True)
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
        os.remove(sock)
    except OSError:
        pass
    return 0


def _run_together(dirs, demo, radio, port, no_browser):
    """Spec 059: the --in-process path and the application bundle's own: start both parts here, wait, stop cleanly."""
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    run = serve_in_process(dirs, demo=demo, radio=radio, port=port)
    if not demo:
        watch_for_radio(run, dirs)
    why = "the demo mesh" if demo else (f"the radio on {radio}" if radio else "no radio yet: a site, watching for one")
    up = _wait_health(run.url + "healthz", 60, stop=stop)
    print(f"Mesh Manager {__version__} on this computer: the screen is {run.url} with {why}; files under {dirs['root']}; Ctrl-C stops it", flush=True)
    if up and not no_browser:
        try:
            webbrowser.open(run.url)
        except Exception:  # noqa: BLE001
            pass
    while not stop.is_set():
        stop.wait(0.5)
    run.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
