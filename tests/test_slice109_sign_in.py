#!/usr/bin/env python3
"""Spec 109: sign in with a name, and the password from the screen.

Today the sign-in page asks for a password and nothing else, and every change made from the screen is
written into the audit under the one word "operator" (web.py, the POST routes). There is no way to sign
out, no way to change the password without the installer, and the installer takes the password on its
command line, where every process on the box can read it.

Decision D3 (1.5.0): the name typed at sign-in is attribution against the one operator
password, not an account. D4: the password is changed on This computer, a generated one must be changed
at first sign-in, and the installer takes the password from the environment or stdin, never argv.

Every block is guarded: a missing route or helper is one verdict, not a traceback hiding the rest.
"""
import base64, http.client, json, os, re, stat, subprocess, sys, tempfile, threading, time
sys.path.insert(0, os.path.dirname(__file__))
from _common import ROOT, check, check_true, finish, read  # noqa: E402
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "tests"))
try:
    from mesh_manager import web as W
    from mesh_manager import connections as K
    from mesh_manager import channel as CH
    import fakebridge_lib as FB
except Exception as ex:  # noqa: BLE001
    check_true("the screen and the fake bridge import", False, f"{type(ex).__name__}: {ex}")
    finish()

PW = "correct horse"


# ---- a fake bridge whose answers a check can set, forwarding everything else to the shared fake ------------
class Proxy(FB.FakeBridge):
    def __init__(self, over=None):
        self.inner = FB.FakeBridge()
        self.over = dict(over or {})
        super().__init__()

    def _one(self, c):
        f = c.makefile("rb")
        if not CH.said_hello(f, self._token):
            c.sendall(b'{"error": "no"}\n'); c.close(); return
        try:
            req = json.loads(f.readline().decode())
        except ValueError:
            c.sendall(b'{"error": "bad json"}\n'); c.close(); return
        op = req.pop("op", None)
        if op == "events":
            c.sendall(b'{"kind": "hello"}\n'); self.clients.append(c); return
        self.calls.append((op, dict(req)))
        if op in self.over:
            v = self.over[op]
            rep = v(req) if callable(v) else v
        else:
            try:
                rep = W.BridgeClient(self.inner.path).ask(op, **req)
            except W.BridgeDown as e_:
                rep = {"error": str(e_)}
        c.sendall((json.dumps(rep) + "\n").encode()); c.close()


def serve(config, bridge=None, passwd=True, generated=False, first=None):
    etc = tempfile.mkdtemp()
    if passwd:
        W.write_password(os.path.join(etc, "passwd"), PW)
    if generated:
        open(os.path.join(etc, "passwd.generated"), "w").write("generated at install\n")
    if first is not None:
        json.dump(first, open(os.path.join(etc, "first-run.json"), "w"))
    bridge = bridge or Proxy()
    srv = W.make_server("127.0.0.1", 0, bridge.path, etc, dict({"UPDATE_MODE": "off"}, **config), state_dir=tempfile.mkdtemp())
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.2)
    return srv.server_address[1], etc, bridge


def req(port, method, path, body=None, cookie=None, ctype="application/x-www-form-urlencoded"):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    h = {}
    if cookie:
        h["Cookie"] = cookie
    if body is not None:
        h["Content-Type"] = ctype
    c.request(method, path, body=body, headers=h)
    r = c.getresponse(); data = r.read(); hd = {k.lower(): v for k, v in r.getheaders()}
    c.close()
    return r.status, hd, data.decode("utf-8", "replace")


def form(**kw):
    import urllib.parse
    return urllib.parse.urlencode(kw)


def login(port, name, pw=PW):
    st, hd, body = req(port, "POST", "/login", form(name=name, password=pw))
    return st, hd, hd.get("set-cookie", "").split(";")[0], body


def audit(etc):
    return K.audit_tail(etc, 500)


def audit_text(etc):
    try:
        return open(os.path.join(etc, "audit.log")).read()
    except OSError:
        return ""


