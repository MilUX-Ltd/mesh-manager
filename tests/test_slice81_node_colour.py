#!/usr/bin/env python3
"""Spec 081: a colour of its own.

Matt: "Tracker names display as the same as points on the map. Points should have different icons
and colours and a different-coloured box for the name."

The icons already varied. The colour did not: every pin was the same brand green and every name sat
in the same white box. The catch is that green, amber and red already mean something on this map, so
an identity palette has to keep out of their way.
"""
import os, re, sys
sys.path.insert(0, os.path.dirname(__file__))
from _common import ROOT, check, check_true, finish, read  # noqa: E402

web = read("src/mesh_manager/web.py") or ""
i = web.index("CSS = ")
css = web[i:web.index('"""', web.index('"""', i) + 3)]


def lum(h):
    h = h.lstrip("#")
    c = [int(h[k:k + 2], 16) / 255 for k in (0, 2, 4)]
    c = [(x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4) for x in c]
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]


def contrast(a, b):
    la, lb = lum(a), lum(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def hue(h):
    h = h.lstrip("#")
    r, g, b = [int(h[k:k + 2], 16) / 255 for k in (0, 2, 4)]
    mx, mn = max(r, g, b), min(r, g, b)
    if mx == mn:
        return 0.0
    d = mx - mn
    if mx == r:
        return (60 * ((g - b) / d) + 360) % 360
    if mx == g:
        return (60 * ((b - r) / d) + 120) % 360
    return (60 * ((r - g) / d) + 240) % 360


# AC1 the colour comes from the id, and the same id always gives the same colour
check_true("AC1 there is a colour per node, derived from its id", "function nodeColour(id)" in web)
m = re.search(r"function nodeColour\(id\)\{(.+?)\n", web, re.S)
body = m.group(1) if m else ""
after = (web.split("function nodeColour(id)", 1) + [""])[1][:300]
check_true("AC1 it hashes the id rather than counting markers", "charCodeAt" in body and "%" in after)
check_true("AC1 nothing is stored, so every screen agrees", "nodeColour" in web and "localStorage.setItem('mm-node-col" not in web)

# AC2/AC3/AC4 the palette itself
light = dict(re.findall(r"--(node-\d):(#[0-9A-Fa-f]{6})", css.split("[data-theme=dark]", 1)[0]))
# a suite that crashes tells you nothing: every lookup below survives the code not being there yet.
dark = dict(re.findall(r"--(node-\d):(#[0-9A-Fa-f]{6})", (css.split("[data-theme=dark]", 1) + [""])[1].split("}", 1)[0]))
check("AC3 eight identity tokens in the light block", len(light), 8)
check("AC3 and eight in the dark block", len(dark), 8)
check_true("AC3 anchored on the MilUX blue-grey", light.get("node-1", "").upper() == "#586F7C")
check_true("AC3 the JavaScript reads the tokens, it does not carry hex of its own",
           "var(--node-1)" in web and not re.search(r"NODE_COLOURS=\[[^]]*#", web))

# green, amber and red are the bands and the lamps: an identity colour must not land on them
for name, vals, ground in (("light", light, "#FFFFFF"), ("dark", dark, "#182416")):
    for tok, val in sorted(vals.items()):
        h = hue(val)
        check_true(f"AC2 {name} --{tok} ({val}) is not a green, amber or red", not (h < 70 or 80 <= h <= 170))
        r = contrast(val, ground)
        check_true(f"AC4 {name} --{tok} clears 4.5:1 on its ground ({r:.2f})", r >= 4.5)
check_true("AC2 no two nodes share a colour by accident: the eight are distinct", len(set(v.upper() for v in light.values())) == 8)

# AC5 the pin wears it
check_true("AC5 the pin takes a colour", re.search(r"function nodeIcon\(kind,extra,col\)", web) is not None)
pin = (web.split("function nodeIcon(kind,extra,col)", 1) + [""])[1][:400]
check_true("AC5 on its ring", "border-color:" in pin)
check_true("AC5 and on its glyph", "color:" in pin.replace("border-color:", ""))

# AC6 the name box wears it too, and the Spec 079 slider still empties it
check_true("AC6 the label is tinted with the node's colour", "function tintLabel(" in web and "--lbl-tint" in web)
rule = re.search(r"\.leaflet-tooltip\.mm-node\{([^}]*--lbl-a[^}]*)\}", css)
rule = rule.group(1) if rule else ""
check_true("AC6 the tint feeds the same mix the Names slider drives", "--lbl-tint" in rule and "--lbl-a" in rule)
check_true("AC6 with the plain surface as the fallback when no node is named",
           "var(--lbl-tint,var(--surface-raised))" in rule)
check_true("AC6 the border follows the same colour", "var(--lbl-edge,var(--line))" in rule)
check_true("AC6 the text colour is still left alone",
           "color:" not in rule.replace("border-color:", "").replace("background:", "").replace("color-mix", "mix"))

# AC7 both layers, and playback by node. Named rather than counted: Spec 083 folded the two live call
# sites into one drawNodes, and a count would have failed on a change that kept the guarantee.
calls = [l.strip() for l in web.splitlines() if "nodeIcon(" in l and "function nodeIcon(" not in l]
# Each call passes a third argument, and the colour reaching it is derived from the node. Spec 090
# put one decider in front of nodeColour, groupColour, which returns the group's colour, the fixed
# grey, or the identity colour. Naming nodeColour here pinned the check to the mechanism instead of
# the guarantee, and it failed on a change that kept the guarantee exactly.
COLOURERS = ("groupColour(", "nodeColour(")


def coloured(line):
    arg = line.split("nodeIcon(", 1)[1]
    third = arg.split(",", 2)[2] if arg.count(",") >= 2 else ""
    return any(c in third for c in COLOURERS) or re.search(r"\bcol\b", third) is not None


check_true("AC7 every marker is drawn with a colour", bool(calls) and all(coloured(c) for c in calls))
check_true("AC7 and that colour is the node's own",
           re.search(r"\bcol\s*=\s*(group|node)Colour\(", web) is not None)
live = [c for c in calls if "'play'" not in c]
play = [c for c in calls if "'play'" in c]
check_true("AC7 the live layer is covered, through the one draw both maps use",
           len(live) == 1 and "function drawNodes(" in web)
check_true("AC7 and the playback layer with it", len(play) == 1)
# the point being played carries no identity; the colour must come from the node it belongs to
check_true("AC7 playback colours by the node being played, not by the point",
           len(play) == 1 and re.search(r"(group|node)Colour\((nn|tr)\b", play[0]) is not None,
           (play or ["none"])[0][-90:])

# AC8 the icon was already per-node and stays that way
check_true("AC8 the icon still comes from the node", "nodeIcon(n.icon," in web and "nodeIcon(nn.icon," in web)
br = read("src/mesh_manager/bridge.py") or ""
check_true("AC8 and the bridge still resolves it own-then-group-then-radio", '"radio"' in br and 'n["icon"]' in br)

finish()
