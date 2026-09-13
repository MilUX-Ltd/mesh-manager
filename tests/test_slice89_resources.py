#!/usr/bin/env python3
"""Spec 089: the box serves its own brief, and the brief only.

The endpoint declared tools and nothing else, so a connecting client got one sentence of
instruction and the role and skills sat on the box as files nothing sent.

The specification treats the two primitives differently and that decides the design. Prompts are
user-controlled: a person picks one. Resources are application-driven, and the host may include
them automatically, so a resource can enter a context with nobody having decided. Therefore the
brief is a resource and the mesh's data stays a tool, where the floor gates it and the audit
records an agent that chose to look.
"""
import http.client, json, os, re, sys, tempfile, threading, time
sys.path.insert(0, os.path.dirname(__file__))
from _common import ROOT, check, check_true, finish, read  # noqa: E402
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "tests"))
from fakebridge_lib import start_fake_bridge  # noqa: E402
from mesh_manager import catalogue as C  # noqa: E402
from mesh_manager import connections as K  # noqa: E402
from mesh_manager import web as W  # noqa: E402

SKILLS = sorted(d for d in os.listdir(os.path.join(ROOT, "skills"))
                if os.path.isdir(os.path.join(ROOT, "skills", d)))

fb = start_fake_bridge()
etc = tempfile.mkdtemp()
srv = W.make_server(bind="127.0.0.1", port=0, socket_path=fb.path, etc_dir=etc,
                    config={"AUTH": "off"}, state_dir=tempfile.mkdtemp())
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
time.sleep(0.3)

conn = K.mint(etc, "suite-89", "propose")
TOKEN = conn["token"] if isinstance(conn, dict) else conn


def rpc(method, params=None, token=None, _id=1):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
    body = json.dumps({"jsonrpc": "2.0", "id": _id, "method": method, "params": params or {}})
    c.request("POST", "/mcp", body, {"Content-Type": "application/json",
                                     "Authorization": f"Bearer {token if token is not None else TOKEN}"})
    r = c.getresponse()
    out = r.read().decode()
    c.close()
    try:
        return r.status, json.loads(out)
    except ValueError:
        return r.status, {"raw": out[:200]}


# ---- AC1 the capabilities are declared -------------------------------------------------------------
st, init = rpc("initialize", {"protocolVersion": "2025-06-18"})
caps = ((init.get("result") or {}).get("capabilities") or {})
check("AC1 the endpoint still declares tools", "tools" in caps, True)
check("AC1 and now declares resources", "resources" in caps, True)
check("AC1 and prompts", "prompts" in caps, True)

# ---- AC2 the brief is offered ------------------------------------------------------------------------
st, lst = rpc("resources/list")
res = ((lst.get("result") or {}).get("resources") or [])
check_true("AC2 resources/list answers with a list", isinstance(res, list) and len(res) > 0, json.dumps(lst)[:200])
by_uri = {r.get("uri"): r for r in res if isinstance(r, dict)}
_txt = json.dumps(res)
check_true("AC2 the role is offered", "mesh-manager-agent" in _txt, str(sorted(by_uri))[:300])
_absent = [s for s in SKILLS if s not in _txt]
check("AC2 and every skill", _absent, [])
check_true("AC2 and the operator's standing brief", re.search(r"context|brief", _txt, re.I) is not None)
_thin = [r.get("uri") for r in res if not all(r.get(k) for k in ("uri", "name", "mimeType"))]
check("AC2 every entry carries uri, name and mimeType", _thin, [])
_untitled = [r.get("uri") for r in res if not r.get("title") or not r.get("description")]
check("AC2 and a title and description a person can read", _untitled, [])

# ---- AC3 what it serves is the file it is running --------------------------------------------------------
def read_uri(u):
    _s, out = rpc("resources/read", {"uri": u})
    cont = ((out.get("result") or {}).get("contents") or [])
    return (cont[0].get("text") if cont and isinstance(cont[0], dict) else None), out


_role_uri = next((u for u in by_uri if "mesh-manager-agent" in str(u)), None)
if _role_uri:
    got, _ = read_uri(_role_uri)
    check("AC3 the role it serves is the role it is running", got, read("agents/mesh-manager-agent.md"))
else:
    check_true("AC3 the role it serves is the role it is running", False, "no role resource to read")
for s in SKILLS:
    u = next((u for u in by_uri if s in str(u)), None)
    if not u:
        check_true(f"AC3 {s} is readable", False, "not offered")
        continue
    got, _ = read_uri(u)
    check(f"AC3 {s} is served byte for byte", got, read(f"skills/{s}/SKILL.md"))

# ---- AC4 no resource carries the mesh's data ---------------------------------------------------------------
# With no resources at all these pass for the wrong reason, which is how a guard rots into a
# decoration. There has to be something to guard before an absence means anything.
check_true("AC4 there are resources to check, so an empty answer is not mistaken for a clean one",
           len(res) >= 1 + len(SKILLS), f"{len(res)} offered")
