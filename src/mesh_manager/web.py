"""The screen: a stdlib HTTP server on the box, a client of the bridge's socket, showing the
mesh as it is now (Spec 003). Loopback by default; the operator is the one who opens it.
Nothing answers before sign-in except /healthz and /login."""
import argparse
import base64
import collections
import hashlib
import hmac
import html
import http.server
import json
import math
import os
import queue
import mimetypes
import re
import secrets
import socket
import sqlite3
import struct
import sys
import threading
import time
import urllib.parse
import zlib

from . import __version__
from .common import DEFAULT_CONFIG, DEFAULT_SOCKET, read_config
from . import catalogue as C
from . import mgrs as MG
from . import connections as K
from . import updates as U
from .common import DEFAULT_STATE

DEFAULT_ETC = "/etc/mesh-manager"
SESSION_HOURS = 12
THROTTLE_FAILS, THROTTLE_WINDOW = 5, 60
STATUS_TICK = 15

# ---- the operator password ---------------------------------------------------------------------
def write_password(path, password, iterations=200_000):
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    with open(path, "w") as fh:
        fh.write(f"pbkdf2_sha256${iterations}${salt.hex()}${digest.hex()}\n")
    try:
        os.chmod(path, 0o640)
    except OSError:
        pass


def check_password(path, password):
    try:
        alg, iters, salt, want = open(path).read().strip().split("$")
        got = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(iters))
        return alg == "pbkdf2_sha256" and hmac.compare_digest(got.hex(), want)
    except (OSError, ValueError):
        return False


_fails = collections.defaultdict(collections.deque)
_fails_lock = threading.Lock()


def throttled(ip):
    with _fails_lock:
        d = _fails[ip]
        now = time.time()
        while d and now - d[0] > THROTTLE_WINDOW:
            d.popleft()
        return len(d) >= THROTTLE_FAILS


def note_fail(ip):
    with _fails_lock:
        _fails[ip].append(time.time())


def reset_throttle():
    with _fails_lock:
        _fails.clear()


class Sessions:
    def __init__(self, etc_dir):
        self.path = os.path.join(etc_dir, "web.secret")
        if os.path.exists(self.path):
            self.secret = open(self.path, "rb").read().strip()
        else:
            self.secret = secrets.token_bytes(32)
            with open(self.path, "wb") as fh:
                fh.write(self.secret)
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass

    def _sign(self, body):
        return base64.urlsafe_b64encode(hmac.new(self.secret, body.encode(), hashlib.sha256).digest()).decode().rstrip("=")

    def issue(self):
        body = f"{secrets.token_hex(12)}.{int(time.time()) + SESSION_HOURS * 3600}"
        return f"{body}.{self._sign(body)}"

    def verify(self, value):
        try:
            sid, exp, sig = value.split(".")
            body = f"{sid}.{exp}"
            return hmac.compare_digest(self._sign(body), sig) and int(exp) > time.time()
        except (ValueError, AttributeError):
            return False


def bind_from_config(conf):
    try:
        port = int(conf.get("PORT") or 8093)
    except (TypeError, ValueError):
        port = 8093
    return (str(conf.get("BIND") or "127.0.0.1"), port)


# ---- the bridge, over its socket -------------------------------------------------------------------
class BridgeDown(Exception):
    pass


class BridgeClient:
    def __init__(self, socket_path):
        self.socket_path = socket_path

    def ask(self, op, timeout=5, **args):
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(timeout)
            s.connect(self.socket_path)
            s.sendall((json.dumps({"op": op, **args}) + "\n").encode())
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = s.recv(1 << 20)
                if not chunk:
                    break
                buf += chunk
            s.close()
            return json.loads(buf.decode() or "{}")
        except (OSError, ValueError) as e:
            raise BridgeDown(f"{type(e).__name__}: {e}") from e

    def reachable(self):
        try:
            return "version" in self.ask("status", timeout=2)
        except BridgeDown:
            return False


class Web:
    """Shared state: the bridge client, sessions, the SSE fan-out, and the pump that keeps
    one events connection open to the bridge."""
    def __init__(self, socket_path, etc_dir, config, bind, state_dir=DEFAULT_STATE):
        self.state_dir = state_dir
        self.client = BridgeClient(socket_path)
        self.sessions = Sessions(etc_dir)
        self.passwd = os.path.join(etc_dir, "passwd")
        self.config = config
        self.bind = bind
        # Sign-in is on unless the operator turns it off in the config (AUTH=off) or with
        # --no-auth: their deliberate act, for a screen behind a tunnel or on a closed LAN.
        self.auth_on = str(config.get("AUTH", "on")).strip().lower() not in ("off", "no", "false", "0")
        self.subs = []
        self.lock = threading.Lock()
        self.last_status = {}
        self.messages = collections.deque(maxlen=200)
        self.etc_dir = etc_dir
        self.stop = threading.Event()
        self.arch = U.config_arch(config)
        self.start_unit = None            # the suite substitutes systemctl here
        threading.Thread(target=self._pump, name="events-pump", daemon=True).start()
        threading.Thread(target=self._status_tick, name="status-tick", daemon=True).start()
        if self.update_mode() != "off":
            U.Checker(self, arch=self.arch).start()

    def github_token(self):
        try:
            return open(os.path.join(self.etc_dir, "github.token")).read().strip()
        except OSError:
            return ""

    def set_github_token(self, token):
        p = os.path.join(self.etc_dir, "github.token")
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(token.strip() + "\n")
        os.chmod(p, 0o600)

    def update_mode(self):
        try:
            m = json.load(open(os.path.join(self.etc_dir, "update.json"))).get("mode")
        except (OSError, ValueError):
            m = None
        m = m or str(self.config.get("UPDATE_MODE") or "manual")
        return m if m in ("manual", "auto", "off") else "manual"

    def set_update_mode(self, mode):
        if mode in ("manual", "auto", "off"):
            with open(os.path.join(self.etc_dir, "update.json"), "w") as fh:
                json.dump({"mode": mode}, fh)

    def update_available(self):
        rec = U.last_check(self.state_dir)
        return rec.get("version") if rec.get("available") and rec.get("version") != __version__ else None

    def subscribe(self):
        q = queue.Queue(maxsize=500)
        with self.lock:
            self.subs.append(q)
        return q

    def unsubscribe(self, q):
        with self.lock:
            if q in self.subs:
                self.subs.remove(q)

    def broadcast(self, line):
        if '"kind": "text"' in line:
            try:
                self.messages.append(json.loads(line))
            except ValueError:
                pass
        with self.lock:
            for q in list(self.subs):
                try:
                    q.put_nowait(line)
                except queue.Full:
                    pass

    def _pump(self):
        while not self.stop.is_set():
            try:
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.settimeout(60)
                s.connect(self.client.socket_path)
                s.sendall(b'{"op": "events"}\n')
                f = s.makefile("rb")
                while not self.stop.is_set():
                    ln = f.readline()
                    if not ln:
                        break
                    ln = ln.decode("utf-8", "replace").strip()
                    if ln and '"kind": "ping"' not in ln:
                        self.broadcast(ln)
                s.close()
            except OSError:
                pass
            self.stop.wait(3)

    def _status_tick(self):
        while not self.stop.is_set():
            try:
                st = self.client.ask("status")
                self.last_status = st
                self.broadcast(json.dumps({"kind": "status", **st}))
            except BridgeDown:
                self.broadcast(json.dumps({"kind": "status", "bridge": "unreachable"}))
            self.stop.wait(STATUS_TICK)


# ---- actions, from the catalogue: the screen, the API and the MCP all come through here -------
def api_action_routes():
    return [a["id"] for a in C.ACTIONS]


def run_action(web, aid, args, who):
    """Validate against the catalogue, run, audit. Returns (http code, result dict)."""
    action = C.by_id(aid)
    if not action:
        return 404, {"error": f"no action {aid}"}
    known = None
    if any(i["type"] in ("node", "node_or_all") for i in action["inputs"]):
        try:
            known = {n.get("id") for n in web.client.ask("nodes").get("nodes", [])}
        except BridgeDown:
            known = None
    clean, err = C.validate(action, args, known)
    if err:
        K.audit(web.etc_dir, who=who, event="refused", action=aid, error=err)
        return 400, {"error": err}
    if action["op"] == "web:messages":
        return 200, {"messages": list(web.messages)}
    if action["op"] == "web:update_staged":
        return 200, {"staged": U.staged(web.state_dir, arch=web.arch, running=__version__)}
    if action["op"] == "web:update_rollback":
        out = U.rollback(web.state_dir, str(clean.get("version") or ""), running=__version__,
                         mode=web.update_mode(), arch=web.arch, start_unit=web.start_unit)
        if out.get("error"):
            K.audit(web.etc_dir, who=who, event="refused", action=aid, error=out["error"])
            return 400, out
        K.audit(web.etc_dir, who=who, event="ran", action=aid, result=out.get("version"))
        return 200, out
    if action["op"] == "web:map_sources":
        t = tile_sources(web.config, web.etc_dir)
        return 200, {"default": t["default"], "sources": [{k: v for k, v in x.items() if k != "url"} | {"url": x["url"]} for x in t["sources"]],
                     "on_disk": disk_map_sources(tilesets_dir(web.config)), "added": saved_map_sources(web.etc_dir), "dir": tilesets_dir(web.config)}
    if action["op"] == "web:map_source_add":
        rec, err = map_source_add(web.etc_dir, name=clean.get("name", ""), xml=clean.get("xml", ""), url=clean.get("url", ""),
                                  minzoom=clean.get("minzoom"), maxzoom=clean.get("maxzoom"))
        if err:
            K.audit(web.etc_dir, who=who, event="refused", action=aid, error=err)
            return 400, {"error": err}
        K.audit(web.etc_dir, who=who, event="ran", action=aid, result=rec["id"])
        return 200, {"written": {"id": rec["id"], "name": rec["name"]}, "confirmed": True}
    if action["op"] == "web:map_source_remove":
        rec, err = map_source_remove(web.etc_dir, str(clean.get("id") or ""))
        if err:
            K.audit(web.etc_dir, who=who, event="refused", action=aid, error=err)
            return 400, {"error": err}
        K.audit(web.etc_dir, who=who, event="ran", action=aid, result=rec["removed"])
        return 200, {"removed": rec["removed"], "confirmed": True}
    try:
        slow = action["risk"] in ("change", "unreachable") or aid.startswith("bench_")
        res = web.client.ask(action["op"], timeout=(FLASH_TIMEOUT_S if action["risk"] == "flash" else WRITE_TIMEOUT_S) if slow else 8, **clean)
    except BridgeDown as e:
        K.audit(web.etc_dir, who=who, event="failed", action=aid, error=str(e))
        return 503, {"error": f"could not ask the radio: the bridge is not answering on its socket ({e})"}
    if aid == "channels":
        res = {k: v for k, v in res.items() if k != "url"}
    if aid in ("nodes", "links"):
        rows, db_rows, heard, db = nodes_tables(res.get("nodes", []), res.get("routes"))
        res = dict(res, rows_html=rows, db_rows_html=db_rows, heard=heard, db=db)
    if action["risk"] != "read":
        K.audit(web.etc_dir, who=who, event="run", action=aid, arguments=clean, outcome="error" if "error" in res else "ok")
    code = 400 if "error" in res and action["risk"] != "read" else 200
    return code, res


def mcp_tools(autonomy):
    tools = []
    for a in C.visible(autonomy):
        tools.append({"name": a["id"], "description": a["description"] + (f" Risk: {a['risk']}." if a["risk"] != "read" else ""),
                      "inputSchema": C.tool_schema(a)})
    tools.append({"name": "mesh_context", "description": "The operator's standing brief for connected agents: what this mesh is for, its region and channel policy, standing orders. Read this first in a new session.",
                  "inputSchema": {"type": "object", "properties": {}}})
    if C.rank(autonomy) >= C.rank("propose"):
        tools.append({"name": "propose", "description": "Queue any catalogue action for a person to confirm on the Activity page, with your rationale. Validated exactly as a direct call is. Use it for anything above your autonomy, and for anything you are not sure the operator wants.",
                      "inputSchema": {"type": "object", "properties": {"action": {"type": "string"}, "arguments": {"type": "object"}, "rationale": {"type": "string"}},
                                      "required": ["action", "rationale"]}})
    return tools


def mcp_call(web, conn, name, args):
    """One tool call for a connection. Returns an MCP result object."""
    def text(obj, is_error=False):
        body = obj if isinstance(obj, str) else json.dumps(obj, indent=1, default=str)
        r = {"content": [{"type": "text", "text": body}]}
        if is_error:
            r["isError"] = True
        return r
    who = conn["name"]
    if name == "mesh_context":
        try:
            return text(open(os.path.join(web.etc_dir, "context.md")).read())
        except OSError:
            return text("No standing brief has been written yet. Ask the operator to fill in Settings > Standing brief; offer to draft one from what status, nodes and channels show.")
    if name == "propose":
        if C.rank(conn["autonomy"]) < C.rank("propose"):
            return text(f"propose needs propose autonomy; this connection ({who}) is {conn['autonomy']}", True)
        action = C.by_id(str(args.get("action", "")))
        if not action:
            return text(f"no catalogue action {args.get('action')!r}", True)
        clean, err = C.validate(action, args.get("arguments") or {}, None)
        if err:
            K.audit(web.etc_dir, who=who, event="refused", action=action["id"], error=err)
            return text({"error": err}, True)
        rec = K.propose(web.etc_dir, who, action["id"], clean, args.get("rationale", ""))
        return text({"proposal": rec["id"], "action": action["id"], "arguments": clean, "status": "queued for a person on the Activity page"})
    action = C.by_id(name)
    if not action:
        return text(f"no tool {name}", True)
    if C.rank(conn["autonomy"]) < C.rank(action["floor"]):
        K.audit(web.etc_dir, who=who, event="refused", action=name, error=f"needs {action['floor']}, connection is {conn['autonomy']}")
        return text(f"{name} needs {action['floor']} autonomy; this connection ({who}) is {conn['autonomy']}. Use propose to queue it for a person.", True)
    code, res = run_action(web, name, args, who)
    return text(res, code >= 400)


# ---- the QR, as a PNG, with nothing but the standard library around the matrix ----------------
def qr_png(url, scale=6, quiet=4):
    import pyqrcode  # in the release's wheel set (the Meshtastic library depends on it)
    code = pyqrcode.create(url, error="L").code
    n = len(code)
    size = (n + 2 * quiet) * scale
    rows = []
    for y in range(size):
        my = y // scale - quiet
        row = bytearray(b"\xff" * size)
        if 0 <= my < n:
            for x in range(size):
                mx = x // scale - quiet
                if 0 <= mx < n and code[my][mx]:
                    row[x] = 0
        rows.append(b"\x00" + bytes(row))

    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xffffffff)
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 0, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(b"".join(rows), 9)) + chunk(b"IEND", b"")


# ---- the pages ------------------------------------------------------------------------------------
# Spec 007: one token block, a dark theme on the same tokens, the state strip on every page, and
# nothing that reloads under the operator's finger.
CSS = """
:root{--surface:#F7F6EB;--surface-raised:#FFFFFF;--surface-sunken:#EDEBDD;--ink:#1C2418;--ink-muted:#4F5A4B;--ink-muted-strong:#3B4538;--line:#D2C78D;--line-strong:#B5B171;--accent:#113308;--accent-ink:#F7F6EB;--gold:#B5B171;--ok:#2E6B30;--warn:#8A5300;--bad:#9E2A22;--live:#D2C78D;--tap:32px;--s1:4px;--s2:8px;--s3:12px;--s4:16px;--s6:24px;--r:8px;--mono:"Roboto Mono",ui-monospace,Menlo,Consolas,monospace}
[data-theme=dark]{--surface:#0F1A0C;--surface-raised:#182416;--surface-sunken:#0B140A;--ink:#EEF0E6;--ink-muted:#B9C0B2;--ink-muted-strong:#CBD2C4;--line:#2E3F2A;--line-strong:#586F7C;--accent:#1F4A16;--accent-ink:#F7F6EB;--gold:#D2C78D;--ok:#7FC982;--warn:#F0B35A;--bad:#F08C84;--live:#D2C78D}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){--surface:#0F1A0C;--surface-raised:#182416;--surface-sunken:#0B140A;--ink:#EEF0E6;--ink-muted:#B9C0B2;--ink-muted-strong:#CBD2C4;--line:#2E3F2A;--line-strong:#586F7C;--accent:#1F4A16;--accent-ink:#F7F6EB;--gold:#D2C78D;--ok:#7FC982;--warn:#F0B35A;--bad:#F08C84;--live:#D2C78D}}
*{box-sizing:border-box}body{margin:0;background:var(--surface);color:var(--ink);font:14px/1.45 Manrope,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
input,select,textarea,button{font:inherit}
header{background:var(--accent);color:var(--accent-ink);padding:0 var(--s4);display:flex;align-items:center;gap:var(--s4);min-height:var(--tap)}
header .brand{font-weight:700;letter-spacing:.02em;white-space:nowrap}header .brand small{font-weight:400;opacity:.8;margin-left:var(--s2)}
nav{display:flex;flex-wrap:nowrap;gap:var(--s1)}nav a{color:var(--accent-ink);text-decoration:none;opacity:.85;padding:0 var(--s3);min-height:var(--tap);display:inline-flex;align-items:center;border-bottom:3px solid transparent;white-space:nowrap}nav a.on{opacity:1;border-bottom-color:var(--gold)}nav a:hover{opacity:1}
details.more{position:relative;margin-left:auto}details.more summary{list-style:none;cursor:pointer;min-height:var(--tap);display:inline-flex;align-items:center;gap:var(--s2);padding:0 var(--s3);color:var(--accent-ink)}details.more summary::-webkit-details-marker{display:none}
details.more nav{position:absolute;right:0;top:100%;background:var(--surface-raised);border:1px solid var(--line);border-radius:var(--r);flex-direction:column;z-index:6;min-width:220px;box-shadow:0 8px 24px rgba(0,0,0,.18)}details.more nav a{color:var(--ink);border-bottom:0;padding:0 var(--s4);opacity:1}details.more nav a.on{background:var(--surface-sunken)}
button.theme{background:transparent;color:var(--accent-ink);border:1px solid var(--live);min-width:var(--tap);padding:0 var(--s2)}
.state{display:flex;flex-wrap:wrap;gap:var(--s2) var(--s4);align-items:center;background:var(--surface-raised);border-bottom:1px solid var(--line);padding:var(--s1) var(--s4);font-size:.85rem}
.state .body{display:contents}.lamp{display:inline-block;width:12px;height:12px;border-radius:50%;background:var(--ink-muted);margin-right:var(--s2);vertical-align:-1px}.lamp--ok{background:var(--ok)}.lamp--warn{background:var(--warn)}.lamp--bad{background:var(--bad)}
.state .word{font-weight:600}.state .live{margin-left:auto;color:var(--ink-muted)}.state .live b{color:var(--ok)}.state .live.stale b{color:var(--warn)}.state .live.down b{color:var(--bad)}
main{padding:var(--s3) var(--s4);max-width:1200px;margin:0 auto}h1{font-size:1.2rem;margin:var(--s1) 0 var(--s3);color:var(--accent)}[data-theme=dark] h1{color:var(--gold)}h2{font-size:.95rem;color:var(--ink-muted-strong);margin:var(--s4) 0 var(--s2)}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:var(--s3)}.card{background:var(--surface-raised);border:1px solid var(--line);border-radius:var(--r);padding:var(--s2) var(--s3)}
.card .k{font-size:.72rem;color:var(--ink-muted);text-transform:uppercase;letter-spacing:.04em}.card .v{font-size:1rem;font-weight:600;overflow-wrap:anywhere}
.ok{color:var(--ok)}.warn{color:var(--warn)}.bad{color:var(--bad)}.pill{display:inline-block;padding:0 var(--s2);border-radius:999px;font-size:.75rem;background:var(--surface-sunken);color:var(--ink-muted-strong);border:1px solid var(--line);white-space:nowrap}
.tablewrap{overflow-x:auto}table{width:100%;border-collapse:collapse;background:var(--surface-raised);border:1px solid var(--line);border-radius:var(--r)}th,td{padding:var(--s1) var(--s2);text-align:left;border-bottom:1px solid var(--line);font-variant-numeric:tabular-nums;vertical-align:top}
th{background:var(--surface-sunken);color:var(--ink-muted-strong);font-size:.72rem;text-transform:uppercase;letter-spacing:.04em}tr.db td{color:var(--ink-muted-strong)}
.meta{color:var(--ink-muted);font-size:.85rem}.res{margin-top:var(--s1)}.res:empty{display:none;margin:0}.sub{font-size:.8rem;color:var(--ink-muted)}
pre.log{background:var(--surface-sunken);color:var(--ink);border:1px solid var(--line);padding:var(--s3);border-radius:var(--r);font:14px/1.45 var(--mono);max-height:70vh;overflow:auto;white-space:pre-wrap;margin:0}
pre.log .ln{display:block}pre.log .ln--radio{color:var(--ink-muted-strong)}pre.log[data-show=bridge] .ln--radio{display:none}pre.log[data-show=radio] .ln--bridge{display:none}pre.log .ln--WARNING,pre.log .ln--ERROR{color:var(--bad)}pre.log[data-level=warn] .ln--INFO,pre.log[data-level=warn] .ln--DEBUG{display:none}
.controls{display:flex;gap:var(--s3);flex-wrap:wrap;align-items:center;margin-bottom:var(--s3)}.controls label{display:inline-flex;align-items:center;gap:var(--s2)}.controls select{width:auto;margin:0}
.sig{display:inline-flex;align-items:center;gap:var(--s2);white-space:nowrap}td time,td .pill{white-space:nowrap}.sig__bars{width:22px;height:16px;flex:none}.sig__bars rect{fill:var(--line)}.sig--4 rect,.sig--3 .b1,.sig--3 .b2,.sig--3 .b3{fill:var(--ok)}.sig--2 .b1,.sig--2 .b2{fill:var(--warn)}.sig--1 .b1{fill:var(--bad)}
.batt--low{color:var(--bad);font-weight:600}
button{background:var(--accent);color:var(--accent-ink);border:1px solid transparent;border-radius:6px;padding:0 var(--s3);min-height:var(--tap);font-size:.9rem;cursor:pointer}button:hover{filter:brightness(1.15)}button.line{background:transparent;color:var(--ink);border-color:var(--line-strong)}button.danger{background:var(--bad)}button.quiet{background:var(--surface-sunken);color:var(--ink);border-color:var(--line)}
button:disabled{opacity:.5;cursor:not-allowed}.row-actions{display:flex;gap:var(--s1);flex-wrap:wrap;align-items:center}button.icon,details.fold.ctl.icon summary{width:28px;min-height:28px;height:28px;padding:0;display:inline-flex;align-items:center;justify-content:center}button.icon svg,details.fold.ctl.icon summary svg{width:16px;height:16px;display:block}details.fold.ctl.icon summary::after,details.fold.ctl.icon[open] summary::after{content:none}details.fold.ctl.icon[open]{flex-basis:100%}details.fold.ctl.icon[open] summary{margin-bottom:var(--s1)}.visually-hidden{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}.mm-centre button{width:30px;height:30px;padding:0;border:0;border-radius:2px;background:var(--surface-raised);color:var(--ink);display:flex;align-items:center;justify-content:center;cursor:pointer}.mm-centre button:hover{background:var(--surface-sunken);filter:none}.mm-centre button:disabled{color:var(--ink-muted);cursor:not-allowed;opacity:1}.mm-centre button svg{width:18px;height:18px}a.plain{color:inherit;text-decoration:none;border-bottom:1px dotted var(--line-strong)}a.plain:hover{border-bottom-style:solid}.chart{width:100%;max-width:600px;height:auto;display:block;background:var(--surface-raised);border:1px solid var(--line);border-radius:var(--r)}.chart polyline{fill:none;stroke:var(--accent);stroke-width:2}[data-theme=dark] .chart polyline{stroke:var(--gold)}.chart line{stroke-dasharray:3 4;stroke-width:1}.chart line.warn{stroke:var(--warn)}.chart line.bad{stroke:var(--bad)}.chart text{fill:var(--ink-muted);font-size:10px}.mm-readout{background:var(--surface-raised);color:var(--ink);border:1px solid var(--line);border-radius:4px;padding:2px 8px;font-size:.8rem;font-variant-numeric:tabular-nums;white-space:nowrap}.mm-readout:empty{display:none}.leaflet-tooltip.mm-grid{background:var(--surface-raised);color:var(--ink-muted);border:1px solid var(--line);box-shadow:none;padding:0 4px;font-size:10px;font-variant-numeric:tabular-nums}.leaflet-tooltip.mm-grid::before{display:none}.tip{position:fixed;z-index:50;display:none;max-width:280px;padding:var(--s1) var(--s2);background:var(--ink);color:var(--surface);border-radius:6px;font-size:.8rem;line-height:1.35;box-shadow:0 4px 14px rgba(0,0,0,.25);pointer-events:none}.tip b{display:block;font-weight:600}.tip div{opacity:.85;margin-top:2px}
input[type=text],input[type=number],input[type=password],select,textarea{width:100%;padding:var(--s1) var(--s2);min-height:var(--tap);font-size:.9rem;border:1px solid var(--line-strong);border-radius:6px;background:var(--surface-raised);color:var(--ink);margin:var(--s1) 0 var(--s3)}
label{display:block}label.check{display:flex;gap:var(--s2);align-items:flex-start;min-height:var(--tap);margin:var(--s2) 0}label.check input{width:18px;height:18px;margin-top:2px;flex:none}
form.card{max-width:560px}form.login{max-width:360px;margin:3rem auto}form.card.danger{border-color:var(--bad)}form.card.danger h2{color:var(--bad)}
details.fold{margin-top:var(--s4)}details.fold summary{cursor:pointer;min-height:var(--tap);display:flex;align-items:center;gap:var(--s2);color:var(--ink-muted-strong)}details.fold.ctl summary{display:inline-flex;padding:0 var(--s3);font-size:.9rem;border:1px solid var(--line-strong);border-radius:6px;color:var(--ink);list-style:none}details.fold.ctl summary::-webkit-details-marker{display:none}details.fold.ctl summary::after{content:' ▸';margin-left:var(--s1)}details.fold.ctl[open] summary::after{content:' ▾'}
.sheet{position:fixed;inset:0;background:var(--surface);display:flex;flex-direction:column;align-items:center;justify-content:center;gap:var(--s4);z-index:10;padding:var(--s4);text-align:center}.sheet[hidden]{display:none}.sheet img{max-width:min(90vw,70vh);height:auto}
.newlines{position:sticky;bottom:var(--s2);float:right}code{font-family:var(--mono);font-size:.9em}
.pill.upd{background:var(--gold);color:var(--accent);border-color:var(--gold);text-decoration:none;margin-left:var(--s2)}.proposal.done{opacity:.6}.regform{display:grid;grid-template-columns:1fr 1fr auto;gap:var(--s1);align-items:start;min-width:220px}.regform input{margin:0}.regform .res{grid-column:1/-1}.manage{display:grid;gap:var(--s3);min-width:280px;margin-top:var(--s2)}.manage form{background:var(--surface-sunken);border:1px solid var(--line);border-radius:var(--r);padding:var(--s3)}.manage form.danger{border-color:var(--bad)}footer{padding:var(--s4);color:var(--ink-muted);font-size:.85rem;text-align:center}
.map{width:100%;max-height:72vh;display:block;background:var(--surface-raised);border:1px solid var(--line);border-radius:var(--r)}
.map .ring{fill:none;stroke:var(--line);stroke-dasharray:2 4}.map text{fill:var(--ink-muted);font-size:11px}
.map .node{fill:var(--surface-raised);stroke:var(--accent);stroke-width:2}.map .node.nopos{stroke-dasharray:3 3}.map .own{fill:var(--accent);stroke:var(--gold);stroke-width:2}
.map .name{fill:var(--ink);font-size:12px;font-weight:600}.map .link{stroke-width:3}.map .band-4,.map .band-3{stroke:var(--ok)}.map .band-2{stroke:var(--warn)}.map .band-1,.map .band-0{stroke:var(--bad)}
.map .relayed{stroke:var(--ink-muted);stroke-width:2;stroke-dasharray:6 6}.map .route{stroke:var(--gold);stroke-width:5;opacity:.7;fill:none}.map .lbl{fill:var(--ink);font-size:11px;paint-order:stroke;stroke:var(--surface-raised);stroke-width:3px}
.geo{height:62vh;min-height:320px;border:1px solid var(--line);border-radius:var(--r);background:var(--surface-sunken)}.views button.on{background:var(--accent);color:var(--accent-ink);border-color:var(--accent)}
.leaflet-tooltip.mm-node,.leaflet-tooltip.mm-link,.leaflet-tooltip.mm-ring{background:var(--surface-raised);color:var(--ink);border:1px solid var(--line);box-shadow:none;font-weight:600;padding:0 var(--s1)}.leaflet-tooltip.mm-link{font-size:11px}.leaflet-tooltip.mm-ring{font-weight:400;color:var(--ink-muted)}.leaflet-tooltip.mm-node::before,.leaflet-tooltip.mm-link::before,.leaflet-tooltip.mm-ring::before{display:none}
.linkbar .dir{display:flex;flex-wrap:wrap;align-items:center;gap:var(--s1);margin:var(--s1) 0}.linkbar .hop{display:inline-block;padding:0 var(--s2);border-left:6px solid var(--line);background:var(--surface-sunken);border-radius:0 4px 4px 0;white-space:nowrap}
.linkbar .hop.band-4,.linkbar .hop.band-3{border-color:var(--ok)}.linkbar .hop.band-2{border-color:var(--warn)}.linkbar .hop.band-1,.linkbar .hop.band-0{border-color:var(--bad)}.linkbar .hop.origin{border-color:var(--accent)}
.spark{width:72px;height:18px;vertical-align:middle}.spark polyline{fill:none;stroke:var(--accent);stroke-width:1.5}.spark line{stroke:var(--line)}.sparkfig{font-size:.8rem;color:var(--ink-muted);white-space:nowrap}
@media (max-width:700px){header nav.primary{position:fixed;bottom:0;left:0;right:0;background:var(--accent);justify-content:space-around;z-index:5;border-top:1px solid var(--live)}main{padding-bottom:calc(var(--tap) + var(--s6))}.hide-narrow{display:none}.state .live{margin-left:0}}
"""
NAV_PRIMARY = [("/", "Mesh"), ("/messages", "Messages"), ("/channels", "Channels"), ("/radio", "Radio")]
NAV_MORE = [("/map", "Map"), ("/nodes", "Nodes"), ("/health", "Health"), ("/register", "Register"), ("/bench", "Bench"), ("/log", "Log"), ("/activity", "Activity"), ("/connections", "Connections"), ("/settings", "Settings"), ("/help", "Help"), ("/about", "About")]
NAV = NAV_PRIMARY + NAV_MORE
e = html.escape
WRITE_TIMEOUT_S = 45   # above the bridge's read-back window, so a slow radio is never called a failure
FLASH_TIMEOUT_S = 420  # a flash: export, bootloader, copy, the device coming back, the version read


