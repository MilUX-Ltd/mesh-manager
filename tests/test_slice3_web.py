#!/usr/bin/env python3
"""Spec 003: the screen, against a fake bridge socket. No radio, no gateway."""
import http.client
import json
import os
import socket
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(__file__))
from _common import ROOT, check, check_true, finish  # noqa: E402

sys.path.insert(0, os.path.join(ROOT, "src"))
try:
    from mesh_manager import web as W
except Exception as e:  # noqa: BLE001
    print(f"FAIL mesh_manager.web imports                                      {type(e).__name__}: {e}")
    print("\nFAILURES: 1"); sys.exit(1)

# ---- a fake bridge on a unix socket, speaking the slice 2 protocol -------------------------
sd = tempfile.mkdtemp()
sock_path = os.path.join(sd, "b.sock")
NODES = [{"id": "!aa000001", "name": "Tracker9", "battery": 77, "snr": 12.5, "hops": 0, "heard": "2026-09-03T02:00:00Z", "hw": "TRACKER_T1000_E", "lat": 51.2, "lon": -1.5},
         {"id": "!bb000002", "name": "Tracker2", "battery": 9, "snr": 3.0, "hops": 1, "heard": "2026-09-03T01:50:00Z", "hw": "TRACKER_T1000_E"}]
STATUS = {"version": "0.1.0", "radio": "/dev/serial/by-id/usb-x-if00", "radio_present": True, "connected": True,
          "observe": False, "last_activity": "2026-09-03T02:00:00Z", "last_forwarded": "2026-09-03T01:59:00Z",
          "nodes_seen": 2, "own": {"id": "!00000001", "name": "TAK Gateway"}, "region": "EU_868", "modem_preset": "SHORT_FAST",
          "primary_channel": "MILUX-TAK", "uptime": 120, "watchdog": "pinging"}
CHANNELS = {"channels": [{"index": 0, "name": "MILUX-TAK", "role": "PRIMARY", "has_key": True}, {"index": 1, "name": "", "role": "DISABLED", "has_key": False}],
            "url": "https://meshtastic.org/e/#SECRET-URL-WITH-KEY-ZZZ"}
event_clients = []


def fake_bridge():
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); srv.bind(sock_path); srv.listen(8)
    while True:
        c, _ = srv.accept()
        threading.Thread(target=serve_one, args=(c,), daemon=True).start()


def serve_one(c):
    f = c.makefile("rb")
    line = f.readline()
    try:
        req = json.loads(line.decode())
    except Exception:  # noqa: BLE001
        c.sendall(b'{"error": "bad json"}\n'); c.close(); return
    op = req.get("op")
    if op == "events":
        c.sendall(b'{"kind": "hello"}\n'); event_clients.append(c); return
    rep = {"status": STATUS, "nodes": {"nodes": NODES}, "channels": CHANNELS,
           "log": {"lines": ["[02:00:01] Connected to the Meshtastic Device", "[02:00:05] Sending <event uid=\"!aa000001\">"]},
           "config": {"long_name": "TAK Gateway"}}.get(op, {"error": "unknown op"})
    c.sendall((json.dumps(rep) + "\n").encode()); c.close()


threading.Thread(target=fake_bridge, daemon=True).start()
time.sleep(0.2)

etc = tempfile.mkdtemp()
W.write_password(os.path.join(etc, "passwd"), "correct horse")
srv = W.make_server(bind="127.0.0.1", port=0, socket_path=sock_path, etc_dir=etc)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
time.sleep(0.2)


