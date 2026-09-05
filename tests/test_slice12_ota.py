#!/usr/bin/env python3
"""Spec 011: over the air. Bridge half on the fake gateway with fake remote nodes; screen half on
the fake bridge."""
import http.client
import json
import os
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

want = {"node_read": "read", "node_set": "change", "node_set_region": "unreachable", "node_channel_push": "change", "node_reboot": "change"}
check("AC1 the five actions with their risks", {k: (C.by_id(k) or {}).get("risk") for k in want}, want)
check_true("AC1 each takes a node id", all(any(i["type"] == "node" and i["name"] == "id" for i in (C.by_id(k) or {}).get("inputs", [])) for k in want))
check("AC1 parity holds", C.parity_problems(C.ACTIONS, W.api_action_routes(), [t["name"] for t in W.mcp_tools("act")]), [])

state = tempfile.mkdtemp()
br = B.Bridge({"SERIAL": ""}, socket_path=os.path.join(state, "b.sock"), state_dir=state, observe=True)
OURS = b"\x07" * 32
br.interface.localNode.localConfig.security.public_key = OURS
br.READBACK_S = 2
MANAGED, UNMANAGED = "!aa000001", "!bb000002"
br.meshtastic_devices[UNMANAGED] = {"long_name": "Stranger", "meshtastic_id": UNMANAGED}
fakegw_lib.FakeRemoteNode.device(MANAGED, OURS)
fakegw_lib.FakeRemoteNode.device(UNMANAGED, None)
br.node_factory = lambda nid: fakegw_lib.FakeRemoteNode(br.interface, nid)
# the register says which is managed, as a bench read would have
br._register_note({"id": MANAGED, "long_name": "Tracker9", "managed": True, "hw": "TRACKER_T1000_E", "firmware": "2.6.11", "role": "TRACKER"})
br._register_note({"id": UNMANAGED, "long_name": "Stranger", "managed": False})

# AC2 read over the air
r = br.op_node_read(id=MANAGED) if hasattr(br, "op_node_read") else {}
dev = fakegw_lib.FakeRemoteNode.device(MANAGED)
check("AC2 names, region, preset, role, interval from the device", (r.get("long_name"), r.get("short_name"), r.get("region"), r.get("modem_preset"), r.get("role"), r.get("position_broadcast_secs")),
      (dev.device_owner.long_name, dev.device_owner.short_name, B.region_name(dev.device_config.lora.region), B.preset_name(dev.device_config.lora.modem_preset), B.role_name(dev.device_config.device.role), int(dev.device_config.position.position_broadcast_secs)))
check("AC2 managed, from the device's own security config", (r.get("managed"), r.get("admin_keys")), (True, 1))
check("AC2 channels without keys", [(c.get("index"), c.get("name"), "psk" in c or "_psk" in c) for c in r.get("channels", [])][:1], [(0, "MILUX-TAK", False)])
reg = br._register_load().get(MANAGED, {})
check_true("AC2 the register refreshed from the answer", reg.get("managed") is True and bool(reg.get("seen_on_air")))

# AC3 node_set
dev.session_calls = 0
r = br.op_node_set(id=MANAGED, long_name="Tracker Nine", position_broadcast_secs=600) if hasattr(br, "op_node_set") else {}
check("AC3 the session passkey was asked for before the write", dev.session_calls >= 1, True)
check("AC3 confirmed from the device's answer with the new values", (r.get("confirmed"), (r.get("read_back") or {}).get("long_name"), (r.get("read_back") or {}).get("position_broadcast_secs")), (True, "Tracker Nine", 600))
check("AC3 the device holds them", (dev.device_owner.long_name, int(dev.device_config.position.position_broadcast_secs)), ("Tracker Nine", 600))
# a device that ignores writes: our key removed from its admin keys behind our back
del dev.device_config.security.admin_key[:]
r = br.op_node_set(id=MANAGED, long_name="Nobody") if hasattr(br, "op_node_set") else {}
check("AC3 ignored: unconfirmed with the device's own value", (r.get("confirmed"), (r.get("read_back") or {}).get("long_name"), bool(r.get("unconfirmed"))), (False, "Tracker Nine", True))
dev.device_config.security.admin_key.append(OURS)
r = br.op_node_set(id=UNMANAGED, long_name="Taken") if hasattr(br, "op_node_set") else {}
check_true("AC3 an unmanaged device is refused with the bench hint", "bench" in str(r.get("error", "")).lower())
check("AC3 ...and nothing was sent to it", fakegw_lib.FakeRemoteNode.device(UNMANAGED).device_owner.long_name, "Remote 0002")

# AC4 region needs the device's own id as confirm
r = br.op_node_set_region(id=MANAGED, region="US") if hasattr(br, "op_node_set_region") else {}
check_true("AC4 without confirm: refused naming the device", MANAGED in str(r.get("error", "")))
r = br.op_node_set_region(id=MANAGED, region="US", confirm=MANAGED) if hasattr(br, "op_node_set_region") else {}
check("AC4 with confirm: the device's own answer carries the new region", (r.get("confirmed"), (r.get("read_back") or {}).get("region"), B.region_name(dev.device_config.lora.region)), (True, "US", "US"))

# AC5 channel push
gw = br.interface.localNode
gw.channels[1].settings.name = "RECCE"; gw.channels[1].settings.psk = b"\x09" * 32; gw.channels[1].role = 2
r = br.op_node_channel_push(id=MANAGED, index=1) if hasattr(br, "op_node_channel_push") else {}
check("AC5 the gateway's slot lands on the device and reads back", (r.get("confirmed"), dev.device_channels[1].settings.name, bytes(dev.device_channels[1].settings.psk) == b"\x09" * 32), (True, "RECCE", True))
check_true("AC5 the answer carries no key", "psk" not in json.dumps(r) and (b"\x09" * 32).hex() not in json.dumps(r))
r = br.op_node_channel_push(id=MANAGED, index=0) if hasattr(br, "op_node_channel_push") else {}
check_true("AC5 slot 0 needs the confirm", MANAGED in str(r.get("error", "")))

# AC6 reboot
r = br.op_node_reboot(id=MANAGED) if hasattr(br, "op_node_reboot") else {}
check_true("AC6 reboot needs the confirm", MANAGED in str(r.get("error", "")))
r = br.op_node_reboot(id=MANAGED, confirm=MANAGED) if hasattr(br, "op_node_reboot") else {}
check_true("AC6 the device was asked to reboot, and the answer says asked", bool(r.get("asked")) and "rebooted" not in json.dumps(r))

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


st, page = req("GET", "/register")
row_m = page[page.index("data-id='!aa000001'"):page.index("data-id='!bb000002'")]
row_u = page[page.index("data-id='!bb000002'"):page.index("data-id='!cc000003'")]
check_true("AC7 a managed row carries the Manage forms", "data-action='node_set'" in row_m and "data-action='node_set_region'" in row_m and "data-action='node_channel_push'" in row_m and "data-action='node_reboot'" in row_m)
check_true("AC7 ...with the confirm tick naming the device", "!aa000001" in row_m and "confirm_tick" in row_m)
check_true("AC7 ...and a Read over the air control", "data-action='node_read'" in row_m)
check_true("AC7 an unmanaged row carries the bench hint and no remote form", "bring it to the bench" in row_u and "data-action='node_set'" not in row_u)
check_true("AC7 no key material, no reload", "psk" not in page.lower() and "location.reload(" not in page)
st, j = req("GET", "/api/node_read?id=!aa000001")
check("AC8 the fake bridge answers node_read", (st, json.loads(j).get("managed")), (200, True))
srv.shutdown()
finish()
