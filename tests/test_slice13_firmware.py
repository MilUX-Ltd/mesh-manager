#!/usr/bin/env python3
"""Spec 010: restore and firmware on the bench. Bridge half on the fake gateway with a fake bench
device and a fake block layer; screen half on the fake bridge; the installer in dry run."""
import http.client
import json
import os
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(__file__))
from _common import ROOT, check, check_true, finish, skip  # noqa: E402
sys.path.insert(0, os.path.join(ROOT, "src"))
import fakegw_lib  # noqa: E402
fakegw_lib.install()
from fakebridge_lib import start_fake_bridge  # noqa: E402
from mesh_manager import bridge as B, catalogue as C, web as W  # noqa: E402

want = {"bench_exports": "read", "bench_restore": "change", "firmware_shelf": "read", "bench_flash": "flash"}
check("AC1 the four actions with their risks", {k: (C.by_id(k) or {}).get("risk") for k in want}, want)
check("AC1 flash is a risk class with floor act", C.FLOOR.get("flash"), "act")
check("AC1 parity holds", C.parity_problems(C.ACTIONS, W.api_action_routes(), [t["name"] for t in W.mcp_tools("act")]), [])

state = tempfile.mkdtemp()
byid = tempfile.mkdtemp()
GW = os.path.join(byid, "usb-Espressif_USB_JTAG_serial_debug_unit_A4:CB:8F:EE:00:01-if00")
NEW = os.path.join(byid, "usb-Seeed_T1000-E_9F3A-if00")
for pth in (GW, NEW):
    open(pth, "w").close()
br = B.Bridge({"SERIAL": GW}, socket_path=os.path.join(state, "b.sock"), state_dir=state, observe=True)
br.interface.localNode.localConfig.security.public_key = b"\x07" * 32
br.serial_dir = byid
br.bootloader_check = lambda path: False
br.serial_factory = fakegw_lib.FakeBenchIface
br.READBACK_S = 2

# ---- AC2 export, change, restore
r = br.op_bench_export(path=NEW)
exp = r.get("export")
ls = br.op_bench_exports(id="!ee000005") if hasattr(br, "op_bench_exports") else {}
check("AC2 bench_exports lists the export with size and time, no content", [(x.get("path") == exp, x.get("bytes", 0) > 100, bool(x.get("when")), "psk" in json.dumps(x)) for x in ls.get("exports", [])], [(True, True, True, False)])
# the device changes: names and slot 0's name; the security section stays the device's own
dev = fakegw_lib.FakeBenchIface.opened[-1].localNode
dev.device_owner.long_name = "Changed"; dev.device_channels[0].settings.name = "Other"
dev.device_config.security.admin_key.append(b"\x05" * 32)
keys_before = [bytes(k) for k in dev.device_config.security.admin_key]
class Persist(fakegw_lib.FakeBenchIface):
    """the same physical device across opens"""
    def __init__(self, path):
        super().__init__(path)
        self.localNode = dev
br.serial_factory = Persist
r = br.op_bench_restore(path=NEW, export=exp) if hasattr(br, "op_bench_restore") else {}
check("AC2 restore writes the export and reads it back", (r.get("confirmed"), dev.device_owner.long_name, dev.device_channels[0].settings.name), (True, "New Device", "LongFast"))
check("AC2 the security section is the device's own, untouched", [bytes(k) for k in dev.device_config.security.admin_key], keys_before)
_psk = dev.device_channels[0].settings.psk.hex()
# A one-byte psk is Meshtastic's index of a well-known key, not key material, and its hex ("01") sits
# inside any date the answer carries, which is why the check failed on one interpreter and not the
# other. Only a real key (16 bytes or more) can leak, so only a real key is looked for by value.
check_true("AC2 the answer holds no key material", "psk" not in json.dumps(r) and (len(_psk) < 32 or _psk not in json.dumps(r)), f"psk bytes {len(_psk) // 2}")
# an export from another id
other_dir = os.path.join(state, "exports", "!dd000004"); os.makedirs(other_dir, exist_ok=True)
other = os.path.join(other_dir, "2026-09-01T00-00-00Z.json")
json.dump(dict(json.load(open(exp)), id="!dd000004"), open(other, "w"))
r = br.op_bench_restore(path=NEW, export=other) if hasattr(br, "op_bench_restore") else {}
check_true("AC2 an export from another id is refused naming both", "!dd000004" in str(r.get("error", "")) and "!ee000005" in str(r.get("error", "")))
r = br.op_bench_restore(path=NEW, export=other, confirm="!ee000005") if hasattr(br, "op_bench_restore") else {}
check("AC2 ...unless confirm names the device (a deliberate clone)", r.get("confirmed"), True)
r = br.op_bench_restore(path=NEW, export="/etc/passwd") if hasattr(br, "op_bench_restore") else {}
check_true("AC2 an export outside the exports directory is refused", "export" in str(r.get("error", "")).lower())

