#!/usr/bin/env python3
"""Spec 001 AC3: the installer, dry-run against a fake root, adopts an earlier gateway install and
never touches the firewall."""
import os
import re
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(__file__))
from _common import ROOT, check, check_true, finish, read  # noqa: E402

S = os.path.join(ROOT, "install", "install.sh")
present = os.path.exists(S)
check_true("install/install.sh present", present)


def fake_root(with_old):
    root = tempfile.mkdtemp()
    for d in ("etc/systemd/system", "opt", "var/lib", "opt/tak"):
        os.makedirs(os.path.join(root, d), exist_ok=True)
    open(os.path.join(root, "opt/tak/CoreConfig.xml"), "w").write(
        "<Configuration><network>\n        <input _name=\"meshtastic\" protocol=\"mcast\" port=\"6970\" group=\"239.2.3.1\">"
        "<filtergroup>mesh</filtergroup></input>\n</network></Configuration>\n")
    if with_old:
        open(os.path.join(root, "etc/vantage-mesh.conf"), "w").write(
            "# written by vantage-mesh-gateway-install\nSERIAL=/dev/serial/by-id/usb-Espressif_USB_JTAG_serial_debug_unit_A4:CB:8F:EE:00:01-if00\n"
            "REGION=EU_868\nCHANNEL=MILUX-TAK\nFILTER_GROUP=mesh\nEXTRA_ARGS=\n")
        open(os.path.join(root, "etc/systemd/system/tak-meshtastic-gateway.service"), "w").write(
            "[Unit]\nDescription=Meshtastic TAK gateway (Vantage Networks)\n")
    return root


def run(root, *args):
    return subprocess.run(["bash", S, "/nonexistent/mesh-manager-1.1.0+milux.3-amd64.tgz", "--dry-run", *args],
                          capture_output=True, text=True,
                          env={**os.environ, "MESH_MANAGER_ROOT": root})


if present:
    r = subprocess.run(["bash", "-n", S], capture_output=True, text=True)
    check("installer parses as bash", r.returncode, 0)
    text = read("install/install.sh") or ""
    check("installer never invokes a firewall tool",
          sorted(set(re.findall(r"\b(ufw|iptables|nft)\b", text))), [])
    root = fake_root(with_old=True)
    a = run(root)
    out = a.stdout + a.stderr
    check("dry run against an old install exits 0", a.returncode, 0)
    for want in ("adopting /etc/vantage-mesh.conf",
                 "SERIAL=/dev/serial/by-id/usb-Espressif_USB_JTAG_serial_debug_unit_A4:CB:8F:EE:00:01-if00",
                 "FILTER_GROUP=mesh",
                 "stop and disable tak-meshtastic-gateway",
                 "keeping /etc/systemd/system/tak-meshtastic-gateway.service as the rollback",
                 "create /var/lib/vantage-mesh",
                 "install mesh-manager-bridge.service",
                 "install mesh-manager-web.service",
                 "create user mesh-manager",
                 "generate an operator password and show it once",
                 "input 'meshtastic' already present; leaving it untouched",
                 "127.0.0.1"):
        check_true(f"dry run says: {want}", want in out)
    check_true("dry run ends by saying what is closed",
               re.search(r"closed", out.strip().splitlines()[-1] if out.strip() else "", re.I) is not None)
    # the second run: the box now carries exactly what the first run would have produced
    os.makedirs(os.path.join(root, "etc/mesh-manager"), exist_ok=True)
    open(os.path.join(root, "etc/mesh-manager/config"), "w").write(
        "# written by mesh-manager install.sh\nSERIAL=/dev/serial/by-id/usb-Espressif_USB_JTAG_serial_debug_unit_A4:CB:8F:EE:00:01-if00\n"
        "REGION=EU_868\nCHANNEL=MILUX-TAK\nFILTER_GROUP=mesh\nEXTRA_ARGS=\nBIND=127.0.0.1\nPORT=8093\nAUTH=on\nUPDATE_REPO=MilUX-Ltd/mesh-manager\nUPDATE_MODE=manual\nUPDATE_CHANNEL=prerelease\n")
    open(os.path.join(root, "etc/mesh-manager/adopted-from-vantage-mesh"), "w").write("2026-09-03T02:00:00Z\n")
    open(os.path.join(root, "etc/systemd/system/mesh-manager-bridge.service"), "w").write("[Unit]\nDescription=Mesh Manager bridge\n")
    open(os.path.join(root, "etc/systemd/system/mesh-manager-web.service"), "w").write("[Unit]\nDescription=Mesh Manager screen\n")
    open(os.path.join(root, "etc/mesh-manager/passwd"), "w").write("pbkdf2_sha256$1$00$00\n")
    os.makedirs(os.path.join(root, "opt/mesh-manager/venv/bin"), exist_ok=True)
    ep = os.path.join(root, "opt/mesh-manager/venv/bin/tak-meshtastic-gateway")
    open(ep, "w").write("#!/bin/sh\n"); os.chmod(ep, 0o755)
    os.makedirs(os.path.join(root, "opt/mesh-manager/release"), exist_ok=True)
    open(os.path.join(root, "opt/mesh-manager/release/.tarball.sha256"), "w").write("0" * 64 + "\n")   # the release the box carries
    b = run(root)
    check("second dry run exits 0", b.returncode, 0)
    check_true("second dry run reports nothing to change", "nothing to change" in (b.stdout + b.stderr))
    na = run(fake_root(with_old=True), "--no-auth")
    check_true("--no-auth: the dry run says sign-in is off and generates no password",
               na.returncode == 0 and "sign-in off" in (na.stdout + na.stderr) and "generate an operator password" not in (na.stdout + na.stderr))
    bare = fake_root(with_old=False)
    c = run(bare)
    check_true("no old install and no flags: refuses", c.returncode != 0)
    check_true("...and names the two flags it needs",
               "--serial" in (c.stdout + c.stderr) and "--filter-group" in (c.stdout + c.stderr))

# The gateway patch is a whole-file diff against upstream's own bytes, and upstream's file uses
# CRLF. Reading and writing the patch in text mode converts them to LF, the patch stops applying,
# and no other suite notices because the suites read the plus side only. 4 Sep 2026: that is
# exactly what happened while rewording a comment in the header. The count is the guard.
import pathlib  # noqa: E402
_gw = pathlib.Path(ROOT, "bridge", "patches", "gateway-01-tak_meshtastic_gateway.patch").read_bytes()
check("the gateway patch keeps upstream's CRLF endings (edit it in binary, never text mode)",
      (_gw.count(b"\r\n") > 600, _gw.count(b"\n") - _gw.count(b"\r\n") > 0),
      (True, True))

finish()