def req(method, path, body=None, cookie=None, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    h = dict(headers or {})
    if cookie: h["Cookie"] = cookie
    if body is not None: h["Content-Type"] = "application/x-www-form-urlencoded"
    c.request(method, path, body=body, headers=h)
    r = c.getresponse(); data = r.read(); hd = dict((k.lower(), v) for k, v in r.getheaders())
    c.close(); return r.status, hd, data


# AC1 nothing answers before sign-in
for p in ("/", "/nodes", "/log", "/channels"):
    st, hd, _ = req("GET", p)
    check(f"AC1 {p} redirects to /login when signed out", (st, hd.get("location")), (302, "/login"))
for p in ("/api/status", "/api/nodes", "/events"):
    st, _, _ = req("GET", p)
    check(f"AC1 {p} answers 401 when signed out", st, 401)
st, _, data = req("GET", "/healthz")
check("AC1 /healthz is open and sees the bridge", (st, json.loads(data).get("ok"), json.loads(data).get("bridge")), (200, True, True))
check("AC1 /login answers 200", req("GET", "/login")[0], 200)

# AC2 sign-in
st, hd, _ = req("POST", "/login", body="password=wrong")
check("AC2 wrong password: 401 and no cookie", (st, "set-cookie" in hd), (401, False))
for _ in range(5):
    req("POST", "/login", body="password=wrong")
check("AC2 the sixth wrong attempt inside a minute: 429", req("POST", "/login", body="password=wrong")[0], 429)
W.reset_throttle()
st, hd, _ = req("POST", "/login", body="password=correct+horse")
check("AC2 right password: redirect to /", (st, hd.get("location")), (302, "/"))
cookie = hd.get("set-cookie", "").split(";")[0]
check_true("AC2 a session cookie is set", cookie.startswith("mm_session="))
tampered = cookie[:-4] + "zzzz"
check("AC2 a tampered cookie is refused", req("GET", "/api/status", cookie=tampered)[0], 401)

# AC3 pages
st, _, home = req("GET", "/", cookie=cookie); home = home.decode()
check("AC3 overview answers", st, 200)
for want in ("TAK Gateway", "EU_868", "SHORT_FAST", "/dev/serial/by-id/usb-x-if00", "MILUX-TAK", "127.0.0.1"):
    check_true(f"AC3 overview carries {want}", want in home)
check_true("AC3 overview carries the closed statement", "closed" in home.lower())
st, _, nodes = req("GET", "/nodes", cookie=cookie); nodes = nodes.decode()
for want in ("Tracker9", "Tracker2", "77", "12.5", "TRACKER_T1000_E"):
    check_true(f"AC3 nodes page carries {want}", want in nodes)
st, _, log = req("GET", "/log", cookie=cookie)
check_true("AC3 log page carries the ring", "Connected to the Meshtastic Device" in log.decode())
st, _, api = req("GET", "/api/nodes", cookie=cookie)
check("AC3 /api/nodes is the bridge's list", json.loads(api).get("nodes"), NODES)

# AC4 channels and the QR
st, _, chp = req("GET", "/channels", cookie=cookie); chp = chp.decode()
check_true("AC4 channels page carries the live channel's name and role, and the free-slot count", "MILUX-TAK" in chp and "PRIMARY" in chp and "free slot" in chp)
check_true("AC4 channels page links the QR image", "/channels/qr.png" in chp)
check_true("AC4 channels page carries no key and no join url", "SECRET-URL" not in chp and "meshtastic.org/e/" not in chp)
st, _, capi = req("GET", "/api/channels", cookie=cookie)
check_true("AC4 /api/channels carries no url", "url" not in json.loads(capi) and "SECRET" not in capi.decode())
st, hd, png = req("GET", "/channels/qr.png", cookie=cookie)
check("AC4 the QR is a PNG", (st, hd.get("content-type"), png[:8]), (200, "image/png", b"\x89PNG\r\n\x1a\n"))

# AC5 SSE
c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
c.request("GET", "/events", headers={"Cookie": cookie})
r = c.getresponse()
check("AC5 /events answers text/event-stream", (r.status, r.getheader("Content-Type", "").split(";")[0]), (200, "text/event-stream"))
time.sleep(0.3)
for ec in list(event_clients):
    try:
        ec.sendall((json.dumps({"kind": "packet", "from": "!aa000001", "port": "POSITION_APP"}) + "\n").encode())
    except OSError:
        pass
r.fp.raw._sock.settimeout(3) if hasattr(r.fp, "raw") else None
buf = b""; deadline = time.time() + 3
def _got_event(b):
    return b"event: mesh" in b and b"\n\n" in b.split(b"event: mesh", 1)[-1]
while time.time() < deadline and not _got_event(buf):
    try:
        chunk = r.fp.raw.read(1) if hasattr(r.fp, "raw") else r.read(1)
        if not chunk: break
        buf += chunk
    except (socket.timeout, TimeoutError):
        break
check_true("AC5 the bridge event arrives on the SSE stream as event: mesh", b"event: mesh" in buf and b"!aa000001" in buf, buf[-120:])
c.close()

# sign-in off, the operator's deliberate act
etc2 = tempfile.mkdtemp()
srv2 = W.make_server(bind="127.0.0.1", port=0, socket_path=sock_path, etc_dir=etc2, config={"AUTH": "off"})
port2 = srv2.server_address[1]
threading.Thread(target=srv2.serve_forever, daemon=True).start()
time.sleep(0.2)
c2 = http.client.HTTPConnection("127.0.0.1", port2, timeout=5); c2.request("GET", "/"); r2 = c2.getresponse(); body2 = r2.read().decode(); c2.close()
check_true("AUTH=off: the overview answers with no cookie and says sign-in is off", r2.status == 200 and "Sign-in is off" in body2)
c2 = http.client.HTTPConnection("127.0.0.1", port2, timeout=5); c2.request("GET", "/login"); r2 = c2.getresponse(); r2.read(); c2.close()
check("AUTH=off: /login redirects home", (r2.status, r2.getheader("Location")), (302, "/"))
srv2.shutdown()

# AC6 bind defaults
check("AC6 default bind is loopback", W.bind_from_config({}), ("127.0.0.1", 8093))
check("AC6 the operator's bind is honoured", W.bind_from_config({"BIND": "0.0.0.0", "PORT": "8093"}), ("0.0.0.0", 8093))
srv.shutdown()
finish()
