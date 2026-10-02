#!/usr/bin/env python3
"""1.5.1: a channel key or a site invite never goes where the security floor says
it must not: fetched before anyone asked to see it, or carried in a URL.

  AC1  with Channels loaded and nothing pressed, no request for /channels/qr.png has been made: the initial
       HTML carries no src for it. The src is set on Show, and cleared on close and on the 60 s timeout.
  AC2  a join URL is read in a POST body; GET /api/channel_decode with a url answers 405.
  AC3  the site join form renders with method=post; a GET carrying invite= changes nothing and answers 405.
       Checked as a browser with JavaScript off behaves: the form is submitted by its own method and action.

Written first, committed failing, then built to green (CLAUDE.md, Method). Every block is guarded: a missing
helper is one verdict, not a traceback hiding the rest.
"""
import html as html_lib, http.client, json, os, re, sys, tempfile, threading, time, urllib.parse
sys.path.insert(0, os.path.dirname(__file__))
from _common import ROOT, check, check_true, finish  # noqa: E402
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "tests"))
import fakegw_lib  # noqa: E402
fakegw_lib.install()
import fakebridge_lib  # noqa: E402
from fakebridge_lib import start_fake_bridge  # noqa: E402
from mesh_manager import web as W  # noqa: E402

NOW = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
FUTURE = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 600))
# the documentation host, a made-up code: nothing here is a real key (LESSONS 42)
JOIN_URL = "https://meshtastic.org/e/#CgMSAQEFAKEKEYONLY"
INVITE = "box.example.invalid:8094/k7m2q9zz/ab12cd34/20991231T000000Z"

fakebridge_lib.STATUS.update({"mode": "server", "tak": "off", "peer_port": 8094,
                              "site": {"id": "ab" * 32, "name": "Field box"}})
fb = start_fake_bridge()
srv = W.make_server(bind="127.0.0.1", port=0, socket_path=fb.path, etc_dir=tempfile.mkdtemp(),
                    config={"AUTH": "off"}, state_dir=tempfile.mkdtemp())
threading.Thread(target=srv.serve_forever, daemon=True).start()
time.sleep(0.3)
PORT = srv.server_address[1]
_orig_ask = srv.web.client.ask
ASKED = []   # every op the screen puts to the bridge, with its arguments


def _ask(op, *a, **k):
    ASKED.append((op, dict(k)))
    if op == "peers":
        return {"site": {"id": "ab" * 32, "short": "abababababab", "name": "Field box", "address": "dev.example.invalid",
                         "listening": True, "port": 8094},
                "peers": [], "invites": [], "pictures": []}
    return _orig_ask(op, *a, **k)


srv.web.client.ask = _ask


def send(method, path, body=None, ctype=None):
    c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=10)
    c.request(method, path, body=body, headers={"Content-Type": ctype} if ctype else {})
    r = c.getresponse()
    out = (r.status, r.read().decode(errors="replace"))
    c.close()
    return out


def ops_asked():
    return [op for op, _ in ASKED]


PAGES = {}
for _p in ("/channels", "/devices/add", "/connections"):
    PAGES[_p] = send("GET", _p)
check("setup: Channels, Add a device and the sites tab all render", sorted((p, s) for p, (s, _) in PAGES.items() if s == 200),
      sorted((p, 200) for p in PAGES))

# ---- AC1 no QR before Show ---------------------------------------------------------------------------------
for pth in ("/channels", "/devices/add"):
    page = PAGES[pth][1]
    srcs = re.findall(r"<img\b[^>]*\bsrc=['\"][^'\"]*qr\.png[^'\"]*['\"]", page, re.I)
    check(f"AC1 {pth}: the initial HTML carries no src for /channels/qr.png", srcs, [])
    check_true(f"AC1 {pth}: the sheet is there, hidden, with an image to fill", bool(re.search(r"id='qr-sheet'[^>]*\bhidden\b[^>]*><img\b", page)))
    check_true(f"AC1 {pth}: the Show button is there", "data-qr-open" in page)

chan = PAGES["/channels"][1]
script = "\n".join(re.findall(r"(?is)<script\b[^>]*>(.*?)</script>", chan))
opens = [m.start() for m in re.finditer(r"/channels/qr\.png", script)]
click = re.search(r"querySelectorAll\('\[data-qr-open\]'\).*?(?=\n\s*document\.querySelectorAll\('\[data-qr-close\]'\))", script, re.S)
check_true("AC1 the script names the QR address only inside the Show handler",
           bool(click) and len(opens) == 1 and click.start() < opens[0] < click.end(), f"{len(opens)} mention(s)")
