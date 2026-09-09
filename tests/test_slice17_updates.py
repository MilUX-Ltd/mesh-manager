#!/usr/bin/env python3
"""Spec 015: updates from GitHub, against a fake GitHub on this machine."""
import hashlib
import http.client
import http.server
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(__file__))
from _common import ROOT, check, check_true, finish, read, skip  # noqa: E402
sys.path.insert(0, os.path.join(ROOT, "src"))
from fakebridge_lib import start_fake_bridge  # noqa: E402
from mesh_manager import web as W, updates as U, __version__  # noqa: E402

TGZ = b"\x1f\x8b" + b"tarball bytes " * 50
SHA = hashlib.sha256(TGZ).hexdigest()
calls = []


class FakeGitHub(http.server.BaseHTTPRequestHandler):
    releases = []
    assets = {}

    def log_message(self, *a):
        pass

    def do_GET(self):
        calls.append((self.path, self.headers.get("Authorization"), self.headers.get("Accept")))
        if not self.headers.get("Authorization", "").startswith("Bearer "):
            self.send_response(401); self.end_headers(); return
        if self.path.startswith("/repos/MilUX-Ltd/mesh-manager/releases"):
            body = json.dumps(FakeGitHub.releases).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body); return
        if self.path.startswith("/assets/"):
            body = FakeGitHub.assets.get(self.path.split("/")[-1])
            if body is None:
                self.send_response(404); self.end_headers(); return
            self.send_response(200); self.send_header("Content-Type", "application/octet-stream"); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body); return
        self.send_response(404); self.end_headers()


gh = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeGitHub)
threading.Thread(target=gh.serve_forever, daemon=True).start()
API = f"http://127.0.0.1:{gh.server_address[1]}"


def rel(ver, pre=True, draft=False, assets=True, arch="amd64", sha=SHA, tgz=TGZ):
    names = [f"mesh-manager-{ver}-{arch}.tgz", f"mesh-manager-{ver}-{arch}.tgz.sha256", "install.sh"] if assets else []
    FakeGitHub.assets[f"{ver}-tgz"] = tgz
    FakeGitHub.assets[f"{ver}-sha"] = f"{sha}  mesh-manager-{ver}-{arch}.tgz\n".encode()
    FakeGitHub.assets[f"{ver}-install"] = b"#!/bin/sh\necho staged installer $1\n"
    ids = {names[0]: f"{ver}-tgz", names[1]: f"{ver}-sha", "install.sh": f"{ver}-install"} if assets else {}
    return {"tag_name": f"v{ver}" + ("-beta.1" if pre else ""), "name": f"Mesh Manager {ver}", "draft": draft, "prerelease": pre, "published_at": "2026-09-03T08:00:00Z",
            "body": f"Notes for {ver}.", "assets": [{"name": n, "url": f"{API}/assets/{ids[n]}", "size": 10} for n in names]}


# ---- AC1 the check
FakeGitHub.releases = [rel("0.9.0", draft=True), rel("0.5.0", pre=False), rel("0.7.2", pre=True), rel("0.8.0", pre=True, assets=False), rel("0.6.0", pre=True, arch="arm64")]
state = tempfile.mkdtemp()
cfg = {"UPDATE_REPO": "MilUX-Ltd/mesh-manager", "UPDATE_CHANNEL": "prerelease", "UPDATE_MODE": "manual"}
r = U.check(cfg, token="t0ken", state_dir=state, arch="amd64", api=API, running="0.2.2")
check("AC1 the newest admitted release with the three assets", (r.get("version"), r.get("available"), r.get("tag")), ("0.7.2", True, "v0.7.2-beta.1"))
check_true("AC1 the check is recorded", os.path.exists(os.path.join(state, "updates", "check.json")) and json.load(open(os.path.join(state, "updates", "check.json"))).get("version") == "0.7.2")
r = U.check(dict(cfg, UPDATE_CHANNEL="stable"), token="t0ken", state_dir=state, arch="amd64", api=API, running="0.2.2")
check("AC1 the stable channel ignores pre-releases", (r.get("version"), r.get("available")), ("0.5.0", True))
r = U.check(cfg, token="t0ken", state_dir=state, arch="amd64", api=API, running="0.7.2")
check("AC1 not newer: not available", (r.get("version"), r.get("available")), ("0.7.2", False))
r = U.check(cfg, token="t0ken", state_dir=state, arch="amd64", api=API, running="0.9.5")
check("AC1 older than running: not available", r.get("available"), False)
n = len(calls)
r = U.check(cfg, token="", state_dir=state, arch="amd64", api=API, running="0.2.2")
check_true("AC1 without a token: the reason, and no call", "token" in str(r.get("error", "")).lower() and len(calls) == n)
r = U.check(cfg, token="t0ken", state_dir=state, arch="amd64", api="http://127.0.0.1:9/", running="0.2.2")
check_true("AC1 a GitHub error is recorded, not raised", bool(r.get("error")) and json.load(open(os.path.join(state, "updates", "check.json"))).get("error"))
check_true("AC1 the token travels as a bearer with the GitHub accept header", any(a == "Bearer t0ken" and "github" in (acc or "") for _p, a, acc in calls))

