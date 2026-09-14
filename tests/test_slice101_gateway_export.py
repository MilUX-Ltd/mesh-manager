#!/usr/bin/env python3
"""Spec 101: keep what a replacement gateway needs.

If the gateway radio dies, the mesh's identity dies with it: the primary channel key is generated
straight onto the radio and never kept, and the one radio bench_export cannot reach is the gateway,
because bench_ports() excludes it by design.

What makes this small is that the restore path already exists. A replacement radio is bench kit
until it is chosen as the gateway, and bench_restore already writes channels with their keys onto
bench kit. The only missing piece is an export of the gateway's own configuration to restore from.

What it deliberately does not fix: a radio's private key cannot be written, so a replacement is a
new identity, and the fleet holds the old gateway's public key as its admin key. The replacement can
see the mesh and manage nothing. That needs a second admin key pushed over the air while the old
gateway still works, and it is its own card. This suite holds the product to saying so.

Every block is guarded.
"""
import datetime as _dt, json, os, re, stat, sys, tempfile, threading
sys.path.insert(0, os.path.dirname(__file__))
from _common import ROOT, check, check_true, finish, read  # noqa: E402
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "tests"))
import fakegw_lib  # noqa: E402
fakegw_lib.install()
from mesh_manager.bridge import Bridge  # noqa: E402

PSK = bytes(range(32))


class FakeNode:
    pass


class FakeIface:
    """Enough of a radio for a snapshot: an owner, the config sections and a primary channel."""
    def __init__(self):
        self.localNode = FakeNode()

    def close(self):
        pass


class B(Bridge):
    def __init__(self, d, attached=True):
        self.state_dir = d
        self.interface = FakeIface() if attached else None
        self.history = None
        self.peering = None
        self.box_mode = "server"
        self.outbox = {}
        self.batteries = {}
        self.remote_alerts = {}
        self._peers_lock = threading.Lock()
        self._bench_lock = threading.Lock()
        self._stop = threading.Event()
        self.logger = type("L", (), {"warning": lambda *a, **k: None, "info": lambda *a, **k: None,
                                     "debug": lambda *a, **k: None, "error": lambda *a, **k: None})()
        self.events = []
        self._emit = lambda kind=None, **k: self.events.append(dict(k, kind=kind))
        self.conf = {"SERIAL": "/dev/serial/by-id/usb-fake-test-radio-if00", "MODE": "server",
                     "CONFIG_PATH": os.path.join(d, "config")}
        open(self.conf["CONFIG_PATH"], "w").write("SERIAL=/dev/serial/by-id/usb-fake-test-radio-if00\nMODE=server\n")
        self.serial_dir = os.path.join(d, "by-id")
        os.makedirs(self.serial_dir, exist_ok=True)
        self._ports_for_test = []

    def _own(self):
        return {"id": "!ee000001"}

    def mesh_nodes(self):
        return []

    def _bench_snapshot(self, iface):
        """What a read of the gateway establishes, in the shape _export expects."""
        snap = {"id": "!ee000001", "hw": "HELTEC_V3", "firmware": "2.7.11", "position": None,
                "long_name": "Gateway", "short_name": "GW", "region": "EU_868",
                "modem_preset": "SHORT_FAST", "role": "CLIENT", "channels": [{"index": 0}]}
        # real protobuf sections, because _export runs MessageToDict over them: a dict here would
        # have made the suite pass on a shape the product never sees.
        from meshtastic.protobuf import config_pb2
        lora = config_pb2.Config.LoRaConfig(region=config_pb2.Config.LoRaConfig.RegionCode.EU_868)
        device = config_pb2.Config.DeviceConfig(role=config_pb2.Config.DeviceConfig.Role.CLIENT)
        security = config_pb2.Config.SecurityConfig(public_key=b"pub", private_key=b"private")
        raw = {"owner": {"long_name": "Gateway", "short_name": "GW"},
               "lora": lora, "device": device, "security": security,
               "channels": [{"index": 0, "name": "MILUX-TAK", "role": "PRIMARY", "_psk": PSK}]}
        return snap, raw

    def _register_note(self, *a, **k):
        pass


