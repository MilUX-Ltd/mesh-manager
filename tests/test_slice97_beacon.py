#!/usr/bin/env python3
"""Spec 097: a beacon on a timer, and an alert when it stops being answered.

Every proof the box holds that the mesh is alive is passive, so a quiet mesh and a dead one look
the same until somebody types a message and watches for the tick.

The part that is not obvious: a direct message and a broadcast do not prove the same thing. A
direct message's ack is the destination radio answering. A broadcast's is implicit, produced when
the local radio hears its own packet repeated by a neighbour, so it proves something within one hop
rebroadcast it and nothing about any named device. The alert's words have to match which proof was
actually had, or the product is claiming knowledge it does not have.

Every block is guarded. A missing helper is one verdict, not a traceback that hides every check
after it.
"""
import json, os, sys, tempfile, threading, time
sys.path.insert(0, os.path.dirname(__file__))
from _common import ROOT, check, check_true, finish, read  # noqa: E402
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "tests"))
import fakegw_lib  # noqa: E402
fakegw_lib.install()
from mesh_manager.bridge import Bridge  # noqa: E402


class FakeIface:
    """A radio that records what it was asked to send and hands back a packet id."""
    def __init__(self):
        self.sent = []
        self.next_id = 1000

    def sendText(self, text, destinationId=None, channelIndex=0, wantAck=False, onResponse=None, **_):
        self.next_id += 1
        self.sent.append({"text": text, "to": destinationId, "channel": int(channelIndex or 0),
                          "wantAck": bool(wantAck), "id": self.next_id})
        return type("P", (), {"id": self.next_id})()


class B(Bridge):
    """Enough bridge to drive a beacon without a radio on the bench."""
    def __init__(self, d, iface=None):
        self.state_dir = d
        self.interface = iface
        self.history = None
        self.peering = None
        self.box_mode = "server"
        self.outbox = {}
        self.batteries = {}
        self.remote_alerts = {}
        self._peers_lock = threading.Lock()
        self._stop = threading.Event()
        self.logger = type("L", (), {"warning": lambda *a, **k: None, "info": lambda *a, **k: None,
                                     "debug": lambda *a, **k: None})()
        self.events = []
        self._emit = lambda kind=None, **k: self.events.append(dict(k, kind=kind))

    def _own(self):
        return {"id": "!ee000001", "name": "Gateway"}

    def mesh_nodes(self):
        return [{"id": "!ee000011", "name": "Tracker 4", "heard_here": True}]

    def _register_load(self):
        return {"!ee000011": {"label": "Tracker 4", "managed": True}}

    def own_position(self):
        return {}


def box(iface=None):
    return B(tempfile.mkdtemp(), iface)


HAVE = all(hasattr(Bridge, m) for m in ("op_beacon_settings", "op_beacon_set", "op_beacon", "_beacon_due"))
if not HAVE:
    for ac in ("AC1 a beacon is off until somebody turns it on",
               "AC2 the interval, the target and the threshold are kept on the box",
               "AC3 an interval below the floor is refused in words",
               "AC4 a target is a node id or a channel index and nothing else",
               "AC5 one beacon per interval and no more",
               "AC6 an ack sets the miss count back to zero",
               "AC7 only a consecutive run counts",
               "AC8 the alert says what was actually proven",
               "AC9 the alert clears on the next ack",
               "AC10 a beacon is not a chat message",
               "AC11 a radio that is not there is not a miss",
               "AC12 the state is readable",
               "AC13 turning it off clears the alert behind it"):
        check_true(ac, False, "no beacon helpers on the bridge yet")
    finish()

# ---- AC1 off by default -------------------------------------------------------------------------------
a = box(FakeIface())
st = a.op_beacon_settings()
check("AC1 a beacon is off until somebody turns it on", bool(st.get("enabled")), False)
a._beacon_due(now=time.time())
check("AC1 and a box nobody configured sends nothing", len(a.interface.sent), 0)

# ---- AC2, AC3, AC4 the settings ------------------------------------------------------------------------
r = a.op_beacon_set(enabled="on", target="!ee000011", every_min=5, misses=3)
check("AC2 the settings are written", bool(r.get("confirmed")), True)
st = a.op_beacon_settings()
check("AC2 the interval, the target and the threshold are kept on the box",
      (st.get("every_min"), st.get("target"), st.get("misses"), bool(st.get("enabled"))),
      (5, "!ee000011", 3, True))
# and they survive a restart, because a beacon that forgets itself on a reboot is not a standing proof
st2 = B(a.state_dir, FakeIface()).op_beacon_settings()
check("AC2 and they survive a restart", (st2.get("target"), st2.get("every_min")), ("!ee000011", 5))

