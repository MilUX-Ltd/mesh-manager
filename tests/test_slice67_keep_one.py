#!/usr/bin/env python3
"""Spec 067: one release to roll back to, not five. What is kept, what is removed, and what the card says."""
import os, sys, tempfile
sys.path.insert(0, os.path.dirname(__file__))
from _common import ROOT, check, check_true, finish, read  # noqa: E402
sys.path.insert(0, os.path.join(ROOT, "src"))
from mesh_manager import updates as U  # noqa: E402

ARCH = "amd64"


def stage(root, ver):
    d = os.path.join(root, "updates", ver)
    os.makedirs(d, exist_ok=True)
    tgz = os.path.join(d, f"mesh-manager-{ver}-{ARCH}.tgz")
    open(tgz, "wb").write(b"x" * 32)
    open(tgz + ".sha256", "w").write("0" * 64 + f"  {os.path.basename(tgz)}\n")
    open(os.path.join(d, "install.sh"), "w").write("#!/bin/sh\n")
    return d


def left(root, running):
    return sorted(r["version"] for r in U.staged(root, arch=ARCH, running=running))


# AC1 and AC2: the default keeps the running release and one to go back to
a = tempfile.mkdtemp()
for v in ("0.13.0", "0.16.1", "0.17.1", "0.17.2", "0.18.0"):
    stage(a, v)
out = U.prune_staged(a, running="0.18.0", arch=ARCH)
check("AC1 the running release and the one below it are what is left", left(a, "0.18.0"), ["0.17.2", "0.18.0"])
check("AC1 and the rest are named as removed", sorted(out["removed"]), ["0.13.0", "0.16.1", "0.17.1"])
check("AC2 two release directories on disk and no more", len(os.listdir(os.path.join(a, "updates"))), 2)
check_true("AC2 something was freed", out["freed"] > 0, str(out["freed"]))

# AC2 again: keep counts releases to go back to, not rows
b = tempfile.mkdtemp()
for v in ("0.20.0", "0.21.0", "0.22.0", "0.23.0"):
    stage(b, v)
U.prune_staged(b, keep=2, running="0.23.0", arch=ARCH)
check("AC2 keep=2 leaves two to go back to, beside the running one", left(b, "0.23.0"), ["0.21.0", "0.22.0", "0.23.0"])

# AC3: the running release survives anything
c = tempfile.mkdtemp()
for v in ("0.24.0", "0.25.0"):
    stage(c, v)
U.prune_staged(c, keep=0, running="0.25.0", arch=ARCH)
check("AC3 keep=0 leaves the running release alone", left(c, "0.25.0"), ["0.25.0"])
d = tempfile.mkdtemp()
stage(d, "0.25.0")
U.prune_staged(d, running="0.25.0", arch=ARCH)
check("AC3 a box with only the running release keeps it", left(d, "0.25.0"), ["0.25.0"])
check_true("AC3 a state directory that is not there raises nothing",
           isinstance(U.prune_staged("/nonexistent-state-067", running="0.5.0"), dict))

# AC4 and AC5: the screen
src = read("src/mesh_manager/web.py") or ""
check_true("AC4 the box tidies its staged releases when the screen starts", "prune_staged" in src and "PRUNE_ON_START" in src)
check_true("AC5 the card no longer promises five", "The five most recent are kept" not in src)
check_true("AC5 and says one is kept", "one release" in src.lower() and "way back" in src.lower())
finish()
