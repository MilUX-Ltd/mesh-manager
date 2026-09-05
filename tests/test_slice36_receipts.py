#!/usr/bin/env python3
"""Spec 034: a text is delivered, or the radio says why not."""
import os, sys, tempfile
sys.path.insert(0, os.path.dirname(__file__))
from _common import ROOT, check, check_true, finish  # noqa: E402
sys.path.insert(0, os.path.join(ROOT, "src"))
import fakegw_lib  # noqa: E402
fakegw_lib.install()
from mesh_manager import bridge as B  # noqa: E402

state = tempfile.mkdtemp()
br = B.Bridge({"SERIAL": ""}, socket_path=os.path.join(state, "b.sock"), state_dir=state, observe=True, gps_reader=False)
emitted = []
br._emit = lambda kind, **kw: emitted.append((kind, kw))

# ---- AC1 the send asks for an ack and keeps the id -------------------------------------------
out = br.op_send_text(text="check in", channel=0, to="!aa000001")
sent = br.interface.data[-1]
check_true("AC1 sent with wantAck and a handler", sent.get("wantAck") is True and callable(sent.get("onResponse")))
check_true("AC1 the answer carries the packet id", isinstance(out.get("id"), int) and out["id"] > 0)
pid = out["id"]

# ---- AC2 delivered --------------------------------------------------------------------------------
emitted.clear()
sent["onResponse"]({"fromId": "!aa000001", "decoded": {"portnum": "ROUTING_APP", "requestId": pid, "routing": {"errorReason": "NONE"}}})
ack = [v for k, v in emitted if k == "ack"]
check("AC2 delivered emits ack ok", (len(ack), ack[-1].get("request_id") if ack else None, ack[-1].get("ok") if ack else None), (1, pid, True))
check_true("AC2 the outbox marks it delivered", br.outbox.get(pid, {}).get("ack") == "delivered")

# ---- AC3 failed, with the radio's reason ----------------------------------------------------------
out2 = br.op_send_text(text="are you there", channel=0, to="!bb000002")
pid2 = out2["id"]; sent2 = br.interface.data[-1]
emitted.clear()
sent2["onResponse"]({"fromId": "!bb000002", "decoded": {"portnum": "ROUTING_APP", "requestId": pid2, "routing": {"errorReason": "MAX_RETRANSMIT"}}})
ack = [v for k, v in emitted if k == "ack"]
check("AC3 a routing error emits ack not ok with the reason", (ack[-1].get("ok"), ack[-1].get("reason")) if ack else None, (False, "MAX_RETRANSMIT"))
check_true("AC3 the outbox carries the reason", br.outbox.get(pid2, {}).get("ack") == "MAX_RETRANSMIT")

# ---- AC4 an ack nobody asked for ----------------------------------------------------------------------
emitted.clear()
sent["onResponse"]({"fromId": "!aa000001", "decoded": {"portnum": "ROUTING_APP", "requestId": 999999, "routing": {"errorReason": "NONE"}}})
check("AC4 an unknown request id changes nothing and raises nothing", [k for k, _ in emitted if k == "ack"], [])

# ---- AC5 the history keeps the outcome ------------------------------------------------------------------
rows = br.history.query("messages", limit=10)
mine = [r for r in rows if r.get("text") in ("check in", "are you there")]
check("AC5 the history rows carry the outcome", sorted(str(r.get("ack")) for r in mine), ["MAX_RETRANSMIT", "delivered"])
finish()
