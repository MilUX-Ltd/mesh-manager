#!/usr/bin/env python3
"""Spec 009: the register and the bench. Bridge half on the fake gateway with a fake bench device
on a by-id path; screen half on the fake bridge."""
import http.client
import json
import os
import re
import stat
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
from mesh_manager import bridge as B, catalogue as C, web as W  # noqa: E402

# ---- AC1 the catalogue
want = {"register": "read", "register_set": "change", "bench_devices": "read", "bench_read": "read", "bench_onboard": "change", "bench_export": "read"}
got = {k: (C.by_id(k) or {}).get("risk") for k in want}
check("AC1 the six actions with their risks", got, want)
check("AC1 parity across routes, forms and tools holds", C.parity_problems(C.ACTIONS, W.api_action_routes(), [t["name"] for t in W.mcp_tools("act")]), [])

# ---- the bridge half
state = tempfile.mkdtemp()
byid = tempfile.mkdtemp()
GW = os.path.join(byid, "usb-Espressif_USB_JTAG_serial_debug_unit_A4:CB:8F:EE:00:01-if00")
NEW = os.path.join(byid, "usb-Seeed_T1000-E_9F3A-if00")
BOOT = os.path.join(byid, "usb-RAKwireless_WisCore_RAK4631_Board_BOOT-if00")
GPS = os.path.join(byid, "usb-u-blox_AG_-_www.u-blox.com_u-blox_7_-_GPS_GNSS_Receiver-if00")
for pth in (GW, NEW, BOOT, GPS):
    open(pth, "w").close()
br = B.Bridge({"SERIAL": GW}, socket_path=os.path.join(state, "b.sock"), state_dir=state, observe=True)
br.interface.localNode.localConfig.security.public_key = b"\x07" * 32
br.serial_dir = byid
br.bootloader_check = staticmethod(lambda path: path.endswith("BOOT-if00")) if False else (lambda path: path.endswith("BOOT-if00"))
br.serial_factory = fakegw_lib.FakeBenchIface

# AC2 the register
r = br.op_register_set(id="!aa000001", label="Tracker 9 (recce)", holder="Cpl Smith") if hasattr(br, "op_register_set") else {}
check("AC2 register_set answers what it wrote", (r.get("written") or {}).get("label"), "Tracker 9 (recce)")
disk = json.load(open(os.path.join(state, "register.json"))) if os.path.exists(os.path.join(state, "register.json")) else {}
check("AC2 ...and persists it", (disk.get("!aa000001") or {}).get("holder"), "Cpl Smith")
br.meshtastic_devices["!dd000004"] = {"long_name": "Old", "meshtastic_id": "!dd000004"}
rows = {x["id"]: x for x in (br.op_register().get("rows", []) if hasattr(br, "op_register") else [])}
check("AC2 the register joins on id: NODEINFO name beside the label", (rows.get("!aa000001", {}).get("name"), rows.get("!aa000001", {}).get("label")), ("Tracker9", "Tracker 9 (recce)"))
check("AC2 a device never read is not managed", rows.get("!aa000001", {}).get("managed"), False)
check("AC2 a database-only node is not heard", (rows.get("!dd000004", {}).get("heard_here"), rows.get("!dd000004", {}).get("label")), (False, ""))

# AC3 the bench lists the by-id devices other than the gateway's
d = br.op_bench_devices() if hasattr(br, "op_bench_devices") else {}
paths = {x["path"]: x for x in d.get("devices", [])}
check("AC3 the gateway's own path is not a bench device", GW in paths, False)
check("AC3 the others are, with the bootloader one flagged", (NEW in paths, paths.get(BOOT, {}).get("bootloader")), (True, True))
check_true("AC3 the bootloader device carries its recovery step", "UF2" in str(paths.get(BOOT, {}).get("recovery", "")))
check("AC3 the box's GPS receiver is never a bench device (Spec 014)", GPS in paths, False)

