#!/usr/bin/env python3
"""Spec 112: Devices and channels, Add a device.

Three routes on one tab: a tracker by USB, a phone by the channel code, and a code somebody gave you.
The USB route is the bench's onboarding, which already writes and reads back, with three faults under
it: the configuration export the confirm promises "first" is taken after the writes, so it holds the
new settings rather than the old; the label and holder the catalogue takes are computed and dropped;
and the screen hears one answer at the end rather than each step as the device confirms it. The code
route decodes names but not roles, and says nothing when the code is for a different mesh; adopting
with Replace checks the channel names came back and not the region or preset.

Not here, on purpose (1.5.1): the channel code image fetched before Show, and the decode
asked by GET with the code in the query.

Every block is guarded: a missing helper is one verdict, not a traceback hiding the rest.
"""
import base64, http.client, json, os, re, sys, tempfile, threading, time
sys.path.insert(0, os.path.dirname(__file__))
from _common import ROOT, check, check_true, finish  # noqa: E402
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "tests"))
import fakegw_lib  # noqa: E402
fakegw_lib.install()
import fakebridge_lib  # noqa: E402
from mesh_manager import bridge as B, catalogue as C, web as W  # noqa: E402
from meshtastic.protobuf import apponly_pb2  # noqa: E402

# ---- the bridge half, on the fake gateway with a fake tracker on a by-id path --------------------------
state, byid = tempfile.mkdtemp(), tempfile.mkdtemp()
GW = os.path.join(byid, "usb-Espressif_USB_JTAG_serial_debug_unit_A4:CB:8F:EE:00:01-if00")
NEW = os.path.join(byid, "usb-Seeed_T1000-E_9F3A-if00")
for pth in (GW, NEW):
    open(pth, "w").close()
br = B.Bridge({"SERIAL": GW}, socket_path=os.path.join(state, "b.sock"), state_dir=state, observe=True)
OURS = b"\x07" * 32
br.interface.localNode.localConfig.security.public_key = OURS
br.serial_dir = byid
br.bootloader_check = lambda path: False
br.serial_factory = fakegw_lib.FakeBenchIface
events = []
br._emit = lambda kind=None, **k: events.append(dict(k, kind=kind))
GW_PSK = bytes(br.interface.localNode.channels[0].settings.psk)

gid = ((br.op_group_set(name="Trainees", colour="node-3") or {}).get("group") or {}).get("id")
check_true("setup: a declared group with a stable id", bool(gid), "")

events.clear()
r = br.op_bench_onboard(path=NEW, long_name="Trainee 12", short_name="T12", role="TRACKER",
                        label="Trainee 12 (A Coy)", holder="OCdt Jones", group="Trainees")
onb = [e for e in events if e.get("kind") == "onboard"]
steps = [e.get("step") for e in onb]

# ---- AC1 the old settings are saved before anything is written -------------------------------------------
exp = r.get("export") or ""
doc = json.load(open(exp)) if exp and os.path.exists(exp) else {}
check("AC1 the export holds the device's names as they were", ((doc.get("owner") or {}).get("long_name")), "New Device")
check("AC1 and its channel as it was", [c.get("name") for c in (doc.get("channels") or [])][:1], ["LongFast"])
check("AC1 the first step reported is the export, before any write", steps[:1], ["exported"])

# ---- AC2 each step is reported as the device reads it back -------------------------------------------------
WANT = ["exported", "names", "role", "channel", "lora", "admin_key", "group"]
check("AC2 every step, in order", steps, WANT)
check_true("AC2 each one confirmed from the device's own answer",
           len(onb) == len(WANT) and all(e.get("confirmed") is True for e in onb), json.dumps(onb)[:300])
check("AC2 the answer is still confirmed as a whole", r.get("confirmed"), True)
_blob = json.dumps(onb) + json.dumps(r)
check_true("AC2 no step and no answer carries key material",
           GW_PSK.hex() not in _blob and OURS.hex() not in _blob and base64.b64encode(GW_PSK).decode()[:12] not in _blob
           and "psk" not in _blob, "")