def _utc_secs(iso):
    import calendar
    return calendar.timegm(time.strptime(str(iso)[:19], "%Y-%m-%dT%H:%M:%S"))


def age(iso):
    """'40 s ago', '12 min ago', '1 h 12 min ago', '3 d ago'; the ISO stamp itself if unreadable."""
    try:
        s = max(0, int(time.time() - _utc_secs(iso)))
    except (ValueError, TypeError):
        return str(iso or "")
    if s < 60:
        return f"{s} s ago"
    m = s // 60
    if m < 60:
        return f"{m} min ago"
    h = m // 60
    if h < 48:
        return f"{h} h {m % 60} min ago"
    return f"{h // 24} d ago"


def hhmm(iso=None):
    """Local clock time for an ISO stamp, or for now."""
    try:
        t = _utc_secs(iso) if iso else time.time()
    except (ValueError, TypeError):
        return str(iso or "")
    return time.strftime("%H:%M", time.localtime(t))


LIVE_JS = r"""<script>
(function(){
  var root=document.documentElement;
  try{var t=localStorage.getItem('mm-theme'); if(t){root.dataset.theme=t;}}catch(x){}
  var tb=document.querySelector('[data-theme-toggle]');
  if(tb){tb.addEventListener('click',function(){var cur=root.dataset.theme||(matchMedia('(prefers-color-scheme: dark)').matches?'dark':'light');var nx=cur==='dark'?'light':'dark';root.dataset.theme=nx;try{localStorage.setItem('mm-theme',nx);}catch(x){}});}
  window.mmNow=function(){var d=new Date();return ('0'+d.getHours()).slice(-2)+':'+('0'+d.getMinutes()).slice(-2);};
  window.mmHm=function(iso){var d=new Date(iso||'');return isNaN(d.getTime())?(iso||''):('0'+d.getHours()).slice(-2)+':'+('0'+d.getMinutes()).slice(-2);};
  window.mmAge=function(iso){var t=Date.parse(iso||'');if(isNaN(t))return '';var s=Math.max(0,Math.round((Date.now()-t)/1000));if(s<60)return s+' s ago';var m=Math.floor(s/60);if(m<60)return m+' min ago';var h=Math.floor(m/60);if(h<48)return h+' h '+(m%60)+' min ago';return Math.floor(h/24)+' d ago';};
  function ages(){document.querySelectorAll('time[data-age]').forEach(function(t){var a=window.mmAge(t.getAttribute('datetime'));if(a){t.textContent=a;}});}
  setInterval(ages,15000);
  var lf=document.getElementById('live'),last=Date.now(),down=false;
  function tick(){if(!lf)return;var s=Math.round((Date.now()-last)/1000);lf.className='live'+(down?' down':(s>60?' stale':''));lf.innerHTML=down?'feed <b>down</b>':('live <b>'+(s<2?'now':s+' s ago')+'</b>');}
  setInterval(tick,1000);tick();
  var pend={};
  window.mmFrag=function(name,id,after){if(pend[name])return;pend[name]=true;setTimeout(function(){pend[name]=false;
    fetch('/fragment/'+name).then(function(r){return r.text();}).then(function(h){var el=document.getElementById(id);if(el){el.innerHTML=h;}ages();if(after){after();}}).catch(function(){});},400);};
  var es=new EventSource('/events');
  es.addEventListener('mesh',function(ev){var d;try{d=JSON.parse(ev.data);}catch(x){return;}last=Date.now();down=false;tick();
    if(d.kind==='status'){window.mmFrag('state','state-body');}
    if(window.onMesh){window.onMesh(d);}});
  es.onerror=function(){down=true;tick();};
})();
</script>"""


def state_strip(st):
    st = st or {}
    if "version" not in st:
        lamp, word = "bad", "Bridge not answering"
    elif st.get("bootloader"):
        lamp, word = "bad", "Radio in bootloader"
    elif not st.get("radio_present"):
        lamp, word = "bad", "Radio missing"
    elif not st.get("connected"):
        lamp, word = "warn", "Radio not connected"
    elif st.get("observe"):
        lamp, word = "ok", "Observing"
    else:
        lamp, word = "ok", "Bridging to TAK"
    heard = int(st.get("nodes_seen") or 0)
    db = st.get("nodes_db")
    counts = f"{heard} heard here" + (f", {int(db)} in the radio's database" if db is not None else "")
    parts = [f"<span class='word'><i class='lamp lamp--{lamp}'></i>{e(word)}</span>", f"<span>{e(counts)}</span>"]
    if st.get("region") or st.get("modem_preset"):
        parts.append(f"<span>{e(st.get('region') or '?')} · {e(st.get('modem_preset') or '?')}</span>")
    if st.get("primary_channel"):
        parts.append(f"<span>primary <b>{e(st['primary_channel'])}</b></span>")
    g = st.get("gps")
    if isinstance(g, dict) and g:
        # Spec 031: what the last read of the receiver established. Where the box thinks it is
        # decides where every node is drawn relative to it, so a receiver with no fix should not
        # look like a good one. A box with no receiver says nothing rather than showing a lamp
        # that can never go green.
        seen, used, via = g.get("seen"), g.get("used"), str(g.get("via") or "")
        when = str(g.get("checked") or "")
        if not g.get("reachable"):
            glamp, gword, sats = "warn", "GPS not answering", ""
        elif not g.get("fix"):
            glamp, gword = "warn", "GPS no fix"
            sats = f" · {int(seen)} sats seen" if isinstance(seen, int) else ""
        else:
            glamp, gword = "ok", "GPS fix"
            sats = f" · {int(used)} sats used" if isinstance(used, int) else ""
        tip = "  ".join(x for x in (
            f"read from {via}" if via else "",
            f"last read {when}" if when else "",
            f"{int(seen)} seen" if isinstance(seen, int) else "",
            f"{int(used)} used" if isinstance(used, int) else "") if x)
        parts.append(f"<span class='word' data-tip='{e(tip)}' tabindex='0'>"
                     f"<i class='lamp lamp--{glamp}'></i>{e(gword + sats)}</span>")
    if st.get("alerts_open"):
        n = int(st["alerts_open"])
        parts.append(f"<a href='/health#alerts' class='pill' style='background:var(--bad);color:#fff;border-color:var(--bad)'>{n} alert{'s' if n != 1 else ''}</a>")
    return "".join(parts)


def page(title, body, active="", own="", st=None, pending=0, head="", update=None):
    prim = "".join(f"<a href='{p}' class='{'on' if p == active else ''}'>{e(t)}</a>" for p, t in NAV_PRIMARY)
    more = "".join(f"<a href='{p}' class='{'on' if p == active else ''}'>{e(t)}{(' <span class=pill>' + str(pending) + '</span>') if (p == '/activity' and pending) else ''}</a>"
                   for p, t in NAV_MORE)
    more_on = any(p == active for p, _ in NAV_MORE)
    return f"""<!doctype html><html lang='en-GB'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
<title>{e(title)} · Mesh Manager</title><style>{CSS}</style>{head}</head><body data-own='{e(own)}'>
<header><span class='brand'>Mesh Manager<small>{e(__version__)}</small></span><nav class='primary'>{prim}</nav>
<details class='more'><summary>{'<b>More</b>' if more_on else 'More'}{(' <span class=pill>' + str(pending) + '</span>') if pending else ''}</summary><nav>{more}</nav></details>
{("<a class='pill upd' href='/about'>update available: " + e(str(update)) + "</a>") if update else ""}<button type='button' class='theme' data-theme-toggle title='Light or dark'>◐</button></header>
<div class='state' role='status'><span class='body' id='state-body'>{state_strip(st)}</span><span id='live' class='live'>live <b>…</b></span></div>
<main><h1>{e(title)}</h1>{body}</main>
<footer>Mesh Manager by MilUX Ltd · GPL-3.0-or-later · the mesh as it is now, from the box that carries the radio</footer>
{LIVE_JS}{TIP_JS}</body></html>"""


TIP_JS = r"""<script>
(function(){
  // an instant tooltip for anything with data-tip (and data-tip-more): one fixed element placed by script, so no table wrapper clips it; on hover and on keyboard focus
  var tip=null,onEl=null;
  function show(el){var t=el.getAttribute('data-tip');if(!t)return;if(!tip){tip=document.createElement('div');tip.className='tip';tip.setAttribute('role','tooltip');document.body.appendChild(tip);}
    tip.innerHTML='';var b=document.createElement('b');b.textContent=t;tip.appendChild(b);var m=el.getAttribute('data-tip-more');if(m){var d=document.createElement('div');d.textContent=m;tip.appendChild(d);}
    tip.style.display='block';var r=el.getBoundingClientRect();var w=tip.offsetWidth,h=tip.offsetHeight;var x=Math.min(Math.max(8,r.left+r.width/2-w/2),window.innerWidth-w-8);var y=r.top-h-8;if(y<8){y=r.bottom+8;}
    tip.style.left=x+'px';tip.style.top=y+'px';onEl=el;}
  function hide(){if(tip){tip.style.display='none';}onEl=null;}
  document.addEventListener('mouseover',function(ev){var el=ev.target.closest&&ev.target.closest('[data-tip]');if(el&&el!==onEl){show(el);}else if(!el&&onEl){hide();}});
  document.addEventListener('mouseout',function(ev){var el=ev.target.closest&&ev.target.closest('[data-tip]');if(el&&!(ev.relatedTarget&&el.contains(ev.relatedTarget))){hide();}});
  document.addEventListener('focusin',function(ev){var el=ev.target.closest&&ev.target.closest('[data-tip]');if(el){show(el);}});
  document.addEventListener('focusout',function(){hide();});
  document.addEventListener('click',function(ev){if(ev.target.closest&&ev.target.closest('[data-tip]')){hide();}});
  window.addEventListener('scroll',hide,true);
})();
</script>"""


def bare_page(title, body, head=""):
    """A page with nothing but its body: the map in a window of its own (Spec 019)."""
    return f"""<!doctype html><html lang='en-GB'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
<title>{e(title)} · Mesh Manager</title><style>{CSS}
body.bare{{margin:0}}body.bare .state{{display:none}}body.bare main{{padding:var(--s2);max-width:none}}body.bare .geo{{height:calc(100vh - 4.5rem);min-height:240px}}</style>{head}</head><body class='bare'>
<div class='state' role='status' hidden><span class='body' id='state-body'></span><span id='live' class='live'></span></div>
<main>{body}</main>{LIVE_JS}{TIP_JS}</body></html>"""


def card(k, v, cls=""):
    return f"<div class='card'><div class='k'>{e(k)}</div><div class='v {cls}'>{v}</div></div>"


def overview_cards(st):
    if not st or "version" not in st:
        return "<p class='bad'>The bridge is not answering on its socket. Is mesh-manager-bridge running? <code>journalctl -u mesh-manager-bridge</code></p>"
    radio = st.get("radio") or "(none)"
    present, boot, conn = st.get("radio_present"), st.get("bootloader"), st.get("connected")
    radio_txt = ("in bootloader mode" if boot else ("present" if present else "MISSING")) + (", connected" if conn else ", not connected")
    radio_cls = "bad" if (boot or not present) else ("ok" if conn else "warn")
    own = st.get("own") or {}
    mode = "observing: listening only, forwarding nothing" if st.get("observe") else "bridging to TAK"
    fwd = st.get("last_forwarded")
    act = st.get("last_activity")
    heard, db = int(st.get("nodes_seen") or 0), st.get("nodes_db")
    return "".join([
        card("Bridge", f"running · {e(mode)}", "ok"),
        card("Radio", f"{e(radio_txt)}<div class='meta'>{e(radio)}</div>", radio_cls),
        card("This radio", f"{e(own.get('name') or '?')} <span class='pill'>{e(own.get('id') or '')}</span>"),
        card("Region · preset", f"{e(st.get('region') or '?')} · {e(st.get('modem_preset') or '?')}"),
        card("Primary channel", e(st.get("primary_channel") or "?")),
        card("Nodes", f"{heard} heard here" + (f"<div class='meta'>{int(db)} in the radio's database</div>" if db is not None else "")),
        card("Mesh health", (f"<a href='/health'>{float(st['chutil']):.1f}% <span class='pill'>{e(st.get('verdict') or '')}</span></a>" if st.get("chutil") is not None else "<a href='/health'>no reading yet</a>") + "<div class='meta'>channel utilisation</div>",
             {"quiet": "ok", "normal": "ok", "busy": "warn", "saturated": "bad"}.get(st.get("verdict") or "", "")),
        card("Last packet that would have gone to TAK" if st.get("observe") else "Last packet forwarded to TAK",
             (f"<time datetime='{e(fwd)}' data-age>{e(age(fwd))}</time>" if fwd else "none yet"), "ok" if fwd else "warn"),
        card("Last activity on the serial loop", (f"<time datetime='{e(act)}' data-age>{e(age(act))}</time>" if act else "none yet")),
        card("Watchdog", e(st.get("watchdog") or "?"), "ok" if st.get("watchdog") == "pinging" else "warn"),
        card("Up for", f"{int(st.get('uptime') or 0) // 60} min"),
    ])


def position_words(own):
    words = _position_words(own)
    if own and own.get('lat') is not None and own.get('lon') is not None:
        m = MG.mgrs(own['lat'], own['lon'], 4)
        if m:
            words = f"{words} · {m}"
    return words


def _position_words(own):
    """Which source placed the box, in the operator's words."""
    src = (own or {}).get("position_source")
    if src == "config":
        return "the position set at install"
    if src == "gps":
        sats = own.get("sats")
        return f"the box's own GPS receiver ({sats} satellites, fix at {hhmm(own.get('time'))})" if sats else f"the box's own GPS receiver (fix at {hhmm(own.get('time'))})"
    if src == "radio_gps":
        return f"the gateway radio's own GPS (fix at {hhmm(own.get('time'))})"
    if src == "devices":
        g = own.get("gps") or {}
        if g.get("reachable") and not g.get("fix"):
            sky = f" ({g.get('seen')} satellites seen, {g.get('used') or 0} used)" if g.get("seen") is not None else ""
            return f"GPS receiver connected, no fix yet{sky}: placed among the devices it hears (the median of {own.get('count') or '?'} positions), an estimate"
        return f"placed among the devices it hears (the median of {own.get('count') or '?'} positions): an estimate, not a fix; plug in a GPS receiver or set --map-lat and --map-lon at install"
    if src == "radio":
        return "the radio's own stored fix (no receiver, no declaration, nothing heard with a fix; a radio without GPS may carry a stale one)"
    return "no position: nodes are placed by hops"


def map_body(L, tiles=None, disk=None, added=None, folder=""):
    js = """<script>window.onMesh=function(d){if(d.kind==='packet'||d.kind==='route'||d.kind==='status'||d.kind==='forwarded'){window.mmFrag('map','map-box');if(window.mmOverlay){window.mmOverlay();}}if(d.kind==='survey'&&window.mmSurvey){window.mmSurvey(d);}if((d.kind==='position'||(d.kind==='survey'&&d.state==='asked'))&&window.mmCoverTick){setTimeout(window.mmCoverTick,1500);}};</script>"""
    own = L.get("own") or {}
    how = position_words(own)
    return (f"<p class='meta'>This box at the centre, every node heard since the bridge started about it; a solid link is coloured by the SNR of the last packet that came straight from that node, a dashed one has only ever come through a relay, a database-only node is not drawn. Box position: {e(how)}.</p>"
            f"{mesh_views(L, tiles or tile_sources({}), 800)}{survey_form(L)}{map_sources_form(tiles, disk, added, folder)}{js}{WRITE_JS}")


def map_sources_form(t, disk=None, added=None, folder=""):
    """Spec 029: what the map can draw, and how to add a TAK source."""
    a, rm = _act("map_source_add"), _act("map_source_remove")
    rows = ""
    for src in (disk or []) + (added or []):
        if src.get("error"):
            rows += f"<tr><td><b>{e(src.get('name'))}</b><div class='sub'>{e(src.get('id'))}</div></td><td class='meta'>{e(src.get('where') or '')}</td><td class='bad'>{e(src['error'])}</td><td></td></tr>"
            continue
        zooms = " · ".join(x for x in ([f"zoom {src['minzoom']} to {src['maxzoom']}"] if src.get("minzoom") is not None and src.get("maxzoom") is not None else []) + ([src.get("tile_type")] if src.get("tile_type") else []))
        drop = (f"<form data-action='map_source_remove' data-risk='change' data-confirm=\"{e(rm.get('confirm') or '')}\" style='display:inline'><input type='hidden' name='id' value='{e(src['id'])}'><button class='quiet'>Remove</button><div class='res meta' role='status'></div></form>") if src.get("removable") else ""
        rows += f"<tr><td><b>{e(src.get('name'))}</b><div class='sub'>{e(src.get('id'))}</div></td><td class='meta'>{e(src.get('where') or '')}</td><td class='meta'>{e(zooms)}</td><td>{drop}</td></tr>"
    built = ", ".join(e(x["name"]) for x in (t or {}).get("sources", []) if not str(x.get("id", "")).startswith(("tak-", "own-")))
    return (f"<details class='fold' id='map-sources' style='margin-top:var(--s3)'><summary>Map sources</summary>"
            f"<p class='meta'>The layer control offers {built}. Add the imagery you already carry in TAK: drop an ATAK <code>&lt;customMapSource&gt;</code> XML into the box's map folder{(' (' + e(folder) + ')') if folder else ''} and it appears here, or paste one below. Tiles load in the viewer's browser; the box sends nothing.</p>"
            f"<div class='tablewrap'><table><thead><tr><th>Source</th><th>From</th><th>Detail</th><th></th></tr></thead><tbody>{rows or '<tr><td colspan=4 class=meta>Nothing beyond the built-in sources yet.</td></tr>'}</tbody></table></div>"
            f"<form data-action='map_source_add' class='card' data-risk='change' data-confirm=\"{e(a.get('confirm') or '')}\" style='max-width:760px;margin-top:var(--s3)'>"
            f"<h2 style='margin-top:0'>{e(a['title'])}</h2><p class='meta'>{e(a['description'])}</p>"
            "<label>Name<input type='text' name='name' maxlength='60' placeholder='e.g. Andover imagery'></label>"
            "<label>ATAK custom map source XML<textarea name='xml' rows='4' placeholder='&lt;customMapSource&gt;…&lt;/customMapSource&gt;'></textarea></label>"
            "<label>or a tile URL template<input type='text' name='url' maxlength='500' placeholder='https://example/{z}/{x}/{y}.png'></label>"
            "<div class='regform' style='grid-template-columns:1fr 1fr'><label>Lowest zoom<input type='number' name='minzoom' min='0' max='22'></label><label>Highest zoom<input type='number' name='maxzoom' min='0' max='22'></label></div>"
            "<button class='line' style='margin-top:var(--s2)'>Add it</button><div class='res meta' role='status'></div></form></details>")


def survey_form(L):
    """Spec 022: ask one node for its position on an interval while someone walks it."""
    a = _act("survey_start")
    nodes = [n for n in (L.get("nodes") or []) if n.get("id") and n.get("heard_here", True)]
    opts = "".join(f"<option value='{e(n['id'])}'>{e(dname(n))} ({e(n['id'])})</option>" for n in nodes)
    return (f"<details class='fold' id='survey' style='margin-top:var(--s3)'><summary>Coverage survey</summary><p class='meta'>{e(a['description'])} Turn the coverage layer on above to watch it fill.</p>"
            f"<form data-action='survey_start' class='regform' style='grid-template-columns:2fr 1fr 1fr auto;max-width:720px'>"
            f"<select name='dest' aria-label='node'>{opts}</select>"
            "<input type='number' name='interval' value='15' min='5' max='120' aria-label='seconds between asks' title='seconds between asks'>"
            "<input type='number' name='minutes' value='10' min='1' max='120' aria-label='minutes' title='minutes to keep asking'>"
            "<button class='line'>Start</button><div class='res meta' role='status'></div></form>"
            "<form data-action='survey_stop' style='display:inline'><button class='quiet'>Stop</button><div class='res meta' role='status'></div></form>"
            "<div class='meta' id='survey-line'></div></details>"
            "<script>window.mmSurvey=function(d){var el=document.getElementById('survey-line');if(!el||!d)return;"
            "if(d.state==='started'){el.textContent='survey of '+d.dest+' started: every '+d.interval+' s for '+d.minutes+' min';}"
            "else if(d.state==='asked'){el.textContent='survey of '+d.dest+': asked '+d.asked+' time'+(d.asked===1?'':'s')+' · '+window.mmNow();}"
            "else if(d.state==='ended'){el.textContent='survey of '+d.dest+' ended: asked '+d.asked+', '+(d.answers===null||d.answers===undefined?'answers unknown':d.answers+' answer'+(d.answers===1?'':'s')+' in the history');}};</script>")


MAP_HEAD = "<link rel='stylesheet' href='/static/leaflet/leaflet.css'><script src='/static/leaflet/leaflet.js'></script>"


def overview_body(st, bind, auth_on=True, nodes=None, links=None, tiles=None):
    closed = (f"<h2>What is open</h2><p class='meta'>This screen answers on <b>{e(bind[0])}:{bind[1]}</b> and nothing else. "
              + ("" if auth_on else "<b>Sign-in is off</b>: anyone who can reach this address is the operator. ")
              + "The bridge binds no port of its own; it owns the radio and speaks to TAK Server over the multicast input. "
              "Everything else on this box is closed until the operator opens it.</p>")
    js = """<script>window.onMesh=function(d){if(d.kind==='status'||d.kind==='forwarded'||d.kind==='connection'){window.mmFrag('overview','overview-cards');}
if(d.kind==='packet'||d.kind==='forwarded'||d.kind==='status'||d.kind==='route'){if(window.mmNodes){window.mmNodes();}window.mmFrag('map','map-box');if(window.mmOverlay){window.mmOverlay();}}
if(d.kind==='route'&&window.mmRoute){window.mmRoute(d);}if(d.kind==='position'&&window.mmPosition){window.mmPosition(d);}if(d.kind==='telemetry'&&window.mmTelemetry){window.mmTelemetry(d);}};</script>"""
    L = links or {"own": {}, "nodes": nodes or [], "routes": {}}
    return (f"<h2 style='margin-top:0'>The mesh</h2>{mesh_views(L, tiles or tile_sources({}))}{nodes_body(L.get('nodes') or [], intro=False, routes=L.get('routes'))}"
            f"<h2>This box</h2><div class='cards' id='overview-cards'>{overview_cards(st)}</div>{closed}{js}")


# -- nodes
SIG_SVG = ("<svg class='sig__bars' viewBox='0 0 22 16' aria-hidden='true'><rect class='b1' x='0' y='12' width='4' height='4'/>"
           "<rect class='b2' x='6' y='8' width='4' height='8'/><rect class='b3' x='12' y='4' width='4' height='12'/><rect class='b4' x='18' y='0' width='4' height='16'/></svg>")


def sig(snr, hops):
    """Bars by SNR band with the figure beside them, never colour alone; a hop pill after."""
    hop = ""
    if hops is not None:
        hop = " <span class='pill'>" + ("direct" if int(hops) == 0 else f"{int(hops)} hop" + ("s" if int(hops) != 1 else "")) + "</span>"
    if snr is None:
        return "<span class='sig sig--0'><span class='sub'>no reading</span></span>" + hop
    snr = float(snr)
    bars = 4 if snr >= 10 else 3 if snr >= 5 else 2 if snr >= -7 else 1 if snr >= -12 else 0
    return f"<span class='sig sig--{bars}' title='SNR {snr:g} dB'>{SIG_SVG}<span>{snr:g} dB</span></span>{hop}"


STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
TILE_SOURCES = [
    {"id": "google-hybrid", "name": "Google hybrid", "url": "https://mt{s}.google.com/vt/lyrs=y&x={x}&y={y}&z={z}", "subdomains": "0123", "maxzoom": 20, "attribution": "Imagery and map data &copy; Google", "internet": True},
    {"id": "google-satellite", "name": "Google satellite", "url": "https://mt{s}.google.com/vt/lyrs=s&x={x}&y={y}&z={z}", "subdomains": "0123", "maxzoom": 20, "attribution": "Imagery &copy; Google", "internet": True},
    {"id": "google-roads", "name": "Google roads", "url": "https://mt{s}.google.com/vt/lyrs=m&x={x}&y={y}&z={z}", "subdomains": "0123", "maxzoom": 20, "attribution": "Map data &copy; Google", "internet": True},
    {"id": "google-terrain", "name": "Google terrain", "url": "https://mt{s}.google.com/vt/lyrs=p&x={x}&y={y}&z={z}", "subdomains": "0123", "maxzoom": 20, "attribution": "Map data &copy; Google", "internet": True},
    {"id": "osm", "name": "OpenStreetMap", "url": "https://tile.openstreetmap.org/{z}/{x}/{y}.png", "maxzoom": 19, "attribution": "&copy; OpenStreetMap contributors", "internet": True},
]
SET_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
TILE_TYPES = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp"}


def tilesets_dir(config):
    d = str((config or {}).get("MAP_MBTILES_DIR") or "").strip()
    if d:
        return d
    return "/opt/tak-maps" if os.path.isdir("/opt/tak-maps") else ""


def _mb_open(path):
    try:
        return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
    except sqlite3.Error:
        return sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True, timeout=2)


def list_tilesets(d):
    """Every MBTiles set in the directory by its file name, with what its metadata says."""
    out = []
    if not d or not os.path.isdir(d):
        return out
    for fn in sorted(os.listdir(d)):
        if not fn.endswith(".mbtiles"):
            continue
        sid = fn[:-8]
        if not SET_ID.match(sid):
            continue
        path = os.path.join(d, fn)
        try:
            c = _mb_open(path)
            meta = dict(c.execute("select name, value from metadata").fetchall())
            c.close()
        except sqlite3.Error as ex:
            out.append({"id": sid, "name": sid, "path": path, "error": f"unreadable: {type(ex).__name__}", "drawable": False})
            continue
        fmt = str(meta.get("format") or "").lower()
        try:
            bounds = [float(x) for x in str(meta.get("bounds") or "").split(",")] if meta.get("bounds") else None
        except ValueError:
            bounds = None
        def num(k):
            try:
                return int(meta[k])
            except (KeyError, TypeError, ValueError):
                return None
        out.append({"id": sid, "name": str(meta.get("name") or sid), "path": path, "format": fmt, "drawable": fmt in TILE_TYPES,
                    "minzoom": num("minzoom"), "maxzoom": num("maxzoom"), "bounds": bounds, "attribution": str(meta.get("attribution") or ""),
                    "description": str(meta.get("description") or "")[:300]})
    return out


