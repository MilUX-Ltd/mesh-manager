#!/usr/bin/env python3
"""Spec 031: the receiver's own state, on the strip."""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(__file__))
from _common import ROOT, check, check_true, finish  # noqa: E402
sys.path.insert(0, os.path.join(ROOT, "src"))
import fakegw_lib  # noqa: E402
fakegw_lib.install()
from mesh_manager.web import state_strip  # noqa: E402

BASE = {"connected": True, "radio_present": True, "nodes_seen": 3, "nodes_db": 4}


def strip(gps):
    return state_strip(dict(BASE, gps=gps) if gps is not None else dict(BASE))


# ---- AC1 the bridge's status carries it ----------------------------------------------------------
from mesh_manager.bridge import Bridge  # noqa: E402  (fakegw_lib.install() above supplies the gateway)

b = Bridge.__new__(Bridge)               # no radio, no threads: only op_status is under test
b.conf, b.started, b.observe = {"SERIAL": ""}, 0.0, False
b.last_activity = b.last_forwarded = None
b.interface = None
b.state_dir, b.socket_path, b.watchdog_state = "/tmp", "/tmp/s.sock", "pinging"
b.gps_state = {"reachable": True, "fix": True, "seen": 11, "used": 8,
               "checked": "2026-09-04T10:00:00Z", "via": "gpsd://127.0.0.1:2947"}
b._lora = lambda: None
b.op_channels = lambda **_: {"channels": []}
b._own = lambda: {}
b._own_chutil = lambda: None
b._verdict = lambda _c: None
b._alerts_load = lambda: {"open": []}
b.own_position = lambda: None
st = b.op_status()
check("AC1 op_status carries what the last read established", st.get("gps"), b.gps_state)

# ---- AC2 a box with no receiver says nothing about one ------------------------------------------
s = strip(None)
check_true("AC2 no gps at all: no GPS element", "GPS" not in s)

# ---- AC3 the receiver did not answer -------------------------------------------------------------
s = strip({"reachable": False, "checked": "2026-09-04T10:00:00Z", "via": "gpsd://127.0.0.1:2947"})
check_true("AC3 not answering: a warn lamp and the words", "lamp--warn" in s and "not answering" in s and "GPS" in s)

# ---- AC4 reachable, no fix ------------------------------------------------------------------------
s = strip({"reachable": True, "fix": False, "seen": 7, "used": 0, "checked": "2026-09-04T10:00:00Z", "via": "gpsd://127.0.0.1:2947"})
check_true("AC4 no fix: a warn lamp and the words", "lamp--warn" in s and "no fix" in s.lower())
check_true("AC4 no fix: satellites seen are shown", re.search(r"7\s*(satellites?|sats?)\b", s, re.I) is not None)

# ---- AC5 reachable, with a fix ---------------------------------------------------------------------
s = strip({"reachable": True, "fix": True, "seen": 11, "used": 8, "checked": "2026-09-04T10:00:00Z", "via": "gpsd://127.0.0.1:2947"})
check_true("AC5 a fix: an ok lamp", "lamp--ok" in s and "GPS fix" in s)
check_true("AC5 a fix: satellites used are shown", re.search(r"8\s*(satellites?|sats?)\b", s, re.I) is not None)

# ---- AC6 the detail is in the tooltip ---------------------------------------------------------------
# 5 Sep 2026 content review: the tip names the receiver in words, not its socket address (that is for whoever installed the box)
check_true("AC6 the tooltip names what was read", "data-tip" in s and "Receiver: gpsd" in s)
check_true("AC6 the tooltip carries when it was last read", "2026-09-04T10:00:00Z" in s or "10:00" in s)

# a source that reports no satellite counts must not print an empty count
s = strip({"reachable": True, "fix": True, "seen": None, "used": None, "checked": "2026-09-04T10:00:00Z", "via": "/dev/ttyACM0"})
check_true("AC5 no satellite counts reported: none are printed", "lamp--ok" in s and "None" not in s)
check_true("AC5 a serial receiver names its path", "/dev/ttyACM0" in s)

finish()
