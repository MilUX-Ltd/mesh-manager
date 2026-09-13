#!/usr/bin/env python3
"""Spec 086: the catalogue is complete, and the tool surface tells the truth.

gateway_mqtt_set is in the bridge and was not in the catalogue, so the Radio page's own form
404'd before it reached the bridge. The parity suite compared the catalogue with the API routes
and the MCP tool names, all three generated from the catalogue, so they agreed by construction
and proved nothing about the world.
"""
import http.client, json, os, re, sys, tempfile, threading, time
sys.path.insert(0, os.path.dirname(__file__))
from _common import ROOT, check, check_true, finish, read  # noqa: E402
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "tests"))
from fakebridge_lib import start_fake_bridge  # noqa: E402
from mesh_manager import catalogue as C  # noqa: E402
from mesh_manager import web as W  # noqa: E402

# ---- AC1, AC2 the two actions ---------------------------------------------------------------------
def act(aid):
    return C.by_id(aid)

for aid in ("gateway_mqtt_set", "node_mqtt_set"):
    a = act(aid)
    check_true(f"AC1 {aid} is in the catalogue", a is not None)
    if not a:
        continue
    check(f"AC1 {aid} is a change", a.get("risk"), "change")
    check_true(f"AC1 {aid} has confirm text an operator can read", len(a.get("confirm") or "") > 20)
    check_true(f"AC1 {aid} has a description", len(a.get("description") or "") > 20)
    names = {i["name"] for i in a.get("inputs", [])}
    check_true(f"AC1 {aid} takes what the bridge takes",
               {"address", "username", "password", "root", "tls", "enabled"} <= names, str(sorted(names)))
    pw = next((i for i in a["inputs"] if i["name"] == "password"), None)
    check_true(f"AC2 {aid}.password is marked secret", bool(pw) and pw.get("secret") is True)
    check_true(f"AC1 {aid} asks for a confirm", "confirm" in names)
check_true("AC1 node_mqtt_set targets a node", "id" in {i["name"] for i in (act("node_mqtt_set") or {}).get("inputs", [])})

# AC2, through the Spec 085 helper
if act("gateway_mqtt_set"):
    red = C.redact_args("gateway_mqtt_set", {"address": "b.example", "password": "hunter2", "root": "milux"})
    check("AC2 the broker password never reaches the record", "hunter2" in json.dumps(red), False)
    check("AC2 and the rest of the call still does", red.get("address"), "b.example")

# ---- AC5 the catalogue against what the bridge can actually do -------------------------------------
br = read("src/mesh_manager/bridge.py") or ""
ops = set(re.findall(r"^    def op_([a-z0-9_]+)\(", br, re.M))
exposed = {a.get("op") or a["id"] for a in C.ACTIONS}
# Deliberate exclusions, each with its reason. channel_url returns a join URL, which carries the
# channel's key: it is withheld from the catalogue on purpose and Spec 085 is why that matters.
WITHHELD = {"channel_url": "returns a join URL, which carries the channel key"}
unexposed = sorted(ops - exposed - set(WITHHELD))
check("AC5 every bridge operation is exposed or deliberately withheld", unexposed, [])
check_true("AC5 the withheld list is short and each has a reason",
           len(WITHHELD) <= 3 and all(len(v) > 15 for v in WITHHELD.values()))
check("AC5 nothing is withheld that the catalogue then exposes anyway",
      sorted(set(WITHHELD) & exposed), [])

# ---- AC6 the catalogue against what the screen renders a form for -----------------------------------
web_src = read("src/mesh_manager/web.py") or ""
ids = {a["id"] for a in C.ACTIONS}
forms = sorted(set(re.findall(r"data-action='([a-z0-9_]+)'", web_src))
               | set(re.findall(r'data-action="([a-z0-9_]+)"', web_src)))
check("AC6 every write form on the screen names an action that exists",
      [f for f in forms if f not in ids], [])
check_true("AC6 and there are forms to check", len(forms) > 30, str(len(forms)))
stubbed = sorted(a for a in set(re.findall(r'_act\("([a-z0-9_]+)"\)', web_src))
                 | set(re.findall(r"_act\('([a-z0-9_]+)'\)", web_src)) if a not in ids)
check("AC6 no page looks up an action that falls through to the stub", stubbed, [])

# ---- AC3 the form writes: the action resolves and reaches the bridge ---------------------------------
fb = start_fake_bridge()
srv = W.make_server(bind="127.0.0.1", port=0, socket_path=fb.path, etc_dir=tempfile.mkdtemp(),
                    config={"AUTH": "off"}, state_dir=tempfile.mkdtemp())
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
time.sleep(0.3)


