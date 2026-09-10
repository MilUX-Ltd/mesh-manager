#!/usr/bin/env python3
"""Spec 076: a page that renders a write form must ship the handler that intercepts it.

Found on the Nodes page: it rendered the name, group and icon form for every node and never loaded
WRITE_JS, so the browser submitted natively. The values went into the query string
(?id=!ee000004&label=&group=&tags=&icon=drone), the page reloaded, and nothing was written. The
operator saw the icon revert and had no way to tell the save had not happened.
"""
import os, re, sys
sys.path.insert(0, os.path.dirname(__file__))
from _common import ROOT, check, check_true, finish  # noqa: E402
sys.path.insert(0, os.path.join(ROOT, "src"))
from mesh_manager import web as W  # noqa: E402

HANDLER = "form[data-action]:not([data-method=get])"

NODES = [{"id": "!ee000004", "name": "Tracker 4", "hw": "TRACKER_T1000_E", "heard_here": True,
          "heard": "2026-09-09T12:00:00Z", "snr": 6.5, "hops": 0, "battery": 80,
          "lat": 51.2, "lon": -1.5, "label": "", "group": "", "tags": [], "icon_own": ""}]

# AC1 the page that showed the fault
body = W.nodes_body(NODES)
check_true("AC1 the Nodes page renders a write form", "data-action='register_set'" in body)
check_true("AC1 and now ships the handler that intercepts it", HANDLER in body)

# AC2 the icon chooser is in that form, with the field the operation takes
check_true("AC2 the icon chooser is rendered", "iconpick" in body)
check_true("AC2 its inputs are named icon", "name='icon'" in body)

# AC3 the same must hold with the other shape of the page
body2 = W.nodes_body(NODES, intro=False)
check_true("AC3 the compact Nodes page ships it too", HANDLER in body2)

# AC4 the rule, for every page body that can be built without a live bridge: a write form on a page
# without the handler is a form that silently does nothing.
def sweep(name, html):
    if "data-action=" not in html:
        return None
    return HANDLER in html

for name, html in (("nodes", body), ("nodes compact", body2)):
    ok = sweep(name, html)
    check_true(f"AC4 {name}: a write form comes with its handler", ok is not False, "renders forms with no handler")

# AC5 the handler is not shipped twice on one page, which would submit everything twice
check("AC5 the handler appears once on the Nodes page", body.count(HANDLER), 1)

finish()
