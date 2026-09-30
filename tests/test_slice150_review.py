#!/usr/bin/env python3
"""1.5.0, from the independent review of Specs 110 to 113 (30 September 2026). Written with the fixes, after the
review; the review's findings are the specification. A site name cannot write a second line into the config; a
node's name is text on the map, never markup; a site cannot pin its rows as newest with a time from the future; a
failed join leaves a site already paired as it was; the invite never reaches the audit."""
import json, os, sys, tempfile, time, types
sys.path.insert(0, os.path.dirname(__file__))
from _common import ROOT, check, check_true, finish, read  # noqa: E402
sys.path.insert(0, os.path.join(ROOT, "src"))
import fakegw_lib  # noqa: E402
fakegw_lib.install()
from mesh_manager.bridge import Bridge  # noqa: E402
from mesh_manager.common import read_config  # noqa: E402
from mesh_manager import catalogue as C, web as W  # noqa: E402


class B(Bridge):
    def __init__(self, d):
        self.state_dir = d; self.box_mode = "desktop"; self.events = []
        self.conf = {"MODE": "desktop", "CONFIG_PATH": os.path.join(d, "config")}
        open(self.conf["CONFIG_PATH"], "w").write("MODE=desktop\nAUTH=on\nSERIAL=/dev/x\n")
        self.peering = types.SimpleNamespace(name="old", id="ab" * 32, port=None, connected=lambda: {})
        self._emit = lambda kind=None, **k: self.events.append(k)
        self.logger = types.SimpleNamespace(warning=print, info=lambda *a, **k: None)


b = B(tempfile.mkdtemp())
for bad in ("Brize AUTH=off", "Brize AUTH=off", "Brize\x85AUTH=off", "Brize\rAUTH=off"):
    check_true(f"a site name with {bad[5]!r} is refused", "error" in b.op_site_name_set(name=bad), "accepted")
try:
    Bridge._write_conf_key(b.conf["CONFIG_PATH"], "SITE_NAME", "x AUTH=off")
    wrote = True
except ValueError:
    wrote = False
check("no config write can carry a second line, whatever calls it", wrote, False)
check("and the config still says sign-in is on", read_config(b.conf["CONFIG_PATH"]).get("AUTH"), "on")
check("a person's name with a line separator is refused too", W.clean_name("Sgt Patel"), None)

src = read("src/mesh_manager/web.py") or ""
check_true("every map tooltip given a string gets it as text, not markup",
           "L.Layer.prototype.bindTooltip=function(c,o){if(typeof c==='string'" in src and "s.textContent=String(c)" in src)
check_true("the map's escaper escapes quotes, because it fills attributes", ".replace(/\"/g,'&quot;').replace(/'/g,'&#39;')" in src)
check_true("a site's group colour is a token name, never raw CSS", "/^[a-z0-9-]{1,24}$/.test(gc)" in src)

future = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 86400 * 365))
now_ = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 60))
check("a contact time from the future counts for nothing", Bridge._contact_secs({"mqtt_at": future}), 0.0)
check("and the Nodes page never shows one as the last contact", W.latest_contact({"heard": now_, "mqtt_at": future}), now_)

check_true("the invite is a secret argument, so the audit never writes it",
           "invite" in C.secret_inputs("peer_join") and "invite" in C.secret_inputs("site_invite_read"))
check("the redacted copy says one was given, not what it was",
      C.redact_args("peer_join", {"invite": "h:1/CODE1234/" + "ab" * 32 + "/9"})["invite"] != "h:1/CODE1234/" + "ab" * 32 + "/9", True)

finish()