# ---- AC3 the shelf
fw = os.path.join(state, "firmware"); os.makedirs(fw, exist_ok=True)
pins = json.load(open(os.path.join(ROOT, "firmware", "PINS.json")))
t1000 = next(i for i in pins["images"] if i["id"] == "t1000e-2.6.11")
import hashlib
good = b"UF2\n" + b"\x00" * 100
t1000_path = os.path.join(fw, t1000["file"])
open(t1000_path, "wb").write(good)
br.pins = dict(pins, images=[dict(i, sha256=hashlib.sha256(good).hexdigest(), bytes=len(good)) if i["id"] == "t1000e-2.6.11" else i for i in pins["images"]])
wrong = next(i for i in pins["images"] if i["id"] == "heltec-v4-2.7.26")
open(os.path.join(fw, wrong["file"]), "wb").write(b"not the image")
sh = br.op_firmware_shelf() if hasattr(br, "op_firmware_shelf") else {}
states = {i["id"]: i.get("state") for i in sh.get("images", [])}
check("AC3 verified, wrong and missing against the box's directory", (states.get("t1000e-2.6.11"), states.get("heltec-v4-2.7.26"), states.get("factory-erase-s140-7.3.0")), ("verified", "wrong", "missing"))
check_true("AC3 a missing pin names the path to fill", any(i.get("state") == "missing" and i.get("path", "").startswith(fw) for i in sh.get("images", [])))

# ---- AC4 refusals
calls = []
br.flash_hooks = {"touch": lambda path: calls.append(("touch", path)), "wait_volume": lambda label, timeout: calls.append(("wait_volume", label)) or "/dev/sdz1",
                  "mount": lambda dev_: calls.append(("mount", dev_)) or os.path.join(state, "vol"), "copy": lambda src, mnt: calls.append(("copy", os.path.basename(src))),
                  "unmount": lambda dev_: calls.append(("unmount", dev_)), "wait_port": lambda path, timeout: calls.append(("wait_port", path)) or True,
                  "esptool": lambda args: calls.append(("esptool", args)) or (0, "ok"), "has_esptool": lambda: True}
r = br.op_bench_flash(path=NEW, image="t1000e-2.6.11") if hasattr(br, "op_bench_flash") else {}
check_true("AC4 without confirm: refused naming the device", "!ee000005" in str(r.get("error", "")))
r = br.op_bench_flash(path=NEW, image="heltec-v4-2.7.26", confirm="!ee000005") if hasattr(br, "op_bench_flash") else {}
check_true("AC4 a pin for other hardware is refused naming both", "HELTEC_V4" in str(r.get("error", "")) and "TRACKER_T1000_E" in str(r.get("error", "")))
r = br.op_bench_flash(path=NEW, image="factory-erase-s140-7.3.0", confirm="!ee000005") if hasattr(br, "op_bench_flash") else {}
check_true("AC4 a missing image is refused naming the path", "missing" in str(r.get("error", "")).lower())
r = br.op_bench_flash(path=GW, image="t1000e-2.6.11", confirm="!ee000021") if hasattr(br, "op_bench_flash") else {}
check_true("AC4 the gateway's own path is refused", "Radio page" in str(r.get("error", "")))
check("AC4 nothing was touched by a refusal", calls, [])

# ---- AC5 the UF2 flow on the fake block layer
dev.device_firmware = "2.6.11"
r = br.op_bench_flash(path=NEW, image="t1000e-2.6.11", confirm="!ee000005") if hasattr(br, "op_bench_flash") else {}
check("AC5 the stages in order", [c[0] for c in calls], ["touch", "wait_volume", "mount", "copy", "unmount", "wait_port"])
check("AC5 the export first, then confirmed with the version read back", (bool(r.get("export")) and os.path.exists(r.get("export", "")), r.get("confirmed"), r.get("version")), (True, True, "2.6.11"))
check_true("AC5 the stages are in the answer", r.get("stages", [])[:2] == ["exported", "in bootloader"] and "version read" in r.get("stages", []))
calls.clear()
br.flash_hooks["wait_volume"] = lambda label, timeout: None
r = br.op_bench_flash(path=NEW, image="t1000e-2.6.11", confirm="!ee000005") if hasattr(br, "op_bench_flash") else {}
check_true("AC5 a volume that never appears: unconfirmed with the recovery step, never confirmed", r.get("confirmed") is False and "factory-erase" in str(r.get("unconfirmed", "")) and "copy" not in [c[0] for c in calls])