# ---- AC2 the download
r = U.check(cfg, token="t0ken", state_dir=state, arch="amd64", api=API, running="0.2.2")
d = U.download(r, token="t0ken", state_dir=state)
sd = os.path.join(state, "updates", "0.7.2")
check("AC2 the three files staged and READY written", (sorted(os.listdir(sd)), d.get("ready")), (["READY", "install.sh", "mesh-manager-0.7.2-amd64.tgz", "mesh-manager-0.7.2-amd64.tgz.sha256"], True))
check_true("AC2 READY names the tarball", open(os.path.join(sd, "READY")).read().strip().endswith("mesh-manager-0.7.2-amd64.tgz"))
FakeGitHub.releases = [rel("0.7.3", sha="0" * 64)]
r = U.check(cfg, token="t0ken", state_dir=state, arch="amd64", api=API, running="0.2.2")
d = U.download(r, token="t0ken", state_dir=state)
check_true("AC2 a wrong hash: no READY, the mismatch named", not os.path.exists(os.path.join(state, "updates", "0.7.3", "READY")) and "sha256" in str(d.get("error", "")))

# ---- AC3 apply and update.sh
started = []
a = U.apply(state, version="0.7.3", start_unit=lambda unit: started.append(unit) or 0)
check_true("AC3 apply refuses without READY", "READY" in str(a.get("error", "")) or "not staged" in str(a.get("error", "")).lower())
a = U.apply(state, version="0.7.2", start_unit=lambda unit: started.append(unit) or 0)
check("AC3 apply starts the update unit once READY exists", (a.get("started"), started), (True, ["mesh-manager-update.service"]))
out = subprocess.run(["bash", os.path.join(ROOT, "install", "update.sh"), "--dry-run"], capture_output=True, text=True, env=dict(os.environ, MESH_MANAGER_STATE=state)).stdout
check_true("AC3 update.sh finds the staging, re-checks the hash and runs the staged installer with no flags", "0.7.2" in out and "sha256 ok" in out and "install.sh" in out and "--" not in out.split("would run:")[-1].split("\n")[0].replace("--dry-run", ""))

# ---- AC4 the screen
fb = start_fake_bridge()
etc = tempfile.mkdtemp()
_maj, _min = __version__.split(".")[:2]; NEXT = f"{_maj}.{int(_min) + 1}.0"   # a release above whatever is running (0.9.1 stopped being one at 0.10.0)
FakeGitHub.releases = [rel(NEXT)]
srv = W.make_server(bind="127.0.0.1", port=0, socket_path=fb.path, etc_dir=etc, config=dict(cfg, UPDATE_API=API, UPDATE_MODE="manual", AUTH="off"), state_dir=state)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start(); time.sleep(0.3)


def req(method, path, body=None, ctype="application/x-www-form-urlencoded"):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    h = {"Content-Type": ctype} if body is not None else {}
    c.request(method, path, body=body, headers=h); r = c.getresponse(); data = r.read().decode(); c.close()
    return r.status, data