for bad, why in ((4, "below the floor"), (0, "zero"), (1441, "above the ceiling")):
    r = a.op_beacon_set(every_min=bad)
    check_true(f"AC3 an interval {why} is refused in words", "error" in r and "5" in str(r.get("error")),
               json.dumps(r)[:120])
check("AC3 and the refusal did not change what was set", a.op_beacon_settings().get("every_min"), 5)

for bad in ("everyone", "^all", "!nothex", "channel 9", "9"):
    r = a.op_beacon_set(target=bad)
    check_true(f"AC4 {bad!r} is not a target", "error" in r, json.dumps(r)[:120])
check("AC4 a channel index is a target", "error" in a.op_beacon_set(target="channel:0"), False)
check("AC4 a node id is a target", "error" in a.op_beacon_set(target="!ee000011"), False)

# ---- AC5 one per interval ------------------------------------------------------------------------------
b = box(FakeIface())
b.op_beacon_set(enabled="on", target="!ee000011", every_min=5, misses=3)
t0 = time.time()
b._beacon_due(now=t0)
check("AC5 the first tick sends one", len(b.interface.sent), 1)
b._beacon_due(now=t0 + 60)
b._beacon_due(now=t0 + 120)
check("AC5 and nothing more until the interval is up", len(b.interface.sent), 1)
b._beacon_due(now=t0 + 301)
check("AC5 then exactly one more", len(b.interface.sent), 2)
_s = b.interface.sent[0]
check("AC5 a beacon asks for an ack", _s.get("wantAck"), True)
check("AC5 and goes where it was told", _s.get("to"), "!ee000011")
check_true("AC5 its text says what it is, because a handset shows it to a person",
           "mesh manager" in str(_s.get("text")).lower() or "check" in str(_s.get("text")).lower(),
           str(_s.get("text")))
check_true("AC5 and it is short, because it costs airtime", len(str(_s.get("text")).encode()) <= 40,
           f"{len(str(_s.get('text')).encode())} bytes: {_s.get('text')}")

# ---- AC6, AC7 the counting -----------------------------------------------------------------------------
c = box(FakeIface())
c.op_beacon_set(enabled="on", target="!ee000011", every_min=5, misses=3)
t0 = time.time()


def tick(bx, at, answer=False):
    bx._beacon_due(now=at)
    pid = bx.interface.sent[-1]["id"]
    if answer:
        bx._on_ack({"decoded": {"requestId": pid, "routing": {"errorReason": "NONE"}}})
    return pid


# The rule, stated because the arithmetic depends on it: a beacon is a miss when the NEXT one goes
# out and it was never answered. One that has only just left has not had its interval yet, and
# counting it would raise the alarm a whole interval early and blame a device that is still
# answering. A late ack therefore still lands in time.
tick(c, t0, answer=True)
check("AC6 an ack sets the miss count back to zero", c.op_beacon().get("misses"), 0)
check_true("AC6 and records when it was answered", bool(c.op_beacon().get("answered")), str(c.op_beacon()))

tick(c, t0 + 301)            # judges the first, which was answered; sends the second
tick(c, t0 + 602)            # judges the second, silent
check("AC7 one unanswered beacon counts one", c.op_beacon().get("misses"), 1)
tick(c, t0 + 903, answer=True)
check("AC7 an ack in the middle of a run resets it", c.op_beacon().get("misses"), 0)
check("AC7 and no alert was raised on the way", [x for x in (c.op_alerts().get("open") or [])
                                                 if str(x.get("kind") or "") == "beacon"], [])

# ---- AC8, AC9 the alert, and its words -----------------------------------------------------------------
for i in range(1, 5):
    tick(c, t0 + 903 + 301 * i)
open_ = [x for x in (c.op_alerts().get("open") or []) if str(x.get("kind") or "") == "beacon"]
check_true("AC8 at the threshold an alert is raised", len(open_) == 1, json.dumps(open_)[:200])
_words = str((open_ or [{}])[0].get("text") or "")
check_true("AC8 a node beacon's alert names the node", "Tracker 4" in _words, _words)
check_true("AC8 and says it has not answered", "answer" in _words.lower(), _words)

tick(c, t0 + 903 + 301 * 6, answer=True)
check("AC9 the alert clears on the next ack",
      [x for x in (c.op_alerts().get("open") or []) if str(x.get("kind") or "") == "beacon"], [])
check("AC9 and the count is back to zero", c.op_beacon().get("misses"), 0)

