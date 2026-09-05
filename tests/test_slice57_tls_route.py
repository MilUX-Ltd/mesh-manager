#!/usr/bin/env python3
"""Spec 057: a TLS route for the screen. The installer's dry run, the forwarded client address, the Secure cookie, About."""
import http.client, os, subprocess, sys, tempfile, threading, time
sys.path.insert(0, os.path.dirname(__file__))
from _common import ROOT, check, check_true, finish, read  # noqa: E402
sys.path.insert(0, os.path.join(ROOT, "src"))
from fakebridge_lib import start_fake_bridge  # noqa: E402
from mesh_manager import web as W  # noqa: E402

S = os.path.join(ROOT, "install", "install.sh")
def dry(*args):
    return subprocess.run(["bash", S, "/nonexistent/mesh-manager-1.1.0+milux.3-amd64.tgz", "--dry-run", "--mode", "hub", *args], capture_output=True, text=True, timeout=60, env={**os.environ, "MESH_MANAGER_ROOT": tempfile.mkdtemp()})

# AC1: the installer
txt = read("install/install.sh") or ""
check_true("AC1 --tls-route is in the usage", "--tls-route <host>" in txt)
r = dry("--tls-route", "hub.example.org")
out = r.stdout + r.stderr
check_true("AC1 a dry run says what it would write", "Caddyfile.d/mesh-manager.caddy" in out and "127.0.0.1:8093" in out and "import /etc/caddy/Caddyfile.d/*.caddy" in out, out[-900:])
check_true("AC1 and would reload Caddy and record the route", "caddy" in out.lower() and "ROUTE_HOST=hub.example.org" in out, out[-600:])
check_true("AC1 the two ports are named for the operator, no firewall tool is run", "80/tcp" in out and "443/tcp" in out and "firewall" in out and "would: ufw" not in out and "would: firewall" not in out, out[-600:])
root2 = tempfile.mkdtemp(); os.makedirs(os.path.join(root2, "etc/caddy"))
open(os.path.join(root2, "etc/caddy/Caddyfile"), "w").write("# The Caddyfile is an easy way to configure your Caddy web server.\n:80 {\n\troot * /usr/share/caddy\n\tfile_server\n}\n")
r3 = subprocess.run(["bash", S, "/nonexistent/mesh-manager-1.1.0+milux.3-amd64.tgz", "--dry-run", "--mode", "hub", "--tls-route", "hub.example.org"], capture_output=True, text=True, timeout=60, env={**os.environ, "MESH_MANAGER_ROOT": root2})
check_true("AC1 the package's placeholder Caddyfile is replaced, not appended to", "placeholder Caddyfile" in (r3.stdout + r3.stderr), (r3.stdout + r3.stderr)[-400:])
open(os.path.join(root2, "etc/caddy/Caddyfile"), "w").write("vantage.example.org {\n\troot * /srv/demo\n\tfile_server\n}\n")
r4 = subprocess.run(["bash", S, "/nonexistent/mesh-manager-1.1.0+milux.3-amd64.tgz", "--dry-run", "--mode", "hub", "--tls-route", "hub.example.org"], capture_output=True, text=True, timeout=60, env={**os.environ, "MESH_MANAGER_ROOT": root2})
check_true("AC1 a Caddyfile with sites of its own is kept and gains the import line", "placeholder" not in (r4.stdout + r4.stderr) and "add to" in (r4.stdout + r4.stderr) and "left as it is" in (r4.stdout + r4.stderr), (r4.stdout + r4.stderr)[-400:])
r2 = dry("--tls-route", "not a host!")
check_true("AC1 a value that is not a hostname is refused", r2.returncode != 0 and "tls-route" in (r2.stdout + r2.stderr), (r2.stdout + r2.stderr)[-300:])

# AC2: the client address
class H:  # the shape client_ip reads
    def __init__(self, addr, fwd=None):
        self.client_address = (addr, 1); self.headers = {"X-Forwarded-For": fwd} if fwd else {}
check("AC2 the forwarded address counts only from loopback", (W.client_ip(H("127.0.0.1", "203.0.113.5, 10.0.0.1")), W.client_ip(H("::1", "203.0.113.7")), W.client_ip(H("198.51.100.9", "203.0.113.5")), W.client_ip(H("127.0.0.1"))), ("203.0.113.5", "203.0.113.7", "198.51.100.9", "127.0.0.1"))

# AC3: the Secure cookie
fb = start_fake_bridge()
etc = tempfile.mkdtemp()
srv = W.make_server(bind="127.0.0.1", port=0, socket_path=fb.path, etc_dir=etc, config={"AUTH": "on", "ROUTE_HOST": "hub.example.org"}, state_dir=tempfile.mkdtemp())
W.write_password(os.path.join(etc, "passwd"), "correct horse")
port = srv.server_address[1]; threading.Thread(target=srv.serve_forever, daemon=True).start(); time.sleep(0.3)
def login(headers):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    c.request("POST", "/login", body="password=correct+horse", headers={"Content-Type": "application/x-www-form-urlencoded", **headers}); r = c.getresponse(); r.read(); ck = r.getheader("Set-Cookie") or ""; c.close()
    return r.status, ck
st1, ck1 = login({"X-Forwarded-Proto": "https", "X-Forwarded-For": "203.0.113.5"})
st2, ck2 = login({})
check_true("AC3 signing in over the route sets a Secure cookie", st1 in (302, 303) and "mm_session=" in ck1 and "; Secure" in ck1, repr((st1, ck1[:80])))
check_true("AC3 and without TLS the cookie has no Secure", st2 in (302, 303) and "mm_session=" in ck2 and "Secure" not in ck2, repr((st2, ck2[:80])))

# AC4: About
c = http.client.HTTPConnection("127.0.0.1", port, timeout=10); c.request("GET", "/about", headers={"Cookie": ck1.split(";")[0]}); r = c.getresponse(); body = r.read().decode(); c.close()
check_true("AC4 About shows where the screen is reached", r.status == 200 and "https://hub.example.org" in body, repr((r.status, "hub.example.org" in body)))

# AC5: the guide
g = read("docs/GUIDE.md") or ""
check_true("AC5 the guide's Setting up names the route and its two ports", "--tls-route" in g and "443" in g and "80" in g)
finish()
