#!/usr/bin/env python3
"""Spec 033: what the device on the bench says about its own receiver."""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(__file__))
from _common import ROOT, check, check_true, finish  # noqa: E402
sys.path.insert(0, os.path.join(ROOT, "src"))
import fakegw_lib  # noqa: E402
fakegw_lib.install()
from mesh_manager import bridge as B  # noqa: E402

state = tempfile.mkdtemp()
byid = tempfile.mkdtemp()
RADIO = os.path.join(byid, "usb-Espressif_USB_JTAG_serial_debug_unit_A4:CB:8F:EE:00:01-if00")
GW = os.path.join(byid, "usb-Seeed_T1000-E_EE02-if00")      # the device on the bench, not the gateway
for _p in (RADIO, GW):
    open(_p, "w").close()
br = B.Bridge({"SERIAL": RADIO}, socket_path=os.path.join(state, "b.sock"), state_dir=state, observe=True, gps_reader=False)
br.serial_dir = byid
br.bootloader_check = lambda path: False
br.serial_factory = fakegw_lib.FakeBenchIface
now = int(time.time())

# op_bench_read opens the device, reads it and closes it, so the test keeps one interface and
# hands the same one back each time, the way a device left plugged in behaves.
_iface = {}


def bench_iface():
    if "i" not in _iface:
        _iface["i"] = fakegw_lib.FakeBenchIface(GW)
    return _iface["i"]


br._bench_open = lambda path=None: (bench_iface(), None)


# ---- AC1 a device with a fix ----------------------------------------------------------------------
iface = bench_iface()
iface.position = {"latitude": 51.5, "longitude": -0.12, "altitude": 34, "satsInView": 9, "time": now - 120}
r = br.op_bench_read(path=GW)
p = r.get("position") or {}
check("AC1 the fix comes back with the read", (p.get("fix"), round(p.get("lat") or 0, 4), round(p.get("lon") or 0, 4)), (True, 51.5, -0.12))
check("AC1 altitude and satellites where the device reports them", (p.get("alt"), p.get("sats")), (34, 9))
check_true("AC1 the fix carries its time", bool(p.get("time")))

# ---- AC2 a receiver with no fix --------------------------------------------------------------------
iface = bench_iface()
iface.position = {}
r = br.op_bench_read(path=GW)
p = r.get("position") or {}
check("AC2 no fix: said so, with no coordinates", (p.get("fix"), p.get("lat"), p.get("lon")), (False, None, None))
check_true("AC2 and it is not confused with position being switched off", p.get("enabled") is not False)

# ---- AC3 position broadcasting switched off ---------------------------------------------------------
iface = bench_iface()
iface.position = {}
for _cfg in (iface.localNode.localConfig, iface.localNode.device_config):
    _cfg.position.position_broadcast_secs = 0
    _cfg.position.gps_mode = 0                             # GpsMode.DISABLED
r = br.op_bench_read(path=GW)
p = r.get("position") or {}
check("AC3 switched off is a state of its own", (p.get("fix"), p.get("enabled")), (False, False))
check_true("AC3 and it says which, so nobody chases a receiver fault that is a setting",
           "off" in str(p.get("state", "")).lower() or "disabled" in str(p.get("state", "")).lower())

# ---- AC4 the export carries it -----------------------------------------------------------------------
iface = bench_iface()
iface.position = {"latitude": 51.5, "longitude": -0.12, "time": now - 60}
for _cfg in (iface.localNode.localConfig, iface.localNode.device_config):
    _cfg.position.position_broadcast_secs = 900
    _cfg.position.gps_mode = 1
r = br.op_bench_export(path=GW)
check_true("AC4 the export succeeded", bool(r.get("export")))
import json  # noqa: E402
doc = json.load(open(r["export"]))
check_true("AC4 the export carries the same position record",
           (doc.get("position") or {}).get("fix") is True and round((doc["position"] or {}).get("lat") or 0, 4) == 51.5)

check_true("AC5 the fix carries its MGRS", isinstance((br.op_bench_read(path=GW).get("position") or {}).get("mgrs"), str))

# ---- AC5 the screen ----------------------------------------------------------------------------------
from mesh_manager import web as W  # noqa: E402

src = W.MAP_JS if hasattr(W, "MAP_JS") else ""
page = W.WRITE_JS
check_true("AC5 the device summary says what the receiver did", "fixText" in page
           and "a receiver, but no fix" in page and "position switched off on the device" in page)
check_true("AC5 a fix offers the map, built as a node and not as markup",
           "mapLink" in page and "createElement('a')" in page and "/map#at=" in page)
import inspect  # noqa: E402
websrc = inspect.getsource(W)
check_true("AC5 the map opens on a place from the hash, taking only two numbers",
           "#at=" in websrc and "at=(-?" in websrc.replace("\\\\", "\\"))

finish()