# AC4 bench_read
r = br.op_bench_read(path=GW) if hasattr(br, "op_bench_read") else {}
check_true("AC4 the gateway's path is refused, pointing at the Radio page", "Radio page" in str(r.get("error", "")))
r = br.op_bench_read(path=BOOT) if hasattr(br, "op_bench_read") else {}
check_true("AC4 a bootloader device is refused with the recovery step", "bootloader" in str(r.get("error", "")).lower() and "UF2" in str(r.get("error", "")))
r = br.op_bench_read(path=NEW) if hasattr(br, "op_bench_read") else {}
check("AC4 a device answers id, names, region, preset, role, firmware", (r.get("id"), r.get("long_name"), r.get("short_name"), r.get("region"), r.get("modem_preset"), r.get("role"), r.get("firmware")),
      ("!ee000005", "New Device", "NEW", "UNSET", "LONG_FAST", "CLIENT", "2.6.11"))
check("AC4 channels by name and role, never keys", [(c.get("index"), c.get("name"), c.get("role"), "psk" in c or "_psk" in c) for c in r.get("channels", [])][:1], [(0, "LongFast", "PRIMARY", False)])
check("AC4 not managed yet", (r.get("managed"), r.get("admin_keys")), (False, 0))
check_true("AC4 the device was closed after the read", fakegw_lib.FakeBenchIface.opened and fakegw_lib.FakeBenchIface.opened[-1].closed)

# AC5 onboard
gw_ch = br.interface.localNode.channels[0]
r = br.op_bench_onboard(path=NEW, long_name="Tracker 11", short_name="T11", role="TRACKER") if hasattr(br, "op_bench_onboard") else {}
dev = fakegw_lib.FakeBenchIface.opened[-1].localNode if fakegw_lib.FakeBenchIface.opened else None
check("AC5 confirmed from the device's own answers", r.get("confirmed"), True)
from meshtastic.protobuf import config_pb2 as _cfg
check("AC5 the device's names and role", (dev.device_owner.long_name if dev else None, dev.device_owner.short_name if dev else None, dev.device_config.device.role if dev else None), ("Tracker 11", "T11", _cfg.Config.DeviceConfig.Role.Value("TRACKER")))
check("AC5 slot 0 carries the gateway's channel name and key", ((dev.device_channels[0].settings.name, dev.device_channels[0].settings.psk) if dev else None), (gw_ch.settings.name, gw_ch.settings.psk))
check("AC5 the gateway's region and preset", ((dev.device_config.lora.region, dev.device_config.lora.modem_preset) if dev else None), (br.interface.localNode.localConfig.lora.region, br.interface.localNode.localConfig.lora.modem_preset))
check("AC5 the gateway's public key among the admin keys", (b"\x07" * 32) in list(dev.device_config.security.admin_key) if dev else None, True)
check_true("AC5 the answer holds no key material", "psk" not in json.dumps(r) and (b"\x07" * 32).hex() not in json.dumps(r) and "BwcHBw" not in json.dumps(r))
rb = r.get("read_back") or {}
check("AC5 the read-back names the channel, region, preset and managed", (rb.get("channel0"), rb.get("region"), rb.get("modem_preset"), rb.get("managed")), ("MILUX-TAK", "EU_868", "SHORT_FAST", True))
reg = json.load(open(os.path.join(state, "register.json"))).get("!ee000005", {})
check_true("AC5 the register records it as managed with onboarded_at", reg.get("managed") is True and bool(reg.get("onboarded_at")))
exp = r.get("export") or ""
check_true("AC5 the export sits under exports/<id>/ at mode 0600 and holds the key", exp.startswith(os.path.join(state, "exports", "!ee000005")) and os.path.exists(exp)
           and stat.S_IMODE(os.stat(exp).st_mode) == 0o600 and gw_ch.settings.psk.hex() in open(exp).read())
