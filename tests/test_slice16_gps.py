#!/usr/bin/env python3
"""Spec 014: the box knows where it is. The receiver on a fake NMEA stream, the precedence, the
bench list, the screen, the installer."""
import http.client
import os
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(__file__))
from _common import ROOT, check, check_true, finish  # noqa: E402
sys.path.insert(0, os.path.join(ROOT, "src"))
import fakegw_lib  # noqa: E402
fakegw_lib.install()
from fakebridge_lib import start_fake_bridge  # noqa: E402
import fakebridge_lib as FB  # noqa: E402
from mesh_manager import bridge as B, web as W  # noqa: E402

GGA_FIX = b"$GPGGA,073542.00,5112.76800,N,00130.33720,W,1,08,1.02,84.3,M,47.2,M,,*5C\r\n"
GGA_NOFIX = b"$GPGGA,073542.00,,,,,0,00,99.99,,,,,,*48\r\n"
RMC_V = b"$GPRMC,073542.00,V,,,,,,,030926,,,N*7B\r\n"
JUNK = b"$GPGSV,3,1,11,01,45,120,30*7C\r\n"


class FakePort:
    opened = []

    def __init__(self, lines):
        self.lines, self.closed = list(lines), False
        FakePort.opened.append(self)

    def readline(self):
        time.sleep(0.01)
        return self.lines.pop(0) if self.lines else b""

    def close(self):
        self.closed = True


state = tempfile.mkdtemp()
byid = tempfile.mkdtemp()
GW = os.path.join(byid, "usb-Espressif_USB_JTAG_serial_debug_unit_A4:CB:8F:EE:00:01-if00")
GPS = os.path.join(byid, "usb-u-blox_AG_-_www.u-blox.com_u-blox_7_-_GPS_GNSS_Receiver-if00")
NEW = os.path.join(byid, "usb-Seeed_T1000-E_9F3A-if00")
for p in (GW, GPS, NEW):
    open(p, "w").close()
br = B.Bridge({"SERIAL": GW}, socket_path=os.path.join(state, "b.sock"), state_dir=state, observe=True, gps_reader=False) if "gps_reader" in B.Bridge.__init__.__code__.co_varnames else B.Bridge({"SERIAL": GW}, socket_path=os.path.join(state, "b.sock"), state_dir=state, observe=True)
br.serial_dir = byid
br.bootloader_check = lambda path: False

# AC1 the receiver
br.gps_port_factory = lambda path: FakePort([JUNK, GGA_NOFIX, GGA_FIX])
fix = br.read_gps(timeout=2) if hasattr(br, "read_gps") else None
check("AC1 a GGA fix is parsed", (round((fix or {}).get("lat", 0), 5), round((fix or {}).get("lon", 0), 5), (fix or {}).get("sats"), bool((fix or {}).get("time"))), (51.21280, -1.50562, 8, True))
check_true("AC1 the port is closed after the read", FakePort.opened and FakePort.opened[-1].closed)
br.gps_port_factory = lambda path: FakePort([GGA_NOFIX, RMC_V, JUNK])
check("AC1 no fix: no GPS position", br.read_gps(timeout=1) if hasattr(br, "read_gps") else "missing", None)

# AC2 precedence: gps > the radio's own GPS fix > config > devices > the radio's stored position > none
br.gps_fix = {"lat": 51.3, "lon": -1.4, "sats": 8, "time": "2026-09-03T07:35:42Z", "path": GPS}
br.conf = {"SERIAL": GW, "MAP_LAT": "51.2", "MAP_LON": "-1.5"}
br.interface.position = {"latitude": 51.4, "longitude": -1.3, "locationSource": "LOC_INTERNAL", "time": int(time.time()) - 60}
check("AC2 the receiver's fix beats everything", ((br.own_position() or {}).get("source"), (br.own_position() or {}).get("lat")), ("gps", 51.3))
br.gps_fix = None
check("AC2 the radio's own GPS fix beats the declared position", ((br.own_position() or {}).get("source"), (br.own_position() or {}).get("lat")), ("radio_gps", 51.4))
br.interface.position = {"latitude": 54.1, "longitude": -0.3, "locationSource": "LOC_MANUAL"}
check("AC2 a set or stored radio position is not a fix: the declared position wins", ((br.own_position() or {}).get("source"), (br.own_position() or {}).get("lat")), ("config", 51.2))
br.conf = {"SERIAL": GW}
br.meshtastic_devices["!bb000002"] = {"long_name": "Two", "meshtastic_id": "!bb000002", "last_lat": 51.25, "last_lon": -1.55}
br._mesh_radio["!bb000002"] = {"heard": "2026-09-03T07:00:00Z", "snr": 5.0, "hops": 0}
pos = br.own_position() or {}
check("AC2 devices (the median of heard nodes with a fix) beats the radio's stored position", (pos.get("source"), pos.get("lat"), pos.get("lon"), pos.get("count")), ("devices", 51.225, -1.525, 2))
del br.meshtastic_devices["!bb000002"]; del br._mesh_radio["!bb000002"]
br.meshtastic_devices["!aa000001"]["last_lat"] = 0; br.meshtastic_devices["!aa000001"]["last_lon"] = 0
check("AC2 a stored radio position is not a source (Spec 018): none", br.own_position(), None)
br.interface.position = None
check("AC2 none", br.own_position(), None)
br.meshtastic_devices["!aa000001"]["last_lat"] = 51.2; br.meshtastic_devices["!aa000001"]["last_lon"] = -1.5

