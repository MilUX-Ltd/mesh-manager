#!/usr/bin/env python3
"""Spec 100: choose the gateway radio, from the screen, after install.

The gateway was chosen once on a command line before anybody saw the screen, and could not be
changed from the product at all. A box with no radio refused to start; a box pointed at a radio that
was not there showed MISSING and offered nothing.

None of this is new machinery. Spec 062 already made a laptop with nothing plugged in a site
watching for a radio; bench_ports() already lists every radio when no gateway is configured; and
desktop.py already swaps the radio under a running screen. The box shape simply never got any of it.

Identity is deliberately not here. A replacement radio has a different node id and key pair, and
what that does to the fleet's admin trust and the mesh's channel key is a separate job. This suite covers
getting a radio chosen and attached, and nothing about whether the mesh it joins is the right one.

Every block is guarded: a missing helper is one verdict, not a traceback hiding the rest.
"""
import json, os, re, sys, tempfile, threading
sys.path.insert(0, os.path.dirname(__file__))
from _common import ROOT, check, check_true, finish, read  # noqa: E402
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "tests"))
import fakegw_lib  # noqa: E402
fakegw_lib.install()
from mesh_manager.bridge import Bridge  # noqa: E402

# A by-id directory of the suite's own, listed exactly as a box lists /dev/serial/by-id. The ports
# fallback is not the path a box takes, and its name filter would not match these anyway.
BYID = tempfile.mkdtemp()
R_NAME = "usb-Espressif_USB_JTAG_serial_debug_unit_A4:CB:8F:EE:00:01-if00"
O_NAME = "usb-1a86_USB_Single_Serial_58A3097418-if00"
G_NAME = "usb-u-blox_AG_-_www.u-blox.com_u-blox_GNSS_receiver-if00"
for _n in (R_NAME, O_NAME, G_NAME):
    open(os.path.join(BYID, _n), "w").close()
RADIO = os.path.join(BYID, R_NAME)
OTHER = os.path.join(BYID, O_NAME)
GPS = os.path.join(BYID, G_NAME)

CONFIG = ("SERIAL={serial}\nREGION=EU_868\nCHANNEL=MILUX\nFILTER_GROUP=MilUX\nEXTRA_ARGS=\n"
          "BIND=127.0.0.1\nPORT=8093\nAUTH=on\nMODE=server\nHISTORY_DAYS=30\n")


class B(Bridge):
    """A box with a config file and a set of ports, and no real radio."""
    def __init__(self, d, serial="", ports=(RADIO, OTHER, GPS)):
        self.state_dir = d
        self.interface = None
        self.history = None
        self.peering = None
        self.box_mode = "server"
        self.outbox = {}
        self.batteries = {}
        self.remote_alerts = {}
        self._peers_lock = threading.Lock()
        self._stop = threading.Event()
        self.logger = type("L", (), {"warning": lambda *a, **k: None, "info": lambda *a, **k: None,
                                     "debug": lambda *a, **k: None, "error": lambda *a, **k: None})()
        self.events = []
        self._emit = lambda kind=None, **k: self.events.append(dict(k, kind=kind))
        self.conf = {"SERIAL": serial, "MODE": "server", "CONFIG_PATH": os.path.join(d, "config")}
        open(self.conf["CONFIG_PATH"], "w").write(CONFIG.format(serial=serial))
        self.serial_dir = BYID
        self._ports_for_test = []
        self.restarted = 0

    def _own(self):
        return {"id": "!ee000001"}

    def mesh_nodes(self):
        return []


def box(serial="", ports=(RADIO, OTHER, GPS)):
    return B(tempfile.mkdtemp(), serial, ports)


HAVE = all(hasattr(Bridge, m) for m in ("op_gateway", "op_gateway_set"))
if not HAVE:
    for ac in ("AC1 a box with no radio starts",
               "AC2 it says it is watching for a radio",
               "AC3 gateway answers with the radio and the candidates",
               "AC4 a set writes SERIAL and leaves the rest of the file alone",
               "AC5 a path the box cannot see is refused",
               "AC6 a path that is not a by-id path is refused",
               "AC7 the bridge re-attaches after a set",
               "AC8 setting the radio already in use changes nothing",
               "AC9 the action is in the catalogue with a confirm",
               "AC10 the screen offers it",
               "AC11 the chooser names candidates the way the Bench does"):
        check_true(ac, False, "no op_gateway / op_gateway_set on the bridge yet")
    finish()

