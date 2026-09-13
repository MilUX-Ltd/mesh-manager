#!/usr/bin/env python3
"""Spec 091: filter the COP by group, many at once, and only here.

The filter that existed was a single select, one group or everyone, which cannot express two of
ten and has no way to say "the ungrouped ones". The card is firm that show all is a filter state
and ungrouped is a category of node, and that conflating them makes both unusable.

The filter is per screen. Group definitions are shared; the view of them is not.
"""
import os, re, sys
sys.path.insert(0, os.path.dirname(__file__))
from _common import ROOT, check, check_true, finish, read  # noqa: E402

web = read("src/mesh_manager/web.py") or ""

# ---- AC5 one decision function, asked by all three layers --------------------------------------
m = re.search(r"function shownHere\(([\s\S]{0,600}?)\n  \}", web)
check_true("AC5 there is one function deciding whether a node is shown", m is not None,
           "no shownHere in the map")
body = m.group(0) if m else ""
_askers = len(re.findall(r"shownHere\(", web))
check_true("AC5 the map, the trails and the playback all ask it", _askers >= 4, f"{_askers} call sites")
# the old single-select decision must be gone, or two answers exist at once
check("AC5 the single-select filter is gone", re.findall(r"function groupChosen\(", web), [])
check("AC5 and nothing still compares a group for equality to filter",
      re.findall(r"\(n\.group\|\|''\)!==g\b|\(groups_\[r\.node\]\|\|''\)!==g\b", web), [])

# ---- AC1, AC2, AC3, AC4 the model -----------------------------------------------------------------
check_true("AC1 the selection is a set, not a value", re.search(r"(SEL|sel)\s*=\s*\{\}", web) is not None,
           "no set-shaped selection")
check_true("AC2 the ungrouped are a category of their own",
           re.search(r"UNGROUPED_KEY|'__ungrouped__'|\"__ungrouped__\"", web) is not None,
           "no distinct key for the ungrouped")
# show all is the empty selection, and it is not the same thing as the ungrouped category
check_true("AC3 show all is the state where nothing is selected",
           re.search(r"none selected|nothing selected|show all", body, re.I) is not None, body[:200])
check_true("AC4 a node with no group is matched by the ungrouped key, not by an empty name",
           re.search(r"UNGROUPED_KEY", body) is not None, body[:220])

# ---- AC6 it survives a reload --------------------------------------------------------------------------
check_true("AC6 the selection is kept in localStorage", "mm-group-filter" in web, "no stored key")
check_true("AC6 and read back on load", re.search(r"getItem\('mm-group-filter'\)", web) is not None)

# ---- AC7 nothing leaves the browser -----------------------------------------------------------------------
# the filter must not ride on any request, nor be written by any action
_leaks = []
for pat in (r"group_filter", r"groups_selected", r"filter=.*group"):
    _leaks += [l.strip()[:90] for l in web.splitlines()
               if re.search(pat, l) and ("fetch(" in l or "/api/" in l or "data-action" in l)]
check("AC7 no request carries the filter", _leaks, [])
check("AC7 and no catalogue action writes it",
      [a for a in re.findall(r"\"id\": \"([a-z_]+)\"", read("src/mesh_manager/catalogue.py") or "")
       if "group_filter" in a or "filter_set" in a], [])

# ---- AC8 the control says what is selected ---------------------------------------------------------------------
check_true("AC8 the control reports its own state without being opened",
           re.search(r"filterWord|summaryWord|gfWord", web) is not None,
           "nothing writes a summary of the selection")

# ---- AC1, AC2 the control itself, not only the model -----------------------------------------------------
# The model passed while the screen still rendered the old single select, which nothing read any
# more. A filter nobody can reach is not a filter.
check_true("AC1 the control is rendered as a checkbox per group, not a select",
           "class='gf'" in web and "id='gfilter'" in web, "no multi-select control in the page")
# scoped to the map: the Nodes page has a group filter of its own on a list, which this card does
# not touch. An unscoped check condemned it and would have had me rip out a working control.
_mapsrc = web[web.find("def mesh_views("):web.find("def waypoint_card(") if "def waypoint_card(" in web else web.find("def mesh_views(") + 6000]
check("AC1 the COP's old single select is gone", re.findall(r"id='group-filter'", _mapsrc), [])
check_true("AC2 and the ungrouped have their own box in it",
           re.search(r"value='__ungrouped__'", web) is not None, "no ungrouped checkbox")
check_true("AC3 there is a show-all control", "id='gf-all'" in web, "no way back to everything")
check_true("AC8 the summary shows the word without opening the fold", "id='gf-word'" in web)

# ---- AC9 absent when there is nothing to filter -------------------------------------------------------------------
mv = web[web.find("def mesh_views("):web.find("def mesh_views(") + 4000]
check_true("AC9 with no group at all the control is not rendered",
           re.search(r"if groups else", mv) is not None, "no empty case in mesh_views")

finish()
