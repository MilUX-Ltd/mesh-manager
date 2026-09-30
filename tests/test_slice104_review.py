#!/usr/bin/env python3
"""Spec 104, from its independent review (30 September 2026): packets stored before the
broker mark existed are counted as not known rather than as nothing; the mesh's utilisation is the
mean of the nodes this radio hears, one vote each, leaving out nodes only a broker carries; and the
existing hourly chart keeps the first part-hour of its window. Committed failing; the build does
not edit it."""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(__file__))
from _common import ROOT, check, finish  # noqa: E402
sys.path.insert(0, os.path.join(ROOT, "src"))
import fakegw_lib  # noqa: E402
fakegw_lib.install()
from mesh_manager import bridge as B  # noqa: E402
from mesh_manager.history import utc  # noqa: E402

T0 = 1_789_999_200.0            # on the hour
NOW = T0 + 23 * 3600 + 1800
_real_time = time.time
time.time = lambda: NOW
state = tempfile.mkdtemp()
br = B.Bridge({"SERIAL": "/dev/serial/by-id/usb-fake-test-radio-if00", "HISTORY_DAYS": 30}, socket_path=os.path.join(state, "b.sock"),
              state_dir=state, observe=True, gps_reader=False)
h = br.history
own = (br._own() or {}).get("id")
H5 = T0 + 5 * 3600

# stored before the upgrade: no broker mark at all
for i in range(40):
    h.packet("!ee000061", port="POSITION_APP", snr=5.0, hops=0, ts=utc(H5 + i * 30), via_mqtt=None)
# heard over the air, and one node a broker alone carries
for n in ("!ee000062", "!ee000063"):
    h.packet(n, port="POSITION_APP", snr=5.0, hops=0, ts=utc(H5 + 60), via_mqtt=0)
h.packet("!ee000064", port="POSITION_APP", ts=utc(H5 + 60), via_mqtt=1)
for off, ch in ((300, 4.0), (900, 6.0)):
    h.telemetry("!ee000062", chutil=ch, ts=utc(H5 + off))
h.telemetry("!ee000063", chutil=10.0, ts=utc(H5 + 600))
h.telemetry("!ee000064", chutil=60.0, ts=utc(H5 + 600))   # another mesh's air, carried by a broker
# this radio, in the part-hour before the first whole hour of the window
h.telemetry(own, chutil=9.0, airutil=0.5, ts=utc(NOW - 24 * 3600 + 600))

hl = br.op_health(hours=24)
ser = hl.get("series") or []
h5 = next((x for x in ser if x.get("hour") == utc(H5)[:13] + ":00Z"), {})
check("rows stored before the broker mark are counted as not known, not as nothing", sum(int(x.get("unmarked") or 0) for x in ser), 40)
check("and in their hour", h5.get("unmarked"), 40)
check("the mesh is the mean of the nodes this radio hears, one vote each, leaving out a broker's", h5.get("mesh_chutil"), 7.5)
check("the existing hourly chart keeps the first part-hour of its window", [x["hour"] for x in hl.get("hourly") or []][:1], [utc(NOW - 24 * 3600 + 600)[:13] + ":00Z"])
time.time = _real_time

finish()