def tile_bytes(d, sid, z, x, y):
    """One raster tile from a set, XYZ addressed (MBTiles rows are TMS: flipped)."""
    if not SET_ID.match(sid or ""):
        return None, None
    path = os.path.join(d, sid + ".mbtiles")
    if not d or not os.path.exists(path):
        return None, None
    try:
        c = _mb_open(path)
        fmt = str(dict(c.execute("select name, value from metadata where name='format'").fetchall()).get("format") or "png").lower()
        row = c.execute("select tile_data from tiles where zoom_level=? and tile_column=? and tile_row=?", (z, x, (1 << z) - 1 - y)).fetchone()
        c.close()
    except sqlite3.Error:
        return None, None
    if not row or fmt not in TILE_TYPES:
        return None, None
    return bytes(row[0]), TILE_TYPES[fmt]


ATAK_PLACEHOLDERS = (("{$z}", "{z}"), ("{$x}", "{x}"), ("{$y}", "{y}"), ("{$s}", "{s}"))


def atak_url_to_leaflet(url):
    """ATAK writes its tile placeholders {$z}/{$x}/{$y}; the map draws {z}/{x}/{y} (Spec 029)."""
    u = str(url or "").strip()
    if "{$q}" in u:
        return None, "that source addresses tiles by quadkey, which this map cannot draw"
    for a, b in ATAK_PLACEHOLDERS:
        u = u.replace(a, b)
    if not re.match(r"^https?://", u):
        return None, "the URL must start with http:// or https://"
    if not ("{z}" in u and "{x}" in u and "{y}" in u):
        return None, "the URL needs {z}, {x} and {y} in it (ATAK writes them {$z}, {$x}, {$y})"
    if len(u) > 500:
        return None, "the URL is too long"
    return u, None


def parse_map_source_xml(text):
    """One ATAK <customMapSource>: the file an operator already drops into ATAK's imagery folder."""
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(str(text or "").strip())
    except ET.ParseError as ex:
        return None, f"that is not XML this screen can read: {ex}"
    node = root if root.tag == "customMapSource" else root.find(".//customMapSource")
    if node is None:
        return None, "no <customMapSource> in that XML"
    def txt(tag, default=""):
        el = node.find(tag)
        return (el.text or "").strip() if el is not None and el.text else default
    url, err = atak_url_to_leaflet(txt("url"))
    if err:
        return None, err
    def num(tag):
        try:
            return int(txt(tag))
        except ValueError:
            return None
    return {"name": txt("name") or "TAK map source", "url": url, "minzoom": num("minZoom"),
            "maxzoom": num("maxZoom"), "tile_type": (txt("tileType") or "png").lower()}, None


def disk_map_sources(d):
    """ATAK custom map source XML in the box's map folder, beside its MBTiles (Spec 029)."""
    out = []
    if not d or not os.path.isdir(d):
        return out
    try:
        names = sorted(os.listdir(d))
    except OSError:
        return out
    for fn in names:
        if not fn.lower().endswith(".xml"):
            continue
        sid = fn[:-4]
        if not SET_ID.match(sid):
            continue
        try:
            with open(os.path.join(d, fn), encoding="utf-8", errors="replace") as f:
                src, err = parse_map_source_xml(f.read(200000))
        except OSError as ex:
            src, err = None, f"unreadable: {type(ex).__name__}"
        if src:
            out.append(dict(src, id="tak-" + sid, where="the box's map folder", internet=True, removable=False))
        else:
            out.append({"id": "tak-" + sid, "name": sid, "error": err, "where": "the box's map folder", "removable": False})
    return out


def map_sources_path(etc_dir):
    return os.path.join(etc_dir or ".", "map-sources.json")


def saved_map_sources(etc_dir):
    """Sources the operator added on the screen, kept on the box so every browser sees them."""
    try:
        d = json.load(open(map_sources_path(etc_dir)))
    except (OSError, ValueError):
        return []
    return [x for x in d if isinstance(x, dict) and x.get("url") and x.get("id")]


def save_map_sources(etc_dir, sources):
    os.makedirs(etc_dir, exist_ok=True)
    tmp = map_sources_path(etc_dir) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(sources, f)
    os.replace(tmp, map_sources_path(etc_dir))


def map_source_add(etc_dir, name="", xml="", url="", minzoom=None, maxzoom=None):
    name = str(name or "").strip()[:60]
    src = None
    if str(xml or "").strip():
        src, err = parse_map_source_xml(xml)
        if err:
            return None, err
    else:
        u, err = atak_url_to_leaflet(url)
        if err:
            return None, err
        src = {"name": name or "map source", "url": u, "minzoom": None, "maxzoom": None, "tile_type": "png"}
    if name:
        src["name"] = name
    for k, v in (("minzoom", minzoom), ("maxzoom", maxzoom)):
        if v is not None and str(v).strip() != "":
            try:
                src[k] = max(0, min(22, int(v)))
            except (TypeError, ValueError):
                return None, f"{k} must be a number"
    sid = "own-" + re.sub(r"[^a-z0-9]+", "-", src["name"].lower()).strip("-")[:40]
    if not SET_ID.match(sid) or sid == "own-":
        return None, "give the source a name with letters or digits in it"
    have = saved_map_sources(etc_dir)
    # the layer control is keyed by name, so a repeated name would hide a layer rather than add one
    taken = {str(x.get("name", "")).strip().lower() for x in have} | {str(x.get("name", "")).strip().lower() for x in TILE_SOURCES}
    if src["name"].strip().lower() in taken or any(x["id"] == sid for x in have):
        return None, f"a source called {src['name']} is already there"
    rec = dict(src, id=sid, where="added on this screen", internet=True, removable=True)
    have.append(rec)
    save_map_sources(etc_dir, have)
    return rec, None


def map_source_remove(etc_dir, sid):
    have = saved_map_sources(etc_dir)
    keep = [x for x in have if x["id"] != sid]
    if len(keep) == len(have):
        return None, f"no source {sid} was added on this screen; the built-in ones and the box's own files cannot be removed here"
    save_map_sources(etc_dir, keep)
    return {"removed": sid}, None


def tile_sources(config, etc_dir=None):
    """The sources the map offers, with the config's default resolved: the built-in ones, the
    box's MBTiles, ATAK custom map sources on the box, and any the operator added (Spec 029)."""
    sources = [dict(s) for s in TILE_SOURCES]
    for t in disk_map_sources(tilesets_dir(config)):
        if t.get("url"):
            sources.append({"id": t["id"], "name": t["name"], "url": t["url"], "minzoom": t.get("minzoom"),
                            "maxzoom": t.get("maxzoom"), "attribution": e(t.get("name") or ""), "internet": True})
    for t in saved_map_sources(etc_dir) if etc_dir else []:
        sources.append({"id": t["id"], "name": t["name"], "url": t["url"], "minzoom": t.get("minzoom"),
                        "maxzoom": t.get("maxzoom"), "attribution": e(t.get("name") or ""), "internet": True})
    local = [t for t in list_tilesets(tilesets_dir(config)) if t.get("drawable")]
    for t in local:
        sources.append({"id": t["id"], "name": t["name"], "url": f"/tiles/{t['id']}/{{z}}/{{x}}/{{y}}", "minzoom": t.get("minzoom"), "maxzoom": t.get("maxzoom") or 18,
                        "bounds": t.get("bounds"), "attribution": e(t.get("attribution") or ""), "internet": False})
    want = str((config or {}).get("MAP_TILES") or "google-hybrid").strip()
    ids = [s_["id"] for s_ in sources]
    if want == "local":
        default = local[0]["id"] if local else "google-hybrid"
    else:
        default = want if want in ids else "google-hybrid"
    return {"default": default, "sources": sources}


OVERLAY_JS = r"""<script>
(function(){
  var wrap=document.getElementById('mesh-views');if(!wrap)return;
  var tiles=JSON.parse(wrap.dataset.tiles||'{}'),has=wrap.dataset.hasPosition==='1';
  function tok(n){return getComputedStyle(document.documentElement).getPropertyValue(n).trim();}
  function show(v){wrap.querySelectorAll('[data-view]').forEach(function(x){if(x.tagName==='BUTTON'){x.classList.toggle('on',x.dataset.view===v);}else{x.hidden=x.dataset.view!==v;}});
    try{localStorage.setItem('mm-view',v);}catch(e){}if(v==='map'&&window.mmMap){setTimeout(function(){window.mmMap.invalidateSize();},60);}}
  wrap.querySelectorAll('button[data-view]').forEach(function(b){b.addEventListener('click',function(){show(b.dataset.view);});});
  var chosen=wrap.dataset.defaultView;try{var s=localStorage.getItem('mm-view');if(s==='plan'||(s==='map'&&has)){chosen=s;}}catch(e){}
  if(!has||!window.L){show('plan');return;}
  var map=L.map('map-geo',{zoomControl:true,attributionControl:true});window.mmMap=map;
  var layers={},current=null,byName={};
  (tiles.sources||[]).forEach(function(s){var o={maxZoom:20,attribution:s.attribution||''};if(s.subdomains){o.subdomains=s.subdomains;}if(s.maxzoom){o.maxNativeZoom=s.maxzoom;}if(s.minzoom){o.minNativeZoom=s.minzoom;}
    var l=L.tileLayer(s.url,o);l._mm=s;layers[s.id]=l;byName[s.name||s.id]=l;});
  function label(id){var s=layers[id]._mm;var el=document.getElementById('tiles-now');if(el){el.textContent='tiles: '+(s.name||id)+(s.internet?' (from the internet to your browser; the box sends nothing)':' (the box\'s own)');}}
  function use(id){if(!layers[id])return;if(current&&layers[current]){map.removeLayer(layers[current]);}current=id;layers[id].addTo(map);label(id);}
  L.control.layers(byName,null,{position:'topright'}).addTo(map);
  map.on('baselayerchange',function(ev){if(ev.layer&&ev.layer._mm){current=ev.layer._mm.id;label(current);}});
  // If the imagery will not load, fall back to one of the box's own sets, but only to one that
  // covers where we are looking, and only after several tiles fail rather than one. A single
  // transient error used to switch the map to whichever local set came first, which on a box
  // carrying sets for another country left a blank map and no way back (Matt, 4 Sep 2026).
  var fell=false,tileErrors=0,errorSince=0;
  function localCovering(){var c=map.getCenter();
    return (tiles.sources||[]).filter(function(s){if(s.internet)return false;var b=s.bounds;
      if(!b||b.length!==4)return true;                       // no bounds recorded: it may cover us
      return c.lng>=b[0]&&c.lng<=b[2]&&c.lat>=b[1]&&c.lat<=b[3];})[0];}
  Object.keys(layers).forEach(function(id){layers[id].on('tileerror',function(){
    if(fell||!layers[id]._mm.internet||current!==id)return;
    var now=Date.now();if(now-errorSince>10000){tileErrors=0;errorSince=now;}
    if(++tileErrors<6)return;
    var n=document.getElementById('tiles-note');var local=localCovering();
    if(!local){if(n){n.textContent='the imagery is not loading, and none of the box\'s own map sets covers here';}fell=true;return;}
    fell=true;use(local.id);
    if(n){n.textContent='the imagery would not load: switched to the box\'s own tiles ('+(local.name||local.id)+'). Pick another in the layer control.';}});});
  use(tiles.default);
  // trails (Spec 021): each node's positions over a window, drawn under the markers, fading with age
  // coverage (Spec 022): every heard position with its signal, a dot by band, hollow when relayed; under the trails
  var cover_=L.layerGroup().addTo(map);
  function coverHours(){var el=document.getElementById('cover-hours');var v=el?el.value:'0';try{if(!el){v=localStorage.getItem('mm-cover')||'0';}}catch(e){}return v;}
  function drawCover(rows){cover_.clearLayers();var hrs=parseFloat(coverHours());if(!(hrs>0)||!rows||!rows.length)return;var now=Date.now(),win=hrs*3600*1000;
    var names={};(lastJ&&lastJ.nodes||[]).forEach(function(n){names[n.id]=n.label||n.name||n.id;});var n=0;
    rows.forEach(function(r){if(r.snr===null||r.snr===undefined||r.lat===null||r.lon===null)return;if(now-Date.parse(r.ts)>win)return;if(++n>4000)return;var b=band(r.snr),direct=(r.hops===0||r.hops===null||r.hops===undefined);
      L.circleMarker([r.lat,r.lon],{radius:4,color:tok(bandTok(b)),weight:direct?1:1.5,fillColor:tok(bandTok(b)),fillOpacity:direct?0.75:0,opacity:0.9,interactive:true})
        .bindTooltip((names[r.node]||r.node)+' · '+String(r.ts).slice(11,16)+'Z · '+r.snr+' dB'+(direct?'':' · relayed'),{sticky:true,className:'mm-link'}).addTo(cover_);});}
  var coverSel=document.getElementById('cover-hours');if(coverSel){try{var c0=localStorage.getItem('mm-cover');if(c0!==null){coverSel.value=c0;}}catch(e){}coverSel.addEventListener('change',function(){try{localStorage.setItem('mm-cover',coverSel.value);}catch(e){}fetchTrails(true);});}
  var trails_=L.layerGroup().addTo(map),trailLast=0;
  var TRAIL_COLOURS=['--gold','--ok','--line-strong','--accent'];
  function trailHours(){var el=document.getElementById('trail-hours');var v=el?el.value:'3';try{if(!el){v=localStorage.getItem('mm-trails')||'3';}}catch(e){}return v;}
  function drawTrails(rows){trails_.clearLayers();var hrs=parseFloat(trailHours());if(!(hrs>0)||!rows||!rows.length)return;var now=Date.now(),win=hrs*3600*1000;
    var byNode={};rows.forEach(function(r){if(r.lat===null||r.lon===null)return;(byNode[r.node]=byNode[r.node]||[]).push(r);});
    var names={};(lastJ&&lastJ.nodes||[]).forEach(function(n){names[n.id]=n.label||n.name||n.id;});
    Object.keys(byNode).forEach(function(id,idx){var pts=byNode[id];pts.sort(function(a,b){return a.ts<b.ts?-1:1;});var stride=Math.max(1,Math.floor(pts.length/600));var col=tok(TRAIL_COLOURS[idx%TRAIL_COLOURS.length]);
      for(var i=stride;i<pts.length;i+=stride){var a=pts[i-stride],b=pts[i];var age=now-Date.parse(b.ts);if(age>win)continue;var d=map.distance([a.lat,a.lon],[b.lat,b.lon]);if(d>2000)continue;
        var op=0.25+0.75*Math.max(0,1-age/win);L.polyline([[a.lat,a.lon],[b.lat,b.lon]],{color:col,weight:4,opacity:op,interactive:true}).bindTooltip((names[id]||id)+' · '+b.ts.slice(11,16)+'Z',{sticky:true,className:'mm-link'}).addTo(trails_);}});}
  function fetchTrails(force){var th=parseFloat(trailHours()),ch=parseFloat(coverHours());var hrs=Math.max(th>0?th:0,ch>0?ch:0);if(!(hrs>0)){trails_.clearLayers();cover_.clearLayers();return;}var now=Date.now();if(!force&&now-trailLast<60000)return;trailLast=now;
    var since=new Date(now-hrs*3600*1000).toISOString().replace(/\.\d+Z$/,'Z');
    fetch('/api/history?kind=positions&since='+encodeURIComponent(since)+'&limit=5000').then(function(r){return r.json();}).then(function(j){drawTrails(j.rows||[]);drawCover(j.rows||[]);}).catch(function(){});}
  var trailSel=document.getElementById('trail-hours');if(trailSel){try{var t0=localStorage.getItem('mm-trails');if(t0!==null){trailSel.value=t0;}}catch(e){}trailSel.addEventListener('change',function(){try{localStorage.setItem('mm-trails',trailSel.value);}catch(e){}fetchTrails(true);});}
  setInterval(function(){fetchTrails(false);},60000);
  window.mmCoverTick=function(){trailLast=0;fetchTrails(true);};
  // MGRS and UTM on WGS84 (Spec 023): a mirror of mgrs.py, function for function
  var MG=(function(){var A=6378137,F=1/298.257223563,K0=0.9996,E2=F*(2-F),EP2=E2/(1-E2),BANDS='CDEFGHJKLMNPQRSTUVWX',COLS=['ABCDEFGH','JKLMNPQR','STUVWXYZ'],ROWS='ABCDEFGHJKLMNPQRSTUV';
    function zoneFor(lat,lon){var z=Math.floor((lon+180)/6)+1;if(lat>=56&&lat<64&&lon>=3&&lon<12)z=32;if(lat>=72&&lat<84){if(lon>=0&&lon<9)z=31;else if(lon>=9&&lon<21)z=33;else if(lon>=21&&lon<33)z=35;else if(lon>=33&&lon<42)z=37;}return z;}
    function bandFor(lat){if(lat<-80||lat>84)return null;return BANDS[Math.min(19,Math.floor((lat+80)/8))];}
    function toUtm(lat,lon,zone){zone=zone||zoneFor(lat,lon);var lon0=((zone-1)*6-180+3)*Math.PI/180,p=lat*Math.PI/180,l=lon*Math.PI/180;var n=A/Math.sqrt(1-E2*Math.pow(Math.sin(p),2)),t=Math.pow(Math.tan(p),2),c=EP2*Math.pow(Math.cos(p),2),a=(l-lon0)*Math.cos(p);
      var m=A*((1-E2/4-3*E2*E2/64-5*E2*E2*E2/256)*p-(3*E2/8+3*E2*E2/32+45*E2*E2*E2/1024)*Math.sin(2*p)+(15*E2*E2/256+45*E2*E2*E2/1024)*Math.sin(4*p)-(35*E2*E2*E2/3072)*Math.sin(6*p));
      var x=K0*n*(a+(1-t+c)*Math.pow(a,3)/6+(5-18*t+t*t+72*c-58*EP2)*Math.pow(a,5)/120)+500000;var y=K0*(m+n*Math.tan(p)*(a*a/2+(5-t+9*c+4*c*c)*Math.pow(a,4)/24+(61-58*t+t*t+600*c-330*EP2)*Math.pow(a,6)/720));
      var south=lat<0;if(south)y+=10000000;return {zone:zone,hemi:south?'S':'N',x:x,y:y};}
    function fromUtm(zone,hemi,x,y){if(hemi==='S')y-=10000000;var lon0=((zone-1)*6-180+3)*Math.PI/180;var m=y/K0,mu=m/(A*(1-E2/4-3*E2*E2/64-5*E2*E2*E2/256)),e1=(1-Math.sqrt(1-E2))/(1+Math.sqrt(1-E2));
      var p1=mu+(3*e1/2-27*Math.pow(e1,3)/32)*Math.sin(2*mu)+(21*e1*e1/16-55*Math.pow(e1,4)/32)*Math.sin(4*mu)+(151*Math.pow(e1,3)/96)*Math.sin(6*mu);
      var n1=A/Math.sqrt(1-E2*Math.pow(Math.sin(p1),2)),t1=Math.pow(Math.tan(p1),2),c1=EP2*Math.pow(Math.cos(p1),2),r1=A*(1-E2)/Math.pow(1-E2*Math.pow(Math.sin(p1),2),1.5),d=(x-500000)/(n1*K0);
      var lat=p1-(n1*Math.tan(p1)/r1)*(d*d/2-(5+3*t1+10*c1-4*c1*c1-9*EP2)*Math.pow(d,4)/24+(61+90*t1+298*c1+45*t1*t1-252*EP2-3*c1*c1)*Math.pow(d,6)/720);
      var lon=lon0+(d-(1+2*t1+c1)*Math.pow(d,3)/6+(5-2*c1+28*t1-3*c1*c1+8*EP2+24*t1*t1)*Math.pow(d,5)/120)/Math.cos(p1);return [lat*180/Math.PI,lon*180/Math.PI];}
    function square(zone,x,y){return COLS[(zone-1)%3][Math.floor(x/100000)-1]+ROWS[(Math.floor(y/100000)+(zone%2===0?5:0))%20];}
    function pad(n,p){var s=String(n);while(s.length<p)s='0'+s;return s;}
    function mgrs(lat,lon,prec){var band=bandFor(lat);if(!band)return '';var u=toUtm(lat,lon);var sq=square(u.zone,u.x,u.y);var p=Math.max(0,Math.min(5,prec===undefined?5:prec));if(p===0)return u.zone+band+' '+sq;var div=Math.pow(10,5-p);return u.zone+band+' '+sq+' '+pad(Math.floor((u.x%100000)/div),p)+' '+pad(Math.floor((u.y%100000)/div),p);}
    return {zoneFor:zoneFor,toUtm:toUtm,fromUtm:fromUtm,mgrs:mgrs};})();window.mmMgrs=MG.mgrs;
  // the readout: MGRS and degrees under the mouse; the box's own at rest
  var Readout=L.Control.extend({onAdd:function(){var d=L.DomUtil.create('div','mm-readout');d.id='map-readout';d.textContent='';return d;}});map.addControl(new Readout({position:'bottomleft'}));
  function readout(ll,label){var el=document.getElementById('map-readout');if(!el)return;if(!ll){el.textContent='';return;}el.textContent=(label?label+' ':'')+MG.mgrs(ll.lat,ll.lng,5)+' · '+ll.lat.toFixed(5)+', '+ll.lng.toFixed(5);}
  map.on('mousemove',function(ev){readout(ev.latlng,'');});map.on('mouseout',function(){readout(ownLL,'this box');});
  // the grid: UTM lines in the zone of the map's centre, 1 km from zoom 13, 10 km below, labelled at the west and south edges
  var grid_=L.layerGroup().addTo(map);
  function gridOn(){var el=document.getElementById('grid-on');if(el)return el.checked;try{return localStorage.getItem('mm-grid')==='1';}catch(e){return false;}}
  function drawGrid(){grid_.clearLayers();if(!gridOn()||!map._loaded)return;var b=map.getBounds(),c=map.getCenter();var zone=MG.zoneFor(c.lat,c.lng),hemi=c.lat<0?'S':'N';var step=map.getZoom()>=13?1000:10000;
    var corners=[[b.getSouth(),b.getWest()],[b.getSouth(),b.getEast()],[b.getNorth(),b.getWest()],[b.getNorth(),b.getEast()]].map(function(p){return MG.toUtm(p[0],p[1],zone);});
    var minX=Math.min.apply(null,corners.map(function(u){return u.x;})),maxX=Math.max.apply(null,corners.map(function(u){return u.x;})),minY=Math.min.apply(null,corners.map(function(u){return u.y;})),maxY=Math.max.apply(null,corners.map(function(u){return u.y;}));
    var col=tok('--ink-muted'),n=0;
    for(var x=Math.ceil(minX/step)*step;x<=maxX&&n<80;x+=step,n++){var p1=MG.fromUtm(zone,hemi,x,minY),p2=MG.fromUtm(zone,hemi,x,maxY);L.polyline([p1,p2],{color:col,weight:1,opacity:0.55,interactive:false}).addTo(grid_);
      L.tooltip({permanent:true,direction:'top',className:'mm-grid',opacity:0.9}).setLatLng([b.getSouth()+(b.getNorth()-b.getSouth())*0.02,p1[1]]).setContent(String(Math.floor((x%100000)/1000)).padStart(2,'0')).addTo(grid_);}
    for(var y=Math.ceil(minY/step)*step;y<=maxY&&n<80;y+=step,n++){var q1=MG.fromUtm(zone,hemi,minX,y),q2=MG.fromUtm(zone,hemi,maxX,y);L.polyline([q1,q2],{color:col,weight:1,opacity:0.55,interactive:false}).addTo(grid_);
      L.tooltip({permanent:true,direction:'right',className:'mm-grid',opacity:0.9}).setLatLng([q1[0],b.getWest()+(b.getEast()-b.getWest())*0.01]).setContent(String(Math.floor((y%100000)/1000)).padStart(2,'0')).addTo(grid_);}}
  map.on('moveend',drawGrid);map.on('zoomend',drawGrid);
  var gridBox=document.getElementById('grid-on');if(gridBox){try{gridBox.checked=localStorage.getItem('mm-grid')==='1';}catch(e){}gridBox.addEventListener('change',function(){try{localStorage.setItem('mm-grid',gridBox.checked?'1':'0');}catch(e){}drawGrid();});}
  var overlay=L.layerGroup().addTo(map),fitted=false,lastJ=null,ownLL=null;
  // Spec 033: /map#at=lat,lon opens on a place, so a fix read off a device on the bench can be
  // looked at where it is. Only two numbers are ever taken from the hash, and the marker sits on
  // a layer of its own because the overlay is cleared on every redraw.
  var benchAt=null,benchLayer=L.layerGroup().addTo(map);
  (function(){var m=/^#at=(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)$/.exec(window.location.hash||'');if(!m)return;
    var la=parseFloat(m[1]),lo=parseFloat(m[2]);if(!(la>=-90&&la<=90&&lo>=-180&&lo<=180))return;
    benchAt=[la,lo];
    L.circleMarker(benchAt,{radius:9,color:tok('--gold'),weight:2,fillColor:tok('--gold'),fillOpacity:0.5})
      .bindTooltip('read on the bench',{permanent:true,direction:'bottom',className:'mm-node'}).addTo(benchLayer);})();
  // a map fitted while its container had no size (a background tab, a hidden view, a page still laying out) is the whole world at
  // zoom 0; so the first fit only counts once the container has a size, and a resize or the tab coming back refits it (0.2.12)
  function sized(){var r=document.getElementById('map-geo').getBoundingClientRect();return r.width>40&&r.height>40;}
  function refit(){map.invalidateSize();if(!fitted&&lastJ){draw(lastJ);}else{rings();}}
  if(window.ResizeObserver){new ResizeObserver(function(){refit();}).observe(document.getElementById('map-geo'));}
  document.addEventListener('visibilitychange',function(){if(!document.hidden){setTimeout(refit,50);}});
  window.addEventListener('resize',function(){setTimeout(refit,50);});
  // the range rings follow the zoom: three rings inside the shorter half of the view, at a step of 1, 2 or 5 x 10^n metres;
  // the slider sets their opacity, and at zero nothing is drawn (Spec 019)
  var rings_=L.layerGroup().addTo(map),centre=null;
  function niceStep(raw){if(!(raw>0))return 10;var mag=Math.pow(10,Math.floor(Math.log10(raw)));var c_=[1,2,5].map(function(x){return x*mag;}).filter(function(v){return v<=raw;});return c_.length?c_[c_.length-1]:mag;}
  function ringStep(){var b=map.getBounds(),m=map.getCenter();var h=map.distance([b.getNorth(),m.lng],[b.getSouth(),m.lng])/2,w=map.distance([m.lat,b.getWest()],[m.lat,b.getEast()])/2;return niceStep(Math.min(h,w)/3);}
  function alpha(){var v=60;try{var s=localStorage.getItem('mm-ring-alpha');if(s!==null)v=parseInt(s,10);}catch(e){}var el=document.getElementById('ring-alpha');if(el&&el.value!==''){v=parseInt(el.value,10);}if(isNaN(v))v=60;return Math.max(0,Math.min(100,v))/100;}
  function rings(){rings_.clearLayers();if(!centre||!map._loaded)return;var a=alpha(),lbl=document.getElementById('ring-step');if(a<=0){if(lbl){lbl.textContent='rings off';}return;}var step=ringStep();if(lbl){lbl.textContent='rings every '+dist(step);}
    for(var k=1;k<=3;k++){L.circle(centre,{radius:k*step,color:tok('--accent'),weight:4,opacity:a*0.55,dashArray:'4 6',fill:false,interactive:false}).addTo(rings_);
      L.circle(centre,{radius:k*step,color:tok('--gold'),weight:2,opacity:a,dashArray:'4 6',fill:false,interactive:false}).addTo(rings_);
      L.tooltip({permanent:true,direction:'top',className:'mm-ring',opacity:a,interactive:false}).setLatLng([centre[0]+(k*step)/110574,centre[1]]).setContent(dist(k*step)).addTo(rings_);}}
  map.on('zoomend',rings);
  // centre on me: a one-kilometre view with this box in the middle (Matt, 4 Sep 2026)
  var Centre=L.Control.extend({onAdd:function(){var d=L.DomUtil.create('div','leaflet-bar mm-centre');var b=L.DomUtil.create('button','',d);b.type='button';b.id='map-centre';b.setAttribute('aria-label','Centre on this box');b.setAttribute('data-tip','Centre on this box');b.setAttribute('data-tip-more','A one-kilometre view with this box in the middle');
      b.innerHTML="<svg viewBox='0 0 16 16' fill='none' stroke='currentColor' stroke-width='1.6' stroke-linecap='round' aria-hidden='true'><circle cx='8' cy='8' r='4'/><circle cx='8' cy='8' r='1' fill='currentColor' stroke='none'/><path d='M8 1v3M8 12v3M1 8h3M12 8h3'/></svg>";
      L.DomEvent.disableClickPropagation(d);L.DomEvent.on(b,'click',function(ev){L.DomEvent.stop(ev);if(!ownLL)return;map.invalidateSize();map.fitBounds(ownLL.toBounds(1000),{animate:false});});return d;}});
  map.addControl(new Centre({position:'topleft'}));
  function centreBtn(on){var b=document.getElementById('map-centre');if(!b)return;b.disabled=!on;b.setAttribute('data-tip-more',on?'A one-kilometre view with this box in the middle':'No position for this box yet');}
  var slider=document.getElementById('ring-alpha');if(slider){try{var s0=localStorage.getItem('mm-ring-alpha');if(s0!==null){slider.value=s0;}}catch(e){}slider.addEventListener('input',function(){try{localStorage.setItem('mm-ring-alpha',slider.value);}catch(e){}rings();});}
  var pop=document.getElementById('map-pop');if(pop){pop.addEventListener('click',function(){window.open('/map/full','mm-map','popup=yes,width=1100,height=800');});}
  function band(v){if(v===null||v===undefined)return 0;return v>=10?4:v>=5?3:v>=-7?2:v>=-12?1:0;}
  function bandTok(b){return b>=3?'--ok':b===2?'--warn':'--bad';}
  function dist(m){return m>=1000?(m/1000).toFixed(m>=10000?0:1)+' km':Math.round(m)+' m';}
  function draw(J){lastJ=J;overlay.clearLayers();var own=J.own||{};if(own.lat===null||own.lat===undefined||own.lon===null||own.lon===undefined){ownLL=null;centreBtn(false);return;}var c=[own.lat,own.lon];ownLL=L.latLng(c[0],c[1]);centreBtn(true);readout(ownLL,'this box');
    var byId={},pts=[];(J.nodes||[]).forEach(function(n){byId[n.id]=n;if(n.heard_here===false)return;if(n.lat===null||n.lat===undefined||n.lon===null||n.lon===undefined)return;pts.push(n);});
    centre=c;rings();
    pts.forEach(function(n){var ll=[n.lat,n.lon],ds=n.direct_snr;
      if(ds!==null&&ds!==undefined){L.polyline([c,ll],{color:tok(bandTok(band(ds))),weight:3}).bindTooltip(ds+' dB',{permanent:true,direction:'center',className:'mm-link'}).addTo(overlay);}
      else{L.polyline([c,ll],{color:tok('--ink-muted'),weight:2,dashArray:'6 6'}).addTo(overlay);}});
    Object.keys(J.routes||{}).forEach(function(d){var rt=J.routes[d],path=[c];(rt.towards||[]).forEach(function(h){var n=byId[h.id];if(n&&n.lat!==null&&n.lat!==undefined&&n.lon!==null&&n.lon!==undefined){path.push([n.lat,n.lon]);}});
      if(path.length>1){L.polyline(path,{color:tok('--gold'),weight:5,opacity:.7}).addTo(overlay);}});
    pts.forEach(function(n){L.circleMarker([n.lat,n.lon],{radius:8,color:tok('--accent'),weight:2,fillColor:tok('--surface-raised'),fillOpacity:1}).bindTooltip(n.label||n.name||n.id,{permanent:true,direction:'bottom',className:'mm-node'}).addTo(overlay);});
    L.circleMarker(c,{radius:9,color:tok('--gold'),weight:2,fillColor:tok('--accent'),fillOpacity:1}).bindTooltip(own.name||'this box',{permanent:true,direction:'bottom',className:'mm-node'}).addTo(overlay);
    var nopos=(J.nodes||[]).filter(function(n){return n.heard_here!==false&&(n.lat===null||n.lat===undefined||n.lon===null||n.lon===undefined);});
    var ul=document.getElementById('nopos');if(ul){ul.innerHTML=nopos.length?'<b>No position, not placed:</b> '+nopos.map(function(n){var t=document.createElement('span');t.textContent=(n.label||n.name||n.id)+((n.direct_snr!==null&&n.direct_snr!==undefined)?' ('+n.direct_snr+' dB)':' (relayed)');return t.innerHTML;}).join(', '):'';}
    if(!fitted&&sized()&&!benchAt){var b=L.latLngBounds([c]);pts.forEach(function(n){b.extend([n.lat,n.lon]);});map.fitBounds(b.pad(0.35),{maxZoom:17});fitted=true;}
    if(benchAt&&!fitted&&sized()){map.setView(benchAt,16);fitted=true;}}
  var last=0;
  window.mmOverlay=function(){var now=Date.now();if(now-last<1500)return;last=now;fetch('/api/links').then(function(r){return r.json();}).then(function(J){draw(J);fetchTrails(false);}).catch(function(){});};
  show(chosen);last=0;window.mmOverlay();
})();
</script>"""


