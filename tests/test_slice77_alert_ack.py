#!/usr/bin/env python3
"""Spec 077: an alert can be acknowledged, and acknowledging it means something.

Matt, looking at eleven open alerts with no way to clear them: "There needs to be a way of
dismissing / accepting the alerts."

The hard part is not removing a row. _raise_alert refuses to reopen a key that is already open and
_clear_alert removes it when the condition goes, so simply deleting an alert would let the next
pass raise it straight back. An acknowledgement has to be remembered.
"""
import os, sys, tempfile
sys.path.insert(0, os.path.dirname(__file__))
from _common import ROOT, check, check_true, finish  # noqa: E402
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "tests"))
import fakegw_lib  # noqa: E402
fakegw_lib.install()
from mesh_manager import bridge as B, web as W  # noqa: E402

state = tempfile.mkdtemp(); byid = tempfile.mkdtemp()
GW = os.path.join(byid, "usb-Espressif_USB_JTAG_serial_debug_unit_A4:CB:8F:EE:00:01-if00")
open(GW, "w").close()
br = B.Bridge({"SERIAL": GW}, socket_path=os.path.join(state, "b.sock"), state_dir=state,
              observe=True, gps_reader=False)
br.serial_dir = byid; br.bootloader_check = lambda path: False

def raise_two():
    a = br._alerts_load()
    br._raise_alert(a, "!ee000011", "silent", "Tracker 1 silent for 35 min")
    br._raise_alert(a, "!ee000012", "battery", "Tracker 2 battery 5%")
    br._alerts_save(a)

raise_two()
check("AC1 two alerts are open", len(br._alerts_load()["open"]), 2)

# AC2 acknowledging one takes it off the open list
out = br.op_alert_ack(node="!ee000011", kind="silent")
check("AC2 it reports what it acknowledged", out.get("acknowledged"), ["!ee000011:silent"])
check("AC2 one alert is left open", len(br._alerts_load()["open"]), 1)

# AC3 and it does not come straight back while the condition still holds
a = br._alerts_load()
again = br._raise_alert(a, "!ee000011", "silent", "Tracker 1 silent for 40 min")
check("AC3 an acknowledged alert is not raised again", again, False)
check("AC3 so it stays off the open list", len(a["open"]), 1)

# AC4 when the condition really clears, the acknowledgement is forgotten
a = br._alerts_load()
br._clear_alert(a, "!ee000011", "silent")
br._alerts_save(a)
check("AC4 the acknowledgement is forgotten on a real clear", len(br._alerts_load().get("acked") or {}), 0)
a = br._alerts_load()
check_true("AC4 so a recurrence alerts afresh", br._raise_alert(a, "!ee000011", "silent", "silent again"))
br._alerts_save(a)

# AC5 acknowledging every open alert at once needs confirm
out = br.op_alert_ack(every="yes")
check_true("AC5 all at once needs confirm", "error" in out, repr(out)[:110])
out = br.op_alert_ack(every="yes", confirm="yes")
check("AC5 with confirm it takes the lot", len(br._alerts_load()["open"]), 0)

# AC6 nothing is silently lost: the acknowledged ones are still reported
rep = br.op_alerts()
check_true("AC6 the alerts operation reports the acknowledged", len(rep.get("acked") or []) >= 2,
           str(len(rep.get("acked") or [])))

# AC7 it refuses an alert that is not there, rather than pretending
check_true("AC7 an unknown alert is refused", "error" in br.op_alert_ack(node="!ee000098", kind="silent"))
check_true("AC7 and asking for nothing is refused", "error" in br.op_alert_ack())

# AC8 the screen offers it, and never offers to acknowledge a peer's alert
html = W.alerts_section({"open": [{"node": "!ee000011", "kind": "silent", "text": "quiet", "since": "2026-09-09T10:00:00Z"},
                                  {"node": "!ee000099", "kind": "battery", "text": "theirs", "since": "2026-09-09T10:00:00Z",
                                   "origin_name": "MilUX live hub"}],
                         "acked": [], "recent": [], "settings": {}}, True)
check_true("AC8 an Acknowledge button is offered", "data-action='alert_ack'" in html)
check_true("AC8 and an Acknowledge all", "name='every'" in html)
check_true("AC8 a peer's alert is theirs to acknowledge", "on its own site" in html)

finish()
