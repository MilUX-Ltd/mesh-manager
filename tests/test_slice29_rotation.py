#!/usr/bin/env python3
"""Spec 027: the key rotation checklist."""
import http.client
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
from mesh_manager import bridge as B, web as W  # noqa: E402

state = tempfile.mkdtemp()
br = B.Bridge({"SERIAL": ""}, socket_path=os.path.join(state, "b.sock"), state_dir=state, observe=True, gps_reader=False)
events = []
br._emit = lambda kind, **kw: events.append((kind, kw)) if kind != "log" else None
def pkt(fr):
    return {"fromId": fr, "toId": "^all", "rxSnr": 5.0, "hopStart": 3, "hopLimit": 3, "decoded": {"portnum": "POSITION_APP", "payload": b"x" * 10}}
# before the rotation: a registered device, and a stranger heard here
br._register_save({"!aa000001": {"label": "Recce lead"}, "!dd000004": {"label": "Spare"}})
br._on_receive(pkt("!bb000002"), None)
check("AC1 nothing marked yet", br.op_rotation_status().get("rotation"), None)
check("AC1 a bad slot is refused", "error" in br.op_rotation_mark(index=9), True)
time.sleep(1.1)
r = br.op_rotation_mark(index=0, note="rotated in the app")
st = br.op_rotation_status()
check("AC1 the mark records the time and the expected devices", (r.get("confirmed"), r.get("expected"), sorted(w["id"] for w in st["waiting"]), st["counts"]), (True, 3, ["!aa000001", "!bb000002", "!dd000004"], {"expected": 3, "back": 0, "waiting": 3}))
check("AC1 the names come from the register", [w["name"] for w in st["waiting"] if w["id"] == "!aa000001"], ["Recce lead"])
time.sleep(1.1)
br._on_receive(pkt("!aa000001"), None)
st = br.op_rotation_status()
check("AC1 a packet after the mark moves the device to back, with the time", ([b["id"] for b in st["back"]], bool(st["back"][0]["heard"]), sorted(w["id"] for w in st["waiting"]), st["counts"]["back"]), (["!aa000001"], True, ["!bb000002", "!dd000004"], 1))
check("AC1 a rotation event went out", [kw.get("state") for k, kw in events if k == "rotation"], ["marked"])
# AC2 a confirmed rotate marks itself
marks = []
br._rotation_mark = lambda index, name=None, source="screen", note=None: marks.append((index, source)) or {"ts": B.utc(time.time()), "expected": {}}
br._write_reply = lambda *a, **k: {"confirmed": True, "index": 0}
br._readback = lambda fn: (True, B.utc(time.time()), None, {"name": "MILUX-TAK", "_psk": None})
br._adopt_slot = lambda i, row: None
br._public = lambda row: {}
class _Ch:
    class settings: name = "MILUX-TAK"; psk = b""
    role = 1
class _Node:
    channels = [_Ch()]
    def writeChannel(self, i): pass
br.interface.localNode = _Node()
br._confirm_needed = lambda confirm, text: None
br.op_channel_rotate(index=0, confirm="!x")
check("AC2 a confirmed rotate marks the rotation on its own", marks, [(0, "rotated from the screen")])

fb = start_fake_bridge()
srv = W.make_server(bind="127.0.0.1", port=0, socket_path=fb.path, etc_dir=tempfile.mkdtemp(), config={"AUTH": "off"})
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start(); time.sleep(0.3)
def get(p):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10); c.request("GET", p); r = c.getresponse(); b = r.read().decode(); c.close(); return r.status, b
s, page = get("/channels")
check_true("AC3 the Channels page shows the checklist and the form", s == 200 and "Since the key rotation" in page and "1 of 2 back" in page and "Tracker2" in page and "data-action='rotation_mark'" in page)
s, frag = get("/fragment/rotation")
check_true("AC3 the fragment renders it, with the counts", s == 200 and "<h2 id='rotation'>" in frag and "1 of 2 back" in frag)
srv.shutdown()
finish()
