#!/usr/bin/env python3
"""Spec 007: the screen, second pass, against the fake bridge."""
import http.client
import json
import os
import re
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(__file__))
from _common import ROOT, check, check_true, finish  # noqa: E402
sys.path.insert(0, os.path.join(ROOT, "src"))
from fakebridge_lib import start_fake_bridge, NODES  # noqa: E402
try:
    from mesh_manager import web as W
except Exception as e:  # noqa: BLE001
    print(f"FAIL imports                                                     {type(e).__name__}: {e}")
    print("\nFAILURES: 1"); sys.exit(1)

fb = start_fake_bridge()
etc = tempfile.mkdtemp()
srv = W.make_server(bind="127.0.0.1", port=0, socket_path=fb.path, etc_dir=etc, config={"AUTH": "off"})
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
time.sleep(0.4)


def req(method, path, body=None, ctype="application/x-www-form-urlencoded"):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    h = {"Content-Type": ctype} if body is not None else {}
    c.request(method, path, body=body, headers=h)
    r = c.getresponse(); data = r.read(); hd = dict((k.lower(), v) for k, v in r.getheaders())
    c.close(); return r.status, hd, data.decode("utf-8", "replace")


def lum(hexs):
    h = hexs.lstrip("#"); r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    f = lambda c: c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)


