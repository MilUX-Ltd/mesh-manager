#!/usr/bin/env python3
"""Spec 030: roll back to a release the box already has on disk."""
import hashlib
import http.client
import json
import os
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(__file__))
from _common import ROOT, check, check_true, finish  # noqa: E402
sys.path.insert(0, os.path.join(ROOT, "src"))
from fakebridge_lib import start_fake_bridge  # noqa: E402
from mesh_manager import __version__, catalogue as C, updates as U, web as W  # noqa: E402

ARCH = "amd64"


def stage(state, ver, body=b"tarball for a release", complete=True, sha=None, ready=False):
    """Put a release's artefacts on disk the way download() leaves them after an apply."""
    d = os.path.join(state, "updates", ver)
    os.makedirs(d, exist_ok=True)
    tgz = os.path.join(d, f"mesh-manager-{ver}-{ARCH}.tgz")
    with open(tgz, "wb") as fh:
        fh.write(body)
    digest = sha or hashlib.sha256(body).hexdigest()
    with open(tgz + ".sha256", "w") as fh:
        fh.write(f"{digest}  {os.path.basename(tgz)}\n")
    if complete:
        with open(os.path.join(d, "install.sh"), "w") as fh:
            fh.write("#!/bin/sh\necho staged installer $1\n")
        os.chmod(os.path.join(d, "install.sh"), 0o755)
    if ready:
        with open(os.path.join(d, "READY"), "w") as fh:
            fh.write(tgz + "\n")
    return d, tgz


# ---- AC1 what the box could return to -----------------------------------------------------------
state = tempfile.mkdtemp()
stage(state, "0.3.9"); stage(state, "0.4.0"); stage(state, "0.4.1")
stage(state, "0.4.2", complete=False)                      # no installer: not a candidate
os.makedirs(os.path.join(state, "updates", "notaversion"), exist_ok=True)
rows = U.staged(state, arch=ARCH, running="0.4.1")
vers = [r["version"] for r in rows]
check("AC1 every complete staging, newest first", vers, ["0.4.1", "0.4.0", "0.3.9"])
check_true("AC1 an incomplete staging is not offered", "0.4.2" not in vers)
check_true("AC1 the running one is marked and the others are not",
           [r.get("running") for r in rows] == [True, False, False])
check_true("AC1 each row carries its size and when it was staged",
           all(isinstance(r.get("size"), int) and r.get("size") > 0 and r.get("staged") for r in rows))

# ---- AC2 the roll back itself -------------------------------------------------------------------
started = []
r = U.rollback(state, "0.4.0", running="0.4.1", start_unit=lambda u: started.append(u) or 0)
ready = os.path.join(state, "updates", "0.4.0", "READY")
check("AC2 the unit is started for the chosen version", (r.get("started"), r.get("version"), started),
      (True, "0.4.0", ["mesh-manager-update.service"]))
check_true("AC2 READY names that version's tarball",
           os.path.exists(ready) and open(ready).read().strip().endswith("mesh-manager-0.4.0-amd64.tgz"))

# update.sh takes the newest READY by modification time, which the fresh marker is.
stage(state, "0.4.3", ready=True)
os.utime(os.path.join(state, "updates", "0.4.3", "READY"), (1, 1))   # an older, stale marker
U.rollback(state, "0.3.9", running="0.4.1", start_unit=lambda u: 0)
out = subprocess.run(["bash", os.path.join(ROOT, "install", "update.sh"), "--dry-run"],
                     capture_output=True, text=True, env=dict(os.environ, MESH_MANAGER_STATE=state)).stdout
check_true("AC2 update.sh picks the roll-back marker, not an older staged one", "0.3.9" in out and "0.4.3" not in out)

# ---- AC3 a tarball that no longer matches its hash -----------------------------------------------
bad = tempfile.mkdtemp()
stage(bad, "0.4.0", body=b"the bytes on disk", sha=hashlib.sha256(b"what was published").hexdigest())
started2 = []
r = U.rollback(bad, "0.4.0", running="0.4.1", start_unit=lambda u: started2.append(u) or 0)
check_true("AC3 a hash that no longer matches refuses, says so, and starts nothing",
           "sha256" in str(r.get("error", "")).lower() and not started2
           and not os.path.exists(os.path.join(bad, "updates", "0.4.0", "READY")))

# ---- AC4 the refusals ---------------------------------------------------------------------------
r = U.rollback(state, "0.4.1", running="0.4.1", start_unit=lambda u: 0)
check_true("AC4 the running version is refused with a reason", "running" in str(r.get("error", "")).lower())
r = U.rollback(state, "9.9.9", running="0.4.1", start_unit=lambda u: 0)
check_true("AC4 a version the box has not got is refused with a reason",
           bool(r.get("error")) and "9.9.9" in str(r.get("error")))
r = U.rollback(state, "", running="0.4.1", start_unit=lambda u: 0)
check_true("AC4 no version at all is refused", bool(r.get("error")))