# ---- AC1, AC2 a box with no radio is a site, not an error ----------------------------------------------
src = read("src/mesh_manager/bridge.py") or ""
_radioless = re.search(r"radioless\s*=\s*([^\n]+)", src)
check_true("AC1 a box with no SERIAL is radioless rather than refused",
           _radioless is not None and 'box_mode == "desktop"' not in _radioless.group(1),
           _radioless.group(1) if _radioless else "no radioless line")
_main = src[src.find("def main(argv=None):"):]
check_true("AC1 and main no longer exits on a box with no SERIAL",
           'MODE not in ("hub", "desktop")' not in _main and "no SERIAL in the config" not in _main,
           _main[:400])
check_true("AC2 it says it is watching for a radio, whatever shape it is",
           len(re.findall(r"watching for a radio", src)) >= 1 and "a laptop, watching for a radio" not in src,
           "the words are still fenced to a laptop")

# ---- AC3 what the box knows about its radio -------------------------------------------------------------
a = box(serial=RADIO)
g = a.op_gateway()
check("AC3 it names the radio in use", g.get("serial"), RADIO)
_paths = [c.get("path") for c in (g.get("candidates") or [])]
check_true("AC3 and lists what is plugged in", RADIO in _paths and OTHER in _paths, str(_paths))
check_true("AC3 including the one in use, so it can be seen to be the one in use",
           RADIO in _paths, str(_paths))
check_true("AC3 and says whether the radio it is pointed at is actually there",
           "present" in g, str(sorted(g)))

b = box(serial="")
g2 = b.op_gateway()
check("AC3 a box with no radio says so plainly", (g2.get("serial"), g2.get("present")), ("", False))
check_true("AC3 and still lists the candidates, which is the whole point",
           RADIO in [c.get("path") for c in (g2.get("candidates") or [])], json.dumps(g2)[:200])

# ---- AC11 the candidates read the way the Bench reads ------------------------------------------------------
_cand = (g.get("candidates") or [{}])[0]
for k in ("path", "kind"):
    check_true(f"AC11 a candidate carries {k}", k in _cand, str(sorted(_cand)))
# Better than marking it: gps_path() finds the box's receiver and bench_ports() drops it, so a GPS
# is never offered as a gateway and cannot be chosen by mistake. The check holds that, not a label.
_gps = [c for c in (g.get("candidates") or []) if c.get("path") == GPS]
check("AC11 a GPS receiver is not offered as a gateway at all", _gps, [])
check_true("AC11 and the radios still are",
           {c["path"] for c in (g.get("candidates") or [])} == {RADIO, OTHER},
           str([c["path"] for c in (g.get("candidates") or [])]))

# ---- AC4 a set writes the config, and only the one key ------------------------------------------------------
c = box(serial=RADIO)
r = c.op_gateway_set(path=OTHER)
check_true("AC4 a set is confirmed", bool(r.get("confirmed")), json.dumps(r)[:200])
_conf = open(c.conf["CONFIG_PATH"]).read()
check_true("AC4 SERIAL is the new radio", f"SERIAL={OTHER}" in _conf, _conf[:200])
check_true("AC4 and nothing else in the file moved",
           all(line in _conf for line in ("REGION=EU_868", "CHANNEL=MILUX", "FILTER_GROUP=MilUX",
                                          "BIND=127.0.0.1", "PORT=8093", "AUTH=on", "MODE=server")),
           _conf)
check("AC4 and the file still has one SERIAL line", len(re.findall(r"(?m)^SERIAL=", _conf)), 1)

# ---- AC5, AC6 what is refused ---------------------------------------------------------------------------------
d_ = box(serial=RADIO)
r = d_.op_gateway_set(path="/dev/serial/by-id/usb-not-plugged-in-if00")
check_true("AC5 a path the box cannot see is refused", "error" in r, json.dumps(r)[:200])
check_true("AC5 and the refusal names what it can see",
           O_NAME in str(r.get("error")) or "can see" in str(r.get("error")).lower(), str(r.get("error")))
