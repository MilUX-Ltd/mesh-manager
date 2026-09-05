#!/usr/bin/env python3
"""Spec 038: the six things the operator types every day, as buttons."""
import http.client, json, os, sys, tempfile, threading, time
sys.path.insert(0, os.path.dirname(__file__))
from _common import ROOT, check, check_true, finish  # noqa: E402
sys.path.insert(0, os.path.join(ROOT, "src"))
from fakebridge_lib import start_fake_bridge  # noqa: E402
from mesh_manager import catalogue as C, web as W  # noqa: E402

fb = start_fake_bridge(); etc = tempfile.mkdtemp()
srv = W.make_server(bind="127.0.0.1", port=0, socket_path=fb.path, etc_dir=etc, config={"AUTH": "off"}, state_dir=tempfile.mkdtemp())
port = srv.server_address[1]; threading.Thread(target=srv.serve_forever, daemon=True).start(); time.sleep(0.3)

def req(method, path, body=None, ctype="application/json"):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    c.request(method, path, body=body, headers={"Content-Type": ctype} if body is not None else {})
    r = c.getresponse(); data = r.read().decode(); c.close(); return r.status, data

# ---- AC4 defaults, AC3 read ----------------------------------------------------------------------
st, body = req("GET", "/api/quick_messages")
d = json.loads(body)
check("AC4 a fresh box has defaults", (st, len(d.get("messages") or []) >= 3), (200, True))
check_true("AC3 in the catalogue as read and change", (C.by_id("quick_messages") or {}).get("risk") == "read" and (C.by_id("quick_messages_set") or {}).get("risk") == "change")

# ---- AC1 / AC3 write ----------------------------------------------------------------------------------
st, body = req("POST", "/api/quick_messages_set", json.dumps({"messages": ["Check in", "RTB", "Send your location"]}))
check("AC3 quick_messages_set writes", (st, json.loads(body).get("confirmed")), (200, True))
check_true("AC1 written to the box", json.load(open(os.path.join(etc, "quick.json"))) == ["Check in", "RTB", "Send your location"])
st, body = req("POST", "/api/quick_messages_set", json.dumps({"messages": [f"m{i}" for i in range(9)]}))
check_true("AC1 nine is refused with a reason", st == 400 and "eight" in json.loads(body).get("error", "").lower())
st, body = req("POST", "/api/quick_messages_set", json.dumps({"messages": ["x" * 201]}))
check_true("AC1 over 200 bytes is refused", st == 400 and "200" in json.loads(body).get("error", ""))

# ---- AC2 the buttons -------------------------------------------------------------------------------------
st, body = req("GET", "/messages")
check_true("AC2 the Messages page shows the presets as buttons", st == 200 and "data-quick=" in body and ">RTB<" in body)
check_true("AC2 a press fills the field, it does not send", "elements.text.value" in body and "data-quick" in body)
st, body = req("GET", "/settings")
check_true("AC1 Settings edits them", "quick" in body.lower() and "name='quick'" in body)
finish()