def ring_step(half_metres):
    """The range-ring step for a view whose shorter half is this many metres: the largest of 1, 2 or
    5 x 10^n such that three rings fit. Mirrors niceStep in the overlay."""
    raw = float(half_metres) / 3
    if raw <= 0:
        return 10
    mag = 10 ** math.floor(math.log10(raw))
    cands = [x * mag for x in (1, 2, 5) if x * mag <= raw]
    return cands[-1] if cands else mag


def mesh_views(L, tiles, size=640, bare=False):
    """The two views: the map over tiles (Leaflet, when the box has a position) and the plan.
    bare: the map on a page of its own (no Pop out control)."""
    own = L.get("own") or {}
    has = own.get("lat") is not None and own.get("lon") is not None
    attr = json.dumps(tiles).replace("&", "&amp;").replace("'", "&#39;").replace('"', "&quot;")
    why = "" if has else "<p class='meta'>no position for this box: the map view needs one. Set --map-lat and --map-lon at install, or give the radio a fix; the plan view places nodes by hops meanwhile.</p>"
    return (f"<div id='mesh-views' data-default-view='{'map' if has else 'plan'}' data-has-position='{'1' if has else '0'}' data-tiles='{attr}'>"
            "<div class='controls views'><button type='button' class='line' data-view='map'>Map</button><button type='button' class='line' data-view='plan'>Plan</button>"
            "<label class='meta' for='ring-alpha' title='The range rings and their labels, from solid to invisible'>rings <input type='range' id='ring-alpha' min='0' max='100' step='5' value='60' style='vertical-align:middle;width:110px'></label><span class='meta' id='ring-step'></span>"
            "<label class='meta' for='trail-hours' title='Each node&#39;s track over the window, fading with age'>trails <select id='trail-hours'><option value='0'>off</option><option value='1'>1 h</option><option value='3' selected>3 h</option><option value='12'>12 h</option><option value='24'>24 h</option></select></label>"
            "<label class='meta' for='cover-hours' title='Every position heard, a dot coloured by its signal band; hollow when it came through a relay'>coverage <select id='cover-hours'><option value='0' selected>off</option><option value='3'>3 h</option><option value='24'>24 h</option><option value='168'>7 d</option></select></label>"
            "<label class='meta' for='grid-on' title='The MGRS grid: 1 km lines from zoom 13, 10 km below, kilometre digits along the edges'><input type='checkbox' id='grid-on' style='width:auto;min-height:0;margin:0 4px 0 0;vertical-align:middle'>grid</label>"
            + ("" if bare else "<button type='button' class='line' id='map-pop' title='The map on its own, in a window of its own'>Pop out</button>")
            + "<span class='meta' id='tiles-now'></span><span class='meta warn' id='tiles-note'></span></div>"
            f"<div data-view='map' hidden><div id='map-geo' class='geo'></div>{why}<div class='meta' id='nopos' style='margin-top:var(--s2)'></div></div>"
            f"<div data-view='plan' id='map-box'>{map_svg(L, size)}</div></div>{OVERLAY_JS}")


def band(snr):
    """The four signal bands the glyph, the map and the link bar share; 0 for unknown."""
    if snr is None:
        return 0
    snr = float(snr)
    return 4 if snr >= 10 else 3 if snr >= 5 else 2 if snr >= -7 else 1 if snr >= -12 else 0


def spark(history, n=40):
    """The last n SNR readings as a small line, with the figures beside it; nothing under two."""
    vals = [float(h[1]) for h in (history or [])[-n:] if h and len(h) > 1 and h[1] is not None]
    if len(vals) < 2:
        return ""
    lo, hi = -20.0, 15.0
    def y(v):
        return 20 - (min(hi, max(lo, v)) - lo) / (hi - lo) * 18
    step = 80.0 / max(1, len(vals) - 1)
    pts = " ".join(f"{2 + i * step:.1f},{y(v):.1f}" for i, v in enumerate(vals))
    fig = f"last {vals[-1]:g} · best {max(vals):g} · worst {min(vals):g} dB"
    return (f"<svg class='spark' viewBox='0 0 84 22' role='img' aria-label='{e(fig)}'><title>{e(fig)}</title><line x1='0' y1='{y(0):.1f}' x2='84' y2='{y(0):.1f}'/><polyline points='{pts}'/></svg>"
            f"<span class='sparkfig visually-hidden'>{e(fig)}</span>")


def route_bar(rt):
    """A traceroute answer as segments, one per hop, coloured by band, both directions."""
    if not rt:
        return "<span class='meta'>no route asked for yet</span>"
    if rt.get("error"):
        return f"<span class='meta bad'>no route: {e(str(rt['error']))}</span>"
    def seg(h):
        sn = h.get("snr")
        txt = f"{float(sn):g} dB" if sn is not None else "? dB, unknown"
        return f"<span class='hop band-{band(sn)}'><b>{e(str(h.get('name') or h.get('id') or ''))}</b> {e(txt)}</span>"
    towards, back = rt.get("towards") or [], rt.get("back") or []
    origin = back[-1].get("name") if back else "this radio"
    dest = towards[-1].get("name") if towards else str(rt.get("dest") or "")
    hops = int(rt.get("hops") or 0)
    return (f"<div class='linkbar'><div class='dir'><span class='meta'>out</span><span class='hop origin'><b>{e(str(origin))}</b></span>{''.join(seg(h) for h in towards)}</div>"
            f"<div class='dir'><span class='meta'>back</span><span class='hop origin'><b>{e(str(dest))}</b></span>{''.join(seg(h) for h in back)}</div>"
            f"<div class='meta'>{hops} hop{'s' if hops != 1 else ''} · asked <time datetime='{e(str(rt.get('ts') or ''))}' data-age>{e(age(rt.get('ts') or ''))}</time></div></div>")


def map_svg(L, size=640):
    """The mesh as a picture: this box at the centre, every heard node about it, links by band,
    routes hop to hop. Geography when the box has a position, hop rings when it has not."""
    import math
    own = L.get("own") or {}
    nodes = [n for n in (L.get("nodes") or []) if n.get("heard_here", True) and n.get("id")]
    routes = L.get("routes") or {}
    cx = cy = size / 2.0
    R = size / 2.0 - 48
    pos, rings = {}, []          # id -> (x, y, how); [(radius px, label)]
    geo = own.get("lat") is not None and own.get("lon") is not None
    if geo:
        lat0, lon0 = float(own["lat"]), float(own["lon"])
        kx, ky = 111320.0 * math.cos(math.radians(lat0)), 110574.0
        offs = {n["id"]: ((float(n["lon"]) - lon0) * kx, (float(n["lat"]) - lat0) * ky)
                for n in nodes if n.get("lat") is not None and n.get("lon") is not None}
        maxd = max([math.hypot(dx, dy) for dx, dy in offs.values()] + [50.0])
        raw = maxd / 3.0
        mag = 10 ** math.floor(math.log10(raw))
        step = next((m * mag for m in (1, 2, 5, 10) if m * mag >= raw), 10 * mag)
        scale = (R - 26) / (3 * step)
        for k in (1, 2, 3):
            d = k * step
            rings.append((d * scale, f"{d / 1000:g} km" if d >= 1000 else f"{d:g} m"))
        for nid, (dx, dy) in offs.items():
            px, py = dx * scale, -dy * scale
            d = math.hypot(px, py)
            if d < 40:      # inside the box's own marker: keep the bearing, push it clear
                a = math.atan2(py, px) if d else math.radians(-90)
                px, py = 40 * math.cos(a), 40 * math.sin(a)
            pos[nid] = (cx + px, cy + py, "fix")
        legend = f"rings every {rings[0][1]}; range rings are geometry, not propagation; the box: {position_words(own).split(':')[0]}"
    else:
        maxh = max([int(n["hops"]) for n in nodes if n.get("hops") is not None] + [0])
        count = max(1, min(3, maxh + 1))
        for k in range(1, count + 1):
            rings.append(((R - 26) * k / count, "direct" if k == 1 else f"{k - 1} hop" + ("s" if k > 2 else "")))
        byring = {}
        for n in nodes:
            if n.get("hops") is not None:
                byring.setdefault(min(int(n["hops"]) + 1, count), []).append(n["id"])
        for k, ids in byring.items():
            for i, nid in enumerate(ids):
                a = math.radians(-90 + 360.0 * i / len(ids))
                r = rings[k - 1][0]
                pos[nid] = (cx + r * math.cos(a), cy + r * math.sin(a), "hops")
        legend = "no position for this box: rings are by hops, not distance (give the radio a fix, or set MAP_LAT and MAP_LON at install)"
    # whoever is left (no fix, unknown hops, or a route hop we have never heard) sits on the outer ring
    need = [n["id"] for n in nodes if n["id"] not in pos]
    for rt in routes.values():
        for h in (rt.get("towards") or []) + (rt.get("back") or []):
            hid = h.get("id")
            if hid and hid != own.get("id") and hid not in pos and hid not in need:
                need.append(hid)
    for i, nid in enumerate(need):
        a = math.radians(-90 + 360.0 * i / max(1, len(need)))
        pos[nid] = (cx + R * math.cos(a), cy + R * math.sin(a), "none")
    # nodes a few metres apart would sit on top of each other: push overlapping markers apart,
    # a little at a time, keeping each roughly where geography or its ring put it
    ids = list(pos)
    for _ in range(40):
        moved = False
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                (x1, y1, h1), (x2, y2, h2) = pos[ids[i]], pos[ids[j]]
                dx, dy = x2 - x1, y2 - y1
                d = math.hypot(dx, dy)
                if d < 30:
                    if d < 1e-6:
                        dx, dy, d = 1.0, 0.0, 1.0
                    push = (30 - d) / 2 + 0.5
                    ux, uy = dx / d, dy / d
                    pos[ids[i]] = (x1 - ux * push, y1 - uy * push, h1)
                    pos[ids[j]] = (x2 + ux * push, y2 + uy * push, h2)
                    moved = True
        if not moved:
            break
    names = {n["id"]: dname(n) for n in nodes}
    for rt in routes.values():
        for h in (rt.get("towards") or []) + (rt.get("back") or []):
            names.setdefault(h.get("id"), str(h.get("name") or h.get("id") or ""))
    out = [f"<svg class='map' viewBox='0 0 {size} {size}' role='img' aria-label='The mesh about this box'>"]
    for r, lbl in rings:
        out.append(f"<circle class='ring' cx='{cx:.1f}' cy='{cy:.1f}' r='{r:.1f}'/><text x='{cx + 4:.1f}' y='{cy - r - 3:.1f}'>{e(lbl)}</text>")
    for n in nodes:
        x, y, _ = pos[n["id"]]
        ds = n.get("direct_snr")
        if ds is not None:
            out.append(f"<line class='link band-{band(ds)}' x1='{cx:.1f}' y1='{cy:.1f}' x2='{x:.1f}' y2='{y:.1f}'/>"
                       f"<text class='lbl' x='{(cx + x) / 2:.1f}' y='{(cy + y) / 2 - 4:.1f}' text-anchor='middle'>{float(ds):g} dB</text>")
        else:
            out.append(f"<line class='link relayed' x1='{cx:.1f}' y1='{cy:.1f}' x2='{x:.1f}' y2='{y:.1f}'/>")
    for rt in routes.values():
        pts = [(cx, cy)]
        labels = []
        for h in rt.get("towards") or []:
            p = pos.get(h.get("id"))
            if not p:
                continue
            labels.append(((pts[-1][0] + p[0]) / 2, (pts[-1][1] + p[1]) / 2, h.get("snr")))
            pts.append((p[0], p[1]))
        if len(pts) > 1:
            out.append(f"<polyline class='route' points='{' '.join(f'{x:.1f},{y:.1f}' for x, y in pts)}'/>")
            for x, y, sn in labels:
                out.append(f"<text class='lbl' x='{x:.1f}' y='{y + 14:.1f}' text-anchor='middle'>{(str(float(sn)) + ' dB') if sn is not None else '? dB'}</text>")
    for nid, (x, y, how) in pos.items():
        out.append(f"<circle class='node{' nopos' if how == 'none' else ''}' data-id='{e(nid)}' data-pos='{e(how)}' cx='{x:.1f}' cy='{y:.1f}' r='9'/>"
                   f"<text class='name' x='{x:.1f}' y='{y + 22:.1f}' text-anchor='middle'>{e(names.get(nid, nid))}</text>")
    out.append(f"<circle class='own' data-own='{e(str(own.get('id') or ''))}' cx='{cx:.1f}' cy='{cy:.1f}' r='10'/>"
               f"<text class='name' x='{cx:.1f}' y='{cy + 24:.1f}' text-anchor='middle'>{e(str(own.get('name') or 'this box'))}</text>")
    out.append(f"<text x='12' y='{size - 12}'>{e(legend)}</text></svg>")
    return "".join(out)


def dname(n):
    """The operator's label where there is one, else the radio's own name, else the id."""
    return str((n or {}).get("label") or (n or {}).get("name") or (n or {}).get("id") or "?")


ICONS = {
    # traceroute: a path through two relays
    "traceroute": "<svg viewBox='0 0 16 16' fill='none' stroke='currentColor' stroke-width='1.6' stroke-linecap='round' stroke-linejoin='round' aria-hidden='true'><circle cx='2.5' cy='13.5' r='1.5'/><circle cx='8' cy='6' r='1.5'/><circle cx='13.5' cy='2.5' r='1.5'/><path d='M3.6 12.4 6.9 7.1M9.1 4.9l3.3-1.3'/></svg>",
    # position: a map pin
    "request_position": "<svg viewBox='0 0 16 16' fill='none' stroke='currentColor' stroke-width='1.6' stroke-linecap='round' stroke-linejoin='round' aria-hidden='true'><path d='M8 14.5s-4.5-4.2-4.5-8A4.5 4.5 0 0 1 12.5 6.5c0 3.8-4.5 8-4.5 8z'/><circle cx='8' cy='6.5' r='1.6'/></svg>",
    # battery: a cell with its terminal
    "request_telemetry": "<svg viewBox='0 0 16 16' fill='none' stroke='currentColor' stroke-width='1.6' stroke-linecap='round' stroke-linejoin='round' aria-hidden='true'><rect x='1.5' y='4.5' width='11' height='7' rx='1.2'/><path d='M14.5 7v2M4 7.5v1M6.5 7.5v1'/></svg>",
    # ask for its name: an identity badge with a refresh arc over it
    "request_nodeinfo": "<svg viewBox='0 0 16 16' fill='none' stroke='currentColor' stroke-width='1.6' stroke-linecap='round' stroke-linejoin='round' aria-hidden='true'><rect x='1.5' y='4' width='13' height='9' rx='1.5'/><circle cx='5.6' cy='7.6' r='1.4'/><path d='M3.4 11.2c.5-1 1.3-1.5 2.2-1.5s1.7.5 2.2 1.5M10 7h3M10 9.6h3'/></svg>",
    # name: a tag
    "name": "<svg viewBox='0 0 16 16' fill='none' stroke='currentColor' stroke-width='1.6' stroke-linecap='round' stroke-linejoin='round' aria-hidden='true'><path d='M2 2h5.5l6.5 6.5-5.5 5.5L2 7.5z'/><circle cx='5.2' cy='5.2' r='1'/></svg>",
}


def node_row(n, db=False, routes=None):
    nid = str(n.get("id") or "")
    name = dname(n)
    own_name = str(n.get("name") or "") if n.get("label") and n.get("name") else ""
    pos = (f"{n['lat']:.5f}, {n['lon']:.5f} · {MG.mgrs(n['lat'], n['lon'], 4) or ''}".rstrip(" ·") if n.get("lat") is not None and n.get("lon") is not None else "no fix")
    sub = " · ".join(x for x in (own_name, str(n.get("hw") or ""), pos) if x)
    heard = n.get("heard") or n.get("last_heard_db")
    heard_html = f"<time datetime='{e(str(heard))}' data-age>{e(age(heard))}</time>" if heard else "<span class='sub'>never</span>"
    batt = n.get("battery")
    # one line for the figure, one small line for the voltage and the age together (0.2.10: short rows)
    ts = n.get("battery_ts")
    bits = ([f"{float(n['voltage']):g} V"] if n.get("voltage") else []) + ([f"<time datetime='{e(str(ts))}' data-age>{e(age(ts))}</time>"] if ts else [])
    under = f"<div class='sub'>{' · '.join(bits)}</div>" if bits else ""
    if n.get("charging"):
        batt_html = f"<span class='ok'>on charge</span>{under}"
    elif batt is None:
        batt_html = "<span class='sub'>no reading</span>"
    elif float(batt) < 20:
        batt_html = f"<span class='batt batt--low'>{int(batt)}%</span>{under}"
    else:
        batt_html = f"{int(batt)}%{under}"
    # the asks as icon buttons: the words live in the tooltip and the label the result line uses
    asks = "".join(f"<button class='line icon' data-action='{e(a['id'])}' data-dest='{e(nid)}' data-label='{e(a['title'])}' data-tip='{e(a['title'])}' data-tip-more='{e(a['description'])}' aria-label='{e(a['title'])}'>{ICONS[a['id']]}</button>"
                   for a in C.ACTIONS if a["risk"] == "air" and len(a["inputs"]) == 1
                   and a["inputs"][0]["type"] == "node" and a["id"] in ICONS)
    return (f"<tr data-id='{e(nid)}' class='{'db' if db else ''}'><td><b><a href='/node?id={e(nid)}' class='plain' title='This node over time'>{e(name)}</a></b><div class='sub'>{e(nid)}{('<span class=hide-narrow> · ' + e(sub) + '</span>') if sub else ''}</div></td>"
            f"<td>{sig(n.get('snr'), n.get('hops'))}{('<div>' + spark(n.get('history')) + '</div>') if not db and spark(n.get('history')) else ''}</td><td>{batt_html}</td><td>{heard_html}</td>"
            f"<td><div class='row-actions'>{asks}"
            + ("" if db else f"<details class='fold ctl icon'><summary data-tip='Name' data-tip-more='A display name for this device, kept on the box; it changes nothing on the radio' aria-label='Name'>{ICONS['name']}</summary><form data-action='register_set' class='regform' data-refresh='' style='grid-template-columns:1fr auto'><input type='hidden' name='id' value='{e(nid)}'>"
                                f"<input type='text' name='label' value='{e(str(n.get('label') or ''))}' maxlength='80' placeholder='display name (changes nothing on the radio)' aria-label='display name'>"
                                "<button type='submit' class='line'>Save</button><div class='res meta' role='status'></div></form></details>")
            + f"</div><div class='res meta' role='status'></div>"
            f"<div class='route-slot'>{route_bar((routes or {}).get(nid)) if (routes or {}).get(nid) else ''}</div></td></tr>")


NODES_JS = r"""<script>
(function(){
  document.addEventListener('click',function(ev){var b=ev.target.closest('button[data-action][data-dest]');if(!b)return;
    var tr=b.closest('tr'),res=tr?tr.querySelector('.res'):null,nm=tr&&tr.querySelector('b')?tr.querySelector('b').textContent:b.dataset.dest;
    if(res){res.textContent=b.dataset.label+': asking the box';res.className='res meta warn';}
    fetch('/api/'+b.dataset.action,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({dest:b.dataset.dest})})
      .then(function(r){return r.json().then(function(j){return [r.status,j];});})
      .then(function(x){if(!res)return;if(x[0]>=400){res.textContent='not asked: '+(x[1].error||x[0]);res.className='res meta bad';}else{res.textContent=b.dataset.label+' asked of '+nm+' at '+window.mmNow()+(b.dataset.action==='traceroute'?' · no answer yet':'');res.className='res meta '+(b.dataset.action==='traceroute'?'warn':'ok');}})
      .catch(function(){if(res){res.textContent='could not ask the box';res.className='res meta bad';}});});
  var last=0;
  window.mmNodes=function(){var now=Date.now();if(now-last<1500)return;last=now;
    fetch('/api/links').then(function(r){return r.json();}).then(function(j){
      [['nodes',j.rows_html],['nodes-db',j.db_rows_html]].forEach(function(p){var tb=document.getElementById(p[0]);if(!tb||p[1]===undefined)return;
        if(document.activeElement&&tb.contains(document.activeElement))return; // never rebuild a table the operator is typing in
        var tmp=document.createElement('tbody');tmp.innerHTML=p[1]||'';var keep={};
        // a live refresh keeps the result line, an open Name control and, above all, a row the operator is typing in (0.2.10)
        tb.querySelectorAll('tr[data-id]').forEach(function(tr){var r=tr.querySelector('.res');var d=tr.querySelector('details');
          var inp=d?d.querySelector('input[name=label]'):null;
          keep[tr.dataset.id]={res:(r&&r.textContent)?[r.innerHTML,r.className]:null,open:!!(d&&d.open),draft:(d&&d.open&&inp)?inp.value:null,focus:!!(document.activeElement&&tr.contains(document.activeElement))};});
        tmp.querySelectorAll('tr[data-id]').forEach(function(tr){var k=keep[tr.dataset.id];if(!k)return;
          if(k.focus){var old=tb.querySelector("tr[data-id='"+tr.dataset.id+"']");if(old){tr.replaceWith(old);return;}}
          if(k.res){var r=tr.querySelector('.res');if(r){r.innerHTML=k.res[0];r.className=k.res[1];}}
          if(k.open){var d=tr.querySelector('details');if(d){d.open=true;var inp=d.querySelector('input[name=label]');if(inp&&k.draft!==null&&k.draft!==inp.value){inp.value=k.draft;}}}});
        tb.innerHTML='';while(tmp.firstChild){tb.appendChild(tmp.firstChild);}});
      var h=document.getElementById('nodes-heard-count');if(h&&j.heard!==undefined){h.textContent=j.heard;}
      var d=document.getElementById('nodes-db-count');if(d&&j.db!==undefined){d.textContent=j.db;}
      document.querySelectorAll('time[data-age]').forEach(function(t){var a=window.mmAge(t.getAttribute('datetime'));if(a){t.textContent=a;}});
    }).catch(function(){});};
  window.mmPosition=function(d){if(!d||!d.id)return;var tr=document.querySelector("tr[data-id='"+d.id+"']");if(!tr)return;var res=tr.querySelector('.res');if(!res)return;
    if(d.lat===null||d.lat===undefined){res.textContent='position: answered '+window.mmNow()+' without a fix';res.className='res meta warn';}
    else{res.textContent='position received '+window.mmNow()+': '+Number(d.lat).toFixed(5)+', '+Number(d.lon).toFixed(5);res.className='res meta ok';}
    if(window.mmNodes){window.mmNodes();}};
  window.mmTelemetry=function(d){if(!d||!d.id)return;var tr=document.querySelector("tr[data-id='"+d.id+"']");if(!tr)return;var res=tr.querySelector('.res');if(!res)return;
    if(d.note){res.textContent='battery: '+d.note+' '+window.mmNow();res.className='res meta warn';}
    else{res.textContent='battery received '+window.mmNow()+': '+(d.charging?'on charge':(d.battery!==null&&d.battery!==undefined?d.battery+'%':'no level'))+(d.voltage?' · '+d.voltage+' V':'');res.className='res meta ok';}
    if(window.mmNodes){window.mmNodes();}};
  window.mmRoute=function(d){if(!d||!d.dest)return;var tr=document.querySelector("tr[data-id='"+d.dest+"']");if(!tr)return;
    var slot=tr.querySelector('.route-slot'),res=tr.querySelector('.res');
    if(d.error){if(res){res.textContent='no route: '+d.error;res.className='res meta bad';}return;}
    if(res&&/asked/.test(res.textContent)){res.textContent=res.textContent.replace(/ · no answer yet$/,'')+' · answered '+window.mmNow();res.className='res meta ok';}
    fetch('/fragment/route/'+encodeURIComponent(d.dest)).then(function(r){return r.text();}).then(function(h){if(slot){slot.innerHTML=h;}}).catch(function(){});};
})();
</script>"""


def nodes_tables(nodes, routes=None):
    heard = [n for n in nodes if n.get("heard_here", True)]
    db = [n for n in nodes if not n.get("heard_here", True)]
    rows = "".join(node_row(n, routes=routes) for n in heard) or "<tr><td colspan=5 class='meta'>No node heard since this bridge started. A quiet mesh is not a broken bridge: wait for a tracker to speak.</td></tr>"
    db_rows = "".join(node_row(n, db=True) for n in db)
    return rows, db_rows, len(heard), len(db)


