#!/usr/bin/env python3
"""Spec 012: the help page, against the fake bridge."""
import http.client
import os
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(__file__))
from _common import ROOT, check, check_true, finish  # noqa: E402
sys.path.insert(0, os.path.join(ROOT, "src"))
from fakebridge_lib import start_fake_bridge  # noqa: E402
from mesh_manager import web as W  # noqa: E402


def serve(config):
    fb = start_fake_bridge()
    srv = W.make_server(bind="127.0.0.1", port=0, socket_path=fb.path, etc_dir=tempfile.mkdtemp(), config=config)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.3)
    return srv, srv.server_address[1]


def get(port, path):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10); c.request("GET", path); r = c.getresponse(); b = r.read().decode(); c.close()
    return r.status, b


srv, port = serve({"AUTH": "off", "REGION": "EU_868"})
st, page = get(port, "/help")
check("AC1 /help answers", st, 200)
check_true("AC1 the kit guide: this radio and the primary channel", "!00000001" in page and "MILUX-TAK" in page)
check_true("AC1 the fleet: count and a holder", "Cpl Smith" in page and "managed" in page)
check_true("AC1 the shelf: a pin's version and state", "2.6.11" in page and "verified" in page and "missing" in page)
check_true("AC1 the lessons", "A quiet mesh is not a broken bridge" in page and "audit_verdict" not in page)
check_true("AC1 the four states", "unconfirmed" in page and "read back" in page)
check_true("AC1 Help is under More", "href='/help'" in page)
check_true("AC1 no reload", "location.reload(" not in page)
check_true("AC2 regions equal: no warning", "does not match" not in page)
srv.shutdown()
srv, port = serve({"AUTH": "off", "REGION": "US"})
st, page = get(port, "/help")
check_true("AC2 regions differ: the warning names both", "does not match" in page and "US" in page and "EU_868" in page)
srv.shutdown()
out = subprocess.run([sys.executable, "-m", "pip", "wheel", ROOT, "--no-deps", "-w", tempfile.mkdtemp(), "-q"], capture_output=True, text=True)
whl = [l for l in out.stdout.splitlines()] if False else None
import glob, zipfile
wheels = sorted(glob.glob(os.path.join(tempfile.gettempdir(), "**", "mesh_manager-*.whl"), recursive=True), key=os.path.getmtime)
check_true("AC3 the lessons file ships in the wheel", bool(wheels) and any(n.endswith("mesh_manager/mesh-lessons.md") for n in zipfile.ZipFile(wheels[-1]).namelist()))
finish()
