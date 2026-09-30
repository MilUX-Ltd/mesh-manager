#!/usr/bin/env python3
"""Spec 111: Connect, joining sites as a journey.

Today the peers sit on /connections beside the agents, a laptop is offered an Invite it can never
use, the refusal for a bad code is one sentence for three faults (bridge.py:897), the code is spent
before the certificate is checked (peers.py:256 before 263), a used code stays in peers.json until
it expires, the listener records nothing about a refused attempt (peers.py:276-281), and a second
Join press with the same invite deletes the joiner's record of a site it has just joined
(bridge.py:1333-1336).

Two real bridges in one process on the fake gateway for the bridge half (as test_slice52 does), and
the screen on the fake bridge for the Connect page. Every block is guarded: a missing helper or a
changed signature is one FAIL verdict naming it, never a traceback that hides the rest.

Out of scope and not tested here (1.5.1): an invite submitted by GET without JavaScript.
The ways out (Save as a file, the share menu, Show full screen) and in (Open a file, scan) are
checked by hand at the live gate; the ACs here do not depend on them.
"""
import base64, hashlib, html as html_lib, http.client, json, os, re, socket, ssl, subprocess, sys, tempfile, threading, time
sys.path.insert(0, os.path.dirname(__file__))
from _common import ROOT, check, check_true, finish, read  # noqa: E402
sys.path.insert(0, os.path.join(ROOT, "src"))
import fakegw_lib  # noqa: E402
fakegw_lib.install()
import fakebridge_lib  # noqa: E402
from fakebridge_lib import start_fake_bridge  # noqa: E402
from mesh_manager import bridge as B, peers as P, web as W  # noqa: E402

FAKE_SERIAL = "/dev/serial/by-id/usb-fake-test-radio-if00"


def guard(label, fn):
    """Run one block; an exception is one FAIL naming it, and the suite carries on."""
    try:
        fn()
    except Exception as ex:  # noqa: BLE001
        check_true(label, False, f"{type(ex).__name__}: {str(ex)[:160]}")


def wait_for(pred, secs=6.0):
    t0 = time.time()
    while time.time() - t0 < secs:
        try:
            if pred():
                return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.2)
    return False


def site(name):
    d = tempfile.mkdtemp()
    return B.Bridge({"SERIAL": FAKE_SERIAL, "MODE": "server", "SITE_NAME": name}, socket_path=os.path.join(d, "b.sock"), state_dir=d), d


hub_state = tempfile.mkdtemp()
hub = B.Bridge({"SERIAL": FAKE_SERIAL, "MODE": "hub", "PEER_BIND": "127.0.0.1", "PEER_PORT": 0, "SITE_NAME": "Hub",
                "SITE_ADDRESS": "127.0.0.1"}, socket_path=os.path.join(hub_state, "b.sock"), state_dir=hub_state)
HUB_ID = hub.op_status()["site"]["id"]
HUB_PORT = hub.op_status()["peer_port"]
PEERS_JSON = os.path.join(hub_state, "peers.json")


def peers_file():
    try:
        return open(PEERS_JSON).read()
    except OSError:
        return ""


def invite_row(inv_id):
    return next((r for r in (hub.op_peers().get("invites") or []) if r.get("id") == inv_id), None)


def raw_attempt(code, claimed, key_path, cert_pem):
    """A dialler that holds a real key and certificate but claims to be another site: the listener must
    refuse it on the certificate. Returns the listener's answer frame."""
    raw = socket.create_connection(("127.0.0.1", HUB_PORT), timeout=10)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
    s = ctx.wrap_socket(raw, server_hostname="mesh-manager"); f = s.makefile("rb")
    s.sendall((json.dumps({"hello": 1, "site": claimed, "name": "Stranger", "cert": cert_pem}) + "\n").encode())
    ch = json.loads(f.readline().decode() or "{}")
    sig = P.sign(key_path, base64.b64decode(ch["challenge"]))
    s.sendall((json.dumps({"auth": sig, "code": code}) + "\n").encode())
    ans = json.loads(f.readline().decode() or "{}")
    s.close()
    return ans