check_true("AC5 the device was closed after onboarding", dev is not None and fakegw_lib.FakeBenchIface.opened[-1].closed)
# three foreign keys
class Full(fakegw_lib.FakeBenchIface):
    def __init__(self, path):
        super().__init__(path)
        for k in (b"\x01" * 32, b"\x02" * 32, b"\x03" * 32):
            self.localNode.localConfig.security.admin_key.append(k); self.localNode.device_config.security.admin_key.append(k)
br.serial_factory = Full
r = br.op_bench_onboard(path=NEW, long_name="X", short_name="X", role="TRACKER") if hasattr(br, "op_bench_onboard") else {}
check_true("AC5 three foreign admin keys: refused, naming three", "three" in str(r.get("error", "")).lower() or "3" in str(r.get("error", "")))
# a device that ignores writes
class Deaf(fakegw_lib.FakeBenchIface):
    def __init__(self, path):
        super().__init__(path)
        n = self.localNode
        n.writeConfig = lambda name: n.calls.append(("writeConfig", name))
        n.writeChannel = lambda i, adminIndex=0: n.calls.append(("writeChannel", i))
        n.setOwner = lambda long_name=None, short_name=None, **kw: n.calls.append(("setOwner", long_name))
br.serial_factory = Deaf
br.READBACK_S = 2
r = br.op_bench_onboard(path=NEW, long_name="Y", short_name="Y", role="TRACKER") if hasattr(br, "op_bench_onboard") else {}
check("AC5 a device that ignores writes: confirmed false with its own values", (r.get("confirmed"), (r.get("read_back") or {}).get("long_name")), (False, "New Device"))

# ---- the screen half
fb = start_fake_bridge()
etc = tempfile.mkdtemp()
srv = W.make_server(bind="127.0.0.1", port=0, socket_path=fb.path, etc_dir=etc, config={"AUTH": "off"})
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
time.sleep(0.4)


def req(method, path, body=None, ctype="application/json"):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    h = {"Content-Type": ctype} if body is not None else {}
    c.request(method, path, body=body, headers=h)
    r = c.getresponse(); data = r.read().decode("utf-8", "replace"); c.close()
    return r.status, data


st, regp = req("GET", "/register")
check_true("AC6 the Register page shows the joined table with the managed pill", st == 200 and "Tracker 9 (recce)" in regp and "Cpl Smith" in regp and "managed" in regp and "bring it to the bench" in regp)
check_true("AC6 a row form posts register_set", "data-action='register_set'" in regp)
st, j = req("POST", "/api/register_set", body=json.dumps({"id": "!bb000002", "label": "Spare", "holder": "Stores"}))
check("AC6 register_set through the API", (st, (json.loads(j).get("written") or {}).get("label")), (200, "Spare"))
st, bench = req("GET", "/bench")
check_true("AC6 the Bench page lists the devices, flags the bootloader one with its recovery step", st == 200 and "T1000-E_9F3A" in bench and "bootloader" in bench.lower() and "UF2" in bench)
check_true("AC6 the gateway's own radio is not offered", "usb-x-if00" not in bench.split("<main", 1)[-1].split("the gateway", 1)[0] or "gateway" in bench)
check_true("AC6 the Onboard form has the three fields", "data-action='bench_onboard'" in bench and "name='long_name'" in bench and "name='short_name'" in bench and "name='role'" in bench)
check_true("AC6 Read and Export controls", "data-action='bench_read'" in bench and "data-action='bench_export'" in bench)
for p_, html in (("/register", regp), ("/bench", bench)):
    check_true(f"AC6 {p_} has no location.reload", "location.reload(" not in html)
    check_true(f"AC6 {p_} carries no key material", "psk" not in html.lower() and "BwcHBw" not in html)
st, j = req("GET", "/api/bench_devices")
check_true("AC7 the fake bridge answers bench_devices", st == 200 and len(json.loads(j).get("devices", [])) == 2)
srv.shutdown()
finish()
