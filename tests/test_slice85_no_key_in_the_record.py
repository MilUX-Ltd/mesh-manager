#!/usr/bin/env python3
"""Spec 085: no key in the record.

A meshtastic.org join URL carries the channel's pre-shared key, and channel_adopt takes one as a
plain text input. Every path that records what an action was asked to do stored the arguments
verbatim, so adopting a channel wrote a channel key into audit.log and onto Activity.

Found on the estate on 12 September 2026: one line on deployed carrying the key for a live
channel. This is the defect, not the cleanup.
"""
import json, os, re, sys, tempfile
sys.path.insert(0, os.path.dirname(__file__))
from _common import ROOT, check, check_true, finish, read  # noqa: E402
sys.path.insert(0, os.path.join(ROOT, "src"))
from mesh_manager import catalogue as C  # noqa: E402
from mesh_manager import connections as K  # noqa: E402
from mesh_manager import web as W  # noqa: E402

JOIN = "https://meshtastic.org/e/#CgMSAQEaB0V4YW1wbGUtTk9ULUEtUkVBTC1LRVk"

# ---- AC1 the catalogue can say an input carries key material ------------------------------------
def inp(aid, name):
    a = C.by_id(aid) or {}
    return next((i for i in a.get("inputs", []) if i["name"] == name), None)

for aid in ("channel_adopt", "channel_decode"):
    i = inp(aid, "url")
    check_true(f"AC1 {aid}.url is marked as carrying key material", bool(i) and i.get("secret") is True)
check_true("AC1 an ordinary input is not marked", inp("channel_adopt", "mode").get("secret") is not True)

# ---- AC2 one helper, and nobody redacts for themselves -------------------------------------------
check_true("AC2 the catalogue offers one redaction helper", hasattr(C, "redact_args"))
src_web = read("src/mesh_manager/web.py") or ""
src_conn = read("src/mesh_manager/connections.py") or ""
check("AC2 every recorder calls it", src_web.count("redact_args") + src_conn.count("redact_args") >= 3, True)
check_true("AC2 nobody guesses at a word list",
           not re.search(r'in \(\s*"password"|"password"\s*,\s*"key"|lower\(\)\s*==\s*"password"', src_web + src_conn))

# ---- the helper itself ----------------------------------------------------------------------------
if hasattr(C, "redact_args"):
    out = C.redact_args("channel_adopt", {"url": JOIN, "mode": "replace", "confirm": "!ee000001"})
    check_true("AC3 the secret is gone", JOIN not in json.dumps(out))
    check("AC6 and the ordinary arguments are untouched",
          [out.get("mode"), out.get("confirm")], ["replace", "!ee000001"])
    check_true("AC6 the record still says a value was supplied", "url" in out and out["url"] != "")
    check_true("AC6 and what it says is not the value", JOIN not in str(out["url"]))
    check("AC10 an action with no secret is returned unchanged",
          C.redact_args("radio_set", {"long_name": "Gateway", "tx_power": 14}),
          {"long_name": "Gateway", "tx_power": 14})
    check("AC10 an unknown action is left alone rather than emptied",
          C.redact_args("no_such_action", {"a": 1}), {"a": 1})
    check("AC10 a secret that was not supplied does not appear",
          C.redact_args("channel_adopt", {"mode": "replace"}), {"mode": "replace"})

# ---- AC3 to AC5 the three recorders, end to end ---------------------------------------------------
# Guarded: a suite that throws before its verdicts tells you nothing about the other nine criteria.
if not hasattr(C, "redact_args"):
    for n in ("AC3 to AC5 the three recorders", "AC6 the record survives redaction",
              "AC7 an old line is not printed", "AC8 the proposal keeps and hides its value"):
        check_true(n, False, "no redact_args yet")
    finish()

etc = tempfile.mkdtemp()
K.audit(etc, who="operator", event="run", action="channel_adopt",
        arguments=C.redact_args("channel_adopt", {"url": JOIN, "mode": "replace"}), outcome="ok")
K.audit(etc, who="an-agent", event="call", action="channel_adopt",
        arguments=C.redact_args("channel_adopt", {"url": JOIN}), autonomy="act")
K.propose(etc, who="an-agent", action="channel_adopt", args={"url": JOIN, "mode": "replace"}, rationale="because")
log = open(os.path.join(etc, "audit.log")).read()
check("AC3 to AC5 no join URL in the audit log at all", JOIN in log, False)
check("AC3 to AC5 and nothing that looks like one", "meshtastic.org/e/#" in log, False)
check_true("AC6 the three records are still there", log.count("channel_adopt") >= 3)
check_true("AC6 with their ordinary arguments", '"mode": "replace"' in log)

# ---- AC8 the proposal keeps what it needs to run, and never shows it ------------------------------
props = K.proposals(etc)
check("AC8 the proposal was stored", len(props), 1)
check("AC8 it still holds the real value, because it has to run", props[0]["arguments"].get("url"), JOIN)
form = W.proposal_form(props[0])
check("AC8 and the form does not render it", JOIN in form, False)
check_true("AC8 the form says the value is held rather than showing a blank nobody understands",
           "held" in form.lower() or "not shown" in form.lower())

# ---- AC7 the renderer refuses even for a line written before this ----------------------------------
old = {"ts": "2026-09-09T06:42:30Z", "who": "operator", "event": "run", "action": "channel_adopt",
       "arguments": {"url": JOIN, "mode": "replace"}, "outcome": "ok"}
detail = W.audit_detail(old)
check("AC7 an old line's key is not printed", JOIN in detail, False)
check_true("AC7 but the rest of the old line still reads", "replace" in detail)

# ---- AC9 the guard against the next one ------------------------------------------------------------
SUSPECT = re.compile(r"url|key|psk|password|token|secret|qr", re.I)
NOT_KEY_MATERIAL = {("map_source_add", "url")}   # a tile URL template, no key in it
missed = []
for a in C.ACTIONS:
    for i in a.get("inputs", []):
        if i.get("secret"):
            continue
        if SUSPECT.search(i["name"]) and (a["id"], i["name"]) not in NOT_KEY_MATERIAL:
            missed.append(f'{a["id"]}.{i["name"]}')
check("AC9 no input that looks like key material is unmarked and unexplained", sorted(missed), [])
check_true("AC9 the allow-list is small enough to read", len(NOT_KEY_MATERIAL) <= 3)

finish()
