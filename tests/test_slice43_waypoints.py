#!/usr/bin/env python3
"""Spec 041: a pin dropped on the mesh reaches TAK, and the map."""
import os, sys, tempfile, time
sys.path.insert(0, os.path.dirname(__file__))
from _common import ROOT, check, check_true, finish  # noqa: E402
sys.path.insert(0, os.path.join(ROOT, "src"))
import fakegw_lib  # noqa: E402
fakegw_lib.install()
from meshtastic.protobuf import mesh_pb2  # noqa: E402
from mesh_manager import bridge as B, catalogue as C  # noqa: E402

state = tempfile.mkdtemp()
br = B.Bridge({"SERIAL": ""}, socket_path=os.path.join(state, "b.sock"), state_dir=state, observe=True, gps_reader=False)
br.socket_client = fakegw_lib.FakeSock()
emitted = []
br._emit = lambda kind, **kw: emitted.append((kind, kw))

wp = mesh_pb2.Waypoint(id=4242, latitude_i=int(51.5 * 1e7), longitude_i=int(-0.12 * 1e7), expire=int(time.time()) + 3600, name="RV Alpha", description="meet here")
br._on_receive({"fromId": "!aa000001", "toId": "^all", "rxSnr": 6.0, "hopStart": 3, "hopLimit": 3,
                "decoded": {"portnum": "WAYPOINT_APP", "payload": wp.SerializeToString()}}, br.interface)
rows = br.history.query("waypoints")
check("AC1 stored with its facts", (len(rows), rows[-1].get("name") if rows else None, rows[-1].get("wid") if rows else None, round(rows[-1].get("lat") or 0, 3) if rows else None), (1, "RV Alpha", 4242, 51.5))
ev = [v for k, v in emitted if k == "waypoint"]
check_true("AC1 emitted", ev and ev[-1].get("name") == "RV Alpha")
cot = b"".join(br.socket_client.sent)
check_true("AC2 forwarded to TAK as a spot marker carrying the name", b'type="b-m-p-s-m"' in cot and b"RV Alpha" in cot)

# ---- AC3 expired ---------------------------------------------------------------------------------------
gone = mesh_pb2.Waypoint(id=4242, latitude_i=0, longitude_i=0, expire=1, name="RV Alpha")
br._on_receive({"fromId": "!aa000001", "toId": "^all", "decoded": {"portnum": "WAYPOINT_APP", "payload": gone.SerializeToString()}}, br.interface)
live = br.op_waypoints().get("waypoints", [])
check("AC3 an expired or zeroed waypoint is no longer shown", [w.get("wid") for w in live], [])

# ---- AC4 sending one ------------------------------------------------------------------------------------
out = br.op_waypoint_send(name="RV Bravo", description="fall back", lat=51.501, lon=-0.121, expire_min=60)
check_true("AC4 it went on the air with the name", out.get("sent") and br.interface.waypoints and br.interface.waypoints[-1][0] == "RV Bravo")
check_true("AC4 waypoint_send is air in the catalogue", (C.by_id("waypoint_send") or {}).get("risk") == "air")
check_true("AC5 waypoints is a read", (C.by_id("waypoints") or {}).get("risk") == "read")
finish()
