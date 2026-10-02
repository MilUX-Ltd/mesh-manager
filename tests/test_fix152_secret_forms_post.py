#!/usr/bin/env python3
"""1.5.2: a secret typed into a form never goes into a URL, whether or not the page's script runs.

1.5.1 made the site forms post and refused a GET carrying invite=. The same fault was left in every other form
that holds a secret: a form with no method is a GET when JavaScript does not run, so what is typed in it goes
into the address bar, the browser's history and any proxy's log.

  AC1  every rendered form with a password-type input or a field named url, key, token, password, secret or
       invite has method=post and an action.
  AC2  a GET whose query carries any of those field names answers 405, changes nothing and does not echo the value.

The crawl reaches every page the menu reaches; a floor on the number of secret-bearing forms found stops a crawl
that found nothing from passing (LESSONS 48). The proposal form is built from a proposal, so it is rendered from
one. Every block is guarded: a missing helper is one verdict, not a traceback hiding the rest.
"""
import html as html_lib, http.client, os, re, sys, tempfile, threading, time, urllib.parse
sys.path.insert(0, os.path.dirname(__file__))
from _common import ROOT, check, check_true, finish  # noqa: E402
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "tests"))
import fakegw_lib  # noqa: E402
fakegw_lib.install()
import fakebridge_lib  # noqa: E402
from fakebridge_lib import start_fake_bridge  # noqa: E402
from mesh_manager import catalogue as C, web as W  # noqa: E402

SECRET_NAMES = ("url", "key", "token", "password", "secret", "invite")
# made-up values on the documentation host: nothing here is a real key or password (LESSONS 42)
PROBE = "PROBE-NOT-A-REAL-SECRET-7f3a"

fakebridge_lib.STATUS.update({"mode": "server", "tak": "off", "peer_port": 8094, "site": {"id": "ab" * 32, "name": "Field box"}})
fb = start_fake_bridge()
srv = W.make_server(bind="127.0.0.1", port=0, socket_path=fb.path, etc_dir=tempfile.mkdtemp(), config={"AUTH": "off"}, state_dir=tempfile.mkdtemp())
threading.Thread(target=srv.serve_forever, daemon=True).start()
time.sleep(0.3)
PORT = srv.server_address[1]
_orig_ask = srv.web.client.ask
ASKED = []


def _ask(op, *a, **k):
    ASKED.append(op)
    if op == "peers":
        return {"site": {"id": "ab" * 32, "short": "abababababab", "name": "Field box", "address": "dev.example.invalid", "listening": True, "port": 8094},
                "peers": [], "invites": [], "pictures": []}
    return _orig_ask(op, *a, **k)


srv.web.client.ask = _ask


def send(method, path, body=None, ctype=None):
    c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=10)
    c.request(method, path, body=body, headers={"Content-Type": ctype} if ctype else {})
    r = c.getresponse()
    out = (r.status, r.read().decode(errors="replace"), r.getheader("Location") or "")
    c.close()
    return out


# ---- the crawl: every page the menu reaches, and the ones a menu does not link --------------------------------
SEEDS = ["/", "/channels", "/devices/add", "/connections", "/settings", "/map", "/computer/where", "/computer/updates", "/messages/quick",
         "/connect/mqtt", "/connect/tak", "/connect/agents", "/register", "/bench", "/activity", "/health", "/radio", "/node?id=!aa000001"]
SKIP = ("/static/", "/api/", "/events", "/fragment/", "/channels/qr.png")
pages, queue = {}, list(SEEDS)
while queue and len(pages) < 80:
    p = queue.pop(0)
    if p in pages or any(p.startswith(s) for s in SKIP):
        continue
    code, body, loc = send("GET", p)
    if code in (301, 302, 303, 307, 308) and loc.startswith("/"):
        queue.append(loc)
        continue
    if code != 200:
        continue
    pages[p] = body
    for h in re.findall(r"""href=['"](/[^'"#]*)""", body):
        h = html_lib.unescape(h)
        if h not in pages and h.split("?")[0] not in ("/logout",):
            queue.append(h)
check_true("setup: the crawl reached the menu's pages", len(pages) >= 12, f"{len(pages)} pages")


def attr(tag, name):
    m = re.search(r"(?<![\w-])" + name + r"=(?:'([^']*)'|\"([^\"]*)\")", tag)
    return html_lib.unescape(m.group(1) or m.group(2)) if m else None