def nodes_body(nodes, intro=True, routes=None):
    rows, db_rows, heard, db = nodes_tables(nodes, routes)
    head = "<thead><tr><th>Node</th><th>Signal</th><th>Battery</th><th>Heard</th><th>Ask</th></tr></thead>"
    lead = (f"<p class='meta'><span id='nodes-heard-count'>{heard}</span> heard here since the bridge started, "
            f"<span id='nodes-db-count'>{db}</span> more in the radio's database. Joined on radio id; names are labels, never identity.</p>") if intro else ""
    fold = (f"<details class='fold'><summary><span id='nodes-db-count'>{db}</span>&nbsp;in the radio's database only, not heard since this bridge started <span class='pill'>database only</span></summary>"
            f"<div class='tablewrap'><table>{head}<tbody id='nodes-db'>{db_rows}</tbody></table></div></details>") if db or not intro else ""
    js = NODES_JS if intro else NODES_JS + "<script>window.onMesh=window.onMesh||function(d){if(d.kind==='packet'||d.kind==='forwarded'||d.kind==='status'){window.mmNodes();}};</script>"
    return f"{lead}<div class='tablewrap'><table>{head}<tbody id='nodes'>{rows}</tbody></table></div>{fold}{js}"


def nodes_rows_html(nodes):
    return nodes_tables(nodes)[0]


# -- log
LOG_LINE = re.compile(r"^\[(\d\d:\d\d:\d\d)\] (\w+) (.*)$", re.S)
DEVICE_LINE = re.compile(r"\b(DEBUG|INFO|WARN|ERROR|TRACE)\s*\|")


def log_line_html(line):
    m = LOG_LINE.match(line)
    ts, lvl, msg = m.groups() if m else ("", "", line)
    radio = "??:??:??" in msg or bool(DEVICE_LINE.search(msg))
    msg = msg.replace("??:??:??", ts or "--:--:--")   # the radio has no clock; this is when the box heard it
    return f"<span class='ln ln--{'radio' if radio else 'bridge'} ln--{e(lvl or 'INFO')}'>{e(('[' + ts + '] ') if ts else '')}{e(lvl + ' ' if lvl else '')}{e(msg)}</span>"


def log_body(lines):
    js = r"""<script>
(function(){var p=document.getElementById('log'),nb=document.getElementById('newlines'),pending=0;
  function atBottom(){return p.scrollTop+p.clientHeight>=p.scrollHeight-24;}
  function render(line){var m=/^\[(\d\d:\d\d:\d\d)\] (\w+) ([\s\S]*)$/.exec(line);var ts=m?m[1]:'',lvl=m?m[2]:'',msg=m?m[3]:line;
    var radio=msg.indexOf('??:??:??')>=0||/\b(DEBUG|INFO|WARN|ERROR|TRACE)\s*\|/.test(msg);msg=msg.split('??:??:??').join(ts||'--:--:--');
    var s=document.createElement('span');s.className='ln ln--'+(radio?'radio':'bridge')+' ln--'+(lvl||'INFO');s.textContent=(ts?'['+ts+'] ':'')+(lvl?lvl+' ':'')+msg;return s;}
  window.onMesh=function(d){if(d.kind!=='log'||!p)return;var was=atBottom();p.appendChild(render(d.line||''));
    while(p.children.length>600){p.removeChild(p.firstChild);}
    if(was){p.scrollTop=p.scrollHeight;}else{pending++;nb.textContent=pending+' new line'+(pending===1?'':'s');nb.style.display='inline-block';}};
  nb.addEventListener('click',function(){p.scrollTop=p.scrollHeight;pending=0;nb.style.display='none';});
  p.addEventListener('scroll',function(){if(atBottom()){pending=0;nb.style.display='none';}});
  document.querySelector('[data-log-filter]').addEventListener('change',function(ev){p.dataset.show=ev.target.value;});
  document.querySelector('[data-log-level]').addEventListener('change',function(ev){p.dataset.level=ev.target.value;});
  p.scrollTop=p.scrollHeight;})();
</script>"""
    body = "".join(log_line_html(x) for x in lines)
    return (f"<p class='meta'>The bridge's last {len(lines)} lines; new ones arrive as they happen. The radio's own chatter, roughly every minute, is normal; silence is what the watchdog watches.</p>"
            "<div class='controls'><label>Show <select data-log-filter><option value='bridge'>the bridge's lines</option><option value='all'>the bridge's and the radio's lines</option><option value='radio'>the radio's lines only</option></select></label>"
            "<label>Level <select data-log-level><option value='all'>everything</option><option value='warn'>warnings and errors</option></select></label></div>"
            f"<pre class='log' id='log' data-show='bridge' data-level='all'>{body}</pre><button type='button' class='quiet newlines' id='newlines' style='display:none'>new lines</button>{js}")


# -- writes: one handler for every catalogue form and slot button on the screen
WRITE_JS = r"""<script>
(function(){
  if(window.mmWriteBound){return;}window.mmWriteBound=true;
  var own=document.body.dataset.own||'';
  function fill(t){return (t||'').replace(/\{own\}/g, own);}
  function summ(o){if(o===null||o===undefined)return '';if(typeof o!=='object')return String(o);return Object.keys(o).filter(function(k){return k.charAt(0)!=='_';}).map(function(k){return k+' '+(typeof o[k]==='object'?JSON.stringify(o[k]):o[k]);}).join(', ');}
  function show(el,s,j){if(!el)return;var msg,cls;
    if(s>=400){msg='not written: '+(j.error||s);cls='bad';}
    else if(j.confirmed){msg='written and read back at '+window.mmNow()+(j.read_back?': '+summ(j.read_back):'');cls='ok';}
    else if(j.unconfirmed&&/no answer/.test(j.unconfirmed)){msg='asked, no answer yet (sent '+window.mmHm(j.sent)+')';cls='warn';}
    else if(j.confirmed===false){msg='sent '+window.mmHm(j.sent)+', the radio reports '+(j.read_back?summ(j.read_back):(j.unconfirmed||'something else'));cls='warn';}
    else if(j.stages){msg=(j.confirmed?'flashed; the device came back and reports firmware '+(j.version||'?'):'not confirmed: '+(j.unconfirmed||'the device did not come back'))+(j.export?' · configuration exported first':'');cls=j.confirmed?'ok':'bad';}
    else if(j.asked){msg=(j.note||'asked')+' at '+window.mmHm(j.asked);cls='warn';}
    else {msg=j.error?('not done: '+j.error):('done at '+window.mmNow()+(Object.keys(j).length?': '+summ(j):''));cls=j.error?'bad':'ok';}
    el.textContent=msg;el.className='res meta '+cls;}
  function post(url,body,el){
    return fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})
      .then(function(r){return r.json().then(function(j){return [r.status,j];});})
      .then(function(x){show(el,x[0],x[1]);return x;})
      .catch(function(){if(el){el.textContent='could not ask the box';el.className='res meta bad';}return [0,{}];});
  }
  function fixText(p){ // Spec 033: what the device says about its own receiver
    if(!p)return '';
    if(p.enabled===false)return ' · position switched off on the device';
    if(!p.fix)return ' · a receiver, but no fix';
    return ' · fix '+p.lat.toFixed(5)+', '+p.lon.toFixed(5)+(p.mgrs?' ('+p.mgrs+')':'')
      +(p.sats?' · '+p.sats+' sats':'')+(p.alt!==null&&p.alt!==undefined?' · '+p.alt+' m':'')
      +(p.time?' · fixed '+window.mmHm(p.time):'');}
  function mapLink(host,p){ // built as a node, never as markup: the device's own strings are untrusted
    var old=host?host.querySelector('a[data-benchmap]'):null;if(old){old.remove();}
    if(!host||!p||!p.fix)return;
    var a=document.createElement('a');a.setAttribute('data-benchmap','');a.className='line';
    a.href='/map#at='+p.lat.toFixed(6)+','+p.lon.toFixed(6);a.textContent='Show it on the map';
    host.appendChild(a);}
  function device(j){if(j.error)return 'could not read it: '+j.error;
    var ch=(j.channels||[]).map(function(c){return c.index+' '+(c.name||'(unnamed)')+' '+(c.role||'')+(c.has_key?' (key set)':'');}).join(', ');
    return (j.long_name||'?')+' ('+(j.short_name||'?')+') · '+(j.id||'?')+(j.hw?' · '+j.hw:'')+' · firmware '+(j.firmware||'unknown')+' · '+(j.region||'?')+' '+(j.modem_preset||'?')+' · role '+(j.role||'?')+(j.tx_power!==undefined&&j.tx_power!==null?' · '+j.tx_power+' dBm':'')+(j.position_broadcast_secs?' · position every '+j.position_broadcast_secs+' s':'')+' · '+(j.managed?'managed by this radio':'not managed ('+(j.admin_keys===null||j.admin_keys===undefined?'?':j.admin_keys)+' admin keys)')+' · channels: '+(ch||'none')+(j.missing&&j.missing.length?' · no answer for '+j.missing.join(', '):'')+fixText(j.position)+(j.read_at?' · read '+window.mmHm(j.read_at):'');}
  document.addEventListener('submit',function(ev){var f=ev.target.closest('form[data-method=get]');if(!f)return;
    (function(){ev.preventDefault();ev.stopImmediatePropagation();
      var q=new URLSearchParams(new FormData(f)).toString();var host=f.closest('.card')||f.closest('td')||f.parentNode;var out=f.querySelector('.out')||(host?host.querySelector('.out'):null);var btn=f.querySelector('button');if(btn){btn.disabled=true;}
      if(out){out.textContent=(f.dataset.action==='node_read'?'asking the device over the air; seconds to a minute':'asking the device on its cable, this takes a few seconds');out.className='out meta';}
      fetch('/api/'+f.dataset.action+'?'+q).then(function(r){return r.json().then(function(j){return [r.status,j];});})
        .then(function(x){if(btn){btn.disabled=false;}if(!out)return;var j=x[1];
          if(x[0]>=400||j.error){out.textContent='not done: '+(j.error||x[0]);out.className='out meta bad';return;}
          out.textContent=(f.dataset.render==='device'?device(j):('export written at '+window.mmNow()+': '+(j.export||'')+' ('+(j.bytes||0)+' bytes)'));out.className='out meta ok';
          if(f.dataset.render==='device'){mapLink(host,j.position);document.dispatchEvent(new CustomEvent('mm-device',{detail:{card:host,device:j}}));}})
        .catch(function(){if(btn){btn.disabled=false;}if(out){out.textContent='could not ask the box';out.className='out meta bad';}});})();},true);
  document.addEventListener('submit',function(ev){var f=ev.target.closest('form[data-action]:not([data-method=get]):not([id=send])');if(!f)return;
    (function(){ev.preventDefault();
      var action=f.dataset.action,body={},unreachable=f.dataset.risk==='unreachable';
      new FormData(f).forEach(function(v,k){if(v!==''&&k!=='confirm_tick'){var el=f.elements[k];body[k]=(el&&el.type==='number')?parseInt(v,10):v;}});
      if(f.dataset.action==='node_channel_push'&&parseInt(f.elements.index.value,10)>0){unreachable=false;}
      if(unreachable){var tick=f.querySelector('input[name=confirm_tick]');if(tick&&!tick.checked){alert('Tick the consequence first.');return;}var card=f.closest('.card');body.confirm=f.dataset.target||(card&&card.dataset.own)||own;}
      if(f.dataset.action==='bench_flash'){var sel=f.querySelector('select[data-pins]');var o=sel&&sel.options[sel.selectedIndex];if(o&&o.dataset.note&&!confirm(o.dataset.note+' Continue?'))return;var st=f.querySelector('.stages');if(st){st.textContent='';}}
      var text=fill(f.dataset.confirm);
      if(text&&!confirm(text))return;
      var url=f.dataset.proposal?'/api/proposal/run':('/api/'+action);
      if(f.dataset.proposal){body={id:f.dataset.proposal,arguments:body};}
      var btn=f.querySelector('button[type=submit]');if(btn){btn.disabled=true;}
      var rs=f.querySelector('.res');if(rs){rs.textContent=(f.dataset.risk?'sent at '+window.mmNow()+', waiting for the read-back':'sending');rs.className='res meta warn';}
      post(url,body,rs).then(function(x){if(btn){btn.disabled=false;}
        if(x[0]&&x[0]<400){if(f.dataset.proposal){f.classList.add('done');f.querySelectorAll('button').forEach(function(b){b.disabled=true;});}
          if(f.dataset.refresh){var p=f.dataset.refresh.split(':');window.mmFrag(p[0],p[1]);}
          if(f.dataset.clear){f.reset();}}});
    })();
  });
  document.addEventListener('click',function(ev){var b=ev.target.closest('button[data-action][data-index]');if(!b)return;
    (function(){
      var action=b.dataset.action,idx=parseInt(b.dataset.index,10),body={index:idx};
      var text=fill(b.dataset.confirm);
      if(b.dataset.risk==='unreachable'&&idx===0){if(!confirm(text))return;body.confirm=own;}
      else if(text&&!confirm(text))return;
      var res=b.parentNode.parentNode.querySelector('.res');
      post('/api/'+action,body,res).then(function(x){if(x[0]&&x[0]<400&&b.dataset.refresh){var p=b.dataset.refresh.split(':');window.mmFrag(p[0],p[1]);}});
    })();
  });
  document.addEventListener('click',function(ev){var b=ev.target.closest('button[data-dismiss]');if(!b)return;
    (function(){if(!confirm('Dismiss this proposal? Its rationale stays in the audit.'))return;
      var f=b.closest('form');
      post('/api/proposal/dismiss',{id:b.dataset.dismiss},f?f.querySelector('.res'):null).then(function(x){if(x[0]&&x[0]<400&&f){f.classList.add('done');f.querySelectorAll('button').forEach(function(x){x.disabled=true;});}});})();
  });
  var dec=document.getElementById('decode');
  if(dec){dec.addEventListener('click',function(){var url=document.querySelector('form[data-action=channel_adopt] input[name=url]').value;
    fetch('/api/channel_decode?url='+encodeURIComponent(url)).then(function(r){return r.json();}).then(function(j){
      var out=document.getElementById('decoded');var btn=document.querySelector('form[data-action=channel_adopt] button[type=submit]');
      if(j.error){out.textContent='cannot read it: '+j.error;out.className='meta bad';btn.disabled=true;return;}
      out.textContent='This URL carries '+j.count+' channel(s): '+j.channels.map(function(n){return n||'(unnamed)';}).join(', ')+' · region '+(j.region||'not set')+' · preset '+(j.modem_preset||'not set')+'. Read that before you adopt it.';
      out.className='meta ok';btn.disabled=false;});});}
  document.querySelectorAll('[data-qr-open]').forEach(function(b){b.addEventListener('click',function(){document.getElementById('qr-sheet').hidden=false;});});
  document.querySelectorAll('[data-qr-close]').forEach(function(b){b.addEventListener('click',function(){document.getElementById('qr-sheet').hidden=true;});});
  document.querySelectorAll('[data-read-again]').forEach(function(b){b.addEventListener('click',function(){window.location.href=window.location.pathname;});});
  document.querySelectorAll('[data-copy]').forEach(function(b){b.addEventListener('click',function(){var t=b.dataset.copy;
    (navigator.clipboard?navigator.clipboard.writeText(t):Promise.reject()).then(function(){b.textContent='Copied';},function(){window.prompt('Copy the token:',t);});});});
})();
</script>"""


def _act(aid):
    return C.by_id(aid) or {"confirm": "", "risk": "read", "description": "", "inputs": [], "title": aid}


def read_line(res, page_path):
    at = hhmm(res.get("read_at")) if res.get("read_at") else hhmm()
    return f"<p class='meta'>read from the radio at {e(at)} <button type='button' class='quiet' data-read-again>Read again</button></p>"


def channel_rows(ch):
    chans = ch.get("channels", [])
    live = [c for c in chans if c.get("role") != "DISABLED"]
    rot, dele = _act("channel_rotate"), _act("channel_delete")
    rows = ""
    for c in live:
        i = int(c.get("index", 0))
        ctl = (f"<button class='line' data-action='channel_rotate' data-index='{i}' data-risk='{'unreachable' if i == 0 else 'change'}' data-refresh='channels:channel-rows' "
               f"data-confirm=\"{e(rot['confirm'] if i == 0 else 'A fresh key on ' + (c.get('name') or 'slot ' + str(i)) + '. Devices on it need the new QR.')}\">Rotate key</button>")
        if i >= 1:
            ctl += f" <button class='danger' data-action='channel_delete' data-index='{i}' data-refresh='channels:channel-rows' data-confirm=\"{e(dele['confirm'])}\">Delete</button>"
        rows += (f"<tr><td>{i}</td><td>{e(c.get('name') or '(unnamed)')}</td><td><span class='pill'>{e(c.get('role') or '')}</span></td>"
                 f"<td>{'set' if c.get('has_key') else 'none'}</td><td><div class='row-actions'>{ctl}</div><div class='res meta' role='status'></div></td></tr>")
    free = 8 - len(live)
    rows += f"<tr><td colspan=5 class='meta'>{free} free slot{'s' if free != 1 else ''} of 8.</td></tr>"
    return rows


def channels_body(ch, own_id="?", st=None, rotation=None):
    st = st or {}
    primary = next((c.get("name") for c in ch.get("channels", []) if c.get("role") == "PRIMARY"), None) or st.get("primary_channel") or "the primary channel"
    err = f"<p class='warn'>{e(ch['error'])}</p>" if ch.get("error") else ""
    qr = ("<h2>Join the primary channel</h2><p class='meta'>The join QR carries the channel name, the key, the region and the modem preset; the key appears nowhere else on this screen. "
          "Show it only to a device you mean to join.</p><button type='button' data-qr-open>Show the join QR</button>"
          f"<div class='sheet' id='qr-sheet' hidden><img src='/channels/qr.png' alt='Join QR for {e(primary)}' width='320' height='320'>"
          f"<div><b>{e(primary)}</b> · {e(st.get('region') or '?')} · {e(st.get('modem_preset') or '?')}</div><p class='meta'>Scan it in the Meshtastic app, or hold it up to a tracker's onboarding.</p>"
          "<button type='button' class='line' data-qr-close>Close</button></div>"
          if ch.get("url") else "<p class='meta'>No primary channel with a key is readable yet.</p>")
    cre, ado = _act("channel_create"), _act("channel_adopt")
    create = (f"<form class='card' data-action='channel_create' data-risk='change' data-refresh='channels:channel-rows' data-clear='1' data-confirm=\"{e(cre['confirm'])}\">"
              f"<h2 style='margin-top:0'>{e(cre['title'])}</h2><p class='meta'>{e(cre['description'])}</p>"
              "<label>Name (11 bytes at most)<input type='text' name='name' maxlength='11' required></label>"
              "<label>Slot<select name='index'><option value=''>first free</option>" + "".join(f"<option value='{i}'>{i}</option>" for i in range(1, 8)) + "</select></label>"
              "<button type='submit'>Create and read back</button><div class='res meta' role='status'></div></form>")
    adopt = (f"<form class='card danger' data-action='channel_adopt' data-risk='unreachable' data-refresh='channels:channel-rows' data-confirm=\"{e(ado['confirm'])}\">"
             f"<h2 style='margin-top:0'>{e(ado['title'])}</h2><p class='meta'>{e(ado['description'])}</p>"
             "<label>Join URL<input type='text' name='url' required autocomplete='off'></label>"
             "<button type='button' class='line' id='decode'>Read it first</button><div id='decoded' class='meta' style='margin:.4rem 0'></div>"
             "<label>Mode<select name='mode'><option value='add'>add its channels to free slots</option><option value='replace'>replace this radio's channels and region</option></select></label>"
             f"<label class='check'><input type='checkbox' name='confirm_tick'><span>I understand a replace moves this radio to the URL's channels and region; devices on the old ones will not hear it. This radio is {e(own_id)}.</span></label>"
             "<button type='submit' disabled>Adopt and read back</button><div class='res meta' role='status'></div></form>")
    return (f"{err}{read_line(ch, '/channels')}<div class='tablewrap'><table><thead><tr><th>#</th><th>Name</th><th>Role</th><th>Key</th><th></th></tr></thead><tbody id='channel-rows'>{channel_rows(ch)}</tbody></table></div>"
            f"<div class='cards' style='margin-top:1rem'>{create}{adopt}</div>{qr}<div id='rotation-body'>{rotation_section(rotation)}</div>{ROTATION_JS}{WRITE_JS}")


def manage_forms(r):
    """Over the air (Spec 011): the forms for one managed device, every write read back."""
    nid = str(r.get("id") or "")
    ns, nr, npush, nrb = _act("node_set"), _act("node_set_region"), _act("node_channel_push"), _act("node_reboot")
    ins = {i["name"]: i for i in nr.get("inputs", [])}
    def sel(name, values):
        return f"<select name='{name}'><option value=''>leave as is</option>" + "".join(f"<option value='{e(str(x))}'>{e(str(x))}</option>" for x in values) + "</select>"
    read = (f"<form data-action='node_read' data-method='get' data-render='device'><input type='hidden' name='id' value='{e(nid)}'><button type='submit' class='line'>Read over the air</button></form><div class='out meta' role='status'></div>")
    setf = (f"<form data-action='node_set' data-risk='change' data-target='{e(nid)}' data-confirm=\"{e(ns.get('confirm') or '')}\"><input type='hidden' name='id' value='{e(nid)}'>"
            f"<h2 style='margin-top:0;font-size:.95rem'>{e(ns.get('title') or '')}</h2>"
            "<label>Long name<input type='text' name='long_name' maxlength='39'></label><label>Short name (4 bytes)<input type='text' name='short_name' maxlength='4'></label>"
            "<label>TX power, dBm<input type='number' name='tx_power' min='0' max='30'></label><label>Position broadcast, seconds<input type='number' name='position_broadcast_secs' min='32' max='86400'></label>"
            "<button type='submit'>Write over the air and read back</button><div class='res meta' role='status'></div></form>")
    regf = (f"<form class='danger' data-action='node_set_region' data-risk='unreachable' data-target='{e(nid)}' data-confirm=\"{e(nr.get('confirm') or '')}\"><input type='hidden' name='id' value='{e(nid)}'>"
            f"<h2 style='margin-top:0;font-size:.95rem;color:var(--bad)'>{e(nr.get('title') or '')}</h2>"
            f"<label>Region{sel('region', ins.get('region', {}).get('values', []))}</label><label>Modem preset{sel('modem_preset', ins.get('modem_preset', {}).get('values', []))}</label><label>Role{sel('role', ins.get('role', {}).get('values', []))}</label>"
            f"<label class='check'><input type='checkbox' name='confirm_tick'><span>I understand this device may be unreachable over the air afterwards and reboots. This device is {e(nid)}.</span></label>"
            "<button type='submit' class='danger'>Write over the air and read back</button><div class='res meta' role='status'></div></form>")
    push = (f"<form data-action='node_channel_push' data-risk='unreachable' data-target='{e(nid)}' data-confirm=\"{e(npush.get('confirm') or '')}\"><input type='hidden' name='id' value='{e(nid)}'>"
            f"<h2 style='margin-top:0;font-size:.95rem'>{e(npush.get('title') or '')}</h2>"
            "<label>Slot<select name='index'>" + "".join(f"<option value='{i}'{' selected' if i == 1 else ''}>{i}{' (primary)' if i == 0 else ''}</option>" for i in range(8)) + "</select></label>"
            f"<label class='check'><input type='checkbox' name='confirm_tick'><span>For slot 0: I understand the device's primary channel is replaced. This device is {e(nid)}.</span></label>"
            "<button type='submit'>Push and read back</button><div class='res meta' role='status'></div></form>")
    reboot = (f"<form data-action='node_reboot' data-risk='unreachable' data-target='{e(nid)}' data-confirm=\"{e(nrb.get('confirm') or '')}\"><input type='hidden' name='id' value='{e(nid)}'>"
              f"<label class='check'><input type='checkbox' name='confirm_tick'><span>Reboot in ten seconds; off the mesh while it does. This device is {e(nid)}.</span></label>"
              "<button type='submit' class='line'>Reboot over the air</button><div class='res meta' role='status'></div></form>")
    return (f"<details class='fold ctl'><summary>Manage</summary><div class='manage'>{read}{setf}{regf}{push}{reboot}</div></details>")


def register_rows(reg):
    rows = ""
    for r in reg.get("rows", []):
        nid = str(r.get("id") or "")
        heard = r.get("heard") or r.get("last_heard_db")
        heard_html = f"<time datetime='{e(str(heard))}' data-age>{e(age(heard))}</time>" if heard else ("<span class='sub'>on the bench only, not heard on the air</span>" if r.get("bench_only") else "<span class='sub'>not heard</span>")
        managed = ("<span class='pill'>managed</span>" + (f"<div class='sub'>since <time datetime='{e(str(r.get('managed_at') or ''))}' data-age>{e(age(r.get('managed_at') or ''))}</time></div>" if r.get("managed_at") else "")
                   + manage_forms(r) if r.get("managed") else "<span class='sub'>not managed: bring it to the bench</span>")
        form = (f"<form data-action='register_set' data-refresh='register:register-rows' class='regform'><input type='hidden' name='id' value='{e(nid)}'>"
                f"<input type='text' name='label' value='{e(str(r.get('label') or ''))}' maxlength='80' placeholder='label' aria-label='label'>"
                f"<input type='text' name='holder' value='{e(str(r.get('holder') or ''))}' maxlength='80' placeholder='who holds it' aria-label='who holds it'>"
                "<button type='submit' class='line'>Save</button><div class='res meta' role='status'></div></form>")
        fg = _act("node_forget")
        forget = (f"<details class='fold ctl'><summary>Forget</summary><form data-action='node_forget' data-risk='change' data-confirm=\"{e(fg.get('confirm') or '')}\" data-refresh='register:register-rows'>"
                  f"<input type='hidden' name='id' value='{e(nid)}'><label>Its label and holder<select name='register'><option value='keep'>keep, for when it is heard again</option><option value='drop'>drop</option></select></label>"
                  "<button type='submit' class='danger'>Forget this node</button><div class='res meta' role='status'></div></form></details>")
        rows += (f"<tr data-id='{e(nid)}'><td><b>{e(dname(r))}</b><div class='sub'>{e(nid)}{(' · ' + e(str(r.get('name') or ''))) if r.get('label') and r.get('name') else ''}</div>{forget}</td><td>{form}</td>"
                 f"<td>{e(str(r.get('hw') or ''))}<div class='sub'>{e(str(r.get('firmware') or 'firmware unknown'))}</div></td><td>{e(str(r.get('role') or ''))}</td>"
                 f"<td>{managed}</td><td>{heard_html}</td></tr>")
    return rows or "<tr><td colspan=6 class='meta'>No device in the register and none in the radio's database.</td></tr>"


def register_body(reg, drift=None):
    js = r"""<script>
(function(){var last=0;
  window.mmRegister=function(){var now=Date.now();if(now-last<1500)return;last=now;
    fetch('/fragment/register').then(function(r){return r.text();}).then(function(h){var tb=document.getElementById('register-rows');if(!tb)return;
      if(document.activeElement&&tb.contains(document.activeElement))return; // never rebuild a table the operator is typing in
      var tmp=document.createElement('tbody');tmp.innerHTML=h;var keep={};
      tb.querySelectorAll('tr[data-id]').forEach(function(tr){keep[tr.dataset.id]={open:!!(tr.querySelector('details')&&tr.querySelector('details').open),
        res:[].map.call(tr.querySelectorAll('.res,.out'),function(x){return [x.className,x.innerHTML];}),focus:document.activeElement&&tr.contains(document.activeElement)};});
      tmp.querySelectorAll('tr[data-id]').forEach(function(tr){var k=keep[tr.dataset.id];if(!k)return;if(k.focus){var old=tb.querySelector("tr[data-id='"+tr.dataset.id+"']");if(old){tr.replaceWith(old);return;}}
        if(k.open&&tr.querySelector('details')){tr.querySelector('details').open=true;}
        var xs=tr.querySelectorAll('.res,.out');k.res.forEach(function(p,i){if(xs[i]&&p[1]){xs[i].className=p[0];xs[i].innerHTML=p[1];}});});
      tb.innerHTML='';while(tmp.firstChild){tb.appendChild(tmp.firstChild);}
      document.querySelectorAll('time[data-age]').forEach(function(t){var a=window.mmAge(t.getAttribute('datetime'));if(a){t.textContent=a;}});}).catch(function(){});};
  window.onMesh=function(d){if(d.kind==='register'||d.kind==='bench'||d.kind==='node'||d.kind==='status'){window.mmRegister();}};})();
</script>"""
    n = len(reg.get("rows", []))
    managed = sum(1 for r in reg.get("rows", []) if r.get("managed"))
    return (f"<p class='meta'>{n} device{'s' if n != 1 else ''} the radio knows of or the bench has seen, {managed} managed. Joined on radio id and nothing else: the node's own name from the air sits beside your label, never in its place. "
            "A device is managed only when a read of the device itself showed this radio's public key among its admin keys; the bench is where that happens.</p>"
            "<div class='tablewrap'><table><thead><tr><th>Node</th><th>Label · holder</th><th>Hardware · firmware</th><th>Role</th><th>Managed</th><th>Heard</th></tr></thead>"
            f"<tbody id='register-rows'>{register_rows(reg)}</tbody></table></div>{stale_form()}<div id='drift-body' style='margin-top:var(--s4)'>{drift_section(drift)}</div>{DRIFT_JS}{js}{WRITE_JS}")