HAVE_WORDS = callable(getattr(P, "check_words", None)) and callable(getattr(P, "bip39_words", None))
HAVE_READ = callable(getattr(B.Bridge, "op_peer_invite_read", None))
HAVE_CANCEL = callable(getattr(B.Bridge, "op_peer_invite_cancel", None))


# ---- AC2 an invite has an id, a label, a listing and a cancel ----------------------------------------------
def ac2():
    inv = hub.op_peer_invite(label="Brize laptop")
    check_true("AC2 an invite answers an id of its own", bool(inv.get("id")), json.dumps({k: v for k, v in inv.items() if k != "qr_svg"})[:200])
    check_true("AC2 and the id is not the code, so a listing can name it without the code",
               bool(inv.get("id")) and inv.get("id") != inv.get("code") and str(inv.get("code") or "") not in str(inv.get("id")), repr((inv.get("id"), inv.get("code"))))
    check("AC2 the answer carries the label it was given", inv.get("label"), "Brize laptop")
    row = invite_row(inv.get("id")) if inv.get("id") else None
    check_true("AC2 the site's listing holds the invite by its id", row is not None, json.dumps(hub.op_peers().get("invites"))[:200])
    check("AC2 and names it by its label", (row or {}).get("label"), "Brize laptop")
    check_true("AC2 and says when it runs out", bool((row or {}).get("expires")), json.dumps(row)[:160])
    check_true("AC2 and the listing does not carry the code", str(inv.get("code") or "ZZZZ") not in json.dumps(hub.op_peers()), "the code is in op_peers")
    if not HAVE_CANCEL:
        check_true("AC2 an invite can be cancelled", False, "no op_peer_invite_cancel on the bridge yet")
        return
    inv2 = hub.op_peer_invite(label="Cancel me")
    r = hub.op_peer_invite_cancel(id=inv2.get("id"))
    check_true("AC2 an invite can be cancelled by its id", "error" not in r, json.dumps(r)[:160])
    check("AC2 a cancelled invite leaves the listing", invite_row(inv2.get("id")), None)
    check_true("AC2 and its code leaves peers.json", str(inv2.get("code")) not in peers_file(), "the cancelled code is still on disk")
    j, _ = site("Late")
    r = j.op_peer_join(invite=inv2["invite"])
    check_true("AC2 and a join with a cancelled invite is refused", "error" in r, json.dumps(r)[:160])


guard("AC2 invite id, label, listing and cancel", ac2)


# ---- AC3 three check words, the same at both ends -----------------------------------------------------------
def ac3():
    if not HAVE_WORDS:
        check_true("AC3 check words", False, "no peers.check_words / peers.bip39_words yet")
        return
    wl = list(P.bip39_words())
    check("AC3 the bundled list is the BIP-39 English list: 2048 distinct words", (len(wl), len(set(wl))), (2048, 2048))
    check("AC3 and it is that list, not another of the same length", (wl[:1], wl[-1:]), (["abandon"], ["zoo"]))
    w = list(P.check_words(HUB_ID))
    check("AC3 three words", len(w), 3)
    check_true("AC3 each from the bundled list", all(x in wl for x in w), repr(w))
    check("AC3 the same fingerprint gives the same words", list(P.check_words(HUB_ID)), w)
    other = hashlib.sha256(b"another site").hexdigest()
    check_true("AC3 another fingerprint gives other words", list(P.check_words(other)) != w, repr((w, P.check_words(other))))
    dig = hashlib.sha256(b"mesh-manager check words v1\n" + HUB_ID.encode("ascii")).digest()
    n = int.from_bytes(dig[:5], "big") >> 7
    want = [wl[(n >> 22) & 2047], wl[(n >> 11) & 2047], wl[n & 2047]]
    check("AC3 derived as the spec fixes it, so every build computes the same words", w, want)
    code = ("import sys; sys.path.insert(0, %r); from mesh_manager import peers as P; print(' '.join(P.check_words(%r)))"
            % (os.path.join(ROOT, "src"), HUB_ID))
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=dict(os.environ, PYTHONHASHSEED="12345"), timeout=60)
    check("AC3 another process computes the same words", out.stdout.split(), w)
    inv = hub.op_peer_invite(label="Words")
    check("AC3 the invite answer carries the listener's words", inv.get("words"), w)
    if not HAVE_READ:
        check_true("AC3 the joiner computes the same words from the invite", False, "no op_peer_invite_read on the bridge yet")
        return
    j, _ = site("Words joiner")
    check("AC3 the joiner computes the same words from the invite it holds", (j.op_peer_invite_read(invite=inv["invite"]) or {}).get("words"), w)
    check_true("AC3 NOTICE records the BIP-39 wordlist and its licence", re.search(r"BIP-?39", read("NOTICE") or "") is not None, "no BIP-39 line in NOTICE")


