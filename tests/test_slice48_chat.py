#!/usr/bin/env python3
"""Spec 048: Messages as a chat. The page on the fake bridge; the pure functions under node."""
import http.client, json, os, re, shutil, subprocess, sys, tempfile, threading, time
sys.path.insert(0, os.path.dirname(__file__))
from _common import ROOT, check, check_true, finish, skip  # noqa: E402
sys.path.insert(0, os.path.join(ROOT, "src"))
from fakebridge_lib import start_fake_bridge  # noqa: E402
from mesh_manager import web as W  # noqa: E402

fb = start_fake_bridge()
srv = W.make_server(bind="127.0.0.1", port=0, socket_path=fb.path, etc_dir=tempfile.mkdtemp(), config={"AUTH": "off"}, state_dir=tempfile.mkdtemp())
port = srv.server_address[1]; threading.Thread(target=srv.serve_forever, daemon=True).start(); time.sleep(0.3)
def get(p):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10); c.request("GET", p); r = c.getresponse(); b = r.read().decode(); c.close(); return r.status, b

st, body = get("/messages")
check("AC1 the page answers", st, 200)
check_true("AC1 the chat list and the panes", "id='chat-list'" in body and "id='chat-panes'" in body)
check_true("AC1 the pure block is marked", "/* chat:pure:start */" in body and "/* chat:pure:end */" in body)
check_true("AC1 the composer template carries the quick messages", "data-quick=" in body and "id='chat-composer'" in body)
check_true("AC1 and AC7 the confirm texts for channel, direct and group are on the chat root", "data-confirm-channel=" in body and "data-confirm-direct=" in body and "data-confirm-group=" in body)
check("AC7 the old send form is gone", "id='send'" in body, False)
m = re.search(r"/\* chat:pure:start \*/([\s\S]*?)/\* chat:pure:end \*/", body)
node = shutil.which("node")
if not node:
    skip("AC2 to AC6 the pure functions under node", "node is not installed here; the workflow runner has it")
elif m:
    js = m.group(1) + r"""
var own='!00000001', t=Date.parse('2026-01-01T10:00:00Z');
function iso(ms){return new Date(ms).toISOString().replace('.000Z','Z');}
var msgs=[{from:'!aa000001',name:'Tracker9',to:'^all',channel:0,ts:iso(t),text:'hi'},
          {from:own,name:'box',to:'!aa000001',channel:0,ts:iso(t+60000),text:'yo',sent:true,mid:1},
          {from:'!bb000002',name:'Tracker2',to:own,channel:0,ts:iso(t+120000),text:'psst'},
          {from:'!aa000001',name:'Tracker9',to:'^all',channel:1,ts:iso(t+180000),text:'ops'}];
var channels=[{index:0,name:'MESH',role:'PRIMARY'},{index:1,name:'OPS',role:'SECONDARY'},{index:2,name:'QUIET',role:'SECONDARY'}];
var groups=[{name:'Recce',members:['!aa000001'],count:1}];
var chats=chatsFrom(msgs,own,channels,groups,{});
console.log(JSON.stringify({keys:msgs.map(function(m){return chatKey(m,own);}), order:chats.map(function(c){return c.key;}),
  ch0:(chats.filter(function(c){return c.key==='ch:0';})[0]||{}), quiet:(chats.filter(function(c){return c.key==='ch:2';})[0]||{}).ts||null,
  grp:chats.some(function(c){return c.key==='group:Recce';}),
  open1:openPane(['a','b','c'],'d',3), open2:openPane(['a','b'],'a',3),
  unread_ch0:unreadCount(msgs,'ch:0',own,0), unread_ch0_seen:unreadCount(msgs,'ch:0',own,t), unread_dm:unreadCount(msgs,'dm:!aa000001',own,0), unread_dm2:unreadCount(msgs,'dm:!bb000002',own,0),
  conf:[needsConfirm('ch:0'),needsConfirm('group:Recce'),needsConfirm('dm:!aa000001')]}));
"""
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
        fh.write(js); path = fh.name
    try:
        out = subprocess.run([node, path], capture_output=True, text=True, timeout=30)
        got = json.loads(out.stdout.strip() or "{}") if out.returncode == 0 else {"error": out.stderr.strip()[:300]}
    except (OSError, ValueError, subprocess.SubprocessError) as ex:
        got = {"error": f"{type(ex).__name__}: {ex}"}
    check("AC2 a message finds its chat", got.get("keys"), ["ch:0", "dm:!aa000001", "dm:!bb000002", "ch:1"])
    check("AC3 newest first; a silent channel is still listed", (got.get("order") or [])[:1] + [got.get("quiet")], ["ch:1", None])
    check_true("AC3 every chat is there once", sorted(got.get("order") or []) == ["ch:0", "ch:1", "ch:2", "dm:!aa000001", "dm:!bb000002", "group:Recce"], str(got.get("order")))
    check("AC3 a chat carries its name, its last line and its time", ((got.get("ch0") or {}).get("name"), (got.get("ch0") or {}).get("last"), bool((got.get("ch0") or {}).get("ts"))), ("MESH", "hi", True))
    check("AC4 a fourth pane replaces the oldest; reopening moves to the end", (got.get("open1"), got.get("open2")), (["b", "c", "d"], ["b", "a"]))
    check("AC5 unread counts others' messages newer than last seen", (got.get("unread_ch0"), got.get("unread_ch0_seen"), got.get("unread_dm"), got.get("unread_dm2")), (1, 0, 0, 1))
    check("AC6 a channel or a group confirms, a direct message does not", got.get("conf"), [True, True, False])
    if "error" in got:
        check("AC2 node ran the functions", got["error"], "ran")
finish()