def contrast(a, b):
    la, lb = lum(a), lum(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


pages = {p: req("GET", p)[2] for p in ("/", "/nodes", "/messages", "/log", "/channels", "/radio", "/activity", "/connections", "/settings", "/about")}
css = W.CSS

# AC1 tokens, contrast, dark theme
tokens_light = dict(re.findall(r"--([a-z0-9-]+):\s*(#[0-9A-Fa-f]{6})", css.split("[data-theme=dark]", 1)[0].split(":root{", 1)[1].split("}", 1)[0]))
check_true("AC1 the light token block defines the roles", {"surface", "surface-raised", "ink", "ink-muted", "accent", "ok", "warn", "bad", "live", "line-strong"} <= set(tokens_light))
dark_block = css.split("[data-theme=dark]", 1)[1].split("}", 1)[0] if "[data-theme=dark]" in css else ""
tokens_dark = dict(re.findall(r"--([a-z0-9-]+):\s*(#[0-9A-Fa-f]{6})", dark_block))
check_true("AC1 a dark theme redefines the same roles", {"surface", "surface-raised", "ink", "ink-muted", "ok", "warn", "bad"} <= set(tokens_dark))
for name, toks in (("light", tokens_light), ("dark", tokens_dark)):
    for fg, bg in (("ink", "surface"), ("ink", "surface-raised"), ("ink-muted", "surface-raised"), ("ok", "surface-raised"), ("warn", "surface-raised"), ("bad", "surface-raised")):
        if fg in toks and bg in toks:
            ratio = contrast(toks[fg], toks[bg])
            check_true(f"AC1 {name}: --{fg} on --{bg} passes 4.5:1 ({ratio:.2f})", ratio >= 4.5)
if "accent" in tokens_light and "live" in tokens_light:
    check_true("AC1 light: the live indicator on the header passes 4.5:1", contrast(tokens_light["live"], tokens_light["accent"]) >= 4.5)
body_css = css.split("[data-theme=dark]", 1)[1] if "[data-theme=dark]" in css else css
stray = [h for h in re.findall(r"#[0-9A-Fa-f]{6}\b", body_css) if h.upper() not in {v.upper() for v in list(tokens_light.values()) + list(tokens_dark.values())}]
check("AC1 no raw hex outside the token blocks", sorted(set(stray)), [])
check_true("AC1 form controls inherit the font", re.search(r"(input|select|textarea|button)[^{]*\{[^}]*font:\s*inherit", css) is not None)
check_true("AC1 body text is 14 px (the 0.2.9 density pass)", re.search(r"body\{[^}]*14px", css) is not None)
check_true("AC1 the header carries the theme toggle", "data-theme-toggle" in pages["/"])

# AC2 no reload
for p, html in pages.items():
    check_true(f"AC2 {p} has no location.reload", "location.reload(" not in html)

# AC3 the state strip everywhere, counts with denominators
for p, html in pages.items():
    check_true(f"AC3 {p} carries the state strip", "class='state" in html or 'class="state' in html)
m = re.search(r"(\d+) heard here", pages["/"])
check_true("AC3 the strip names the heard-here count", m is not None)
check_true("AC3 the strip names the database count", re.search(r"(\d+) in the radio", pages["/"]) is not None)
heard_strip = int(m.group(1)) if m else -1
heard_nodes = len([n for n in NODES if n.get("heard_here", True)])
check("AC3 the strip's heard-here count equals the Nodes page's", heard_strip, heard_nodes)

# AC4 nav and targets
check_true("AC4 four primary items and More", all(x in pages["/"] for x in ("href='/'", "href='/messages'", "href='/channels'", "href='/radio'")) and "More" in pages["/"])
check_true("AC4 the tap token is 32px and buttons use it (the 0.2.9 density pass)", "--tap:32px" in css.replace(" ", "") and re.search(r"button\{[^}]*min-height:\s*var\(--tap\)", css) is not None)
check_true("AC4 nav links use the tap height", re.search(r"nav a\{[^}]*min-height:\s*var\(--tap\)", css) is not None)

# AC5 nodes
nodes = pages["/nodes"]
check_true("AC5 rows carry data-id", "data-id='!aa000001'" in nodes)
check_true("AC5 a signal glyph with bars and the figure", "class='sig" in nodes and "12.5" in nodes and "sig__bars" in nodes)
check_true("AC5 a hop pill", "direct" in nodes and re.search(r"\d hops?<", nodes) is not None)
check_true("AC5 low battery flagged", "batt--low" in nodes)
check_true("AC5 a null battery shows no reading", "no reading" in nodes and ">0<" not in nodes.split("no reading")[0][-200:])
check_true("AC5 heard as a time element with the ISO stamp", re.search(r"<time[^>]*datetime='2026-09-03T02:00:00Z'", nodes) is not None)
check_true("AC5 a result line per row", nodes.count("class='res") >= 2)
check_true("AC5 database-only nodes in a collapsed group with a pill", "<details" in nodes and "database only" in nodes)

# AC6 messages confirms
msgs = pages["/messages"]
check_true("AC6 channel confirm names the channel and the count; direct confirm names the node", "data-confirm-channel=" in msgs and "data-confirm-direct=" in msgs and "MILUX-TAK" in msgs and "heard" in msgs)
check_true("AC6 the channel select lists only enabled slots", msgs.count("<option value='") - msgs.count("value='^all'") - len(NODES) <= 2)

# AC7 log
log = pages["/log"]
check_true("AC7 log defaults to the bridge's lines with a toggle for the radio's", "data-log-filter" in log and "??:??:??" not in log.split("<pre", 1)[-1].split("</pre>", 1)[0])

# AC8 QR behind a control; read time and Read again
ch = pages["/channels"]
check_true("AC8 the QR is behind a control that names channel and region", "Show the join QR" in ch and "/channels/qr.png" in ch and "MILUX-TAK" in ch and "EU_868" in ch)
for p in ("/channels", "/radio"):
    check_true(f"AC8 {p} says when it was read and offers Read again", "read from the radio at" in pages[p] and "Read again" in pages[p])

# AC9 autonomy change needs a confirm token; act minting confirms; proposals as forms
from mesh_manager import connections as K
c = K.mint(etc, "agent-two", "observe")
st, _, _ = req("POST", "/connections/autonomy", body=f"id={c['id']}&autonomy=act")
check("AC9 an autonomy change without the confirm token is refused", st, 400)
st, _, _ = req("POST", "/connections/autonomy", body=f"id={c['id']}&autonomy=act&confirm=agent-two")
check("AC9 ...with the token it is accepted", st, 302)
st, _, _ = req("POST", "/connections", body="name=agent-three&autonomy=act")
check("AC9 minting act without the confirm token is refused", st, 400)
K.propose(etc, "agent-two", "send_text", {"text": "hello", "channel": 0}, "why")
act = req("GET", "/activity")[2]
check_true("AC9 a proposal renders as the catalogue form with the agent's values", "data-action='send_text'" in act and "value='hello'" in act)

# AC10 timeouts and the two sentences
check_true("AC10 the write timeout exceeds the read-back window", getattr(W, "WRITE_TIMEOUT_S", 0) > 30)
st, _, body = req("POST", "/api/channel_create", body=json.dumps({"name": "X"}), ctype="application/json")
check_true("AC10 a bridge that answers unknown op is not called 'could not ask the radio'", "could not ask the radio" not in body)
srv2 = W.make_server(bind="127.0.0.1", port=0, socket_path="/nonexistent/sock", etc_dir=tempfile.mkdtemp(), config={"AUTH": "off"})
p2 = srv2.server_address[1]; threading.Thread(target=srv2.serve_forever, daemon=True).start(); time.sleep(0.2)
c2 = http.client.HTTPConnection("127.0.0.1", p2, timeout=10); c2.request("POST", "/api/send_text", body=json.dumps({"text": "x"}), headers={"Content-Type": "application/json"}); r2 = c2.getresponse(); b2 = r2.read().decode(); c2.close()
check_true("AC10 an unreachable bridge answers 'could not ask the radio'", "could not ask the radio" in b2)
srv2.shutdown(); srv.shutdown()
# 4 Sep 2026: with the More menu open over the map, the tiles painted over its lower half.
# Leaflet's controls sit at z-index 800; anything of ours that floats must sit above that.
import re as _re
_css = W.CSS
for _sel, _label in (("details.more nav{", "the More menu"), (".tip{", "the tooltip"), ("header{", "the header")):
    _blk = _css[_css.index(_sel):_css.index("}", _css.index(_sel))]
    _z = _re.search(r"z-index:(\d+)", _blk)
    check_true(f"{_label} floats above every Leaflet layer (z > 1000)", _z is not None and int(_z.group(1)) > 1000)

finish()
