#!/usr/bin/env python3
"""Spec 047: playback with a timeline. The pure functions run under node when node is here."""
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

st, body = get("/map")
check("AC1 the map answers", st, 200)
check_true("AC1 the timeline canvas", "<canvas id='timeline'" in body)
for cid in ("play-go", "play-start", "play-rev", "play-speed", "play-fit", "play-at", "play-pos"):
    check_true(f"AC1 control {cid}", f"id='{cid}'" in body)
check_true("AC4 the trails select offers 72 h", "value='72'" in body)
st, _ = get("/api/trails?hours=72")
check("AC4 /api/trails?hours=72 is accepted", st, 200)
check_true("AC5 keys are ignored while typing", re.search(r"keydown[\s\S]{0,400}(INPUT|SELECT|TEXTAREA)", body) is not None)
m = re.search(r"/\* pure:start \*/([\s\S]*?)/\* pure:end \*/", body)
check_true("AC6 the pure functions are marked", m is not None)
node = shutil.which("node")
if not node:
    skip("AC2 AC3 AC6 the pure functions under node", "node is not installed here; the workflow runner has it")
elif m:
    js = m.group(1) + r"""
var t0=Date.parse('2026-01-01T00:00:00Z'), T=[t0,t0+60000,t0+120000,t0+180000];
var rows=T.map(function(t){return {node:'a',ts:new Date(t).toISOString().replace('.000Z','Z')};});
console.log(JSON.stringify({median:medianInterval(T), gap:gapFor(T), gapFloor:gapFor([t0,t0+1000,t0+2000]),
  stale:isStaleAt(t0+180000+130000,T,120000), fresh:isStaleAt(t0+180000+10000,T,120000),
  pos:(posAt(rows,'a',t0+90000)||{}).ts||null, none:posAt(rows,'a',t0-1),
  step:stepPlay(t0,1000,1,60,[t0+500,t0+3600000]), stepEnd:stepPlay(t0+3599000,1000,1,60,[t0,t0+3600000]), stepBack:stepPlay(t0+1000,1000,-1,60,[t0,t0+3600000])}));
"""
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
        fh.write(js); path = fh.name
    try:
        out = subprocess.run([node, path], capture_output=True, text=True, timeout=30)
        got = json.loads(out.stdout.strip() or "{}") if out.returncode == 0 else {"error": out.stderr.strip()[:200]}
    except (OSError, ValueError, subprocess.SubprocessError) as ex:
        got = {"error": f"{type(ex).__name__}: {ex}"}
    check("AC2 the median interval of four reports a minute apart is 60 s", got.get("median"), 60000)
    check("AC2 the gap is four medians", got.get("gap"), 240000)
    check("AC2 with a floor of two minutes", got.get("gapFloor"), 120000)
    check("AC2 stale when the last report is older than the gap", (got.get("stale"), got.get("fresh")), (True, False))
    check("AC3 posAt is the last report at or before t", got.get("pos"), "2026-01-01T00:01:00Z")
    check("AC3 and nothing after", got.get("none"), None)
    st = got.get("step") or {}
    check("AC7 a step from the start advances and keeps playing (the window slides)", (st.get("T") == t0_ms + 60000 if (t0_ms := 1767225600000) else None, st.get("playing")), (True, True))
    check("AC7 the end stops it, in the direction of travel", ((got.get("stepEnd") or {}).get("playing"), (got.get("stepBack") or {}).get("playing")), (False, False))
    if "error" in got:
        check("AC6 node ran the functions", got["error"], "ran")
finish()
