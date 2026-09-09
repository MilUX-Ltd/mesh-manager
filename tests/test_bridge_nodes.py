#!/usr/bin/env python3
"""test_bridge_nodes.py - mesh_nodes() in the bridge (carried from the earlier gateway build, Spec 003).

Named to the console glob on purpose, so CONTRIBUTING's one gate
(`for t in console/test-console-*.py`) runs it. It does not test the console: it tests
the `mesh_nodes()` the gateway patch installs, because that function decides what the map
is ever able to draw, and it is the one piece of this feature that no console stub can
reach.

The function is EXTRACTED FROM THE PATCH rather than copied here, so this suite cannot
quietly drift away from what actually ships to a box.

    python3 tests/test_bridge_nodes.py
"""
import os
import sys

FAILURES = 0


def check(name, got, want):
    global FAILURES
    ok = got == want
    if not ok:
        FAILURES += 1
    print(f"{'ok  ' if ok else 'FAIL'} {name:58s} got {got!r} want {want!r}")


def check_true(name, cond, detail=""):
    global FAILURES
    if not cond:
        FAILURES += 1
    print(f"{'ok  ' if cond else 'FAIL'} {name:58s} {detail}")


HERE = os.path.dirname(os.path.abspath(__file__))
PATCH = os.path.join(HERE, os.pardir, "bridge",
                     "patches", "gateway-01-tak_meshtastic_gateway.patch")

# the "+" side of a whole-file diff IS the patched file
with open(PATCH) as fh:
    lines = fh.read().split("\n")
_diff_start = next(i for i, ln in enumerate(lines) if ln.startswith('--- '))  # after the provenance header
patched = "\n".join(ln[1:] for ln in lines[_diff_start + 3:] if ln.startswith("+"))
check_true("the patch still carries mesh_nodes", "def mesh_nodes(self):" in patched)
if "def mesh_nodes(self):" not in patched:
    print(f"\nFAILURES: {FAILURES}")
    sys.exit(1)

body = patched[patched.index("    def mesh_nodes(self):"):patched.index("    def heartbeat(self):")]
ns = {}
exec("class G:\n" + body, ns)          # noqa: S102 - the point is to run the shipped code
G = ns["G"]


def nodes(devices, radio=None):
    g = G()
    g.meshtastic_devices = devices
    g._mesh_radio = radio or {}
    return g.mesh_nodes()


def dev(name, lat=None, lon=None, battery=0, mid=""):
    return {"long_name": name, "battery": battery, "meshtastic_id": mid,
            "last_lat": "0.0" if lat is None else lat,
            "last_lon": "0.0" if lon is None else lon}


# ---------- names are a LABEL, never an identity (the MILUX-T2 lesson) -------------------
# Live, 31 Aug 2026: a tracker renamed to Tracker2 months earlier still arrived as MILUX-T2,
# because the gateway loads the radio's stored node database at startup and a NODEINFO rename
# could never overwrite a name already there. An earlier cut of the duplicate fix matched
# records on the callsign - which would have put THIS node's position onto a different radio.
# A device's Meshtastic long_name and its ATAK callsign are separate fields that need not
# agree, and either can be stale. The join is the radio id and nothing else.
out = nodes({
    "!ee000022": dev("Tracker2", mid="!ee000022"),
    "!ee000023": dev("Tracker 2 (old record)", battery=79, mid="!ee000023"),
    # the ATAK contact rode in on !ee000023 but calls itself Tracker2
    "Tracker2": dev("Tracker2", lat=51.21277, lon=-1.505802, battery=83, mid="!ee000023"),
}, {"!ee000022": {"heard": "t1", "snr": 14.0, "hops": 0},
    "!ee000023": {"heard": "t2", "snr": 13.8, "hops": 0}})
placed = [n for n in out if "lat" in n]
check("callsign clash: 3 records -> 2 nodes", len(out), 2)
check("the position lands on the radio that SENT it", 
      placed[0]["id"] if placed else None, "!ee000023")
check_true("and NOT on the node that merely shares the callsign",
           all("lat" not in n for n in out if n["id"] == "!ee000022"))

# ---------- heard here, vs merely known from the radio's database ------------------------
out = nodes({"!ee000027": dev("MILUX-T1", mid="!ee000027"),
             "!ee000022": dev("Tracker2", mid="!ee000022")},
            {"!ee000022": {"heard": "t", "snr": 9.0, "hops": 0}})
g = {n["id"]: n for n in out}
check("a nodedb entry never heard is flagged as such",
      g["!ee000027"]["heard_here"], False)
check("a node actually heard is flagged heard", g["!ee000022"]["heard_here"], True)