def guarded(label, fn):
    try:
        fn()
    except Exception as ex:  # noqa: BLE001
        check_true(f"{label}: ran without an exception", False, f"{type(ex).__name__}: {ex}")


# ---- AC1 the sign-in page names the computer, and asks for a name and the password --------------------------
def ac1():
    W.reset_throttle()
    hub = Proxy({"status": dict(FB.STATUS, mode="hub", radio=None, radio_present=False, peers=2,
                                site={"id": "ab" * 32, "name": "MILUX-HUB"})})
    port, _, _ = serve({"MODE": "hub", "SITE_ADDRESS": "mesh.example.org"}, bridge=hub)
    st, _, page = req(port, "GET", "/login")
    check("AC1 /login answers before sign-in", st, 200)
    check_true("AC1 it names the computer: the site name", "MILUX-HUB" in page, "no site name on the sign-in page")
    check_true("AC1 and the kind of computer: Hub", re.search(r"\bHub\b", page) is not None, "no 'Hub'")
    check_true("AC1 and the address it is reached at", "mesh.example.org" in page, "no address")
    check_true("AC1 it asks for a name", re.search(r"<input[^>]*name=['\"]name['\"]", page) is not None, "no name field")
    check_true("AC1 and the password", re.search(r"<input[^>]*type=['\"]password['\"][^>]*name=['\"]password['\"]|<input[^>]*name=['\"]password['\"][^>]*type=['\"]password['\"]", page) is not None, "no password field")
    check_true("AC1 nothing from the mesh or the links shows before sign-in",
               "Tracker9" not in page and "2 sites" not in page and "joined to" not in page.lower(), "mesh or site detail leaked")
    check_true("AC1 the page no longer sends people to the installer's argv",
               "install.sh --password" not in page, "still says install.sh --password")
    box = Proxy({"status": dict(FB.STATUS, mode="server", site={"id": "cd" * 32, "name": "EDGE"})})
    port2, _, _ = serve({"MODE": "server"}, bridge=box)
    _, _, page2 = req(port2, "GET", "/login")
    check_true("AC1 a box says Box and its own name", "EDGE" in page2 and re.search(r"\bBox\b", page2) is not None, "no 'EDGE' or 'Box'")
    check_true("AC1 and never shows the radio's path before sign-in", "/dev/serial/by-id" not in page2, "radio path leaked")


guarded("AC1", ac1)