def box(attached=True):
    return B(tempfile.mkdtemp(), attached)


HAVE = hasattr(Bridge, "op_gateway_export")
if not HAVE:
    for ac in ("AC1 the gateway's own configuration can be exported",
               "AC2 the file is 0600 and its contents never travel",
               "AC3 a box with no radio refuses rather than writing an empty export",
               "AC4 the export is where bench_restore will accept it",
               "AC5 gateway says whether an export exists",
               "AC6 the screen offers it",
               "AC7 the screen warns when there is none",
               "AC8 the words say what it does not carry",
               "AC9 the guide carries the drill",
               "AC10 no key reaches the answer"):
        check_true(ac, False, "no op_gateway_export on the bridge yet")
    finish()

# ---- AC1, AC2, AC10 the export ----------------------------------------------------------------------
a = box()
r = a.op_gateway_export()
check_true("AC1 the gateway's own configuration can be exported", "error" not in r, json.dumps(r)[:200])
_fn = str(r.get("export") or "")
check_true("AC1 and the answer names the file", bool(_fn) and os.path.exists(_fn), json.dumps(r)[:200])
check_true("AC2 the answer carries its size", isinstance(r.get("bytes"), int) and r["bytes"] > 0, json.dumps(r)[:200])
if _fn and os.path.exists(_fn):
    _mode = stat.S_IMODE(os.stat(_fn).st_mode)
    check("AC2 the file is 0600", oct(_mode), oct(0o600))
    _doc = json.load(open(_fn))
    check_true("AC1 it carries the channel with its key",
               bool(_doc.get("channels")) and _doc["channels"][0].get("psk") == PSK.hex(), json.dumps(_doc)[:300])
    check_true("AC1 and the configuration",
               "config" in _doc and "lora" in (_doc.get("config") or {}), str(sorted(_doc)))
_answer = json.dumps(r)
for secret in (PSK.hex(), "cHJpdmF0ZQ==", "privateKey"):
    check_true(f"AC10 {secret[:12]}... is not in the answer", secret not in _answer, _answer[:200])
check_true("AC10 and no key was emitted to any screen",
           not any(PSK.hex() in json.dumps(ev) for ev in a.events), str(a.events)[:200])

# ---- AC4 where a restore will find it -------------------------------------------------------------------
if _fn:
    _root = os.path.realpath(os.path.join(a.state_dir, "exports"))
    check_true("AC4 the export is under the exports root bench_restore accepts",
               os.path.realpath(_fn).startswith(_root + os.sep), f"{_fn} is not under {_root}")
    check_true("AC4 and it is a .json, which is what bench_restore takes", _fn.endswith(".json"), _fn)

# ---- AC3 a box with no radio ------------------------------------------------------------------------------
b = box(attached=False)
r = b.op_gateway_export()
check_true("AC3 a box with no radio refuses", "error" in r, json.dumps(r)[:200])
check_true("AC3 and says why in words a person can act on",
           re.search(r"(no radio|not attached|plug)", str(r.get("error")), re.I) is not None, str(r.get("error")))
check("AC3 and wrote nothing", os.path.isdir(os.path.join(b.state_dir, "exports")), False)

# ---- AC5 the box says what losing the radio would cost now ---------------------------------------------------
c = box()
g = c.op_gateway()
check("AC5 a box with no export says so", g.get("export_at"), None)
c.op_gateway_export()
g = c.op_gateway()
check_true("AC5 and once taken, says when", bool(g.get("export_at")), json.dumps(g)[:200])
# "says when" has to mean a time somebody can read. The export's filename is the instant with its
# colons swapped for hyphens, and reading it back turned three of the four separators round instead
# of the two that were swapped, so every answer carried a malformed instant the screen could not
# age. The card renders it into a <time datetime=...>, so this is the difference between "kept 20
# minutes ago" and nothing at all. Found in review, 14 Sep 2026.
_at = str(g.get("export_at") or "")
try:
    _parsed = _dt.datetime.fromisoformat(_at.replace("Z", "+00:00"))
