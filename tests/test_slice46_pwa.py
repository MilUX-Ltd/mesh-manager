#!/usr/bin/env python3
"""Spec 046: installable on a phone and a tablet."""
import http.client, json, os, struct, subprocess, sys, tempfile, threading, time
sys.path.insert(0, os.path.dirname(__file__))
from _common import ROOT, check, check_true, finish, skip  # noqa: E402
sys.path.insert(0, os.path.join(ROOT, "src"))
from fakebridge_lib import start_fake_bridge  # noqa: E402
from mesh_manager import web as W  # noqa: E402

fb = start_fake_bridge()
srv = W.make_server(bind="127.0.0.1", port=0, socket_path=fb.path, etc_dir=tempfile.mkdtemp(), config={"AUTH": "off"}, state_dir=tempfile.mkdtemp())
port = srv.server_address[1]; threading.Thread(target=srv.serve_forever, daemon=True).start(); time.sleep(0.3)
def get(p):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10); c.request("GET", p); r = c.getresponse(); b = r.read(); ct = r.getheader("Content-Type", ""); c.close(); return r.status, b, ct

st, body, ct = get("/manifest.webmanifest")
m = json.loads(body.decode()) if st == 200 else {}
check("AC1 the manifest answers as JSON", (st, "json" in ct), (200, True))
check("AC1 name, short name, start url, display", (m.get("name"), m.get("short_name"), m.get("start_url"), m.get("display")), ("Mesh Manager", "Mesh", "/", "standalone"))
check_true("AC1 theme and background colours", bool(m.get("theme_color")) and bool(m.get("background_color")))
pngs = [i for i in m.get("icons", []) if i.get("type") == "image/png"]
check_true("AC1 icons at 192 and 512 as PNG", {"192x192", "512x512"} <= {i.get("sizes") for i in pngs}, str([i.get("sizes") for i in pngs]))

def png_size(b):
    if b[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    return struct.unpack(">II", b[16:24])
for i in pngs:
    st, b, ct = get(i["src"])
    want = tuple(int(x) for x in i["sizes"].split("x"))
    check(f"AC2 {i['src']} is a PNG of the advertised size", (st, png_size(b)), (200, want))
st, body, ct = get("/")
page = body.decode()
import re
apple = re.search(r"<link rel='apple-touch-icon'[^>]*href='([^']+)'", page)
check_true("AC3 the head links the manifest", "rel='manifest'" in page and "href='/manifest.webmanifest'" in page)
check_true("AC3 theme-color and mobile-web-app-capable", "name='theme-color'" in page and "mobile-web-app-capable" in page)
check_true("AC3 an Apple touch icon is linked", bool(apple))
if apple:
    st, b, ct = get(apple.group(1))
    check("AC2 the Apple touch icon is 180", (st, png_size(b)), (200, (180, 180)))
for p in ("/", "/map", "/nodes"):
    st, body, ct = get(p)
    check(f"AC4 {p} registers no service worker", "serviceWorker" in body.decode(), False)
try:
    out = subprocess.run(["git", "ls-files", "src/mesh_manager/static/icons"], cwd=ROOT, capture_output=True, text=True, timeout=20).stdout.split()
    check_true("AC5 the icons are tracked, so the cut carries them", len([f for f in out if f.endswith(".png")]) >= 3, str(out))
except (OSError, subprocess.SubprocessError) as ex:
    skip("AC5 the icons are tracked", f"git not usable here: {ex}")
finish()
