"""Spec 060: Mesh Manager in the Windows notification area. The same shape as the menu-bar app on a Mac: the
bridge and the screen run as threads of this process, a tray icon says what the radio is doing and offers the
screen, the files and Quit. Part of Mesh Manager, GPL-3.0-or-later."""
import atexit
import os
import signal
import subprocess
import sys
import webbrowser

from . import __version__
from . import desktop as D
from . import window as WN
from .macapp import menu_lines   # the same words on both platforms


def _icon_image():
    """The tray icon: the product's own 192 px mark, or a plain square if Pillow cannot read it."""
    from PIL import Image
    here = os.path.dirname(os.path.abspath(__file__))
    for name in ("icon-192.png", "icon-512.png", "maskable-192.png"):
        p = os.path.join(here, "static", "icons", name)
        if os.path.exists(p):
            return Image.open(p).convert("RGBA").resize((64, 64))
    return Image.new("RGBA", (64, 64), (17, 51, 8, 255))


def main(argv=None):
    """The Windows application's entry. Without the tray library it runs the screen in the console and says so."""
    dirs = D.app_dirs()
    log_fh = None
    if getattr(sys, "frozen", False):   # 0.20.1: an application has no console; its words go to a file
        try:
            log_fh, log_path = D.app_log(dirs)
            sys.stdout = sys.stderr = log_fh
            print(f"the log of this run is {log_path}", flush=True)
        except Exception:  # noqa: BLE001
            log_fh = None
    port = int(D.read_config(dirs["config"]).get("PORT") or 8093) if os.path.exists(dirs["config"]) else 8093
    state, running = D.port_state(port)
    if state == "ours":   # 0.20.1
        url = f"http://127.0.0.1:{port}/"
        print(f"Mesh Manager {running} is already running: its icon is by the clock, and its screen is {url}", flush=True)
        webbrowser.open(url)
        return 0
    radio = D.find_radio()
    try:
        import pystray
    except Exception:  # noqa: BLE001
        print("no notification area here (pystray is not installed): running the screen in this window instead", flush=True)
        return D._run_together(dirs, radio is None, radio, None, False)

    run = D.serve_in_process(dirs, demo=radio is None, radio=radio, port=None)
    atexit.register(run.stop)
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, lambda *_: (run.stop(), os._exit(0)))
        except (ValueError, OSError):
            pass
    D._wait_health(run.url + "healthz", 60)

    def open_screen(*_):
        """Spec 061: the app's own window; a browser only where this machine has no web view."""
        if WN.available():
            if not WN.raise_window():
                WN.open_window(run.url, "Mesh Manager")
            return
        webbrowser.open(run.url)

    def open_browser(*_):
        webbrowser.open(run.url)

    def open_files(*_):
        if sys.platform.startswith("win"):
            os.startfile(dirs["root"])  # noqa: S606  the operator's own folder
        else:
            subprocess.run(["open" if sys.platform == "darwin" else "xdg-open", dirs["root"]], check=False)

    def quit_all(icon, *_):
        run.stop()
        icon.stop()

    def state(_=None):
        return "\n".join(menu_lines(run.status(), run.url, radio))

    menu = pystray.Menu(
        pystray.MenuItem(lambda _: menu_lines(run.status(), run.url, radio)[0], None, enabled=False),
        pystray.MenuItem(lambda _: menu_lines(run.status(), run.url, radio)[1], None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Open Mesh Manager", open_screen, default=True),
        pystray.MenuItem("Open in a browser", open_browser),
        pystray.MenuItem("Show the files", open_files),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(f"Mesh Manager {__version__}", None, enabled=False),
        pystray.MenuItem("Quit", quit_all),
    )
    icon = pystray.Icon("mesh-manager", _icon_image(), f"Mesh Manager {__version__}", menu)
    if WN.available():
        # Spec 061: the web view owns the main loop on Windows, so the icon runs beside it
        import webview
        icon.run_detached()
        WN.open_window(run.url, "Mesh Manager")
        webview.start()
        icon.stop()
    else:
        print("no web view on this machine: the screen opens in a browser instead", flush=True)
        webbrowser.open(run.url)
        icon.run()
    run.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
