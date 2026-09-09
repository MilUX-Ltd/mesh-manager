#!/usr/bin/env python3
"""Spec 072: turn a radio's MQTT on and off from the screen, over the air.

A radio carries its own MQTT settings and a paired phone reads them off it, so the settings have to
reach every radio. Until the fleet was given the gateways' admin keys that meant a cable.
"""
import os, sys, tempfile
sys.path.insert(0, os.path.dirname(__file__))
from _common import ROOT, check, check_true, finish  # noqa: E402
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "tests"))
import fakegw_lib  # noqa: E402
fakegw_lib.install()
from mesh_manager import bridge as B  # noqa: E402

state = tempfile.mkdtemp(); byid = tempfile.mkdtemp()
GW = os.path.join(byid, "usb-Espressif_USB_JTAG_serial_debug_unit_A4:CB:8F:EE:00:01-if00")
open(GW, "w").close()
br = B.Bridge({"SERIAL": GW}, socket_path=os.path.join(state, "b.sock"), state_dir=state,
              observe=True, gps_reader=False)
br.serial_dir = byid
br.bootloader_check = lambda path: False

OURS = b"\x11" * 32
br.interface.localNode.localConfig.security.public_key = OURS
NID = "!ee000072"
fakegw_lib.FakeRemoteNode.devices.clear()
br.node_factory = lambda nid: fakegw_lib.FakeRemoteNode(br.interface, nid, ours=OURS)

# the box has read this node and knows it is managed
reg = br._register_load()
reg[NID] = {"name": "Tracker", "managed": True}
br._register_save(reg)

ARGS = dict(id=NID, address="tak.milux.co.uk", username="matt", password="a-secret",
            root="milux", tls="on", enabled="on", confirm="yes")

# AC6: confirm is required before anything is written
no_confirm = dict(ARGS); no_confirm.pop("confirm")
out = br.op_node_mqtt_set(**no_confirm)
check_true("AC6 it refuses without confirm", "error" in out, repr(out)[:120])

# AC1 to AC4: the write, read back from the device itself
out = br.op_node_mqtt_set(**ARGS)
rb = out.get("read_back", {})
check("AC1 the address reached the radio", rb.get("address"), "tak.milux.co.uk")
check("AC1 confirmed against the device's own answer", out.get("confirmed"), True)
check("AC2 the client proxy is on", rb.get("proxy_to_client_enabled"), True)
check("AC2 encryption is on, never plaintext to a broker", rb.get("encryption_enabled"), True)
check("AC2 JSON stays off", rb.get("json_enabled"), False)
check("AC3 config_ok_to_mqtt is set in the same call", rb.get("config_ok_to_mqtt"), True)
check("AC3 and ignore_mqtt is cleared", rb.get("ignore_mqtt"), False)
check_true("AC4 the password is never echoed", "a-secret" not in repr(out), repr(out)[:200])
check("AC4 it says a password is set, not what it is", rb.get("password_set"), True)

# AC5: off means off
out = br.op_node_mqtt_set(id=NID, address="tak.milux.co.uk", enabled="off", confirm="yes")
check("AC5 enabled=off turns MQTT off on that radio", out.get("read_back", {}).get("enabled"), False)

# AC7: a node the box has never read
out = br.op_node_mqtt_set(id="!ee000099", address="x", confirm="yes")
check_true("AC7 an unmanaged node is refused", "error" in out, repr(out)[:140])
check_true("AC7 and the message names the read that would fix it",
           "read" in (out.get("error") or "").lower(), repr(out.get("error")))

# AC8: a radio that does not answer is unconfirmed, never a silent success
fakegw_lib.FakeRemoteNode.device(NID).readback_delay = None
out = br.op_node_mqtt_set(**ARGS)
check("AC8 a silent radio is not reported as confirmed", out.get("confirmed"), False)
check_true("AC8 and it says so", bool(out.get("unconfirmed")), repr(out.get("unconfirmed")))

finish()
