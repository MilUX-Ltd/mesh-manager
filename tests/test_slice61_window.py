#!/usr/bin/env python3
"""Spec 061: the app's own window. What it is, when it is used, and what happens where there is none."""
import os, sys
sys.path.insert(0, os.path.dirname(__file__))
from _common import ROOT, check, check_true, finish, read, skip  # noqa: E402
sys.path.insert(0, os.path.join(ROOT, "src"))
from mesh_manager import __version__, window as WN  # noqa: E402

# AC1: what the window is
check_true("AC1 the module says whether this machine can show one", isinstance(WN.available(), bool))
check_true("AC1 the view says who it is", WN.user_agent().startswith("MeshManager/") and __version__ in WN.user_agent(), WN.user_agent())
check_true("AC1 on a Mac with the binding it can; without a web view it cannot",
           WN.available() == bool(WN._backend()), repr((WN.available(), WN._backend())))
check_true("AC1 opening one where there is none answers politely rather than raising",
           WN.open_window("http://127.0.0.1:9/", "x") is None or WN.available(), "an unavailable backend must return None")

# AC2 and AC3: which the apps use
mac = read("src/mesh_manager/macapp.py") or ""
win = read("src/mesh_manager/winapp.py") or ""
for name, src in (("the Mac app", mac), ("the Windows app", win)):
    check_true(f"AC2/AC3 {name} opens the window when there is one and the browser when there is not",
               "window" in src and "WN.available()" in src and "webbrowser.open" in src, src[:0] or name)
    check_true(f"AC2/AC3 {name} offers both in its menu", "Open Mesh Manager" in src and "Open in a browser" in src)
check_true("AC2 the Mac app no longer opens a browser at start", "webbrowser.open(run.url)\n    MeshManagerApp" not in mac)

# AC4: the builds carry the bindings
sh = read("release/build-macapp.sh") or ""
ps = read("release/build-winapp.ps1") or ""
if sh:
    check_true("AC4 the Mac build installs and collects the WebKit binding", "pyobjc-framework-WebKit" in sh and "--collect-all WebKit" in sh)
else:
    skip("AC4 the Mac build installs and collects the WebKit binding", "the release tooling is not in this tree; this check runs in the source repository")
if ps:
    check_true("AC4 the Windows build installs and collects its web view", "pywebview" in ps and "--collect-all webview" in ps)
else:
    skip("AC4 the Windows build installs and collects its web view", "the release tooling is not in this tree; this check runs in the source repository")

# AC5: the guide
g = read("docs/GUIDE.md") or ""
check_true("AC5 the guide says the app has its own window and closing it leaves the bridge running",
           "own window" in g and "closing" in g.lower() and "keeps running" in g.lower())
# 0.20.1, found by Matt: the app would not open while another copy held the port, and died without a word.
from mesh_manager import desktop as D  # noqa: E402
check("0.20.1 a port nobody holds is free", D.port_state(59991)[0], "free")
check_true("0.20.1 the app tells a person a copy is already running rather than dying quietly",
           "already running" in mac and "already running" in win, "both apps must say it")
check_true("0.20.1 and it writes a log a person can read, since an application has no terminal",
           "app_log" in mac and "app_log" in win and "def app_log" in (read("src/mesh_manager/desktop.py") or ""))
check_true("0.20.1 a port held by something else is not fatal", 'port_state(want)[0] == "busy"' in (read("src/mesh_manager/desktop.py") or ""))
if sh:
    check_true("0.20.1 the bundle refuses a second copy from Finder", "LSMultipleInstancesProhibited" in sh)
else:
    skip("0.20.1 the bundle refuses a second copy from Finder", "the release tooling is not in this tree; this check runs in the source repository")
check_true("0.20.1 the probe binds the way the screen binds, so a socket still closing is not read as busy",
           "SO_REUSEADDR" in (read("src/mesh_manager/desktop.py") or ""))

finish()
