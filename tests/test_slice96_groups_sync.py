#!/usr/bin/env python3
"""Spec 096: group definitions cross the peer link, and survive a delete.

Two problems. The release card named tombstones: delete on one box, and the peer that still holds
the group puts it back, so delete is the one operation a naive sync silently undoes.

The harder one is underneath. A group was keyed by its name and a node's membership was that name,
so the name was the identity. The card requires a rename on one box and a recolour on the other to
both stand, and with the name as the key that cannot happen: a rename is one group vanishing and
another appearing, and the recolour has nothing to apply to. A group now has a stable id and the
name is an ordinary field.
"""
import json, os, sys, tempfile, threading, time
sys.path.insert(0, os.path.dirname(__file__))
from _common import ROOT, check, check_true, finish, read  # noqa: E402
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "tests"))
import fakegw_lib  # noqa: E402
fakegw_lib.install()
from mesh_manager.bridge import Bridge  # noqa: E402


class B(Bridge):
    """A box with a group store and a register, and no radio."""
    def __init__(self, d):
        self.state_dir = d
        self.interface = None
        self.peering = None
        self.logger = type("L", (), {"warning": lambda *a, **k: None, "info": lambda *a, **k: None})()
        self._emit = lambda *a, **k: None
        # op_register walks the peers' pictures on its way past; a real bridge has these and the
        # suite crashed instead of reporting when it did not.
        self._peers_lock = threading.Lock()
        self.remote_nodes = {}
        self.batteries = {}

    def _own(self):
        return {"id": "!ee000001"}

    def mesh_nodes(self):
        return []


def box():
    return B(tempfile.mkdtemp())


# ---- AC1 an id that survives a rename -------------------------------------------------------------
a = box()
r = a.op_group_set(name="Recce", colour="node-3")
gid = (r.get("group") or {}).get("id")
check_true("AC1 a group is given an id", bool(gid), str(r))
r2 = a.op_group_set(id=gid, name="Recce Two")
check("AC1 the id survives a rename", (r2.get("group") or {}).get("id"), gid)
check("AC1 and the name is what changed", (r2.get("group") or {}).get("name"), "Recce Two")
check_true("AC1 there is still one group, not two",
           len([g for g in a.op_groups().get("groups") or []]) == 1, str(a.op_groups()))

# ---- AC2 an old box migrates ------------------------------------------------------------------------
old = box()
json.dump({"Alpha": {"icon": "radio", "colour": "node-2", "created": "2026-09-01T00:00:00Z"}},
          open(os.path.join(old.state_dir, "groups.json"), "w"))
json.dump({"!ee000021": {"label": "One", "group": "Alpha"}, "!ee000022": {"label": "Two", "group": ""}},
          open(os.path.join(old.state_dir, "register.json"), "w"))
gs = {g["name"]: g for g in (old.op_groups().get("groups") or [])}
check_true("AC2 the old group survives migration", "Alpha" in gs, str(gs))
check_true("AC2 and has been given an id", bool((gs.get("Alpha") or {}).get("id")), str(gs))
check("AC2 its member is still in it", (gs.get("Alpha") or {}).get("count"), 1)

# ---- AC3, AC4, AC5 per-field last write wins ------------------------------------------------------------
# guarded: a missing helper is one verdict, not a traceback that hides every check after it. Fourth
# time today, so it is written in rather than remembered.
HAVE_SYNC = all(hasattr(Bridge, m) for m in ("_groups_item", "_groups_merge"))
check_true("AC3 the box can build and merge a shared groups item", HAVE_SYNC,
           "no _groups_item / _groups_merge")
if not HAVE_SYNC:
    for ac in ("AC3 fields carry their write time", "AC4 a rename and a recolour both stand",
               "AC5 an older write never wins", "AC13 merging twice is a no-op",
               "AC6 a delete leaves a tombstone", "AC7 the delete reaches the other box",
               "AC8 membership is last write wins per node"):
        check_true(ac, False, "no sync helpers to exercise")
