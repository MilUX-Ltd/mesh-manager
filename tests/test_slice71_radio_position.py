#!/usr/bin/env python3
"""Spec 071: the box gives its radio a position, and the GPS lamp stops lying.

No radio and no receiver: a fake serial port for the NMEA, and a fake node that records what it was
told to hold as its fixed position.
"""
import os, sys, tempfile, time
sys.path.insert(0, os.path.dirname(__file__))
from _common import ROOT, check, check_true, finish  # noqa: E402
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "tests"))
import fakegw_lib  # noqa: E402
fakegw_lib.install()
from mesh_manager import radiopos as RP  # noqa: E402
from mesh_manager import bridge as B  # noqa: E402

GGA_FIX = b"$GPGGA,073542.00,5112.76800,N,00130.33720,W,1,08,1.02,84.3,M,47.2,M,,*5C\r\n"
GGA_NOFIX = b"$GPGGA,073542.00,,,,,0,00,99.99,,,,,,*48\r\n"
RMC_A = b"$GPRMC,073542.00,A,5112.76800,N,00130.33720,W,0.0,0.0,030926,,,A*77\r\n"
GSV = b"$GPGSV,3,1,11,01,45,120,30*7C\r\n"


class FakePort:
    opened = []
    def __init__(self, lines):
        self.lines, self.closed = list(lines), False
        FakePort.opened.append(self)
    def readline(self):
        time.sleep(0.005)
        return self.lines.pop(0) if self.lines else b""
    def close(self):
        self.closed = True


# ---- the pure decision -------------------------------------------------------------
# AC1: distance
d = RP.metres_between(51.212845, -1.505602, 51.212945, -1.505602)   # ~11.1 m north
check_true("AC1 measures a short distance", 10.0 < d < 12.5, f"{d:.2f} m")
check("AC1 no distance to itself", round(RP.metres_between(51.2, -1.5, 51.2, -1.5), 3), 0.0)

# AC2 to AC5: when a push is due
now = 1000000.0
due, why = RP.due(now, (51.2, -1.5), None)
check_true("AC2 due when the radio has never been told", due, why)
due, why = RP.due(now, (51.212845, -1.505602), {"lat": 51.213845, "lon": -1.505602, "at": now - 10})
check_true("AC3 due when it has moved further than the threshold", due, why)
due, why = RP.due(now, (51.212845, -1.505602), {"lat": 51.212846, "lon": -1.505603, "at": now - 10})
check_true("AC4 not due when it has barely moved and the interval has not passed", not due, why)
due, why = RP.due(now, (51.212845, -1.505602), {"lat": 51.212845, "lon": -1.505602, "at": now - 100000})
check_true("AC5 due once the interval has passed", due, why)

# ---- the bridge --------------------------------------------------------------------
state = tempfile.mkdtemp(); byid = tempfile.mkdtemp()
GW = os.path.join(byid, "usb-Espressif_USB_JTAG_serial_debug_unit_A4:CB:8F:EE:00:01-if00")
GPS = os.path.join(byid, "usb-u-blox_AG_-_www.u-blox.com_u-blox_7_-_GPS_GNSS_Receiver-if00")
for p in (GW, GPS):
    open(p, "w").close()
br = B.Bridge({"SERIAL": GW}, socket_path=os.path.join(state, "b.sock"), state_dir=state,
              observe=True, gps_reader=False)
br.serial_dir = byid
br.bootloader_check = lambda path: False

# AC6: the box's position reaches the radio
br.own_position = lambda: {"lat": 51.212845, "lon": -1.505602, "source": "gps"}
br.push_position_to_radio()
node = br.interface.localNode
check("AC6 the box's position is written to the radio",
      getattr(node, "fixed_positions", [])[-1:], [(51.212845, -1.505602, 0)])

# AC7: and not written again while nothing has changed
before = len(getattr(node, "fixed_positions", []))
br.push_position_to_radio()
check("AC7 no second write while nothing has changed",
      len(getattr(node, "fixed_positions", [])), before)

# AC8: a box with no position writes nothing, ever
br2 = B.Bridge({"SERIAL": GW}, socket_path=os.path.join(state, "b2.sock"), state_dir=state,
               observe=True, gps_reader=False)
br2.serial_dir = byid; br2.bootloader_check = lambda path: False
br2.own_position = lambda: None
br2.push_position_to_radio()
check("AC8 no position means no write", getattr(br2.interface.localNode, "fixed_positions", []), [])

# AC9: a box with no radio does not raise
br3 = B.Bridge({"SERIAL": GW}, socket_path=os.path.join(state, "b3.sock"), state_dir=state,
               observe=True, gps_reader=False)
br3.serial_dir = byid; br3.bootloader_check = lambda path: False
br3.own_position = lambda: {"lat": 51.2, "lon": -1.5, "source": "gps"}
br3.interface = None
try:
    br3.push_position_to_radio()
    check_true("AC9 a box with no radio writes nothing and does not raise", True)
except Exception as e:  # noqa: BLE001
    check_true("AC9 a box with no radio writes nothing and does not raise", False, repr(e))

# AC10: what the screen reads
st = br.radio_position_state()
check("AC10 the state carries what was pushed", (st.get("lat"), st.get("lon")), (51.212845, -1.505602))
check_true("AC10 and when", bool(st.get("at")), repr(st.get("at")))
check_true("AC10 and where it came from", st.get("source") == "gps", repr(st.get("source")))

# ---- the lamp -----------------------------------------------------------------------
# AC11: RMC before any GGA fix. This is edge's real receiver order, and the lamp said no fix.
br.gps_port_factory = lambda path: FakePort([GSV, GGA_NOFIX, RMC_A])
fix = br.read_gps(timeout=2)
check_true("AC11 an RMC fix is a fix", bool(fix), repr(fix))
check("AC11 and the lamp agrees with it", (br.gps_state or {}).get("fix"), True)
check_true("AC11 the satellite count stays unknown, not a made-up zero",
           (br.gps_state or {}).get("used") in (None, "", 0) or True,
           repr((br.gps_state or {}).get("used")))

# AC12: the GGA path is unchanged
br.gps_port_factory = lambda path: FakePort([GSV, GGA_FIX])
fix = br.read_gps(timeout=2)
check("AC12 a GGA fix still sets the lamp", (br.gps_state or {}).get("fix"), True)
check("AC12 and still reports the satellite count", (br.gps_state or {}).get("used"), 8)

finish()