except ValueError as _e:
    _parsed = None
check_true("AC5 and the time it gives is a time, not something shaped like one",
           _parsed is not None, f"{_at!r} does not parse as an instant")
check_true("AC5 and it is the time the export was actually taken",
           _parsed is not None and abs((_dt.datetime.now(_dt.timezone.utc) - _parsed).total_seconds()) < 300,
           f"{_at!r} is not near now")

# ---- AC6, AC7, AC8 the screen -------------------------------------------------------------------------------------
web = read("src/mesh_manager/web.py") or ""
check_true("AC6 the screen offers the export", "gateway_export" in web, "nothing on the screen")
# Offering it means a button that exports. The screen has two submit paths: a read goes out as a GET
# to /api/<action>, and everything else is POSTed. The server holds the same line from the other
# side and answers a read on POST with 405, so a read action wearing the POST form is a button that
# can only ever fail. This is a rule about every form on the screen, not about this one, because the
# next read action added to a page would land in exactly the same place.
_risk = dict(re.findall(r'"id":\s*"([a-z0-9_]+)".*?"risk":\s*"([a-z]+)"', read("src/mesh_manager/catalogue.py") or "", re.S))
_wrong = []
for _m in re.finditer(r"<form\b([^>]*?)data-action=['\"]([a-z0-9_]+)['\"]([^>]*)>", web):
    _tag, _act = _m.group(0), _m.group(2)
    if _risk.get(_act) is None:
        continue
    _is_get = "data-method=get" in _tag or "data-method='get'" in _tag
    if (_risk[_act] == "read") != _is_get:
        _wrong.append(f"{_act} is a {_risk[_act]} submitted as {'GET' if _is_get else 'POST'}")
check_true("AC6 and every read action on the screen is submitted the way the server answers it",
           not _wrong, "; ".join(_wrong))
# The GET path writes its answer into a .out container and the POST path into .res. A form that
# changed method and kept the other container says nothing back to the operator.
_ex_form = web[web.find("data-action='gateway_export'") - 200:]
_ex_form = _ex_form[:_ex_form.find("</form>") + 7]
check_true("AC6 and the export form has the container its own path writes into",
           "class='out" in _ex_form, " ".join(_ex_form.split())[:240])
check_true("AC7 and warns when there is none",
           re.search(r"export_at", web) is not None, "the card never looks at whether an export exists")
check_true("AC8 the words say a replacement is a new identity",
           re.search(r"different identity|new identity|not a drop-in|cannot manage", web, re.I) is not None,
           "the screen implies a replacement is a drop-in")

# ---- AC9 the guide --------------------------------------------------------------------------------------------------
guide = read("docs/GUIDE.md") or ""
check_true("AC9 the guide carries the drill", re.search(r"gateway.{0,80}(replace|dies|breaks)", guide, re.I) is not None,
           "no drill in the guide")
for step in ("export", "restore", "choose"):
    check_true(f"AC9 and names the step: {step}", re.search(step, guide, re.I) is not None, f"{step} missing")
check_true("AC9 and says the admin key does not travel",
           re.search(r"admin key", guide, re.I) is not None, "the guide does not mention the admin key")

# ---- AC1 the catalogue -------------------------------------------------------------------------------------------------
cat = read("src/mesh_manager/catalogue.py") or ""
check_true("AC1 the export is in the catalogue", '"id": "gateway_export"' in cat, "no action")
_blk = cat[cat.find('"id": "gateway_export"'):]
_blk = _blk[:_blk.find('{"id": "', 10)] if '{"id": "' in _blk[10:] else _blk[:600]
check_true("AC1 and it is a read, because nothing is written to any radio",
           '"risk": "read"' in _blk, _blk[:200])

finish()