# ---- AC2 the name is attribution, carried in the signed session ---------------------------------------------
def ac2():
    W.reset_throttle()
    port, etc, _ = serve({"MODE": "server"})
    st, hd, _, _ = login(port, "", PW)
    check_true("AC2 a blank name is refused, and no session is issued", st == 400 and "set-cookie" not in hd, f"status {st}")
    st, hd, _, _ = login(port, "x" * 41, PW)
    check_true("AC2 a name over 40 characters is refused", st == 400 and "set-cookie" not in hd, f"status {st}")
    st, hd, cookie, _ = login(port, "Sgt Patel", PW)
    check("AC2 a name and the right password sign in", (st, hd.get("location")), (302, "/"))
    val = cookie.partition("=")[2]
    parts = val.split(".")
    check("AC2 the session carries the name as a signed part: sid.exp.name.sig", len(parts), 4)
    if len(parts) == 4:
        try:
            got = base64.urlsafe_b64decode(parts[2] + "=" * (-len(parts[2]) % 4)).decode()
        except Exception:  # noqa: BLE001
            got = None
        check("AC2 the name part is the name, base64url", got, "Sgt Patel")
        forged = ".".join([parts[0], parts[1], base64.urlsafe_b64encode(b"Mallory").decode().rstrip("="), parts[3]])
        check("AC2 a session with a changed name is refused", req(port, "GET", "/api/status", cookie=f"mm_session={forged}")[0], 401)
    st, _, _ = req(port, "POST", "/api/quick_messages_set", json.dumps({"messages": ["On my way"]}), cookie=cookie, ctype="application/json")
    check("AC2 a change from the screen runs", st, 200)
    rows = audit(etc)
    ran = [r for r in rows if r.get("action") == "quick_messages_set"]
    check("AC2 the audit names who made it: operator: <name>", ran[-1].get("who") if ran else None, "operator: Sgt Patel")
    signin = [r for r in rows if r.get("event") == "sign-in"]
    check("AC2 and the sign-in itself is audited under the name", signin[-1].get("who") if signin else None, "operator: Sgt Patel")
    check_true("AC2 the audit never holds the password", PW not in audit_text(etc), "password in the audit")
    for _ in range(5):
        login(port, "Sgt Patel", "wrong")
    check("AC2 the throttle is unchanged: the sixth wrong password in a minute is 429", login(port, "Sgt Patel", "wrong")[0], 429)
    src = read("src/mesh_manager/web.py") or ""
    check("AC2 no change is written under the bare word operator any more",
          re.findall(r"who=\"operator\"|run_action\([^)]*,\s*\"operator\"\)", src), [])
    # a laptop has no sign-in: the name comes from the first run (Spec 110) and is used the same way
    W.reset_throttle()
    lport, letc, _ = serve({"MODE": "desktop", "AUTH": "off"}, passwd=False,
                           first={"state": "done", "name": "Matt", "done": "2026-09-30T12:00:00Z"})
    check("AC2 a laptop has no sign-in page", req(lport, "GET", "/login")[:2][0], 302)
    req(lport, "POST", "/api/quick_messages_set", json.dumps({"messages": ["Back at base"]}), ctype="application/json")
    lran = [r for r in audit(letc) if r.get("action") == "quick_messages_set"]
    check("AC2 on a laptop the name from the first run is the attribution", lran[-1].get("who") if lran else None, "operator: Matt")


guarded("AC2", ac2)


# ---- AC3 sign out -------------------------------------------------------------------------------------------
def ac3():
    W.reset_throttle()
    port, etc, _ = serve({"MODE": "server"})
    _, _, cookie, _ = login(port, "Cpl Jones")
    check("AC3 signed in first", req(port, "GET", "/api/status", cookie=cookie)[0], 200)
    st, hd, _ = req(port, "POST", "/logout", "", cookie=cookie)
    check("AC3 sign-out goes back to the sign-in page", (st, hd.get("location")), (302, "/login"))
    sc = hd.get("set-cookie", "")
    check_true("AC3 and clears the cookie in the browser", sc.startswith("mm_session=") and "Max-Age=0" in sc, sc)
    check("AC3 the old session, replayed, is refused", req(port, "GET", "/api/status", cookie=cookie)[0], 401)
    out = [r for r in audit(etc) if r.get("event") == "sign-out"]
    check("AC3 and the sign-out is audited under the name", out[-1].get("who") if out else None, "operator: Cpl Jones")
    check("AC3 sign-out without a session is refused, not a page", req(port, "POST", "/logout", "")[0], 401)


guarded("AC3", ac3)


