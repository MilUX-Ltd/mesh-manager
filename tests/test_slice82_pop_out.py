#!/usr/bin/env python3
"""Spec 082: a chat in its own window, and in the order you want them.

Matt: "Messages. Pop the chats out into new windows. Move the chats around the grid."

The catch is that both windows are the same origin and share the same browser storage, so a popped
out window that wrote the list of open panes would rewrite the grid behind it.
"""
import http.client, json, os, re, shutil, subprocess, sys, tempfile, threading, time
sys.path.insert(0, os.path.dirname(__file__))
from _common import ROOT, check, check_true, finish, read, skip  # noqa: E402
sys.path.insert(0, os.path.join(ROOT, "src"))
from fakebridge_lib import start_fake_bridge  # noqa: E402
from mesh_manager import web as W  # noqa: E402

fb = start_fake_bridge()
srv = W.make_server(bind="127.0.0.1", port=0, socket_path=fb.path, etc_dir=tempfile.mkdtemp(),
                    config={"AUTH": "off"}, state_dir=tempfile.mkdtemp())
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
time.sleep(0.3)
c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
c.request("GET", "/messages")
r = c.getresponse()
body = r.read().decode()
c.close()
check("the page still answers", r.status, 200)

web = read("src/mesh_manager/web.py") or ""
i = web.index("CSS = ")
css = web[i:web.index('"""', web.index('"""', i) + 3)]

# AC1 the chat can leave the page
check_true("AC1 the chat's menu offers to pop it out", "data-act='popout'" in body)
check_true("AC1 it opens a window, not a tab", "window.open(" in body)
check_true("AC1 the window is named after the chat, so a taskbar of them can be read",
           re.search(r"name\s*=\s*'mm-chat-'\s*\+\s*key", body) is not None
           and re.search(r"window\.open\(\s*url\s*,\s*name\s*,", body) is not None)
check_true("AC1 and sized for a conversation, not a page", re.search(r"width=\d+,height=\d+", body) is not None)

# AC2 a move, not a copy
pop = body.split("function popOut(key){", 1)
pop = pop[1][:1400] if len(pop) > 1 else ""
check_true("AC2 popping out takes the pane out of the grid", "open=open.filter(" in pop)
check_true("AC2 a chat already popped out is brought forward, not opened twice",
           ".focus()" in pop or ".focus()" in body.split("function openChat(", 1)[-1][:400])
check_true("AC2 openChat sends you to the window when the chat is already in one",
           "popped[" in body and "openChat" in body)

# AC3 the popped-out window is the chat and nothing else
check_true("AC3 the window asks for solo", "solo=1" in body or "solo=" in body)
check_true("AC3 the page knows it is solo", "mm-solo" in body and "mm-solo" in css)
for hidden in ("nav.primary", "footer", "h1", ".chat-side"):
    check_true(f"AC3 solo hides {hidden}", hidden in css.split("body.mm-solo", 1)[-1][:600])
check_true("AC3 its close button closes the window", "window.close()" in body)

# AC4 closing it puts the chat back where it was
check_true("AC4 the window tells the page it came from, on the way out",
           "mmChatReturn" in body and "beforeunload" in body)
check_true("AC4 and it goes back where it was, not on the end",
           re.search(r"mmChatReturn\s*=\s*function\([^)]*\)\{[\s\S]{0,300}?splice\(", body) is not None)
check_true("AC4 a page that has gone does no harm", "window.opener" in body and "catch" in body)

# AC5 a window is a view, not the state
keep = re.search(r"function keep\(\)\{([\s\S]{0,600}?)\}catch", body)
keep = keep.group(1) if keep else ""
check_true("AC5 what has been read is written from either window", "mm-chat-seen" in keep)
check_true("AC5 pins, mutes and hides are written from either window",
           all(k in keep for k in ("mm-chat-pins", "mm-chat-muted", "mm-chat-hidden")))
check_true("AC5 the list of open panes is not written by a popped-out window",
           re.search(r"if\s*\(!SOLO\)\s*localStorage\.setItem\('mm-chat-open'", keep) is not None)

# AC6/AC7 two ways to move a pane
check_true("AC6 a pane is dragged by its head", "draggable" in body and "dragstart" in body and "drop" in body)
check_true("AC7 and moved from the chat's own menu", "data-act='left'" in body and "data-act='right'" in body)
check_true("AC7 the menu says which way", "Move left" in body and "Move right" in body)
menu = re.search(r"function menuFor\(key\)\{([\s\S]{0,1400}?)\n  function", body)
menu = menu.group(1) if menu else ""
check_true("AC7 and offers neither where there is nowhere to go",
           "at>0" in menu.replace(" ", "") and "at<open.length-1" in menu.replace(" ", ""))
check_true("AC7 nor offers to move or pop out a chat that is already in its own window",
           "SOLO" in menu)

# AC8 remembered where the open panes already are
check_true("AC8 the order rides the list that is already kept", "mm-chat-open" in body)

# AC9 a refused window says so
check_true("AC9 a blocked window is said out loud, not swallowed",
           re.search(r"if\s*\(!w\b|if\s*\(!win\b", pop) is not None and ("blocked" in body or "would not open" in body))

# AC10 the move is a pure function, tested as one
check_true("AC10 movePane sits in the pure block",
           re.search(r"/\* chat:pure:start \*/[\s\S]*?function movePane\([\s\S]*?/\* chat:pure:end \*/", body) is not None)
m = re.search(r"/\* chat:pure:start \*/([\s\S]*?)/\* chat:pure:end \*/", body)
node = shutil.which("node")
if not node:
    skip("AC10 movePane under node", "node is not installed here; the workflow runner has it")
elif m:
    js = m.group(1) + r"""
console.log(JSON.stringify({
  right: movePane(['a','b','c'],'a',1),
  left:  movePane(['a','b','c'],'c',-1),
  middle:movePane(['a','b','c'],'b',1),
  offLeft: movePane(['a','b','c'],'a',-1),
  offRight:movePane(['a','b','c'],'c',1),
  absent: movePane(['a','b','c'],'zz',1),
  single: movePane(['a'],'a',1),
  empty:  movePane([],'a',1),
  copy:   (function(){var o=['a','b','c'];movePane(o,'a',1);return o;})()
}));
"""
    p = subprocess.run([node, "-e", js], capture_output=True, text=True)
    if p.returncode != 0:
        check_true("AC10 movePane runs", False)
        print(p.stderr[:500])
    else:
        g = json.loads(p.stdout)
        check("AC10 right moves one place right", g["right"], ["b", "a", "c"])
        check("AC10 left moves one place left", g["left"], ["a", "c", "b"])
        check("AC10 the middle one moves and the rest keep their order", g["middle"], ["a", "c", "b"])
        check("AC10 it will not fall off the left end", g["offLeft"], ["a", "b", "c"])
        check("AC10 nor off the right", g["offRight"], ["a", "b", "c"])
        check("AC10 a key that is not open is left alone", g["absent"], ["a", "b", "c"])
        check("AC10 one pane has nowhere to go", g["single"], ["a"])
        check("AC10 nothing to move", g["empty"], [])
        check("AC10 it returns a new list rather than reordering in place", g["copy"], ["a", "b", "c"])

finish()
