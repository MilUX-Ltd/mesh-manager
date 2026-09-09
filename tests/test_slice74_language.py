#!/usr/bin/env python3
"""Spec 074: one vocabulary across the Meshtastic app on the operator's phone and Mesh Manager.

Every word asserted here was read off Matt's S23 with uiautomator on 9 Sep 2026, not recalled.
"""
import os, sys
sys.path.insert(0, os.path.dirname(__file__))
from _common import ROOT, check, check_true, finish, read  # noqa: E402
sys.path.insert(0, os.path.join(ROOT, "src"))
from mesh_manager import web as W  # noqa: E402

web = read("src/mesh_manager/web.py") or ""

# AC1 the signal word, checked against the app on a live mesh: SNR 6.00 and 6.50 both read "Good"
check("AC1 6.5 dB is Good, as the app said", W.band_word(6.5), "Good")
check("AC1 6.0 dB is Good, as the app said", W.band_word(6.0), "Good")
check("AC1 a weak link is Fair", W.band_word(-3), "Fair")
check("AC1 a poor link is Poor", W.band_word(-9), "Poor")
check("AC1 no reading says nothing rather than guessing", W.band_word(None), "")
check_true("AC1 the word is shown beside the bars", "sigword" in web)

# AC2 "Last heard" is the app's phrase; ours was a truncation of it
check_true("AC2 the node table says Last heard", "<th>Last heard</th>" in web)
check_true("AC2 and the register does too", web.count("<th>Last heard</th>") >= 2,
           f"{web.count('<th>Last heard</th>')} tables")

# AC3 the app calls it AirUtil; "Air time" reads as a duration, not a percentage
check_true("AC3 air utilisation, not air time", "<th>Air utilisation</th>" in web)
check_true("AC3 and channel utilisation beside it", "<th>Channel utilisation</th>" in web)

# AC4 the channel's security in the app's own words
check_true("AC4 a keyed channel reads Secure", "'Secure'" in web)
check_true("AC4 an unkeyed one reads Insecure channel", "Insecure channel" in web)
check_true("AC4 and says when position is not precise", "not precise" in web)
check_true("AC4 the chat data carries the precision", '"precise"' in web)
check_true("AC4 an open padlock for the insecure case", "lockopen" in web)

# AC5 what we did NOT take: our own decisions stand
check_true("AC5 the node list is still a table, not cards", "<th>Node</th>" in web)
check_true("AC5 the empty state still explains itself",
           "A quiet mesh is not a broken bridge" in web)
check_true("AC5 the brand accent is still the heading colour",
           "--accent" in web and "#113308" in web)

finish()
