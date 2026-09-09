#!/usr/bin/env python3
"""Spec 065: the application updates itself. What it picks, what it trusts, when it looks, what it says."""
import hashlib, json, os, sys, tempfile
sys.path.insert(0, os.path.dirname(__file__))
from _common import ROOT, check, check_true, finish, read  # noqa: E402
sys.path.insert(0, os.path.join(ROOT, "src"))
from mesh_manager import appupdate as U, macapp as M  # noqa: E402

RELEASES = [
    {"tag_name": "v0.23.0", "prerelease": True, "assets": [
        {"name": "Mesh-Manager-0.23.0.dmg", "browser_download_url": "https://x/0.23.0.dmg", "size": 30},
        {"name": "Mesh-Manager-0.23.0.dmg.sha256", "browser_download_url": "https://x/0.23.0.dmg.sha256", "size": 96},
        {"name": "Mesh-Manager-0.23.0-windows-x64.zip", "browser_download_url": "https://x/0.23.0.zip", "size": 28},
        {"name": "mesh-manager-0.23.0-amd64.tgz", "browser_download_url": "https://x/t", "size": 21}]},
    {"tag_name": "v0.22.0", "prerelease": True, "assets": [
        {"name": "Mesh-Manager-0.22.0.dmg", "browser_download_url": "https://x/0.22.0.dmg", "size": 30}]},
]

# AC1: what it picks
r = U.newer_release(RELEASES, running="0.22.0", system="Darwin")
check("AC1 the newest above the running one, and the file for this platform", (r["version"], r["asset"]["name"]), ("0.23.0", "Mesh-Manager-0.23.0.dmg"))
check("AC1 the hash beside it is found", (r.get("sha_url") or "").endswith(".dmg.sha256"), True)
check("AC1 Windows takes the zip", U.newer_release(RELEASES, running="0.22.0", system="Windows")["asset"]["name"], "Mesh-Manager-0.23.0-windows-x64.zip")
check("AC1 nothing when the running version is the newest", U.newer_release(RELEASES, running="0.23.0", system="Darwin"), None)
check("AC1 nor when it is ahead of the newest", U.newer_release(RELEASES, running="0.24.0", system="Darwin"), None)
check("AC1 a platform with no build is nothing, not an error", U.newer_release(RELEASES, running="0.22.0", system="Linux"), None)

# AC2: what it trusts
d = tempfile.mkdtemp(); blob = os.path.join(d, "x.dmg")
open(blob, "wb").write(b"a disk image")
good = hashlib.sha256(b"a disk image").hexdigest()
check("AC2 a matching hash is accepted", U.hash_ok(blob, good + "  Mesh-Manager-0.23.0.dmg"), True)
check("AC2 a hash that does not match is refused", U.hash_ok(blob, "0" * 64), False)
check("AC2 and no hash at all is refused", U.hash_ok(blob, ""), False)

# AC3: when it looks
cfg = os.path.join(d, "config")
open(cfg, "w").write("MODE=desktop\n")
check("AC3 telling is the default", U.update_mode({"MODE": "desktop"}), "manual")
check("AC3 off is honoured", U.update_mode({"UPDATE_MODE": "off"}), "off")
check("AC3 and so is taking it", U.update_mode({"UPDATE_MODE": "auto"}), "auto")
check("AC3 a word nobody knows falls back to telling", U.update_mode({"UPDATE_MODE": "sometimes"}), "manual")

# AC4: applying keeps what works
src = read("src/mesh_manager/appupdate.py") or ""
check_true("AC4 the swap keeps the old application until the new one runs", "keep" in src.lower() and "rollback" in src.lower() or "put back" in src.lower())
check_true("AC4 and the download never lands on the running application", "tempfile" in src and "mkdtemp" in src)
check_true("AC4 the image is mounted and let go", "hdiutil" in src and "detach" in src)

# AC5: what the menu says
lines = M.menu_lines({"connected": True, "radio": "/dev/cu.usbmodem1", "nodes_heard": 1, "mode": "desktop"}, "http://127.0.0.1:8093/", "/dev/cu.usbmodem1", update="0.23.0")
check_true("AC5 the menu names the version waiting", any("0.23.0" in l for l in lines), repr(lines))
mac = read("src/mesh_manager/macapp.py") or ""
check_true("AC5 and offers to take it", "Update to" in mac and "appupdate" in mac)
check_true("AC5 with auto it takes it unasked", '"auto"' in mac or "'auto'" in mac)

# AC6: the guide
g = read("docs/GUIDE.md") or ""
check_true("AC6 the guide says how a laptop updates", "UPDATE_MODE" in g and "update" in g.lower() and "laptop" in g.lower())
finish()
