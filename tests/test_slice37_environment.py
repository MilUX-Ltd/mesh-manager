#!/usr/bin/env python3
"""Spec 035: what a sensor node says about the air around it."""
import os, sys, tempfile
sys.path.insert(0, os.path.dirname(__file__))
from _common import ROOT, check, check_true, finish  # noqa: E402
sys.path.insert(0, os.path.join(ROOT, "src"))
import fakegw_lib  # noqa: E402
fakegw_lib.install()
from meshtastic.protobuf import telemetry_pb2  # noqa: E402
from mesh_manager import bridge as B, web as W  # noqa: E402

state = tempfile.mkdtemp()
br = B.Bridge({"SERIAL": ""}, socket_path=os.path.join(state, "b.sock"), state_dir=state, observe=True, gps_reader=False)
emitted = []
br._emit = lambda kind, **kw: emitted.append((kind, kw))

# ---- AC1 heard on the air ----------------------------------------------------------------------
pkt = {"fromId": "!aa000001", "toId": "^all", "rxSnr": 8.5, "hopStart": 3, "hopLimit": 3,
       "decoded": {"portnum": "TELEMETRY_APP", "telemetry": {"environmentMetrics": {"temperature": 18.4, "relativeHumidity": 61.0, "barometricPressure": 1009.2}}, "payload": b""}}
br._on_receive(pkt, br.interface)
rows = br.history.query("environment", node="!aa000001")
check("AC1 the reading is in the environment table", (len(rows), rows[-1].get("temperature") if rows else None, rows[-1].get("humidity") if rows else None, rows[-1].get("pressure") if rows else None), (1, 18.4, 61.0, 1009.2))
env = [v for k, v in emitted if k == "environment"]
check_true("AC5 an environment event carries the id and the reading", env and env[-1].get("id") == "!aa000001" and env[-1].get("temperature") == 18.4)

# ---- AC2 the answer to an ask ----------------------------------------------------------------------
t = telemetry_pb2.Telemetry(); t.environment_metrics.temperature = 21.0; t.environment_metrics.relative_humidity = 40.0
br._on_telemetry_answer({"fromId": "!bb000002", "decoded": {"payload": t.SerializeToString()}})
rows = br.history.query("environment", node="!bb000002")
check("AC2 an answer with environment metrics is stored too", (len(rows), rows[-1].get("temperature") if rows else None), (1, 21.0))

# ---- AC3 history reads it ---------------------------------------------------------------------------
out = br.op_history(kind="environment", node="!aa000001")
check("AC3 history serves environment rows", len(out.get("rows") or []), 1)

# ---- AC4 the node page --------------------------------------------------------------------------------
n = {"id": "!aa000001", "name": "Sensor", "heard": "2026-09-04T10:00:00Z", "heard_here": True}
page_with = W.node_body(n, [], [], 0, 24, env=rows_a if (rows_a := br.history.query("environment", node="!aa000001")) else [])
check_true("AC4 with readings: the latest reading and a temperature chart", "18.4" in page_with and "Temperature" in page_with)
page_without = W.node_body({"id": "!cc000003", "name": "Tracker", "heard_here": True}, [], [], 0, 24, env=[])
check_true("AC4 without readings: nothing about the environment", "Temperature" not in page_without)
finish()
