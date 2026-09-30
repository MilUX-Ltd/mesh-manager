#!/usr/bin/env python3
"""Spec 102: the history store's foundations. Every table indexed; the signal strength
kept beside the SNR; a reading carried by a broker marked as such and never stored as this box's.
These are the acceptance tests. They were committed failing, and the build does not edit them."""
import math
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(__file__))
from _common import ROOT, check, check_true, finish  # noqa: E402
sys.path.insert(0, os.path.join(ROOT, "src"))
import fakegw_lib  # noqa: E402
fakegw_lib.install()
from mesh_manager import bridge as B  # noqa: E402
from mesh_manager.history import TABLES, History, utc  # noqa: E402


def bridge(state):
    return B.Bridge({"SERIAL": "/dev/serial/by-id/usb-fake-test-radio-if00", "HISTORY_DAYS": 30},
                    socket_path=os.path.join(state, "b.sock"), state_dir=state, observe=True, gps_reader=False)


def position(fr, lat, lon, **extra):
    p = {"fromId": fr, "toId": "^all", "hopStart": 3, "hopLimit": 3,
         "decoded": {"portnum": "POSITION_APP", "payload": b"x" * 20, "position": {"latitude": lat, "longitude": lon}}}
    p.update(extra)
    return p


def safe(fn, *a, **k):
    """Run one call a criterion depends on; a call the code cannot yet take is a failure, not a crash."""
    try:
        return fn(*a, **k)
    except Exception as e:  # noqa: BLE001
        return f"raised {type(e).__name__}: {e}"


def cols(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


# AC1 every table is indexed on its time and on node-then-time
state = tempfile.mkdtemp()
br = bridge(state)
conn = sqlite3.connect(os.path.join(state, "history.db"))
have = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()}
missing = sorted(f"{t}_{s}" for t in TABLES for s in ("ts", "node") if f"{t}_{s}" not in have)
check("AC1 every table carries its ts and node indexes", missing, [])
plan = " ".join(str(r[-1]) for r in conn.execute("EXPLAIN QUERY PLAN SELECT * FROM positions WHERE node = ? AND ts >= ?", ("!aa000001", "2026-09-30T00:00:00Z")).fetchall())
check_true("AC1 a node-and-window query on positions searches an index", "USING INDEX" in plan and "SCAN positions" not in plan, plan)
plan = " ".join(str(r[-1]) for r in conn.execute("EXPLAIN QUERY PLAN SELECT * FROM telemetry WHERE ts >= ?", ("2026-09-30T00:00:00Z",)).fetchall())
check_true("AC1 a window query on telemetry searches an index", "USING INDEX" in plan and "SCAN telemetry" not in plan, plan)

# AC2 a store written by an earlier version gains the indexes and the columns, and its old rows say "not known"
old = tempfile.mkdtemp()
legacy = sqlite3.connect(os.path.join(old, "history.db"))
legacy.execute("CREATE TABLE positions (id INTEGER PRIMARY KEY, ts TEXT NOT NULL, node TEXT NOT NULL, lat REAL NOT NULL, lon REAL NOT NULL, snr REAL, hops INTEGER)")
legacy.execute("CREATE TABLE packets (id INTEGER PRIMARY KEY, ts TEXT NOT NULL, node TEXT, port TEXT, snr REAL, hops INTEGER, size INTEGER)")
legacy.execute("INSERT INTO positions (ts, node, lat, lon, snr, hops) VALUES (?, ?, ?, ?, ?, ?)", (utc(), "!dd000004", 51.1, -1.4, 6.5, 0))
legacy.commit(); legacy.close()
h = History(old)
check_true("AC2 an earlier store opens", h.ok)
c2 = sqlite3.connect(os.path.join(old, "history.db"))
have2 = {r[0] for r in c2.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()}
check("AC2 it gains the indexes it lacked", sorted(n for n in ("positions_ts", "positions_node", "packets_ts", "packets_node") if n not in have2), [])
check("AC2 positions and packets gain rssi and via_mqtt", ({"rssi", "via_mqtt"} <= cols(c2, "positions"), {"rssi", "via_mqtt"} <= cols(c2, "packets")), (True, True))
row = h.query("positions")[0]
check("AC2 an old row keeps its SNR and says nothing it does not know", (row["snr"], row.get("rssi"), row.get("via_mqtt")), (6.5, None, None))
check("AC2 an old row is not claimed as heard over the air", safe(h.query, "positions", over_air=True), [])
h.close()
h = History(old)
check_true("AC2 opening the same store twice changes nothing and raises nothing", h.ok and len(h.query("positions")) == 1)
h.close()