FORM = re.compile(r"(<form\b[^>]*>)(.*?)</form>", re.S | re.I)


def holds_secret(inner):
    for m in re.finditer(r"<(?:input|textarea)\b[^>]*>", inner, re.I):
        t = m.group(0)
        if (attr(t, "type") or "").lower() == "password" or (attr(t, "name") or "").lower() in SECRET_NAMES:
            return True
    return False


def secret_forms():
    out = []
    for path, page in pages.items():
        for m in FORM.finditer(page):
            if holds_secret(m.group(2)):
                out.append((path, m.group(1)))
    # the proposal form is built from a proposal, so build one with a secret in it
    sec_action = next((a["id"] for a in C.ACTIONS if C.secret_inputs(a["id"]) and a.get("risk") != "read"), None) if hasattr(C, "ACTIONS") else None
    if sec_action:
        names = C.secret_inputs(sec_action)
        pr = {"id": "p1", "action": sec_action, "who": "an agent", "created": "2026-10-01T09:00:00Z", "rationale": "probe",
              "arguments": {n: PROBE for n in names}}
        html = W.proposal_form(pr)
        for m in FORM.finditer(html):
            out.append(("(proposal form for " + sec_action + ")", m.group(1)))
            if not holds_secret(m.group(2)):
                out.append(("(proposal form, no secret field found)", "<form>"))
    return out


forms = secret_forms()
check_true("AC1 setup: the crawl found the forms that hold a secret", len(forms) >= 5, f"{len(forms)} found")
check_true("AC1 setup: the Devices and channels page carries one", any(p == "/channels" for p, _ in forms), str(sorted({p for p, _ in forms})))

bad = []
for path, tag in forms:
    if (attr(tag, "method") or "").lower() != "post" or not attr(tag, "action"):
        bad.append((path, (attr(tag, "data-action") or tag[:60]), attr(tag, "method"), attr(tag, "action")))
check("AC1 every form with a secret input has method=post and an action", bad, [])

# JavaScript off: the browser submits the form as the form says; the secret must not be in what it asks for
for path, tag in forms:
    act, meth = attr(tag, "action") or "", (attr(tag, "method") or "get").upper()
    if meth != "POST" or not act:
        continue
    n1 = len(ASKED)
    code, _, _ = send("POST", act, urllib.parse.urlencode({"password": PROBE, "url": PROBE}), "application/x-www-form-urlencoded")
    if act == "/api/proposal/run":
        # a proposal runs only by its id, which a form posted without its script does not carry: it finds none and runs nothing
        check_true(f"AC1 JS off: a proposal form posts, finds no proposal and runs nothing", code == 404 and ASKED[n1:] == [], f"POST {act} -> {code}, asked {ASKED[n1:]}")
    else:
        check_true(f"AC1 JS off: {attr(tag, 'data-action') or path} posts to a route that takes a POST", code not in (404, 405), f"POST {act} -> {code}")
    check_true(f"AC1 JS off: and its address carries no secret", PROBE not in act and "?" not in act, act)

# ---- AC2 a GET carrying a secret field name is refused ------------------------------------------------------
n0 = len(ASKED)
for name in SECRET_NAMES + ("Password", "TOKEN"):
    for path in ("/", "/channels", "/connections", "/settings", "/api/status", "/channels/qr.png", "/healthz"):
        code, body, _ = send("GET", f"{path}{'&' if '?' in path else '?'}{name}={PROBE}")
        check_true(f"AC2 GET {path}?{name}= answers 405", code == 405, f"status {code}")
        check_true(f"AC2 and does not echo the value", PROBE not in body)
code, body, _ = send("GET", f"/connections?hours=24&label=x&{SECRET_NAMES[3]}={PROBE}&other=1")
check("AC2 refused with other fields beside it", code, 405)
check("AC2 the screen asked the bridge for nothing on a refused GET", [op for op in ASKED[n0:] if op not in ("status", "peers")], [])
for plain in ("/", "/channels", "/connections", "/node?id=!aa000001&hours=24", "/health?group=x"):
    check(f"AC2 an ordinary GET {plain} still answers 200", send("GET", plain)[0], 200)
check("AC2 a POST carrying the same field still reaches its handler", send("POST", "/api/channel_decode", f"url={PROBE}", "application/x-www-form-urlencoded")[0] != 405, True)

finish()