check_true("AC5 and nothing was written", f"SERIAL={RADIO}" in open(d_.conf["CONFIG_PATH"]).read(),
           open(d_.conf["CONFIG_PATH"]).read()[:120])
for bad in ("/dev/ttyACM0", "COM3", "", "not a path", BYID + "/../../etc/passwd"):
    r = d_.op_gateway_set(path=bad)
    check_true(f"AC6 {bad!r} is refused", "error" in r, json.dumps(r)[:140])

# ---- AC7 the bridge re-attaches ---------------------------------------------------------------------------------
e_ = box(serial=RADIO)
r = e_.op_gateway_set(path=OTHER)
check_true("AC7 the answer says the bridge is restarting onto the new radio",
           re.search(r"restart|outage|moment", json.dumps(r), re.I) is not None, json.dumps(r)[:200])
check_true("AC7 and the bridge was actually asked to re-attach",
           e_.restarted >= 1 or e_._stop.is_set(), f"restarted={e_.restarted}, stop={e_._stop.is_set()}")

# ---- AC8 setting what is already set ------------------------------------------------------------------------------
f_ = box(serial=RADIO)
r = f_.op_gateway_set(path=RADIO)
check_true("AC8 setting the radio already in use is not an error", "error" not in r, json.dumps(r)[:160])
check_true("AC8 and says nothing changed", re.search(r"already|unchanged|no change", json.dumps(r), re.I) is not None,
           json.dumps(r)[:200])
check("AC8 and the bridge was not restarted for nothing", (f_.restarted, f_._stop.is_set()), (0, False))

# ---- AC9 the catalogue -----------------------------------------------------------------------------------------------
cat = read("src/mesh_manager/catalogue.py") or ""
check_true("AC9 the read is in the catalogue", '"id": "gateway"' in cat, "no gateway read action")
check_true("AC9 and the write", '"id": "gateway_set"' in cat, "no gateway_set action")
# the entry itself, not a fixed slice: 1200 characters ran into the next action, which is a read,
# and condemned this one for carrying a word that belonged to its neighbour.
_start = cat.find('"id": "gateway_set"')
_blk = cat[_start:cat.find('{"id": "', _start + 10)]
check_true("AC9 it is not a read", '"risk": "read"' not in _blk, _blk[:200])
check_true("AC9 and its confirm says the mesh goes down for a moment",
           "confirm" in _blk and re.search(r"(outage|down|moment|restart)", _blk, re.I) is not None, _blk[:400])

# ---- AC10 the screen -----------------------------------------------------------------------------------------------------
web = read("src/mesh_manager/web.py") or ""
check_true("AC10 the screen offers the chooser", "gateway_set" in web, "nothing on the screen")
# Found by opening the page: the card was only rendered on the branch where the radio is unreadable,
# and "gateway_set is somewhere in web.py" passed anyway. The Radio page must render it on BOTH
# branches, because changing a working radio is the case this card exists for.
_rb = web[web.find("def radio_body("):web.find("def proposal_form(")]
# the page's own returns, at one indent: radio_body holds a nested helper whose return is its own
_returns = [l for l in _rb.splitlines() if l.startswith("    return")]
check_true("AC10 every way out of the Radio page carries the chooser",
           bool(_returns) and all("gateway_card(gw)" in l for l in _returns),
           " | ".join(l.strip()[:70] for l in _returns))
# This check used to look for the phrase anywhere in web.py. It passed while the phrase sat behind
# `if mode == "desktop"` and a box still read "Radio missing", which is the whole fault this card is
# about. It now holds the two things that have to be true on a box.
_strip = web[web.find('elif not st.get("radio_present"):'):][:500]
check_true("AC10 the strip says watching for a radio on any shape that has none chosen",
           'mode") == "desktop"' not in _strip or 'not st.get("radio")' in _strip, _strip[:300])
_face = web[web.find('card("Radio",'):][:900]
check_true("AC10 and the home page offers the way to choose one",
           "/radio" in _face and re.search(r"watching for a radio", _face, re.I) is not None, _face[:400])

# ---- AC12 a box that never touches it is untouched ---------------------------------------------------------------------------
check_true("AC12 nothing about this reaches a box that already works",
           "gateway_set" not in (read("install/install.sh") or ""), "the installer grew a dependency on it")

finish()
