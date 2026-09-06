#!/usr/bin/env python3
"""Spec 066: the map says when it is not live, comes back on a press and on its own, and one slider dims
the rings, the nodes and the tracks. The pure functions run under node when node is here."""
import http.client, json, os, re, shutil, subprocess, sys, tempfile, threading, time
sys.path.insert(0, os.path.dirname(__file__))
from _common import ROOT, check, check_true, finish, read, skip  # noqa: E402
sys.path.insert(0, os.path.join(ROOT, "src"))
from fakebridge_lib import start_fake_bridge  # noqa: E402
from mesh_manager import web as W  # noqa: E402

fb = start_fake_bridge()
srv = W.make_server(bind="127.0.0.1", port=0, socket_path=fb.path, etc_dir=tempfile.mkdtemp(), config={"AUTH": "off"}, state_dir=tempfile.mkdtemp())
port = srv.server_address[1]; threading.Thread(target=srv.serve_forever, daemon=True).start(); time.sleep(0.3)


def get(p):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10); c.request("GET", p); r = c.getresponse()
    b = r.read().decode(); c.close(); return r.status, b


st, body = get("/map")
check("the map answers", st, 200)

# AC3: the control that comes back to live, and the words that say it will happen on its own
check_true("AC3 a control that returns the map to live", "id='play-live'" in body)
_livebtn = re.search(r"<button[^>]*id='play-live'[^>]*>", body)
check_true("AC3 it is named for what it does",
           _livebtn is not None and re.search(r"aria-label='[^']*live", _livebtn.group(0), re.I) is not None,
           "the button must say 'live' in its label, not only in its icon")
check_true("AC3 the map says how long before it returns on its own", "id='play-idle'" in body)
check_true("AC3 the button starts disabled, because the map starts live", re.search(r"id='play-live'[^>]*disabled", body) is not None)

# AC4: a slider, not three presets
check_true("AC4 the dimming control is a range input", re.search(r"type='range'[^>]*id='map-dim'", body) is not None)
check_true("AC4 the three presets are gone", "name='rings'" not in body, "off/faint/solid replaced the slider on 5 Sep; the slider is what Matt uses")
check_true("AC4 one value drives the rings, the nodes and the tracks",
           all(w in body for w in ("mm-dim", "dimNodes", "dimTracks")))

# AC5 and AC6: what it says at nothing, and what it remembers
check_true("AC5 at zero the label says the overlay is off", "overlay off" in body)
check_true("AC6 the value is remembered", "localStorage.setItem('mm-dim'" in body.replace('"', "'"))
check_true("AC6 a browser holding the old rings setting is carried over", "mm-ring-alpha" in body)

# AC1 and AC2: the pure functions
m = re.search(r"/\* pure:start \*/([\s\S]*?)/\* pure:end \*/", body)
check_true("AC1 the pure functions are marked", m is not None)
node = shutil.which("node")
if not node:
    skip("AC1 AC2 the pure functions under node", "node is not installed here; the workflow runner has it")
elif m:
    js = m.group(1) + r"""
var t1=Date.parse('2026-01-01T12:00:00Z'), rg=[t1-3*3600*1000, t1];
console.log(JSON.stringify({
  liveNull: liveState(null, rg),
  liveEnd:  liveState(t1-200, rg),
  past:     liveState(t1-3600*1000, rg),
  idleWaits:   idleReturn(60000, false, 120000),
  idleReturns: idleReturn(120000, false, 120000),
  idleNotWhilePlaying: idleReturn(999000, true, 120000),
  dimOff: dimLabel(0), dimSome: dimLabel(60), dimFull: dimLabel(100)}));
"""
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
        fh.write(js); path = fh.name
    try:
        out = subprocess.run([node, path], capture_output=True, text=True, timeout=30)
        got = json.loads(out.stdout.strip() or "{}") if out.returncode == 0 else {"error": out.stderr.strip()[:300]}
    except (OSError, ValueError, subprocess.SubprocessError) as ex:
        got = {"error": f"{type(ex).__name__}: {ex}"}
    finally:
        os.unlink(path)
    check_true("AC1 the pure functions run", "error" not in got, str(got.get("error", ""))[:200])
    check("AC1 no instant chosen is live", (got.get("liveNull") or {}).get("live"), True)
    check("AC1 and it says so", (got.get("liveNull") or {}).get("words"), "live")
    check("AC1 within a second of the end is still live", (got.get("liveEnd") or {}).get("live"), True)
    check("AC1 an hour back is not live", (got.get("past") or {}).get("live"), False)
    check_true("AC1 it names the instant and how far back", "11:00" in str((got.get("past") or {}).get("words")) and "1 h" in str((got.get("past") or {}).get("words")),
               repr((got.get("past") or {}).get("words")))
    check("AC2 before the timeout it stays where it is", got.get("idleWaits"), False)
    check("AC2 at the timeout it returns to live", got.get("idleReturns"), True)
    check("AC2 it never interrupts a playback that is running", got.get("idleNotWhilePlaying"), False)
    check("AC5 zero says the overlay is off", got.get("dimOff"), "overlay off")
    check_true("AC5 and a value says what it is", "60" in str(got.get("dimSome")), repr(got.get("dimSome")))

# AC7: the guide
g = read("docs/GUIDE.md") or ""
check_true("AC7 the guide says how to get back to live", "back to live" in g.lower())
check_true("AC7 and what the slider does", "dim" in g.lower() and "tracks" in g.lower())
finish()
