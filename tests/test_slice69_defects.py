#!/usr/bin/env python3
"""Spec 069: the defect slice. Each of these passed a hardware gate and failed a person."""
import http.client, os, sys, tempfile, threading, time
sys.path.insert(0, os.path.dirname(__file__))
from _common import ROOT, check, check_true, finish  # noqa: E402
sys.path.insert(0, os.path.join(ROOT, "src"))
from fakebridge_lib import start_fake_bridge  # noqa: E402
from mesh_manager import web as W  # noqa: E402


def serve(config):
    fb = start_fake_bridge()
    srv = W.make_server(bind="127.0.0.1", port=0, socket_path=fb.path, etc_dir=tempfile.mkdtemp(),
                        config=config, state_dir=tempfile.mkdtemp())
    threading.Thread(target=srv.serve_forever, daemon=True).start(); time.sleep(0.3)
    return srv.server_address[1]


def get(port, path):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10); c.request("GET", path)
    r = c.getresponse(); b = r.read().decode(); c.close(); return r.status, b


box = serve({"AUTH": "off"})
lap = serve({"AUTH": "off", "MODE": "desktop"})

# AC1 and AC2: the words match the shape
for path in ("/help", "/about"):
    _, b = get(lap, path)
    check_true(f"AC1 no journalctl on a laptop ({path})", "journalctl" not in b)
    check_true(f"AC2 a laptop is not called a box ({path})", "this box" not in b.lower() or "this laptop" in b.lower())
_, hb = get(box, "/help")
check_true("AC1 a box still gets the unit's log", "journalctl" in hb)
_, lf = get(lap, "/")
check_true("AC2 the footer on a laptop says laptop", "from the laptop that carries the radio" in lf)
_, bf = get(box, "/")
check_true("AC2 and on a box it still says box", "from the box that carries the radio" in bf)
_, lh = get(lap, "/help")
check_true("AC2 Help does not tell a laptop it is a box without TAK", "This box runs without TAK" not in lh)

# AC3: no tracebacks at the operator
check_true("AC3 the audit renders a value, never a traceback", "def audit_detail" in (open(os.path.join(ROOT, "src/mesh_manager/web.py")).read()))
check("AC3 a traceback is reduced to what failed",
      W.audit_detail({"error": 'Traceback (most recent call last):\n  File "x", line 1\nFileNotFoundError: [Errno 2] No such file'}),
      "error: FileNotFoundError: [Errno 2] No such file")
check("AC3 a plain value is left alone", W.audit_detail({"version": "0.27.0"}), "version: 0.27.0")
check("AC3 and nothing is nothing", W.audit_detail({}), "")

# AC4: the counts reconcile
_, home = get(box, "/")
check_true("AC4 the strip says the radio's database includes the radio",
           "in the radio's database" in home and "this radio included" in home)

# AC5 and AC6
for port, name in ((box, "box"), (lap, "laptop")):
    _, c = get(port, "/connections")
    check_true(f"AC5 no ADR cited at the operator ({name})", "ADR 003" not in c and "ADR-003" not in c)
_, ch = get(box, "/channels")
check_true("AC6 the Channels heading names what the control does",
           "Join a channel" not in ch and "Put another device on this channel" in ch)
finish()
