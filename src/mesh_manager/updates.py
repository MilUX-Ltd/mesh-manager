"""Updates from GitHub releases (Spec 015): check, download and verify, start the root helper
that installs. The token lives beside the screen's other files at 0600; the check and the
download are the screen's; the install is the update unit's, as root, from a staging directory
the screen filled and verified."""
import hashlib
import json
import os
import re
import subprocess
import threading
import time
import urllib.request

from . import __version__
from .common import utc

DEFAULT_API = "https://api.github.com"
DEFAULT_REPO = "MilUX-Ltd/mesh-manager"   # the public repository: where a release anyone can install comes from.
# A box that should take its releases from somewhere else sets UPDATE_REPO in its config.
UNIT = "mesh-manager-update.service"
VER = re.compile(r"v?(\d+)\.(\d+)\.(\d+)")
CHECK_EVERY = 24 * 3600
FIRST_CHECK_AFTER = 120


def vtuple(s):
    m = VER.search(str(s or ""))
    return tuple(int(x) for x in m.groups()) if m else None


def _get(url, token, accept, timeout=15):
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}", "Accept": accept, "User-Agent": "mesh-manager/" + __version__,
                                               "X-GitHub-Api-Version": "2022-11-28"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def config_arch(config):
    """The architecture the release names. Only amd64 is built, so that is the default whatever
    the box reports about itself; a box on another architecture names it in UPDATE_ARCH once a
    release carries that file. Sniffing the machine here would send every arm box looking for a
    release that does not exist."""
    a = str((config or {}).get("UPDATE_ARCH") or "").strip().lower()
    return a if re.fullmatch(r"[a-z0-9_]{2,12}", a or "") else "amd64"


def _updates_dir(state_dir):
    d = os.path.join(state_dir, "updates")
    os.makedirs(d, exist_ok=True)
    return d


def _record(state_dir, rec):
    try:
        d = _updates_dir(state_dir)
        tmp = os.path.join(d, ".check.tmp")
        with open(tmp, "w") as fh:
            json.dump(rec, fh, indent=1)
        os.replace(tmp, os.path.join(d, "check.json"))
    except OSError:
        pass
    return rec


def last_check(state_dir):
    try:
        return json.load(open(os.path.join(state_dir, "updates", "check.json")))
    except (OSError, ValueError):
        return {}


def last_log(state_dir, n=60):
    try:
        return open(os.path.join(state_dir, "updates", "last.log")).read().splitlines()[-n:]
    except OSError:
        return []


def check(config, token, state_dir, arch="amd64", api=None, running=None):
    """The newest release the channel admits that carries the three files for this architecture."""
    config = config or {}
    repo = str(config.get("UPDATE_REPO") or DEFAULT_REPO)
    channel = str(config.get("UPDATE_CHANNEL") or "prerelease")
    api = (api or config.get("UPDATE_API") or DEFAULT_API).rstrip("/")
    running = running or __version__
    rec = {"checked": utc(time.time()), "repo": repo, "channel": channel, "running": running, "arch": arch}
    if not token:
        rec["error"] = "no GitHub token: enter one on Settings (a fine-grained token, this repository, contents read-only)"
        return _record(state_dir, rec)
    try:
        rels = json.loads(_get(f"{api}/repos/{repo}/releases?per_page=20", token, "application/vnd.github+json"))
    except Exception as e:  # noqa: BLE001
        rec["error"] = f"could not reach GitHub: {type(e).__name__}: {str(e)[:160]}"
        return _record(state_dir, rec)
    best = None
    for r in rels if isinstance(rels, list) else []:
        if r.get("draft"):
            continue
        if r.get("prerelease") and channel != "prerelease":
            continue
        v = vtuple(r.get("tag_name"))
        if not v:
            continue
        ver = ".".join(str(x) for x in v)
        names = {a.get("name"): a for a in r.get("assets", []) if a.get("name")}
        tgz = f"mesh-manager-{ver}-{arch}.tgz"
        if tgz not in names or f"{tgz}.sha256" not in names or "install.sh" not in names:
            continue
        if best is None or v > best[0]:
            best = (v, r, names, ver, tgz)
    if not best:
        rec["error"] = f"no release on {repo} carries mesh-manager-<version>-{arch}.tgz, its .sha256 and install.sh"
        return _record(state_dir, rec)
    v, r, names, ver, tgz = best
    rec.update({"version": ver, "tag": r.get("tag_name"), "name": r.get("name"), "notes": (r.get("body") or "")[:4000], "published": r.get("published_at"),
                "prerelease": bool(r.get("prerelease")), "available": v > (vtuple(running) or (0, 0, 0)),
                "assets": {"tgz": {"name": tgz, "url": names[tgz].get("url"), "size": names[tgz].get("size")},
                           "sha256": {"name": f"{tgz}.sha256", "url": names[f"{tgz}.sha256"].get("url")},
                           "install": {"name": "install.sh", "url": names["install.sh"].get("url")}}})
    return _record(state_dir, rec)


