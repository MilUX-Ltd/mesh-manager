#!/usr/bin/env python3
"""Spec 020: the history store."""
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
from mesh_manager.history import History  # noqa: E402

state = tempfile.mkdtemp()
br = B.Bridge({"SERIAL": "", "HISTORY_DAYS": 30}, socket_path=os.path.join(state, "b.sock"), state_dir=state, observe=True, gps_reader=False)
check_true("the store opens in the state directory", br.history.ok and os.path.exists(os.path.join(state, "history.db")))

# AC1 one row per thing heard
br._on_receive({"fromId": "!aa000001", "toId": "^all", "rxSnr": 9.5, "hopStart": 3, "hopLimit": 3, "decoded": {"portnum": "POSITION_APP", "payload": b"x" * 20, "position": {"latitude": 51.2001, "longitude": -1.5002}}}, None)
br._on_receive({"fromId": "!aa000001", "toId": "^all", "rxSnr": 8.0, "hopStart": 3, "hopLimit": 3, "decoded": {"portnum": "TELEMETRY_APP", "payload": b"y" * 12, "telemetry": {"deviceMetrics": {"batteryLevel": 64, "voltage": 3.91, "channelUtilization": 4.2, "airUtilTx": 0.7, "uptimeSeconds": 900}}}}, None)
br._on_receive({"fromId": "!aa000001", "toId": "^all", "rxSnr": 7.0, "hopStart": 3, "hopLimit": 3, "channel": 0, "decoded": {"portnum": "TEXT_MESSAGE_APP", "payload": b"hello", "text": "hello"}}, None)
pos = br.op_history(kind="positions")["rows"]; tel = br.op_history(kind="telemetry")["rows"]; msg = br.op_history(kind="messages")["rows"]; pk = br.op_history(kind="packets")["rows"]
check("AC1 a position row", (len(pos), pos[0]["node"], pos[0]["lat"], pos[0]["lon"], pos[0]["snr"], pos[0]["hops"]), (1, "!aa000001", 51.2001, -1.5002, 9.5, 0))
check("AC1 a telemetry row with utilisation", (len(tel), tel[0]["level"], tel[0]["voltage"], tel[0]["chutil"], tel[0]["airutil"], tel[0]["uptime"]), (1, 64, 3.91, 4.2, 0.7, 900))
check("AC1 a message row", (len(msg), msg[0]["text"], msg[0]["name"], msg[0]["dest"]), (1, "hello", "Tracker9", "^all"))
check("AC1 a packet row per packet, with size", (len(pk), [r["port"] for r in pk], pk[0]["size"]), (3, ["POSITION_APP", "TELEMETRY_APP", "TEXT_MESSAGE_APP"], 20))
# the TAK path: the gateway's record moved, no decoded position
br.meshtastic_devices["!aa000001"]["last_lat"] = 51.2100; br.meshtastic_devices["!aa000001"]["last_lon"] = -1.5100
br._on_receive({"fromId": "!aa000001", "toId": "^all", "rxSnr": 6.0, "hopStart": 3, "hopLimit": 3, "decoded": {"portnum": "ATAK_PLUGIN", "payload": b"z" * 60}}, None)
br._on_receive({"fromId": "!aa000001", "toId": "^all", "rxSnr": 6.0, "hopStart": 3, "hopLimit": 3, "decoded": {"portnum": "ATAK_PLUGIN", "payload": b"z" * 60}}, None)
pos = br.op_history(kind="positions")["rows"]
check("AC1 the gateway's record moving writes one position row, not one per packet", (len(pos), pos[-1]["lat"], pos[-1]["lon"]), (2, 51.21, -1.51))
br.op_send_text(text="from the box", channel=0)
check("AC1 a sent text is history too", br.op_history(kind="messages")["rows"][-1]["text"], "from the box")

# AC2 survives a restart
br.history.close()
br2 = B.Bridge({"SERIAL": ""}, socket_path=os.path.join(state, "b2.sock"), state_dir=state, observe=True, gps_reader=False)
check("AC2 a new bridge on the same state directory reads the rows back", (len(br2.op_history(kind="positions")["rows"]), len(br2.op_history(kind="messages")["rows"])), (2, 2))

# AC3 filters and the summary
br2._on_receive({"fromId": "!bb000002", "toId": "^all", "rxSnr": 3.0, "hopStart": 3, "hopLimit": 2, "decoded": {"portnum": "POSITION_APP", "payload": b"x", "position": {"latitude": 51.3, "longitude": -1.6}}}, None)
check("AC3 by node", [r["node"] for r in br2.op_history(kind="positions", node="!bb000002")["rows"]], ["!bb000002"])
check("AC3 by since (nothing before now plus a minute)", br2.op_history(kind="positions", since=B.utc(time.time() + 60))["count"], 0)
check("AC3 limit caps, newest last", [r["lat"] for r in br2.op_history(kind="positions", limit=1)["rows"]], [51.3])
check("AC3 an unknown kind is refused", "error" in br2.op_history(kind="secrets"), True)
sm = br2.op_history_summary()
check("AC3 the summary counts and spans", (sm["ok"], sm["tables"]["positions"]["rows"], bool(sm["tables"]["positions"]["oldest"]), sm["days"]), (True, 3, True, 30))

# AC4 retention
h = History(tempfile.mkdtemp(), days=1)
h.position("!cc000003", 1.0, 1.0, ts=B.utc(time.time() - 3 * 86400))
h.position("!cc000003", 2.0, 2.0)
h.trim(force=True)
check("AC4 rows older than the retention go", [r["lat"] for r in h.query("positions")], [2.0])
import mesh_manager.history as HM  # noqa: E402
HM.CAP = 5
for i in range(8):
    h.packet("!cc000003", port="P", size=i)
h.trim(force=True)
check("AC4 rows beyond the cap go, newest kept", [r["size"] for r in h.query("packets")], [3, 4, 5, 6, 7])
HM.CAP = 200_000

# AC5 the Messages page after a restart shows what the store has
fb = start_fake_bridge()
srv = W.make_server(bind="127.0.0.1", port=0, socket_path=fb.path, etc_dir=tempfile.mkdtemp(), config={"AUTH": "off"})
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start(); time.sleep(0.3)
def get(p):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10); c.request("GET", p); r = c.getresponse(); b = r.read().decode(); c.close(); return r.status, b
s, body = get("/messages")
check_true("AC5 the Messages page shows a stored text after a restart", s == 200 and "stored before the restart" in body)
s, about = get("/about")
check_true("AC5 About shows the history store", s == 200 and "History" in about and "history.db" in about)
srv.shutdown()
finish()
