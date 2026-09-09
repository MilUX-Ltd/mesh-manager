#!/usr/bin/env python3
"""Spec 001 AC1: the bridge tree carried in, with provenance, verified dictionaries and the
pins in exactly one place."""
import hashlib
import os
import re
import sys

sys.path.insert(0, os.path.dirname(__file__))
from _common import ROOT, check, check_true, finish, read, plus_side, skip  # noqa: E402

P = "bridge/patches/"
gw = read(P + "gateway-01-tak_meshtastic_gateway.patch")
py = read(P + "gateway-02-pyproject.patch")
sp = read(P + "sitepkg-02-atak_pb2.patch")
check_true("gateway patch present", gw is not None)
check_true("pyproject patch present", py is not None)
check_true("site-package patch present", sp is not None)

HEADER = re.compile(r"\A# MilUX patch, \d{1,2} \w+ 2026.*", re.M)
for name, text in (("gateway", gw), ("pyproject", py), ("sitepkg", sp)):
    check_true(f"{name} patch begins with a provenance header",
               bool(text) and HEADER.match(text) is not None)
    check_true(f"{name} patch header names the upstream version it applies to",
               bool(text) and "1.1.0" in text.split("\n--- ", 1)[0] if text else False)

plus = plus_side(gw or "")
for fn in ("def takv2_decode(", "def heartbeat(self):", "def mesh_nodes(self):"):
    check_true(f"gateway patch plus side carries {fn}", fn in plus)
check_true("gateway patch writes the heartbeat at its contract path",
           "/var/lib/vantage-mesh/heartbeat.json" in plus)
check_true("pyproject patch pins zstandard 0.25.0",
           bool(py) and 'zstandard = "0.25.0"' in (py or ""))

D = os.path.join(ROOT, "bridge", "dicts")
sums = read("bridge/dicts/SHA256SUMS") or ""
want = dict(reversed(l.split()) for l in sums.splitlines() if l.strip())
check("two dictionaries listed in SHA256SUMS", sorted(want),
      ["dict_aircraft.zstd", "dict_non_aircraft.zstd"])
for fn, digest in sorted(want.items()):
    p = os.path.join(D, fn)
    got = hashlib.sha256(open(p, "rb").read()).hexdigest() if os.path.exists(p) else None
    check(f"{fn} matches SHA256SUMS", got, digest)
prov = read("bridge/dicts/PROVENANCE.txt") or ""
check_true("PROVENANCE.txt names the SDK commit",
           "SDK_COMMIT=3e85d9354111e809c8600c94040b81d25ddb51f3" in prov)

cut = read("release/cut-release.sh") or ""
if not cut:
    skip("release/cut-release.sh consistency", "the release tooling is not in this tree")
for pin in ('GATEWAY_PIN="TAK-Meshtastic-Gateway==1.1.0"', 'MESHTASTIC_PIN="meshtastic==2.7.11"',
            "zstandard==0.25.0", 'MILUX_REV="3"'):
    check_true(f"cut script declares {pin}", pin in cut) if cut else None
# the pins live in the cut script and nowhere else: no other tracked file may restate them
elsewhere = []
for dp, dn, fns in os.walk(ROOT):
    dn[:] = [d for d in dn if d not in (".git", "tests", "docs", "venv", ".venv", "__pycache__")]
    for fn in fns:
        rel = os.path.relpath(os.path.join(dp, fn), ROOT)
        # CI must name the library version it installs, so the workflow may restate the Meshtastic
        # pin; the check below holds it equal to the cut script's, so the two cannot drift.
        if rel == "release/cut-release.sh" or rel.startswith("bridge/patches/") or rel.startswith(".github/workflows/"):
            continue
        t = read(rel) or ""
        if "meshtastic==2.7.11" in t or "TAK-Meshtastic-Gateway==1.1.0" in t:
            elsewhere.append(rel)
if cut:
    check("no file outside the cut script restates a pin", elsewhere, [])
    _wf = read(".github/workflows/tests.yml") or ""
    _m = re.search(r"meshtastic==([0-9.]+)", _wf)
    check_true("the workflow installs the same Meshtastic the cut pins",
               _m is not None and f'MESHTASTIC_PIN="meshtastic=={_m.group(1)}"' in cut)
finish()