one, two = box(), box()
mk = one.op_group_set(name="Recce", colour="node-3")
gid = (mk.get("group") or {}).get("id")
if HAVE_SYNC:
  item = one._groups_item()
  check_true("AC3 the shared item carries when each field was written",
             all("at" in (g or {}) for g in (item.get("groups") or {}).values()), json.dumps(item)[:200])
  two._groups_merge(item)
  check_true("AC4 the group reaches the other box",
             gid in {g["id"] for g in two.op_groups().get("groups") or []}, str(two.op_groups()))

  time.sleep(1.05)
  one.op_group_set(id=gid, name="Recce Two")          # rename here
  two.op_group_set(id=gid, colour="node-6")           # recolour there
  one._groups_merge(two._groups_item())
  two._groups_merge(one._groups_item())
  g1 = {g["id"]: g for g in one.op_groups().get("groups") or []}[gid]
  g2 = {g["id"]: g for g in two.op_groups().get("groups") or []}[gid]
  check("AC4 the rename stands on both", (g1.get("name"), g2.get("name")), ("Recce Two", "Recce Two"))
  check("AC4 and so does the recolour", (g1.get("colour"), g2.get("colour")), ("node-6", "node-6"))

  # an older write must not win
  stale = json.loads(json.dumps(one._groups_item()))
  stale["groups"][gid]["name"] = "Ancient"
  stale["groups"][gid]["at"]["name"] = "2020-01-01T00:00:00Z"
  two._groups_merge(stale)
  check("AC5 an older write never overwrites a newer one",
        {g["id"]: g for g in two.op_groups().get("groups") or []}[gid].get("name"), "Recce Two")

  # ---- AC13 merging twice changes nothing --------------------------------------------------------------------
  before = json.dumps(two._groups_item(), sort_keys=True)
  two._groups_merge(one._groups_item())
  two._groups_merge(one._groups_item())
  check("AC13 merging the same item twice is a no-op", json.dumps(two._groups_item(), sort_keys=True), before)

  # ---- AC6, AC7 the tombstone ------------------------------------------------------------------------------------
  time.sleep(1.05)
  one.op_group_delete(id=gid)
  check("AC6 the group is gone here", [g for g in one.op_groups().get("groups") or [] if g["id"] == gid], [])
  check_true("AC6 and a tombstone is kept to send",
             bool(((one._groups_item().get("groups") or {}).get(gid) or {}).get("deleted")),
             json.dumps(one._groups_item())[:200])
  two._groups_merge(one._groups_item())
  check("AC7 the delete reaches the other box", [g for g in two.op_groups().get("groups") or [] if g["id"] == gid], [])
  # and the box that still remembered it must not put it back
  one._groups_merge(two._groups_item())
  check("AC6 a peer that still held it does not resurrect it",
        [g for g in one.op_groups().get("groups") or [] if g["id"] == gid], [])

  # ---- AC8 membership, last write wins per node --------------------------------------------------------------------
  x, y = box(), box()
  m = x.op_group_set(name="Vehicles", colour="node-4")
  vid = (m.get("group") or {}).get("id")
  y._groups_merge(x._groups_item())
  x.op_register_set(id="!ee000031", group=vid)
  y.op_register_set(id="!ee000032", group=vid)
  x._groups_merge(y._groups_item())
  y._groups_merge(x._groups_item())
  for b, who in ((x, "one"), (y, "two")):
      reg = b._register_load()
      check(f"AC8 both moves survive on box {who}",
            (str((reg.get('!ee000031') or {}).get('group') or ''), str((reg.get('!ee000032') or {}).get('group') or '')),
            (vid, vid))

# ---- AC12 everything that reads a membership still reads a name --------------------------------------------------
# The id is storage and wire only. Two places read the register directly rather than the node payload
# and both broke on the way through: a group message found nobody, and a device onboarded on the
# bench showed a hex id where its group should be.
z = box()
zg = (z.op_group_set(name="Vehicles", colour="node-4").get("group") or {}).get("id")
z.op_register_set(id="!ee000041", group="Vehicles", label="bench one")
check("AC12 a group message addressed by name finds its members",
      sorted(z._group_members("Vehicles")) if hasattr(z, "_group_members") else
      sorted(nid for nid, r in z._register_load().items() if str(r.get("group") or "") == z._group_key("Vehicles")),
      ["!ee000041"])
try:
    _bench = {r["id"]: r for r in (z.op_register().get("rows") or [])}
except (AttributeError, TypeError, KeyError) as ex:   # one verdict, never a crash that hides the rest
    _bench = {}
    check_true("AC12 the register can be read at all", False, f"{type(ex).__name__}: {ex}")
check("AC12 a device never heard on the air still shows the group's name, not its id",
      str((_bench.get("!ee000041") or {}).get("group") or ""), "Vehicles")
check_true("AC12 and the id is not what a reader sees", zg not in json.dumps(_bench), zg)

# ---- AC9, AC10 the sharing table ---------------------------------------------------------------------------------
check_true("AC9 groups is a class in the sharing table", "groups" in Bridge.SHARING_CLASSES,
           str(Bridge.SHARING_CLASSES))
check_true("AC9 with an out and an in switch",
           set(Bridge.SHARING_DEFAULT.get("groups") or {}) >= {"out", "in"}, str(Bridge.SHARING_DEFAULT.get("groups")))

# ---- AC11, AC12 the lifecycle and the payload --------------------------------------------------------------------
br = read("src/mesh_manager/bridge.py") or ""
for what, hook in (("create and rename", "op_group_set"), ("delete", "op_group_delete"), ("reassign", "op_register_set")):
    i = br.find(f"def {hook}(")
    j = br.find("\n    def ", i + 10)
    check_true(f"AC11 {what} tells the peers", "_peer_share(\"groups\"" in br[i:j] or "_share_groups(" in br[i:j],
               f"{hook} is silent")
check_true("AC12 the node payload still carries the group's name",
           'n["group"] = ' in br and "groups.get(" in br, "the payload changed shape")

finish()
