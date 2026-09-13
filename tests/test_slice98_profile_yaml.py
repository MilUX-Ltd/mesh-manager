#!/usr/bin/env python3
"""Spec 098: profiles in the format the Meshtastic CLI already writes.

Probing the real `meshtastic` 2.7.11 turned up the thing that decides this card: a full
`--export-config` file carries `security.privateKey`, `security.adminKey` and `channel_url`, which
is the channel key. The product's security floor says a key is never rendered, never in a URL,
never in a log line. So the CLI's file and this product's profile are not the same object.

The profile is five fields about how a radio behaves. The owner, the location and the keys are who
a radio is. The export carries the first and never the second; the import reads the first, names
what it ignored, and carries no key anywhere, not even into the report that says what it ignored.

Every block is guarded: a missing helper is one verdict, not a traceback that hides the rest.
"""
import json, os, sys, tempfile
sys.path.insert(0, os.path.dirname(__file__))
from _common import ROOT, check, check_true, finish, read  # noqa: E402
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "tests"))
import fakegw_lib  # noqa: E402
fakegw_lib.install()
from mesh_manager.bridge import Bridge  # noqa: E402

# a real full export, in the CLI's own shape, with the secrets it really carries
FULL = """# start of Meshtastic configure yaml
channel_url: https://meshtastic.org/e/#CgMSAQE6AggB
config:
  device:
    role: TRACKER
    nodeInfoBroadcastSecs: 10800
  lora:
    region: EU_868
    modemPreset: SHORT_FAST
    txPower: 20
    usePreset: true
    hopLimit: 3
  position:
    positionBroadcastSecs: 900
    gpsUpdateInterval: 120
  security:
    privateKey: base64:MFcXvSk3rWQSZMcOMZ0ZZM1sJHhFqpZKQDWQsIP4fVQ=
    publicKey: base64:oSZMcOMZ0ZZM1sJHhFqpZKQDWQsIP4fVQMFcXvSk3rW=
    adminKey:
      - base64:ZMcOMZ0ZZM1sJHhFqpZKQDWQsIP4fVQMFcXvSk3rWQS=
location:
  lat: 51.5
  lon: -0.12
module_config:
  mqtt:
    enabled: true
    address: mqtt.example
owner: Tracker 4
owner_short: TR4
"""
SECRETS = ("MFcXvSk3rWQ", "oSZMcOMZ0ZZM", "ZMcOMZ0ZZM1sJ", "CgMSAQE6AggB")


class B(Bridge):
    """Enough bridge to hold a profile, with no radio."""
    def __init__(self, d):
        self.state_dir = d
        self.interface = None
        self.logger = type("L", (), {"warning": lambda *a, **k: None, "info": lambda *a, **k: None,
                                     "debug": lambda *a, **k: None})()
        self.audit = []
        self._emit = lambda *a, **k: None


def box():
    return B(tempfile.mkdtemp())


HAVE = all(hasattr(Bridge, m) for m in ("op_profile_export", "op_profile_import"))
if not HAVE:
    for ac in ("AC1 the export is the CLI's shape",
               "AC2 the export carries no secret",
               "AC3 the export names itself",
               "AC4 an unset field is absent, not empty",
               "AC5 a file in that shape imports",
               "AC6 both spellings reach the same field",
               "AC7 a full export imports its five and names what it ignored",
               "AC8 no key crosses on the way in",
               "AC9 a bad value is refused and nothing is written",
               "AC10 rubbish is refused in words",
               "AC11 a round trip is stable"):
        check_true(ac, False, "no profile_export / profile_import on the bridge yet")
    check_true("AC12 both actions are in the catalogue", False, "nothing to check yet")
    check_true("AC13 the screen and the guide carry it", False, "nothing to check yet")
    check_true("AC14 PyYAML is declared", "PyYAML" in (read("pyproject.toml") or ""),
               "pyyaml is not a declared dependency")
    finish()

import yaml  # noqa: E402

# ---- AC1, AC3, AC4 what goes out -----------------------------------------------------------------------
a = box()
a.op_profile_set(role="TRACKER", tx_power=20, position_broadcast_secs=900, region="EU_868",
                 modem_preset="SHORT_FAST")
out = a.op_profile_export()
text = str(out.get("yaml") or "")
check_true("AC3 the export names itself", text.lstrip().startswith("#") and "profile" in text[:200].lower(),
           text[:120])
