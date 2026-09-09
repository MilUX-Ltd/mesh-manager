#!/usr/bin/env python3
"""Spec 063: the bench on a laptop. Ports rather than a Linux directory, and the gateway never listed."""
import os, sys, tempfile
sys.path.insert(0, os.path.dirname(__file__))
from _common import ROOT, check, check_true, finish, read  # noqa: E402
sys.path.insert(0, os.path.join(ROOT, "src"))
import fakegw_lib  # noqa: E402
fakegw_lib.install()
from mesh_manager import bridge as B  # noqa: E402

class P:  # what a system port looks like
    def __init__(self, device, vid=None, pid=None, serial_number=None, manufacturer=None, product=None):
        self.device, self.vid, self.pid = device, vid, pid
        self.serial_number, self.manufacturer, self.product = serial_number, manufacturer, product

MAC = [P("/dev/cu.Bluetooth-Incoming-Port"),
       P("/dev/cu.usbmodem101", 0x303a, 0x1001, "A4:CB:8F:EE:00:02", "Espressif", "USB JTAG_serial debug unit"),
       P("/dev/cu.usbmodem58FA0987", 0x2886, 0x0059, "9F3A2B", "Seeed", "T1000-E")]
WIN = [P("COM3"), P("COM7", 0x2886, 0x0059, "9F3A2B", "Seeed", "T1000-E")]

# AC1: one shape everywhere
rows = B.bench_ports(system="Darwin", ports=MAC)
check("AC1 the ports a Mac reports, in the product's own shape", [r["path"] for r in rows], ["/dev/cu.usbmodem101", "/dev/cu.usbmodem58FA0987"])
check("AC1 with what each device says of itself", (rows[1]["vendor"], rows[1]["product"], rows[1]["serial"], rows[1]["id"]), ("Seeed", "T1000-E", "9F3A2B", "2886:0059:9F3A2B"))
check("AC1 and Windows the same", [r["path"] for r in B.bench_ports(system="Windows", ports=WIN)], ["COM7"])

# AC2: Linux keeps its by-id names
byid = tempfile.mkdtemp()
for nm in ("usb-Seeed_T1000-E_9F3A-if00", "usb-Espressif_USB_JTAG_serial_debug_unit_A4-if00"):
    open(os.path.join(byid, nm), "w").write("")
lin = B.bench_ports(system="Linux", serial_dir=byid, ports=[])
check("AC2 a Linux box is still known by its by-id names", sorted(os.path.basename(r["path"]) for r in lin), ["usb-Espressif_USB_JTAG_serial_debug_unit_A4-if00", "usb-Seeed_T1000-E_9F3A-if00"])

# AC3 and AC4: what is never listed, and what is listed anyway
own = B.bench_ports(system="Darwin", ports=MAC, gateway="/dev/cu.usbmodem101")
check("AC3 the gateway is not bench kit", [r["path"] for r in own], ["/dev/cu.usbmodem58FA0987"])
moved = B.bench_ports(system="Darwin", ports=MAC, gateway="/dev/cu.usbmodem9999", gateway_serial="A4:CB:8F:EE:00:02")
check("AC3 and is still known when its port has changed", [r["path"] for r in moved], ["/dev/cu.usbmodem58FA0987"])
nogps = B.bench_ports(system="Darwin", ports=MAC + [P("/dev/cu.usbserial-GPS1", 0x1546, 0x01a7, "GNSS1", "u-blox", "GNSS Receiver")], gps="/dev/cu.usbserial-GPS1")
check("AC3 nor the receiver", "/dev/cu.usbserial-GPS1" in [r["path"] for r in nogps], False)
plain = B.bench_ports(system="Darwin", ports=[P("/dev/cu.usbmodemXYZ")])
check("AC4 a silent device is still listed, by its path", (len(plain), plain[0]["id"]), (1, "/dev/cu.usbmodemXYZ"))

# AC5: the operation and the page
st = tempfile.mkdtemp()
b = B.Bridge({"SERIAL": "", "MODE": "desktop"}, socket_path=os.path.join(st, "b.sock"), state_dir=st)
b._ports_for_test = MAC
d = b.op_bench_devices()
check_true("AC5 the operation answers from the ports", isinstance(d.get("devices"), list) and "gateway" in d)
b.stop()
page = read("src/mesh_manager/web.py") or ""
check_true("AC5 the page shows the maker and the serial where known", "vendor" in page and "serial" in page and "bench" in page.lower())

# AC6: the guide
g = read("docs/GUIDE.md") or ""
check_true("AC6 the guide says a laptop can be a bench", "bench" in g.lower() and "laptop" in g.lower() and ("plug" in g.lower()))
finish()