# ---- AC7 the automatic checker would roll forward again ------------------------------------------
r = U.rollback(state, "0.4.0", running="0.4.1", mode="auto", start_unit=lambda u: 0)
check_true("AC7 in auto mode the answer warns that the checker will roll forward",
           "auto" in str(r.get("warning", "")).lower())
r = U.rollback(state, "0.4.0", running="0.4.1", mode="manual", start_unit=lambda u: 0)
check_true("AC7 in manual mode there is no such warning", not r.get("warning"))

# ---- AC6 the catalogue --------------------------------------------------------------------------
ids = {a["id"]: a for a in C.ACTIONS}
check_true("AC6 update_staged is a read", ids.get("update_staged", {}).get("risk") == "read")
check_true("AC6 update_rollback is a change", ids.get("update_rollback", {}).get("risk") == "change")
check_true("AC6 update_rollback takes the version to return to",
           any(i.get("name") == "version" for i in ids.get("update_rollback", {}).get("inputs", [])))

# ---- AC5 and AC6 on the screen -------------------------------------------------------------------
fb = start_fake_bridge()
etc = tempfile.mkdtemp()
srv = W.make_server(bind="127.0.0.1", port=0, socket_path=fb.path, etc_dir=etc,
                    config={"UPDATE_MODE": "manual", "AUTH": "off"}, state_dir=state)
srv.web.start_unit = lambda unit: 0        # there is no systemd in the suite
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
time.sleep(0.3)


def req(method, path, body=None, ctype="application/x-www-form-urlencoded"):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    h = {"Content-Type": ctype} if body is not None else {}
    c.request(method, path, body=body, headers=h)
    r = c.getresponse()
    data = r.read().decode()
    c.close()
    return r.status, data


with open(os.path.join(state, "updates", "last.log"), "w") as fh:
    fh.write("== 2026-09-04T10:00:00Z applying 0.4.1\nthe installer said something\n")
st, body = req("GET", "/about")
check("AC5 About answers", st, 200)
check_true("AC5 About offers the versions the box could return to", "0.4.0" in body and "0.3.9" in body)
check_true("AC5 About shows the last update log", "the installer said something" in body)
check_true("AC5 the press names roll back", "roll back" in body.lower() or "rollback" in body.lower())
check_true("AC5 it says a roll back returns the code and not the config",
           "config" in body.lower() and ("not" in body.lower() or "keeps" in body.lower()))

st, body = req("POST", "/api/update/rollback", body=json.dumps({"version": "0.4.0"}), ctype="application/json")
check("AC6 the API rolls back", (st, json.loads(body).get("version")), (200, "0.4.0"))
st, body = req("POST", "/api/update/rollback", body=json.dumps({"version": "9.9.9"}), ctype="application/json")
check_true("AC6 the API refuses a version the box has not got", bool(json.loads(body).get("error")))
st, body = req("POST", "/api/update/staged", body="{}", ctype="application/json")
check_true("AC6 the API lists what is staged", "0.4.0" in body)

# nothing staged at all
empty = tempfile.mkdtemp()
srv2 = W.make_server(bind="127.0.0.1", port=0, socket_path=fb.path, etc_dir=tempfile.mkdtemp(),
                     config={"UPDATE_MODE": "manual", "AUTH": "off"}, state_dir=empty)
port2 = srv2.server_address[1]
threading.Thread(target=srv2.serve_forever, daemon=True).start()
time.sleep(0.3)
c = http.client.HTTPConnection("127.0.0.1", port2, timeout=10)
c.request("GET", "/about")
body2 = c.getresponse().read().decode()
c.close()
check_true("AC5 with nothing to return to it says so and offers no press",
           "nothing to roll back to" in body2.lower())

# ---- the last check is read against the version running now, not the one that was running then --
# On the kit, About offered "Update now to 0.5.0" while running 0.5.0, because availability was
# decided when the check ran and never reconsidered.
rec = {"checked": "2026-09-04T16:09:00Z", "version": __version__, "available": True, "channel": "prerelease"}
check_true("a check that named the version now running is not still available",
           U.is_available(rec, running=__version__) is False)
check_true("a check that named a newer version still is",
           U.is_available({"version": "99.0.0", "available": True}, running=__version__) is True)
check_true("a check that errored is not available", U.is_available({"error": "no"}, running="0.1.0") is False)

# ---- staged releases do not grow without limit -------------------------------------------------
many = tempfile.mkdtemp()
for v in ("0.2.5", "0.2.6", "0.2.7", "0.3.7", "0.3.8", "0.4.1", "0.4.2", "0.4.3", "0.4.4", "0.5.0"):
    stage(many, v)
kept = U.prune_staged(many, keep=4, running="0.5.0", arch=ARCH)
left = sorted(r["version"] for r in U.staged(many, arch=ARCH, running="0.5.0"))
check("the newest few are kept, the rest removed", left, ["0.4.2", "0.4.3", "0.4.4", "0.5.0"])
check_true("the running version is never removed", "0.5.0" in left and kept["removed"])
check_true("pruning a directory it cannot read raises nothing",
           isinstance(U.prune_staged("/nonexistent-state", keep=4, running="0.5.0"), dict))

finish()
