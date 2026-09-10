#!/usr/bin/env python3
"""Spec 079: the name box fades, the name does not.

Matt: "I feel the name boxes need to be dimmable as well. Maybe a global slider on the transparency
of the background box rather than the whole box. On a small map with many nodes this might be hard
to read."

The existing Dim slider takes the rings, the markers and the tracks together. Applying it to a label
would fade the text with the box, which is the opposite of what a crowded map needs.
"""
import os, re, sys
sys.path.insert(0, os.path.dirname(__file__))
from _common import ROOT, check, check_true, finish, read  # noqa: E402

web = read("src/mesh_manager/web.py") or ""
i = web.index("CSS = ")
css = web[i:web.index('"""', web.index('"""', i) + 3)]

# AC1 a control of its own, separate from Dim
check_true("AC1 there is a control for the name boxes", "id='map-label-dim'" in web)
check_true("AC1 it is not the same control as Dim", "id='map-dim'" in web and "map-label-dim" != "map-dim")
check_true("AC1 it starts solid, so nothing changes until it is moved",
           re.search(r"id='map-label-dim'[^>]*value='100'", web) is not None)

# AC2 what fades is the background and the border, never the text
m = re.search(r"\.leaflet-tooltip\.mm-node\{([^}]*--lbl-a[^}]*)\}", css)
rule = m.group(1) if m else ""
check_true("AC2 the background is driven by the control", "background:color-mix" in rule)
check_true("AC2 and the border with it", "border-color:color-mix" in rule)
check_true("AC2 the text colour is left alone", "color:" not in rule.replace("border-color:", "").replace("background:", ""))

# AC3 a browser without color-mix still gets a readable label
plain = re.search(r"\.leaflet-tooltip\.mm-node,[^{]*\{([^}]*)\}", css)
check_true("AC3 an opaque rule stands before it as the fallback",
           plain is not None and "background:var(--surface-raised)" in (plain.group(1) if plain else ""))

# AC4 the choice is remembered, under its own key
check_true("AC4 remembered", "mm-lbl-a" in web)
check_true("AC4 and not confused with the Dim key", "mm-dim" in web)

# AC5 it says what it is doing, for a screen reader and for anyone who cannot see the difference
check_true("AC5 the control reports its value in words", "labelWord" in web and "aria-valuetext" in web)
check_true("AC5 including the extreme, which is a box-less name", "no box, the name alone" in web)

# AC6 the other overlay dimming is untouched: this is an addition, not a change of behaviour
check_true("AC6 Dim still takes the rings, the markers and the tracks",
           "function applyDim()" in web and "rings();dimNodes();dimTracks();" in web)

finish()
