#!/usr/bin/env python3
"""Spec 001 AC2: the release cut, checked with no network."""
import os
import re
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(__file__))
from _common import ROOT, check, check_true, finish, read, skip  # noqa: E402

S = os.path.join(ROOT, "release", "cut-release.sh")
if not os.path.exists(S):
    skip("release/cut-release.sh", "the release tooling is not in this tree; this suite runs in the source repository")
    finish()
present = os.path.exists(S)
check_true("release/cut-release.sh present", present)
if present:
    r = subprocess.run(["bash", "-n", S], capture_output=True, text=True)
    check("cut script parses as bash", r.returncode, 0)
    text = read("release/cut-release.sh") or ""
    check_true("output is named mesh-manager-<version>-<arch>.tgz (the product's version; the gateway pin is in the manifest)",
               'mesh-manager-$(cat "$REPO/VERSION")-${ARCH}.tgz' in text)
    check_true("writes RELEASE.json", "RELEASE.json" in text)
    check_true("writes LICENSES/", "LICENSES" in text)
    check_true("the release carries its own installer", 'cp "$REPO/install/install.sh" "$B/install.sh"' in text)
    check_true("--arch accepts amd64 and arm64 only",
               re.search(r'\^\(amd64\|arm64\)\$', text) is not None)
    chk = subprocess.run(["bash", S, "--check"], capture_output=True, text=True, cwd=ROOT,
                         env={**os.environ, "MESH_MANAGER_OFFLINE": "1"})
    check("--check exits 0 with no network", chk.returncode, 0)
    out = chk.stdout + chk.stderr
    for want in ("TAK-Meshtastic-Gateway==1.1.0", "meshtastic==2.7.11", "zstandard==0.25.0",
                 "gateway-01-tak_meshtastic_gateway.patch", "gateway-02-pyproject.patch",
                 "sitepkg-02-atak_pb2.patch", "dict_non_aircraft.zstd", "dict_aircraft.zstd",
                 "3e85d9354111e809c8600c94040b81d25ddb51f3"):
        check_true(f"--check prints {want}", want in out)
    # a patch stripped of its provenance header must fail the check
    tmp = tempfile.mkdtemp()
    try:
        shutil.copytree(os.path.join(ROOT, "bridge"), os.path.join(tmp, "bridge"))
        shutil.copytree(os.path.join(ROOT, "release"), os.path.join(tmp, "release"))
        pp = os.path.join(tmp, "bridge", "patches", "gateway-02-pyproject.patch")
        body = open(pp).read()
        open(pp, "w").write(body[body.index("--- "):] if "--- " in body else body)
        bad = subprocess.run(["bash", os.path.join(tmp, "release", "cut-release.sh"), "--check"],
                             capture_output=True, text=True, cwd=tmp,
                             env={**os.environ, "MESH_MANAGER_OFFLINE": "1"})
        check_true("--check fails when a patch has no provenance header", bad.returncode != 0,
                   (bad.stdout + bad.stderr).strip().splitlines()[-1:] if (bad.stdout + bad.stderr).strip() else "")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
finish()