guard("AC3 check words", ac3)


# ---- AC4, AC5 refusals said apart; the code survives a certificate failure; the listener records it --------
FAULTS = {}


def ac4_ac5():
    stranger, sdir = site("Stranger")
    inv = hub.op_peer_invite(label="Edge")
    key_p, crt_p = os.path.join(sdir, "site.key"), os.path.join(sdir, "site.crt")
    ans = raw_attempt(inv["code"], "0" * 64, key_p, open(crt_p).read())
    check_true("AC4 a dialler whose certificate does not match the site it claims is refused",
               "error" in ans and re.search(r"certificate", str(ans.get("error")), re.I) is not None, repr(ans)[:200])
    # AC5: the listener keeps the last attempt and its fault against the invite, in the words the dialler saw
    row = None
    if inv.get("id"):
        wait_for(lambda: (invite_row(inv["id"]) or {}).get("last_attempt"), 3)
        row = invite_row(inv["id"])
    la = (row or {}).get("last_attempt") or {}
    check("AC5 the listener records the fault against the invite, in the words the dialler saw", la.get("fault"), ans.get("error"))
    check_true("AC5 and when it happened", bool(la.get("at")), json.dumps(row)[:200])
    edge, _ = site("Edge")
    j = edge.op_peer_join(invite=inv["invite"])
    check_true("AC4 the code was not spent by the certificate failure: the right site still joins with it", j.get("joined") is True, repr(j)[:200])
    check_true("AC4 after a use the code is no longer in peers.json", inv["code"] not in peers_file(), "the used code is still on disk")
    r_used = stranger.op_peer_join(invite=inv["invite"])
    FAULTS["used"] = str(r_used.get("error") or "")
    # expired: a code made to last one second
    ttl = P.INVITE_TTL
    P.INVITE_TTL = 1
    try:
        inv_e = hub.op_peer_invite(label="Too late")
    finally:
        P.INVITE_TTL = ttl
    time.sleep(2.2)
    r_exp = stranger.op_peer_join(invite=inv_e["invite"])
    FAULTS["expired"] = str(r_exp.get("error") or "")
    check_true("AC4 an expired code is not left in peers.json", inv_e["code"] not in peers_file(), "the expired code is still on disk")
    host_port = f"127.0.0.1:{HUB_PORT}"
    r_wrong = stranger.op_peer_join(invite=f"{host_port}/ZZZZ2222/{HUB_ID}")
    FAULTS["wrong"] = str(r_wrong.get("error") or "")
    for k, others in (("expired", ("used", "wrong")), ("used", ("expired", "wrong")), ("wrong", ("expired", "used"))):
        msg = FAULTS[k].lower()
        check_true(f"AC4 the {k} refusal says {k}, and not the other two",
                   k in msg and not any(o in msg for o in others), repr(FAULTS[k]))
    check("AC4 the three refusals are three different sentences", len({FAULTS["expired"], FAULTS["used"], FAULTS["wrong"]}), 3)
    check_true("AC4 nothing was pinned for the stranger", all(p.get("name") != "Stranger" for p in hub.op_peers().get("peers", [])), repr([p.get("name") for p in hub.op_peers().get("peers", [])]))


