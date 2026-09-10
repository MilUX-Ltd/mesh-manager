#!/usr/bin/env python3
"""Spec 078: a tidy that cannot tidy says so.

Matt, looking at seven releases on a box under a card that says one is kept: "No need to keep so
many releases. Just need to keep the last one for rollback."

The rule was already right and prune_staged was already correct. It was failing and saying nothing:
the staging directories on that box belong to root, because they were written by update.sh, which
runs as root. The screen runs as the service user and cannot remove them, and the OSError was
swallowed.
"""
import os, stat, sys, tempfile
sys.path.insert(0, os.path.dirname(__file__))
from _common import ROOT, check, check_true, finish, read  # noqa: E402
sys.path.insert(0, os.path.join(ROOT, "src"))
from mesh_manager import updates as U  # noqa: E402

ARCH = "amd64"


def stage(root, ver):
    d = os.path.join(root, "updates", ver)
    os.makedirs(d, exist_ok=True)
    tgz = os.path.join(d, f"mesh-manager-{ver}-{ARCH}.tgz")
    open(tgz, "wb").write(b"x" * 64)
    open(tgz + ".sha256", "w").write("0" * 64 + f"  {os.path.basename(tgz)}\n")
    open(os.path.join(d, "install.sh"), "w").write("#!/bin/sh\n")
    return d


# AC1 the ordinary case still keeps the running release and one to go back to
a = tempfile.mkdtemp()
for v in ("0.28.0", "0.29.0", "1.0.0", "1.0.1"):
    stage(a, v)
out = U.prune_staged(a, running="1.0.1", arch=ARCH)
check("AC1 two directories are left", len(os.listdir(os.path.join(a, "updates"))), 2)
check("AC1 and it reports nothing failed", out.get("failed"), [])

# AC2 a directory it cannot remove is reported, not swallowed
b = tempfile.mkdtemp()
for v in ("0.13.0", "0.28.0", "1.0.0", "1.0.1"):
    stage(b, v)
locked = os.path.join(b, "updates", "0.13.0")
os.chmod(locked, stat.S_IRUSR | stat.S_IXUSR)          # readable, not writable: removal will fail
try:
    out = U.prune_staged(b, running="1.0.1", arch=ARCH)
    failed = out.get("failed") or []
    check_true("AC2 the failure is reported", len(failed) == 1, repr(failed)[:160])
    check("AC2 and it names the version", (failed[0] if failed else {}).get("version"), "0.13.0")
    check_true("AC2 and says why", bool((failed[0] if failed else {}).get("why")),
               repr((failed[0] if failed else {}).get("why")))
    check_true("AC2 the ones it could remove still went", "0.28.0" not in os.listdir(os.path.join(b, "updates")))
finally:
    os.chmod(locked, stat.S_IRWXU)

# AC5 a box whose cut is built for another Python still has a way back, and still tidies.
# This is how edge looked: every tarball named -amd64-py314.tgz, so staged() found nothing, the
# Roll back card was empty, and seven releases sat there with nothing to remove them.
def stage_py314(root, ver):
    d = os.path.join(root, "updates", ver)
    os.makedirs(d, exist_ok=True)
    tgz = os.path.join(d, f"mesh-manager-{ver}-{ARCH}-py314.tgz")
    open(tgz, "wb").write(b"x" * 64)
    open(tgz + ".sha256", "w").write("0" * 64 + f"  {os.path.basename(tgz)}\n")
    open(os.path.join(d, "install.sh"), "w").write("#!/bin/sh\n")

c = tempfile.mkdtemp()
for v in ("0.28.0", "0.29.0", "1.0.0", "1.0.1"):
    stage_py314(c, v)
rows = U.staged(c, arch=ARCH, running="1.0.1")
check("AC5 a py314 box can see what it could return to", len(rows), 4)
check_true("AC5 and knows which one it is running",
           [r["version"] for r in rows if r.get("running")] == ["1.0.1"])
U.prune_staged(c, running="1.0.1", arch=ARCH)
check("AC5 and the tidy works there too", len(os.listdir(os.path.join(c, "updates"))), 2)

# AC3 the screen says it, rather than claiming one is kept while seven sit there
web = read("src/mesh_manager/web.py") or ""
check_true("AC3 the start-up tidy keeps its result", "prune_failed" in web)
check_true("AC3 the Roll back card shows it", "The tidy could not remove" in web)
check_true("AC3 and explains the cause an operator can act on", "belongs to root" in web)

# AC4 the cause is removed: update.sh runs as root, so it hands the tree back
up = read("install/update.sh") or ""
check_true("AC4 update.sh hands the staging tree to the service user",
           "chown -R mesh-manager:mesh-manager" in up and '"$STATE/updates"' in up)
check_true("AC4 only after a successful apply", "(( rc == 0 )) && id -u mesh-manager" in up)

finish()
