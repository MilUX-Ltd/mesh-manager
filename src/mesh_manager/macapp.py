"""Spec 059: Mesh Manager in the macOS menu bar. The bridge and the screen run as threads of this process
(there is no `python -m` inside an application bundle); the menu says what the radio is doing and offers the
screen, the files and Quit. Part of Mesh Manager, GPL-3.0-or-later."""
import atexit
import os
import signal
import subprocess
import sys
import threading
import webbrowser

from . import __version__
from . import appupdate as UP
from . import desktop as D
from . import window as WN


def menu_lines(status, url, radio, update=None):
    """The words in the menu, as a list, built from the bridge's own status. A pure function so the words are
    tested without a menu bar."""
    st = status or {}
    if not radio:
        first = "No radio: showing the demo mesh"
    elif st.get("bootloader"):
        first = f"Radio in bootloader on {os.path.basename(str(radio))}"
    elif st.get("connected"):
        first = f"Radio on {os.path.basename(str(radio))}"
    else:
        first = f"Waiting for the radio on {os.path.basename(str(radio))}"
    heard = st.get("nodes_heard")
    db = st.get("nodes_db")
    second = ("nothing heard yet" if not heard else f"{int(heard)} heard here") + (f", {int(db)} in the radio's database" if db else "")
    third = f"Mesh Manager {update} is waiting" if update else f"The screen is {url}"
    peers = int(st.get("peers") or 0)   # Spec 062: the link, visible with the window closed
    if peers:
        return [first, second, f"joined to {peers} site{'' if peers == 1 else 's'}", third]
    return [first, second, third]


def _stop_on_terminate(run):
    """Spec 059: the app must let go of the radio however it is asked to stop, not only by its own Quit.
    A signal (kill, a script, the terminal) is caught here; the Apple Event that Quit sends from anywhere else
    arrives as the Cocoa notification, which atexit alone does not see because the app never reaches exit."""
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, lambda *_: (run.stop(), os._exit(0)))
        except (ValueError, OSError):  # not the main thread, or no such signal
            pass
    try:
        from AppKit import NSApplicationWillTerminateNotification
        from Foundation import NSNotificationCenter, NSObject

        class _Terminator(NSObject):
            def onTerminate_(self, _note):
                run.stop()

        run._terminator = _Terminator.alloc().init()   # kept alive on the handle; Cocoa does not retain it
        NSNotificationCenter.defaultCenter().addObserver_selector_name_object_(
            run._terminator, "onTerminate:", NSApplicationWillTerminateNotification, None)
    except Exception:  # noqa: BLE001  a Mac without PyObjC, or any other platform
        pass


