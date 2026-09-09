#!/usr/bin/env python3
"""Spec 073: the composer counts the bytes the box counts, and the chat head says if a channel is keyed."""
import os, re, sys
sys.path.insert(0, os.path.dirname(__file__))
from _common import ROOT, check, check_true, finish, read  # noqa: E402
sys.path.insert(0, os.path.join(ROOT, "src"))
from mesh_manager import catalogue as C  # noqa: E402

web = read("src/mesh_manager/web.py") or ""

# AC1: the count must be bytes, because that is what the box enforces
check_true("AC1 the screen counts UTF-8 bytes, not characters", "TextEncoder" in web)
check_true("AC1 and it is not counting .length of the string",
           not re.search(r"count[^\n]{0,40}\.value\.length", web))
degree = "x" * 197 + "°°°"
check("AC1 a degree sign costs two bytes", (len(degree), len(degree.encode())), (200, 203))

# AC4: the limits come from the catalogue, so the screen and the box cannot disagree
def limit(op, field="text"):
    spec = [o for o in C.ACTIONS if o.get("op") == op][0]
    return [i for i in spec["inputs"] if i["name"] == field][0]["max_bytes"]
check("AC4 send_text is 200 bytes in the catalogue", limit("send_text"), 200)
check("AC4 peer_send_text is 180 bytes in the catalogue", limit("peer_send_text"), 180)
check_true("AC4 the screen takes both limits from the catalogue, not a literal",
           "SEND_MAX" in web and "PEER_MAX" in web)

# AC2: rendered under both composers
check_true("AC2 the channel composer has a count", web.count("data-bytecount") >= 2,
           f"{web.count('data-bytecount')} composers carry one")

# AC3: the screen refuses a send past the limit
check_true("AC3 the send button is disabled past the limit",
           "chat-over" in web and re.search(r"\.disabled\s*=\s*over", web) is not None)

# AC5 and AC6: the key state reaches the chat head
check_true("AC5 the chat data carries has_key for each channel",
           re.search(r'"has_key":\s*bool\(c\.get\("has_key"\)\)', web) is not None)
check_true("AC6 the chat head shows a lock for a keyed channel",
           "chatLock(c)" in web and re.search(r"\.name'\)\.insertAdjacentHTML\('beforeend',chatLock", web) is not None)
check_true("AC6 and the lock is styled so it does not sit on the name", ".chat-lock{" in web)

finish()
