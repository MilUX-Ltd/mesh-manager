#!/usr/bin/env python3
"""Spec 099: the installer names the radios it can see.

Installing needs --serial /dev/serial/by-id/<path> and nothing in the product ever said how to turn
a radio you just plugged in into that string. The README printed a placeholder, the installer's
refusal printed the same placeholder back, and the guide started a step later than the operator did.

The installer is running as root on the box with the radio plugged into it. It refuses, correctly,
because it must not guess which of several radios is the gateway. It should refuse with the list in
its hand.

MESH_MANAGER_ROOT lets this suite give the installer a /dev/serial/by-id of its own, so the checks
are about what it says, not about what happens to be plugged into this machine.
"""
import os, re, subprocess, sys, tempfile
sys.path.insert(0, os.path.dirname(__file__))
from _common import ROOT, check, check_true, finish, read  # noqa: E402

INSTALL = os.path.join(ROOT, "install", "install.sh")


def box(entries=()):
    """A fake box with a /dev/serial/by-id holding the names given."""
    root = tempfile.mkdtemp()
    for d in ("opt/tak", "etc/systemd/system", "etc/mesh-manager", "dev/serial/by-id"):
        os.makedirs(os.path.join(root, d), exist_ok=True)
    for nm in entries:
        open(os.path.join(root, "dev/serial/by-id", nm), "w").close()
    return root


def run(root, *args):
    """--dry-run throughout, which is how every suite drives this installer and the only way to
    reach the --serial check without being root: the root check comes first and would otherwise be
    the refusal under test. A person running it with sudo reaches exactly the same block."""
    if "--dry-run" not in args:
        args = (*args, "--dry-run")
    r = subprocess.run(["bash", INSTALL, "/nonexistent.tgz", *args],
                       capture_output=True, text=True, env=dict(os.environ, MESH_MANAGER_ROOT=root))
    return r.returncode, r.stdout + r.stderr


RADIO = "usb-Espressif_USB_JTAG_serial_debug_unit_A4:CB:8F:EE:00:01-if00"
RADIO2 = "usb-1a86_USB_Single_Serial_58A3097418-if00"
GPS = "usb-u-blox_AG_-_www.u-blox.com_u-blox_GNSS_receiver-if00"

# ---- AC1, AC2 what a refusal says when it can see the answer ---------------------------------------
rc, out = run(box([RADIO, GPS]), "--mode", "server")
check_true("AC1 the installer lists what it can see", "/dev/serial/by-id/" + RADIO in out,
           out[-600:] or "(no output)")
check_true("AC1 the path is whole, so it can be copied into the command",
           re.search(r"/dev/serial/by-id/" + re.escape(RADIO) + r"(\s|$)", out) is not None, out[-400:])
check_true("AC2 a GPS receiver is named as one, not offered as a gateway",
           re.search(r"(gps|gnss|receiver)", out, re.I) is not None, out[-400:])
_gps_line = [l for l in out.splitlines() if GPS in l]
check_true("AC2 and the GPS line says so on the line itself",
           bool(_gps_line) and re.search(r"(gps|gnss|receiver|not a radio)", _gps_line[0], re.I) is not None,
           (_gps_line or ["the GPS is not listed at all"])[0])

# ---- AC4 listing is help, not permission ------------------------------------------------------------
check("AC4 it still refuses", rc != 0, True)
check_true("AC4 and nothing was installed", "STAGE-OK install" not in out, out[-200:])
check_true("AC4 and it stopped at the refusal rather than carrying on",
           "write /etc/mesh-manager/config" not in out, out[-300:])

# ---- AC5 the next command, ready to run --------------------------------------------------------------
rc1, out1 = run(box([RADIO]), "--mode", "server")
check_true("AC5 with one candidate it prints the whole command to run next",
           "--serial /dev/serial/by-id/" + RADIO in out1, out1[-500:])
check_true("AC5 and that command names the tarball, not a placeholder",
           re.search(r"install\.sh\s+\S*\.tgz", out1) is not None or "install.sh" in out1, out1[-300:])

# ---- AC3 nothing plugged in ----------------------------------------------------------------------------
rc2, out2 = run(box([]), "--mode", "server")
check("AC3 it still refuses with nothing plugged in", rc2 != 0, True)
check_true("AC3 and says so plainly rather than printing an empty list",
           re.search(r"(nothing|no radio|none|not see any)", out2, re.I) is not None, out2[-400:])
check_true("AC3 and names a likely reason",
           re.search(r"(plug|cable|power|usb)", out2, re.I) is not None, out2[-400:])

# ---- AC6 an install that passes --serial is untouched -----------------------------------------------------
root = box([RADIO])
rc3, out3 = run(root, "--serial", "/dev/serial/by-id/" + RADIO, "--mode", "server", "--dry-run")
check("AC6 a dry run with --serial still succeeds", rc3, 0)
check_true("AC6 and says nothing about choosing a radio",
           not re.search(r"radios this box can see|which radio", out3, re.I), out3[-300:])

# ---- AC7 a shape that needs no radio is untouched -----------------------------------------------------------
rc4, out4 = run(box([RADIO]), "--mode", "hub", "--site-address", "hub.example", "--dry-run")
check("AC7 a hub installs with no radio and no complaint", rc4, 0)
check_true("AC7 and is not shown a list it has no use for",
           not re.search(r"radios this box can see", out4, re.I), out4[-300:])

# ---- AC10 the words do not assume a Linux background -----------------------------------------------------------
_jargon = [w for w in ("udev", "tty device", "sysfs", "devfs") if re.search(rf"\b{w}\b", out, re.I)]
check("AC10 no jargon a new operator would not know", _jargon, [])
check_true("AC10 and the list is introduced in words", re.search(r"(radio|device)s? (this box|I) can see", out, re.I) is not None,
           out[-400:])

# ---- AC8, AC9 the documentation ----------------------------------------------------------------------------------
readme = read("README.md") or ""
_find = re.search(r"ls\s+(-\w+\s+)?/dev/serial/by-id", readme)
check_true("AC8 the README shows how to find the path", _find is not None, "no find command in the README")
if _find:
    _install_cmd = readme.find("--serial /dev/serial/by-id/<your radio>")
    check_true("AC8 and shows it before the command that needs it",
               _find.start() < _install_cmd if _install_cmd > 0 else True,
               f"find at {_find.start()}, install command at {_install_cmd}")

guide = read("docs/GUIDE.md") or ""
_setup = guide[guide.find("## Setting up"):guide.find("### A box without TAK")]
check_true("AC9 the guide's Setting up begins where the operator begins",
           re.search(r"plug", _setup, re.I) is not None, _setup[:200])
check_true("AC9 and tells them how to find it",
           "by-id" in _setup or "find" in _setup.lower(), _setup[:200])

finish()
