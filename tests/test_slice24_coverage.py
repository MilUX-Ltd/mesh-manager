#!/usr/bin/env python3
"""Spec 022: coverage survey."""
import http.client
import json
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
emitted = []
br._emit = lambda kind, **kw: emitted.append((kind, kw)) if kind != "log" else None
check("AC1 a missing node is refused", "error" in br.op_survey_start(dest=""), True)
check("AC1 an interval outside 5 to 120 is refused", "error" in br.op_survey_start(dest="!aa000001", interval=2), True)
check("AC1 nothing runs yet", br.op_survey_status().get("running"), False)
# a short run: the loop's floor is 5 s in the catalogue, but the op takes what the test gives within bounds; use 5 s and stop early
before = len(br.interface.data)
r = br.op_survey_start(dest="!aa000001", interval=5, minutes=1)
check("AC1 started", (r.get("started"), r.get("dest"), r.get("interval")), (True, "!aa000001", 5))
check("AC1 a second survey while one runs is refused", "error" in br.op_survey_start(dest="!bb000002"), True)
time.sleep(0.4)
st = br.op_survey_status()
asks = [d for d in br.interface.data[before:] if d.get("wantResponse")]
check("AC1 it asked through the request-position path and counted", (st.get("running"), st.get("asked"), len(asks) >= 1, st.get("dest")), (True, 1, True, "!aa000001"))
# an answer lands in the history as a position with its signal
br._on_receive({"fromId": "!aa000001", "toId": "!ee000025", "rxSnr": 6.5, "hopStart": 3, "hopLimit": 3, "decoded": {"portnum": "POSITION_APP", "payload": b"p" * 20, "position": {"latitude": 51.21, "longitude": -1.51}}}, None)
r = br.op_survey_stop()
time.sleep(0.3)
st = br.op_survey_status()
check("AC1 stop ends it and the status says so, with the answers counted from the history", (r.get("stopped"), st.get("running"), st.get("answers")), (True, False, 1))
kinds = [kw.get("state") for k, kw in emitted if k == "survey"]
check("AC1 survey events: started, asked, ended", (kinds[0], "asked" in kinds, kinds[-1]), ("started", True, "ended"))
check("AC1 stop with nothing running is honest", br.op_survey_stop().get("stopped"), False)

fb = start_fake_bridge()
srv = W.make_server(bind="127.0.0.1", port=0, socket_path=fb.path, etc_dir=tempfile.mkdtemp(), config={"AUTH": "off"})
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start(); time.sleep(0.3)
def get(p):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10); c.request("GET", p); r = c.getresponse(); b = r.read().decode(); c.close(); return r.status, b
def post(p, body):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10); c.request("POST", p, body=json.dumps(body), headers={"Content-Type": "application/json"}); r = c.getresponse(); b = r.read().decode(); c.close(); return r.status, b
s, mp = get("/map")
check_true("AC2 the coverage control and the survey form with its bounds", s == 200 and "id='cover-hours'" in mp and "data-action='survey_start'" in mp and "min='5' max='120'" in mp and "data-action='survey_stop'" in mp)
check_true("AC2 the overlay colours dots by band and draws relayed ones hollow", "cover_" in mp and "bandTok(b)" in mp and "fillOpacity:direct?0.75:0" in mp and "mmCoverTick" in mp)
s, full = get("/map/full")
check_true("AC2 the map of its own has the coverage control, not the form", "id='cover-hours'" in full and "data-action='survey_start'" not in full)
s, body = post("/api/survey_start", {"dest": "!aa000001", "interval": 10, "minutes": 5})
check("AC2 the survey starts through the API", (s, json.loads(body).get("started"), fb.calls[-1][0]), (200, True, "survey_start"))
s, body = get("/api/survey_status")
check("AC2 the status reads", (s, json.loads(body).get("asked")), (200, 3))
srv.shutdown()
finish()