# AC3 the signal strength is kept beside the SNR, and only when it is a number
br._on_receive(position("!aa000001", 51.2001, -1.5002, rxSnr=9.5, rxRssi=-97), None)
pos = br.op_history(kind="positions")["rows"][-1]; pk = br.op_history(kind="packets")["rows"][-1]
check("AC3 a position heard over the air keeps its RSSI and SNR", (pos["snr"], pos.get("rssi"), pos.get("via_mqtt")), (9.5, -97.0, 0))
check("AC3 so does its packet row", (pk["snr"], pk.get("rssi"), pk.get("via_mqtt")), (9.5, -97.0, 0))
br._on_receive(position("!aa000001", 51.2011, -1.5012, rxSnr=8.0), None)
check("AC3 a packet with no RSSI stores none, never nought", br.op_history(kind="positions")["rows"][-1].get("rssi"), None)
for bad in ("loud", float("nan"), float("inf"), True, [1], {"v": 1}):
    br._on_receive(position("!aa000001", 51.2021, -1.5022, rxSnr=7.0, rxRssi=bad), None)
    got = br.op_history(kind="positions")["rows"][-1].get("rssi")
    check_true(f"AC3 an RSSI of {bad!r} is stored as none", got is None, repr(got))
br._on_receive(position("!aa000001", 51.2031, -1.5032, rxSnr=7.0, rxRssi=-5000), None)
check("AC3 an RSSI no radio could report is stored as none", br.op_history(kind="positions")["rows"][-1].get("rssi"), None)
check_true("AC3 no RSSI in the store is NaN or infinite", all(r.get("rssi") is None or math.isfinite(r["rssi"]) for r in br.op_history(kind="positions")["rows"]))

# AC4 a reading a broker carried is marked, and another gateway's measurement is never stored as this box's
br._on_receive(position("!ee000005", 53.4, -2.2, rxSnr=5.8, rxRssi=-60, viaMqtt=True), None)
pos = br.op_history(kind="positions")["rows"][-1]; pk = br.op_history(kind="packets")["rows"][-1]
check("AC4 a broker-carried position is marked, with no signal or hops of ours", (pos["node"], pos.get("via_mqtt"), pos["snr"], pos.get("rssi"), pos["hops"]), ("!ee000005", 1, None, None, None))
check("AC4 so is its packet row", (pk["node"], pk.get("via_mqtt"), pk["snr"], pk.get("rssi"), pk["hops"]), ("!ee000005", 1, None, None, None))
br._on_receive({"fromId": "!ee000005", "toId": "^all", "rxSnr": 5.8, "viaMqtt": True, "channel": 0,
                "decoded": {"portnum": "TEXT_MESSAGE_APP", "payload": b"hello", "text": "hello from far away"}}, None)
check("AC4 a broker-carried message stores no SNR of ours", br.op_history(kind="messages")["rows"][-1]["snr"], None)
check("AC4 a broker-carried position is still a position: the node is where it says", (pos["lat"], pos["lon"]), (53.4, -2.2))

# AC5 the store can be asked for what this radio heard itself
q = br.history.query("positions", node="!ee000005")
check("AC5 by node the broker-carried row is there", len(q), 1)
air = safe(br.history.query, "positions", over_air=True)
check_true("AC5 over_air=True is accepted", isinstance(air, list), repr(air)[:80])
air = air if isinstance(air, list) else []
check("AC5 over_air=True leaves out broker-carried rows", [r["node"] for r in air if r["node"] == "!ee000005"], [])
check_true("AC5 over_air=True keeps what this radio heard", any(r["node"] == "!aa000001" for r in air))
op = safe(br.op_history, kind="packets", over_air=True)
check("AC5 over_air through the bridge op too", [r["node"] for r in (op.get("rows") if isinstance(op, dict) else [])  if r["node"] == "!ee000005"], [])

finish()