# ---- AC4 change the password on This computer ------------------------------------------------------------
def ac4():
    W.reset_throttle()
    port, etc, _ = serve({"MODE": "server"})
    pw_path = os.path.join(etc, "passwd")
    _, _, c1, _ = login(port, "Sgt Patel")
    _, _, c2, _ = login(port, "Cpl Jones")
    before = open(pw_path).read()
    ino = os.stat(pw_path).st_ino
    st, _, _ = req(port, "POST", "/password", form(current="wrong", new="battery staple 42", again="battery staple 42"), cookie=c1)
    check_true("AC4 a wrong current password is refused", st in (400, 401, 403), f"status {st}")
    check_true("AC4 and nothing is written", open(pw_path).read() == before, "passwd changed")
    for _ in range(4):
        req(port, "POST", "/password", form(current="wrong", new="battery staple 42", again="battery staple 42"), cookie=c1)
    check("AC4 wrong current passwords count towards the throttle", req(port, "POST", "/password", form(current="wrong", new="battery staple 42", again="battery staple 42"), cookie=c1)[0], 429)
    W.reset_throttle()
    st, _, _ = req(port, "POST", "/password", form(current=PW, new="short", again="short"), cookie=c1)
    check_true("AC4 a new password under 8 characters is refused", st == 400 and open(pw_path).read() == before, f"status {st}")
    st, _, _ = req(port, "POST", "/password", form(current=PW, new="battery staple 42", again="battery staple 43"), cookie=c1)
    check_true("AC4 two new passwords that differ are refused", st == 400 and open(pw_path).read() == before, f"status {st}")
    st, hd, _ = req(port, "POST", "/password", form(current=PW, new="battery staple 42", again="battery staple 42"), cookie=c1)
    check_true("AC4 the right current password and a good new one are accepted", st in (200, 302), f"status {st}")
    check_true("AC4 the new password works and the old one does not",
               W.check_password(pw_path, "battery staple 42") and not W.check_password(pw_path, PW), open(pw_path).read()[:20])
    check("AC4 the file keeps its mode, 0640", stat.S_IMODE(os.stat(pw_path).st_mode), 0o640)
    check_true("AC4 the file was replaced whole (a rename in the etc dir), not rewritten in place",
               os.stat(pw_path).st_ino != ino, "same inode: written in place, which the screen's account cannot do on a box")
    check("AC4 nothing temporary is left in the etc dir", [f for f in os.listdir(etc) if f.startswith("passwd") and f != "passwd"], [])
    new_cookie = hd.get("set-cookie", "").split(";")[0]
    check_true("AC4 the person who changed it gets a fresh session", new_cookie.startswith("mm_session="), hd.get("set-cookie", ""))
    check("AC4 and it works", req(port, "GET", "/api/status", cookie=new_cookie)[0], 200)
    check("AC4 every other session is signed out", req(port, "GET", "/api/status", cookie=c2)[0], 401)
    ch = [r for r in audit(etc) if r.get("event") == "password-changed"]
    check("AC4 the change is audited under the name", ch[-1].get("who") if ch else None, "operator: Sgt Patel")
    check_true("AC4 and neither password is in the audit",
               bool(audit_text(etc)) and "battery staple" not in audit_text(etc), "a password in the audit")
    st, _, pg = req(port, "GET", "/password", cookie=new_cookie)
    check_true("AC4 the form asks for the current password and the new one twice",
               st == 200 and all(re.search(rf"name=['\"]{n}['\"]", pg) for n in ("current", "new", "again")), f"status {st}")


guarded("AC4", ac4)


# ---- AC5 a generated password must be changed at first sign-in ----------------------------------------------
def ac5():
    W.reset_throttle()
    port, etc, _ = serve({"MODE": "server"}, generated=True)
    _, _, cookie, _ = login(port, "Sgt Patel")
    for p in ("/", "/nodes"):
        st, hd, _ = req(port, "GET", p, cookie=cookie)
        check(f"AC5 while the generated password is in use, {p} goes to the change", (st, hd.get("location")), (302, "/password"))
    st, _, _ = req(port, "POST", "/api/quick_messages_set", json.dumps({"messages": ["x"]}), cookie=cookie, ctype="application/json")
    check("AC5 and a change from the screen is refused until it is done", st, 403)
    check("AC5 the change page itself answers", req(port, "GET", "/password", cookie=cookie)[0], 200)
    st, hd, _ = req(port, "POST", "/password", form(current=PW, new="battery staple 42", again="battery staple 42"), cookie=cookie)
    check_true("AC5 the change is accepted", st in (200, 302), f"status {st}")
    check_true("AC5 and the mark that it was generated is gone", not os.path.exists(os.path.join(etc, "passwd.generated")), "passwd.generated still there")
    fresh = hd.get("set-cookie", "").split(";")[0] or cookie
    st, hd, _ = req(port, "GET", "/", cookie=fresh)
    check_true("AC5 after the change the screen opens", not (st == 302 and hd.get("location") == "/password"), f"{st} {hd.get('location')}")
    W.reset_throttle()
    port2, _, _ = serve({"MODE": "server"}, generated=False)
    _, _, c2, _ = login(port2, "Sgt Patel")
    st, hd, _ = req(port2, "GET", "/", cookie=c2)
    check_true("AC5 a password the operator chose is never forced", hd.get("location") != "/password", f"{st} {hd.get('location')}")