# ---- AC6 esptool
class Heltec(fakegw_lib.FakeBenchIface):
    def __init__(self, path):
        super().__init__(path)
        self.localNode = dev
    def getMyNodeInfo(self):
        i = super().getMyNodeInfo(); i["user"]["hwModel"] = "HELTEC_V4"; return i
br.serial_factory = Heltec
hv4 = os.path.join(fw, wrong["file"]); open(hv4, "wb").write(b"image!")
br.pins = dict(br.pins, images=[dict(i, sha256=hashlib.sha256(b"image!").hexdigest(), bytes=6) if i["id"] == "heltec-v4-2.7.26" else i for i in br.pins["images"]])
calls.clear(); dev.device_firmware = "2.7.26"
r = br.op_bench_flash(path=NEW, image="heltec-v4-2.7.26", confirm="!ee000005") if hasattr(br, "op_bench_flash") else {}
esp = next((c[1] for c in calls if c[0] == "esptool"), [])
check_true("AC6 esptool carries the chip, the port, the offset and the verified file", "esp32s3" in esp and NEW in esp and "0x10000" in esp and hv4 in esp)
check("AC6 the version read back afterwards", (r.get("confirmed"), r.get("version")), (True, "2.7.26"))
br.flash_hooks["has_esptool"] = lambda: False
r = br.op_bench_flash(path=NEW, image="heltec-v4-2.7.26", confirm="!ee000005") if hasattr(br, "op_bench_flash") else {}
check_true("AC6 without esptool: refused naming it", "esptool" in str(r.get("error", "")))

# ---- AC7 the screen
fb = start_fake_bridge()
etc = tempfile.mkdtemp()
srv = W.make_server(bind="127.0.0.1", port=0, socket_path=fb.path, etc_dir=etc, config={"AUTH": "off"})
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
time.sleep(0.4)
c = http.client.HTTPConnection("127.0.0.1", port, timeout=10); c.request("GET", "/bench"); rr = c.getresponse(); bench = rr.read().decode(); c.close()
check_true("AC7 the Shelf shows verified and missing pins", "Shelf" in bench and "verified" in bench and "missing" in bench and "firmware-tracker-t1000-e-2.6.11" in bench)
check_true("AC7 Restore and Flash forms with the tick naming the device", "data-action='bench_restore'" in bench and "data-action='bench_flash'" in bench and "confirm_tick" in bench)
check_true("AC7 the tracker's flash confirm names the GPS regression", "GPS" in bench)
check_true("AC7 no key material, no reload", "psk" not in bench.lower() and "location.reload(" not in bench)
srv.shutdown()

# ---- AC8 the installer and the cut
root = tempfile.mkdtemp()
for d in ("opt/tak", "etc/systemd/system", "dev/serial/by-id"):
    os.makedirs(os.path.join(root, d), exist_ok=True)
open(os.path.join(root, "dev/serial/by-id/usb-x-if00"), "w").close()
out = subprocess.run(["bash", os.path.join(ROOT, "install", "install.sh"), "/nonexistent.tgz", "--serial", "/dev/serial/by-id/usb-x-if00", "--filter-group", "MilUX", "--dry-run"],
                     capture_output=True, text=True, env=dict(os.environ, MESH_MANAGER_ROOT=root)).stdout
check_true("AC8 the installer would install the udisks polkit rule for the bridge's user", "polkit" in out.lower() and "mesh-manager" in out)
check_true("AC8 ...and create the firmware directory", "firmware" in out)
check_true("AC8 ...and restart the bridge when the release is new", "release is new" in out)
_cutp = os.path.join(ROOT, "release", "cut-release.sh")
cut = open(_cutp).read() if os.path.exists(_cutp) else ""
if not cut:
    skip("the cut carries the firmware pins", "the release tooling is not in this tree")
if cut:
    check_true("AC8 the cut carries esptool and its dependencies", "esptool==" in cut and "reedsolo==" in cut and "ecdsa==" in cut and "bitstring==" in cut and "cryptography==" in cut and "intelhex==" in cut)
finish()