try:
    doc = yaml.safe_load(text) or {}
except Exception as ex:  # noqa: BLE001
    doc = {}
    check_true("AC1 the export is YAML at all", False, f"{type(ex).__name__}: {ex}")
cfg = (doc.get("config") or {}) if isinstance(doc, dict) else {}
check("AC1 the export is the CLI's shape", sorted(cfg), ["device", "lora", "position"])
check("AC1 the role is where the CLI puts it", (cfg.get("device") or {}).get("role"), "TRACKER")
check("AC1 and the lora fields", ((cfg.get("lora") or {}).get("region"), (cfg.get("lora") or {}).get("modemPreset"),
                                  (cfg.get("lora") or {}).get("txPower")), ("EU_868", "SHORT_FAST", 20))
check("AC1 and the position interval", (cfg.get("position") or {}).get("positionBroadcastSecs"), 900)

b = box()
b.op_profile_set(role="ROUTER")
_thin = yaml.safe_load(str(b.op_profile_export().get("yaml") or "")) or {}
_tc = (_thin.get("config") or {})
check("AC4 an unset field is absent, not empty", sorted(_tc), ["device"])
check_true("AC4 and no empty section is written", all(v for v in _tc.values()), json.dumps(_tc))

# ---- AC2 nothing secret goes out -----------------------------------------------------------------------
# Two different questions, and they need two different checks. A structural key is absent from the
# DOCUMENT; key material is absent from the TEXT. Grepping the text for "owner" condemned a header
# that promises there is no owner in the file, which is the sentence a reader most wants there.
for bad in ("security", "channel_url", "channelUrl", "owner", "owner_short", "location"):
    check_true(f"AC2 the export has no {bad} field", bad not in doc, str(sorted(doc)))
    check_true(f"AC2 nor inside its config", all(bad not in (v or {}) for v in cfg.values()), json.dumps(cfg))
for bad in ("privateKey", "private_key", "adminKey", "admin_key", "base64:", "https://meshtastic.org/e/"):
    check_true(f"AC2 and no {bad} anywhere in the file", bad not in text, text[:300])

# ---- AC5, AC6 what comes in ----------------------------------------------------------------------------
c = box()
r = c.op_profile_import(yaml="""config:
  device: {role: REPEATER}
  lora: {region: US, modemPreset: LONG_FAST, txPower: 27}
  position: {positionBroadcastSecs: 600}
""")
check_true("AC5 a file in that shape imports", bool(r.get("confirmed")), json.dumps(r)[:200])
check("AC5 and the profile holds what the file said",
      {k: c.op_profile().get(k) for k in ("role", "region", "modem_preset", "tx_power", "position_broadcast_secs")},
      {"role": "REPEATER", "region": "US", "modem_preset": "LONG_FAST", "tx_power": 27,
       "position_broadcast_secs": 600})

d = box()
d.op_profile_import(yaml="""config:
  device: {role: SENSOR}
  lora: {region: ANZ, modem_preset: MEDIUM_SLOW, tx_power: 14}
  position: {position_broadcast_secs: 300}
""")
check("AC6 both spellings reach the same field",
      {k: d.op_profile().get(k) for k in ("modem_preset", "tx_power", "position_broadcast_secs")},
      {"modem_preset": "MEDIUM_SLOW", "tx_power": 14, "position_broadcast_secs": 300})

# ---- AC7, AC8 a real full export -------------------------------------------------------------------------
e = box()
r = e.op_profile_import(yaml=FULL)
check_true("AC7 a full export imports", bool(r.get("confirmed")), json.dumps(r)[:200])
check("AC7 its five fields land",
      {k: e.op_profile().get(k) for k in ("role", "region", "modem_preset", "tx_power", "position_broadcast_secs")},
      {"role": "TRACKER", "region": "EU_868", "modem_preset": "SHORT_FAST", "tx_power": 20,
       "position_broadcast_secs": 900})
_ign = r.get("ignored") or []
check_true("AC7 and it names what it ignored rather than saying nothing", bool(_ign), json.dumps(r)[:200])
for sec in ("owner", "location", "security", "module_config", "channel_url"):
    check_true(f"AC7 {sec} is named as ignored", any(sec in str(x) for x in _ign), json.dumps(_ign))

_answer = json.dumps(r)
_prof = json.dumps(e.op_profile())
_disk = ""
for base, _d, names in os.walk(e.state_dir):
    for n in names:
        try:
            _disk += open(os.path.join(base, n), encoding="utf-8", errors="replace").read()
        except OSError:
            pass