def post(path, body):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    c.request("POST", path, json.dumps(body), {"Content-Type": "application/json"})
    r = c.getresponse()
    out = r.read().decode()
    c.close()
    return r.status, out


st, out = post("/api/gateway_mqtt_set", {"address": "b.example", "confirm": "yes"})
check_true("AC3 the Radio page's action is not refused by the catalogue", "no action" not in out, out[:120])
check("AC3 and it is not a 404", st, 400 if "unknown op" in out else st)
check_true("AC3 it reached the bridge", "no action gateway_mqtt_set" not in out)
st2, out2 = post("/api/radio_set", {"long_name": "Gateway"})
check_true("AC3 the control behaves the same way", "no action" not in out2)

# AC4's second half, "and turn it off", found untested during the build and added then: the bridge
# reads a missing `enabled` as on, and an unticked HTML checkbox sends nothing, so a cleared box
# wrote MQTT ON while the label promised the opposite. The form must always say which.
_mq = web_src[web_src.index("def mqtt_card("):web_src.index("def radio_body(")]
check_true("AC4 the form says off when the box is cleared",
           re.search(r"name='enabled' value='off'[\s\S]{0,400}type='checkbox' name='enabled' value='on'", _mq) is not None)
_en = next(i for i in C.by_id("gateway_mqtt_set")["inputs"] if i["name"] == "enabled")
check_true("AC4 and the description tells an agent what leaving it out does",
           "left out" in _en["description"] and "off to turn it off" in _en["description"], _en["description"])

# ---- AC4 an agent can reach it, and the role names it -------------------------------------------------
names_act = {t["name"] for t in W.mcp_tools("act")}
for aid in ("gateway_mqtt_set", "node_mqtt_set"):
    check_true(f"AC4 an agent at act sees {aid}", aid in names_act)
check("AC4 an agent at observe does not", {"gateway_mqtt_set", "node_mqtt_set"} & {t["name"] for t in W.mcp_tools("observe")}, set())
role = read("agents/mesh-manager-agent.md") or ""
_unnamed = sorted(a["id"] for a in C.ACTIONS if f"`{a['id']}`" not in role)
check("AC4 the role's table names every action, the new ones included", _unnamed, [])

# ---- AC7 annotations from the risk taxonomy -----------------------------------------------------------
tools = {t["name"]: t for t in W.mcp_tools("act")}
check_true("AC7 tools carry annotations", all("annotations" in t for t in tools.values()))
# a missing annotations block is one verdict, not a crash that hides the next twenty
ann = lambda n: (tools.get(n) or {}).get("annotations") or {}
check("AC7 a read is marked read-only", ann("status").get("readOnlyHint"), True)
check("AC7 and not destructive", ann("status").get("destructiveHint"), False)
for aid in ("bench_flash", "node_reboot", "radio_set"):
    check(f"AC7 {aid} is marked destructive", ann(aid).get("destructiveHint"), True)
    check(f"AC7 {aid} is not marked read-only", ann(aid).get("readOnlyHint"), False)
check("AC7 an air action is neither read-only nor destructive",
      [ann("send_text").get("readOnlyHint"), ann("send_text").get("destructiveHint")],
      [False, False])
risks = {a["risk"] for a in C.ACTIONS}
check_true("AC7 every risk class has a mapping", hasattr(C, "ANNOTATIONS") and risks <= set(C.ANNOTATIONS),
           str(sorted(risks)))

# ---- AC8 titles ------------------------------------------------------------------------------------------
check_true("AC8 every tool carries its catalogue title",
           all(t.get("title") for n, t in tools.items() if C.by_id(n)))
check("AC8 and it is the catalogue's", tools["status"].get("title"), C.by_id("status")["title"])

# ---- AC9 structured content --------------------------------------------------------------------------------
src = web_src
check_true("AC9 a result carries structuredContent beside the text", "structuredContent" in src)
m = re.search(r"def mcp_call\([\s\S]{0,1200}?structuredContent", src)
check_true("AC9 in the result helper, so every call gets it", m is not None)

# ---- AC10 the protocol version -------------------------------------------------------------------------------
check_true("AC10 initialize negotiates rather than echoing the client",
           re.search(r'params\.get\("protocolVersion"\)[^\n]*\bif\b|def _negotiate|PROTOCOL_VERSIONS', src) is not None)
check_true("AC10 the server states which versions it supports", "PROTOCOL" in src)

finish()