def download(rec, token, state_dir):
    """The three assets into updates/<version>/, the tarball checked against its .sha256, READY only then."""
    if not rec or not rec.get("version") or not rec.get("assets"):
        return {"error": "nothing to download: check first"}
    if not token:
        return {"error": "no GitHub token"}
    d = os.path.join(_updates_dir(state_dir), rec["version"])
    os.makedirs(d, exist_ok=True)
    ready = os.path.join(d, "READY")
    try:
        os.remove(ready)
    except OSError:
        pass
    paths = {}
    for key in ("tgz", "sha256", "install"):
        a = rec["assets"][key]
        try:
            data = _get(a["url"], token, "application/octet-stream", timeout=900)
        except Exception as e:  # noqa: BLE001
            return {"error": f"could not download {a['name']}: {type(e).__name__}: {str(e)[:160]}"}
        p = os.path.join(d, a["name"])
        with open(p, "wb") as fh:
            fh.write(data)
        if key == "install":
            os.chmod(p, 0o755)
        paths[key] = p
    want = (open(paths["sha256"]).read().split() or [""])[0].lower()
    h = hashlib.sha256()
    with open(paths["tgz"], "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    got = h.hexdigest()
    if not want or want != got:
        return {"error": f"sha256 mismatch: the release says {want[:12] or '?'}, the download is {got[:12]}; nothing staged", "dir": d}
    with open(ready, "w") as fh:
        fh.write(paths["tgz"] + "\n")
    return {"ready": True, "dir": d, "tarball": paths["tgz"], "version": rec["version"]}


def staged(state_dir, arch="amd64", running=None):
    """What the box could return to: every release whose artefacts are still under updates/.

    A successful apply removes only the READY marker, so the tarball, its .sha256 and the
    installer that came with it stay on disk. Rolling back is applying one of those again.
    """
    out = []
    root = os.path.join(state_dir, "updates")
    try:
        names = os.listdir(root)
    except OSError:
        return []
    for name in names:
        d = os.path.join(root, name)
        if not os.path.isdir(d) or vtuple(name) is None:
            continue
        tgz = os.path.join(d, f"mesh-manager-{name}-{arch}.tgz")
        if not (os.path.exists(tgz) and os.path.exists(tgz + ".sha256") and os.path.exists(os.path.join(d, "install.sh"))):
            continue
        out.append({"version": name, "tarball": tgz, "size": os.path.getsize(tgz),
                    "staged": utc(os.path.getmtime(tgz)), "running": name == str(running or "")})
    out.sort(key=lambda r: vtuple(r["version"]) or (0, 0, 0), reverse=True)
    return out


def rollback(state_dir, version, running=None, mode="manual", arch="amd64", start_unit=None):
    """Apply a release the box already has. The hash is checked before anything is marked
    ready, because a roll back happens on a box that is already in trouble."""
    version = str(version or "")
    if not version:
        return {"error": "no version given: name the release to return to"}
    if version == str(running or ""):
        return {"error": f"{version} is the running version; there is nothing to return to"}
    rows = {r["version"]: r for r in staged(state_dir, arch=arch, running=running)}
    if version not in rows:
        return {"error": f"{version} is not on this box: nothing under updates/{version} with a tarball, its .sha256 and an installer"}
    tgz = rows[version]["tarball"]
    want = (open(tgz + ".sha256").read().split() or [""])[0].lower()
    h = hashlib.sha256()
    with open(tgz, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    got = h.hexdigest()
    if not want or want != got:
        return {"error": f"sha256 mismatch for {version}: the staged hash says {want[:12] or '?'}, the tarball on disk is {got[:12]}; refusing to roll back to it"}
    ready = os.path.join(os.path.dirname(tgz), "READY")
    with open(ready, "w") as fh:
        fh.write(tgz + "\n")
    os.utime(ready, None)          # update.sh takes the newest READY by mtime
    rc = (start_unit or _systemctl_start)(UNIT)
    if rc != 0:
        try:
            os.remove(ready)
        except OSError:
            pass
        return {"error": f"could not start {UNIT} (exit {rc}): is the polkit rule from install.sh in place?", "version": version}
    out = {"started": True, "version": version, "unit": UNIT,
           "note": "the bridge and the screen restart; watch /healthz for the version. A roll back returns the code, not the box's config."}
    if str(mode or "").lower() == "auto":
        out["warning"] = ("updates are on auto, so the checker will apply the newest release again "
                          "within the day; put updates on manual if this roll back is to stand")
    return out


def _systemctl_start(unit):
    try:
        return subprocess.run(["systemctl", "start", "--no-block", unit], capture_output=True, text=True, timeout=30).returncode
    except (OSError, subprocess.SubprocessError):
        return 1


def apply(state_dir, version, start_unit=None):
    """Start the root helper for a staged version; it installs with the box's config kept."""
    d = os.path.join(state_dir, "updates", str(version or ""))
    ready = os.path.join(d, "READY")
    if not version or not os.path.exists(ready):
        return {"error": f"{version or '?'} is not staged: no READY under {d}; download it first"}
    rc = (start_unit or _systemctl_start)(UNIT)
    if rc != 0:
        return {"error": f"could not start {UNIT} (exit {rc}): is the polkit rule from install.sh in place?", "version": version}
    return {"started": True, "version": version, "unit": UNIT, "note": "the bridge and the screen restart; watch /healthz for the new version"}


class Checker(threading.Thread):
    """Once a day: check; in auto mode, download and apply what is newer."""
    def __init__(self, web, arch="amd64"):
        super().__init__(name="updates", daemon=True)
        self.web, self.arch = web, arch

    def run(self):
        if self.web.stop.wait(FIRST_CHECK_AFTER):
            return
        while not self.web.stop.is_set():
            try:
                mode = self.web.update_mode()
                if mode != "off":
                    rec = check(self.web.config, self.web.github_token(), self.web.state_dir, self.arch)
                    if mode == "auto" and rec.get("available"):
                        d = download(rec, self.web.github_token(), self.web.state_dir)
                        if d.get("ready"):
                            apply(self.web.state_dir, rec["version"])
            except Exception:  # noqa: BLE001
                pass
            self.web.stop.wait(CHECK_EVERY)
