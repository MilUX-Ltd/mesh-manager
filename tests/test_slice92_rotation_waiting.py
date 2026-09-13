#!/usr/bin/env python3
"""Spec 092: a rotation's waiting count, on whatever page you are on.

A rotation is not done when the key is pushed; it is done when the fleet is back. That count lived
on the rotation page only, so an operator anywhere else had to go and look.

It is not a second alert. An alert is a fault and waits for an acknowledgement; a rotation with
devices still out is work in progress that resolves itself as they return.
"""
import os, re, sys
sys.path.insert(0, os.path.dirname(__file__))
from _common import ROOT, check, check_true, finish, read  # noqa: E402

web = read("src/mesh_manager/web.py") or ""
br = read("src/mesh_manager/bridge.py") or ""

# ---- AC1, AC8 the counts travel with the status, from the one source -----------------------------
check_true("AC1 status carries the rotation's counts", '"rotation_waiting"' in br,
           "status has no rotation count")
# the helper the status calls must ask the rotation the same question the page asks, rather than
# counting for itself. A fixed window either side of a line is not a way to find a function body.
_helper = re.search(r"def _rotation_waiting\(self\):[\s\S]{0,900}?\n    def ", br)
check_true("AC8 there is one helper behind the count", _helper is not None, "no _rotation_waiting")
check_true("AC8 and it asks the rotation's own answer, not a second count",
           bool(_helper) and "op_rotation_status(" in _helper.group(0),
           (_helper.group(0)[:160] if _helper else "none"))

# ---- AC2, AC3, AC5, AC6 the pill ------------------------------------------------------------------
_s0 = web.find("def state_strip(")
_s1 = web.find("\ndef ", _s0 + 10)
strip = web[_s0:_s1 if _s1 > 0 else _s0 + 8000]
check_true("AC3 the strip renders a waiting pill", "rotation_waiting" in strip, "no pill in the strip")
check_true("AC2 and only when there is a rotation with devices out",
           re.search(r"if .*st\.get\(\"rotation_waiting\"\)", strip) is not None, strip[:0] or "unguarded")
check_true("AC5 it says how many, and reads at one",
           re.search(r"waiting.*!= 1|!= 1.*waiting", strip) is not None
           or re.search(r"'s' if .*rotation_waiting", strip) is not None,
           "no singular form")
check_true("AC7 and it links to the rotation", re.search(r"href='/[a-z]+#?rotation", strip) is not None,
           "the count is a dead end")

# ---- AC4 not an alert -------------------------------------------------------------------------------
# the markup that builds the pill, which is a block and not one line: looking for both words on a
# single line found nothing while the pill was right there.
_m = re.search(r"waiting = \([\s\S]{0,700}?\)\n", strip)
_pill = _m.group(0) if _m else ""
check_true("AC4 the pill exists to inspect", bool(_pill), "no pill markup to judge")
check_true("AC4 it is not painted as a fault", "--bad" not in _pill, _pill.strip()[:120])
check_true("AC4 and it is distinguishable from the alerts pill",
           "--bad" in strip, "the alerts pill should still be the red one")

finish()