class Deaf(fakegw_lib.FakeBenchIface):
    """A tracker that takes the export read and then ignores every write."""
    def __init__(self, path):
        super().__init__(path)
        n = self.localNode
        n.writeConfig = lambda name: n.calls.append(("writeConfig", name))
        n.writeChannel = lambda i, adminIndex=0: n.calls.append(("writeChannel", i))
        n.setOwner = lambda long_name=None, short_name=None, **kw: n.calls.append(("setOwner", long_name))


br.serial_factory = Deaf
br.READBACK_S = 2
events.clear()
r2 = br.op_bench_onboard(path=NEW, long_name="Trainee 13", short_name="T13", role="TRACKER", group="Trainees")
onb2 = [e for e in events if e.get("kind") == "onboard"]
check("AC2 a tracker that ignores writes: the names step says not confirmed, and it stops there",
      [(e.get("step"), e.get("confirmed")) for e in onb2], [("exported", True), ("names", False)])
check("AC2 the answer is not confirmed", r2.get("confirmed"), False)
check_true("AC2 and names the step that did not read back", "names" in str(r2.get("unconfirmed") or ""), str(r2.get("unconfirmed")))
br.serial_factory = fakegw_lib.FakeBenchIface

# ---- AC3 the name, label, holder and group are stored ----------------------------------------------------------
reg = json.load(open(os.path.join(state, "register.json"))).get("!ee000005", {}) if os.path.exists(os.path.join(state, "register.json")) else {}
check("AC3 the label given is the label kept", reg.get("label"), "Trainee 12 (A Coy)")
check("AC3 so is the holder", reg.get("holder"), "OCdt Jones")
check("AC3 the group is held by its stable id", reg.get("group"), gid)
_inputs = {i["name"] for i in (C.by_id("bench_onboard") or {}).get("inputs", [])}
check_true("AC3 the catalogue's onboarding takes a group", "group" in _inputs, str(sorted(_inputs)))
check("AC3 parity across routes, forms and tools holds", C.parity_problems(C.ACTIONS, W.api_action_routes(), [t["name"] for t in W.mcp_tools("act")]), [])

# ---- AC5 a code you were given: what it holds, and whether it is this mesh -----------------------------------
def code(chs, region=3, preset=6):
    cs = apponly_pb2.ChannelSet()
    for name, psk in chs:
        s = cs.settings.add(); s.name = name; s.psk = psk
    cs.lora_config.region = region; cs.lora_config.modem_preset = preset
    return "https://meshtastic.org/e/#" + base64.urlsafe_b64encode(cs.SerializeToString()).decode().rstrip("=")


KEY = b"\x5a" * 32
other = code([("BRIZE-NET", KEY), ("SECOND", KEY)], region=3, preset=0)
same = code([("MILUX-TAK", KEY)], region=3, preset=6)
d = br.op_channel_decode(url=other)
check("AC5 decode names the channels", d.get("channels"), ["BRIZE-NET", "SECOND"])
check("AC5 and their roles, primary first", d.get("roles"), ["PRIMARY", "SECONDARY"])
check("AC5 and the region and preset", (d.get("region"), d.get("modem_preset")), ("EU_868", "LONG_FAST"))
check_true("AC5 and never the key", KEY.hex() not in json.dumps(d) and base64.b64encode(KEY).decode()[:12] not in json.dumps(d)
           and "psk" not in json.dumps(d), "")
check("AC5 it says what differs from this radio: the channel and the preset, not the region",
      sorted(d.get("differs") or []) if isinstance(d.get("differs"), list) else d.get("differs"), ["channel", "preset"])
check("AC5 a code for this mesh differs in nothing", br.op_channel_decode(url=same).get("differs"), [])

node = br.interface.localNode


def apply_url(lora=True):
    def setURL(url, addOnly=False):
        node.calls.append(("setURL", addOnly))
        frag = url.split("#", 1)[1]
        cs = apponly_pb2.ChannelSet(); cs.ParseFromString(base64.urlsafe_b64decode(frag + "=" * (-len(frag) % 4)))
        for i, s in enumerate(cs.settings):
            for ch in (node.channels[i], node.device_channels[i]):
                ch.index = i; ch.role = 1 if i == 0 else 2; ch.settings.name = s.name; ch.settings.psk = s.psk
        if lora:
            for cfg in (node.localConfig, node.device_config):
                cfg.lora.region = cs.lora_config.region; cfg.lora.modem_preset = cs.lora_config.modem_preset
    return setURL