guarded("AC5", ac5)


# ---- AC6 the installer never takes the password on its command line ------------------------------------------
S = os.path.join(ROOT, "install", "install.sh")


def fake_root():
    root = tempfile.mkdtemp()
    for d in ("etc/systemd/system", "opt", "var/lib", "opt/tak"):
        os.makedirs(os.path.join(root, d), exist_ok=True)
    open(os.path.join(root, "opt/tak/CoreConfig.xml"), "w").write(
        "<Configuration><network>\n        <input _name=\"meshtastic\" protocol=\"mcast\" port=\"6970\" group=\"239.2.3.1\">"
        "<filtergroup>mesh</filtergroup></input>\n</network></Configuration>\n")
    open(os.path.join(root, "etc/vantage-mesh.conf"), "w").write(
        "# written by vantage-mesh-gateway-install\nSERIAL=/dev/serial/by-id/usb-Espressif_USB_JTAG_serial_debug_unit_A4:CB:8F:EE:00:01-if00\n"
        "REGION=EU_868\nCHANNEL=MILUX-TAK\nFILTER_GROUP=mesh\nEXTRA_ARGS=\n")
    open(os.path.join(root, "etc/systemd/system/tak-meshtastic-gateway.service"), "w").write("[Unit]\nDescription=old\n")
    return root


def dry(*args, env=None, stdin=None):
    e = {k: v for k, v in os.environ.items() if k != "MESH_MANAGER_PASSWORD"}
    e.update(env or {}, MESH_MANAGER_ROOT=fake_root())
    r = subprocess.run(["bash", S, "/nonexistent/mesh-manager-1.5.0-amd64.tgz", "--dry-run", *args],
                       capture_output=True, text=True, env=e, input=stdin, timeout=120)
    return r.returncode, r.stdout + r.stderr


def ac6():
    rc, out = dry("--password", "a-long-password")
    check_true("AC6 --password on the command line is refused", rc != 0, f"exit {rc}")
    check_true("AC6 and the refusal names the two ways that work",
               "MESH_MANAGER_PASSWORD" in out and "--password-stdin" in out, out[-300:])
    check_true("AC6 and it does not echo the password back", "a-long-password" not in out, "the password was printed")
    rc, out = dry(env={"MESH_MANAGER_PASSWORD": "a-long-password"})
    check_true("AC6 MESH_MANAGER_PASSWORD is used: the hash is written, nothing generated",
               rc == 0 and "write the operator password hash" in out and "generate an operator password" not in out, out[-300:])
    rc, out = dry("--password-stdin", stdin="a-long-password\n")
    check_true("AC6 --password-stdin reads one line from stdin: the hash is written, nothing generated",
               rc == 0 and "write the operator password hash" in out and "generate an operator password" not in out, out[-300:])
    rc, out = dry("--password-stdin", stdin="short\n")
    check_true("AC6 a password under 8 characters from stdin is refused, saying why",
               rc != 0 and "8 characters" in out, out[-200:])
    rc, out = dry()
    check_true("AC6 with neither, a password is generated and shown once", rc == 0 and "generate an operator password" in out, out[-300:])
    check_true("AC6 and marked, so it must be changed at first sign-in", "first sign-in" in out, out[-300:])
    text = read("install/install.sh") or ""
    check_true("AC6 the marker is written by the installer and cleared when the operator sets one",
               "passwd.generated" in text, "no passwd.generated in the installer")


guarded("AC6", ac6)

finish()