# a channel beacon must not claim a device answered, because nothing did
d_ = box(FakeIface())
d_.op_beacon_set(enabled="on", target="channel:0", every_min=5, misses=2)
t0 = time.time()
for i in range(3):
    d_._beacon_due(now=t0 + 301 * i)
_w = str(([x for x in (d_.op_alerts().get("open") or []) if str(x.get("kind") or "") == "beacon"] or [{}])[0].get("text") or "")
check_true("AC8 a channel beacon's alert says nothing repeated it, not that the mesh is down",
           "repeat" in _w.lower(), _w)
for claim in ("mesh is down", "no one", "nobody received", "delivered"):
    check_true(f"AC8 and it does not claim {claim!r}", claim not in _w.lower(), _w)
check("AC8 a channel beacon goes to the channel it was given",
      (d_.interface.sent[0].get("to"), d_.interface.sent[0].get("channel")), ("^all", 0))

# ---- AC10 a beacon is not a chat message ---------------------------------------------------------------
e_ = box(FakeIface())
e_.op_beacon_set(enabled="on", target="!ee000011", every_min=5, misses=3)
e_.events.clear()
e_._beacon_due(now=time.time())
_texts = [x for x in e_.events if x.get("kind") == "text"]
check("AC10 a beacon raises no chat event", _texts, [])
_hist = []


class HistorySpy:
    ok = True

    def message(self, *a, **k):
        _hist.append((a, k))

    def set_ack(self, *a, **k):
        pass

    def has_message(self, *a, **k):
        return False

    def query(self, *a, **k):
        return []


f_ = box(FakeIface())
f_.history = HistorySpy()
f_.op_beacon_set(enabled="on", target="!ee000011", every_min=5, misses=3)
f_._beacon_due(now=time.time())
check("AC10 and writes nothing to the conversation", _hist, [])

# ---- AC11 a radio that is not there is not a miss -------------------------------------------------------
g_ = box(None)
g_.op_beacon_set(enabled="on", target="!ee000011", every_min=5, misses=1)
t0 = time.time()
for i in range(4):
    g_._beacon_due(now=t0 + 301 * i)
check("AC11 with no radio the count does not move", g_.op_beacon().get("misses"), 0)
check("AC11 and no alert is raised about the link",
      [x for x in (g_.op_alerts().get("open") or []) if str(x.get("kind") or "") == "beacon"], [])

# ---- AC12 the state is readable --------------------------------------------------------------------------
s = c.op_beacon()
for k in ("enabled", "target", "every_min", "misses", "threshold", "sent", "answered"):
    check_true(f"AC12 the state carries {k}", k in s, str(sorted(s)))

# ---- AC13 off clears the alert behind it -------------------------------------------------------------------
h_ = box(FakeIface())
h_.op_beacon_set(enabled="on", target="!ee000011", every_min=5, misses=1)
t0 = time.time()
h_._beacon_due(now=t0)
h_._beacon_due(now=t0 + 301)
check_true("AC13 there is an alert to clear",
           len([x for x in (h_.op_alerts().get("open") or []) if str(x.get("kind") or "") == "beacon"]) == 1,
           json.dumps(h_.op_alerts().get("open"))[:200])
h_.op_beacon_set(enabled="off")
check("AC13 turning it off clears the alert behind it",
      [x for x in (h_.op_alerts().get("open") or []) if str(x.get("kind") or "") == "beacon"], [])
h_._beacon_due(now=t0 + 602)
check("AC13 and it stops sending", len(h_.interface.sent), 2)

# ---- the catalogue and the screen ---------------------------------------------------------------------------
cat = read("src/mesh_manager/catalogue.py") or ""
# named by what an agent must be able to do, not by how many functions do it: reading the beacon
# and setting it. op_beacon_settings stays a helper the screen's form uses, and exposing the same
# fields twice in one catalogue would be two answers to one question.
check_true("AC2 an agent can read the beacon", '"id": "beacon"' in cat, "no read action")
check_true("AC2 and set it", '"id": "beacon_set"' in cat, "no write action")
check_true("AC2 and turning a transmitter on is an air action, not a read",
           '"id": "beacon_set", "title": "Set the beacon", "risk": "air"' in cat,
           "beacon_set is not risk air")
web = read("src/mesh_manager/web.py") or ""
check_true("AC2 and Settings carries the control", "beacon_set" in web, "nothing on the screen")
check_true("AC12 Health shows when it was last answered", "beacon" in web.lower(), "no beacon on any page")
guide = read("docs/GUIDE.md") or ""
check_true("AC8 the guide says what a channel beacon does not prove",
           "beacon" in guide.lower(), "the guide does not mention it")

finish()