close = re.search(r"function qrClose\(\)\{(.*?)\n", script, re.S)
check_true("AC1 closing the sheet clears the image source", bool(close) and re.search(r"removeAttribute\(\s*['\"]src['\"]\s*\)|\.src\s*=\s*['\"]{2}", close.group(1)) is not None,
           close.group(1)[:90] if close else "no qrClose")
check_true("AC1 the 60 s timeout goes through that same close", bool(re.search(r"left--;.*?if\(left<=0\)\{qrClose\(\);\}", script, re.S)))
check_true("AC1 Escape and the Close button go through it too", script.count("qrClose") >= 4, f"{script.count('qrClose')} uses")

# ---- AC2 a join URL is read in a body ----------------------------------------------------------------------
before = ops_asked().count("channel_decode")
code, body = send("GET", "/api/channel_decode?url=" + urllib.parse.quote(JOIN_URL))
check("AC2 GET /api/channel_decode with a url answers 405", code, 405)
check("AC2 and the bridge is never asked to read it", ops_asked().count("channel_decode"), before)
code, body = send("POST", "/api/channel_decode", json.dumps({"url": JOIN_URL}), "application/json")
check_true("AC2 the same URL in a POST body is accepted (not a 405)", code != 405, f"status {code}")
check("AC2 and reaches the bridge with the URL as its argument", [a.get("url") for op, a in ASKED if op == "channel_decode"][-1:], [JOIN_URL])
for pth, (s, page) in PAGES.items():
    check(f"AC2 {pth}: no script asks channel_decode with a query", re.findall(r"/api/channel_decode\?", page), [])
check_true("AC2 the Read it first button sends the URL as a POST body", "fetch('/api/channel_decode',{method:'POST'" in chan)
for aid in ("site_invite_read", "peer_join", "peer_invite"):
    code, _ = send("GET", f"/api/{aid}?invite=" + urllib.parse.quote(INVITE))
    check(f"AC2 GET /api/{aid} with an invite still answers 405", code, 405)

# ---- AC3 the site join form posts; a GET carrying invite= does nothing ---------------------------------------
conn = PAGES["/connections"][1]
forms = [m.group(0) for m in re.finditer(r"<form\b[^>]*>", conn)]
join = next((f for f in forms if "data-action='peer_join'" in f), "")
inv = next((f for f in forms if "data-action='peer_invite'" in f), "")
check_true("AC3 the sites tab has the Join a site form", bool(join))


def attr(tag, name):
    m = re.search(r"(?<![\w-])" + name + r"=(?:'([^']*)'|\"([^\"]*)\")", tag)
    return html_lib.unescape(m.group(1) or m.group(2)) if m else None


check("AC3 the join form renders with method=post", (attr(join, "method") or "").lower(), "post")
check("AC3 the invite form renders with method=post", (attr(inv, "method") or "").lower(), "post")
check_true("AC3 the join form names the handler it posts to", bool(attr(join, "action")), repr(attr(join, "action")))

# JavaScript off: the browser submits the form as the form says, so do that. The invite must travel in the body.
action = attr(join, "action") or "/connections"
method = (attr(join, "method") or "get").upper()
fields = urllib.parse.urlencode({"invite": INVITE})
if method == "GET":
    target = action + ("&" if "?" in action else "?") + fields
    sent = send("GET", target)
else:
    target = action
    sent = send("POST", action, fields, "application/x-www-form-urlencoded")
check_true("AC3 JS off: the invite is not in the URL the browser asks for", "invite" not in target and urllib.parse.quote(INVITE) not in target, target)
check_true("AC3 JS off: the form's own submission is not refused for its method", sent[0] != 405, f"{method} {target} -> {sent[0]}")

n_calls = len(ASKED)
code, page = send("GET", "/connections?invite=" + urllib.parse.quote(INVITE))
check("AC3 GET /connections carrying invite= answers 405", code, 405)
check_true("AC3 and the refusal does not echo the invite", INVITE not in page and "k7m2q9zz" not in page)
code2, _ = send("GET", "/connections?label=x&invite=" + urllib.parse.quote(INVITE) + "&sharing=nodes")
check("AC3 the same with other fields beside it", code2, 405)
check_true("AC3 a GET with invite= asked the bridge for nothing that joins or reads",
           not [op for op, _ in ASKED[n_calls:] if op in ("peer_join", "site_invite_read", "peer_invite_read")], str(ops_asked()[n_calls:]))
code3, _ = send("GET", "/connections")
check("AC3 the sites tab itself still answers 200 on a plain GET", code3, 200)

finish()