own = (br._own() or {}).get("id")
node.setURL = apply_url(lora=True)
r = br.op_channel_adopt(url=other, mode="replace")
check_true("AC5 Replace without this radio's id is refused, naming it", bool(r.get("needs_confirm")) and own in str(r.get("error")), json.dumps(r)[:200])
r = br.op_channel_adopt(url=other, mode="replace", confirm=own)
check("AC5 Replace, confirmed, reads back names, region and preset", r.get("confirmed"), True)
for _cfg in (node.localConfig, node.device_config):   # back on SHORT_FAST: the confirmed Replace above moved it to LONG_FAST (Matt, 30 Sep 2026)
    _cfg.lora.modem_preset = 6
node.setURL = apply_url(lora=False)   # the radio takes the channels and keeps its old preset
r = br.op_channel_adopt(url=code([("BRIZE-NET", KEY)], region=3, preset=0), mode="replace", confirm=own)
check("AC5 a Replace whose preset did not land is not confirmed", r.get("confirmed"), False)

# ---- AC4 and AC6 the tab, through the web layer with the fake bridge --------------------------------------------
fakebridge_lib.NODES[0]["group"] = "Trainees"
fb = fakebridge_lib.start_fake_bridge()
srv = W.make_server(bind="127.0.0.1", port=0, socket_path=fb.path, etc_dir=tempfile.mkdtemp(),
                    config={"AUTH": "off"}, state_dir=tempfile.mkdtemp())
threading.Thread(target=srv.serve_forever, daemon=True).start()
time.sleep(0.3)


def get(path):
    c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
    c.request("GET", path)
    resp = c.getresponse()
    out = (resp.status, resp.read().decode())
    c.close()
    return out


code_, page = get("/devices/add")
check("AC4 the Add a device tab answers", code_, 200)
check_true("AC4 USB: the onboarding form", "data-action='bench_onboard'" in page, "")
check_true("AC4 USB: the name is asked up front", "name='long_name'" in page, "")
_grp = re.search(r"<select[^>]*name='group'[\s\S]*?</select>", page)
check_true("AC4 USB: a group chosen from the groups, or none",
           _grp is not None and "Trainees" in _grp.group(0) and re.search(r"no group", _grp.group(0), re.I) is not None,
           _grp.group(0)[:200] if _grp else "no group select")
check_true("AC4 USB: each step is shown as the device reads it back", re.search(r"kind\s*===?\s*['\"]onboard['\"]", page) is not None, "")
check_true("AC4 phone: it is called the Channel code", "Channel code" in page, "")
check_true("AC4 phone: with the warning that it carries the key",
           re.search(r"anyone who scans it can read and send on this mesh", page, re.I) is not None, "")
check_true("AC4 phone: shown as the QR the Channels page shows", "/channels/qr.png" in page, "")
check_true("AC4 phone: the sheet closes itself after 60 s (D9)", re.search(r"left\s*=\s*60\b", page) is not None, "")
check_true("AC4 phone: never as text anyone could copy",
           "SECRET-URL-WITH-KEY" not in page and "meshtastic.org/e/#" not in page, "the join URL is in the page")
check_true("AC4 a code: pasted, read, then added beside or replacing",
           "data-action='channel_adopt'" in page and "value='add'" in page and "value='replace'" in page, "")
check_true("AC4 a code: replacing is confirmed by this radio's id",
           (fakebridge_lib.STATUS.get("own") or {}).get("id", "?") in page, "")
check_true("AC4 a code: it warns when the code is not this mesh", re.search(r"will not hear this mesh", page, re.I) is not None, "")

_mode = fakebridge_lib.STATUS["mode"]
fakebridge_lib.STATUS["mode"] = "hub"
try:
    code_h, hub = get("/devices/add")
finally:
    fakebridge_lib.STATUS["mode"] = _mode
check("AC6 on a hub the tab still answers", code_h, 200)
check_true("AC6 and says adding a device needs a radio next to it",
           re.search(r"needs a radio next to it", hub, re.I) is not None and re.search(r"laptop or (a )?box", hub, re.I) is not None, "")
check_true("AC6 and offers no radio route",
           "data-action='bench_onboard'" not in hub and "/channels/qr.png" not in hub and "data-action='channel_adopt'" not in hub,
           "a radio route is on a hub's tab")

finish()
