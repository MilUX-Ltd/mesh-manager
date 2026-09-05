#!/usr/bin/env python3
"""Spec 005: the catalogue, the MCP, the role and the first skills, against the fake bridge."""
import copy
import http.client
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(__file__))
from _common import ROOT, check, check_true, finish, read, skip  # noqa: E402
sys.path.insert(0, os.path.join(ROOT, "src"))
from fakebridge_lib import start_fake_bridge  # noqa: E402

try:
    from mesh_manager import web as W
    from mesh_manager import catalogue as C
    from mesh_manager import connections as K
except Exception as e:  # noqa: BLE001
    print(f"FAIL imports                                                     {type(e).__name__}: {e}")
    print("\nFAILURES: 1"); sys.exit(1)

fb = start_fake_bridge()
etc = tempfile.mkdtemp()
W.write_password(os.path.join(etc, "passwd"), "correct horse")
open(os.path.join(etc, "context.md"), "w").write("# Our mesh\nThe brief, verbatim.\n")
srv = W.make_server(bind="127.0.0.1", port=0, socket_path=fb.path, etc_dir=etc)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
time.sleep(0.3)


def req(method, path, body=None, cookie=None, ctype="application/x-www-form-urlencoded", token=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    h = {}
    if cookie: h["Cookie"] = cookie
    if token: h["Authorization"] = "Bearer " + token
    if body is not None: h["Content-Type"] = ctype
    c.request(method, path, body=body, headers=h)
    r = c.getresponse(); data = r.read(); hd = dict((k.lower(), v) for k, v in r.getheaders())
    c.close(); return r.status, hd, data


def rpc(method, params=None, token=None, id=1):
    st, _, data = req("POST", "/mcp", body=json.dumps({"jsonrpc": "2.0", "id": id, "method": method, "params": params or {}}), ctype="application/json", token=token)
    try:
        return st, json.loads(data)
    except ValueError:
        return st, {}


st, hd, _ = req("POST", "/login", body="password=correct+horse")
cookie = hd.get("set-cookie", "").split(";")[0]

# ---- AC1 parity -------------------------------------------------------------------------------
ids = sorted(a["id"] for a in C.ACTIONS)
routes = sorted(W.api_action_routes())
check("AC1 every catalogue id has an /api route and nothing else does", routes, ids)
tok_act = K.mint(etc, "test-act", "act")["token"]
st, r = rpc("tools/list", token=tok_act)
names = sorted(t["name"] for t in r.get("result", {}).get("tools", []))
check("AC1 an act connection sees the catalogue, propose and mesh_context", names, sorted(ids + ["propose", "mesh_context"]))
pages = {"/messages": req("GET", "/messages", cookie=cookie)[2].decode(), "/nodes": req("GET", "/nodes", cookie=cookie)[2].decode(),
         "/map": req("GET", "/map", cookie=cookie)[2].decode(),  # the survey's controls live on the map (Spec 022)
         "/health": req("GET", "/health", cookie=cookie)[2].decode()}  # the alert test lives on Health (Spec 026)
for a in C.ACTIONS:
    if a["risk"] == "air":
        check_true(f"AC1 air action {a['id']} has a control on a page marked with its id",
                   any(f"data-action='{a['id']}'" in p for p in pages.values()))
broken = copy.deepcopy(C.ACTIONS) + [{"id": "no_such_action", "title": "x", "description": "x", "risk": "read", "floor": "observe", "inputs": [], "op": "no_such"}]
check_true("AC1 an entry with no route fails the parity check", C.parity_problems(broken, W.api_action_routes(), names) != [])

# ---- AC2 the MCP and autonomy ------------------------------------------------------------------
st, _ = rpc("tools/list")
check("AC2 /mcp without a token answers 401", st, 401)
for _ in range(5):
    rpc("tools/list", token="not-a-token")
st, _ = rpc("tools/list", token="not-a-token")
check("AC2 the sixth wrong token answers 429", st, 429)
W.reset_throttle()
tok_obs = K.mint(etc, "test-observe", "observe")["token"]
st, r = rpc("tools/list", token=tok_obs)
obs_names = sorted(t["name"] for t in r.get("result", {}).get("tools", []))
check("AC2 observe sees exactly the reads plus mesh_context", obs_names, sorted([a["id"] for a in C.ACTIONS if a["risk"] == "read"] + ["mesh_context"]))
tok_prop = K.mint(etc, "test-propose", "propose")["token"]
st, r = rpc("tools/list", token=tok_prop)
prop_names = sorted(t["name"] for t in r.get("result", {}).get("tools", []))
check("AC2 propose adds the air actions and propose", prop_names, sorted([a["id"] for a in C.ACTIONS if a["risk"] in ("read", "air")] + ["propose", "mesh_context"]))
st, r = rpc("tools/call", {"name": "status", "arguments": {}}, token=tok_obs)
txt = (r.get("result", {}).get("content") or [{}])[0].get("text", "")
check_true("AC2 tools/call status returns the bridge's status as text", '"version"' in txt and "TAK Gateway" in txt)
st, r = rpc("tools/call", {"name": "send_text", "arguments": {"text": "hi"}}, token=tok_obs)
err = json.dumps(r)
check_true("AC2 send_text at observe is refused naming the autonomy", "observe" in err and ("error" in r or r.get("result", {}).get("isError")))
n_before = len(fb.calls)
st, r = rpc("tools/call", {"name": "send_text", "arguments": {"text": "from the agent", "channel": 0}}, token=tok_prop)
time.sleep(0.2)
check("AC2 send_text at propose reaches the bridge", fb.calls[-1] if len(fb.calls) > n_before else None, ("send_text", {"text": "from the agent", "channel": 0, "to": "^all"}))
audit = read(os.path.relpath(os.path.join(etc, "audit.log"), ROOT)) or open(os.path.join(etc, "audit.log")).read()
check_true("AC2 the call is audited under the connection's name", '"test-propose"' in audit and '"send_text"' in audit)

# ---- AC3 proposals and the Activity page ---------------------------------------------------------
st, r = rpc("tools/call", {"name": "propose", "arguments": {"action": "send_text", "arguments": {"text": "please confirm this", "channel": 0}, "rationale": "a test"}}, token=tok_prop)
pid = json.loads((r.get("result", {}).get("content") or [{"text": "{}"}])[0]["text"]).get("proposal")
check_true("AC3 propose queues a proposal", bool(pid))
st, r = rpc("tools/call", {"name": "propose", "arguments": {"action": "send_text", "arguments": {"text": "x" * 300}, "rationale": "bad"}}, token=tok_prop)
check_true("AC3 bad arguments are refused at propose", "error" in json.dumps(r).lower())
st, _, act = req("GET", "/activity", cookie=cookie); act = act.decode()
check_true("AC3 /activity lists the proposal with its rationale", "please confirm this" in act and "a test" in act)
n_before = len(fb.calls)
st, _, _ = req("POST", "/api/proposal/run", body=json.dumps({"id": pid}), cookie=cookie, ctype="application/json")
time.sleep(0.2)
check("AC3 running the proposal executes it through the catalogue", (st, fb.calls[-1] if len(fb.calls) > n_before else None), (200, ("send_text", {"text": "please confirm this", "channel": 0, "to": "^all"})))
audit = open(os.path.join(etc, "audit.log")).read()
check_true("AC3 the proposal and the run are both audited", '"proposal"' in audit and '"run"' in audit and '"operator"' in audit)
st, r = rpc("tools/call", {"name": "propose", "arguments": {"action": "traceroute", "arguments": {"dest": "!aa000001"}, "rationale": "dismiss me"}}, token=tok_prop)
pid2 = json.loads((r.get("result", {}).get("content") or [{"text": "{}"}])[0]["text"]).get("proposal")
st, _, _ = req("POST", "/api/proposal/dismiss", body=json.dumps({"id": pid2}), cookie=cookie, ctype="application/json")
check_true("AC3 dismiss is audited", '"dismiss"' in open(os.path.join(etc, "audit.log")).read())

# ---- AC4 connections -----------------------------------------------------------------------------
st, _, page = req("POST", "/connections", body="name=agent-one&autonomy=observe", cookie=cookie); page = page.decode()
m = re.search(r"mm_[A-Za-z0-9_-]{20,}", page)
check_true("AC4 minting on the page shows a token once", m is not None)
tok_page = m.group(0) if m else ""
check_true("AC4 the store holds a hash, not the token", tok_page and tok_page not in open(os.path.join(etc, "connections.json")).read())
cid = K.find_by_token(etc, tok_page)["id"]
cname = next(c["name"] for c in K.list_connections(etc) if c["id"] == cid)
st, _ = req("POST", "/connections/autonomy", body=f"id={cid}&autonomy=propose", cookie=cookie)[:2] if False else (None, None)
req("POST", "/connections/autonomy", body=f"id={cid}&autonomy=propose&confirm={cname}", cookie=cookie)   # Spec 007 AC9: the change names the connection
st, r = rpc("tools/list", token=tok_page)
check_true("AC4 autonomy change is honoured", "send_text" in [t["name"] for t in r.get("result", {}).get("tools", [])])
req("POST", "/connections/revoke", body=f"id={cid}", cookie=cookie)
st, _ = rpc("tools/list", token=tok_page)
check("AC4 a revoked token answers 401", st, 401)

# ---- AC5 mesh_context ----------------------------------------------------------------------------
st, r = rpc("tools/call", {"name": "mesh_context", "arguments": {}}, token=tok_obs)
check_true("AC5 mesh_context returns the brief verbatim", (r.get("result", {}).get("content") or [{}])[0].get("text", "") == "# Our mesh\nThe brief, verbatim.\n")
req("POST", "/settings", body="context=" + "%23+New+brief%0Aline+two%0A", cookie=cookie)
check("AC5 /settings saves the brief", open(os.path.join(etc, "context.md")).read(), "# New brief\nline two\n")

# ---- AC6 the role and the skills ----------------------------------------------------------------
role = read("agents/mesh-manager-agent.md") or ""
check_true("AC6 the role exists with frontmatter", role.startswith("---") and "autonomy: propose" in role and "skills:" in role)
# R-28 audit, 4 Sep 2026: the role's autonomy table is what an operator reads to decide what to
# hand over, and it had drifted 30 actions behind the catalogue, understating `act` by including
# neither firmware flashing nor rolling the software back. It is generated from the catalogue
# now; this keeps it that way.
_named = set(re.findall(r"`([a-z_]+)`", role))
_unnamed = sorted(a["id"] for a in C.ACTIONS if a["id"] not in _named)
check("AC6 the role names every catalogue action, so its stated scope is its real scope", _unnamed, [])
for _a in C.ACTIONS:
    _floor_line = [ln for ln in role.splitlines() if ln.startswith(f"| `{_a['floor']}`")]
    check_true(f"AC6 {_a['id']} is named at its own floor ({_a['floor']})",
               any(f"`{_a['id']}`" in ln for ln in _floor_line))
# The role must know no fleet: no radio id, no by-id path, no coordinates, no box. The firm's
# own names are checked too where their list is present (it does not travel with the product).
check_true("AC6 the role names no radio id", re.search(r"![0-9a-f]{8}", role) is None)
check_true("AC6 the role names no serial path", "/dev/serial/by-id/" not in role and "ttyACM" not in role)
check_true("AC6 the role names no coordinates", re.search(r"\b-?\d{1,2}\.\d{4,}\b", role) is None)
_priv = os.path.join(ROOT, "release", "private-strings.txt")
if os.path.exists(_priv):
    for bad in open(_priv).read().strip().split("|"):
        if re.fullmatch(r"[A-Za-z0-9_-]+", bad):
            check_true(f"AC6 the role names no fleet ({bad})", bad not in role)
else:
    skip("AC6 the firm's own fleet names", "private-strings.txt is not in this tree")
tools_all = set(ids + ["propose", "mesh_context"])
for sk in ("skills/mesh-lessons/SKILL.md", "skills/mesh-operate/SKILL.md", "skills/mesh-onboard/SKILL.md"):
    text = read(sk) or ""
    check_true(f"AC6 {sk} exists with frontmatter", text.startswith("---") and "audit_verdict:" in text and "license:" in text)
    named = set(re.findall(r"`([a-z_]+)`", text))
    unknown = sorted(n for n in named if n not in tools_all and n not in C.KNOWN_WORDS)
    check(f"AC6 {sk} names only things that exist", unknown, [])
_cut = os.path.join(ROOT, "release", "cut-release.sh")
if os.path.exists(_cut):
    chk = subprocess.run(["bash", os.path.join(ROOT, "release", "cut-release.sh"), "--check"], capture_output=True, text=True, cwd=ROOT)
    check_true("AC6 --check reports which skills would ship", "skills" in (chk.stdout + chk.stderr) and "unaudited" in (chk.stdout + chk.stderr))
    srv.shutdown()
else:
    skip("the cut carries the role and skills", "the release tooling is not in this tree")
finish()