guard("AC4 and AC5 refusals and the listener's record", ac4_ac5)


# ---- AC6 read before dial, sharing chosen first, submit once ---------------------------------------------------
def ac6():
    inv = hub.op_peer_invite(label="Read me")
    joiner, _ = site("Reader")
    jid = joiner.op_status()["site"]["id"]
    if not HAVE_READ:
        check_true("AC6 the invite is read and shown before anything dials", False, "no op_peer_invite_read on the bridge yet")
    else:
        r = joiner.op_peer_invite_read(invite=inv["invite"])
        check("AC6 reading an invite names the site's fingerprint", r.get("fingerprint"), HUB_ID)
        check("AC6 and where it will dial", r.get("address"), f"127.0.0.1:{HUB_PORT}")
        check_true("AC6 and the time left", bool(r.get("expires")), json.dumps(r)[:200])
        check_true("AC6 and nothing dialled: the listener saw no attempt", not ((invite_row(inv.get("id")) or {}).get("last_attempt")) and jid not in hub.peering.connected(), json.dumps(invite_row(inv.get("id")))[:160])
        check_true("AC6 and the joiner holds no record of the site yet", all(p.get("id") != HUB_ID for p in joiner.op_peers().get("peers", [])), "a read wrote a record")
        bad = joiner.op_peer_invite_read(invite="not an invite")
        check_true("AC6 a text that is not an invite is refused in words", "error" in bad, json.dumps(bad)[:160])
    # sharing chosen before Join: nodes out off, so not even the first picture crosses
    j = joiner.op_peer_join(invite=inv["invite"], sharing={"nodes": {"out": False, "in": True}})
    check_true("AC6 the join with a sharing table succeeds", j.get("joined") is True, repr(j)[:200])
    row = next((p for p in joiner.op_peers().get("peers", []) if p.get("id") == HUB_ID), {})
    check("AC6 the sharing chosen before the join is the site's table", ((row.get("sharing") or {}).get("nodes") or {}).get("out"), False)
    crossed = wait_for(lambda: any(n.get("origin") == jid for n in hub.op_nodes().get("nodes", []) if n.get("remote")), 5)
    check("AC6 and it held from the first picture: no node crossed", crossed, False)
    # submit once, in sequence: the same invite pressed again after it worked
    wait_for(lambda: jid in hub.peering.connected(), 5)
    again = joiner.op_peer_join(invite=inv["invite"])
    check_true("AC6 a second Join with the same invite is answered, not a crash", isinstance(again, dict), repr(again)[:160])
    check_true("AC6 and the joiner still holds its record of the site", any(p.get("id") == HUB_ID for p in joiner.op_peers().get("peers", [])),
               repr([p.get("id", "")[:8] for p in joiner.op_peers().get("peers", [])]))
    check_true("AC6 and the link is still up", wait_for(lambda: jid in hub.peering.connected() and HUB_ID in joiner.peering.connected(), 5),
               "the second press took the link down")
    # submit once, while the first press is still in flight. The listener is slowed between spending the
    # code and pinning the joiner, which is the window bridge.py:1333-1336 falls into: today the second press
    # is refused as a used code and its failure path deletes the record the first press made.
    inv_k = hub.op_peer_invite(label="Double press")
    twin, _ = site("Twin")
    tid = twin.op_status()["site"]["id"]
    res, took = [], []
    hub.peer_pin = lambda *a, **k: (time.sleep(1.0), B.Bridge.peer_pin(hub, *a, **k))[1]
    try:
        def press():
            t0 = time.time()
            res.append(twin.op_peer_join(invite=inv_k["invite"], sharing={"nodes": {"out": False, "in": True}}))
            took.append(time.time() - t0)
        first = threading.Thread(target=press); first.start()
        time.sleep(0.3)
        second = threading.Thread(target=press); second.start()
        first.join(30); second.join(30)
    finally:
        del hub.peer_pin
    check("AC6 two presses, the second while the first is in flight, join once", sum(1 for r in res if r.get("joined") is True), 1)
    other = next((r for r in res if r.get("joined") is not True), {})
    check_true("AC6 and the second press is answered at once, in words saying a join is already under way",
               len(took) == 2 and max(took) < 6 and re.search(r"already|under way|in progress", str(other.get("error") or ""), re.I) is not None
               and not re.search(r"code|no answer", str(other.get("error") or ""), re.I), repr((other, [round(t, 1) for t in took]))[:220])
    rec = twin.peer_pinned(HUB_ID) or {}
    check_true("AC6 and the joiner keeps its record of the site, address included, so it redials after a restart",
               bool(rec.get("address")), json.dumps({k: v for k, v in rec.items() if k != "cert"})[:200])
    check("AC6 and keeps the sharing chosen before the first press", ((rec.get("sharing") or {}).get("nodes") or {}).get("out"), False)
    check_true("AC6 and the link comes up", wait_for(lambda: tid in hub.peering.connected(), 6), "no link after a double press")