# AC3 the receiver is found and is not a bench device
check("AC3 the receiver found by its name", br.gps_path() if hasattr(br, "gps_path") else None, GPS)
paths = [d["path"] for d in br.op_bench_devices().get("devices", [])]
check("AC3 the receiver is not a bench device", GPS in paths, False)
br.conf = {"SERIAL": GW, "MAP_GPS": "/dev/serial/by-id/usb-other-gps-if00"}
check("AC3 MAP_GPS is used as given", br.gps_path() if hasattr(br, "gps_path") else None, "/dev/serial/by-id/usb-other-gps-if00")
br.conf = {"SERIAL": GW}
st = br.op_status()
check_true("AC4 status carries position with its source", isinstance(st.get("position"), (dict, type(None))) and "position" in st)

# AC4 the screen
def serve(links_own):
    saved = dict(FB.LINKS["own"]); FB.LINKS["own"].update(links_own)
    fb = start_fake_bridge()
    srv = W.make_server(bind="127.0.0.1", port=0, socket_path=fb.path, etc_dir=tempfile.mkdtemp(), config={"AUTH": "off"})
    threading.Thread(target=srv.serve_forever, daemon=True).start(); time.sleep(0.3)
    c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10); c.request("GET", "/map"); r = c.getresponse(); body = r.read().decode(); c.close()
    srv.shutdown(); FB.LINKS["own"].clear(); FB.LINKS["own"].update(saved)
    return body


page = serve({"lat": 51.2128, "lon": -1.5056, "position_source": "devices", "count": 7})
check_true("AC4 the map says the box is placed among the devices it hears, an estimate", "placed among the devices it hears" in page and "estimate" in page)
page = serve({"lat": 51.2128, "lon": -1.5056, "position_source": "devices", "count": 1, "gps": {"reachable": True, "fix": False, "seen": 14, "used": 0}})
check_true("AC4 a connected receiver without a fix is said so, with the sky", "GPS receiver connected, no fix yet (14 satellites seen, 0 used)" in page)
page = serve({"lat": 51.2128, "lon": -1.5056, "position_source": "gps", "sats": 8, "time": "2026-09-03T07:35:42Z"})
check_true("AC4 ...and names the receiver for a GPS fix", "GPS" in page and "satellites" in page)

# AC5 the installer
root = tempfile.mkdtemp()
for d in ("opt/tak", "etc/systemd/system", "dev/serial/by-id"):
    os.makedirs(os.path.join(root, d), exist_ok=True)
open(os.path.join(root, "dev/serial/by-id/usb-x-if00"), "w").close()
out = subprocess.run(["bash", os.path.join(ROOT, "install", "install.sh"), "/nonexistent.tgz", "--serial", "/dev/serial/by-id/usb-x-if00", "--filter-group", "MilUX",
                      "--gps", "/dev/serial/by-id/usb-u-blox-if00", "--dry-run"], capture_output=True, text=True, env=dict(os.environ, MESH_MANAGER_ROOT=root)).stdout
check_true("AC5 the installer writes MAP_GPS", "MAP_GPS=/dev/serial/by-id/usb-u-blox-if00" in out)
os.makedirs(os.path.join(root, "etc/mesh-manager"), exist_ok=True)
open(os.path.join(root, "etc/mesh-manager/config"), "w").write("SERIAL=/dev/serial/by-id/usb-x-if00\nREGION=EU_868\nCHANNEL=x\nFILTER_GROUP=MilUX\nEXTRA_ARGS=\nBIND=127.0.0.1\nPORT=8093\nAUTH=off\nMAP_LAT=51.2100\nMAP_LON=-1.5000\n")
out = subprocess.run(["bash", os.path.join(ROOT, "install", "install.sh"), "/nonexistent.tgz", "--no-map-position", "--dry-run"], capture_output=True, text=True, env=dict(os.environ, MESH_MANAGER_ROOT=root)).stdout
check_true("AC5 --no-map-position clears a declared position", "MAP_LAT=" not in out)
finish()
