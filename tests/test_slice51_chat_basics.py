#!/usr/bin/env python3
"""Spec 051: chat basics. The page's controls on the fake bridge; the pure functions under node."""
import http.client, json, os, re, shutil, subprocess, sys, tempfile, threading, time
sys.path.insert(0, os.path.dirname(__file__))
from _common import ROOT, check, check_true, finish, skip  # noqa: E402
sys.path.insert(0, os.path.join(ROOT, "src"))
from fakebridge_lib import start_fake_bridge  # noqa: E402
from mesh_manager import web as W  # noqa: E402

fb = start_fake_bridge()
srv = W.make_server(bind="127.0.0.1", port=0, socket_path=fb.path, etc_dir=tempfile.mkdtemp(), config={"AUTH": "off"}, state_dir=tempfile.mkdtemp())
port = srv.server_address[1]; threading.Thread(target=srv.serve_forever, daemon=True).start(); time.sleep(0.3)
c = http.client.HTTPConnection("127.0.0.1", port, timeout=10); c.request("GET", "/messages"); r = c.getresponse(); body = r.read().decode(); c.close()
check("AC1 the page answers", r.status, 200)
check_true("AC1 the toolbar: new message, filter, mark all read, the unread total", all(x in body for x in ("id='chat-new'", "id='chat-filter'", "id='chat-readall'", "id='chat-total'")))
check_true("AC1 the recipient picker template", "id='chat-picker'" in body)
check_true("AC1 the pane menu's actions", all(f"data-act='{a}'" in body for a in ("read", "unread", "pin", "mute", "hide")))
check_true("AC1 the bubble actions: copy and send again", "data-act='copy'" in body and "data-act='resend'" in body)
check_true("AC8 the Spec 048 contract holds: list, panes, composer, confirm texts", all(x in body for x in ("id='chat-list'", "id='chat-panes'", "id='chat-composer'", "data-confirm-channel=", "data-confirm-direct=", "data-confirm-group=")))

m = re.search(r"/\* chat:pure:start \*/([\s\S]*?)/\* chat:pure:end \*/", body)
node = shutil.which("node")
if not node:
    skip("AC2 to AC7 the pure functions under node", "node is not installed here; the workflow runner has it")