guard("AC6 read before dial, sharing first, submit once", ac6)


# ---- AC1, AC5 the screen: the shape decides the direction; the fault is shown --------------------------------
FAULT_WORDS = "the certificate did not match the site it claimed to be (suite 111)"
FUTURE = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 480))
NOW = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def peers_reply(listening, mode):
    return {"site": {"id": "ab" * 32, "short": "abababababab", "name": "Field box" if mode != "hub" else "Dev hub", "address": "dev.example" if listening else None,
                     "listening": listening, "port": 8094 if listening else None},
            "peers": [{"id": "cd" * 32, "name": "Edge laptop", "state": "connected", "direction": "in", "since": NOW, "last_seen": NOW, "added": NOW, "nodes": 1,
                       "sharing": {"nodes": {"out": True, "in": True}}, "aired": {"count": 0, "last": None}, "note": None}],
            "invites": ([{"id": "k7m2q9", "label": "Brize laptop", "expires": FUTURE, "last_attempt": {"at": NOW, "fault": FAULT_WORDS}}] if listening else []),
            "pictures": []}


fb = start_fake_bridge()
srv = W.make_server(bind="127.0.0.1", port=0, socket_path=fb.path, etc_dir=tempfile.mkdtemp(), config={"AUTH": "off"}, state_dir=tempfile.mkdtemp())
threading.Thread(target=srv.serve_forever, daemon=True).start()
time.sleep(0.3)
SHAPE = {"listening": True, "mode": "server"}
_orig_ask = srv.web.client.ask


def _ask(op, *a, **k):
    if op == "peers":
        return peers_reply(SHAPE["listening"], SHAPE["mode"])
    return _orig_ask(op, *a, **k)


srv.web.client.ask = _ask


def get(path, hops=2):
    """GET, following a redirect or two on the same screen (Spec 107: a page may redirect to its tab)."""
    c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
    c.request("GET", path); r = c.getresponse(); b = r.read().decode(errors="replace"); loc = r.getheader("Location") or ""; c.close()
    if r.status in (301, 302, 303, 307, 308) and hops and loc.startswith("/"):
        return get(loc, hops - 1)
    return r.status, b


def link_to(page, name):
    """The href of the first link whose visible text is `name` (a count badge after it is ignored)."""
    for m in re.finditer(r"<a\b([^>]*)>(.*?)</a>", page, re.S | re.I):
        if re.sub(r"\s*\d+$", "", visible(m.group(2)).strip()) == name:
            h = re.search(r"href=['\"]([^'\"]*)['\"]", m.group(1))
            if h and h.group(1).startswith("/"):
                return html_lib.unescape(h.group(1)).split("#")[0]
    return None


