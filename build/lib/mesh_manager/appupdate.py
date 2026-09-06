"""Spec 065: the application on a laptop updates itself.

A box takes its updates from About, as a root unit driven by systemd. A laptop has none of that, so this is the
laptop's own path: look at the public releases, take the file for this platform, refuse anything whose hash does
not match, and swap the application over while keeping the old one until the new one has started.

Nothing here needs a token: the releases it reads are public. Part of Mesh Manager, GPL-3.0-or-later.
"""
import hashlib
import json
import os
import platform
import plistlib
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import zipfile

from . import __version__
from .common import utc
from .updates import vtuple

RELEASES = "https://api.github.com/repos/MilUX-Ltd/mesh-manager/releases?per_page=20"
CHECK_EVERY = 6 * 3600
FIRST_CHECK_AFTER = 90
MODES = ("off", "manual", "auto")


def update_mode(config):
    """Three settings, in the words the box already uses: never look, tell me, take it. Telling is the default,
    and so is anything nobody recognises: a typo in the config must not turn checking off silently."""
    m = str((config or {}).get("UPDATE_MODE") or "").strip().lower()
    return m if m in MODES else "manual"


def stored_mode(dirs, config):
    """The setting as the screen last saved it, else the config's. The screen and the menu bar read the same
    file, so changing it in one place changes it in both."""
    try:
        m = json.load(open(os.path.join(dirs["etc"], "update.json"))).get("mode")
        if m in MODES:
            return m
    except (OSError, ValueError, AttributeError, TypeError):
        pass
    return update_mode(config)


def migrate_stale_off(dirs):
    """First runs before this version wrote UPDATE_MODE=off into every laptop's config, because a laptop had no
    updater and the box's checker would only have logged errors without a token. Nobody chose that, so it is
    moved to telling once, and the move is recorded so a person who does choose off keeps it."""
    marker = os.path.join(dirs["etc"], "update-mode-migrated")
    if os.path.exists(marker) or not os.path.exists(dirs["config"]):
        return False
    try:
        text = open(dirs["config"]).read()
        if "UPDATE_MODE=off" not in text:
            open(marker, "w").write("nothing to move\n")
            return False
        with open(dirs["config"], "w") as fh:
            fh.write(text.replace("UPDATE_MODE=off", "UPDATE_MODE=manual"))
        os.makedirs(dirs["etc"], exist_ok=True)
        open(marker, "w").write(utc(time.time()) + " off -> manual\n")
        return True
    except OSError:
        return False


def asset_name(system, version):
    """The file a release carries for this platform, or None where there is no build for it."""
    if system == "Darwin":
        return f"Mesh-Manager-{version}.dmg"
    if system == "Windows":
        return f"Mesh-Manager-{version}-windows-x64.zip"
    return None   # a laptop on Linux installs the tarball; it is not a bundled application


def newer_release(releases, running=None, system=None):
    """The newest release above the running version that carries the file for this platform, with the hash
    beside it. Nothing, and no complaint, when the running version is the newest or there is no build here."""
    running = vtuple(running or __version__) or (0, 0, 0)
    system = system or platform.system()
    best = None
    for r in releases if isinstance(releases, list) else []:
        if r.get("draft"):
            continue
        v = vtuple(r.get("tag_name"))
        if not v or v <= running:
            continue
        ver = ".".join(str(x) for x in v)
        names = {a.get("name"): a for a in r.get("assets") or [] if a.get("name")}
        want = asset_name(system, ver)
        if not want or want not in names:
            continue
        if best is None or v > best[0]:
            best = (v, {"version": ver, "tag": r.get("tag_name"), "name": r.get("name"),
                        "notes": (r.get("body") or "")[:4000], "published": r.get("published_at"),
                        "asset": names[want],
                        "sha_url": (names.get(want + ".sha256") or {}).get("browser_download_url")})
    return best[1] if best else None