DRIFT_JS = "<script>(function(){var prev=window.onMesh||function(){};window.onMesh=function(d){prev(d);if(d.kind==='node'){window.mmFrag('drift','drift-body');}};})();</script>"


def drift_section(d):
    """Spec 028: the fleet profile, and every device against it."""
    d = d or {}
    prof = d.get("profile") or {}
    ps = _act("profile_set"); fx = _act("drift_fix")
    def v(k):
        return "" if prof.get(k) is None else e(str(prof.get(k)))
    form = (f"<form data-action='profile_set' class='card' data-risk='change' data-confirm=\"{e(ps.get('confirm') or '')}\" data-refresh='drift:drift-body' style='max-width:760px'><h2 style='margin-top:0'>Fleet profile</h2><p class='meta'>{e(ps['description'])} Blank leaves a field unenforced.</p>"
            f"<div class='regform' style='grid-template-columns:repeat(5,1fr)'><label>Role<input type='text' name='role' value='{v('role')}' placeholder='TRACKER'></label>"
            f"<label>Power (dBm)<input type='number' name='tx_power' value='{v('tx_power')}' min='0' max='30'></label>"
            f"<label>Position every (s)<input type='number' name='position_broadcast_secs' value='{v('position_broadcast_secs')}' min='32' max='86400'></label>"
            f"<label>Region<input type='text' name='region' value='{v('region')}' placeholder='EU_868'></label>"
            f"<label>Preset<input type='text' name='modem_preset' value='{v('modem_preset')}' placeholder='SHORT_FAST'></label></div>"
            "<button class='line' style='margin-top:var(--s2)'>Save the profile</button><div class='res meta' role='status'></div></form>")
    c = d.get("counts") or {}
    rows = ""
    for dev in d.get("devices") or []:
        st = dev.get("state")
        if st == "unread":
            what = "<span class='sub'>never read: read it on the bench or over the air first</span>"
        elif dev.get("diffs"):
            what = "<br>".join(f"<b>{e(x['field'])}</b> is {e(str(x['is']))}, should be {e(str(x['should']))}" for x in dev["diffs"])
        else:
            what = "<span class='ok'>in line</span>"
        hard = any(x["field"] in ("region", "modem_preset", "role") for x in dev.get("diffs") or [])
        fix = ""
        if dev.get("diffs") and dev.get("managed"):
            fix = (f"<form data-action='drift_fix' data-risk='{'unreachable' if hard else 'change'}' data-target='{e(dev['id'])}' data-confirm=\"{e(fx.get('confirm') or '')}\" data-refresh='drift:drift-body' class='regform' style='grid-template-columns:auto auto'>"
                   f"<input type='hidden' name='id' value='{e(dev['id'])}'><input type='hidden' name='scope' value='{'all' if hard else 'safe'}'>"
                   + ("<label class='check'><input type='checkbox' name='confirm_tick'> I understand this device moves band</label>" if hard else "")
                   + f"<button class='line'>Bring into line{' (all)' if hard else ''}</button><div class='res meta' role='status'></div></form>")
        elif dev.get("diffs"):
            fix = "<span class='sub'>not managed: the bench makes it so</span>"
        when = f"<time datetime='{e(dev.get('read_at'))}' data-age>{e(age(dev.get('read_at')))}</time>" if dev.get("read_at") else ""
        rows += f"<tr><td><b>{e(dev.get('name'))}</b><div class='sub'>{e(dev.get('id'))}</div></td><td>{what}</td><td class='meta'>{when}</td><td>{fix}</td></tr>"
    return (f"{form}<h2>Drift</h2><p class='meta'>Every registered device's last read-back against the profile: <b>{int(c.get('in_line') or 0)} in line</b>, {int(c.get('drifted') or 0)} drifted, {int(c.get('unread') or 0)} never read. Enforced: {e(', '.join(d.get('enforced') or []) or 'nothing yet')}.</p>"
            f"<div class='tablewrap'><table><thead><tr><th>Device</th><th>Against the profile</th><th>Read</th><th></th></tr></thead><tbody>{rows or '<tr><td colspan=4 class=meta>No devices in the register.</td></tr>'}</tbody></table></div>")


def stale_form():
    a = _act("nodes_forget_stale")
    return (f"<details class='fold ctl' style='margin-top:var(--s3)'><summary>Forget the stale</summary><form data-action='nodes_forget_stale' data-risk='change' data-confirm=\"{e(a.get('confirm') or '')}\" data-refresh='register:register-rows'>"
            "<p class='meta'>Every node the radio has not heard for a number of days leaves its database and the box's lists: dead radios, old ids, duplicates. Nothing is sent; a node comes back if it is heard again, and labels and holders are kept.</p>"
            "<label>not heard for <input type='number' name='days' value='7' min='1' max='365' style='width:5em'> days</label> <button class='line'>Forget them</button><div class='res meta' role='status'></div></form></details>")


def shelf_card(sh):
    rows = ""
    for i in sh.get("images", []):
        st = i.get("state") or "missing"
        cls = {"verified": "ok", "wrong": "bad", "missing": "warn"}.get(st, "")
        rows += (f"<tr><td><b>{e(str(i.get('version') or ''))}</b> {'<span class=pill>recommended</span>' if i.get('recommended') else ''}<div class='sub'>{e(', '.join(i.get('hw') or []))} · {e(str(i.get('method') or ''))}</div></td>"
                 f"<td><span class='{cls}'>{e(st)}</span><div class='sub'>{e(str(i.get('file') or ''))}</div>{('<div class=sub>put it at ' + e(str(i.get('path') or '')) + '</div>') if st != 'verified' else ''}</td>"
                 f"<td class='meta'>{e(str(i.get('note') or ''))}</td></tr>")
    return (f"<div class='card' style='grid-column:1/-1'><div class='k'>Shelf</div><div class='v'>Firmware pinned for the fleet</div><p class='meta'>Images live on the box under {e(str(sh.get('dir') or ''))}, put there by the installer or by you; the bridge flashes only a file whose sha256 matches its pin.</p>"
            f"<div class='tablewrap'><table><thead><tr><th>Image</th><th>On this box</th><th>Note</th></tr></thead><tbody>{rows or '<tr><td colspan=3 class=meta>No pins in this release.</td></tr>'}</tbody></table></div></div>")


def bench_cards(d, shelf=None):
    onb = _act("bench_onboard")
    roles = next((i.get("values", []) for i in onb.get("inputs", []) if i["name"] == "role"), [])
    cards = ""
    for dev in d.get("devices", []):
        path, name = str(dev.get("path") or ""), os.path.basename(str(dev.get("path") or ""))
        if dev.get("bootloader"):
            cards += (f"<div class='card' data-path='{e(path)}'><div class='k'>{e(dev.get('tty') or '')}</div><div class='v'>{e(name)}</div>"
                      f"<p class='bad'>In bootloader mode: it answers nothing. {e(str(dev.get('recovery') or ''))}</p></div>")
            continue
        if dev.get("kind") == "gps":
            cards += (f"<div class='card' data-path='{e(path)}'><div class='k'>{e(dev.get('tty') or '')}</div><div class='v'>{e(name)}</div>"
                      "<p class='meta'>The box's own GPS receiver on the same USB bus: not a radio, so nothing here opens it.</p></div>")
            continue
        cards += (f"<div class='card' data-path='{e(path)}'><div class='k'>{e(dev.get('tty') or '')}</div><div class='v'>{e(name)}</div>"
                  "<div class='row-actions' style='margin:.5rem 0'>"
                  f"<form data-action='bench_read' data-method='get' data-render='device'><input type='hidden' name='path' value='{e(path)}'><button type='submit' class='line'>Read</button></form>"
                  f"<form data-action='bench_export' data-method='get'><input type='hidden' name='path' value='{e(path)}'><button type='submit' class='line'>Export its configuration</button></form>"
                  "</div><div class='out meta' role='status'></div>"
                  f"<details class='fold ctl'><summary>Onboard</summary><form data-action='bench_onboard' data-risk='change' data-confirm=\"{e(onb.get('confirm') or '')}\">"
                  f"<input type='hidden' name='path' value='{e(path)}'>"
                  "<label>Long name<input type='text' name='long_name' maxlength='39' required></label>"
                  "<label>Short name (4 bytes)<input type='text' name='short_name' maxlength='4' required></label>"
                  "<label>Role<select name='role'>" + "".join(f"<option value='{e(str(rv))}'{' selected' if rv == 'TRACKER' else ''}>{e(str(rv))}</option>" for rv in roles) + "</select></label>"
                  "<button type='submit'>Onboard and read back</button><div class='res meta' role='status'></div></form></details>"
                  + restore_flash_forms(path, shelf) + "</div>")
    return cards or "<p class='meta'>No device on the bench: plug one into the box by USB. The gateway's own radio is never listed here.</p>"


def restore_flash_forms(path, shelf):
    res, fl = _act("bench_restore"), _act("bench_flash")
    pins = [i for i in (shelf or {}).get("images", []) if i.get("state") == "verified"]
    opts = "".join(f"<option value='{e(str(i['id']))}' data-hw='{e(','.join(i.get('hw') or []))}' data-note='{e(str(i.get('note') or ''))}'>{e(str(i.get('version') or ''))} · {e(', '.join(i.get('hw') or []))}{' · recommended' if i.get('recommended') else ''}</option>"
                   for i in sorted(pins, key=lambda x: (not x.get("recommended"), x.get("version") or "")))
    restore = (f"<details class='fold ctl'><summary>Restore</summary><form data-action='bench_restore' data-risk='unreachable' data-confirm=\"{e(res.get('confirm') or '')}\">"
               f"<input type='hidden' name='path' value='{e(path)}'>"
               "<label>Export on the box<select name='export' data-exports><option value=''>Read the device first, then pick one of its exports</option></select></label>"
               "<label class='check'><input type='checkbox' name='confirm_tick'><span>I understand the device's names, channels and settings are replaced by the export's; its own keys stay. Ticked, this also allows an export made from a different device (a clone).</span></label>"
               "<button type='submit'>Restore and read back</button><div class='res meta' role='status'></div></form></details>")
    flash = (f"<details class='fold ctl'><summary>Flash</summary><form data-action='bench_flash' data-risk='unreachable' data-flash='1' data-confirm=\"{e(fl.get('confirm') or '')}\">"
             f"<input type='hidden' name='path' value='{e(path)}'>"
             f"<label>Pinned image<select name='image' data-pins>{opts or '<option value=\'\'>no verified image on the shelf</option>'}</select></label>"
             "<label class='check'><input type='checkbox' name='confirm_tick'><span>I understand: the configuration is exported first, then the device is flashed and reboots; a factory image loses every setting; a flash that does not come back needs the recovery step. This device is the one named on Read.</span></label>"
             "<button type='submit' class='danger'>Export, flash and read the version back</button><div class='res meta' role='status'></div><div class='stages meta'></div></form></details>")
    return restore + flash


def bench_body(d, shelf=None):
    js = r"""<script>
(function(){
  window.onMesh=function(d){if(d.kind==='bench'){window.mmFrag('bench','bench-cards');}
    if(d.kind==='flash'){document.querySelectorAll("form[data-flash]").forEach(function(f){var st=f.querySelector('.stages');if(st){st.textContent=(st.textContent?st.textContent+' → ':'')+d.stage+(d.version?' '+d.version:'');}});}};
  // after a Read, the card knows the device: fill its exports and keep only the pins for its hardware
  document.addEventListener('mm-device',function(ev){var card=ev.detail.card,j=ev.detail.device;if(!card||!j||!j.id)return;
    card.dataset.own=j.id;card.querySelectorAll('input[name=confirm]').forEach(function(x){x.value=j.id;});
    fetch('/api/bench_exports?id='+encodeURIComponent(j.id)).then(function(r){return r.json();}).then(function(x){var sel=card.querySelector('select[data-exports]');if(!sel)return;
      sel.innerHTML=(x.exports||[]).length?(x.exports||[]).map(function(e){return "<option value='"+e.path+"'>"+e.when+" ("+e.bytes+" bytes)</option>";}).join(''):"<option value=''>no export of "+j.id+" on this box yet</option>";}).catch(function(){});
    var pins=card.querySelector('select[data-pins]');if(pins){[].forEach.call(pins.options,function(o){var hw=(o.dataset.hw||'').split(',');o.hidden=!!o.value&&hw.indexOf(j.hw)<0;});}});
})();
</script>"""
    return (f"<p class='meta'>Devices plugged into this box by USB, by their by-id name; the gateway radio ({e(os.path.basename(str(d.get('gateway') or '')) or 'none')}) is set from the Radio page and never opened here. "
            "Read opens the device and shows what it says about itself; Onboard gives it names and a role, this radio's primary channel and key, this radio's region and preset, and this radio's public key as an admin key, every one read back before it shows; "
            "Export saves its configuration, keys included, on the box at mode 0600.</p>"
            f"<div class='cards' id='bench-cards'>{bench_cards(d, shelf)}</div><div class='cards' style='margin-top:1rem'>{shelf_card(shelf or {})}</div>{js}{WRITE_JS}")


def lessons_html():
    """The mesh-lessons rules, from the file the agent reads, without its frontmatter."""
    try:
        text = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "mesh-lessons.md")).read()
    except OSError:
        return "<p class='meta'>The lessons file is not in this install.</p>"
    if text.startswith("---"):
        text = text.split("---", 2)[-1]
    out = []
    for para in [p.strip() for p in text.split("\n\n") if p.strip()]:
        if para.startswith("#"):
            continue
        h = e(" ".join(para.split()))
        h = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", h)
        h = re.sub(r"`([a-z_]+)`", r"<code>\1</code>", h)
        out.append(f"<p>{h}</p>")
    return "".join(out)


def help_body(st, cfg, reg, shelf, declared_region):
    st, cfg, reg, shelf = st or {}, cfg or {}, reg or {}, shelf or {}
    own = st.get("own") or {}
    rows = reg.get("rows", [])
    managed = [r for r in rows if r.get("managed")]
    fleet = "".join(f"<tr><td><b>{e(str(r.get('label') or r.get('name') or r.get('id')))}</b><div class='sub'>{e(str(r.get('id') or ''))} · {e(str(r.get('name') or ''))}</div></td>"
                    f"<td>{e(str(r.get('holder') or ''))}</td><td>{e(str(r.get('hw') or ''))}<div class='sub'>{e(str(r.get('firmware') or ''))}</div></td>"
                    f"<td>{'<span class=pill>managed</span>' if r.get('managed') else '<span class=sub>not managed</span>'}</td></tr>" for r in rows) or "<tr><td colspan=4 class='meta'>No device in the register yet.</td></tr>"
    radio_region = cfg.get("region") or st.get("region")
    if declared_region and radio_region and declared_region != radio_region:
        region = (f"<p class='warn'><b>The radio's region ({e(str(radio_region))}) does not match the region the installer declared ({e(str(declared_region))}).</b> "
                  "One of them is wrong for where the kit is. Changing the radio's region moves it to another band: every device on the old band stops hearing it until it is changed too, so change the fleet on the bench first, then this radio from the Radio page, with the confirm.</p>")
    else:
        region = f"<p class='ok'>The radio is on <b>{e(str(radio_region or '?'))}</b>, the region the installer declared. Before the kit travels to another country, the whole fleet and this radio move together: the devices on the bench first, this radio last.</p>"
    pins = "".join(f"<tr><td><b>{e(str(i.get('version') or ''))}</b>{' <span class=pill>recommended</span>' if i.get('recommended') else ''}{' <span class=pill>recovery</span>' if str(i.get('version') or '').startswith('erase') or 'factory' in str(i.get('id') or '') else ''}<div class='sub'>{e(', '.join(i.get('hw') or []))} · {e(str(i.get('method') or ''))}</div></td>"
                   f"<td><span class='{ {'verified': 'ok', 'wrong': 'bad', 'missing': 'warn'}.get(i.get('state') or 'missing', '') }'>{e(str(i.get('state') or 'missing'))}</span></td><td class='meta'>{e(str(i.get('note') or ''))}</td></tr>" for i in shelf.get("images", [])) or "<tr><td colspan=3 class='meta'>No pins in this release.</td></tr>"
    states = ("<table><thead><tr><th>What the line says</th><th>What it means</th></tr></thead><tbody>"
              "<tr><td>sent hh:mm, waiting for the read-back</td><td>The write went to the device; nothing is shown as true until the device itself answers.</td></tr>"
              "<tr><td>written and read back at hh:mm</td><td>The device's own answer matched what was written. This is the only state that means done.</td></tr>"
              "<tr><td>unconfirmed: sent hh:mm, the radio reports …</td><td>The device answered with something else. What it reports is what it holds, whatever was sent.</td></tr>"
              "<tr><td>asked, no answer yet (sent hh:mm)</td><td>No answer in the window. Over LoRa that is slow and lossy, not failed: read the device again later.</td></tr>"
              "<tr><td>not written: …</td><td>Refused before anything was sent, and the reason.</td></tr></tbody></table>")
    where = (f"<p class='meta'>Units <code>mesh-manager-bridge</code> (owns the radio, forwards to TAK) and <code>mesh-manager-web</code> (this screen). The bridge answers on <code>{e(str(st.get('socket') or ''))}</code>; "
             f"its state, the register, the exports and the firmware shelf live under <code>{e(str(st.get('state_dir') or ''))}</code>. When something is wrong: <code>journalctl -u mesh-manager-bridge -n 200</code>. "
             "A radio in bootloader mode presents a serial port and answers nothing; the bridge waits rather than restarting, and the Bench page names the recovery step.</p>")
    return (f"<h2 style='margin-top:0'>The kit</h2><div class='cards'>{card('This radio', e(str(own.get('name') or '?')) + ' <span class=pill>' + e(str(own.get('id') or '')) + '</span>')}"
            f"{card('Rides', e(str(st.get('region') or '?')) + ' · ' + e(str(st.get('modem_preset') or '?')) + '<div class=meta>primary ' + e(str(st.get('primary_channel') or '?')) + '</div>')}"
            f"{card('The fleet', str(len(rows)) + ' device' + ('s' if len(rows) != 1 else '') + '<div class=meta>' + str(len(managed)) + ' managed by this radio</div>')}"
            f"{card('The radio is at', e(str(st.get('radio') or '?')))}</div>"
            f"<div class='tablewrap' style='margin-top:1rem'><table><thead><tr><th>Device</th><th>Holder</th><th>Hardware · firmware</th><th>Managed</th></tr></thead><tbody>{fleet}</tbody></table></div>"
            f"<h2>Before the kit travels</h2>{region}"
            f"<h2>The shelf</h2><p class='meta'>Firmware the fleet may carry, pinned in this release; recovery images are the way back when a device will not boot.</p><div class='tablewrap'><table><thead><tr><th>Image</th><th>On this box</th><th>Note</th></tr></thead><tbody>{pins}</tbody></table></div>"
            f"<h2>What goes wrong</h2><p class='meta'>The same rules the connected agent reads; every one was paid for on a real mesh.</p>{lessons_html()}"
            f"<h2>The four states of a write</h2>{states}<h2>Where things are</h2>{where}")


def update_box(web):
    rec = U.last_check(web.state_dir)
    mode = web.update_mode()
    tok = bool(web.github_token())
    if not rec:
        last = "never checked" if tok else "never checked; no GitHub token yet (Settings)"
    elif rec.get("error"):
        last = f"checked {hhmm(rec.get('checked'))}: {rec['error']}"
    elif rec.get("available"):
        last = f"checked {hhmm(rec.get('checked'))}: <b>{e(str(rec.get('version')))} available</b> ({e(str(rec.get('tag') or ''))}, published {e(str(rec.get('published') or '')[:10])})"
    else:
        last = f"checked {hhmm(rec.get('checked'))}: up to date ({e(str(rec.get('version') or __version__))} is the newest on {e(str(rec.get('channel') or ''))})"
    notes = ""
    if rec.get("available") and rec.get("notes"):
        notes = f"<details class='fold ctl'><summary>What is in {e(str(rec.get('version')))}</summary><pre class='log' style='max-height:40vh'>{e(str(rec['notes']))}</pre></details>"
    apply_btn = (f"<button type='button' data-update-apply='{e(str(rec.get('version')))}'>Update now to {e(str(rec.get('version')))}</button>" if rec.get("available") else "")
    log = U.last_log(web.state_dir)
    logbox = f"<details class='fold ctl'><summary>The last update's log</summary><pre class='log' style='max-height:40vh'>{e(chr(10).join(log))}</pre></details>" if log else ""
    return (f"<div class='card' id='update-box'><div class='k'>Updates from GitHub · {e(mode)}</div><div class='v'>{last}</div>"
            f"<p class='meta'>Running {e(__version__)}. {e(str(rec.get('repo') or web.config.get('UPDATE_REPO') or U.DEFAULT_REPO))}, channel {e(str(rec.get('channel') or web.config.get('UPDATE_CHANNEL') or 'prerelease'))}. "
            "Update now fetches the release, checks its hash, then the box installs it with the settings it already has; the bridge and this screen restart, so the mesh is off TAK for about a minute.</p>"
            f"<div class='row-actions'><button type='button' class='line' data-update-check>Check now</button>{apply_btn}</div><div class='res meta' id='update-res' role='status'></div>{notes}{logbox}</div>")


UPDATE_JS = r"""<script>
(function(){
  function res(t,c){var r=document.getElementById('update-res');if(r){r.textContent=t;r.className='res meta '+(c||'');}}
  var chk=document.querySelector('[data-update-check]');if(chk){chk.addEventListener('click',function(){chk.disabled=true;res('asking GitHub');
    fetch('/api/update/check',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}).then(function(r){return r.json();}).then(function(j){chk.disabled=false;
      if(j.error){res('checked: '+j.error,'warn');return;}res(j.available?(j.version+' is available; the page will show it'):('up to date: '+j.version+' is the newest'),'ok');setTimeout(function(){window.location.href='/about';},1200);})
    .catch(function(){chk.disabled=false;res('could not ask the box','bad');});});}
  var ap=document.querySelector('[data-update-apply]');if(ap){ap.addEventListener('click',function(){var v=ap.dataset.updateApply;
    if(!confirm('Update to '+v+'? The release is downloaded and checked, then the bridge and this screen restart; the mesh is off TAK for about a minute.'))return;
    ap.disabled=true;res('downloading and checking '+v);
    fetch('/api/update/apply',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({version:v})}).then(function(r){return r.json();}).then(function(j){
      if(j.error){res('not applied: '+j.error,'bad');ap.disabled=false;return;}res('installing '+v+'; the screen will come back on the new version','warn');
      var t0=Date.now();(function poll(){setTimeout(function(){fetch('/healthz').then(function(r){return r.json();}).then(function(h){if(h.version&&h.version!==j.running){window.location.href='/about';}else if(Date.now()-t0<600000){poll();}else{res('the screen is back but still on '+h.version+': read the last update log below','bad');}}).catch(function(){if(Date.now()-t0<600000){poll();}else{res('the screen did not come back in ten minutes: ssh to the box and read journalctl -u mesh-manager-update','bad');}});},3000);})();})
    .catch(function(){res('the box went away mid-request; it may be restarting','warn');});});}
})();
</script>"""