def main(argv=None):
    """The application bundle's entry. Without the menu-bar library (any platform but a Mac, or a plain
    checkout) it runs the screen the same way the command does, and says so."""
    dirs = D.app_dirs()
    log_fh = None
    if getattr(sys, "frozen", False):   # 0.20.1: an application has no terminal; its words go to a file
        try:
            log_fh, log_path = D.app_log(dirs)
            sys.stdout = sys.stderr = log_fh
            print(f"the log of this run is {log_path}", flush=True)
        except Exception:  # noqa: BLE001
            log_fh = None
    port = int(D.read_config(dirs["config"]).get("PORT") or 8093) if os.path.exists(dirs["config"]) else 8093
    state, running = D.port_state(port)
    if state == "ours":   # 0.20.1: a copy is already running; show its screen rather than dying quietly
        url = f"http://127.0.0.1:{port}/"
        print(f"Mesh Manager {running} is already running on this Mac: its menu-bar item is there, and its screen is {url}", flush=True)
        webbrowser.open(url)
        return 0
    radio = D.find_radio()
    try:
        import rumps
    except Exception:  # noqa: BLE001
        print("no menu bar here (rumps is not installed): running the screen in this terminal instead", flush=True)
        return D._run_together(dirs, radio is None, radio, None, False)

    run = D.serve_in_process(dirs, demo=False, radio=radio, port=None)   # Spec 062: a site, radio or not
    D.watch_for_radio(run, dirs)
    UP.sweep_kept(UP.app_bundle())   # Spec 065: this run is the proof the last update worked; let the old one go
    atexit.register(run.stop)   # whichever way the app ends, the bridge lets go of the radio
    _stop_on_terminate(run)
    D._wait_health(run.url + "healthz", 60)

    class MeshManagerApp(rumps.App):
        def __init__(self):
            super().__init__("Mesh Manager", title="◉", quit_button=None)
            self.state = [rumps.MenuItem(t) for t in menu_lines({}, run.url, radio)]
            for it in self.state:
                it.set_callback(None)
            self.update_item = rumps.MenuItem("Checking for an update", callback=None)
            self.update_item.hidden = True
            self.menu = [*self.state, None,
                         self.update_item, None,
                         rumps.MenuItem("Open Mesh Manager", callback=self.open_screen),
                         rumps.MenuItem("Open in a browser", callback=self.open_browser),
                         rumps.MenuItem("Show the files", callback=self.open_files), None,
                         rumps.MenuItem(f"Mesh Manager {__version__}", callback=None), None,
                         rumps.MenuItem("Quit", callback=self.quit_all)]
            if UP.migrate_stale_off(dirs):
                print("updates: this laptop's config said never check, which nothing chose; moved to telling you", flush=True)
            cfg = D.read_config(dirs["config"]) if os.path.exists(dirs["config"]) else {}
            self.watch = UP.Watcher(dirs, cfg, on_found=self.update_found, on_quit=run.stop)
            self.watch.start()   # it reads the setting each time round, so changing it on Settings takes effect
            rumps.Timer(self.refresh, 5).start()
            self._first = rumps.Timer(self.show_first, 0.4)   # the window opens once the run loop is up
            self._first.start()

        def show_first(self, _=None):
            self._first.stop()
            self.open_screen()

        def update_found(self, version):
            """Spec 065: a version is waiting. On `tell me` the menu offers it; on `take it` the watcher is
            already applying it, and the menu says what is happening."""
            auto = UP.update_mode(self.watch.config) == "auto"
            self.update_item.title = f"Taking {version} now, then restarting" if auto else f"Update to {version}"
            self.update_item.set_callback(None if auto else self.take_update)
            self.update_item.hidden = False

        def take_update(self, _=None):
            self.update_item.title = f"Taking {self.watch.found} now"
            self.update_item.set_callback(None)
            threading.Thread(target=self._take, name="take-update", daemon=True).start()

        def _take(self):
            rec = UP.last_check(dirs)
            rel = UP.newer_release(UP.fetch_releases(), running=__version__) if rec.get("available") else None
            out = UP.take(dirs, rel, bundle=UP.app_bundle()) if rel else {"error": "there is nothing waiting now"}
            if out.get("applied") and UP.relaunch(out["bundle"]):
                run.stop()
                os._exit(0)
            self.update_item.title = str(out.get("error") or "the update did not go in")[:70]
            self.update_item.set_callback(None)

        def refresh(self, _=None):
            for item, words in zip(self.state, menu_lines(run.status(), run.url, radio, self.watch.found)):
                item.title = words

        def open_screen(self, _=None):
            """Spec 061: the app's own window; a browser only where this machine has no web view."""
            if WN.available():
                if not WN.raise_window():
                    WN.open_window(run.url, "Mesh Manager")
                return
            webbrowser.open(run.url)

        def open_browser(self, _=None):
            webbrowser.open(run.url)

        def open_files(self, _=None):
            subprocess.run(["open", dirs["root"]], check=False)

        def quit_all(self, _=None):
            run.stop()
            rumps.quit_application()

    if not WN.available():
        print("no web view on this Mac: the screen opens in a browser instead", flush=True)
    MeshManagerApp().run()
    run.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
