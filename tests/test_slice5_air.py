#!/usr/bin/env python3
"""Spec 004: messages and on-air requests from the screen, against the fake bridge."""
import http.client
import json
import os
import socket
import sys
import tempfile
import threading
import time
import urllib.parse

sys.path.insert(0, os.path.dirname(__file__))
from _common import ROOT, check, check_true, finish  # noqa: E402
sys.path.insert(0, os.path.join(ROOT, "src"))
from fakebridge_lib import start_fake_bridge  # noqa: E402

try:
    from mesh_manager import web as W
    from mesh_manager import catalogue as C
except Exception as e:  # noqa: BLE001
    print(f"FAIL imports                                                     {type(e).__name__}: {e}")
    print("\nFAILURES: 1"); sys.exit(1)

fb = start_fake_bridge()
etc = tempfile.mkdtemp()
W.write_password(os.path.join(etc, "passwd"), "correct horse")
srv = W.make_server(bind="127.0.0.1", port=0, socket_path=fb.path, etc_dir=etc)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
time.sleep(0.3)


def req(method, path, body=None, cookie=None, ctype="application/x-www-form-urlencoded"):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    h = {}
    if cookie: h["Cookie"] = cookie
    if body is not None: h["Content-Type"] = ctype
    c.request(method, path, body=body, headers=h)
    r = c.getresponse(); data = r.read(); hd = dict((k.lower(), v) for k, v in r.getheaders())
    c.close(); return r.status, hd, data


st, hd, _ = req("POST", "/login", body="password=correct+horse")
cookie = hd.get("set-cookie", "").split(";")[0]

# AC1 the messages page and the chat ring
st, _, page = req("GET", "/messages", cookie=cookie); page = page.decode()
check("AC1 /messages answers", st, 200)
form_fields = sorted(set(__import__("re").findall(r"name='([a-z_]+)'", page.split("<form", 1)[-1].split("</form>", 1)[0])) - {"password"})
want_fields = sorted(i["name"] for i in C.by_id("send_text")["inputs"])
check("AC1 the form's fields are send_text's catalogue inputs", form_fields, want_fields)
fb.emit({"kind": "text", "from": "!aa000001", "name": "Tracker9", "to": "^all", "channel": 0, "text": "radio check", "ts": "2026-09-03T03:00:00Z"})
time.sleep(0.5)
st, _, msgs = req("GET", "/api/messages", cookie=cookie)
check_true("AC1 a text packet event appears in /api/messages", "radio check" in msgs.decode())

# AC2 send_text
st, _, body = req("POST", "/api/send_text", body=json.dumps({"text": "hello mesh", "channel": 0}), cookie=cookie, ctype="application/json")
check("AC2 send_text answers 200", st, 200)
time.sleep(0.2)
check("AC2 the fake bridge got the text on channel 0 to ^all", fb.calls[-1] if fb.calls else None, ("send_text", {"text": "hello mesh", "channel": 0, "to": "^all"}))
st, _, _ = req("POST", "/api/send_text", body=json.dumps({"text": ""}), cookie=cookie, ctype="application/json")
check("AC2 empty text answers 400", st, 400)
st, _, _ = req("POST", "/api/send_text", body=json.dumps({"text": "x" * 201}), cookie=cookie, ctype="application/json")
check("AC2 201 bytes answers 400", st, 400)
st, _, _ = req("POST", "/api/send_text", body=json.dumps({"text": "hi", "to": "!deadbeef"}), cookie=cookie, ctype="application/json")
check("AC2 an unknown destination answers 400", st, 400)

# AC3 per-node controls and traceroute
st, _, nodes = req("GET", "/nodes", cookie=cookie); nodes = nodes.decode()
check_true("AC3 nodes page carries a traceroute control per node", nodes.count("data-action='traceroute'") >= 2)
check_true("AC3 nodes page carries an ask-position control per node", nodes.count("data-action='request_position'") >= 2)
st, _, _ = req("POST", "/api/traceroute", body=json.dumps({"dest": "!aa000001"}), cookie=cookie, ctype="application/json")
time.sleep(0.2)
check("AC3 traceroute reaches the fake bridge", (st, fb.calls[-1] if fb.calls else None), (200, ("traceroute", {"dest": "!aa000001"})))

# AC4 the radio page
st, _, radio = req("GET", "/radio", cookie=cookie); radio = radio.decode()
check("AC4 /radio answers", st, 200)
for want in ("TAK Gateway", "EU_868", "SHORT_FAST", "14"):
    check_true(f"AC4 /radio carries {want}", want in radio)
check_true("AC4 /radio carries the settings forms (Spec 006 superseded the read-only page)", "data-action='radio_set'" in radio and "data-action='radio_set_region'" in radio)
srv.shutdown()
finish()