for secret in SECRETS:
    check_true(f"AC8 {secret[:9]}... is not in the answer", secret not in _answer, _answer[:200])
    check_true(f"AC8 {secret[:9]}... is not in the profile", secret not in _prof, _prof[:200])
    check_true(f"AC8 {secret[:9]}... is not written to the box", secret not in _disk, _disk[:200])
check_true("AC8 and the ignored report names sections, never values",
           all(not any(s in str(x) for s in SECRETS) for x in _ign), json.dumps(_ign))

# ---- AC9 all or nothing ------------------------------------------------------------------------------------
f = box()
f.op_profile_set(role="TRACKER", tx_power=20, region="EU_868")
before = dict(f.op_profile())
r = f.op_profile_import(yaml="""config:
  device: {role: REPEATER}
  lora: {region: US, txPower: 99}
""")
check_true("AC9 a value out of range is refused", "error" in r, json.dumps(r)[:200])
check_true("AC9 and the refusal names the field", "tx_power" in str(r.get("error")) or "txPower" in str(r.get("error")),
           str(r.get("error")))
check("AC9 and nothing was written: an import is all or nothing", dict(f.op_profile()), before)

# ---- AC10 rubbish, refused in words ---------------------------------------------------------------------------
g = box()
for bad, what in (("this: [is: not: yaml", "broken yaml"),
                  ("just a string", "not a mapping"),
                  ("owner: Tracker 4\n", "no config section"),
                  ("", "nothing at all")):
    r = g.op_profile_import(yaml=bad)
    check_true(f"AC10 {what} is refused in words", "error" in r and len(str(r.get("error"))) > 10,
               json.dumps(r)[:160])

# ---- AC11 a round trip is stable ------------------------------------------------------------------------------
h = box()
h.op_profile_import(yaml=FULL)
one = str(h.op_profile_export().get("yaml") or "")
h.op_profile_import(yaml=one)
two = str(h.op_profile_export().get("yaml") or "")
check("AC11 export, import, export, and the second is the first", two, one)

# ---- AC12, AC13, AC14 the catalogue, the screen, the guide, the dependency ---------------------------------------
cat = read("src/mesh_manager/catalogue.py") or ""
check_true("AC12 export is in the catalogue as a read",
           '"id": "profile_export", "title"' in cat and '"op": "profile_export"' in cat, "no export action")
check_true("AC12 import is in the catalogue as a change",
           '"id": "profile_import"' in cat and '"risk": "change", "op": "profile_import"' in cat, "no import action")
_imp = cat[cat.find('"id": "profile_import"'):cat.find('"id": "profile_import"') + 900]
check_true("AC12 and its confirm says nothing reaches a radio",
           "radio" in _imp and "confirm" in _imp, _imp[:200])

web = read("src/mesh_manager/web.py") or ""
check_true("AC13 the Fleet profile card offers both", "profile_export" in web and "profile_import" in web,
           "the screen offers neither")
# Found by pressing the button, not by reading the code: the screen's form machinery POSTs, and a
# read action is a GET, so an export rendered as a form answers 405 and the button sits on
# "sending" for ever. It is a file route, which is what the CLI wants anyway.
check_true("AC13 the export is a file, not a form that would answer 405",
           "/profile.yaml" in web and "data-action='profile_export'" not in web,
           "the export is still a form")
check_true("AC13 and the file is offered with a name to save it under",
           'filename="mesh-manager-profile.yaml"' in web, "no download name")
# it carries no key, and it is still the operator's fleet configuration: behind the sign-in like
# every other page, not a public route because the contents happen to be safe.
_gate = web.find("if not self._signed_in():")
check_true("AC13 and the file sits behind the sign-in",
           0 < _gate < web.find('if path == "/profile.yaml":'), "the route is outside the gate")
guide = read("docs/GUIDE.md") or ""
check_true("AC13 the guide says it is a fleet profile, not a device backup",
           "fleet profile" in guide.lower() and "backup" in guide.lower(), "the guide does not draw the line")
check_true("AC13 and says why it carries no keys", "key" in guide.lower(), "no mention of keys")
check_true("AC14 PyYAML is declared at a pin", "PyYAML" in (read("pyproject.toml") or ""),
           "pyyaml is not a declared dependency")

finish()
