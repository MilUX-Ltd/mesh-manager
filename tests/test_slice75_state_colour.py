#!/usr/bin/env python3
"""Spec 075: in a data surface, the only saturated colour is state.

--accent (#113308, the MilUX brand) and --ok (#2E6B30) are 2.17:1 apart and 15 degrees of hue
apart. Painted in the same picture, a link that means "good" does not read against nodes that mean
nothing but "a node". The palette does not change; only where each token is allowed.
"""
import os, re, sys
sys.path.insert(0, os.path.dirname(__file__))
from _common import ROOT, check, check_true, finish, read  # noqa: E402

web = read("src/mesh_manager/web.py") or ""
i = web.index("CSS = ")
css = web[i:web.index('"""', web.index('"""', i) + 3)]

DATA_SURFACE = re.compile(r"\.map\s|\.map\.|\.chart|\.spark|\.sig|\.linkbar")

# AC1 no brand green inside a surface that carries state colour
offenders = []
for m in re.finditer(r"([^;{}\n]{1,70})\{([^}]*var\(--accent\)[^}]*)\}", css):
    sel = m.group(1).strip()
    if DATA_SURFACE.search(sel):
        offenders.append(sel)
check("AC1 no data surface paints with the brand accent", offenders, [])

# AC2 the four places that did are now on --edge, which is brand too and 94 degrees away
for sel, prop in ((r"\.map \.node", "stroke"), (r"\.map \.own", "fill"),
                  (r"\.chart polyline", "stroke"), (r"\.spark polyline", "stroke"),
                  (r"\.linkbar \.hop\.origin", "border-color")):
    m = re.search(sel + r"[^{]*\{([^}]*)\}", css)
    body = m.group(1) if m else ""
    check_true(f"AC2 {sel.replace(chr(92), '')} uses --edge", f"{prop}:var(--edge)" in body, body[:60])

# AC3 the brand is untouched where it belongs: chrome
for sel in ("header", "h1", "button", ".chip.on", "header nav.primary"):
    hits = [m.group(1) for m in re.finditer(r"([^;{}\n]{1,80})\{([^}]*var\(--accent\)[^}]*)\}", css)
            if m.group(1).strip() == sel]
    check_true(f"AC3 {sel} still carries the brand", bool(hits), "not painted with the accent")

# AC4 the palette itself did not change
for tok, val in (("accent", "#113308"), ("ok", "#2E6B30"), ("edge", "#586F7C"), ("gold", "#B5B171")):
    check_true(f"AC4 --{tok} is still {val}", f"--{tok}:{val}" in css)

# AC5 state colour still reaches the places that mean state
for sel in (".lamp--ok", ".linkbar .hop.band-2"):
    check_true(f"AC5 {sel} still carries state colour",
               re.search(re.escape(sel) + r"[^{]*\{[^}]*var\(--(ok|warn|bad)\)", css) is not None)

finish()
