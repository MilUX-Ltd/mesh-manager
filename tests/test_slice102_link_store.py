#!/usr/bin/env python3
"""Spec 102, the link store: a packet a broker carried says nothing about any link to this
radio, so it adds nothing to a node's signal history. Found in the 1.5.0 discovery (handover, 30
September 2026): the history store and the node's record were right, the link store still took the
broker's SNR. Committed failing; the build does not edit it."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(__file__))
from _common import ROOT, check, finish  # noqa: E402
sys.path.insert(0, os.path.join(ROOT, "src"))
import fakegw_lib  # noqa: E402
fakegw_lib.install()
from mesh_manager import bridge as B  # noqa: E402

state = tempfile.mkdtemp()
br = B.Bridge({"SERIAL": "/dev/serial/by-id/usb-fake-test-radio-if00"}, socket_path=os.path.join(state, "b.sock"), state_dir=state, observe=True, gps_reader=False)


def pkt(fr, **extra):
    p = {"fromId": fr, "toId": "^all", "hopStart": 3, "hopLimit": 3, "decoded": {"portnum": "TEXT_MESSAGE_APP", "payload": b"hi", "text": "hi"}}
    p.update(extra)
    return p


br._on_receive(pkt("!ee000041", rxSnr=6.25, rxRssi=-54, viaMqtt=True), None)
check("a broker-carried packet adds nothing to the node's signal history", list(br.links.get("!ee000041", [])), [])
check("and does not make it a direct neighbour", br.direct.get("!ee000041"), None)
hist = next((n.get("history") for n in br.op_links().get("nodes", []) if n.get("id") == "!ee000041"), [])
check("so the link map draws no signal for it", hist or [], [])

br._on_receive(pkt("!ee000042", rxSnr=11.5, rxRssi=-92), None)
br._on_receive(pkt("!ee000042", rxSnr=6.25, viaMqtt=True), None)
check("a node heard over the air keeps only the readings this radio took", [x[1] for x in br.links.get("!ee000042", [])], [11.5])
check("and its direct reading is this radio's", br.direct.get("!ee000042"), 11.5)

finish()
