#!/usr/bin/env python3
"""Spec 090: a group has a colour, and the COP uses it.

Groups already carried free-text names, one group per node, and a deletion that returns nodes to
ungrouped. The only thing missing was a colour that reads at a glance across a field of pins.

Two decisions are held here. Ungrouped goes grey once the box has a group and keeps its identity
colour while it has none, so an existing box does not go monochrome on upgrade. And a group's
colour comes from Spec 081's eight, which exclude green, amber and red because those already mean
signal band and alert state on this map.
"""
import json, os, re, sys, tempfile
sys.path.insert(0, os.path.dirname(__file__))
from _common import ROOT, check, check_true, finish, read  # noqa: E402
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "tests"))
import fakegw_lib  # noqa: E402
fakegw_lib.install()
from mesh_manager import catalogue as C  # noqa: E402
from mesh_manager.bridge import Bridge  # noqa: E402

web_src = read("src/mesh_manager/web.py") or ""


class B(Bridge):
    """Just enough bridge to exercise the group store."""
    def __init__(self, d):
        self.state_dir = d
        self._emit = lambda *a, **k: None


d = tempfile.mkdtemp()
b = B(d)

# ---- AC1 a colour is stored, and one is optional ------------------------------------------------
PALETTE = getattr(C, "GROUP_COLOURS", ())
check_true("AC1 there is a named palette to choose from", len(PALETTE) >= 6, str(PALETTE))
first = (PALETTE or ["node-1"])[0]
r = b.op_group_set(name="Recce", colour=first)
check_true("AC1 a group takes a colour", "error" not in r, str(r))
check("AC1 and reports it back", ((r.get("group") or {}).get("colour")), first)
r2 = b.op_group_set(name="No colour here")
check_true("AC1 a group without one still works", "error" not in r2, str(r2))

# ---- AC3 outside the palette is refused -----------------------------------------------------------
bad = b.op_group_set(name="Bad", colour="#ff0000")
check_true("AC3 a colour outside the palette is refused", "error" in bad, str(bad))
check_true("AC3 and the message says what is allowed",
           any(p in str(bad.get("error", "")) for p in PALETTE), str(bad.get("error"))[:140])

# ---- AC4 duplicates are allowed, silently -----------------------------------------------------------
dup = b.op_group_set(name="Also Recce", colour=first)
check_true("AC4 two groups may hold the same colour", "error" not in dup, str(dup))
check_true("AC4 with no warning in the answer", "warn" not in json.dumps(dup).lower(), str(dup))

# ---- AC5 groups reports colours ------------------------------------------------------------------------
gs = {g["name"]: g for g in (b.op_groups().get("groups") or [])}
check("AC5 groups reports the colour it stored", (gs.get("Recce") or {}).get("colour"), first)
check_true("AC5 and a group without one says so plainly",
           (gs.get("No colour here") or {}).get("colour") in (None, ""), str(gs.get("No colour here")))

# ---- AC9 deleting returns nodes to ungrouped, which must not regress --------------------------------------
check_true("AC9 a group can still be deleted", "error" not in b.op_group_delete(name="Also Recce"))
check_true("AC9 and it is gone", "Also Recce" not in {g["name"] for g in (b.op_groups().get("groups") or [])})

# ---- AC2 the catalogue carries it -------------------------------------------------------------------------
a = C.by_id("group_set") or {}
names = {i["name"] for i in a.get("inputs", [])}
check_true("AC2 the catalogue's group_set takes a colour", "colour" in names, str(sorted(names)))
col = next((i for i in a.get("inputs", []) if i["name"] == "colour"), {})
check("AC2 it is an enum, so the screen and an agent get the same list", col.get("type"), "enum")
check("AC2 offering exactly the palette", sorted(col.get("values") or []), sorted(PALETTE))
check_true("AC2 and it is not required", not col.get("required"))

# ---- AC6, AC7 the map ---------------------------------------------------------------------------------------
# the colour a node draws in is decided in one place, and that place knows about groups
m = re.search(r"function groupColour\([\s\S]{0,700}?\n  \}", web_src)
check_true("AC6 there is one function deciding a node's colour with its group", m is not None,
           "no groupColour in the map")
body = m.group(0) if m else ""
check_true("AC6 it uses the group's colour when the node has one", "group" in body, body[:200])
check_true("AC7 it falls back to the identity colour, not grey, when no group exists anywhere",
           "nodeColour" in body, body[:200])
check_true("AC7 and it greys an ungrouped node once a group exists",
           re.search(r"ungrouped|grey|gray", body, re.I) is not None, body[:200])
# the call sites take the whole node, not just its id, or they cannot know its group
_bare = re.findall(r"nodeColour\((?:n|tr)\.id\)", web_src)
check("AC6 no call site still colours by id alone", _bare, [])

# ---- AC8 the grey is a fixed token ---------------------------------------------------------------------------
check_true("AC8 the ungrouped grey is a design token", "--node-none" in web_src, "no --node-none token")
_dark = len(re.findall(r"--node-none\s*:", web_src))
check_true("AC8 defined for the light ground and the dark one", _dark >= 2, f"{_dark} definitions")
check("AC8 the grey is not settable as a group colour",
      [p for p in PALETTE if "none" in str(p)], [])

# ---- AC10 nothing knows a group's name -------------------------------------------------------------------------
for word in ("staff", "trainee", "trainees"):
    hits = [f for f in ("src/mesh_manager/bridge.py", "src/mesh_manager/web.py", "src/mesh_manager/catalogue.py")
            if re.search(rf"['\"]{word}['\"]", read(f) or "", re.I)]
    check(f"AC10 no group named {word!r} is baked in anywhere", hits, [])

# ---- AC11 the screen ---------------------------------------------------------------------------------------------
gsec = web_src[web_src.find("def groups_section("):web_src.find("def register_body(")]
check_true("AC11 the Groups section sets a colour", "colour" in gsec, "no colour control in the groups section")
check_true("AC11 and shows the one a group holds",
           re.search(r"colour", gsec) is not None and "swatch" in gsec, "no swatch rendering the group's colour")

finish()
