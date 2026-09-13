#!/usr/bin/env python3
"""Spec 095: over the air moves to the node's own page.

Four forms and three tick boxes inside one cell of the register table, unusable on a phone and a
page of forms on a laptop. And Forget one press from the node's name, which puts the most
destructive action on the page next to the thing an operator reaches for.
"""
import os, re, sys
sys.path.insert(0, os.path.dirname(__file__))
from _common import ROOT, check, check_true, finish, read  # noqa: E402

web = read("src/mesh_manager/web.py") or ""
OTA = ("node_set", "node_set_region", "node_channel_push", "node_reboot")


def fn(name):
    i = web.find(f"def {name}(")
    if i < 0:
        return ""
    j = web.find("\ndef ", i + 10)
    return web[i:j if j > 0 else len(web)]


rows = fn("register_rows")
# the node page is what node_body renders, and it renders a section built next door. Slicing one
# function and calling it "the page" asserts a mechanism rather than a guarantee: what matters is
# what the page carries, not which function holds the markup.
_nb = fn("node_body")
node = _nb + fn("manage_section")
check_true("both pages are there to compare", bool(rows) and bool(_nb))
check_true("AC4 the node page pulls in a manage section", "manage_section(" in _nb,
           "node_body never renders it")

# ---- AC1, AC3, AC8 what leaves the register ------------------------------------------------------
_left = [a for a in OTA if f"data-action='{a}'" in rows or f"manage_forms(" in rows]
check("AC1 no over-the-air form is left on the register row", _left, [])
check("AC3 and Forget has gone with them", re.findall(r"data-action='node_forget'", rows), [])
check_true("AC8 the register still carries label and holder",
           "name='label'" in rows and "name='holder'" in rows, "the register lost its own fields")
check_true("AC8 and the inventory columns", "_fwcell(" in rows and "_keycell(" in rows)

# ---- AC2 the way through -------------------------------------------------------------------------------
check_true("AC2 the row links to the node's page",
           re.search(r"href='/node\?id=", rows) is not None, "no link to the node")

# ---- AC4, AC5, AC7 what arrives on the node page ----------------------------------------------------------
check_true("AC4 the node page has a Manage heading", re.search(r">Manage<|'Manage'|\"Manage\"", node) is not None,
           "no Manage section")
check_true("AC4 and carries the over-the-air forms", "manage_forms(" in node,
           "the forms did not arrive")
check_true("AC7 through the same helper, not a second copy",
           not any(f"data-action='{a}'" in node for a in OTA),
           "the node page writes its own copies of the forms")
check_true("AC5 Forget is there", "node_forget" in node, "Forget went nowhere")
# the order the operator sees is the order of the concatenation the section returns, not the order
# the variables happen to be assigned in. Third time today a check has read source position and
# meant render order.
_ret = fn("manage_section")
_ret = _ret[_ret.rfind("return "):]
_mi, _fi = _ret.find("manage_forms("), _ret.find("forget")
check_true("AC5 and it sits below the forms in what the page returns", _mi > 0 and _fi > _mi,
           _ret[-200:])

# ---- AC6 an unmanaged device -------------------------------------------------------------------------------
check_true("AC6 an unmanaged device is told why rather than shown nothing",
           re.search(r"not managed", node) is not None, "nothing explains the absence")

finish()