st, body = req("POST", "/settings/update", body="token=t0ken-from-the-page&mode=manual")
tok = os.path.join(etc, "github.token")
check_true("AC4 Settings stores the token at 0600", os.path.exists(tok) and stat.S_IMODE(os.stat(tok).st_mode) == 0o600 and open(tok).read().strip() == "t0ken-from-the-page")
st, page = req("GET", "/settings")
check_true("AC4 ...and never renders it", "t0ken-from-the-page" not in page and "github" in page.lower())
st, j = req("POST", "/api/update/check", body="{}", ctype="application/json")
check("AC4 Check now finds the release", (st, json.loads(j).get("version"), json.loads(j).get("available")), (200, NEXT, True))
st, about = req("GET", "/about")
check_true("AC4 About shows the running version, the update and the controls", __version__ in about and NEXT in about and f"Notes for {NEXT}" in about and "data-update-apply" in about and "data-update-check" in about)
check_true("AC4 the header carries the pill", "update available" in about)
srv.shutdown()
calls.clear()
srv = W.make_server(bind="127.0.0.1", port=0, socket_path=fb.path, etc_dir=etc, config=dict(cfg, UPDATE_API=API, UPDATE_MODE="off", AUTH="off"), state_dir=state)
threading.Thread(target=srv.serve_forever, daemon=True).start(); time.sleep(0.5)
check("AC4 UPDATE_MODE=off: the checker makes no call", len(calls), 0)
srv.shutdown()

# ---- AC5 the installer
root = tempfile.mkdtemp()
for d_ in ("opt/tak", "etc/systemd/system", "dev/serial/by-id"):
    os.makedirs(os.path.join(root, d_), exist_ok=True)
open(os.path.join(root, "dev/serial/by-id/usb-x-if00"), "w").close()
tf = os.path.join(root, "gh.token"); open(tf, "w").write("t0ken\n")
out = subprocess.run(["bash", os.path.join(ROOT, "install", "install.sh"), "/nonexistent.tgz", "--serial", "/dev/serial/by-id/usb-x-if00", "--filter-group", "MilUX",
                      "--github-token-file", tf, "--dry-run"], capture_output=True, text=True, env=dict(os.environ, MESH_MANAGER_ROOT=root)).stdout
check_true("AC5 the installer would install the update unit, its polkit rule, the updates directory and update.sh", "mesh-manager-update.service" in out and "polkit" in out and "updates" in out and "update.sh" in out)
check_true("AC5 ...and write the token at 0600", "github.token" in out and "0600" in out)

# ---- AC6 publishing
_pub = os.path.join(ROOT, "release", "publish-release.sh")
if os.path.exists(_pub):
    out = subprocess.run(["bash", os.path.join(ROOT, "release", "publish-release.sh"), "--check", "0.7.2"], capture_output=True, text=True, cwd=ROOT).stdout
    check_true("AC6 publish-release --check names the tag, the assets and the notes", "v0.7.2" in out and "mesh-manager-0.7.2-amd64.tgz" in out and "install.sh" in out and "notes" in out.lower())
else:
    skip("AC6 publish-release --check", "the release tooling is not in this tree")
gh.shutdown()

# 4 Sep 2026: the repository name lived in two places, and renaming one left boxes checking the
# old name. There is one default now; this is the check that they cannot drift apart again.
from mesh_manager.common import read_config  # noqa: E402
_conf = read_config("/nonexistent")
check("the update repository has one default, and it is the public repository",
      (_conf.get("UPDATE_REPO"), U.DEFAULT_REPO, str(_conf.get("UPDATE_REPO") or U.DEFAULT_REPO)),
      ("", "MilUX-Ltd/mesh-manager", "MilUX-Ltd/mesh-manager"))


# 0.16.1: the Update button asks with the screen's confirm dialog, whose script must travel with About
_about_src = read("src/mesh_manager/web.py") or ""
_ab = _about_src[_about_src.find("def about_body("):_about_src.find("def about_body(") + 600]
check_true("About carries the confirm dialog's script (0.16.1)", "{WRITE_JS}" in _ab and "var ask=window.mmConfirm||function" in _about_src)

finish()