def visible(html):
    html = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", html)
    return re.sub(r"\s+", " ", re.sub(r"(?s)<[^>]+>", " ", html))


def connect_as(mode, listening):
    fakebridge_lib.STATUS.update({"mode": mode, "tak": "off", "peer_port": 8094 if listening else None,
                                  "radio": None if mode == "hub" else fakebridge_lib.STATUS.get("radio") or FAKE_SERIAL,
                                  "site": {"id": "ab" * 32, "name": "Dev hub" if mode == "hub" else "Field box"}})
    SHAPE.update({"mode": mode, "listening": listening})
    # reached as an operator reaches it: Connect in the menu, then its Other Mesh Managers tab (Spec 107)
    s, home = get("/")
    href = link_to(home, "Connect")
    if not href:
        return 404, "no Connect item in the menu"
    s, sect = get(href)
    tab = link_to(sect, "Other Mesh Managers")
    return get(tab) if tab else (s if s != 200 else 404, sect if s != 200 else "no Other Mesh Managers tab under Connect")


def _why(status, text, page):
    if status != 200:
        return f"status {status}"
    m = re.search(r".{0,40}\bpeers?\b.{0,40}", text, re.I) or re.search(r".{0,40}peer.bind.{0,20}", page, re.I)
    return m.group(0) if m else ""


def ac1():
    s, lap = connect_as("desktop", False)
    check("AC1 Connect, from the menu, opens its Other Mesh Managers tab", s, 200)
    txt = visible(lap)
    check_true("AC1 with four tabs: Other Mesh Managers, MQTT, TAK, AI agents",
               all(t in txt for t in ("Other Mesh Managers", "MQTT", "TAK", "AI agents")), txt[:200])
    check_true("AC1 a laptop is offered Join", "data-action='peer_join'" in lap, "no join control on a laptop")
    check_true("AC1 a laptop has no Invite control at all", s == 200 and "peer_invite" not in lap, "an invite control is on a laptop page")
    check_true("AC1 and says why in one line: a laptop dials out; nothing can reach it",
               re.search(r"dials out", txt, re.I) is not None and re.search(r"nothing can reach it", txt, re.I) is not None, txt[:300])
    for name, (mode, listening) in (("laptop", ("desktop", False)), ("box not listening", ("server", False)), ("box listening", ("server", True)), ("hub", ("hub", True))):
        s, page = connect_as(mode, listening)
        t = visible(page)
        check_true(f"AC1 the {name} page names no installer option and says site, not peer",
                   s == 200 and "--peer-bind" not in page and "PEER_BIND" not in page and re.search(r"\bpeers?\b", t, re.I) is None,
                   _why(s, t, page))
    s, box_off = connect_as("server", False)
    check_true("AC1 a box that is not listening offers Join and no Invite",
               s == 200 and "data-action='peer_join'" in box_off and "peer_invite" not in box_off, f"status {s}")
    s, box_on = connect_as("server", True)
    check_true("AC1 a box that is listening offers Invite and Join",
               s == 200 and "data-action='peer_invite'" in box_on and "data-action='peer_join'" in box_on, f"status {s}")
    s, hubp = connect_as("hub", True)
    i_inv, i_join = hubp.find("data-action='peer_invite'"), hubp.find("data-action='peer_join'")
    check_true("AC1 a hub leads with Invite", s == 200 and i_inv >= 0 and (i_join < 0 or i_inv < i_join), f"invite at {i_inv}, join at {i_join}")
    ht = visible(hubp)
    check_true("AC1 and lists every site with its state, the pending invite by its label",
               "Edge laptop" in ht and "Brize laptop" in ht and re.search(r"connected", ht, re.I) is not None, ht[:300])
    # AC5 the fault the listener recorded is on the screen in the same words
    check_true("AC5 the listener's recorded fault is shown against the invite, in its own words", W.e(FAULT_WORDS) in hubp or FAULT_WORDS in ht,
               "the fault is not on the page")


guard("AC1 the Connect page", ac1)

finish()