def series_chart(pts, key, unit="", lo=None, hi=None, guides=(), label=""):
    """A line over time from history rows (ts, key), drawn server-side; guides are (value, class) lines."""
    pts = [p for p in (pts or []) if p.get(key) is not None]
    if len(pts) < 2:
        return f"<p class='meta'>Not enough readings yet for a chart of {e(label or key)}; the box records each one it hears.</p>"
    vals = [float(p[key]) for p in pts]
    lo = min(vals) if lo is None else lo
    hi = max(vals) if hi is None else hi
    if hi <= lo:
        hi = lo + 1
    w, ht = 600, 120
    t0 = time.mktime(time.strptime(pts[0]["ts"], "%Y-%m-%dT%H:%M:%SZ")); t1 = time.mktime(time.strptime(pts[-1]["ts"], "%Y-%m-%dT%H:%M:%SZ"))
    span = max(1.0, t1 - t0)
    def x(ts): return 30 + (time.mktime(time.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")) - t0) / span * (w - 40)
    def y(v): return ht - 18 - (float(v) - lo) / (hi - lo) * (ht - 30)
    line = " ".join(f"{x(p['ts']):.1f},{y(p[key]):.1f}" for p in pts)
    g = "".join(f"<line x1='30' x2='{w - 10}' y1='{y(v):.1f}' y2='{y(v):.1f}' class='{cls}'/><text x='2' y='{y(v) + 4:.1f}'>{v:g}{e(unit)}</text>" for v, cls in guides if lo <= v <= hi)
    return (f"<svg class='chart' viewBox='0 0 {w} {ht}' role='img' aria-label='{e(label or key)} over time'><title>{e(label or key)} over time</title>{g}"
            f"<text x='2' y='{y(hi) + 4:.1f}'>{hi:g}{e(unit)}</text><text x='2' y='{y(lo) + 4:.1f}'>{lo:g}{e(unit)}</text>"
            f"<polyline points='{line}'/><text x='30' y='{ht - 4}'>{e(pts[0]['ts'][5:16].replace('T', ' '))}Z</text><text x='{w - 110}' y='{ht - 4}'>{e(pts[-1]['ts'][5:16].replace('T', ' '))}Z</text></svg>")


def node_body(n, tel, msgs, npos, hours):
    """Spec 025: one node, its facts, its battery and voltage over time, its last messages."""
    pos = (f"{n['lat']:.5f}, {n['lon']:.5f} · {MG.mgrs(n['lat'], n['lon'], 4) or ''}".rstrip(" ·") if n.get("lat") is not None and n.get("lon") is not None else "no fix")
    heard = n.get("heard") or n.get("last_heard_db")
    facts = (card("Node", f"{e(dname(n))}<div class='sub'>{e(n.get('id') or '')}{(' · ' + e(str(n.get('name')))) if n.get('label') and n.get('name') else ''}</div>")
             + card("Hardware", e(str(n.get("hw") or "unknown")))
             + card("Position", e(pos))
             + card("Heard", f"<time datetime='{e(str(heard))}' data-age>{e(age(heard))}</time>" if heard else "never")
             + card("Positions in the window", str(npos)))
    levels = [dict(r, level=(None if r.get("level") is None else (100 if int(r["level"]) > 100 else int(r["level"])))) for r in tel]
    charging = [r["ts"][11:16] for r in tel if r.get("level") is not None and int(r["level"]) > 100]
    sel = "".join(f"<option value='{h}'{' selected' if h == hours else ''}>{t}</option>" for h, t in ((24, "24 h"), (168, "7 d")))
    form = (f"<form method='get' action='/node' class='controls'><input type='hidden' name='id' value='{e(n.get('id') or '')}'><label class='meta'>window <select name='hours' onchange='this.form.submit()'>{sel}</select></label></form>")
    rows = "".join(f"<tr><td class='meta'><time datetime='{e(m['ts'])}' data-age>{e(age(m['ts']))}</time></td><td>{e('everyone' if str(m.get('dest') or '') == '^all' else str(m.get('dest') or ''))}</td><td>{e(str(m.get('text') or ''))}</td></tr>" for m in msgs[-20:])
    return (f"<div class='cards'>{facts}</div>{form}"
            f"<h2>Battery</h2>{series_chart(levels, 'level', '%', 0, 100, ((20, 'bad'),), 'battery')}"
            + (f"<p class='meta'>On charge at {e(', '.join(charging[-6:]))}{' and earlier' if len(charging) > 6 else ''} (shown as 100%).</p>" if charging else "")
            + f"<h2>Voltage</h2>{series_chart(tel, 'voltage', ' V', None, None, ((3.3, 'bad'),), 'voltage')}"
            f"<h2>Last messages</h2><div class='tablewrap'><table><thead><tr><th>When</th><th>To</th><th>Message</th></tr></thead><tbody>{rows or '<tr><td colspan=3 class=meta>None in the window.</td></tr>'}</tbody></table></div>")


def health_chart(h):
    """Channel utilisation by the hour, the 25 and 40 percent lines drawn."""
    pts = h.get("hourly") or []
    if len(pts) < 2:
        return "<p class='meta'>Not enough of the gateway's own telemetry yet for a chart; it reports every few minutes.</p>"
    w, ht = 600, 120
    top = max(45.0, max(float(p.get("chutil") or 0) for p in pts) * 1.1)
    def x(i): return 30 + i * (w - 40) / max(1, len(pts) - 1)
    def y(v): return ht - 18 - float(v) / top * (ht - 30)
    line = " ".join(f"{x(i):.1f},{y(p.get('chutil') or 0):.1f}" for i, p in enumerate(pts))
    guides = "".join(f"<line x1='30' x2='{w - 10}' y1='{y(v):.1f}' y2='{y(v):.1f}' class='{cls}'/><text x='2' y='{y(v) + 4:.1f}'>{v}%</text>" for v, cls in ((25, "warn"), (40, "bad")))
    first, last = pts[0]["hour"][11:16], pts[-1]["hour"][11:16]
    return (f"<svg class='chart' viewBox='0 0 {w} {ht}' role='img' aria-label='channel utilisation by the hour'><title>channel utilisation by the hour</title>{guides}"
            f"<polyline points='{line}'/><text x='30' y='{ht - 4}'>{e(first)}Z</text><text x='{w - 40}' y='{ht - 4}'>{e(last)}Z</text></svg>")


def health_cards(h):
    if not h or "error" in h:
        return f"<p class='bad'>{e(str((h or {}).get('error') or 'no answer'))}</p>"
    v = h.get("verdict") or "unknown"
    cls = {"quiet": "ok", "normal": "ok", "busy": "warn", "saturated": "bad"}.get(v, "")
    ch = h.get("chutil"); air = h.get("airutil"); bud = h.get("budget_pct")
    air_txt = ("no reading" if air is None else f"{float(air):.1f}%") + (f"<div class='meta'>of a {bud:g}% budget on {e(h.get('region') or '')}: {h.get('air_share') if h.get('air_share') is not None else '?'}% used</div>" if bud else f"<div class='meta'>no duty-cycle limit on {e(h.get('region') or 'this region')}</div>")
    cards = (card("Channel utilisation", (f"{float(ch):.1f}%" if ch is not None else "no reading") + f" <span class='pill'>{e(v)}</span>", cls)
             + card("Gateway air time (transmit)", air_txt, "bad" if (h.get("air_share") or 0) >= 80 else ("warn" if (h.get("air_share") or 0) >= 50 else ""))
             + card("Packets per hour", f"{h.get('packets_per_hour', 0)}<div class='meta'>{h.get('packets', 0)} in {h.get('hours')} h</div>")
             + card("Nodes heard", f"{h.get('nodes_heard', 0)}<div class='meta'>in the window</div>"))
    rows = ""
    for d in h.get("nodes") or []:
        rows += (f"<tr><td><b>{e(d.get('name') or d.get('id'))}</b>{' <span class=pill>this radio</span>' if d.get('own') else ''}<div class='sub'>{e(d.get('id') or '')}</div></td>"
                 f"<td>{d.get('packets', 0)}</td><td>{d.get('per_hour', 0)}</td>"
                 f"<td>{('%.1f%%' % float(d['chutil'])) if d.get('chutil') is not None else '<span class=sub>none</span>'}</td>"
                 f"<td>{('%.2f%%' % float(d['airutil'])) if d.get('airutil') is not None else '<span class=sub>none</span>'}</td>"
                 f"<td>{(str(int(d['battery'])) + '%') if d.get('battery') is not None and 0 <= int(d['battery']) <= 100 else ('on charge' if d.get('battery') is not None and int(d['battery']) > 100 else '<span class=sub>none</span>')}</td>"
                 f"<td class='meta'>{('<time datetime=' + chr(39) + e(d['last_telemetry']) + chr(39) + ' data-age>' + e(age(d['last_telemetry'])) + '</time>') if d.get('last_telemetry') else 'none'}</td></tr>")
    return (f"<div class='cards'>{cards}</div><h2>Channel utilisation by the hour</h2>{health_chart(h)}"
            "<h2>Per node</h2><p class='meta'>Packets the gateway heard from each node in the window, and the last device metrics each reported. Utilisation is the share of air time the node's radio hears busy; air time is the share it spends transmitting.</p>"
            "<div class='tablewrap'><table><thead><tr><th>Node</th><th>Packets</th><th>Per hour</th><th>Utilisation</th><th>Air time</th><th>Battery</th><th>Reported</th></tr></thead>"
            f"<tbody>{rows or '<tr><td colspan=7 class=meta>Nothing in the window yet.</td></tr>'}</tbody></table></div>")


ROTATION_JS = "<script>(function(){var prev=window.onMesh||function(){};window.onMesh=function(d){prev(d);if(d.kind==='packet'||d.kind==='rotation'){window.mmFrag('rotation','rotation-body');}};})();</script>"


def rotation_section(rs):
    """Spec 027: since the key rotation, who is back and who is not."""
    rs = rs or {}
    m = _act("rotation_mark")
    form = (f"<details class='fold ctl' style='margin-top:var(--s2)'><summary>Mark a rotation done elsewhere</summary><form data-action='rotation_mark' data-risk='change' data-confirm=\"{e(m.get('confirm') or '')}\" data-refresh='rotation:rotation-body'>"
            f"<p class='meta'>{e(m['description'])}</p><label>Slot <input type='number' name='index' value='0' min='0' max='7' style='width:5em'></label> <label>Note <input type='text' name='note' maxlength='120' placeholder='e.g. rotated in the app'></label> <button class='line'>Mark it</button><div class='res meta' role='status'></div></form></details>")
    rot = rs.get("rotation")
    if not rot:
        return f"<h2 id='rotation'>Since the key rotation</h2><p class='meta'>No rotation marked on this box. A rotation from this screen marks itself; one done elsewhere is marked below, and the checklist then counts every device back on the new key.</p>{form}"
    c = rs.get("counts") or {}
    back = "".join(f"<tr><td><b>{e(b.get('name'))}</b><div class='sub'>{e(b.get('id'))}</div></td><td class='ok'>back</td><td class='meta'><time datetime='{e(b.get('heard'))}' data-age>{e(age(b.get('heard')))}</time></td></tr>" for b in rs.get("back") or [])
    wait = "".join(f"<tr><td><b>{e(w.get('name'))}</b><div class='sub'>{e(w.get('id'))}</div></td><td class='warn'>waiting</td><td class='meta'>not heard since the rotation</td></tr>" for w in rs.get("waiting") or [])
    return (f"<h2 id='rotation'>Since the key rotation</h2><p class='meta'>Slot {int(rot.get('index') or 0)}{(' (' + e(rot.get('name')) + ')') if rot.get('name') else ''}, {e(rot.get('source') or '')} <time datetime='{e(rot.get('ts'))}' data-age>{e(age(rot.get('ts')))}</time>{(': ' + e(rot.get('note'))) if rot.get('note') else ''}. "
            f"<b>{int(c.get('back') or 0)} of {int(c.get('expected') or 0)} back</b>, {int(c.get('waiting') or 0)} waiting. A device is back when this radio hears any packet from it, because a packet it can decode carries the new key.</p>"
            f"<div class='tablewrap'><table><thead><tr><th>Device</th><th>State</th><th>First heard after</th></tr></thead><tbody>{wait}{back or ''}{'' if (wait or back) else '<tr><td colspan=3 class=meta>Nobody was expected.</td></tr>'}</tbody></table></div>{form}")


def alerts_section(al):
    """Spec 026: what is open, what was, the thresholds, and the test."""
    al = al or {}
    st = al.get("settings") or {}
    kinds = {"silent": "warn", "battery": "warn", "unknown": "bad", "fence": "bad"}
    open_rows = "".join(f"<tr><td><span class='pill' style='background:var(--{kinds.get(o.get('kind'), 'warn')});color:#fff;border-color:transparent'>{e(o.get('kind'))}</span></td><td>{e(o.get('text'))}</td><td class='meta'><time datetime='{e(o.get('since'))}' data-age>{e(age(o.get('since')))}</time></td></tr>" for o in al.get("open") or [])
    recent = "".join(f"<tr><td class='meta'><time datetime='{e(r.get('ts'))}' data-age>{e(age(r.get('ts')))}</time></td><td>{e(r.get('kind'))}</td><td>{e(r.get('text'))}</td><td class='meta'>{'cleared ' + e(hhmm(r.get('cleared'))) if r.get('state') == 'cleared' else 'open'}</td></tr>" for r in list(reversed(al.get("recent") or []))[:20])
    a = _act("alert_set"); t = _act("alert_test")
    def opt(name, on):
        return f"<option value='on'{' selected' if on else ''}>on</option><option value='off'{'' if on else ' selected'}>off</option>"
    form = (f"<form data-action='alert_set' class='card' data-risk='change' data-confirm=\"{e(a.get('confirm') or '')}\" style='max-width:720px'><h2 style='margin-top:0'>Thresholds</h2><p class='meta'>{e(a['description'])}</p>"
            f"<div class='regform' style='grid-template-columns:1fr 1fr 1fr'><label>Silent after (min)<input type='number' name='silent_min' value='{int(st.get('silent_min', 30))}' min='1' max='1440'></label>"
            f"<label>Battery under (%)<input type='number' name='battery_pct' value='{int(st.get('battery_pct', 20))}' min='1' max='90'></label>"
            f"<label>Fence (m, 0 off)<input type='number' name='fence_m' value='{int(st.get('fence_m', 0))}' min='0' max='100000'></label>"
            f"<label>Unknown nodes<select name='unknown'>{opt('unknown', st.get('unknown', True))}</select></label>"
            f"<label>To TAK chat<select name='to_tak'>{opt('to_tak', st.get('to_tak', True))}</select></label>"
            "<div></div></div><button class='line' style='margin-top:var(--s2)'>Save</button><div class='res meta' role='status'></div></form>")
    test = (f"<form data-action='alert_test' style='display:inline-block;margin-top:var(--s2)'><button class='quiet' title='{e(t['description'])}'>{e(t['title'])}</button><div class='res meta' role='status'></div></form>")
    return (f"<h2 id='alerts'>Alerts</h2><p class='meta'>A registered device gone quiet, a battery under the threshold, a node not in the register, a node outside the fence. Each is shown here and sent to All Chat Rooms on the TAK Server when To TAK chat is on.</p>"
            f"<div class='tablewrap'><table><thead><tr><th>Open</th><th>What</th><th>Since</th></tr></thead><tbody>{open_rows or '<tr><td colspan=3 class=meta>Nothing open.</td></tr>'}</tbody></table></div>"
            f"<h2>Recent</h2><div class='tablewrap'><table><thead><tr><th>When</th><th>Kind</th><th>What</th><th>State</th></tr></thead><tbody>{recent or '<tr><td colspan=4 class=meta>None yet.</td></tr>'}</tbody></table></div>"
            f"{form}{test}")


def health_body(h, al=None):
    js = "<script>window.onMesh=function(d){if(d.kind==='status'){window.mmFrag('health','health-body');}if(d.kind==='alert'){window.mmFrag('alerts','alerts-body');}};</script>"
    return ("<p class='meta'>How busy the mesh is, from the history store. On LoRa the channel utilisation is the number that says whether the mesh is about to fall over: under 10% is quiet, under 25% normal, under 40% busy, above that saturated. On EU_868 the gateway's own transmit air time must stay under the 10% duty-cycle limit.</p>"
            f"<div id='health-body'>{health_cards(h)}</div><div id='alerts-body'>{alerts_section(al)}</div>{js}{WRITE_JS}")


def history_box(web):
    """Spec 020: what the box remembers, and for how long."""
    try:
        h = web.client.ask("history_summary")
    except Exception:  # noqa: BLE001
        h = None
    if not isinstance(h, dict) or not h.get("ok"):
        return "<h2>History</h2><p class='meta'>The history store is not available on this box.</p>"
    t = h.get("tables") or {}
    rows = "".join(f"<tr><td>{e(k)}</td><td>{int(v.get('rows') or 0)}</td><td class='meta'>{e(str(v.get('oldest') or ''))}</td><td class='meta'>{e(str(v.get('newest') or ''))}</td></tr>" for k, v in t.items())
    mb = (h.get("bytes") or 0) / 1048576
    return (f"<h2>History</h2><p class='meta'>What the box has heard, kept {int(h.get('days') or 30)} days at {e(h.get('path') or '')} ({mb:.1f} MB), so trails, telemetry and messages survive a restart.</p>"
            f"<div class='tablewrap'><table><thead><tr><th>Table</th><th>Rows</th><th>Oldest</th><th>Newest</th></tr></thead><tbody>{rows}</tbody></table></div>")


def bytesize(n):
    """A size a person reads. A release tarball is about 21 MB, so KB alone is unreadable, and
    a small file must not round to '0 KB'."""
    n = int(n or 0)
    if n >= 1 << 20:
        return f"{n / (1 << 20):.1f} MB"
    if n >= 1024:
        return f"{n // 1024} KB"
    return f"{n} bytes"


def rollback_box(web):
    """Spec 030: the releases still on the box, each one press away. A bad release arrives in
    ten seconds, so the way back should not be an SSH session."""
    rows = U.staged(web.state_dir, arch=web.arch, running=__version__)
    back = [r for r in rows if not r.get("running")]
    if not back:
        return ("<div class='card' id='rollback-box'><div class='k'>Roll back</div>"
                "<div class='v'>nothing to roll back to</div>"
                "<p class='meta'>A roll back re-applies a release this box has already taken, from what is still "
                "staged on disk. Only the running version is here, so there is nothing to return to yet.</p></div>")
    auto = web.update_mode() == "auto"
    items = "".join(
        f"<div class='row-actions' style='justify-content:space-between'>"
        f"<span><b>{e(r['version'])}</b> <span class='meta'>· staged <time datetime='{e(str(r.get('staged') or ''))}' data-age>{e(age(str(r.get('staged') or '')))}</time>"
        f" · {e(bytesize(r['size']))}</span></span>"
        f"<button type='button' class='line' data-rollback='{e(r['version'])}'>Roll back to {e(r['version'])}</button></div>"
        for r in back)
    warn = ("<p class='meta' style='color:var(--warn)'>Updates are on <b>auto</b>, so the checker will apply the newest "
            "release again within the day. Put updates on manual in Settings if a roll back is to stand.</p>") if auto else ""
    return (f"<div class='card' id='rollback-box'><div class='k'>Roll back</div><div class='v'>{len(back)} release"
            f"{'s' if len(back) != 1 else ''} still on this box</div>"
            "<p class='meta'>Re-applies a release the box already has, checking its hash first. It returns the "
            "<b>code</b> and not the box's config, which the installer keeps either way. The bridge and this screen "
            "restart, so the mesh is off TAK for about a minute.</p>"
            f"{warn}{items}<div class='res meta' id='rollback-res' role='status'></div></div>")


ROLLBACK_JS = r"""<script>
(function(){
  function res(t,c){var r=document.getElementById('rollback-res');if(r){r.textContent=t;r.className='res meta '+(c||'');}}
  document.querySelectorAll('[data-rollback]').forEach(function(b){b.addEventListener('click',function(){
    var v=b.getAttribute('data-rollback');
    if(!window.confirm('Roll back to '+v+'? The bridge and this screen restart, so the mesh is off TAK for about a minute. This returns the code, not the box\'s settings.'))return;
    b.disabled=true;res('rolling back to '+v);
    fetch('/api/update/rollback',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({version:v})})
      .then(function(r){return r.json();}).then(function(j){
        if(j.error){b.disabled=false;res(j.error,'bad');return;}
        res('rolling back to '+v+(j.warning?(' — '+j.warning):'')+'; waiting for the box to come back');
        var t0=Date.now();(function poll(){setTimeout(function(){
          fetch('/healthz').then(function(r){return r.json();}).then(function(h){
            if(h.version&&h.version===v){window.location.href='/about';}
            else if(Date.now()-t0<600000){poll();}
            else{res('the screen is back but on '+h.version+': read the last update log below','bad');}
          }).catch(function(){if(Date.now()-t0<600000){poll();}else{res('the screen did not come back in ten minutes: read journalctl -u mesh-manager-update on the box','bad');}});},3000);})();})
      .catch(function(){b.disabled=false;res('could not ask the box','bad');});});});
})();
</script>"""


def about_body(st, web):
    return (f"{update_box(web)}{rollback_box(web)}{UPDATE_JS}{ROLLBACK_JS}<div class='cards' style='margin-top:1rem'>{card('Mesh Manager', e(__version__))}{card('Bridge', e(str(st.get('version') or 'not answering')))}"
            f"{card('Licence', 'GPL-3.0-or-later')}{card('Health contract', e(st.get('state_dir') or '/var/lib/vantage-mesh') + '/heartbeat.json')}"
            f"{card('Bridge socket', e(st.get('socket') or web.client.socket_path))}{card('Screen bound to', e(web.bind[0]) + ':' + str(web.bind[1]))}</div>"
            f"{history_box(web)}"
            "<h2>Third-party work</h2><p class='meta'>TAK-Meshtastic-Gateway (OpenTAKServer / brian7704) and the Meshtastic Python library and protobufs, GPL-3.0-or-later; "
            "the TAKPacket-SDK dictionaries (Meshtastic), GPL-3.0-or-later. Attributions and licence texts travel in the release under LICENSES/.</p>")


def seed_messages(web):
    """Spec 020: after a restart the deque is empty; the history store still has the last 200."""
    if web.messages:
        return
    try:
        rep = web.client.ask("history", kind="messages", limit=200)
        rows = (rep or {}).get("rows") if isinstance(rep, dict) else None
    except Exception:  # noqa: BLE001
        rows = None
    for r in rows or []:
        web.messages.append({"ts": r.get("ts"), "from": r.get("node"), "name": r.get("name") or r.get("node"), "to": r.get("dest"),
                             "channel": r.get("channel"), "text": r.get("text"), "stored": True})


def message_rows(web, labels=None):
    seed_messages(web)
    rows = ""
    labels = labels or {}
    for m in list(web.messages):
        who = labels.get(str(m.get("from") or "")) or str(m.get("name") or m.get("from") or "")
        ts = str(m.get("ts") or "")
        when = f"<time datetime='{e(ts)}' data-age>{e(age(ts))}</time>" if ts else ""
        state = " <span class='pill'>acknowledged</span>" if m.get("acked") else (f" <span class='pill'>handed to the radio {e(hhmm(ts))}</span>" if m.get("sent") else "")
        rows += (f"<tr><td class='meta'>{when}</td><td>{e(who)}{state}</td>"
                 f"<td>{e('everyone' if str(m.get('to') or '') == '^all' else str(m.get('to') or ''))}</td><td>{e(str(m.get('text') or ''))}</td></tr>")
    return rows or "<tr><td colspan=4 class='meta'>Nothing heard on the channels since the bridge started.</td></tr>"


def messages_body(web, nodes, chans=None, st=None):
    send = _act("send_text")
    live = [c for c in (chans or []) if c.get("role") != "DISABLED"] or [{"index": 0, "name": (st or {}).get("primary_channel") or "primary", "role": "PRIMARY"}]
    heard = [n for n in nodes if n.get("heard_here", True)]
    ch_opts = "".join(f"<option value='{int(c.get('index', 0))}'>{e(c.get('name') or 'slot ' + str(c.get('index')))}{' (primary)' if c.get('role') == 'PRIMARY' else ''}</option>" for c in live)
    node_opts = "".join(f"<option value='{e(n['id'])}'>{e(dname(n))} ({e(n['id'])}){'' if n.get('heard_here', True) else ' · database only'}</option>" for n in nodes if n.get("id"))
    form = (f"<form id='send' class='card' data-action='send_text' data-clear='1' data-refresh='messages:msg-rows' data-heard='{len(heard)}' "
            "data-confirm-channel='Send to everyone on {channel}, {count} devices heard here: “{text}”' "
            "data-confirm-direct='Send only to {node}: “{text}”. No one else on the mesh sees it.'>"
            f"<h2 style='margin-top:0'>{e(send['title'])}</h2><p class='meta'>{e(send['description'])}</p>"
            "<label>Message (200 bytes at most)<input type='text' name='text' maxlength='200' required></label>"
            f"<label>Channel<select name='channel'>{ch_opts}</select></label>"
            f"<label>To<select name='to'><option value='^all'>everyone on the channel</option>{node_opts}</select></label>"
            "<button type='submit'>Send</button><div class='res meta' role='status'></div></form>")
    js = r"""<script>
(function(){var f=document.getElementById('send');
  f.addEventListener('submit',function(ev){ev.preventDefault();ev.stopImmediatePropagation();
    var chSel=f.elements.channel,toSel=f.elements.to,text=f.elements.text.value;
    var t=toSel.value==='^all'?f.dataset.confirmChannel.replace('{channel}',chSel.options[chSel.selectedIndex].text).replace('{count}',f.dataset.heard):f.dataset.confirmDirect.replace('{node}',toSel.options[toSel.selectedIndex].text);
    if(!confirm(t.replace('{text}',text)))return;
    var res=f.querySelector('.res');
    fetch('/api/send_text',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text:text,channel:parseInt(chSel.value,10),to:toSel.value})})
      .then(function(r){return r.json().then(function(j){return [r.status,j];});})
      .then(function(x){if(x[0]>=400){res.textContent='not sent: '+(x[1].error||x[0]);res.className='res meta bad';}else{res.textContent='handed to the radio at '+window.mmNow();res.className='res meta ok';f.elements.text.value='';window.mmFrag('messages','msg-rows');}})
      .catch(function(){res.textContent='could not ask the box';res.className='res meta bad';});},true);
  window.onMesh=function(d){if(d.kind==='text'||d.kind==='ack'){window.mmFrag('messages','msg-rows');}};})();
</script>"""
    labels = {str(n.get("id")): str(n.get("label") or "") for n in nodes if n.get("label")}
    return (f"<div class='tablewrap'><table><thead><tr><th>When</th><th>From</th><th>To</th><th>Message</th></tr></thead><tbody id='msg-rows'>{message_rows(web, labels)}</tbody></table></div><br>{form}{js}")


def radio_body(cfg, own_id="?"):
    if not cfg or "long_name" not in cfg:
        return "<p class='warn'>The radio's configuration is not readable yet.</p>"
    v = lambda k: e(str(cfg.get(k) if cfg.get(k) is not None else ""))
    rs, rr = _act("radio_set"), _act("radio_set_region")
    settings = (f"<form class='card' data-action='radio_set' data-risk='change' data-confirm=\"{e(rs['confirm'])}\">"
                f"<h2 style='margin-top:0'>{e(rs['title'])}</h2><p class='meta'>Each is written to the radio and shown here only once the radio has answered with it.</p>"
                f"<label>Long name<input type='text' name='long_name' value='{v('long_name')}' maxlength='39'></label>"
                f"<label>Short name (4 bytes)<input type='text' name='short_name' value='{v('short_name')}' maxlength='4'></label>"
                f"<label>TX power, dBm (0 = the region's maximum)<input type='number' name='tx_power' value='{v('tx_power')}' min='0' max='30'></label>"
                f"<label>Position broadcast, seconds<input type='number' name='position_broadcast_secs' value='{v('position_broadcast_secs')}' min='32' max='86400'></label>"
                "<button type='submit'>Write and read back</button><div class='res meta' role='status'></div></form>")
    def sel(name, values, cur):
        return f"<select name='{name}'><option value=''>leave as is ({e(str(cur))})</option>" + "".join(f"<option value='{x}'>{x}</option>" for x in values) + "</select>"
    ins = {i["name"]: i for i in rr["inputs"]}
    region = (f"<form class='card danger' data-action='radio_set_region' data-risk='unreachable' data-confirm=\"{e(rr['confirm'])}\">"
              f"<h2 style='margin-top:0'>{e(rr['title'])}</h2><p class='meta'>{e(rr['description'])}</p>"
              f"<label>Region{sel('region', ins['region']['values'], cfg.get('region'))}</label>"
              f"<label>Modem preset{sel('modem_preset', ins['modem_preset']['values'], cfg.get('modem_preset'))}</label>"
              f"<label>Role{sel('role', ins['role']['values'], cfg.get('role'))}</label>"
              f"<label class='check'><input type='checkbox' name='confirm_tick'><span>I understand: changing the region or preset moves this radio to another band; a fleet on the old setting will not hear it, and the radio reboots. This radio is {e(own_id)}.</span></label>"
              "<button type='submit' class='danger'>Write and read back</button><div class='res meta' role='status'></div></form>")
    return f"{read_line(cfg, '/radio')}<div class='cards'>{settings}{region}</div>{WRITE_JS}"


def proposal_form(pr):
    a = C.by_id(pr["action"]) or {"title": pr["action"], "confirm": "", "risk": "read", "inputs": []}
    args = pr.get("arguments") or {}
    fields = ""
    for i in a.get("inputs", []):
        if i["type"] == "confirm":
            continue
        val = args.get(i["name"], "")
        if i["type"] == "enum":
            fields += (f"<label>{e(i['name'])}<select name='{e(i['name'])}'><option value=''>not set</option>"
                       + "".join(f"<option value='{e(str(x))}'{' selected' if str(x) == str(val) else ''}>{e(str(x))}</option>" for x in i.get("values", [])) + "</select></label>")
        elif i["type"] == "int":
            fields += f"<label>{e(i['name'])}<input type='number' name='{e(i['name'])}' value='{e(str(val))}'></label>"
        elif i["type"] == "object":
            fields += f"<label>{e(i['name'])}<textarea name='{e(i['name'])}' rows='3'>{e(json.dumps(val) if val != '' else '')}</textarea></label>"
        else:
            fields += f"<label>{e(i['name'])}<input type='text' name='{e(i['name'])}' value='{e(str(val))}'></label>"
    risk = a.get("risk", "read")
    return (f"<form class='card proposal{' danger' if risk == 'unreachable' else ''}' data-action='{e(pr['action'])}' data-proposal='{e(pr['id'])}' data-risk='{e(risk)}' data-confirm=\"{e(a.get('confirm') or 'Run this now?')}\">"
            f"<div class='k'>{e(pr.get('who') or '')} proposes · <time datetime='{e(str(pr.get('created') or ''))}' data-age>{e(age(pr.get('created') or ''))}</time></div>"
            f"<div class='v'>{e(a.get('title', pr['action']))} <span class='pill'>{e(pr['action'])}</span></div><p>{e(pr.get('rationale') or '')}</p>{fields}"
            + ("<label class='check'><input type='checkbox' name='confirm_tick'><span>I understand the consequence named above; this radio is {own}.</span></label>".replace("{own}", "the one on this box") if risk == "unreachable" else "")
            + f"<div class='row-actions'><button type='submit'>Run as shown</button><button type='button' class='quiet' data-dismiss='{e(pr['id'])}'>Dismiss</button></div><div class='res meta' role='status'></div></form>")


def activity_body(web):
    props = K.proposals(web.etc_dir)
    forms = "".join(proposal_form(pr) for pr in props) or "<p class='meta'>Nothing pending. An agent at propose autonomy queues its requests here for you to run or dismiss.</p>"
    tail = K.audit_tail(web.etc_dir, 200)
    audit_rows = "".join(f"<tr><td class='meta'><time datetime='{e(str(x.get('ts') or ''))}' data-age>{e(age(x.get('ts') or ''))}</time></td><td>{e(str(x.get('who') or ''))}</td><td>{e(str(x.get('event') or ''))}</td>"
                         f"<td>{e(str(x.get('action') or x.get('name') or ''))}</td><td class='meta'>{e(json.dumps({k: v for k, v in x.items() if k not in ('ts', 'who', 'event', 'action')}, default=str)[:160])}</td></tr>"
                         for x in reversed(tail)) or "<tr><td colspan=5 class='meta'>No audit lines yet.</td></tr>"
    return (f"<h2>Proposals waiting for you</h2><p class='meta'>Each is the catalogue's own form, filled in by the agent; change a field before you run it if you like. Running it reads back from the radio like any other write.</p><div class='cards'>{forms}</div>"
            "<h2>Audit</h2><p class='meta'>Every agent call, proposal, run and dismissal, newest first, under the connection's name.</p>"
            f"<div class='tablewrap'><table><thead><tr><th>When</th><th>Who</th><th>Event</th><th>Action</th><th>Detail</th></tr></thead><tbody>{audit_rows}</tbody></table></div>{WRITE_JS}")


def connections_body(web, minted=None, msg=""):
    conns = K.list_connections(web.etc_dir)
    rows = ""
    for c in conns:
        if c.get("revoked"):
            ctl = f"revoked <time datetime='{e(str(c['revoked']))}' data-age>{e(age(c['revoked']))}</time>"
            aut = e(c["autonomy"])
        else:
            aut = (f"<form method='post' action='/connections/autonomy' data-name='{e(c['name'])}' class='row-actions' style='align-items:center'><input type='hidden' name='id' value='{e(c['id'])}'><input type='hidden' name='confirm' value=''>"
                   f"<select name='autonomy' style='width:auto;margin:0'>" + "".join(f"<option value='{a}'{' selected' if c['autonomy'] == a else ''}>{a}</option>" for a in C.AUTONOMY)
                   + "</select><button type='submit' class='line'>Change</button></form>")
            ctl = f"<form method='post' action='/connections/revoke' data-revoke='{e(c['name'])}'><input type='hidden' name='id' value='{e(c['id'])}'><button class='danger'>Revoke</button></form>"
        created = str(c.get("created") or "")
        used = c.get("last_used")
        rows += (f"<tr><td>{e(c['name'])}</td><td>{aut}</td><td class='meta'><time datetime='{e(created)}' data-age>{e(age(created))}</time></td>"
                 f"<td class='meta'>{('<time datetime=' + chr(39) + e(str(used)) + chr(39) + ' data-age>' + e(age(used)) + '</time>') if used else 'never'}</td><td>{ctl}</td></tr>")
    rows = rows or "<tr><td colspan=5 class='meta'>No connections yet.</td></tr>"
    shown = ""
    if minted:
        cmd = f"claude mcp add mesh-manager --transport http http://{web.bind[0]}:{web.bind[1]}/mcp --header \"Authorization: Bearer {minted['token']}\""
        shown = (f"<div class='card' style='border-color:var(--gold)'><div class='k'>Token for {e(minted['name'])} ({e(minted['autonomy'])}), shown once</div>"
                 f"<div class='v'><code>{e(minted['token'])}</code></div><div class='row-actions' style='margin:.5rem 0'><button type='button' class='line' data-copy='{e(minted['token'])}'>Copy the token</button>"
                 f"<button type='button' class='line' data-copy='{e(cmd)}'>Copy the claude mcp add command</button></div><p class='meta'>Connect with: <code>{e(cmd)}</code></p></div>")
    form = ("<form method='post' action='/connections' class='card' id='mint'><h2 style='margin-top:0'>Mint a connection</h2>"
            "<label>Name<input type='text' name='name' required maxlength='40'></label><input type='hidden' name='confirm' value=''>"
            "<label>Autonomy<select name='autonomy'><option value='observe'>observe: reads only</option><option value='propose' selected>propose: reads, on-air requests, and proposals for the rest</option><option value='act'>act: everything the screen can do</option></select></label>"
            "<button type='submit'>Mint</button></form>")
    js = r"""<script>
(function(){
  document.querySelectorAll('form[data-name]').forEach(function(f){f.addEventListener('submit',function(ev){var a=f.elements.autonomy.value;
    if(!confirm('Change '+f.dataset.name+' to '+a+'? '+(a==='act'?'At act it does everything this screen can do, without asking each time.':a==='propose'?'At propose it reads, asks on air, and queues the rest for you.':'At observe it only reads.'))){ev.preventDefault();return;}
    f.elements.confirm.value=f.dataset.name;});});
  document.querySelectorAll('form[data-revoke]').forEach(function(f){f.addEventListener('submit',function(ev){if(!confirm('Revoke '+f.dataset.revoke+'? Its token stops working now.')){ev.preventDefault();}});});
  var m=document.getElementById('mint');m.addEventListener('submit',function(ev){if(m.elements.autonomy.value==='act'){var n=m.elements.name.value;
    if(!confirm('Mint '+n+' at act? It will do everything this screen can do, without asking each time.')){ev.preventDefault();return;}m.elements.confirm.value=n;}});
})();
</script>"""
    return (f"{('<p class=bad>' + e(msg) + '</p>') if msg else ''}{shown}<div class='tablewrap'><table><thead><tr><th>Name</th><th>Autonomy</th><th>Created</th><th>Last used</th><th></th></tr></thead><tbody>{rows}</tbody></table></div><br>{form}"
            "<p class='meta'>The autonomy dial is yours: observe looks and reports; propose prepares and asks; act does deterministic work without asking each time. Every call is audited under the connection's name on the Activity page.</p>"
            f"{js}{WRITE_JS}")


def settings_body(web, saved=False):
    try:
        ctx = open(os.path.join(web.etc_dir, "context.md")).read()
    except OSError:
        ctx = ""
    return (f"{'<p class=ok>Saved.</p>' if saved else ''}<form method='post' action='/settings'><h2 style='margin-top:0'>Standing brief for connected agents</h2>"
            "<p class='meta'>What this mesh is for, who runs it, the region and channel policy, standing orders. Served verbatim to every connected agent as <code>mesh_context</code>; nothing in the product knows your fleet, this is where it learns it.</p>"
            f"<textarea name='context' rows='16' style='font:14px var(--mono)'>{e(ctx)}</textarea>"
            "<div style='margin-top:.6rem'><button type='submit'>Save</button></div></form>"
            + update_settings(web))


def update_settings(web):
    tok = bool(web.github_token())
    mode = web.update_mode()
    return (f"<form method='post' action='/settings/update' class='card' style='margin-top:1rem'><h2 style='margin-top:0'>Updates from GitHub</h2>"
            "<p class='meta'>The box reads releases of the repository with a fine-grained personal access token limited to that repository, contents read-only. It is kept on the box at 0600 and never shown again. "
            f"{'A token is on the box.' if tok else 'No token yet: updates cannot be checked until one is entered.'}</p>"
            "<label>GitHub token (write only)<input type='password' name='token' autocomplete='off' placeholder='github_pat_…'></label>"
            "<label>Mode<select name='mode'>" + "".join(f"<option value='{m}'{' selected' if m == mode else ''}>{m}: {d}</option>" for m, d in (("manual", "check daily, install on your press"), ("auto", "check daily and install on its own"), ("off", "never talk to GitHub"))) + "</select></label>"
            "<button type='submit'>Save</button></form>")


def login_body(err=""):
    return (f"<form class='login card' method='post' action='/login'><h2 style='margin-top:0'>Sign in</h2>"
            f"{'<p class=bad>' + e(err) + '</p>' if err else ''}"
            "<label>Operator password<input type='password' name='password' autocomplete='current-password' autofocus required></label>"
            "<button type='submit'>Sign in</button><p class='meta'>Set at install; change it with install.sh --password.</p></form>")



# ---- the server -----------------------------------------------------------------------------------
def make_server(bind, port, socket_path, etc_dir, config=None, state_dir=DEFAULT_STATE):
    config = config or {}
    web = None

    class Handler(http.server.BaseHTTPRequestHandler):
        server_version = "MeshManager/" + __version__
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):   # method, path (no query), status; never a body, never a url from the mesh
            sys.stderr.write("%s %s %s\n" % (self.address_string(), self.command, self.path.split("?")[0]))

        # -- helpers
        def _send(self, code, body, ctype="text/html; charset=utf-8", extra=None):
            if isinstance(body, str):
                body = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            if "Cache-Control" not in (extra or {}):
                self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, code, obj):
            self._send(code, json.dumps(obj, default=str), "application/json")

        def _redirect(self, to, extra=None):
            self.send_response(302)
            self.send_header("Location", to)
            self.send_header("Content-Length", "0")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()

        def _signed_in(self):
            if not web.auth_on:
                return True
            c = self.headers.get("Cookie", "")
            for part in c.split(";"):
                k, _, v = part.strip().partition("=")
                if k == "mm_session" and web.sessions.verify(v):
                    return True
            return False

        def _ask(self, op, **args):
            try:
                return web.client.ask(op, **args)
            except BridgeDown:
                return {}

        def _links(self):
            L = self._ask("links")
            if "nodes" not in L:          # an older bridge: the plain node list, no links, no routes
                L = {"own": {}, "nodes": self._ask("nodes").get("nodes", []), "routes": {}}
            return L

        # -- routes
        def _page(self, title, body, active="", own="", st=None, head=""):
            return page(title, body, active, own=own, st=st if st is not None else self._ask("status"), pending=len(K.proposals(web.etc_dir)), head=head, update=web.update_available())

        def do_GET(self):
            path = self.path.split("?")[0]
            if path == "/healthz":
                return self._json(200, {"ok": True, "bridge": web.client.reachable(), "version": __version__})
            if path == "/login":
                if not web.auth_on:
                    return self._redirect("/")
                return self._send(200, page("Sign in", login_body()))
            api = path.startswith("/api/") or path == "/events" or path.startswith("/fragment/")
            if not self._signed_in():
                return self._json(401, {"error": "sign in first"}) if api else self._redirect("/login")
            if path == "/":
                st = self._ask("status")
                L = self._links()
                return self._send(200, self._page("Mesh", overview_body(st, web.bind, web.auth_on, L.get("nodes"), L, tile_sources(web.config, web.etc_dir)), "/", st=st, head=MAP_HEAD))
            if path == "/map":
                return self._send(200, self._page("Map", map_body(self._links(), tile_sources(web.config, web.etc_dir),
                                                                   disk_map_sources(tilesets_dir(web.config)), saved_map_sources(web.etc_dir),
                                                                   tilesets_dir(web.config)), "/map", head=MAP_HEAD))
            if path == "/map/full":
                js = "<script>window.onMesh=function(d){if(d.kind==='packet'||d.kind==='route'||d.kind==='status'||d.kind==='forwarded'){window.mmFrag('map','map-box');if(window.mmOverlay){window.mmOverlay();}}};</script>"
                return self._send(200, bare_page("Map", mesh_views(self._links(), tile_sources(web.config, web.etc_dir), 800, bare=True) + js, head=MAP_HEAD))
            if path == "/api/update/status":
                return self._json(200, {"running": __version__, "mode": web.update_mode(), "token": bool(web.github_token()), "last": U.last_check(web.state_dir), "log": U.last_log(web.state_dir)})
            if path == "/api/tilesets":
                return self._json(200, {"dir": tilesets_dir(web.config), "tilesets": list_tilesets(tilesets_dir(web.config))})
            if path.startswith("/tiles/"):
                parts = path[len("/tiles/"):].split("/")
                if len(parts) != 4 or not SET_ID.match(parts[0]) or not all(p_.isdigit() for p_ in parts[1:]):
                    return self._send(404, "no such tile", "text/plain")
                data, ctype = tile_bytes(tilesets_dir(web.config), parts[0], int(parts[1]), int(parts[2]), int(parts[3]))
                if not data:
                    return self._send(404, "no such tile", "text/plain")
                return self._send(200, data, ctype, {"Cache-Control": "public, max-age=86400"})
            if path.startswith("/static/"):
                rel = os.path.normpath(urllib.parse.unquote(path[len("/static/"):]))
                fp = os.path.normpath(os.path.join(STATIC_DIR, rel))
                if rel.startswith("..") or os.path.isabs(rel) or not fp.startswith(STATIC_DIR + os.sep) or not os.path.isfile(fp):
                    return self._send(404, "no such file", "text/plain")
                ctype = {".js": "text/javascript", ".css": "text/css", ".png": "image/png"}.get(os.path.splitext(fp)[1], "text/plain")
                with open(fp, "rb") as fh:
                    return self._send(200, fh.read(), ctype + ("; charset=utf-8" if ctype.startswith("text") else ""), {"Cache-Control": "public, max-age=86400"})
            if path == "/nodes":
                L = self._links()
                return self._send(200, self._page("Nodes", nodes_body(L.get("nodes") or [], routes=L.get("routes")) + "<script>window.onMesh=function(d){if(d.kind==='packet'||d.kind==='forwarded'||d.kind==='status'){window.mmNodes();}if(d.kind==='route'&&window.mmRoute){window.mmRoute(d);}if(d.kind==='position'&&window.mmPosition){window.mmPosition(d);}if(d.kind==='telemetry'&&window.mmTelemetry){window.mmTelemetry(d);}};</script>", "/nodes"))
            if path == "/log":
                return self._send(200, self._page("Log", log_body(self._ask("log", n=300).get("lines", [])), "/log"))
            if path == "/channels":
                st = self._ask("status")
                own = (st.get("own") or {}).get("id") or "?"
                return self._send(200, self._page("Channels", channels_body(self._ask("channels"), own, st, rotation=self._ask("rotation_status")), "/channels", own=own, st=st))
            if path == "/about":
                st = self._ask("status")
                return self._send(200, self._page("About", about_body(st, web), "/about", st=st))
            if path == "/help":
                st = self._ask("status")
                return self._send(200, self._page("Help", help_body(st, self._ask("config"), self._ask("register"), self._ask("firmware_shelf"), str(web.config.get("REGION") or "")), "/help", st=st))
            if path == "/messages":
                st = self._ask("status")
                return self._send(200, self._page("Messages", messages_body(web, self._ask("nodes").get("nodes", []), self._ask("channels").get("channels", []), st), "/messages", st=st))
            if path == "/radio":
                st = self._ask("status")
                own = (st.get("own") or {}).get("id") or "?"
                return self._send(200, self._page("This radio", radio_body(self._ask("config"), own), "/radio", own=own, st=st))
            if path == "/node":
                q = urllib.parse.parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
                nid = (q.get("id", [""])[0] or "").strip()
                if not re.fullmatch(r"![0-9a-f]{8}", nid):
                    return self._send(404, self._page("Not found", "<p class='meta'>No such node.</p>"))
                try:
                    hours = int(q.get("hours", ["24"])[0])
                except ValueError:
                    hours = 24
                hours = hours if hours in (24, 168) else 24
                node = next((n for n in (self._links().get("nodes") or []) if n.get("id") == nid), None)
                if not node:
                    return self._send(404, self._page("Not found", "<p class='meta'>No such node.</p>"))
                since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - hours * 3600))
                tel = self._ask("history", kind="telemetry", node=nid, since=since, limit=2000).get("rows") or []
                msgs = self._ask("history", kind="messages", node=nid, since=since, limit=200).get("rows") or []
                npos = len(self._ask("history", kind="positions", node=nid, since=since, limit=5000).get("rows") or [])
                return self._send(200, self._page(dname(node), node_body(node, tel, msgs, npos, hours), "/nodes"))
            if path == "/health":
                return self._send(200, self._page("Health", health_body(self._ask("health", hours=24), self._ask("alerts")), "/health"))
            if path == "/fragment/health":
                return self._send(200, health_cards(self._ask("health", hours=24)), "text/html; charset=utf-8")
            if path == "/fragment/drift":
                return self._send(200, drift_section(self._ask("drift")), "text/html; charset=utf-8")
            if path == "/fragment/rotation":
                return self._send(200, rotation_section(self._ask("rotation_status")), "text/html; charset=utf-8")
            if path == "/fragment/alerts":
                return self._send(200, alerts_section(self._ask("alerts")), "text/html; charset=utf-8")
            if path == "/register":
                return self._send(200, self._page("Register", register_body(self._ask("register"), drift=self._ask("drift")), "/register"))
            if path == "/bench":
                return self._send(200, self._page("Bench", bench_body(self._ask("bench_devices"), self._ask("firmware_shelf")), "/bench"))
            if path == "/activity":
                return self._send(200, self._page("Activity", activity_body(web), "/activity"))
            if path == "/connections":
                return self._send(200, self._page("Connections", connections_body(web), "/connections"))
            if path == "/settings":
                return self._send(200, self._page("Settings", settings_body(web), "/settings"))
            if path.startswith("/fragment/"):
                name = path[len("/fragment/"):]
                if name == "state":
                    return self._send(200, state_strip(self._ask("status")))
                if name == "overview":
                    return self._send(200, overview_cards(self._ask("status")))
                if name == "messages":
                    return self._send(200, message_rows(web, {str(n.get("id")): str(n.get("label") or "") for n in self._ask("nodes").get("nodes", []) if n.get("label")}))
                if name == "channels":
                    return self._send(200, channel_rows(self._ask("channels")))
                if name == "map":
                    return self._send(200, map_svg(self._links()))
                if name == "register":
                    return self._send(200, register_rows(self._ask("register")))
                if name == "bench":
                    return self._send(200, bench_cards(self._ask("bench_devices"), self._ask("firmware_shelf")))
                if name.startswith("route/"):
                    nid = urllib.parse.unquote(name[len("route/"):])
                    return self._send(200, route_bar(self._ask("route", id=nid).get("route")))
                return self._send(404, "", "text/plain")
            if path == "/channels/qr.png":
                url = self._ask("channels").get("url")
                if not url:
                    return self._send(404, "no primary channel url", "text/plain")
                try:
                    return self._send(200, qr_png(url), "image/png")
                except Exception as ex:  # noqa: BLE001
                    return self._send(500, f"qr failed: {type(ex).__name__}", "text/plain")
            if path.startswith("/api/"):
                aid = path[len("/api/"):]
                action = C.by_id(aid)
                if not action:
                    return self._json(404, {"error": f"no action {aid}"})
                if action["risk"] != "read":
                    return self._json(405, {"error": f"{aid} is a POST"})
                q = urllib.parse.parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
                args = {k: v[0] for k, v in q.items()}
                if aid == "log" and "n" not in args:
                    args["n"] = 300
                code, res = run_action(web, aid, args, "operator")
                return self._json(code, res)
            if path == "/events":
                return self._sse()
            return self._send(404, self._page("Not found", "<p class='meta'>No such page.</p>"))

        def do_HEAD(self):
            return self.do_GET()

        def _raw(self):
            if not hasattr(self, "_raw_body"):
                n = int(self.headers.get("Content-Length") or 0)
                self._raw_body = self.rfile.read(min(n, 1 << 20)) if n else b""
            return self._raw_body

        def _body(self):
            raw = self._raw()
            ctype = self.headers.get("Content-Type", "")
            if "json" in ctype:
                try:
                    return json.loads(raw.decode("utf-8", "replace") or "{}")
                except ValueError:
                    return {}
            return {k: v[0] for k, v in urllib.parse.parse_qs(raw.decode("utf-8", "replace")).items()}

        def do_POST(self):
            path = self.path.split("?")[0]
            self._raw()
            if path == "/mcp":
                return self._mcp()
            if path != "/login":
                if not self._signed_in():
                    return self._json(401, {"error": "sign in first"})
                body = self._body()
                if path == "/api/update/check":
                    rec = U.check(web.config, web.github_token(), web.state_dir, api=web.config.get("UPDATE_API"))
                    K.audit(web.etc_dir, who="operator", event="update-check", version=rec.get("version"), available=rec.get("available"), error=rec.get("error"))
                    return self._json(200, rec)
                if path == "/api/update/staged":
                    return self._json(200, {"staged": U.staged(web.state_dir, arch=web.arch, running=__version__), "running": __version__})
                if path == "/api/update/rollback":
                    out = U.rollback(web.state_dir, str(body.get("version") or ""), running=__version__,
                                     mode=web.update_mode(), arch=web.arch, start_unit=web.start_unit)
                    K.audit(web.etc_dir, who="operator", event="update-rollback", version=body.get("version"),
                            started=out.get("started"), error=out.get("error"))
                    return self._json(400 if out.get("error") else 200, out)
                if path == "/api/update/apply":
                    want = str(body.get("version") or "")
                    rec = U.last_check(web.state_dir)
                    if not rec.get("available") or (want and want != rec.get("version")):
                        rec = U.check(web.config, web.github_token(), web.state_dir, api=web.config.get("UPDATE_API"))
                    if not rec.get("available"):
                        return self._json(400, {"error": rec.get("error") or "nothing newer to apply", "running": __version__})
                    d = U.download(rec, web.github_token(), web.state_dir)
                    if not d.get("ready"):
                        K.audit(web.etc_dir, who="operator", event="update-refused", version=rec.get("version"), error=d.get("error"))
                        return self._json(400, {"error": d.get("error"), "running": __version__})
                    a = U.apply(web.state_dir, rec["version"])
                    K.audit(web.etc_dir, who="operator", event="update-apply", version=rec.get("version"), started=a.get("started"), error=a.get("error"))
                    return self._json(200 if a.get("started") else 500, dict(a, running=__version__))
                if path == "/settings/update":
                    tok = str(body.get("token") or "").strip()
                    if tok:
                        web.set_github_token(tok)
                        K.audit(web.etc_dir, who="operator", event="github-token-set")
                    web.set_update_mode(str(body.get("mode") or "manual"))
                    return self._redirect("/settings")
                if path.startswith("/api/proposal/"):
                    pid = str(body.get("id", ""))
                    pr = K.proposal_take(web.etc_dir, pid)
                    if not pr:
                        return self._json(404, {"error": "no such proposal"})
                    if path.endswith("/run"):
                        args = body.get("arguments") if isinstance(body.get("arguments"), dict) else (pr.get("arguments") or {})
                        edited = args != (pr.get("arguments") or {})
                        code, res = run_action(web, pr["action"], args, "operator")
                        K.audit(web.etc_dir, who="operator", event="proposal-run", id=pid, proposed_by=pr.get("who"), action=pr["action"], edited=edited, outcome="ok" if code < 400 else "error")
                        return self._json(code, res)
                    K.audit(web.etc_dir, who="operator", event="dismiss", id=pid, proposed_by=pr.get("who"), action=pr["action"], rationale=pr.get("rationale"))
                    return self._json(200, {"dismissed": pid})
                if path.startswith("/api/"):
                    aid = path[len("/api/"):]
                    action = C.by_id(aid)
                    if not action:
                        return self._json(404, {"error": f"no action {aid}"})
                    if action["risk"] == "read":
                        return self._json(405, {"error": f"{aid} is a GET"})
                    code, res = run_action(web, aid, body, "operator")
                    return self._json(code, res)
                if path == "/connections":
                    name, aut = str(body.get("name", "")).strip(), str(body.get("autonomy", "propose"))
                    if aut == "act" and str(body.get("confirm", "")) != name:
                        return self._send(400, self._page("Connections", connections_body(web, msg="Minting at act needs the confirm: name the connection."), "/connections"))
                    try:
                        minted = K.mint(web.etc_dir, name, aut)
                    except ValueError as ex:
                        return self._send(400, self._page("Connections", connections_body(web, msg=str(ex)), "/connections"))
                    return self._send(200, self._page("Connections", connections_body(web, minted=minted), "/connections"))
                if path == "/connections/autonomy":
                    cid = str(body.get("id", ""))
                    conn = next((c for c in K.list_connections(web.etc_dir) if c.get("id") == cid), None)
                    if not conn or str(body.get("confirm", "")) != conn.get("name"):
                        return self._send(400, self._page("Connections", connections_body(web, msg="Changing an autonomy needs the confirm: name the connection."), "/connections"))
                    K.set_autonomy(web.etc_dir, cid, str(body.get("autonomy", "")))
                    return self._redirect("/connections")
                if path == "/connections/revoke":
                    K.revoke(web.etc_dir, str(body.get("id", "")))
                    return self._redirect("/connections")
                if path == "/settings":
                    ctx = str(body.get("context", ""))[:20000]
                    with open(os.path.join(web.etc_dir, "context.md"), "w") as fh:
                        fh.write(ctx)
                    K.audit(web.etc_dir, who="operator", event="context-saved", bytes=len(ctx.encode()))
                    return self._send(200, self._page("Settings", settings_body(web, saved=True), "/settings"))
                return self._json(404, {"error": "no such route"})
            if not web.auth_on:
                return self._redirect("/")
            ip = self.client_address[0]
            if throttled(ip):
                return self._send(429, page("Sign in", login_body("Too many attempts. Wait a minute.")))
            form = urllib.parse.parse_qs(self._raw().decode("utf-8", "replace"))
            pw = (form.get("password") or [""])[0]
            if pw and os.path.exists(web.passwd) and check_password(web.passwd, pw):
                cookie = f"mm_session={web.sessions.issue()}; Path=/; HttpOnly; SameSite=Strict; Max-Age={SESSION_HOURS * 3600}"
                return self._redirect("/", {"Set-Cookie": cookie})
            note_fail(ip)
            return self._send(401, page("Sign in", login_body("That is not the operator password.")))

        def _mcp(self):
            ip = self.client_address[0]
            auth = self.headers.get("Authorization", "")
            token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
            if throttled(ip):
                return self._json(429, {"jsonrpc": "2.0", "id": None, "error": {"code": -32000, "message": "too many bad tokens from this address; wait a minute"}})
            conn = K.find_by_token(web.etc_dir, token) if token else None
            if not conn:
                if token:
                    note_fail(ip)
                return self._json(401, {"jsonrpc": "2.0", "id": None, "error": {"code": -32001, "message": "a bearer token from the Connections page is required"}})
            try:
                req = json.loads(self._raw().decode("utf-8", "replace") or "{}")
            except ValueError:
                return self._json(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}})
            rid = req.get("id")
            method = str(req.get("method", ""))
            params = req.get("params") or {}
            if method == "initialize":
                result = {"protocolVersion": params.get("protocolVersion") or "2025-06-18", "capabilities": {"tools": {}},
                          "serverInfo": {"name": "mesh-manager", "version": __version__},
                          "instructions": "Read mesh_context first, then status, nodes and channels. Your autonomy is " + conn["autonomy"] + "."}
            elif method == "notifications/initialized":
                self.send_response(202); self.send_header("Content-Length", "0"); self.end_headers(); return
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": mcp_tools(conn["autonomy"])}
            elif method == "tools/call":
                name = str(params.get("name", ""))
                K.audit(web.etc_dir, who=conn["name"], event="call", action=name, arguments=params.get("arguments") or {}, autonomy=conn["autonomy"])
                result = mcp_call(web, conn, name, params.get("arguments") or {})
            else:
                return self._json(200, {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"method not found: {method}"}})
            return self._json(200, {"jsonrpc": "2.0", "id": rid, "result": result})

        def _sse(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            q = web.subscribe()
            try:
                self.wfile.write(b": hello\n\n")
                self.wfile.flush()
                while not web.stop.is_set():
                    try:
                        line = q.get(timeout=15)
                        self.wfile.write(f"event: mesh\ndata: {line}\n\n".encode())
                    except queue.Empty:
                        self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                web.unsubscribe(q)

    class Server(http.server.ThreadingHTTPServer):
        daemon_threads = True
        allow_reuse_address = True

        def handle_error(self, request, client_address):
            # a browser that hung up (a reset on the live feed, a cancelled favicon) is not a fault
            import traceback
            et = sys.exc_info()[0]
            if et is not None and issubclass(et, (ConnectionResetError, BrokenPipeError, ConnectionAbortedError)):
                return
            traceback.print_exc()

    srv = Server((bind, port), Handler)
    web = Web(socket_path, etc_dir, config, srv.server_address[:2], state_dir=state_dir)
    srv.web = web
    _orig_shutdown = srv.shutdown

    def shutdown():
        web.stop.set()
        _orig_shutdown()
    srv.shutdown = shutdown
    return srv


def main(argv=None):
    ap = argparse.ArgumentParser(prog="mesh-manager-web", description="Mesh Manager's screen.")
    ap.add_argument("--config", default=DEFAULT_CONFIG)
    ap.add_argument("--socket", default=DEFAULT_SOCKET)
    ap.add_argument("--etc", default=DEFAULT_ETC, help="where passwd and web.secret live")
    ap.add_argument("--state-dir", default=DEFAULT_STATE, help="the bridge's state directory (updates are staged under it)")
    ap.add_argument("--bind"); ap.add_argument("--port", type=int)
    ap.add_argument("--no-auth", action="store_true", help="no sign-in: anyone who can reach the address is the operator (the config's AUTH=off)")
    ap.add_argument("--write-password", action="store_true", help="read a password from MESH_MANAGER_PASSWORD and write <etc>/passwd, then exit")
    ap.add_argument("--mint-connection", metavar="NAME", help="mint an agent connection and print its token once, then exit")
    ap.add_argument("--autonomy", default="propose", choices=list(C.AUTONOMY))
    a = ap.parse_args(argv)
    if a.mint_connection:
        os.makedirs(a.etc, exist_ok=True)
        m = K.mint(a.etc, a.mint_connection, a.autonomy)
        print(f"connection {m['name']} at {m['autonomy']}; token (shown once): {m['token']}")
        return 0
    if a.write_password:
        pw = os.environ.get("MESH_MANAGER_PASSWORD", "")
        if len(pw) < 8:
            print("ERR MESH_MANAGER_PASSWORD must be at least 8 characters", file=sys.stderr)
            return 2
        os.makedirs(a.etc, exist_ok=True)
        write_password(os.path.join(a.etc, "passwd"), pw)
        print(f"password written to {os.path.join(a.etc, 'passwd')}")
        return 0
    conf = read_config(a.config)
    bind, port = bind_from_config(conf)
    if a.bind: bind = a.bind
    if a.port: port = a.port
    if a.no_auth: conf["AUTH"] = "off"
    os.makedirs(a.etc, exist_ok=True)
    srv = make_server(bind, port, a.socket, a.etc, conf, state_dir=a.state_dir)
    print(f"mesh-manager-web {__version__} on http://{bind}:{port} (bridge socket {a.socket})", file=sys.stderr)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