def fetch_releases(url=None, timeout=20):
    """The public releases, unauthenticated: this repository is the one anyone can install from."""
    req = urllib.request.Request(url or RELEASES, headers={
        "Accept": "application/vnd.github+json", "User-Agent": f"MeshManager/{__version__}",
        "X-GitHub-Api-Version": "2022-11-28"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def hash_ok(path, sha_text):
    """Whether the file matches the hash published beside it. No hash, or one that does not match, is a no:
    an update nobody can check is not taken."""
    want = (str(sha_text or "").split() or [""])[0].strip().lower()
    if len(want) != 64:
        return False
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest() == want


def _fetch(url, timeout=900):
    req = urllib.request.Request(url, headers={"Accept": "application/octet-stream",
                                               "User-Agent": f"MeshManager/{__version__}"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def download(rel, into=None):
    """The release's file into a temporary directory of its own, checked against its hash. The download never
    lands anywhere near the running application, and a file that fails its hash is thrown away where it lies."""
    if not rel or not rel.get("asset"):
        return {"error": "nothing to download: check first"}
    url = rel["asset"].get("browser_download_url")
    if not url:
        return {"error": f"the release has no download for {rel['asset'].get('name')}"}
    if not rel.get("sha_url"):
        return {"error": f"{rel['asset']['name']} has no .sha256 beside it on the release: refusing an update nothing can check"}
    d = into or tempfile.mkdtemp(prefix="mesh-manager-update-")
    p = os.path.join(d, rel["asset"]["name"])
    try:
        with open(p, "wb") as fh:
            fh.write(_fetch(url))
        sha = _fetch(rel["sha_url"], timeout=60).decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        return {"error": f"could not download {rel['asset']['name']}: {type(e).__name__}: {str(e)[:160]}", "dir": d}
    if not hash_ok(p, sha):
        try:
            os.remove(p)
        except OSError:
            pass
        return {"error": f"the hash does not match what the release publishes for {rel['asset']['name']}: nothing taken", "dir": d}
    return {"ready": True, "path": p, "dir": d, "version": rel["version"]}


def app_bundle(argv0=None):
    """The application this process is running from: the .app on a Mac, the folder on Windows, else None."""
    exe = os.path.abspath(argv0 or sys.executable)
    if not getattr(sys, "frozen", False):
        return None
    p = exe
    while p not in ("/", ""):
        if p.endswith(".app"):
            return p
        p = os.path.dirname(p)
    return os.path.dirname(exe) if platform.system() == "Windows" else None


def _run(cmd, timeout=300):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _mount(dmg):
    """Mount the image and give back its mount point. Read the plist rather than scraping the words."""
    r = _run(["hdiutil", "attach", dmg, "-nobrowse", "-readonly", "-plist"])
    if r.returncode != 0:
        raise OSError(f"hdiutil attach said {r.returncode}: {(r.stderr or '').strip()[:200]}")
    for ent in plistlib.loads(r.stdout.encode()).get("system-entities", []):
        if ent.get("mount-point"):
            return ent["mount-point"]
    raise OSError("the disk image mounted with nothing on it")


def _detach(point):
    if point:
        _run(["hdiutil", "detach", point, "-quiet"], timeout=120)


def apply_dmg(dmg, bundle, keep=None):
    """Swap the application over on a Mac.

    The new one is copied out of the image first, then the running one is moved aside and the new one put in its
    place. Every step that can fail is undone: if the copy in fails, the old bundle goes back where it was, so a
    failed update leaves what was working exactly as it was. The old one is kept until the new one has started.
    """
    if not bundle or not os.path.isdir(bundle):
        return {"error": f"cannot find the running application ({bundle or 'not a bundle'}): update it by hand"}
    point, staged = None, None
    try:
        point = _mount(dmg)
        found = [os.path.join(point, n) for n in sorted(os.listdir(point)) if n.endswith(".app")]
        if not found:
            return {"error": "the disk image carries no application"}
        staged = os.path.join(tempfile.mkdtemp(prefix="mesh-manager-new-"), os.path.basename(bundle))
        shutil.copytree(found[0], staged, symlinks=True)
    except Exception as e:  # noqa: BLE001
        return {"error": f"could not read the new application: {type(e).__name__}: {str(e)[:200]}"}
    finally:
        _detach(point)
    kept = keep or f"{bundle}.old"
    shutil.rmtree(kept, ignore_errors=True)
    try:
        os.rename(bundle, kept)
    except OSError as e:
        return {"error": f"could not move the running application aside: {e}; nothing changed"}
    try:
        shutil.move(staged, bundle)
    except Exception as e:  # noqa: BLE001  put back: the running application must survive a failed update
        try:
            os.rename(kept, bundle)
        except OSError:
            pass
        return {"error": f"could not put the new application in place: {type(e).__name__}: {str(e)[:200]}; the old one is back"}
    return {"applied": True, "bundle": bundle, "kept": kept}


def apply_zip(zpath, folder, keep=None):
    """The same swap on Windows, out of the release's zip."""
    if not folder or not os.path.isdir(folder):
        return {"error": f"cannot find the running application ({folder or 'not a folder'}): update it by hand"}
    d = tempfile.mkdtemp(prefix="mesh-manager-new-")
    try:
        with zipfile.ZipFile(zpath) as z:
            z.extractall(d)
    except Exception as e:  # noqa: BLE001
        return {"error": f"could not read the new application: {type(e).__name__}: {str(e)[:200]}"}
    inner = [os.path.join(d, n) for n in os.listdir(d) if os.path.isdir(os.path.join(d, n))]
    staged = inner[0] if len(inner) == 1 else d
    kept = keep or f"{folder}.old"
    shutil.rmtree(kept, ignore_errors=True)
    try:
        os.rename(folder, kept)
    except OSError as e:
        return {"error": f"could not move the running application aside: {e}; nothing changed"}
    try:
        shutil.move(staged, folder)
    except Exception as e:  # noqa: BLE001  put back, as on a Mac
        try:
            os.rename(kept, folder)
        except OSError:
            pass
        return {"error": f"could not put the new application in place: {type(e).__name__}: {str(e)[:200]}; the old one is back"}
    return {"applied": True, "bundle": folder, "kept": kept}


def apply(path, bundle=None, system=None):
    """Swap in what was downloaded, whichever platform this is."""
    system = system or platform.system()
    bundle = bundle or app_bundle()
    if system == "Darwin":
        return apply_dmg(path, bundle)
    if system == "Windows":
        return apply_zip(path, bundle)
    return {"error": f"there is no in-application update on {system}: install the tarball"}


def relaunch(bundle, system=None):
    """Start the version just put in place and leave. The old one is removed by the new one on its next run."""
    system = system or platform.system()
    try:
        if system == "Darwin":
            subprocess.Popen(["open", "-n", bundle], start_new_session=True)
        else:
            exe = os.path.join(bundle, "Mesh Manager.exe")
            subprocess.Popen([exe], close_fds=True)
        return True
    except Exception:  # noqa: BLE001
        return False


def sweep_kept(bundle):
    """Remove the application kept from the last update. Called once this one has started, which is the proof
    the swap worked; until then the old one stays on disk."""
    if not bundle:
        return False
    kept = f"{bundle}.old"
    if os.path.isdir(kept):
        shutil.rmtree(kept, ignore_errors=True)
        return True
    return False


def _state(dirs):
    d = os.path.join(dirs["state"], "updates")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "check.json")


def record(dirs, rec):
    try:
        p = _state(dirs)
        with open(p + ".tmp", "w") as fh:
            json.dump(rec, fh, indent=1)
        os.replace(p + ".tmp", p)
    except OSError:
        pass
    return rec


def last_check(dirs):
    try:
        return json.load(open(_state(dirs)))
    except (OSError, ValueError):
        return {}


def check(dirs, running=None, system=None, url=None):
    """Look, and write down what was seen. An unreachable network is a note, not a fault."""
    running = running or __version__
    rec = {"checked": utc(time.time()), "running": running}
    try:
        rel = newer_release(fetch_releases(url), running=running, system=system)
    except Exception as e:  # noqa: BLE001
        rec["error"] = f"could not reach the releases page: {type(e).__name__}: {str(e)[:160]}"
        return record(dirs, rec)
    if rel:
        rec.update({"version": rel["version"], "notes": rel.get("notes"), "published": rel.get("published"),
                    "asset": rel["asset"].get("name"), "checkable": bool(rel.get("sha_url")), "available": True})
        rec["_rel"] = rel
    else:
        rec["available"] = False
    return record(dirs, rec)


def take(dirs, rel, bundle=None, system=None):
    """Download, check the hash, swap. Every refusal says why, and leaves the running application alone."""
    got = download(rel)
    if not got.get("ready"):
        return got
    out = apply(got["path"], bundle=bundle, system=system)
    shutil.rmtree(got.get("dir") or "", ignore_errors=True)
    if out.get("applied"):
        out["version"] = rel["version"]
    return out


class Watcher(threading.Thread):
    """The laptop's own checker: at start, then every six hours. On `auto` it takes what it finds; on `manual`
    it only makes the menu say so; on `off` this thread is never started. One at a time, always."""
    def __init__(self, dirs, config, on_found=None, bundle=None, running=None, on_quit=None):
        super().__init__(name="app-updates", daemon=True)
        self.dirs, self.config = dirs, config or {}
        self.on_found, self.on_quit = on_found, on_quit
        self.bundle = bundle or app_bundle()
        self.running_version = running or __version__
        self.stop = threading.Event()
        self.found = None          # the version waiting, for the menu to read
        self.busy = False

    def mode(self):
        return stored_mode(self.dirs, self.config)

    def once(self):
        if self.busy or self.mode() == "off":
            return None
        self.busy = True
        try:
            rec = check(self.dirs, running=self.running_version)
            rel = rec.pop("_rel", None)
            self.found = rec.get("version") if rec.get("available") else None
            if self.found and self.on_found:
                self.on_found(self.found)
            if rel and self.mode() == "auto":
                out = take(self.dirs, rel, bundle=self.bundle)
                if out.get("applied"):
                    return out
            return rec
        except Exception:  # noqa: BLE001  a check must never take the application down
            return None
        finally:
            self.busy = False

    def run(self):
        if self.stop.wait(FIRST_CHECK_AFTER):
            return
        while not self.stop.is_set():
            out = self.once()
            if isinstance(out, dict) and out.get("applied") and relaunch(out["bundle"]):
                if self.on_quit:      # let go of the radio first: the new copy cannot open a port this one holds
                    try:
                        self.on_quit()
                    except Exception:  # noqa: BLE001
                        pass
                os._exit(0)
            self.stop.wait(CHECK_EVERY)
