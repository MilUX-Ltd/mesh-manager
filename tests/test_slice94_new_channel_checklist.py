#!/usr/bin/env python3
"""Spec 094: the checklist after a new channel, not only after a rotation.

"Since the key rotation" answers "is the fleet back" without anyone counting, and it fired on one
action. Create a channel, push it to the fleet, and you got nothing for the same job.

The part that is not a copy: a rotation counts a device back when it has been HEARD, which is
right because the key changed under the whole mesh. A new channel proves nothing by being heard,
because a device can talk on the primary all day without ever being given the new slot. So this
counts a device back when its own read-back carried the channel.
"""
import json, os, sys, tempfile
sys.path.insert(0, os.path.dirname(__file__))
from _common import ROOT, check, check_true, finish, read  # noqa: E402
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "tests"))
import fakegw_lib  # noqa: E402
fakegw_lib.install()
from mesh_manager.bridge import Bridge  # noqa: E402


class B(Bridge):
    """Enough bridge to drive the rotation record without a radio."""
    def __init__(self, d):
        self.state_dir = d
        self.interface = None
        self.history = None
        self.logger = type("L", (), {"warning": lambda *a, **k: None, "info": lambda *a, **k: None})()
        self._emit = lambda *a, **k: None
        self._reg = {"!ee000011": {"label": "Alpha", "managed": True},
                     "!ee000012": {"label": "Bravo", "managed": True},
                     "!ee000013": {"label": "Charlie", "managed": False}}

    def _register_load(self):
        return self._reg

    def _own(self):
        return {"id": "!ee000001"}

    def mesh_nodes(self):
        return [{"id": "!ee000011", "name": "Alpha", "heard_here": True},
                {"id": "!ee000013", "name": "Charlie", "heard_here": True}]


d = tempfile.mkdtemp()
b = B(d)

# ---- AC1 creating arms, and does not raise ---------------------------------------------------------
check_true("AC1 the bridge can arm a slot", hasattr(b, "_channel_armed"), "no _channel_armed")
if hasattr(b, "_channel_armed"):
    b._channel_armed(3, name="Ops")
    st = b.op_rotation_status()
    check("AC1 a created channel raises no checklist on its own", st.get("rotation"), None)

# ---- AC2, AC4 the first confirmed push raises it, and counts that device ------------------------------
check_true("AC2 the bridge records a confirmed push", hasattr(b, "_channel_pushed"), "no _channel_pushed")
if hasattr(b, "_channel_pushed"):
    b._channel_pushed(3, "!ee000011", confirmed=True)
    st = b.op_rotation_status()
    check_true("AC2 the checklist is up", bool(st.get("rotation")), json.dumps(st)[:180])
    check("AC2 and it is the new channel, not a rotation", (st.get("rotation") or {}).get("kind"), "channel")
    back = {r["id"] for r in (st.get("back") or [])}
    check_true("AC4 the device whose read-back carried it counts back", "!ee000011" in back, str(back))

    # ---- AC6 the expected set is the managed devices ---------------------------------------------------
    waiting = {r["id"] for r in (st.get("waiting") or [])}
    check("AC6 a managed device that has not taken it is waiting", "!ee000012" in waiting, True)
    check("AC6 an unmanaged device is not expected at all",
          "!ee000013" in (back | waiting), False)

    # ---- AC5 being heard does not count on this kind ------------------------------------------------------
    check("AC5 a device merely heard is not counted back", "!ee000013" in back, False)

    # ---- AC3 an unconfirmed push counts nobody --------------------------------------------------------------
    b._channel_pushed(3, "!ee000012", confirmed=False)
    st2 = b.op_rotation_status()
    back2 = {r["id"] for r in (st2.get("back") or [])}
    check("AC3 a push that did not read back counts nobody", "!ee000012" in back2, False)
    b._channel_pushed(3, "!ee000012", confirmed=True)
    st3 = b.op_rotation_status()
    check_true("AC4 and a confirmed one does",
               "!ee000012" in {r["id"] for r in (st3.get("back") or [])})
    check("AC9 the waiting count the strip reads works for a channel too", b._rotation_waiting(), None)

# ---- AC10 a rotation later wins ----------------------------------------------------------------------------
if hasattr(b, "_channel_armed"):
    b._channel_armed(4, name="Later")
    b._rotation_mark(0, name="Primary", source="rotated from the screen")
    st4 = b.op_rotation_status()
    check("AC10 the rotation replaces the channel checklist", (st4.get("rotation") or {}).get("kind"), "rotation")
    check_true("AC10 and nothing is armed behind it",
               not (json.load(open(os.path.join(d, "rotation.json"))).get("armed") or {}),
               read(os.path.join(d, "rotation.json")) or "")

# ---- AC7, AC8 the screen --------------------------------------------------------------------------------------
web = read("src/mesh_manager/web.py") or ""
check_true("AC7 the heading changes with the kind", "Since the new channel" in web,
           "no heading for a new channel")
check_true("AC7 and the rotation keeps its own", "Since the key rotation" in web)

br = read("src/mesh_manager/bridge.py") or ""
check_true("AC8 a rotation still counts a heard device back",
           "kind" in br and ("heard" in br), "the rotation's rule is gone")

# ---- AC1, AC2 the ops actually call the helpers -----------------------------------------------------------
# Every check above drives the helpers directly, which proves the machinery and nothing about
# whether creating a channel or pushing one ever reaches it.
import re  # noqa: E402
_create = br[br.find("def op_channel_create("):br.find("def op_channel_rotate(")]
check_true("AC1 creating a channel arms the slot", "_channel_armed(" in _create,
           "op_channel_create never arms anything")
_push = br[br.find("def op_node_channel_push("):br.find("def op_node_reboot(")]
check_true("AC2 a push records itself", "_channel_pushed(" in _push,
           "op_node_channel_push never records the push")
check_true("AC3 and it hands over whether the read-back carried it",
           re.search(r"_channel_pushed\([^)]*confirmed=", _push) is not None, "the push does not pass its verdict")

finish()