elif m:
    js = m.group(1) + r"""
var own='!00000001', t=Date.parse('2026-01-01T10:00:00Z');
function iso(ms){return new Date(ms).toISOString().replace('.000Z','Z');}
var msgs=[{from:'!aa000001',name:'Tracker9',to:'^all',channel:0,ts:iso(t),text:'at the RV now'},
          {from:own,name:'box',to:'!aa000001',channel:0,ts:iso(t+60000),text:'yo',sent:true,mid:1,ack:'MAX_RETRANSMIT'},
          {from:'!bb000002',name:'Tracker2',to:own,channel:0,ts:iso(t+120000),text:'psst'},
          {from:own,name:'box',to:'!bb000002',channel:0,ts:iso(t+150000),text:'ok',sent:true,mid:2,ack:'delivered'},
          {from:'!aa000001',name:'Tracker9',to:'^all',channel:1,ts:iso(t+180000),text:'ops'}];
var channels=[{index:0,name:'MESH',role:'PRIMARY'},{index:1,name:'rv',role:'SECONDARY'}];
var groups=[{name:'Recce',members:['!aa000001'],count:1}];
var nodes={'!aa000001':{name:'Tracker9'},'!bb000002':{name:'Tracker2'},'!00000001':{name:'box'}};
var rAll=recipients(nodes,channels,groups,own,''), rQ=recipients(nodes,channels,groups,own,'track'), rId=recipients(nodes,channels,groups,own,'!ee000099'), rNone=recipients({},[],[],own,'');
var chats=chatsFrom(msgs,own,channels,groups,{});
var seen1=markRead({}, 'ch:0', msgs, own, t+999000), seen2=markUnread({'dm:!bb000002':t+999000}, 'dm:!bb000002', msgs, own), seen3=markUnread({'ch:1':5}, 'group:Recce', [], own);
var sorted=sortChats(chats,['dm:!bb000002']), vis=visibleChats(chats,['ch:1'],false), visAll=visibleChats(chats,['ch:1'],true);
var f1=filterChats(chats,msgs,own,'rv'), f0=filterChats(chats,msgs,own,'');
var list=msgs.filter(function(m){return chatKey(m,own)==='ch:0';});
console.log(JSON.stringify({
  rAllKeys:rAll.map(function(x){return x.key;}), rQKeys:rQ.map(function(x){return x.key;}), rQLabel:(rQ[0]||{}).name, rId:rId.map(function(x){return [x.key,!!x.unknown];}), rNone:rNone.length,
  read_ch0:seen1['ch:0'], unread_after_unread:unreadCount(msgs,'dm:!bb000002',own,seen2['dm:!bb000002']), untouched:seen3,
  sortedFirst:sorted[0].key, sortedRest:sorted.slice(1).map(function(c){return c.key;}), visKeys:vis.map(function(c){return c.key;}), visAllHas:visAll.some(function(c){return c.key==='ch:1';}),
  total:unreadTotal(chats,[],[]), totalMuted:unreadTotal(chats,['ch:0'],[]), totalHidden:unreadTotal(chats,[],['ch:0','ch:1']),
  f1:f1.map(function(c){return [c.key,c.hits];}), f0:f0.length===chats.length&&f0.every(function(c){return c.hits===0;}),
  firstUnread:[firstUnreadIndex(list,own,0), firstUnreadIndex(list,own,t+999000)],
  resend:[canResend(msgs[1],own), canResend(msgs[3],own), canResend(msgs[0],own)]}));
"""
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
        fh.write(js); path = fh.name
    try:
        out = subprocess.run([node, path], capture_output=True, text=True, timeout=30)
        got = json.loads(out.stdout.strip() or "{}") if out.returncode == 0 else {"error": out.stderr.strip()[:300]}
    except (OSError, ValueError, subprocess.SubprocessError) as ex:
        got = {"error": str(ex)[:300]}
    finally:
        os.unlink(path)
    if "error" in got:
        check("AC2 node ran the functions", got["error"], "ran")
    else:
        check("AC2 recipients: channels, groups, every node but the box", got.get("rAllKeys"), ["ch:0", "ch:1", "group:Recce", "dm:!aa000001", "dm:!bb000002"])
        check("AC2 recipients filtered by name, label first", (got.get("rQKeys"), got.get("rQLabel")), (["dm:!aa000001", "dm:!bb000002"], "Tracker9"))
        check("AC2 a full unknown id is offered, flagged", got.get("rId"), [["dm:!ee000099", True]])
        check("AC2 an empty box offers nothing", got.get("rNone"), 0)
        check_true("AC3 markRead is the chat's newest line", got.get("read_ch0") == 1767261600000, str(got.get("read_ch0")))
        check("AC3 markUnread leaves exactly one unread", got.get("unread_after_unread"), 1)
        check("AC3 a chat with no line from others is unchanged", got.get("untouched"), {"ch:1": 5})
        check("AC4 pinned first, then newest first", (got.get("sortedFirst"), (got.get("sortedRest") or [])[:1]), ("dm:!bb000002", ["ch:1"]))
        check("AC4 hidden chats leave the list unless shown", ("ch:1" in (got.get("visKeys") or []), got.get("visAllHas")), (False, True))
        check("AC5 the unread total skips muted and hidden", (got.get("total"), got.get("totalMuted"), got.get("totalHidden")), (3, 2, 1))
        check("AC6 the filter matches names and text with hit counts", got.get("f1"), [["ch:1", 0], ["ch:0", 1]])
        check("AC6 the empty filter keeps all with no hits", got.get("f0"), True)
        check("AC7 the first unread line, or -1", got.get("firstUnread"), [0, -1])
        check("AC7 only the box's own failed message can be sent again", got.get("resend"), [True, False, False])
else:
    check("AC2 the pure block is on the page", bool(m), True)
finish()