# ---------- the live defect: one tracker, two records, neither complete ------------------
# The shape of a real defect from the first live mesh (30 Aug 2026): a tracker arrived under its radio
# id AND under its ATAK callsign. The radio record had snr/hops and no position; the uid
# record had the position and no radio data. Emitting both drew one tracker twice - plotted
# but reading stale with unknown hops, and again in "reporting without a position".
live = {
    "!ee000022": dev("Tracker2", mid="!ee000022"),
    "Tracker2": dev("Tracker2", lat=51.50018, lon=-0.119700, battery=83, mid="!ee000022"),
    "!ee000023": dev("Tracker 2 (old record)", battery=79, mid="!ee000023"),
    "!ee000026": dev("Vault Sync", mid="!ee000026"),
}
radio = {"!ee000022": {"heard": "2026-08-30T23:14:42Z", "snr": 13.5, "hops": 0},
         "!ee000023": {"heard": "2026-08-30T23:13:33Z", "snr": 13.5, "hops": 0}}
out = nodes(live, radio)
t2 = [n for n in out if n["name"] == "Tracker2"]
check("live case: 4 records collapse to 3 nodes", len(out), 3)
check("live case: Tracker2 appears exactly ONCE", len(t2), 1)
if t2:
    check("live case: the surviving record keeps the position", "lat" in t2[0], True)
    check("live case: and the radio data from the other record", t2[0].get("snr"), 13.5)
    check("live case: and the hop count, so the link is not 'unknown'",
          t2[0].get("hops"), 0)
    check("live case: and it reads fresh, not stale",
          t2[0].get("heard"), "2026-08-30T23:14:42Z")
    check("live case: and the battery the uid record carried", t2[0].get("battery"), 83)
    check("live case: it is keyed by the RADIO id, not the callsign",
          t2[0]["id"], "!ee000022")

# ---------- the other ATAK path: the uid record that DOES know its radio id --------------
out = nodes({"!aaa1": dev("T9", mid="!aaa1"),
             "T9": dev("T9", lat=51.5, lon=-0.1, battery=50, mid="!aaa1")},
            {"!aaa1": {"heard": "t", "snr": 4.0, "hops": 1}})
check("uid record carrying its radio id merges too", len(out), 1)
check_true("and carries both halves",
           out and "lat" in out[0] and out[0]["snr"] == 4.0 and out[0]["battery"] == 50)

# ---------- two radios sharing a callsign are two nodes, always ---------------------------
out = nodes({"!b1": dev("DUPE", mid="!b1"), "!b2": dev("DUPE", mid="!b2")})
check("two radios sharing a callsign stay separate", len(out), 2)

# ---------- the rule the whole feature rests on ------------------------------------------
out = nodes({"!c1": dev("NOFIX", battery=20, mid="!c1")})
check("a node with no fix has NO position, never 0,0", "lat" in out[0], False)
check("but is still reported - it is on the mesh", out[0]["name"], "NOFIX")
out = nodes({"!c2": dev("ZERO", lat=0, lon=0, mid="!c2")})
check("an explicit 0,0 is treated as no fix, not as null island",
      "lat" in out[0], False)

# ---------- radio data is only ever claimed where the radio actually heard it ------------
out = nodes({"!d1": dev("UNHEARD", mid="!d1")})
check("a node not heard since restart reports no snr", out[0]["snr"], None)
check("and no hop count - never a confident 0", out[0]["hops"], None)
check("and no heard time", out[0]["heard"], None)

# ---------- a nameless node still gets an identity ---------------------------------------
out = nodes({"!e1": dev("", mid="!e1")})
check("a node with no callsign falls back to its radio id", out[0]["name"], "!e1")

# ---------- the checker must not silently drop a field the map depends on ---------------
# Found live 31 Aug: the gateway grew heard_here, the console read it, and tak-health.sh's
# re-emit whitelist did not carry it - so it never reached the console. The console's
# fallback masked it. A whitelist is the right call (nothing reaches a snapshot the checker
# did not name), but it has to be kept in step with what the gateway emits.
# The checker is the contract's other half and lives in the estate's console repository. It is
# checked when TAK_HEALTH_SH names it; otherwise the line reads SKIP, never ok, so a missing
# contract check is visible.
CHECKER = next((c for c in [os.environ.get("TAK_HEALTH_SH", "")] if c and os.path.exists(c)), "tak-health.sh (not found)")
emitted = set()
for n in nodes({"!z1": dev("Z", lat=1, lon=2, battery=5, mid="!z1")},
               {"!z1": {"heard": "t", "snr": 1.0, "hops": 0}}):
    emitted |= set(n)
if os.path.exists(CHECKER):
    with open(CHECKER) as fh:
        checker = fh.read()
    missing = sorted(f for f in emitted if f'"{f}"' not in checker)
    check("every field the gateway emits survives the checker whitelist", missing, [])
else:
    print(f"SKIP every field the gateway emits survives the checker whitelist   (no tak-health.sh at {CHECKER})")

print()
print(f"FAILURES: {FAILURES}")
sys.exit(1 if FAILURES else 0)
