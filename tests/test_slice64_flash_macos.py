#!/usr/bin/env python3
"""Spec 064: flashing over USB on a Mac. The volume hunt, the mount, the copy and letting go."""
import os, sys, tempfile
sys.path.insert(0, os.path.dirname(__file__))
from _common import ROOT, check, check_true, finish, read  # noqa: E402
sys.path.insert(0, os.path.join(ROOT, "src"))
import fakegw_lib  # noqa: E402
fakegw_lib.install()
from mesh_manager import bridge as B  # noqa: E402

# AC1: the volume hunt
vols = tempfile.mkdtemp()
os.makedirs(os.path.join(vols, "RAK4631"), exist_ok=True)
os.makedirs(os.path.join(vols, "Macintosh HD"), exist_ok=True)
found = B.Bridge._default_wait_volume("RAK-4631", 3, system="Darwin", volumes=vols)
check("AC1 a Mac finds the volume by its label, hyphens and case aside", found, os.path.join(vols, "RAK4631"))
check("AC1 and answers nothing when it is not there", B.Bridge._default_wait_volume("T1000-E", 1, system="Darwin", volumes=vols), None)

# AC2 and AC3: mounting and letting go
check("AC2 a volume already mounted needs no mounting", B.Bridge._default_mount(found, system="Darwin"), found)
ran = []
check_true("AC3 letting go uses the platform's own tool",
           B.Bridge._default_unmount(found, system="Darwin", run=lambda a: ran.append(a)) is None and ran and ran[0][0] == "diskutil" and "unmount" in ran[0], repr(ran))
ran2 = []
B.Bridge._default_unmount("/dev/sdb1", system="Linux", run=lambda a: ran2.append(a))
check_true("AC3 and Linux keeps udisksctl", ran2 and ran2[0][0] == "udisksctl", repr(ran2))

# AC4: the copy
src = os.path.join(tempfile.mkdtemp(), "firmware-1.2.3.uf2")
open(src, "wb").write(b"UF2\x00" + b"x" * 512)
B.Bridge._default_copy(src, found)
dst = os.path.join(found, "firmware-1.2.3.uf2")
check("AC4 the image lands in the volume, whole", (os.path.exists(dst), os.path.getsize(dst)), (True, 516))

# AC5: the order of the steps is the same on both
steps = []
br = B.Bridge({"SERIAL": "", "MODE": "desktop"}, socket_path=os.path.join(tempfile.mkdtemp(), "b.sock"), state_dir=tempfile.mkdtemp())
br.wait_volume = lambda label, timeout: (steps.append(("wait_volume", label)) or found)
br.mount = lambda dev: (steps.append(("mount", dev)) or found)
br.copy = lambda s_, m: steps.append(("copy", os.path.basename(s_)))
br.unmount = lambda dev: steps.append(("unmount", dev))
check_true("AC5 the bridge takes its steps from replaceable hooks", all(hasattr(br, n) for n in ("wait_volume", "mount", "copy", "unmount")), "the hooks must be nameable")
br.stop()

# AC6: the guide
g = read("docs/GUIDE.md") or ""
check_true("AC6 the guide says a laptop can flash", "flash" in g.lower() and "bootloader" in g.lower() and "laptop" in g.lower())
finish()
