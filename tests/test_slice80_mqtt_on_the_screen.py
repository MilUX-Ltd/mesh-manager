#!/usr/bin/env python3
"""Spec 080: the gateway radio's MQTT, on the screen.

Matt: "I can't see how I manage the bridging / connection between mesh manager installs and the
connection to the MQTT server... I think I need a settings area for managing the settings of the
connection radio, the one acting as the gateway. This might already be in there, but I can't see it."

It was not in there. Spec 070 built the operation and said the screen would show whether the proxy
is connected and what it last carried; mqtt_state() went into the status and nothing ever rendered
it. Both estate boxes were configured over the socket by hand.
"""
import os, re, sys
sys.path.insert(0, os.path.dirname(__file__))
from _common import ROOT, check, check_true, finish, read  # noqa: E382
sys.path.insert(0, os.path.join(ROOT, "src"))
from mesh_manager import web as W  # noqa: E402

LIVE = {"running": True, "connected": True, "broker": "tak.milux.co.uk:8883", "root": "milux",
        "user": "matt", "tls": True, "password_set": True, "sent": 27, "received": 70,
        "topics": ["milux/2/e/MilUXPriv/#"],
        "last_sent": {"topic": "milux/2/e/MilUXPriv/!ee000080", "at": "2026-09-09T15:00:00Z"}}

card = W.mqtt_card(LIVE)

# AC1 it can be set from the screen at all
check_true("AC1 the screen carries the operation", "data-action='gateway_mqtt_set'" in card)
for f in ("address", "username", "password", "root", "tls", "enabled"):
    check_true(f"AC1 the form has {f}", f"name='{f}'" in card)

# AC2 it says what is true now
check_true("AC2 it says it is connected and where", "Carrying the radio's MQTT to tak.milux.co.uk:8883" in card)
check_true("AC2 and what it has carried", "Carried <b>27</b> out and <b>70</b> back" in card)
check_true("AC2 and what it is listening on", "milux/2/e/MilUXPriv/#" in card)

# AC3 a proxy that is not running says why, rather than looking the same as one that is
off = W.mqtt_card({"running": False, "connected": False, "reason": "the radio is not set to proxy through this box"})
check_true("AC3 it says there is no proxy", "No MQTT proxy on this box" in off)
check_true("AC3 and gives the reason", "not set to proxy through this box" in off)
bad = W.mqtt_card({"running": True, "connected": False, "error": "cannot reach the broker"})
check_true("AC3 a running proxy that cannot connect is not called connected", "Not connected to the broker" in bad)
check_true("AC3 and says why", "cannot reach the broker" in bad)

# AC4 the password is never put back on the screen
check_true("AC4 the field is a password field", "type='password'" in card)
check_true("AC4 it says one is set without saying what", "one is set; leave blank to keep it" in card)
check_true("AC4 and no value is ever rendered into it",
           re.search(r"name='password'[^>]*value=", card) is None)

# AC5 the two things Matt conflated are told apart
check_true("AC5 it says the settings live on the radio", "live on the <b>radio</b>" in card)
check_true("AC5 and points at Connections for joining boxes", "/connections" in card)

# AC6 it is on the Radio page, and reachable from anywhere
web = read("src/mesh_manager/web.py") or ""
check_true("AC6 the Radio page renders it", "{mqtt_card(mqtt, cfg)}" in web)
import re as _re
_call = _re.search(r'radio_body\(self\._ask\("config"\)[^)]*\)', web)
check_true("AC6 the route hands it the state",
           _call is not None and 'st.get("mqtt")' in _call.group(0), _call.group(0) if _call else "no radio_body call")
check_true("AC6 the state strip carries it, linked to Radio", "This box carries the radio&#39;s MQTT" in web)

# AC7 a box with no proxy is not made to look faulty in the strip
check_true("AC7 the strip only speaks when there is a proxy", 'if mq.get("running"):' in web)

finish()
