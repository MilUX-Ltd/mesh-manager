#!/usr/bin/env python3
"""Spec 032: ask a node what it calls itself, so a rename shows without forgetting it."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(__file__))
from _common import ROOT, check, check_true, finish  # noqa: E402
sys.path.insert(0, os.path.join(ROOT, "src"))
import fakegw_lib  # noqa: E402
fakegw_lib.install()
from meshtastic.protobuf import mesh_pb2, portnums_pb2  # noqa: E402
from mesh_manager import bridge as B, catalogue as C  # noqa: E402
from mesh_manager.web import ICONS  # noqa: E402

state = tempfile.mkdtemp()
br = B.Bridge({"SERIAL": ""}, socket_path=os.path.join(state, "b.sock"), state_dir=state, observe=True, gps_reader=False)
br.interface.nodes["!aa000001"] = {"user": {"longName": "Old name", "shortName": "OLD", "hwModel": "TRACKER_T1000_E"}, "lastHeard": 1}
emitted = []
br._emit = lambda kind, **kw: emitted.append((kind, kw))

# ---- AC1 the ask ---------------------------------------------------------------------------------
out = br.op_request_nodeinfo(dest="!aa000001")
sent = br.interface.data[-1]
check("AC1 a NODEINFO_APP sendData with wantResponse and a handler",
      (sent["portNum"] == portnums_pb2.PortNum.NODEINFO_APP, sent["dest"], sent["wantResponse"], callable(sent["onResponse"])),
      (True, "!aa000001", True, True))
check_true("AC1 what it sends is the box's own User", isinstance(sent["data"], mesh_pb2.User))
check_true("AC1 the answer says what it asked and when", out.get("requested") == "nodeinfo" and out.get("asked"))
check_true("AC1 with no destination it refuses", bool(br.op_request_nodeinfo().get("error")))

# ---- AC2 and AC4 the answer ------------------------------------------------------------------------
emitted.clear()
u = mesh_pb2.User(id="!aa000001", long_name="New name", short_name="NEW")
sent["onResponse"]({"fromId": "!aa000001", "decoded": {"payload": u.SerializeToString()}})
rec = br.interface.nodes["!aa000001"]["user"]
check("AC2 the node's names are updated", (rec.get("longName"), rec.get("shortName")), ("New name", "NEW"))
ev = [(k, v) for k, v in emitted if k == "nodeinfo"]
check_true("AC4 a nodeinfo event carries the id and the new name",
           ev and ev[-1][1].get("id") == "!aa000001" and ev[-1][1].get("name") == "New name")

# ---- AC3 the hardware model --------------------------------------------------------------------------
check("AC3 an answer without a hardware model leaves what it knew", rec.get("hwModel"), "TRACKER_T1000_E")
u2 = mesh_pb2.User(id="!aa000001", long_name="New name", short_name="NEW", hw_model=mesh_pb2.HardwareModel.RAK4631)
sent["onResponse"]({"fromId": "!aa000001", "decoded": {"payload": u2.SerializeToString()}})
check("AC3 an answer with one takes it", br.interface.nodes["!aa000001"]["user"].get("hwModel"), "RAK4631")

# ---- AC5 a malformed answer ---------------------------------------------------------------------------
emitted.clear()
before = dict(br.interface.nodes["!aa000001"]["user"])
sent["onResponse"]({"fromId": "!aa000001", "decoded": {"payload": b"\xff\xff not a User \xff"}})
check("AC5 a malformed answer changes nothing", br.interface.nodes["!aa000001"]["user"], before)
check_true("AC5 and raises nothing into the receive path", True)

# a node the box has never heard of must not crash the handler
sent["onResponse"]({"fromId": "!bb000002", "decoded": {"payload": mesh_pb2.User(id="!bb000002", long_name="Newcomer").SerializeToString()}})
check_true("AC5 an answer from a node not in the database is taken, not dropped",
           br.interface.nodes.get("!bb000002", {}).get("user", {}).get("longName") == "Newcomer")

# ---- AC6 the catalogue and the row ---------------------------------------------------------------------
a = C.by_id("request_nodeinfo")
check_true("AC6 an air action with exactly one node input",
           a and a["risk"] == "air" and len(a["inputs"]) == 1 and a["inputs"][0]["type"] == "node")
check_true("AC6 it has an icon, so it lands on the node row", "request_nodeinfo" in ICONS)

finish()