_ids = {a["id"] for a in C.ACTIONS}
_leak = sorted({r.get("uri") for r in res
                if any(re.search(rf"\b{re.escape(a)}\b", str(r.get("uri")) + " " + str(r.get("name")))
                       for a in _ids)})
check("AC4 no catalogue action is reachable as a resource", _leak, [])
for probe in ("register", "nodes", "channels", "inventory", "history", "messages"):
    hit = [u for u in by_uri if re.search(rf"(^|[:/]){probe}s?($|[./])", str(u))]
    check(f"AC4 the mesh's {probe} is not attachable context", hit, [])

# ---- AC5 a read is audited, and says what it was ---------------------------------------------------------------
if _role_uri:
    read_uri(_role_uri)
    log = read(os.path.join(etc, "audit.log")) or ""
    if not log:
        try:
            log = open(os.path.join(etc, "audit.log")).read()
        except OSError:
            log = ""
    check_true("AC5 the read is audited under the connection's name", "suite-89" in log, log[-200:] or "empty log")
    _lines = [l for l in log.splitlines() if "suite-89" in l and "resource" in l]
    check_true("AC5 and the record says it was a resource read, not an action", bool(_lines),
               "no line naming a resource read")
    check_true("AC5 the line names what was read", any(str(_role_uri) in l for l in _lines),
               (_lines or ["none"])[-1][:200])

# ---- AC6 the same gate as the tools ------------------------------------------------------------------------------
st, out = rpc("resources/list", token="not-a-real-token")
check("AC6 a stranger's token gets nothing from resources", st, 401)
st, out = rpc("prompts/list", token="not-a-real-token")
check("AC6 nor from prompts", st, 401)

# ---- AC7 the prompts -----------------------------------------------------------------------------------------------
st, pl = rpc("prompts/list")
prompts = ((pl.get("result") or {}).get("prompts") or [])
check_true("AC7 prompts/list answers with a list", isinstance(prompts, list) and len(prompts) > 0, json.dumps(pl)[:200])
_pnames = [p.get("name") for p in prompts if isinstance(p, dict)]
check("AC7 there is one prompt per skill", len(prompts), len(SKILLS))
_pthin = [p.get("name") for p in prompts if not p.get("title") or not p.get("description")]
check("AC7 each carries a title and a description", _pthin, [])

# ---- AC8 a prompt carries the skill, from the one file --------------------------------------------------------------
if _pnames:
    st, pg = rpc("prompts/get", {"name": _pnames[0]})
    msgs = ((pg.get("result") or {}).get("messages") or [])
    check_true("AC8 prompts/get returns messages", bool(msgs), json.dumps(pg)[:200])
    _embedded = [m for m in msgs
                 if isinstance(m.get("content"), dict) and m["content"].get("type") == "resource"]
    check_true("AC8 the skill arrives as an embedded resource, not a second copy of the text",
               bool(_embedded), json.dumps(msgs)[:300])
    if _embedded:
        emb = _embedded[0]["content"].get("resource") or {}
        check_true("AC8 the embedded resource names its uri", bool(emb.get("uri")), str(emb)[:200])
        # the same bytes the resource serves: one source, not two
        served, _ = read_uri(emb.get("uri")) if emb.get("uri") in by_uri else (None, None)
        if served is not None:
            check("AC8 and it is the same text the resource serves", emb.get("text"), served)
        else:
            check_true("AC8 and it is the same text the resource serves", False,
                       f"{emb.get('uri')} is not an offered resource")

# ---- AC9 refusals, not tracebacks -----------------------------------------------------------------------------------
st, out = rpc("resources/read", {"uri": "mesh://nothing/here"})
check("AC9 an unknown resource is refused with the specified code", ((out.get("error") or {}).get("code")), -32002)
st, out = rpc("prompts/get", {"name": "no-such-prompt"})
check("AC9 an unknown prompt is refused as invalid params", ((out.get("error") or {}).get("code")), -32602)

# ---- AC10 the standing brief follows the operator ----------------------------------------------------------------------
# named exactly: every uri is under mesh://brief/, so a "context|brief" match found the role and
# then reported the role's first line as the standing brief. A loose selector is a false verdict.
_ctx_uri = next((u for u in by_uri if str(u).endswith("context.md")), None)
if _ctx_uri:
    open(os.path.join(etc, "context.md"), "w").write("The north gate is ours. Do not touch the relay.\n")
    got, _ = read_uri(_ctx_uri)
    check_true("AC10 the standing brief changes when the operator edits it, with no reconnect",
               "north gate" in str(got), str(got)[:120])
else:
    check_true("AC10 the standing brief changes when the operator edits it, with no reconnect",
               False, "no standing-brief resource")

# ---- AC11 the role stops nagging where the client already has it ----------------------------------------------------------
role = read("agents/mesh-manager-agent.md") or ""
check_true("AC11 the role accounts for the brief arriving as a resource",
           re.search(r"resource", role, re.I) is not None, "the role does not mention resources")

finish()
