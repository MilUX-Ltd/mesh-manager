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
from .common import DEFAULT_CONFIG, DEFAULT_SOCKET, NODE_ICONS, read_config
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
        return rec.get("version") if U.is_available(rec) else None

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
        elif '"kind": "ack"' in line:
            # Spec 034: the radio's word on a message this box sent; find it by packet id
            try:
                ev = json.loads(line)
                for m in self.messages:
                    if m.get("mid") is not None and m.get("mid") == ev.get("request_id"):
                        m["ack"] = "delivered" if ev.get("ok") else (ev.get("reason") or "failed")
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
        seed_messages(web)   # Spec 020: after a restart the store still holds the last 200
        return 200, {"messages": list(web.messages)}
    if action["op"] == "web:quick_messages":
        return 200, {"messages": quick_load(web.etc_dir)}
    if action["op"] == "web:quick_messages_set":
        out, err = quick_save(web.etc_dir, clean.get("messages"))
        if err:
            K.audit(web.etc_dir, who=who, event="refused", action=aid, error=err)
            return 400, {"error": err}
        K.audit(web.etc_dir, who=who, event="ran", action=aid, result=len(out))
        return 200, {"written": len(out), "messages": out, "confirmed": True}
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
        rows, db_rows, heard, db = nodes_tables(res.get("nodes", []), res.get("routes"), _silent_min(web))
        res = dict(res, rows_html=rows, db_rows_html=db_rows, heard=heard, db=db)
    if action["risk"] != "read":
        K.audit(web.etc_dir, who=who, event="run", action=aid, arguments=clean, outcome="error" if "error" in res else "ok")
    code = 400 if "error" in res and action["risk"] != "read" else 200
    return code, res


def _silent_min(web):
    """The Health threshold the node rows judge 'quiet' by; 30 minutes when the bridge cannot say."""
    try:
        return int(web.client.ask("alert_settings", timeout=2).get("silent_min") or 30)
    except (BridgeDown, AttributeError, TypeError, ValueError):
        return 30


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
:root{--surface:#F7F6EB;--surface-raised:#FFFFFF;--surface-sunken:#EDEBDD;--ink:#1C2418;--ink-muted:#4F5A4B;--ink-muted-strong:#3B4538;--line:#D2C78D;--line-strong:#B5B171;--accent:#113308;--accent-ink:#F7F6EB;--gold:#B5B171;--ok:#2E6B30;--warn:#8A5300;--bad:#9E2A22;--live:#D2C78D;--edge:#586F7C;--tap:32px;--s1:4px;--s2:8px;--s3:12px;--s4:16px;--s6:24px;--r:8px;--mono:"Roboto Mono",ui-monospace,Menlo,Consolas,monospace}
[data-theme=dark]{--surface:#0F1A0C;--surface-raised:#182416;--surface-sunken:#0B140A;--ink:#EEF0E6;--ink-muted:#B9C0B2;--ink-muted-strong:#CBD2C4;--line:#2E3F2A;--line-strong:#586F7C;--accent:#1F4A16;--accent-ink:#F7F6EB;--gold:#D2C78D;--ok:#7FC982;--warn:#F0B35A;--bad:#F08C84;--live:#D2C78D;--edge:#8FA1AC}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){--surface:#0F1A0C;--surface-raised:#182416;--surface-sunken:#0B140A;--ink:#EEF0E6;--ink-muted:#B9C0B2;--ink-muted-strong:#CBD2C4;--line:#2E3F2A;--line-strong:#586F7C;--accent:#1F4A16;--accent-ink:#F7F6EB;--gold:#D2C78D;--ok:#7FC982;--warn:#F0B35A;--bad:#F08C84;--live:#D2C78D;--edge:#8FA1AC}}
*{box-sizing:border-box}body{margin:0;background:var(--surface);color:var(--ink);font:14px/1.45 Manrope,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
input,select,textarea,button{font:inherit}
header{background:var(--accent);color:var(--accent-ink);padding:0 var(--s4);display:flex;align-items:center;gap:var(--s4);min-height:var(--tap);position:relative;z-index:1100}
header .brand{font-weight:700;letter-spacing:.02em;white-space:nowrap}header .brand small{font-weight:400;opacity:.8;margin-left:var(--s2)}
nav{display:flex;flex-wrap:nowrap;gap:var(--s1)}nav a{color:var(--accent-ink);text-decoration:none;opacity:.85;padding:0 var(--s3);min-height:var(--tap);display:inline-flex;align-items:center;border-bottom:3px solid transparent;white-space:nowrap}nav a.on{opacity:1;border-bottom-color:var(--gold)}nav a:hover{opacity:1}
details.more{position:relative;margin-left:auto}details.more summary{list-style:none;cursor:pointer;min-height:var(--tap);display:inline-flex;align-items:center;gap:var(--s2);padding:0 var(--s3);color:var(--accent-ink)}details.more summary::-webkit-details-marker{display:none}
details.more nav{position:absolute;right:0;top:100%;background:var(--surface-raised);border:1px solid var(--line);border-radius:var(--r);flex-direction:column;z-index:1100;min-width:220px;box-shadow:0 8px 24px rgba(0,0,0,.18)}details.more nav a{color:var(--ink);border-bottom:0;padding:0 var(--s4);opacity:1}details.more nav a.on{background:var(--surface-sunken)}
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
.sig{display:inline-flex;align-items:center;gap:var(--s2);white-space:nowrap}td time,td .pill{white-space:nowrap}.sig__bars{width:22px;height:16px;flex:none}.sig__bars rect{fill:var(--edge)}.sig--4 rect,.sig--3 .b1,.sig--3 .b2,.sig--3 .b3{fill:var(--ok)}.sig--2 .b1,.sig--2 .b2{fill:var(--warn)}.sig--1 .b1{fill:var(--bad)}
.batt--low{color:var(--bad);font-weight:600}
button{background:var(--accent);color:var(--accent-ink);border:1px solid transparent;border-radius:6px;padding:0 var(--s3);min-height:var(--tap);font-size:.9rem;cursor:pointer}button:hover{filter:brightness(1.15)}button.line{background:transparent;color:var(--ink);border-color:var(--edge)}button.danger{background:var(--bad)}button.quiet{background:var(--surface-sunken);color:var(--ink);border-color:var(--line)}
button:disabled{opacity:.5;cursor:not-allowed}.row-actions{display:flex;gap:var(--s1);flex-wrap:wrap;align-items:center}.row-actions>details.fold.ctl{margin:0}button.icon,details.fold.ctl.icon summary{width:28px;min-height:28px;height:28px;padding:0;display:inline-flex;align-items:center;justify-content:center}button.icon svg,details.fold.ctl.icon summary svg{width:16px;height:16px;display:block}details.fold.ctl.icon summary::after,details.fold.ctl.icon[open] summary::after{content:none}details.fold.ctl.icon[open]{flex-basis:100%}details.fold.ctl.icon[open] summary{margin-bottom:var(--s1)}.visually-hidden{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}.mm-centre button{width:30px;height:30px;padding:0;border:0;border-radius:2px;background:var(--surface-raised);color:var(--ink);display:flex;align-items:center;justify-content:center;cursor:pointer}.mm-centre button:hover{background:var(--surface-sunken);filter:none}.mm-centre button:disabled{color:var(--ink-muted);cursor:not-allowed;opacity:1}.mm-centre button svg{width:18px;height:18px}a.plain{color:var(--accent);text-decoration:underline;text-decoration-thickness:1px;text-underline-offset:2px}a.plain::after{content:' ›';color:var(--ink-muted)}a.plain:hover{text-decoration-thickness:2px}[data-theme=dark] a.plain{color:var(--gold)}.chart{width:100%;max-width:600px;height:auto;display:block;background:var(--surface-raised);border:1px solid var(--line);border-radius:var(--r)}.chart polyline{fill:none;stroke:var(--accent);stroke-width:2}.chart.avail{max-width:none;height:20px;padding:0;border:0;background:transparent}.chart.graph line{stroke-dasharray:none}.chart.graph text{fill:var(--ink);font-size:12px}.chart.graph{max-width:100%}.chart.avail rect.on{fill:var(--ok)}.chart.avail rect.off{fill:var(--edge)}[data-theme=dark] .chart polyline{stroke:var(--gold)}.chart line{stroke-dasharray:3 4;stroke-width:1}.chart line.warn{stroke:var(--warn)}.chart line.bad{stroke:var(--bad)}.chart text{fill:var(--ink-muted);font-size:10px}.mm-readout{background:var(--surface-raised);color:var(--ink);border:1px solid var(--line);border-radius:4px;padding:2px 8px;font-size:.8rem;font-variant-numeric:tabular-nums;white-space:nowrap}.mm-readout:empty{display:none}.leaflet-tooltip.mm-grid{background:var(--surface-raised);color:var(--ink-muted);border:1px solid var(--line);box-shadow:none;padding:0 4px;font-size:10px;font-variant-numeric:tabular-nums}.leaflet-tooltip.mm-grid::before{display:none}.tip{position:fixed;z-index:1200;display:none;max-width:280px;padding:var(--s1) var(--s2);background:var(--ink);color:var(--surface);border-radius:6px;font-size:.8rem;line-height:1.35;box-shadow:0 4px 14px rgba(0,0,0,.25);pointer-events:none}.tip b{display:block;font-weight:600}.tip div{opacity:.85;margin-top:2px}
input[type=text],input[type=number],input[type=password],select,textarea{width:100%;padding:var(--s1) var(--s2);min-height:var(--tap);font-size:.9rem;border:1px solid var(--edge);border-radius:6px;background:var(--surface-raised);color:var(--ink);margin:var(--s1) 0 var(--s3)}
label{display:block}label.check{display:flex;gap:var(--s2);align-items:flex-start;min-height:var(--tap);margin:var(--s2) 0}label.check input{width:18px;height:18px;margin-top:2px;flex:none}
form.card{max-width:560px}form.login{max-width:360px;margin:3rem auto}form.card.danger{border-color:var(--bad)}form.card.danger h2{color:var(--bad)}
details.fold{margin-top:var(--s4)}details.fold summary{cursor:pointer;min-height:var(--tap);display:flex;align-items:center;gap:var(--s2);color:var(--ink-muted-strong)}details.fold.ctl summary{display:inline-flex;white-space:nowrap;padding:0 var(--s3);font-size:.9rem;border:1px solid var(--edge);border-radius:6px;color:var(--ink);list-style:none}details.fold.ctl summary::-webkit-details-marker{display:none}details.fold.ctl summary::after{content:' ▸';margin-left:var(--s1)}details.fold.ctl[open] summary::after{content:' ▾'}
.sheet{position:fixed;inset:0;background:var(--surface);display:flex;flex-direction:column;align-items:center;justify-content:center;gap:var(--s4);z-index:1300;padding:var(--s4);text-align:center;overflow:auto}.sheet .close{position:absolute;top:var(--s3);right:var(--s3)}.sheet[hidden]{display:none}.sheet img{max-width:min(90vw,70vh);height:auto}
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
:focus-visible{outline:3px solid var(--gold);outline-offset:2px}
button.icon .lbl,details.fold.ctl.icon summary .lbl{display:none}[data-labels=on] button.icon,[data-labels=on] details.fold.ctl.icon summary{width:auto;padding:0 var(--s2)}[data-labels=on] button.icon .lbl,[data-labels=on] details.fold.ctl.icon summary .lbl{display:inline;margin-left:var(--s1);font-size:.85rem}
header button.head{background:transparent;color:var(--accent-ink);border:1px solid var(--live)}[data-labels=on] header button.icon,[data-labels=on] .state button.strip{width:var(--tap);padding:0}[data-labels=on] header button.icon .lbl,[data-labels=on] .state button.strip .lbl{display:none}header .headctl{display:flex;gap:var(--s1);margin-left:var(--s2)}
details.more nav .k{font-size:.72rem;color:var(--ink-muted);text-transform:uppercase;letter-spacing:.04em;padding:var(--s2) var(--s4) 0}
.state .state-rest{display:contents}.state button.strip{display:none}
.seg{display:inline-flex;border:1px solid var(--edge);border-radius:6px;overflow:hidden;vertical-align:middle;margin:var(--s1) 0 var(--s3)}.seg label{display:inline-flex;margin:0;cursor:pointer}.seg input{position:absolute;opacity:0;width:0;height:0;margin:0}.seg span{display:inline-flex;align-items:center;min-height:var(--tap);padding:0 var(--s3);border-left:1px solid var(--edge);color:var(--ink);font-size:.9rem;white-space:nowrap}.seg label:first-child span{border-left:0}.seg input:checked+span{background:var(--accent);color:var(--accent-ink)}.seg.danger input:checked+span{background:var(--bad)}.seg input:disabled+span{opacity:.45}.seg input:focus-visible+span{outline:3px solid var(--gold);outline-offset:-3px}
.confirm{background:var(--surface-sunken);border:1px solid var(--edge);border-left:4px solid var(--warn);border-radius:var(--r);padding:var(--s2) var(--s3);margin-top:var(--s2)}.confirm .row-actions{margin-top:var(--s2)}
.filters{display:flex;gap:var(--s2);flex-wrap:wrap;align-items:center;margin-bottom:var(--s2)}.filters input[type=search]{width:auto;min-width:180px;margin:0;padding:var(--s1) var(--s2);min-height:var(--tap);border:1px solid var(--edge);border-radius:6px;background:var(--surface-raised);color:var(--ink);font:inherit}.chip{display:inline-flex;align-items:center;gap:var(--s1);min-height:var(--tap);padding:0 var(--s3);border-radius:999px;border:1px solid var(--edge);background:var(--surface-raised);color:var(--ink);cursor:pointer;font-size:.85rem}.chip.on{background:var(--accent);color:var(--accent-ink);border-color:var(--accent)}.chip b{font-weight:600}
.verdict{display:inline-block;margin-left:var(--s1);font-size:.75rem;font-weight:600}details.fold.ctl.primary summary{background:var(--accent);color:var(--accent-ink);border-color:var(--accent)}details.fold.ctl.bad summary{border-color:var(--bad);color:var(--bad)}.controls label.check{min-height:var(--tap);margin:0;align-items:center}.controls label.check input{width:24px;height:24px;margin:0}.fleet-out{white-space:pre-line}.chat{display:grid;grid-template-columns:minmax(220px,300px) minmax(0,1fr);gap:var(--s3);align-items:start}.chat-side{background:var(--surface-raised);border:1px solid var(--line);border-radius:var(--r);overflow:hidden;max-height:76vh;display:flex;flex-direction:column;min-height:0}.chat-list{overflow-y:auto;min-height:0}.chat-tools{display:flex;flex-wrap:wrap;gap:var(--s1);align-items:center;padding:var(--s2);border-bottom:1px solid var(--line)}.chat-tools .chat-total{font-size:.75rem;color:var(--ink-muted);margin-left:auto}.chat-tools input[type=search]{flex:1 1 100%;min-width:0;margin:0;min-height:var(--tap)}.chat-tools #chat-hidden{font-size:.8rem;min-height:32px}.chat-side.picking>.chat-tools,.chat-side.picking>.chat-list{display:none}.chat-picker{display:flex;flex-direction:column;min-height:0}.chat-picker input[type=search]{margin:var(--s2) var(--s3)}.chat-picks{overflow-y:auto;min-height:0}.chat-menu{position:relative;margin:0}.chat-menu summary{list-style:none;display:inline-flex;cursor:pointer;min-width:var(--tap);min-height:var(--tap);align-items:center;justify-content:center;border:1px solid var(--edge);border-radius:6px;background:var(--surface-raised);color:var(--ink);padding:0}.chat-menu summary:hover{background:var(--surface-sunken)}.chat-menu summary svg{width:16px;height:16px}.chat-menu[open] summary{background:var(--accent);color:var(--accent-ink);border-color:var(--accent)}.chat-menu summary::-webkit-details-marker{display:none}.menu-list{position:absolute;right:0;top:100%;z-index:6;background:var(--surface-raised);border:1px solid var(--line);border-radius:var(--r);box-shadow:0 6px 18px rgba(0,0,0,.18);display:flex;flex-direction:column;min-width:190px;padding:var(--s1) 0}.menu-list button{text-align:left;border:0;border-radius:0;background:transparent;color:var(--ink);padding:var(--s2) var(--s3);min-height:var(--tap);white-space:nowrap}.menu-list button:hover{background:var(--surface-sunken);filter:none}.menu-list.ctx{position:fixed;top:auto;right:auto}.chat-row .nm .mk{display:inline-flex;width:14px;height:14px;vertical-align:-2px;margin-right:4px;color:var(--gold)}.chat-row .nm .mk svg{width:14px;height:14px}.chat-row .mark{display:inline-flex;width:16px;height:16px;color:var(--ink-muted)}.chat-row .mark svg{width:16px;height:16px}.chat-row.hid{opacity:.6}.bubble .act{display:none;border:0;background:transparent;color:inherit;font-size:.72rem;padding:0 4px;min-height:0;text-decoration:underline;cursor:pointer;filter:none}.bubble:hover .act,.bubble:focus-within .act{display:inline}.chat-day.new{color:var(--bad);font-weight:600;width:100%;text-align:center;border-top:1px solid var(--bad);padding-top:2px}.chat-row{display:grid;grid-template-columns:32px minmax(0,1fr) auto;gap:var(--s2);align-items:center;width:100%;text-align:left;padding:var(--s2) var(--s3);background:transparent;color:var(--ink);border:0;border-bottom:1px solid var(--line);border-radius:0;min-height:56px;cursor:pointer}.chat-row:hover{background:var(--surface-sunken);filter:none}.chat-row.on{background:var(--surface-sunken);box-shadow:inset 4px 0 0 var(--accent)}.chat-row .nodeicon{margin:0;width:28px;height:28px}.chat-row .nodeicon svg{width:20px;height:20px}.chat-row .nm{display:block;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.chat-row .last{display:block;font-size:.8rem;color:var(--ink-muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.chat-row .when{font-size:.72rem;color:var(--ink-muted);text-align:right}.chat-row .unread{display:inline-block;min-width:20px;text-align:center;border-radius:999px;background:var(--bad);color:#fff;font-size:.72rem;font-weight:600;padding:0 6px;margin-top:2px}.chat-panes{display:grid;grid-auto-flow:column;grid-auto-columns:minmax(0,1fr);gap:var(--s3)}.chat-panes:empty::before{content:'Choose a chat on the left. Up to three open side by side.';color:var(--ink-muted);font-size:.9rem;display:block;padding:var(--s4)}.chat-win{display:flex;flex-direction:column;background:var(--surface-raised);border:1px solid var(--line);border-radius:var(--r);height:76vh;min-height:360px;min-width:0}.chat-head{display:flex;align-items:center;gap:var(--s2);padding:var(--s2) var(--s3);border-bottom:1px solid var(--line)}.chat-head .nm{font-weight:600;flex:1;min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.chat-head .sub{font-size:.75rem;color:var(--ink-muted)}.chat-head button.back{display:none}.chat-msgs{flex:1;overflow-y:auto;padding:var(--s2) var(--s3);display:flex;flex-direction:column}.bubble{max-width:86%;margin:var(--s1) 0;padding:var(--s1) var(--s3);border-radius:12px;background:var(--surface-sunken);border:1px solid var(--line);overflow-wrap:anywhere}.bubble.me{align-self:flex-end;background:var(--accent);color:var(--accent-ink);border-color:var(--accent)}.bubble .who{font-size:.72rem;opacity:.8}.bubble .meta{font-size:.72rem;opacity:.85;margin-top:2px;display:flex;gap:var(--s2);align-items:center;flex-wrap:wrap}.bubble .meta .pill{font-size:.68rem;padding:0 6px}.bubble.me .meta .pill{background:rgba(255,255,255,.15);color:var(--accent-ink);border-color:rgba(255,255,255,.35)}.chat-day{align-self:center;font-size:.72rem;color:var(--ink-muted);margin:var(--s2) 0}.chat-compose{border-top:1px solid var(--line);padding:var(--s2) var(--s3)}.chat-compose .quick{display:flex;flex-wrap:wrap;gap:var(--s1);margin-bottom:var(--s1)}.chat-compose .quick button{min-height:28px;font-size:.8rem;padding:0 var(--s2)}.chat-compose form{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:var(--s2);align-items:center}.chat-compose input[type=text]{margin:0}.chat-compose .res{grid-column:1/-1;margin:0}.chat-compose .note{grid-column:1/-1;font-size:.75rem;color:var(--ink-muted)}@media (max-width:700px){.chat{display:block}.chat.open .chat-side{display:none}.chat-panes{grid-auto-flow:row}.chat-win{height:calc(100vh - 190px)}.chat-head button.back{display:inline-flex}}#play-rev.on{background:var(--accent);color:var(--accent-ink)}.iconpick{display:flex;flex-wrap:wrap;gap:var(--s1);margin:var(--s1) 0}.iconpick label{margin:0}.iconpick input{position:absolute;opacity:0;width:0;height:0}.iconpick span{display:inline-flex;width:40px;height:40px;align-items:center;justify-content:center;border:1px solid var(--edge);border-radius:6px;color:var(--ink);background:var(--surface-raised)}.iconpick input:checked+span{background:var(--accent);color:var(--accent-ink);border-color:var(--accent)}.iconpick input:focus-visible+span{outline:3px solid var(--gold)}.iconpick svg{width:20px;height:20px}.mm-pin{background:transparent;border:0}.mm-pin-in{display:flex;width:30px;height:30px;border-radius:50%;background:var(--surface-raised);border:2px solid var(--accent);align-items:center;justify-content:center;color:var(--accent)}.mm-pin-in svg{width:18px;height:18px}.mm-pin.play .mm-pin-in{background:var(--gold)}.mm-pin.stale .mm-pin-in{background:transparent;border-style:dashed}.nodeicon{display:inline-flex;width:20px;height:20px;vertical-align:-5px;margin-right:var(--s1);color:var(--accent)}.nodeicon svg{width:18px;height:18px}.controls>details.fold.ctl{margin-top:0}.controls>details.fold.ctl[open]{flex-basis:100%}.filters label{display:inline-flex;align-items:center;gap:var(--s1);margin:0}.filters select{width:auto;margin:0}ol.steps{margin:var(--s1) 0 0 var(--s4);padding:0}ol.steps li{margin:2px 0}.views>button svg{width:16px;height:16px;vertical-align:-3px;margin-right:var(--s1)}details.fold.ctl summary svg{width:16px;height:16px;vertical-align:-3px;margin-right:var(--s1)}
@media (pointer:coarse){button.icon,details.fold.ctl.icon summary{width:40px;height:40px;min-height:40px}.row-actions{gap:var(--s2)}.mm-centre button{width:40px;height:40px}}
@media (max-width:700px){:root{--tap:44px}header nav.primary{position:fixed;bottom:0;left:0;right:calc(var(--tap) + var(--s2));background:var(--accent);justify-content:space-around;z-index:1100;border-top:1px solid var(--live)}header nav.primary a{padding:0 var(--s2);font-size:.8rem}main{padding-bottom:calc(var(--tap) + var(--s6))}.hide-narrow{display:none}.state .live{margin-left:0}
details.more{position:fixed;bottom:0;right:0;z-index:1101;margin:0;background:var(--accent);border-top:1px solid var(--live)}details.more summary{width:calc(var(--tap) + var(--s2));justify-content:center;padding:0}details.more summary .word{display:none}details.more nav{position:fixed;bottom:var(--tap);top:auto;right:0;left:0;max-height:70vh;overflow:auto;border-radius:var(--r) var(--r) 0 0}
.state .state-rest{display:none}.state.open .state-rest{display:contents}.state button.strip{display:inline-flex;margin-left:auto}.state.open button.strip svg{transform:rotate(180deg)}
button.icon,details.fold.ctl.icon summary{width:44px;height:44px;min-height:44px}.row-actions{gap:var(--s2)}.regform{grid-template-columns:1fr}
header .brand small{display:none}}
"""
# The primary bar is where the operator lives (5 Sep 2026 reviews): the mesh, the nodes, the
# messages, the channels, the health. Radio is a set-up page, pressed once a deployment, and sits
# in More with the rest, grouped by what they are about.
NAV_PRIMARY = [("/", "Mesh"), ("/nodes", "Nodes"), ("/messages", "Messages"), ("/channels", "Channels"), ("/health", "Health")]
NAV_GROUPS = [("The fleet", [("/register", "Register"), ("/bench", "Bench"), ("/graph", "Neighbours"), ("/packets", "Packets")]),
              ("The mesh", [("/map", "Map"), ("/log", "Log"), ("/activity", "Activity")]),
              ("The box", [("/radio", "Radio"), ("/connections", "Connections"), ("/settings", "Settings"), ("/help", "Help"), ("/about", "About")])]
NAV_MORE = [item for _, items in NAV_GROUPS for item in items]
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
    """Clock time for an ISO stamp, or for now: Zulu, and it says so. The box's clock and the
    browser's used to render side by side, unlabelled and an hour apart in summer."""
    try:
        t = _utc_secs(iso) if iso else time.time()
    except (ValueError, TypeError):
        return str(iso or "")
    return time.strftime("%H:%MZ", time.gmtime(t))


LIVE_JS = r"""<script>
(function(){
  var root=document.documentElement;
  try{var t=localStorage.getItem('mm-theme'); if(t){root.dataset.theme=t;}}catch(x){}
  try{var qt=new URLSearchParams(window.location.search).get('theme');if(qt==='light'||qt==='dark'){root.dataset.theme=qt;}}catch(x){}
  var tb=document.querySelector('[data-theme-toggle]');
  function themeGlyph(){if(!tb)return;var dark=(root.dataset.theme||(matchMedia('(prefers-color-scheme: dark)').matches?'dark':'light'))==='dark';var s=tb.querySelector('[data-sun]'),m=tb.querySelector('[data-moon]');if(s)s.style.display=dark?'block':'none';if(m)m.style.display=dark?'none':'block';}
  if(tb){tb.addEventListener('click',function(){var cur=root.dataset.theme||(matchMedia('(prefers-color-scheme: dark)').matches?'dark':'light');var nx=cur==='dark'?'light':'dark';root.dataset.theme=nx;try{localStorage.setItem('mm-theme',nx);}catch(x){}themeGlyph();});themeGlyph();}
  // words on icon buttons: the operator's choice, remembered; on by default on a narrow screen, where there is no hover to learn a glyph from (5 Sep 2026 reviews)
  try{var l=localStorage.getItem('mm-labels');if(l===null){l=window.innerWidth<700?'on':'off';}root.dataset.labels=l;}catch(x){root.dataset.labels=window.innerWidth<700?'on':'off';}
  var lbtn=document.querySelector('[data-labels-toggle]');
  if(lbtn){lbtn.setAttribute('aria-pressed',root.dataset.labels==='on'?'true':'false');lbtn.addEventListener('click',function(){var nx=root.dataset.labels==='on'?'off':'on';root.dataset.labels=nx;lbtn.setAttribute('aria-pressed',nx==='on'?'true':'false');try{localStorage.setItem('mm-labels',nx);}catch(x){}});}
  var sb=document.querySelector('.state button.strip'),strip=document.querySelector('.state');
  if(sb&&strip){try{if(localStorage.getItem('mm-strip')==='open'){strip.classList.add('open');}}catch(x){}sb.addEventListener('click',function(){var on=strip.classList.toggle('open');sb.setAttribute('aria-expanded',on?'true':'false');try{localStorage.setItem('mm-strip',on?'open':'closed');}catch(x){}});}
  function two(n){return ('0'+n).slice(-2);}
  window.mmNow=function(){var d=new Date();return two(d.getUTCHours())+':'+two(d.getUTCMinutes())+'Z';};
  window.mmHm=function(iso){var d=new Date(iso||'');return isNaN(d.getTime())?(iso||''):two(d.getUTCHours())+':'+two(d.getUTCMinutes())+'Z';};
  window.mmAge=function(iso){var t=Date.parse(iso||'');if(isNaN(t))return '';var s=Math.max(0,Math.round((Date.now()-t)/1000));if(s<60)return s+' s ago';var m=Math.floor(s/60);if(m<60)return m+' min ago';var h=Math.floor(m/60);if(h<48)return h+' h '+(m%60)+' min ago';return Math.floor(h/24)+' d ago';};
  function ages(){document.querySelectorAll('time[data-age]').forEach(function(t){var a=window.mmAge(t.getAttribute('datetime'));if(a){t.textContent=a;}});}
  setInterval(ages,15000);
  var lf=document.getElementById('live'),last=Date.now(),down=false;
  function tick(){if(!lf)return;var s=Math.round((Date.now()-last)/1000);lf.className='live'+(down?' down':(s>60?' stale':''));lf.innerHTML=down?'updates <b>stopped</b>':('live <b>'+(s<2?'now':s+' s ago')+'</b>');lf.setAttribute('data-tip',down?'The box stopped sending updates to this browser':'How long since the box last spoke to this page');}
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
    elif st.get("mode") == "hub":
        n = int(st.get("peers") or 0)
        lamp, word = ("ok" if st.get("peer_port") else "warn"), f"Hub · {n} peer{'' if n == 1 else 's'}"   # Spec 052: a site with no radio
    elif st.get("bootloader"):
        lamp, word = "bad", "Radio in bootloader"
    elif not st.get("radio_present"):
        lamp, word = "bad", "Radio missing"
    elif not st.get("connected"):
        lamp, word = "warn", "Radio not connected"
    elif st.get("tak") == "off":
        lamp, word = "ok", "Managing the mesh"      # Spec 050: a box without TAK
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
        m_ = re.match(r"^([a-z]+)://", via)
        src = m_.group(1) if m_ else via
        tip = " · ".join(x for x in (
            f"Receiver: {src}" if src else "",
            f"last read {when[11:16]}Z" if len(when) >= 16 else (f"last read {when}" if when else ""),
            (f"{int(seen)} satellites seen" + (f", {int(used)} used" if isinstance(used, int) else "")) if isinstance(seen, int) else "") if x)
        parts.append(f"<span class='word' data-tip='{e(tip)}' tabindex='0'>"
                     f"<i class='lamp lamp--{glamp}'></i>{e(gword + sats)}</span>")
    alerts = ""
    if st.get("alerts_open"):
        n = int(st["alerts_open"])
        alerts = f"<a href='/health#alerts' class='pill' style='background:var(--bad);color:#fff;border-color:var(--bad)'>{n} alert{'s' if n != 1 else ''}</a>"
    # on a phone the standing facts fold behind a chevron; the lamp, the alerts and the live counter stay
    return (parts[0] + alerts + "<span class='state-rest'>" + "".join(parts[1:]) + "</span>"
            + icon_button("chevron", "Show the rest of the status", "The rest of the status", "Nodes, region, preset, channel and receiver", cls="line icon strip", attrs="aria-expanded='false'"))


# Spec 046: installable on a phone and a tablet. No service worker: the app is meaningless without the box,
# and a cached shell would fight the updater.
APP_MANIFEST = {"name": "Mesh Manager", "short_name": "Mesh", "start_url": "/", "scope": "/", "display": "standalone", "orientation": "any",
                "background_color": "#F7F6EB", "theme_color": "#113308", "description": "The mesh as it is now, from the box that carries the radio.",
                "icons": [{"src": "/static/icons/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
                          {"src": "/static/icons/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
                          {"src": "/static/icons/maskable-192.png", "sizes": "192x192", "type": "image/png", "purpose": "maskable"},
                          {"src": "/static/icons/maskable-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
                          {"src": "/static/icons/icon.svg", "sizes": "any", "type": "image/svg+xml"}]}
APP_HEAD = ("<link rel='manifest' href='/manifest.webmanifest'><meta name='theme-color' content='#113308'>"
            "<meta name='mobile-web-app-capable' content='yes'><meta name='apple-mobile-web-app-capable' content='yes'><meta name='apple-mobile-web-app-title' content='Mesh'>"
            "<link rel='apple-touch-icon' href='/static/icons/apple-touch-icon.png'><link rel='icon' href='/static/icons/icon.svg' type='image/svg+xml'>")
VIEWPORT = "<meta name='viewport' content='width=device-width,initial-scale=1,viewport-fit=cover'>"


def page(title, body, active="", own="", st=None, pending=0, head="", update=None, notice=""):
    prim = "".join(f"<a href='{p}' class='{'on' if p == active else ''}'>{e(t)}</a>" for p, t in NAV_PRIMARY)
    more = ""
    for group, items in NAV_GROUPS:
        more += f"<div class='k'>{e(group)}</div>" + "".join(
            f"<a href='{p}' class='{'on' if p == active else ''}'>{e(t)}{(' <span class=pill>' + str(pending) + '</span>') if (p == '/activity' and pending) else ''}</a>" for p, t in items)
    more_on = any(p == active for p, _ in NAV_MORE)
    theme = ("<button type='button' class='theme head icon' data-theme-toggle aria-label='Light or dark' data-tip='Light or dark'>"
             f"<span data-sun>{ICONS['sun']}</span><span data-moon style='display:none'>{ICONS['moon']}</span></button>")
    return f"""<!doctype html><html lang='en-GB'><head><meta charset='utf-8'>{VIEWPORT}
<title>{e(title)} · Mesh Manager</title>{APP_HEAD}<style>{CSS}</style>{head}</head><body data-own='{e(own)}'>
<header><span class='brand'>Mesh Manager<small>{e(__version__)}</small></span><nav class='primary'>{prim}</nav>
<details class='more'><summary aria-label='More pages'>{ICONS['menu']}<span class='word'>{'<b>More</b>' if more_on else 'More'}</span>{(' <span class=pill>' + str(pending) + '</span>') if pending else ''}</summary><nav>{more}</nav></details>
{("<a class='pill upd' href='/about'>update available: " + e(str(update)) + "</a>") if update else ""}<span class='headctl'>{icon_button("type", "Words on buttons", "Words on buttons", "Show a word beside every icon", cls="head icon", attrs="data-labels-toggle aria-pressed='false'")}{theme}</span></header>
<div class='state' role='status'><span class='body' id='state-body'>{state_strip(st)}</span>{("<span class='pill' style='background:var(--warn);color:#fff;border-color:var(--warn)' data-tip='Sign-in is off' data-tip-more='Anyone who can reach this address is the operator'>" + e(notice) + "</span>") if notice else ""}<span id='live' class='live' data-tip='How long since the box last spoke to this page'>live <b>…</b></span></div>
<main><h1>{e(title)}</h1>{body}</main>
<footer>Mesh Manager by MilUX Ltd · GPL-3.0-or-later · the mesh as it is now, from the box that carries the radio</footer>
{LIVE_JS}{TIP_JS}</body></html>"""


TIP_JS = r"""<script>
(function(){
  // an instant tooltip for anything with data-tip (and data-tip-more): one fixed element placed by script, so no table wrapper clips it.
  // Hover and keyboard focus show it; on a touch screen a press held for 450 ms shows it and swallows the tap, so a glyph can be learned
  // without firing what it does (5 Sep 2026 reviews). Escape hides it; the description is exposed through aria-describedby.
  var tip=null,onEl=null,holdTimer=null,swallow=false;
  function place(el){var r=el.getBoundingClientRect();var w=tip.offsetWidth,h=tip.offsetHeight;var x=Math.min(Math.max(8,r.left+r.width/2-w/2),window.innerWidth-w-8);var y=r.top-h-8;if(y<8){y=r.bottom+8;}tip.style.left=x+'px';tip.style.top=y+'px';}
  function show(el){var t=el.getAttribute('data-tip');if(!t)return;if(!tip){tip=document.createElement('div');tip.className='tip';tip.id='mm-tip';tip.setAttribute('role','tooltip');document.body.appendChild(tip);}
    tip.innerHTML='';var b=document.createElement('b');b.textContent=t;tip.appendChild(b);var m=el.getAttribute('data-tip-more');if(m){var d=document.createElement('div');d.textContent=m;tip.appendChild(d);}
    tip.style.display='block';place(el);if(onEl&&onEl!==el){onEl.removeAttribute('aria-describedby');}onEl=el;el.setAttribute('aria-describedby','mm-tip');}
  function hide(){if(tip){tip.style.display='none';}if(onEl){onEl.removeAttribute('aria-describedby');}onEl=null;}
  document.addEventListener('mouseover',function(ev){var el=ev.target.closest&&ev.target.closest('[data-tip]');if(el&&el!==onEl){show(el);}else if(!el&&onEl){hide();}});
  document.addEventListener('mouseout',function(ev){var el=ev.target.closest&&ev.target.closest('[data-tip]');if(el&&!(ev.relatedTarget&&el.contains(ev.relatedTarget))){hide();}});
  document.addEventListener('focusin',function(ev){var el=ev.target.closest&&ev.target.closest('[data-tip]');if(el){show(el);}});
  document.addEventListener('focusout',function(){hide();});
  document.addEventListener('keydown',function(ev){if(ev.key==='Escape'&&onEl){hide();}});
  document.addEventListener('pointerdown',function(ev){if(ev.pointerType!=='touch')return;var el=ev.target.closest&&ev.target.closest('[data-tip]');if(!el)return;
    holdTimer=setTimeout(function(){holdTimer=null;swallow=true;show(el);},450);});
  function release(){if(holdTimer){clearTimeout(holdTimer);holdTimer=null;}}
  document.addEventListener('pointerup',release);document.addEventListener('pointercancel',release);document.addEventListener('pointermove',function(ev){if(ev.pointerType==='touch')release();});
  document.addEventListener('click',function(ev){if(swallow){swallow=false;ev.preventDefault();ev.stopImmediatePropagation();return;}if(ev.target.closest&&ev.target.closest('[data-tip]')){hide();}},true);
  document.addEventListener('contextmenu',function(ev){if(onEl&&ev.target.closest&&ev.target.closest('[data-tip]')===onEl){ev.preventDefault();}});
  window.addEventListener('scroll',function(){if(onEl&&tip&&tip.style.display==='block'){place(onEl);}},true);
})();
</script>"""


def bare_page(title, body, head=""):
    """A page with nothing but its body: the map in a window of its own (Spec 019)."""
    return f"""<!doctype html><html lang='en-GB'><head><meta charset='utf-8'>{VIEWPORT}
<title>{e(title)} · Mesh Manager</title>{APP_HEAD}<style>{CSS}
body.bare{{margin:0}}body.bare .state{{display:none}}body.bare main{{padding:var(--s2);max-width:none}}body.bare .geo{{height:calc(100vh - 4.5rem);min-height:240px}}</style>{head}</head><body class='bare'>
<div class='state' role='status' hidden><span class='body' id='state-body'></span><span id='live' class='live'></span></div>
<main>{body}</main>{LIVE_JS}{TIP_JS}</body></html>"""


def card(k, v, cls=""):
    return f"<div class='card'><div class='k'>{e(k)}</div><div class='v {cls}'>{v}</div></div>"


def dur(secs):
    """A duration a person reads: minutes to ninety, then hours and minutes, past two days days and hours."""
    m = max(0, int(secs or 0)) // 60
    if m <= 90:
        return f"{m} min"
    h = m // 60
    if h >= 48:
        return f"{h // 24} d {h % 24} h"
    return f"{h} h {m % 60} min"


def overview_cards(st):
    if not st or "version" not in st:
        return ("<p class='bad'>The bridge is not answering, so the radio is not being read and nothing is reaching TAK. "
                "Restart the box; if it comes back the same, read <code>journalctl -u mesh-manager-bridge</code> over SSH.</p>")
    radio = st.get("radio") or "(none)"
    present, boot, conn = st.get("radio_present"), st.get("bootloader"), st.get("connected")
    radio_txt = ("in bootloader mode" if boot else ("present" if present else "MISSING")) + (", connected" if conn else ", not connected")
    radio_cls = "bad" if (boot or not present) else ("ok" if conn else "warn")
    own = st.get("own") or {}
    mode = "observing: listening only, forwarding nothing" if st.get("observe") else "bridging to TAK"
    fwd = st.get("last_forwarded")
    act = st.get("last_activity")
    heard, db = int(st.get("nodes_seen") or 0), st.get("nodes_db")
    face = "".join([
        (card("Radio", "none: this site is a hub<div class='meta'>peers join it by an invite; their pictures show here</div>", "ok") if st.get("mode") == "hub"
         else card("Radio", f"{e(radio_txt)}<div class='meta'>{e(radio)}</div>", radio_cls)),
        card("Channel utilisation", (f"<a href='/health'>{float(st['chutil']):.1f}% <span class='pill'>{e(st.get('verdict') or '')}</span></a>" if st.get("chutil") is not None else "<a href='/health'>no reading yet</a>")
             + "<div class='meta'>how busy the channel is · now, from this radio</div>",
             {"quiet": "ok", "normal": "ok", "busy": "warn", "saturated": "bad"}.get(st.get("verdict") or "", "")),
        (card("Last packet heard", (f"<time datetime='{e(st['last_activity'])}' data-age>{e(age(st['last_activity']))}</time>" if st.get("last_activity") else "none yet"), "ok" if st.get("last_activity") else "warn")
         if st.get("tak") == "off" else
         card("Last packet TAK would have had" if st.get("observe") else "Last packet sent to TAK",
              (f"<time datetime='{e(fwd)}' data-age>{e(age(fwd))}</time>" if fwd else "none yet"), "ok" if fwd else "warn")),
    ])
    rest = "".join([
        card("Bridge", f"running · {e(mode)}", "ok"),
        card("This radio", f"{e(own.get('name') or '?')} <span class='pill'>{e(own.get('id') or '')}</span>"),
        card("Region · preset", f"{e(st.get('region') or '?')} · {e(st.get('modem_preset') or '?')}"),
        card("Primary channel", e(st.get("primary_channel") or "?")),
        card("Nodes", f"{heard} heard here" + (f"<div class='meta'>{int(db)} in the radio's database · now, from the radio</div>" if db is not None else "")),
        card("Radio last spoke", (f"<time datetime='{e(act)}' data-age>{e(age(act))}</time>" if act else "none yet")),
        card("Watchdog", e(st.get("watchdog") or "?"), "ok" if st.get("watchdog") == "pinging" else "warn"),
        card("Up for", e(dur(st.get("uptime")))),
    ])
    return (f"<div class='cards'>{face}</div><details class='fold' data-keep='box-detail'><summary>The box in detail</summary><div class='cards'>{rest}</div></details>")


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
    if src == "declared":
        return "the position set on Settings"
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


def map_body(L, tiles=None, disk=None, added=None, folder="", tak_on=True):
    js = """<script>window.onMesh=function(d){if(d.kind==='packet'||d.kind==='route'||d.kind==='status'||d.kind==='forwarded'){window.mmFrag('map','map-box');if(window.mmOverlay){window.mmOverlay();}}if(d.kind==='survey'&&window.mmSurvey){window.mmSurvey(d);}if((d.kind==='position'||(d.kind==='survey'&&d.state==='asked'))&&window.mmCoverTick){setTimeout(window.mmCoverTick,1500);}};</script>"""
    own = L.get("own") or {}
    how = position_words(own)
    return (f"<p class='meta'>This box at the centre, every node heard since the bridge started about it; a solid link is coloured by the SNR of the last packet that came straight from that node, a dashed one has only ever come through a relay, a database-only node is not drawn. Box position: {e(how)}.</p>"
            f"{mesh_views(L, tiles or tile_sources({}), 800, tak_on=tak_on)}{survey_form(L)}{map_sources_form(tiles, disk, added, folder)}{js}{WRITE_JS}")


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
            f"<form data-action='survey_start' class='regform' style='grid-template-columns:2fr 1fr 1fr auto;max-width:720px;align-items:end'>"
            f"<label class='meta'>Node<select name='dest' aria-label='node'>{opts}</select></label>"
            "<label class='meta'>Every (seconds)<input type='number' name='interval' value='15' min='5' max='120' aria-label='seconds between asks'></label>"
            "<label class='meta'>For (minutes)<input type='number' name='minutes' value='10' min='1' max='120' aria-label='minutes to keep asking'></label>"
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
              + ("The bridge binds no port of its own; it owns the radio and speaks to nothing else. " if (st or {}).get("tak") == "off"
                 else "The bridge binds no port of its own; it owns the radio and speaks to TAK Server over the multicast input. ")
              + (f"The bridge also listens for peers on <b>{e(str(st.get('peer_bind')))}:{e(str(st.get('peer_port')))}</b>, TLS, paired sites only (Spec 052). " if (st or {}).get("peer_port") else "")
              + "Everything else on this box is closed until the operator opens it.</p>")
    js = """<script>window.onMesh=function(d){if(d.kind==='status'||d.kind==='forwarded'||d.kind==='connection'){var o=document.querySelector('#overview-cards details'),was=!!(o&&o.open);window.mmFrag('overview','overview-cards',function(){var n=document.querySelector('#overview-cards details');if(n&&was){n.open=true;}});}
if(d.kind==='packet'||d.kind==='forwarded'||d.kind==='status'||d.kind==='route'){window.mmFrag('map','map-box');if(window.mmOverlay){window.mmOverlay();}var h=document.getElementById('home-heard');if(h&&d.kind==='status'&&d.nodes_seen!==undefined){h.textContent=d.nodes_seen;}}};</script>"""
    L = links or {"own": {}, "nodes": nodes or [], "routes": {}}
    heard = len([n for n in (L.get("nodes") or []) if n.get("heard_here", True)])
    return (f"{mesh_views(L, tiles or tile_sources({}), tak_on=(st or {}).get('tak') != 'off')}"
            f"<p class='meta'><a href='/nodes'>Nodes</a>: <span id='home-heard'>{heard}</span> heard here since the bridge started. Who is where, who has gone quiet and who is low on battery is on the Nodes page, one press away.</p>"
            f"<h2>This box</h2><div id='overview-cards'>{overview_cards(st)}</div>{closed}{js}")


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
    return f"<span class='sig sig--{bars}' data-tip='SNR {snr:g} dB' data-tip-more='Signal to noise of the last packet straight from this node'>{SIG_SVG}<span>{snr:g} dB</span></span>{hop}"


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
  var lastRows=[];
  function drawTrails(rows){lastRows=rows||[];trails_.clearLayers();var hrs=parseFloat(trailHours());if(!(hrs>0)||!rows||!rows.length)return;var now=Date.now(),win=hrs*3600*1000;
    var g=groupChosen();var byNode={};rows.forEach(function(r){if(r.lat===null||r.lon===null)return;if(g&&(groups_[r.node]||'')!==g)return;(byNode[r.node]=byNode[r.node]||[]).push(r);});
    var names={};(lastJ&&lastJ.nodes||[]).forEach(function(n){names[n.id]=n.label||n.name||n.id;});
    Object.keys(byNode).forEach(function(id,idx){var pts=byNode[id];pts.sort(function(a,b){return a.ts<b.ts?-1:1;});var stride=Math.max(1,Math.floor(pts.length/600));var col=tok(TRAIL_COLOURS[idx%TRAIL_COLOURS.length]);
      for(var i=stride;i<pts.length;i+=stride){var a=pts[i-stride],b=pts[i];var age=now-Date.parse(b.ts);if(age>win)continue;var d=map.distance([a.lat,a.lon],[b.lat,b.lon]);if(d>2000)continue;
        var op=0.25+0.75*Math.max(0,1-age/win);L.polyline([[a.lat,a.lon],[b.lat,b.lon]],{color:col,weight:4,opacity:op,interactive:true}).bindTooltip((names[id]||id)+' · '+b.ts.slice(11,16)+'Z',{sticky:true,className:'mm-link'}).addTo(trails_);}});
    if(window.requestAnimationFrame){requestAnimationFrame(function(){if(typeof drawTimeline==='function')drawTimeline();});}}
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
    function parse(str){var t=String(str||'').toUpperCase().replace(/\s+/g,'');var m=/^(\d{1,2})([C-HJ-NP-X])([A-HJ-NP-Z])([A-HJ-NP-V])(\d*)$/.exec(t);if(!m)return null;var zone=parseInt(m[1],10),band=m[2],digits=m[5];if(zone<1||zone>60||digits.length%2)return null;
      var p=digits.length/2,div=Math.pow(10,5-p);var ex=p?parseInt(digits.slice(0,p),10)*div+div/2:50000,ny=p?parseInt(digits.slice(p),10)*div+div/2:50000;
      var col=COLS[(zone-1)%3].indexOf(m[3]);if(col<0)return null;var x=(col+1)*100000+ex;var row=ROWS.indexOf(m[4]);if(row<0)return null;row=(row-(zone%2===0?5:0)+20)%20;
      var bi=BANDS.indexOf(band),minLat=-80+bi*8,maxLat=band==='X'?84:minLat+8,south=band<'N';
      for(var k=0;k<10;k++){var y=row*100000+ny+k*2000000;if(y>10000000)break;var ll=fromUtm(zone,south?'S':'N',x,y);if(ll[0]>=minLat-0.01&&ll[0]<maxLat+0.01)return ll;}return null;}
    return {zoneFor:zoneFor,toUtm:toUtm,fromUtm:fromUtm,mgrs:mgrs,parse:parse};})();window.mmMgrs=MG.mgrs;window.mmMgrsParse=MG.parse;
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
  function alpha(){var v=60;try{var s=localStorage.getItem('mm-ring-alpha');if(s!==null)v=parseInt(s,10);}catch(e){}var el=document.querySelector('input[name=rings]:checked');if(el){v=parseInt(el.value,10);}if(isNaN(v))v=60;return Math.max(0,Math.min(100,v))/100;}
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
  (function(){var rs=document.querySelectorAll('input[name=rings]');if(!rs.length)return;try{var s0=localStorage.getItem('mm-ring-alpha');if(s0!==null){rs.forEach(function(r){r.checked=(r.value===s0);});}}catch(e){}
    rs.forEach(function(r){r.addEventListener('change',function(){try{localStorage.setItem('mm-ring-alpha',r.value);}catch(e){}rings();});});})();
  var lay=document.getElementById('layers');if(lay){try{var lo=localStorage.getItem('mm-layers');lay.open=lo===null?window.innerWidth>700:lo==='1';}catch(e){lay.open=window.innerWidth>700;}lay.addEventListener('toggle',function(){try{localStorage.setItem('mm-layers',lay.open?'1':'0');}catch(e){}});}
  var pop=document.getElementById('map-pop');if(pop){pop.addEventListener('click',function(){window.open('/map/full','mm-map','popup=yes,width=1100,height=800');});}
  // place a waypoint by pressing the map, or type a grid reference: either fills the degrees the form sends
  var placing=false,wpBtn=document.getElementById('wp-place'),wpNote=document.getElementById('wp-place-note');
  function fillWp(ll){var la=document.getElementById('wp-lat'),lo=document.getElementById('wp-lon'),mg=document.getElementById('wp-mgrs');if(la)la.value=ll.lat.toFixed(5);if(lo)lo.value=ll.lng.toFixed(5);if(mg)mg.value=MG.mgrs(ll.lat,ll.lng,4);}
  if(wpBtn){wpBtn.addEventListener('click',function(){placing=!placing;wpBtn.setAttribute('aria-pressed',placing?'true':'false');if(wpNote)wpNote.textContent=placing?'press the map where the waypoint goes':'';document.getElementById('map-geo').style.cursor=placing?'crosshair':'';});
    map.on('click',function(ev){if(!placing)return;placing=false;wpBtn.setAttribute('aria-pressed','false');document.getElementById('map-geo').style.cursor='';fillWp(ev.latlng);if(wpNote)wpNote.textContent='placed at '+MG.mgrs(ev.latlng.lat,ev.latlng.lng,4);});}
  var wpM=document.getElementById('wp-mgrs');if(wpM){wpM.addEventListener('input',function(){var n=document.getElementById('wp-mgrs-note');var p=MG.parse(wpM.value);if(!wpM.value.trim()){if(n)n.textContent='';return;}if(!p){if(n)n.textContent='not a grid reference this map can read';return;}var la=document.getElementById('wp-lat'),lo=document.getElementById('wp-lon');if(la)la.value=p[0].toFixed(5);if(lo)lo.value=p[1].toFixed(5);if(n)n.textContent=p[0].toFixed(5)+', '+p[1].toFixed(5);});}
  // Spec 045: fences. A control under Centre enters draw mode: each press drops a corner, Finish (or a double press)
  // closes the outline; Circle instead takes a centre and a radius. The outline goes to the form under the map.
  var fences_=L.layerGroup().addTo(map),draft_=L.layerGroup().addTo(map),drawing=null,corners=[],fenceRadius=200;
  var FenceCtl=L.Control.extend({onAdd:function(){var d=L.DomUtil.create('div','leaflet-bar mm-centre');var b=L.DomUtil.create('button','',d);b.type='button';b.id='fence-draw';b.setAttribute('aria-label','Draw a fence');b.setAttribute('data-tip','Draw a fence on the map');b.setAttribute('data-tip-more','Press the corners, then Finish; a crossing raises an alert');
      b.innerHTML="<svg viewBox='0 0 16 16' fill='none' stroke='currentColor' stroke-width='1.6' stroke-linecap='round' stroke-linejoin='round' aria-hidden='true'><path d='M8 1.8l6 4.4-2.3 7H4.3L2 6.2z'/></svg>";
      L.DomEvent.disableClickPropagation(d);L.DomEvent.on(b,'click',function(ev){L.DomEvent.stop(ev);if(drawing){fenceCancel();}else{fenceStart('polygon');}});return d;}});
  map.addControl(new FenceCtl({position:'topleft'}));
  var fenceBar=null;
  function fenceReadout(t){var el=document.getElementById('map-readout');if(el){el.textContent=t;}}
  function fenceStart(kind){drawing=kind;corners=[];draft_.clearLayers();document.getElementById('map-geo').style.cursor='crosshair';
    if(!fenceBar){fenceBar=document.createElement('div');fenceBar.className='row-actions';fenceBar.id='fence-bar';fenceBar.style.margin='var(--s2) 0';
      fenceBar.innerHTML="<span class='meta' id='fence-hint'></span><button type='button' class='line' id='fence-finish'>Finish</button><button type='button' class='quiet' id='fence-undo'>Undo</button><button type='button' class='quiet' id='fence-circle'>Circle instead</button><label class='meta' id='fence-rlabel' hidden>Radius (metres) <input type='number' id='fence-r' value='200' min='10' max='100000' style='width:7em;margin:0'></label><button type='button' class='quiet' id='fence-cancel2'>Cancel</button>";
      var geo=document.getElementById('map-geo');geo.parentNode.insertBefore(fenceBar,geo.nextSibling);
      fenceBar.querySelector('#fence-finish').addEventListener('click',fenceFinish);fenceBar.querySelector('#fence-undo').addEventListener('click',function(){corners.pop();fenceDraft();});
      fenceBar.querySelector('#fence-cancel2').addEventListener('click',fenceCancel);
      fenceBar.querySelector('#fence-circle').addEventListener('click',function(){drawing=drawing==='circle'?'polygon':'circle';corners=[];fenceDraft();});
      fenceBar.querySelector('#fence-r').addEventListener('input',function(ev){fenceRadius=parseFloat(ev.target.value)||200;fenceDraft();});}
    fenceBar.hidden=false;fenceDraft();}
  function fenceDraft(){draft_.clearLayers();var hint=document.getElementById('fence-hint'),rl=document.getElementById('fence-rlabel'),cb=document.getElementById('fence-circle');
    if(cb){cb.textContent=drawing==='circle'?'Outline instead':'Circle instead';}if(rl){rl.hidden=drawing!=='circle';}
    if(drawing==='circle'){if(hint)hint.textContent=corners.length?'centre set: adjust the radius, then Finish':'press the map at the centre';
      if(corners.length){L.circle(corners[0],{radius:fenceRadius,color:tok('--gold'),weight:3,dashArray:'6 6',fill:true,fillOpacity:.08}).addTo(draft_);}return;}
    if(hint)hint.textContent=corners.length<3?('press the corners ('+corners.length+' of 3 at least)'):(corners.length+' corners: press Finish, or another corner');
    corners.forEach(function(c,i){L.circleMarker(c,{radius:6,color:tok('--gold'),weight:2,fillColor:tok('--surface-raised'),fillOpacity:1}).bindTooltip(String(i+1),{permanent:true,direction:'top',className:'mm-ring'}).addTo(draft_);});
    if(corners.length>1){L.polyline(corners.concat(corners.length>2?[corners[0]]:[]),{color:tok('--gold'),weight:3,dashArray:'6 6'}).addTo(draft_);}}
  function fenceCancel(){drawing=null;corners=[];draft_.clearLayers();if(fenceBar)fenceBar.hidden=true;document.getElementById('map-geo').style.cursor='';var ff=document.getElementById('fence-form');if(ff)ff.hidden=true;}
  function fenceFinish(){var form=document.getElementById('fence-form');if(!form)return;var f=form.querySelector('form'),shape=document.getElementById('fence-shape');
    if(drawing==='circle'){if(!corners.length){return;}f.elements.kind.value='circle';f.elements.lat.value=corners[0][0].toFixed(6);f.elements.lon.value=corners[0][1].toFixed(6);f.elements.radius_m.value=Math.round(fenceRadius);f.elements.points.value='';
      if(shape)shape.textContent='a circle of '+dist(fenceRadius)+' round '+MG.mgrs(corners[0][0],corners[0][1],4);}
    else{if(corners.length<3){var h=document.getElementById('fence-hint');if(h)h.textContent='three corners at least';return;}f.elements.kind.value='polygon';f.elements.points.value=JSON.stringify(corners.map(function(c){return [+c[0].toFixed(6),+c[1].toFixed(6)];}));f.elements.lat.value='';f.elements.lon.value='';f.elements.radius_m.value='';
      if(shape)shape.textContent=corners.length+' corners';}
    form.hidden=false;f.elements.name.focus();}
  map.on('click',function(ev){if(!drawing)return;if(drawing==='circle'){corners=[[ev.latlng.lat,ev.latlng.lng]];}else{corners.push([ev.latlng.lat,ev.latlng.lng]);}fenceDraft();});
  map.on('dblclick',function(ev){if(drawing==='polygon'&&corners.length>=3){L.DomEvent.stop(ev);fenceFinish();}});
  var fc=document.getElementById('fence-cancel');if(fc){fc.addEventListener('click',fenceCancel);}
  function fetchFences(){fetch('/api/fences').then(function(r){return r.json();}).then(function(j){fences_.clearLayers();var tb=document.getElementById('fence-list');var rows='';
    (j.fences||[]).forEach(function(f){var col=f.enabled===false?tok('--ink-muted'):tok('--gold');var lyr=f.kind==='circle'&&f.centre?L.circle(f.centre,{radius:f.radius_m||0,color:col,weight:3,dashArray:'6 6',fillOpacity:.06}):(f.points&&f.points.length>2?L.polygon(f.points,{color:col,weight:3,dashArray:'6 6',fillOpacity:.06}):null);
      if(lyr){lyr.bindTooltip(f.name||f.id,{permanent:true,direction:'center',className:'mm-ring'}).addTo(fences_);}
      var when={enter:'coming in',leave:'going out',both:'either way'}[f.rule]||f.rule;
      var tr=document.createElement('tr');tr.innerHTML="<td><b></b><div class='sub'></div></td><td></td><td></td><td><div class='row-actions'></div><div class='res meta' role='status'></div></td>";
      tr.querySelector('b').textContent=f.name||f.id;tr.querySelector('.sub').textContent=(f.kind==='circle'?('circle, '+dist(f.radius_m||0)):((f.points||[]).length+' corners'))+(f.enabled===false?' · off':'');
      tr.children[1].textContent=when;tr.children[2].textContent=f.group||'everyone';
      var ra=tr.querySelector('.row-actions');
      var tog=document.createElement('button');tog.type='button';tog.className='line';tog.textContent=f.enabled===false?'Turn on':'Turn off';tog.addEventListener('click',function(){fetch('/api/fence_set',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:f.id,enabled:f.enabled===false?'on':'off'})}).then(fetchFences);});
      var rm=document.createElement('button');rm.type='button';rm.className='danger';rm.textContent='Remove';rm.addEventListener('click',function(){window.mmConfirm('Remove the fence '+(f.name||f.id)+'? It no longer alerts. Nothing is sent to any device.',tr.children[3],function(){fetch('/api/fence_delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:f.id})}).then(fetchFences);});});
      ra.appendChild(tog);ra.appendChild(rm);if(tb){tb.appendChild(tr);}rows+='x';});
    if(tb){while(tb.children.length>(j.fences||[]).length){tb.removeChild(tb.firstChild);}if(!(j.fences||[]).length){tb.innerHTML="<tr><td colspan=4 class='meta'>No fence yet. Press the outline on the map (under Centre) and draw one.</td></tr>";}}}).catch(function(){});}
  document.addEventListener('mm-written',function(ev){var a=ev.detail&&ev.detail.action;if(a==='fence_set'||a==='fence_delete'){fenceCancel();fetchFences();}});
  fetchFences();
  function band(v){if(v===null||v===undefined)return 0;return v>=10?4:v>=5?3:v>=-7?2:v>=-12?1:0;}
  function bandTok(b){return b>=3?'--ok':b===2?'--warn':'--bad';}
  function dist(m){return m>=1000?(m/1000).toFixed(m>=10000?0:1)+' km':Math.round(m)+' m';}
  function draw(J){lastJ=J;(J.nodes||[]).forEach(function(n){names_[n.id]=n.label||n.name||n.id;});if(J.own&&J.own.id)names_[J.own.id]=J.own.name||'this box';overlay.clearLayers();fetchGraph();var own=J.own||{};if(own.lat===null||own.lat===undefined||own.lon===null||own.lon===undefined){ownLL=null;centreBtn(false);return;}var c=[own.lat,own.lon];ownLL=L.latLng(c[0],c[1]);centreBtn(true);readout(ownLL,'this box');
    var g=groupChosen();var byId={},pts=[];(J.nodes||[]).forEach(function(n){byId[n.id]=n;groups_[n.id]=n.group||'';if(n.heard_here===false)return;if(g&&(n.group||'')!==g)return;if(n.lat===null||n.lat===undefined||n.lon===null||n.lon===undefined)return;pts.push(n);});
    centre=c;rings();
    pts.forEach(function(n){var ll=[n.lat,n.lon],ds=n.direct_snr;
      if(ds!==null&&ds!==undefined){L.polyline([c,ll],{color:tok(bandTok(band(ds))),weight:3}).bindTooltip(ds+' dB',{permanent:true,direction:'center',className:'mm-link'}).addTo(overlay);}
      else{L.polyline([c,ll],{color:tok('--ink-muted'),weight:2,dashArray:'6 6'}).addTo(overlay);}});
    Object.keys(J.routes||{}).forEach(function(d){var rt=J.routes[d],path=[c];(rt.towards||[]).forEach(function(h){var n=byId[h.id];if(n&&n.lat!==null&&n.lat!==undefined&&n.lon!==null&&n.lon!==undefined){path.push([n.lat,n.lon]);}});
      if(path.length>1){L.polyline(path,{color:tok('--gold'),weight:5,opacity:.7}).addTo(overlay);}});
    pts.forEach(function(n){L.marker([n.lat,n.lon],{icon:nodeIcon(n.icon,''),keyboard:false}).bindTooltip(n.label||n.name||n.id,{permanent:true,direction:'bottom',className:'mm-node'}).addTo(overlay);});
    L.circleMarker(c,{radius:9,color:tok('--gold'),weight:2,fillColor:tok('--accent'),fillOpacity:1}).bindTooltip(own.name||'this box',{permanent:true,direction:'bottom',className:'mm-node'}).addTo(overlay);
    var nopos=(J.nodes||[]).filter(function(n){return n.heard_here!==false&&(n.lat===null||n.lat===undefined||n.lon===null||n.lon===undefined);});
    var ul=document.getElementById('nopos');if(ul){ul.innerHTML=nopos.length?'<b>Heard, but no position, so not on the map:</b> '+nopos.map(function(n){var t=document.createElement('span');t.textContent=(n.label||n.name||n.id)+((n.direct_snr!==null&&n.direct_snr!==undefined)?' ('+n.direct_snr+' dB)':' (relayed)');return t.innerHTML;}).join(', '):'';}
    if(!fitted&&sized()&&!benchAt){var b=L.latLngBounds([c]);pts.forEach(function(n){b.extend([n.lat,n.lon]);});map.fitBounds(b.pad(0.35),{maxZoom:17});fitted=true;}
    if(benchAt&&!fitted&&sized()){map.setView(benchAt,16);fitted=true;}}
  // Spec 040 and Spec 047: playback. Every node where it last was at instant T (nothing interpolated), the run it had
  // walked since its last gap, hollow with the age when its last report is older than its gap; a timeline with a row per
  // node and a tick per report, seekable by press or drag; speeds, reverse, fit, keys. Built from the trails rows the map holds.
  /* pure:start */
  function posAt(rows,node,t){var best=null;for(var i=0;i<rows.length;i++){var r=rows[i];if(r.node!==node)continue;var ts=Date.parse(r.ts);if(ts<=t&&(!best||ts>Date.parse(best.ts)))best=r;}return best;}
  function medianInterval(times){if(!times||times.length<2)return null;var d=[];for(var i=1;i<times.length;i++)d.push(times[i]-times[i-1]);d.sort(function(a,b){return a-b;});var m=Math.floor(d.length/2);return d.length%2?d[m]:(d[m-1]+d[m])/2;}
  function gapFor(times){var med=medianInterval(times);return Math.max(120000,med?4*med:120000);}
  function isStaleAt(t,times,gap){var last=null;for(var i=0;i<times.length;i++){if(times[i]<=t&&(last===null||times[i]>last))last=times[i];}return last===null||(t-last)>gap;}
  // one step of the clock: forward stops only at the end, backward only at the start; the window slides with now, so a T just below
  // the start is clamped, never mistaken for the end of a backward run (found on the demo: play stopped before its first frame)
  function stepPlay(T,dt,dir,spd,rg){var nT=T+dt*dir*spd,playing=true;if(dir>0&&nT>=rg[1]){nT=rg[1];playing=false;}else if(dir<0&&nT<=rg[0]){nT=rg[0];playing=false;}if(nT<rg[0])nT=rg[0];if(nT>rg[1])nT=rg[1];return {T:nT,playing:playing};}
  /* pure:end */
  var play_=L.layerGroup().addTo(map),playT=document.getElementById('play-t'),playGo=document.getElementById('play-go'),playAt=document.getElementById('play-at'),playPos=document.getElementById('play-pos');
  var SPEEDS=[1,10,60,300,1000],speedSel=document.getElementById('play-speed'),tl=document.getElementById('timeline'),tctx=tl?tl.getContext('2d'):null;
  var T=null,playing=false,dir=1,lastFrame=null,hidden_={},scrubbing=false,tlLabelW=110,tlHead=16,tlRows=[],tlRowH=12;
  var names_={},groups_={};
  function groupChosen(){var el=document.getElementById('group-filter');return el?el.value:'';}
  function nodeIcon(kind,extra){var svg=(window.mmNodeIcons||{})[kind]||(window.mmNodeIcons||{}).radio||'';return L.divIcon({className:'mm-pin '+(extra||''),html:"<span class='mm-pin-in'>"+svg+"</span>",iconSize:[30,30],iconAnchor:[15,15],tooltipAnchor:[0,15]});}
  var gsel_=document.getElementById('group-filter');if(gsel_){gsel_.addEventListener('change',function(){if(lastJ)draw(lastJ);drawTrails(lastRows);renderPlay();});}
  function playRange(){var hrs=parseFloat(trailHours());if(!(hrs>0))hrs=3;var now=Date.now();return [now-hrs*3600*1000,now];}
  function speed(){return SPEEDS[speedSel?parseInt(speedSel.value,10):0]||1;}
  function tracks(){var g=groupChosen();var by={};lastRows.forEach(function(r){if(r.lat===null||r.lon===null)return;if(g&&(groups_[r.node]||'')!==g)return;(by[r.node]=by[r.node]||[]).push(r);});
    return Object.keys(by).sort(function(a,b){return (names_[a]||a).toLowerCase()<(names_[b]||b).toLowerCase()?-1:1;}).map(function(id){var pts=by[id].slice().sort(function(a,b){return a.ts<b.ts?-1:1;});var times=pts.map(function(r){return Date.parse(r.ts);});return {id:id,pts:pts,times:times,gap:gapFor(times)};});}
  function two(n){return ('0'+n).slice(-2);}
  function fmtUTC(t){var d=new Date(t);return two(d.getUTCHours())+':'+two(d.getUTCMinutes())+':'+two(d.getUTCSeconds())+'Z';}
  function fmtDur(ms){var s=Math.max(0,Math.round(ms/1000));var h=Math.floor(s/3600),m=Math.floor((s%3600)/60);return (h?h+' h ':'')+m+' min';}
  function fmtAge(ms){var s=Math.max(0,Math.round(ms/1000));if(s<60)return s+' s';var m=Math.floor(s/60);if(m<60)return m+' min';return Math.floor(m/60)+' h '+(m%60)+' min';}
  function atEnd(){var rg=playRange();return T===null||T>=rg[1]-1000;}
  function renderPlay(){play_.clearLayers();var rg=playRange();
    if(atEnd()){if(!map.hasLayer(overlay))map.addLayer(overlay);if(!map.hasLayer(trails_))map.addLayer(trails_);if(playAt)playAt.textContent='now';if(playPos)playPos.textContent='';if(playT&&!scrubbing)playT.value=1000;drawTimeline();return;}
    if(map.hasLayer(overlay))map.removeLayer(overlay);if(map.hasLayer(trails_))map.removeLayer(trails_);
    if(playAt)playAt.textContent=fmtUTC(T);if(playPos)playPos.textContent=fmtDur(T-rg[0])+' of '+fmtDur(rg[1]-rg[0]);if(playT&&!scrubbing)playT.value=Math.round((T-rg[0])/(rg[1]-rg[0])*1000);
    tracks().forEach(function(tr){if(hidden_[tr.id])return;var p=posAt(tr.pts,tr.id,T);if(!p)return;var stale=isStaleAt(T,tr.times,tr.gap);
      var run=[];for(var i=tr.pts.length-1;i>=0;i--){var ts=tr.times[i];if(ts>T)continue;if(run.length&&(Date.parse(run[0].ts)-ts)>tr.gap)break;run.unshift(tr.pts[i]);}
      if(run.length>1)L.polyline(run.map(function(r){return [r.lat,r.lon];}),{color:tok('--gold'),weight:4,opacity:.8}).addTo(play_);
      var nn=(lastJ&&lastJ.nodes||[]).filter(function(x){return x.id===tr.id;})[0]||{};
      L.marker([p.lat,p.lon],{icon:nodeIcon(nn.icon,'play'+(stale?' stale':'')),keyboard:false}).bindTooltip((names_[tr.id]||tr.id)+(stale?' · '+fmtAge(T-Date.parse(p.ts))+' old':''),{permanent:true,direction:'bottom',className:'mm-node'}).addTo(play_);});
    drawTimeline();}
  function tlLayout(){if(!tl)return null;var r=tl.getBoundingClientRect(),dpr=window.devicePixelRatio||1;var w=Math.max(200,Math.floor(r.width)),h=Math.max(40,Math.floor(r.height));if(tl.width!==Math.round(w*dpr)||tl.height!==Math.round(h*dpr)){tl.width=Math.round(w*dpr);tl.height=Math.round(h*dpr);}tctx.setTransform(dpr,0,0,dpr,0,0);return [w,h];}
  function drawTimeline(){if(!tctx)return;tlRows=tracks();var want=Math.max(60,tlHead+Math.max(1,tlRows.length)*14+6);if(Math.abs(tl.clientHeight-want)>2){tl.style.height=want+'px';}
    var dims=tlLayout();if(!dims)return;var W=dims[0],H=dims[1];var rg=playRange(),W0=rg[0],W1=rg[1];tctx.clearRect(0,0,W,H);
    tlRowH=Math.max(8,Math.min(18,(H-tlHead-2)/Math.max(1,tlRows.length)));var x=function(t){return tlLabelW+(t-W0)/(W1-W0)*(W-tlLabelW-6);};
    tctx.font='10px -apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif';tctx.textBaseline='top';var span=W1-W0;var step=span>30*36e5?6*36e5:span>8*36e5?36e5:span>2*36e5?15*6e4:5*6e4;
    for(var m=Math.ceil(W0/step)*step;m<W1;m+=step){tctx.fillStyle=tok('--line');tctx.fillRect(x(m),tlHead-3,1,H-tlHead+3);tctx.fillStyle=tok('--ink-muted');tctx.fillText(fmtUTC(m).slice(0,5)+'Z',x(m)+3,2);}
    if(!tlRows.length){tctx.fillStyle=tok('--ink-muted');tctx.fillText('no position reports in the window',tlLabelW,tlHead+4);}
    tlRows.forEach(function(tr,i){var y=tlHead+i*tlRowH;tctx.globalAlpha=hidden_[tr.id]?.45:1;tctx.fillStyle=tok('--ink');tctx.fillText((names_[tr.id]||tr.id).slice(0,16),4,y+Math.max(0,(tlRowH-11)/2));
      tctx.fillStyle=tok('--accent');tr.times.forEach(function(t){tctx.fillRect(x(t),y+1,1.5,Math.max(2,tlRowH-2));});tctx.globalAlpha=1;});
    var cx=x(atEnd()?W1:T);tctx.fillStyle=tok('--gold');tctx.fillRect(cx-1,0,2,H);}
  function seekFromEvent(ev){var r=tl.getBoundingClientRect();var rg=playRange();var f=Math.min(1,Math.max(0,(ev.clientX-r.left-tlLabelW)/(r.width-tlLabelW-6)));T=rg[0]+f*(rg[1]-rg[0]);renderPlay();}
  if(tl){tl.addEventListener('pointerdown',function(ev){var r=tl.getBoundingClientRect();if(ev.clientX-r.left<tlLabelW){var row=Math.floor((ev.clientY-r.top-tlHead)/tlRowH);var tr=tlRows[row];if(tr){hidden_[tr.id]=!hidden_[tr.id];renderPlay();}return;}scrubbing=true;try{tl.setPointerCapture(ev.pointerId);}catch(e){}seekFromEvent(ev);});
    tl.addEventListener('pointermove',function(ev){if(scrubbing)seekFromEvent(ev);});tl.addEventListener('pointerup',function(ev){scrubbing=false;try{tl.releasePointerCapture(ev.pointerId);}catch(e){}});tl.addEventListener('pointercancel',function(){scrubbing=false;});
    if(window.ResizeObserver){new ResizeObserver(function(){drawTimeline();}).observe(tl);}}
  function setPlaying(p){playing=p;lastFrame=null;if(playGo){var ic=window.mmIcons||{};playGo.setAttribute('aria-label',p?'Pause':'Play the window back');playGo.setAttribute('data-tip',p?'Pause':'Play the window back');playGo.innerHTML=(p?(ic.pause||''):(ic.play||''))+"<span class='lbl'>"+(p?'Pause':'Play')+"</span>";}}
  function frame(now){if(playing&&!scrubbing){var rg=playRange();if(T===null)T=dir>0?rg[0]:rg[1];if(lastFrame!==null){var st=stepPlay(T,now-lastFrame,dir,speed(),rg);T=st.T;if(!st.playing)setPlaying(false);}lastFrame=now;renderPlay();}requestAnimationFrame(frame);}
  requestAnimationFrame(frame);
  if(playT){playT.addEventListener('input',function(){var rg=playRange();T=rg[0]+(rg[1]-rg[0])*parseInt(playT.value,10)/1000;scrubbing=true;renderPlay();scrubbing=false;});}
  if(playGo){playGo.addEventListener('click',function(){if(playing){setPlaying(false);return;}if(atEnd()&&dir>0){T=playRange()[0];}setPlaying(true);});}
  var ps=document.getElementById('play-start');if(ps){ps.addEventListener('click',function(){var rg=playRange();T=dir>0?rg[0]:rg[1];renderPlay();});}
  var pr=document.getElementById('play-rev');if(pr){pr.addEventListener('click',function(){dir=-dir;pr.setAttribute('aria-pressed',dir<0?'true':'false');pr.classList.toggle('on',dir<0);});}
  var pf=document.getElementById('play-fit');if(pf){pf.addEventListener('click',function(){var b=null;tracks().forEach(function(tr){if(hidden_[tr.id])return;tr.pts.forEach(function(r){var ll=L.latLng(r.lat,r.lon);b=b?b.extend(ll):L.latLngBounds([ll]);});});if(b&&b.isValid()){map.fitBounds(b.pad(0.25));}});}
  // keys: space, the arrows (one per cent of the window, ten with shift), [ and ] for speed, r to reverse, f to fit; never while typing
  window.addEventListener('keydown',function(ev){var tg=ev.target&&ev.target.tagName;if(tg==='INPUT'||tg==='SELECT'||tg==='TEXTAREA')return;var geo=document.getElementById('map-geo');if(!geo||geo.closest('[hidden]'))return;var rg=playRange(),win=rg[1]-rg[0];
    if(ev.code==='Space'||ev.key===' '){ev.preventDefault();if(playGo)playGo.click();}
    else if(ev.key==='ArrowLeft'||ev.key==='ArrowRight'){ev.preventDefault();if(T===null)T=rg[1];T=Math.min(rg[1],Math.max(rg[0],T+(ev.key==='ArrowRight'?1:-1)*win*(ev.shiftKey?0.1:0.01)));renderPlay();}
    else if(ev.key==='['||ev.key===']'){if(speedSel){var i=parseInt(speedSel.value,10)+(ev.key===']'?1:-1);if(i>=0&&i<SPEEDS.length)speedSel.value=String(i);}}
    else if(ev.key==='r'){if(pr)pr.click();}else if(ev.key==='f'){if(pf)pf.click();}});
  // Spec 041: waypoints heard on the mesh, as pins; Spec 042: neighbour edges between positioned nodes
  var wps_=L.layerGroup().addTo(map),graph_=L.layerGroup().addTo(map),wpsOn=document.getElementById('wps-on'),graphOn=document.getElementById('graph-on');
  function fetchWaypoints(){if(wpsOn&&!wpsOn.checked){wps_.clearLayers();return;}fetch('/api/waypoints').then(function(r){return r.json();}).then(function(j){wps_.clearLayers();(j.waypoints||[]).forEach(function(w){if(w.lat===null||w.lat===undefined)return;
    L.circleMarker([w.lat,w.lon],{radius:7,color:tok('--gold'),weight:3,fillColor:tok('--surface-raised'),fillOpacity:1}).bindTooltip(w.name+(w.description?' · '+w.description:''),{permanent:true,direction:'top',className:'mm-node'}).addTo(wps_);});}).catch(function(){});}
  function fetchGraph(){if(!graphOn||!graphOn.checked||!lastJ){graph_.clearLayers();return;}fetch('/api/neighbors?hours='+(trailHours()||24)).then(function(r){return r.json();}).then(function(j){graph_.clearLayers();var by={};(lastJ.nodes||[]).forEach(function(n){by[n.id]=n;});if(lastJ.own&&lastJ.own.id)by[lastJ.own.id]=lastJ.own;
    (j.edges||[]).forEach(function(x){var a=by[x.from],b=by[x.to];if(!a||!b||a.lat===null||a.lat===undefined||b.lat===null||b.lat===undefined)return;
      L.polyline([[a.lat,a.lon],[b.lat,b.lon]],{color:tok(bandTok(band(x.snr))),weight:2,dashArray:'2 6',opacity:.9}).bindTooltip((x.from_name||x.from)+' hears '+(x.to_name||x.to)+' at '+x.snr+' dB',{sticky:true,className:'mm-link'}).addTo(graph_);});}).catch(function(){});}
  if(wpsOn)wpsOn.addEventListener('change',fetchWaypoints);if(graphOn)graphOn.addEventListener('change',fetchGraph);
  fetchWaypoints();setInterval(fetchWaypoints,60000);
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


def mesh_views(L, tiles, size=640, bare=False, tak_on=True):
    """The two views: the map over tiles (Leaflet, when the box has a position) and the plan.
    bare: the map on a page of its own (no Pop out control)."""
    own = L.get("own") or {}
    has = own.get("lat") is not None and own.get("lon") is not None
    attr = json.dumps(tiles).replace("&", "&amp;").replace("'", "&#39;").replace('"', "&quot;")
    why = "" if has else "<p class='meta'>No position for this box, so the map view is off and the plan view places nodes by hops. Give the box its position on <a href='/settings#position'>Settings</a>, or plug in a GPS receiver.</p>"
    groups = sorted({str(n.get("group")) for n in (L.get("nodes") or []) if n.get("group")})
    gsel = ("<label class='meta' data-tip='Group' data-tip-more='Only this group&#39;s devices on the map, the trails and the playback'>Group <select id='group-filter'><option value=''>everyone</option>" + "".join(f"<option value='{e(g)}'>{e(g)}</option>" for g in groups) + "</select></label>") if groups else "<select id='group-filter' hidden aria-hidden='true'><option value=''></option></select>"
    layers = ("<details class='fold ctl' id='layers' style='margin-top:0'><summary data-tip='Layers, trails, rings and grid' data-tip-more='What the map draws besides the nodes'>" + ICONS["layers"] + "Map layers</summary><div class='controls' style='margin:var(--s2) 0 0'>" + gsel +
              "<span class='meta' data-tip='Rings' data-tip-more='Range rings and their labels: geometry, not what the radio will reach'>Rings</span>" + seg("rings", (("0", "off"), ("60", "faint"), ("100", "solid")), "60") + "<span class='meta' id='ring-step'></span>"
              "<label class='meta' for='trail-hours' data-tip='Trails' data-tip-more='Each node&#39;s track over the window, fading with age'>Trails <select id='trail-hours'><option value='0'>off</option><option value='1'>1 h</option><option value='3' selected>3 h</option><option value='12'>12 h</option><option value='24'>24 h</option><option value='72'>3 d</option></select></label>"
              "<label class='meta check' data-tip='Neighbours' data-tip-more='Who hears whom, from the neighbour reports nodes broadcast'><input type='checkbox' id='graph-on'> Neighbours</label>"
              "<label class='meta check' data-tip='Waypoints' data-tip-more='Waypoints heard on the mesh, as pins'><input type='checkbox' id='wps-on' checked> Waypoints</label>"
              "<label class='meta' for='cover-hours' data-tip='Coverage' data-tip-more='Every position heard, coloured by signal; hollow came through a relay'>Coverage <select id='cover-hours'><option value='0' selected>off</option><option value='3'>3 h</option><option value='24'>24 h</option><option value='168'>7 d</option></select></label>"
              "<label class='meta check' for='grid-on' data-tip='Grid' data-tip-more='MGRS: 1 km lines from zoom 13, 10 km below'><input type='checkbox' id='grid-on'> Grid</label>"
              "</div></details>")
    wp = ("<details class='fold ctl' style='margin-top:var(--s3)'><summary>Drop a waypoint on the mesh</summary><form class='card' data-action='waypoint_send' data-risk='air' data-confirm='This reaches every device on the primary channel" + (" and is forwarded to TAK as a marker.'>" if tak_on else ".'>") +
          "<label>Name (30 bytes at most)<input type='text' name='name' maxlength='30' required></label><label>Description (100 bytes at most)<input type='text' name='description' maxlength='100'></label>"
          "<div class='row-actions' style='margin-bottom:var(--s2)'>" + icon_button("place", "Place it on the map", "Place it on the map", "Then press the map where the waypoint goes; the fields fill in", attrs="id='wp-place'") + "<span class='meta' id='wp-place-note'></span></div>"
          "<label>Grid (MGRS), or the degrees below<input type='text' id='wp-mgrs' placeholder='30U XC 9988 0936' autocomplete='off' aria-describedby='wp-mgrs-note'><span class='meta' id='wp-mgrs-note'></span></label>"
          "<label>Latitude<input type='text' name='lat' id='wp-lat' inputmode='decimal' required placeholder='51.5000'></label><label>Longitude<input type='text' name='lon' id='wp-lon' inputmode='decimal' required placeholder='-0.1200'></label>"
          "<label>Expires in (minutes)<input type='number' name='expire_min' value='60' min='1' max='10080'></label><button type='submit'>Send the waypoint</button><div class='res meta' role='status'></div></form></details>")
    return (f"<div id='mesh-views' data-default-view='{'map' if has else 'plan'}' data-has-position='{'1' if has else '0'}' data-tiles='{attr}'>"
            f"<div class='controls views'><button type='button' class='line' data-view='map' aria-label='Map view' data-tip='The map with imagery'>{ICONS['map']}Map</button><button type='button' class='line' data-view='plan' aria-label='Plan view' data-tip='Range rings from this box' data-tip-more='The mesh drawn round this box, without imagery'>{ICONS['plan']}Plan</button>"
            + ("" if bare else icon_button("popout", "Open the map in a window", "Open the map in a window", "The map on its own, in a window of its own", attrs="id='map-pop'"))
            + layers + "<span class='meta' id='tiles-now'></span><span class='meta warn' id='tiles-note'></span></div>"
            f"<div data-view='map' hidden><div id='map-geo' class='geo'></div>{playback_bar()}{why}<div class='meta' id='nopos' style='margin-top:var(--s2)'></div>{fence_forms(L)}{wp}</div>"
            f"<div data-view='plan' id='map-box'>{map_svg(L, size)}</div></div><script>window.mmNodeIcons={json.dumps(NODE_ICON_SVG)};window.mmIcons={json.dumps({'play': ICONS['play'], 'pause': ICONS['pause']})};</script>{OVERLAY_JS}")


def playback_bar():
    """Spec 047: the playback controls and the timeline under the map. The window is the trails window."""
    speeds = "".join(f"<option value='{i}'{' selected' if v == 60 else ''}>{v}x</option>" for i, v in enumerate((1, 10, 60, 300, 1000)))
    return ("<div id='playback' style='margin-top:var(--s2)'><div class='controls' style='margin-bottom:var(--s1)'>"
            + icon_button("skip_back", "To the start", "To the start", "Of the window, or its end when playing backwards", attrs="id='play-start'")
            + icon_button("play", "Play the window back", "Play the window back", "Space plays and pauses", attrs="id='play-go'")
            + icon_button("reverse", "Reverse", "Reverse", "Play backwards (r)", attrs="id='play-rev' aria-pressed='false'")
            + f"<label class='meta' data-tip='Speed' data-tip-more='[ and ] change it'>Speed <select id='play-speed'>{speeds}</select></label>"
            + icon_button("fit", "Fit the map to the tracks", "Fit the map to the tracks", "The whole window's tracks in view (f)", attrs="id='play-fit'")
            + "<input type='range' id='play-t' min='0' max='1000' value='1000' aria-label='Scrub the window' style='width:160px;vertical-align:middle;margin:0'>"
            "<span id='play-at' class='meta' style='font-variant-numeric:tabular-nums'></span><span id='play-pos' class='meta'></span></div>"
            "<canvas id='timeline' style='width:100%;height:80px;display:block;border:1px solid var(--line);border-radius:var(--r);background:var(--surface-raised);touch-action:none' aria-label='Timeline: a row per node, a tick per report; press or drag to seek, press a name to hide it'></canvas>"
            "<p class='meta' style='margin:var(--s1) 0 0'>A row per node, a tick per position report; press or drag to seek, press a name to hide it. Nothing is interpolated: a node sits where it last reported, hollow with the age when that is older than its usual gap. For a record, export the positions on Health and open them in Pinecone.</p></div>")


def fence_forms(L):
    """Spec 045: the card that names a drawn fence, and the list of fences on this box."""
    fs = _act("fence_set")
    groups = sorted({str(n.get("group")) for n in (L.get("nodes") or []) if n.get("group")})
    gsel = "<label>Applies to<select name='group'><option value=''>everyone</option>" + "".join(f"<option value='{e(g)}'>{e(g)}</option>" for g in groups) + "</select></label>"
    return (f"<div id='fence-form' class='card' hidden style='margin-top:var(--s3);max-width:560px'><form data-action='fence_set' data-risk='change' data-confirm=\"{e(fs.get('confirm') or '')}\" data-clear='1'>"
            "<h2 style='margin-top:0'>Name the fence</h2><p class='meta' id='fence-shape'></p>"
            "<input type='hidden' name='kind' value='polygon'><input type='hidden' name='points'><input type='hidden' name='lat'><input type='hidden' name='lon'><input type='hidden' name='radius_m'>"
            "<label>Name (40 bytes at most)<input type='text' name='name' maxlength='40' required placeholder='e.g. Compound'></label>"
            "<div><span class='meta'>Alert when</span><br>" + seg("rule", (("enter", "Coming in"), ("leave", "Going out"), ("both", "Either")), "both") + "</div>"
            + gsel +
            "<div class='row-actions'><button type='submit'>Save the fence</button><button type='button' class='quiet' id='fence-cancel'>Cancel</button></div><div class='res meta' role='status'></div></form></div>"
            "<details class='fold' id='fences' style='margin-top:var(--s3)'><summary>Fences</summary><p class='meta'>Areas drawn on the map. A device crossing one raises a geofence alert here and in TAK chat, by the same path as the quiet and battery alerts. A device with no position is neither in nor out.</p>"
            "<div class='tablewrap'><table><thead><tr><th>Fence</th><th>Alert when</th><th>Applies to</th><th></th></tr></thead><tbody id='fence-list'><tr><td colspan=4 class='meta'>loading</td></tr></tbody></table></div></details>")


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


def _svg(inner, fill="none"):
    return (f"<svg viewBox='0 0 16 16' fill='{fill}' stroke='currentColor' stroke-width='1.6' stroke-linecap='round' stroke-linejoin='round' aria-hidden='true'>{inner}</svg>")


# One drawing per meaning, the same on every page: 16 px, stroke only, drawn for this product.
ICONS = {
    "plus": "<svg viewBox='0 0 16 16' fill='none' stroke='currentColor' stroke-width='1.6' stroke-linecap='round' aria-hidden='true'><path d='M8 3v10M3 8h10'/></svg>",
    "check_all": "<svg viewBox='0 0 16 16' fill='none' stroke='currentColor' stroke-width='1.6' stroke-linecap='round' stroke-linejoin='round' aria-hidden='true'><path d='M1.5 8.5 4.5 11.5 10 5'/><path d='M7 11.5 8.5 13 14.5 6.5'/></svg>",
    "dots": "<svg viewBox='0 0 16 16' fill='currentColor' aria-hidden='true'><circle cx='3' cy='8' r='1.4'/><circle cx='8' cy='8' r='1.4'/><circle cx='13' cy='8' r='1.4'/></svg>",
    "bell_off": "<svg viewBox='0 0 16 16' fill='none' stroke='currentColor' stroke-width='1.5' stroke-linecap='round' stroke-linejoin='round' aria-hidden='true'><path d='M4 11V7a4 4 0 0 1 6.5-3.1M12 7v4l1 1.5H3L4 11'/><path d='M6.5 13.5a1.5 1.5 0 0 0 3 0'/><path d='M2 2l12 12'/></svg>",
    "pin": "<svg viewBox='0 0 16 16' fill='none' stroke='currentColor' stroke-width='1.5' stroke-linecap='round' stroke-linejoin='round' aria-hidden='true'><path d='M9.5 1.5 14.5 6.5 11.5 7.5 9 10l-.5 4.5L4.5 10.5 1.5 7.5 6 7l1-3z'/><path d='M4.5 10.5 1.5 13.5'/></svg>",

    "check": _svg("<path d='M3 8.5l3 3 7-7'/>"),
    "forget": _svg("<circle cx='8' cy='8' r='6'/><path d='M3.8 3.8l8.4 8.4'/>"),
    "sliders": _svg("<path d='M3 4h10M3 8h10M3 12h10'/><circle cx='6' cy='4' r='1.6' fill='currentColor'/><circle cx='10.5' cy='8' r='1.6' fill='currentColor'/><circle cx='5' cy='12' r='1.6' fill='currentColor'/>"),
    "read": _svg("<path d='M8 2v8M4.5 6.5 8 10l3.5-3.5M3 13.5h10'/>"),
    "export": _svg("<rect x='2' y='9' width='12' height='4.5' rx='1'/><path d='M8 1.5v5M6 4.5 8 6.5l2-2M11.5 11.3h.01'/>"),
    "restore": _svg("<path d='M2.5 8a5.5 5.5 0 1 0 1.6-3.9M2.5 2.5v4h4'/>"),
    "flash": _svg("<path d='M9 1.5 3.5 9h4l-1 5.5L13 7H9z'/>"),
    "onboard": _svg("<circle cx='8' cy='8' r='6'/><path d='M5.2 8.2l1.9 1.9L11 6.2'/>"),
    "trash": _svg("<path d='M2.5 4h11M6 4V2.5h4V4M4 4l.7 9.5h6.6L12 4M6.8 7v4M9.2 7v4'/>"),
    "key": _svg("<circle cx='5.5' cy='10.5' r='3'/><path d='M7.6 8.4 13.5 2.5M11 5l2 2'/>"),
    "qr": _svg("<rect x='2' y='2' width='4.5' height='4.5'/><rect x='9.5' y='2' width='4.5' height='4.5'/><rect x='2' y='9.5' width='4.5' height='4.5'/><path d='M9.5 9.5h2v2h-2zM14 9.5v1M9.5 14h1M12.5 12.5H14V14'/>"),
    "users": _svg("<circle cx='6' cy='5.5' r='2.5'/><path d='M1.5 14c.5-2.7 2.2-4 4.5-4s4 1.3 4.5 4M10.5 3.2a2.5 2.5 0 0 1 0 4.6M12 10c1.6.4 2.4 1.7 2.5 4'/>"),
    "shapes": _svg("<circle cx='11.5' cy='4.5' r='2.5'/><rect x='2' y='9' width='5' height='5' rx='1'/><path d='M4.5 2 7 6.5H2zM9.5 9.5h5v5h-5z'/>"),
    "fence": _svg("<path d='M8 1.8l6 4.4-2.3 7H4.3L2 6.2z'/>"),
    "layers": _svg("<path d='M8 2l6.5 3.5L8 9 1.5 5.5zM1.5 8.5 8 12l6.5-3.5M1.5 11.5 8 15l6.5-3.5'/>"),
    "type": _svg("<path d='M3 4V2.5h10V4M8 2.5v11M6 13.5h4'/>"),
    "sun": _svg("<circle cx='8' cy='8' r='3'/><path d='M8 1v1.5M8 13.5V15M1 8h1.5M13.5 8H15M3 3l1 1M12 12l1 1M3 13l1-1M12 4l1-1'/>"),
    "moon": _svg("<path d='M13.5 9.5A6 6 0 1 1 6.5 2.5a4.8 4.8 0 0 0 7 7z'/>"),
    "menu": _svg("<path d='M2.5 4h11M2.5 8h11M2.5 12h11'/>"),
    "help": _svg("<circle cx='8' cy='8' r='6.5'/><path d='M6 6.2a2 2 0 1 1 3 1.7c-.7.4-1 .8-1 1.6M8 12h.01'/>"),
    "refresh": _svg("<path d='M13.5 8a5.5 5.5 0 1 1-1.6-3.9M13.5 2.5v4h-4'/>"),
    "popout": _svg("<path d='M6.5 3H3v10h10V9.5M9.5 2.5h4v4M13.5 2.5 7.5 8.5'/>"),
    "play": _svg("<path d='M4.5 2.5v11l9-5.5z'/>", fill="currentColor"),
    "pause": _svg("<path d='M5 2.5v11M11 2.5v11' stroke-width='2.4'/>"),
    "skip_back": _svg("<path d='M3 2.5v11M13 3l-8 5 8 5z'/>", fill="currentColor"),
    "reverse": _svg("<path d='M8 3 2.5 8 8 13zM14 3 8.5 8l5.5 5z'/>", fill="currentColor"),
    "fit": _svg("<path d='M2.5 6V2.5H6M10 2.5h3.5V6M13.5 10v3.5H10M6 13.5H2.5V10'/>"),
    "close": _svg("<path d='M3.5 3.5l9 9M12.5 3.5l-9 9'/>"),
    "chevron": _svg("<path d='M3 6l5 5 5-5'/>"),
    "filter": _svg("<path d='M2 3h12L9.5 8.5v4.5l-3 1.5V8.5z'/>"),
    "map": _svg("<path d='M1.5 4l4-1.5 5 1.5 4-1.5v10l-4 1.5-5-1.5-4 1.5zM5.5 2.5v10M10.5 4v10'/>"),
    "plan": _svg("<circle cx='8' cy='8' r='6.5'/><circle cx='8' cy='8' r='3.5'/><circle cx='8' cy='8' r='1' fill='currentColor'/>"),
    "place": _svg("<path d='M8 14.5s-4.5-4.2-4.5-8A4.5 4.5 0 0 1 12.5 6.5c0 3.8-4.5 8-4.5 8z'/><path d='M8 4.5v4M6 6.5h4'/>"),
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


NODE_ICON_SVG = {
    "radio": _svg("<rect x='3' y='6' width='10' height='8' rx='1.5'/><path d='M8 6V2.5M5.5 2.5h5M6 9.5h.01M8 11.5h2'/>"),
    "person": _svg("<circle cx='8' cy='4.5' r='2.5'/><path d='M3 14c.4-3 2.3-4.5 5-4.5s4.6 1.5 5 4.5'/>"),
    "vehicle": _svg("<path d='M2 10l1.5-4h9L14 10v3H2z'/><circle cx='4.8' cy='12.5' r='1.3' fill='currentColor'/><circle cx='11.2' cy='12.5' r='1.3' fill='currentColor'/><path d='M2 10h12'/>"),
    "router": _svg("<path d='M8 2v12M4 14h8M8 2 4.5 8.5M8 2l3.5 6.5M5.6 6.8h4.8'/>"),
    "repeater": _svg("<path d='M8 14V6M6.5 3.5 8 2l1.5 1.5'/><path d='M3.5 6.5a6 6 0 0 1 9 0M5.5 8.5a3.5 3.5 0 0 1 5 0'/>"),
    "base": _svg("<path d='M2.5 8 8 2.5 13.5 8M4 7v7h8V7M6.5 14v-4h3v4'/>"),
    "drone": _svg("<circle cx='3.5' cy='3.5' r='1.8'/><circle cx='12.5' cy='3.5' r='1.8'/><circle cx='3.5' cy='12.5' r='1.8'/><circle cx='12.5' cy='12.5' r='1.8'/><path d='M5 5l6 6M11 5l-6 6'/><circle cx='8' cy='8' r='1.2' fill='currentColor'/>"),
    "boat": _svg("<path d='M2 10h12l-2 3.5H4zM8 10V2.5M8 3l4.5 6.5M8 3 4.5 9'/>"),
    "bike": _svg("<circle cx='4' cy='11' r='2.5'/><circle cx='12' cy='11' r='2.5'/><path d='M4 11 6.5 5.5H10L12 11M6.5 5.5H9M8.5 11l1.5-5'/>"),
    "dog": _svg("<path d='M3 9.5 4.5 5h4l2 2.5H13l.5 3.5-2 .5-.5 2.5h-2l-.5-2h-3l-.5 2h-2z'/><circle cx='6' cy='7.2' r='.6' fill='currentColor'/>"),
    "box": _svg("<path d='M2.5 5 8 2.5 13.5 5v6L8 13.5 2.5 11zM2.5 5 8 7.5 13.5 5M8 7.5v6'/>"),
    "medic": _svg("<path d='M6 2.5h4v3.5h3.5v4H10v3.5H6V10H2.5V6H6z'/>"),
    "flag": _svg("<path d='M3.5 14V2.5M3.5 3h8l-1.5 3 1.5 3h-8'/>"),
    "star": _svg("<path d='M8 2l1.8 3.9 4.2.5-3.1 2.9.8 4.2L8 11.4l-3.7 2.1.8-4.2L2 6.4l4.2-.5z'/>"),
}
assert set(NODE_ICON_SVG) >= set(NODE_ICONS)


def icon_picker(field, current, inherit=True):
    """The map icon chooser: a row of the drawings, one chosen, each with its word in the tip."""
    opts = ([("inherit", "From the group")] if inherit else []) + [(k, k) for k in NODE_ICONS]
    cur = current if current in NODE_ICONS else ("inherit" if inherit else "radio")
    out = "<span class='iconpick' role='radiogroup' aria-label='Map icon'>"
    for v, word in opts:
        svg = NODE_ICON_SVG.get(v) or ICONS["users"]
        out += f"<label data-tip='{e(word)}'><input type='radio' name='{e(field)}' value='{e(v)}'{' checked' if v == cur else ''} aria-label='{e(word)}'><span>{svg}</span></label>"
    return out + "</span>"


def icon_button(name, label, tip, more="", cls="line icon", attrs="", btn_type="button"):
    """Every icon control on every page goes through here, so a glyph cannot ship without its
    name, its tip and the word the labels switch reveals (5 Sep 2026 reviews)."""
    return (f"<button type='{btn_type}' class='{e(cls)}' aria-label='{e(label)}' data-tip='{e(tip)}'"
            + (f" data-tip-more='{e(more)}'" if more else "") + (" " + attrs if attrs else "")
            + f">{ICONS[name]}<span class='lbl'>{e(label)}</span></button>")


def seg(name, options, value, danger=(), attrs="", disabled=()):
    """A segmented control: radio inputs under the hood, so a form reads it like a select.
    options: (value, word) pairs; the chosen one is filled, a danger one fills red."""
    out = f"<span class='seg{' danger' if any(str(value) == str(d) for d in danger) else ''}' role='radiogroup' data-seg='{e(name)}' {attrs}>"
    for v, word in options:
        out += (f"<label><input type='radio' name='{e(name)}' value='{e(str(v))}'{' checked' if str(v) == str(value) else ''}{' disabled' if v in disabled else ''}"
                f"{' data-danger' if v in danger else ''}><span>{e(word)}</span></label>")
    return out + "</span>"


ASK_NOUN = {"traceroute": "a route", "request_position": "a position", "request_telemetry": "a battery", "request_nodeinfo": "its name"}
ASK_MORE = {"traceroute": "Asks for the hops out and back; a minute is normal", "request_position": "Asks the node to send its position now",
            "request_telemetry": "Asks for battery, voltage and uptime now", "request_nodeinfo": "Brings back a name changed over the air"}


def node_row(n, db=False, routes=None, silent_min=30):
    nid = str(n.get("id") or "")
    name = dname(n)
    own_name = str(n.get("name") or "") if n.get("label") and n.get("name") else ""
    has_fix = n.get("lat") is not None and n.get("lon") is not None
    pos = (f"{n['lat']:.5f}, {n['lon']:.5f} · {MG.mgrs(n['lat'], n['lon'], 4) or ''}".rstrip(" ·") if has_fix else "no fix")
    sub = " · ".join(x for x in (own_name, str(n.get("hw") or ""), pos) if x)
    if n.get("remote"):  # Spec 052: a node from a peer's picture
        sub = f"via {n.get('origin_name') or str(n.get('origin') or '')[:12]}" + (" · " + sub if sub else "")
    heard = n.get("heard") or n.get("last_heard_db")
    quiet = False
    try:
        quiet = bool(heard) and not db and (time.time() - _utc_secs(heard)) > int(silent_min) * 60
    except (TypeError, ValueError):
        quiet = False
    heard_html = (f"<time datetime='{e(str(heard))}' data-age>{e(age(heard))}</time>" + (f"<span class='verdict warn' data-tip='Quiet' data-tip-more='Nothing heard for longer than the silent threshold on Health'>quiet</span>" if quiet else "")) if heard else "<span class='sub'>never</span>"
    batt = n.get("battery")
    # one line for the figure, one small line for the voltage and the age together (0.2.10: short rows)
    ts = n.get("battery_ts")
    bits = ([f"{float(n['voltage']):g} V"] if n.get("voltage") else []) + ([f"<time datetime='{e(str(ts))}' data-age>{e(age(ts))}</time>"] if ts else [])
    under = f"<div class='sub'>{' · '.join(bits)}</div>" if bits else ""
    low = False
    if n.get("charging"):
        batt_html = f"<span class='ok'>on charge</span>{under}"
    elif batt is None:
        batt_html = "<span class='sub'>no reading</span>"
    elif float(batt) < 20:
        low = True
        batt_html = f"<span class='batt batt--low'>{int(batt)}%</span>{under}"
    else:
        batt_html = f"{int(batt)}%{under}"
    # the asks as icon buttons: a name, a tip and the noun the result line uses (5 Sep 2026 reviews)
    asks = "".join(icon_button(a["id"], a["title"], a["title"], ASK_MORE.get(a["id"], ""),
                               attrs=f"data-action='{e(a['id'])}' data-dest='{e(nid)}' data-label='{e(a['title'])}' data-ask='{e(ASK_NOUN.get(a['id'], 'it'))}'")
                   for a in C.ACTIONS if a["risk"] == "air" and len(a["inputs"]) == 1 and a["inputs"][0]["type"] == "node" and a["id"] in ICONS)
    search = " ".join(x for x in (name, nid, own_name, str(n.get("hw") or ""), str(n.get("holder") or ""), str(n.get("group") or ""), " ".join(n.get("tags") or [])) if x).lower()
    try:
        heard_secs = int(_utc_secs(heard)) if heard else 0
    except (TypeError, ValueError):
        heard_secs = 0
    attrs = (f"data-quiet='{1 if quiet else 0}' data-low='{1 if low else 0}' data-nofix='{0 if has_fix else 1}' data-search='{e(search)}' "
             f"data-heard='{heard_secs}' data-batt='{'' if batt is None else int(batt)}' data-group='{e(str(n.get('group') or ''))}'")
    glyph = f"<span class='nodeicon' data-tip='{e(str(n.get('icon') or 'radio'))}{(' · ' + e(str(n.get('group')))) if n.get('group') else ''}'>{NODE_ICON_SVG.get(str(n.get('icon') or 'radio'), NODE_ICON_SVG['radio'])}</span>"
    gtags = " ".join([f"<span class='pill'>{e(str(n.get('group')))}</span>"] if n.get("group") else []) + "".join(f" <span class='pill' style='opacity:.8'>{e(str(t))}</span>" for t in (n.get("tags") or [])[:4])
    return (f"<tr data-id='{e(nid)}' class='{'db' if db else ''}' {attrs}><td>{glyph}<b><a href='/node?id={e(nid)}' class='plain' data-tip='This node over time' data-tip-more='Battery, voltage, hours heard and messages'>{e(name)}</a></b>{(' ' + gtags) if gtags else ''}<div class='sub'>{e(nid)}{('<span class=hide-narrow> · ' + e(sub) + '</span>') if sub else ''}</div></td>"
            f"<td>{sig(n.get('snr'), n.get('hops'))}{('<div>' + spark(n.get('history')) + '</div>') if not db and spark(n.get('history')) else ''}</td><td>{batt_html}</td><td>{heard_html}</td>"
            f"<td><div class='row-actions'>{asks}"
            + ("" if db else node_name_fold(n))
            + f"</div><div class='res meta' role='status'></div>"
            f"<div class='route-slot'>{route_bar((routes or {}).get(nid)) if (routes or {}).get(nid) else ''}</div></td></tr>")


def node_name_fold(n):
    """The row's own control for what the box knows about the device: a name, a group, tags and a map
    icon (Spec 044), kept on the box, never written to the radio."""
    nid = str(n.get("id") or "")
    return (f"<details class='fold ctl icon'><summary data-tip='Name, group and icon' data-tip-more='Kept on the box, not the radio' aria-label='Name, group and icon'>{ICONS['name']}<span class='lbl'>Name</span></summary><form data-action='register_set' class='regform' data-refresh='' style='grid-template-columns:1fr 1fr'><input type='hidden' name='id' value='{e(nid)}'>"
            f"<input type='text' name='label' value='{e(str(n.get('label') or ''))}' maxlength='80' placeholder='display name' aria-label='display name'>"
            f"<input type='text' name='group' value='{e(str(n.get('group') or ''))}' maxlength='40' placeholder='group (e.g. Recce)' aria-label='group' list='groups'>"
            f"<input type='text' name='tags' value='{e(', '.join(n.get('tags') or []))}' maxlength='300' placeholder='tags, comma separated' aria-label='tags' style='grid-column:1/-1'>"
            f"<div style='grid-column:1/-1'><span class='meta'>Map icon</span>{icon_picker('icon', str(n.get('icon_own') or ''), inherit=True)}</div>"
            "<button type='submit' class='line'>Save</button><span class='meta' style='align-self:center'>kept on the box, not the radio</span><div class='res meta' role='status'></div></form></details>")


NODES_JS = r"""<script>
(function(){
  document.addEventListener('click',function(ev){var b=ev.target.closest('button[data-action][data-dest]');if(!b)return;
    var tr=b.closest('tr'),res=tr?tr.querySelector('.res'):null,nm=tr&&tr.querySelector('b')?tr.querySelector('b').textContent:b.dataset.dest;
    if(res){res.textContent='asking the box for '+(b.dataset.ask||'it');res.className='res meta warn';}
    fetch('/api/'+b.dataset.action,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({dest:b.dataset.dest})})
      .then(function(r){return r.json().then(function(j){return [r.status,j];});})
      .then(function(x){if(!res)return;if(x[0]>=400){res.textContent='not asked: '+(x[1].error||x[0]);res.className='res meta bad';}else{res.textContent='asked '+nm+' for '+(b.dataset.ask||'it')+' at '+window.mmNow()+(b.dataset.action==='traceroute'?' · no answer yet':'');res.className='res meta '+(b.dataset.action==='traceroute'?'warn':'ok');}})
      .catch(function(){if(res){res.textContent=window.mmNoAnswer;res.className='res meta bad';}});});
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
      if(window.mmNodeFilter){window.mmNodeFilter();}
    }).catch(function(){});};
  // the filter row: a search, three chips with counts, and an order (5 Sep 2026 reviews: at fifty radios nobody wants all of them)
  var chips={},q=document.getElementById('nf-q'),sortSel=document.getElementById('nf-sort'),groupSel=document.getElementById('group-filter');
  document.querySelectorAll('.chip[data-nf]').forEach(function(c){c.addEventListener('click',function(){var k=c.dataset.nf;chips[k]=!chips[k];c.classList.toggle('on',!!chips[k]);c.setAttribute('aria-pressed',chips[k]?'true':'false');window.mmNodeFilter();});});
  if(q){q.addEventListener('input',function(){window.mmNodeFilter();});}if(sortSel){sortSel.addEventListener('change',function(){window.mmNodeFilter();});}if(groupSel){groupSel.addEventListener('change',function(){window.mmNodeFilter();});}
  window.mmNodeFilter=function(){var tb=document.getElementById('nodes');if(!tb)return;var rows=[].slice.call(tb.querySelectorAll('tr[data-id]'));var text=(q&&q.value||'').trim().toLowerCase();var g=groupSel?groupSel.value:'';
    var counts={quiet:0,low:0,nofix:0};
    rows.forEach(function(tr){['quiet','low','nofix'].forEach(function(k){if(tr.dataset[k]==='1')counts[k]++;});
      var ok=(!text||(tr.dataset.search||'').indexOf(text)>=0)&&(!g||tr.dataset.group===g)&&(!chips.quiet||tr.dataset.quiet==='1')&&(!chips.low||tr.dataset.low==='1')&&(!chips.nofix||tr.dataset.nofix==='1');tr.hidden=!ok;});
    document.querySelectorAll('.chip[data-nf] b').forEach(function(b){var k=b.parentNode.dataset.nf;b.textContent=counts[k];});
    var mode=sortSel?sortSel.value:'';if(mode){rows.sort(function(a,b){if(mode==='quiet'){var qa=(a.dataset.quiet==='1')?0:1,qb=(b.dataset.quiet==='1')?0:1;if(qa!==qb)return qa-qb;return parseInt(a.dataset.heard||'0',10)-parseInt(b.dataset.heard||'0',10);}
      var ba=a.dataset.batt===''?999:parseInt(a.dataset.batt,10),bb=b.dataset.batt===''?999:parseInt(b.dataset.batt,10);return ba-bb;});rows.forEach(function(r){tb.appendChild(r);});}
    var none=document.getElementById('nf-none');if(none){none.hidden=!(rows.length&&rows.every(function(r){return r.hidden;}));}};
  window.mmNodeFilter();
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


def nodes_tables(nodes, routes=None, silent_min=30):
    heard = [n for n in nodes if n.get("heard_here", True)]
    db = [n for n in nodes if not n.get("heard_here", True)]
    rows = "".join(node_row(n, routes=routes, silent_min=silent_min) for n in heard) or "<tr><td colspan=5 class='meta'>No node heard since this bridge started. A quiet mesh is not a broken bridge: wait for a tracker to speak, or plug one into this box and set it up on the <a href='/bench'>Bench</a>.</td></tr>"
    db_rows = "".join(node_row(n, db=True, silent_min=silent_min) for n in db)
    return rows, db_rows, len(heard), len(db)


def nodes_body(nodes, intro=True, routes=None, silent_min=30, groups=None):
    rows, db_rows, heard, db = nodes_tables(nodes, routes, silent_min)
    live = [n for n in nodes if n.get("heard_here", True)]
    head = "<thead><tr><th>Node</th><th>Signal</th><th>Battery</th><th>Heard</th><th>Ask</th></tr></thead>"
    lead = (f"<p class='meta'><span id='nodes-heard-count'>{heard}</span> heard here since the bridge started, "
            f"<span id='nodes-db-count'>{db}</span> more in the radio's database. Joined on radio id; names are labels, never identity.</p>") if intro else ""
    def cnt(k):
        if k == "quiet":
            return sum(1 for n in live if n.get("heard") and (time.time() - _utc_secs(n["heard"])) > int(silent_min) * 60)
        if k == "low":
            return sum(1 for n in live if n.get("battery") is not None and not n.get("charging") and float(n["battery"]) < 20)
        return sum(1 for n in live if n.get("lat") is None or n.get("lon") is None)
    gsel = ""
    if groups:
        gsel = "<label class='meta'>Group <select id='group-filter'><option value=''>every group</option>" + "".join(f"<option value='{e(g)}'>{e(g)}</option>" for g in groups) + "</select></label>"
    filters = ("<div class='filters' id='node-filters'><input type='search' id='nf-q' placeholder='Find a node' aria-label='Find a node by name, id or hardware'>"
               f"<button type='button' class='chip' data-nf='quiet' aria-pressed='false' data-tip='Only the quiet ones' data-tip-more='Nothing heard for longer than the silent threshold on Health'>quiet <b>{cnt('quiet')}</b></button>"
               f"<button type='button' class='chip' data-nf='low' aria-pressed='false' data-tip='Only low batteries' data-tip-more='Under 20 per cent'>battery low <b>{cnt('low')}</b></button>"
               f"<button type='button' class='chip' data-nf='nofix' aria-pressed='false' data-tip='Only nodes without a fix'>no fix <b>{cnt('nofix')}</b></button>"
               f"{gsel}<label class='meta'>Show <select id='nf-sort'><option value=''>as heard</option><option value='quiet'>quiet first</option><option value='low'>low battery first</option></select></label></div>")
    fold = (f"<details class='fold'><summary><span id='nodes-db-count'>{db}</span>&nbsp;in the radio's database only, not heard since this bridge started <span class='pill'>database only</span></summary>"
            f"<div class='tablewrap'><table>{head}<tbody id='nodes-db'>{db_rows}</tbody></table></div></details>") if db or not intro else ""
    js = NODES_JS if intro else NODES_JS + "<script>window.onMesh=window.onMesh||function(d){if(d.kind==='packet'||d.kind==='forwarded'||d.kind==='status'){window.mmNodes();}};</script>"
    dl = "<datalist id='groups'>" + "".join(f"<option value='{e(g)}'>" for g in (groups or [])) + "</datalist>"
    return f"{lead}{filters}{dl}<div class='tablewrap'><table>{head}<tbody id='nodes'>{rows}</tbody></table></div><p class='meta' id='nf-none' hidden>No node matches that filter.</p>{fold}{js}"


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
    if(was){p.scrollTop=p.scrollHeight;}else{pending++;nb.textContent=pending+' new line'+(pending===1?'':'s')+' · go to the end';nb.style.display='inline-block';}};
  nb.addEventListener('click',function(){p.scrollTop=p.scrollHeight;pending=0;nb.style.display='none';});
  p.addEventListener('scroll',function(){if(atBottom()){pending=0;nb.style.display='none';}});
  document.querySelector('[data-log-filter]').addEventListener('change',function(ev){p.dataset.show=ev.target.value;});
  document.querySelector('[data-log-level]').addEventListener('change',function(ev){p.dataset.level=ev.target.value;});
  p.scrollTop=p.scrollHeight;})();
</script>"""
    body = "".join(log_line_html(x) for x in lines)
    return (f"<p class='meta'>The bridge's last {len(lines)} lines; new ones arrive as they happen. The radio's own chatter, roughly every minute, is normal; silence is what the watchdog watches.</p>"
            "<div class='controls'><span data-tip='Show' data-tip-more='The bridge&#39;s own lines, or the radio&#39;s chatter as well'><span class='meta'>Show</span> " + seg("logshow", (("bridge", "The bridge"), ("all", "Both"), ("radio", "The radio only")), "bridge", attrs="data-log-filter") + "</span>"
            "<span><span class='meta'>Level</span> " + seg("loglevel", (("all", "Everything"), ("warn", "Warnings and errors")), "all", attrs="data-log-level") + "</span></div>"
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
  window.mmNoAnswer='no answer from the box: check you are still on its Wi-Fi, then try again';
  // one in-page confirm instead of the browser's: readable in sunlight, styled, focus on No, Escape cancels (5 Sep 2026 reviews)
  window.mmConfirm=function(text,host,onYes){if(!text){onYes();return;}var slot=host||document.body;var old=slot.querySelector(':scope > .confirm');if(old){old.remove();}
    var p=document.createElement('div');p.className='confirm';p.setAttribute('role','alertdialog');var s=document.createElement('div');s.textContent=text;p.appendChild(s);
    var row=document.createElement('div');row.className='row-actions';var yes=document.createElement('button');yes.type='button';yes.textContent='Yes, do it';var no=document.createElement('button');no.type='button';no.className='quiet';no.textContent='No';
    row.appendChild(yes);row.appendChild(no);p.appendChild(row);
    function done(){p.remove();document.removeEventListener('keydown',esc,true);}function esc(ev){if(ev.key==='Escape'){ev.preventDefault();done();}}
    yes.addEventListener('click',function(){done();onYes();});no.addEventListener('click',done);document.addEventListener('keydown',esc,true);slot.appendChild(p);no.focus();};
  window.mmTick=function(f){var tick=f.querySelector('input[name=confirm_tick]');if(!tick||tick.checked)return true;
    var rs=f.querySelector('.res');if(rs){rs.textContent='Tick the box that says you understand, then press again.';rs.className='res meta bad';}var lab=tick.closest('label');if(lab){lab.style.outline='2px solid var(--bad)';lab.style.outlineOffset='2px';}tick.focus();return false;};
  function post(url,body,el){
    return fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})
      .then(function(r){return r.json().then(function(j){return [r.status,j];});})
      .then(function(x){show(el,x[0],x[1]);return x;})
      .catch(function(){if(el){el.textContent=window.mmNoAnswer;el.className='res meta bad';}return [0,{}];});
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
        .catch(function(){if(btn){btn.disabled=false;}if(out){out.textContent=window.mmNoAnswer;out.className='out meta bad';}});})();},true);
  document.addEventListener('submit',function(ev){var f=ev.target.closest('form[data-action]:not([data-method=get]):not([id=send])');if(!f)return;
    (function(){ev.preventDefault();
      var action=f.dataset.action,body={},unreachable=f.dataset.risk==='unreachable';
      new FormData(f).forEach(function(v,k){if(v!==''&&k!=='confirm_tick'){var el=f.elements[k];body[k]=(el&&el.type==='number')?parseInt(v,10):v;}});
      if(f.dataset.action==='node_channel_push'&&parseInt(f.elements.index.value,10)>0){unreachable=false;}
      if(unreachable){if(!window.mmTick(f))return;var card=f.closest('.card');body.confirm=f.dataset.target||(card&&card.dataset.own)||own;}
      var text=fill(f.dataset.confirm);
      if(f.dataset.action==='bench_flash'){var sel=f.querySelector('select[data-pins]');var o=sel&&sel.options[sel.selectedIndex];if(o&&o.dataset.note){text=(text?text+' ':'')+'Note on this image: '+o.dataset.note;}var st=f.querySelector('.stages');if(st){st.textContent='';}}
      window.mmConfirm(text,f,function(){
        var url=f.dataset.proposal?'/api/proposal/run':('/api/'+action);
        if(f.dataset.proposal){body={id:f.dataset.proposal,arguments:body};}
        var btn=f.querySelector('button[type=submit]');if(btn){btn.disabled=true;}
        var rs=f.querySelector('.res');if(rs){rs.textContent=(f.dataset.risk?'sent at '+window.mmNow()+', waiting for the read-back':'sending');rs.className='res meta warn';}
        post(url,body,rs).then(function(x){if(btn){btn.disabled=false;}
          if(x[0]&&x[0]<400){document.dispatchEvent(new CustomEvent('mm-written',{detail:{action:action,result:x[1]}}));}
          if(action==='bench_onboard'&&x[0]&&x[0]<400&&x[1].confirmed&&rs){var rb=x[1].read_back||{};var ch=(rb.channels&&rb.channels[0]&&rb.channels[0].name)||rb.channel0||'';
            rs.textContent=(rb.long_name||body.long_name||'The device')+' is on the mesh and managed by this radio. Read back at '+window.mmNow()+': role '+(rb.role||body.role||'?')+(ch?', channel '+ch:'')+(rb.region?', '+rb.region+' '+(rb.modem_preset||''):'')+'. ';
            var a=document.createElement('a');a.href='/register';a.textContent='See it on the register';rs.appendChild(a);}
          if(x[0]&&x[0]<400){if(f.dataset.proposal){f.classList.add('done');f.querySelectorAll('button').forEach(function(b){b.disabled=true;});}
            if(f.dataset.refresh){var p=f.dataset.refresh.split(':');window.mmFrag(p[0],p[1]);}
            if(f.dataset.clear){f.reset();}}});});
    })();
  });
  document.addEventListener('click',function(ev){var b=ev.target.closest('button[data-action][data-index]');if(!b)return;
    (function(){
      var action=b.dataset.action,idx=parseInt(b.dataset.index,10),body={index:idx};
      var text=fill(b.dataset.confirm);
      if(b.dataset.risk==='unreachable'&&idx===0){body.confirm=own;}
      var host=b.closest('td')||b.parentNode.parentNode;var res=host.querySelector('.res');
      window.mmConfirm(text,host,function(){post('/api/'+action,body,res).then(function(x){if(x[0]&&x[0]<400&&b.dataset.refresh){var p=b.dataset.refresh.split(':');window.mmFrag(p[0],p[1]);}});});
    })();
  });
  document.addEventListener('click',function(ev){var b=ev.target.closest('button[data-dismiss]');if(!b)return;
    (function(){var f=b.closest('form');
      window.mmConfirm('Dismiss this proposal? Its rationale stays in the audit.',f,function(){
      post('/api/proposal/dismiss',{id:b.dataset.dismiss},f?f.querySelector('.res'):null).then(function(x){if(x[0]&&x[0]<400&&f){f.classList.add('done');f.querySelectorAll('button').forEach(function(x){x.disabled=true;});}});});})();
  });
  var dec=document.getElementById('decode');
  if(dec){dec.addEventListener('click',function(){var url=document.querySelector('form[data-action=channel_adopt] input[name=url]').value;
    fetch('/api/channel_decode?url='+encodeURIComponent(url)).then(function(r){return r.json();}).then(function(j){
      var out=document.getElementById('decoded');var btn=document.querySelector('form[data-action=channel_adopt] button[type=submit]');
      if(j.error){out.textContent='cannot read it: '+j.error;out.className='meta bad';btn.disabled=true;return;}
      out.textContent='This URL carries '+j.count+' channel(s): '+j.channels.map(function(n){return n||'(unnamed)';}).join(', ')+' · region '+(j.region||'not set')+' · preset '+(j.modem_preset||'not set')+'. Read that before you adopt it.';
      out.className='meta ok';btn.disabled=false;});});}
  // the join QR sheet is a dialog: focus moves to Close, Escape closes, focus returns, and it closes itself after a minute
  var sheet=document.getElementById('qr-sheet'),qrTimer=null,qrOpener=null;
  function qrClose(){if(!sheet)return;sheet.hidden=true;if(qrTimer){clearInterval(qrTimer);qrTimer=null;}if(qrOpener){qrOpener.focus();}}
  document.querySelectorAll('[data-qr-open]').forEach(function(b){b.addEventListener('click',function(){if(!sheet)return;qrOpener=b;var img=sheet.querySelector('img');
    if(img&&b.dataset.qrIndex!==undefined){img.src='/channels/qr.png?index='+encodeURIComponent(b.dataset.qrIndex)+'&t='+Date.now();img.alt='Join QR for '+(b.dataset.qrName||'this channel');}
    var nm=sheet.querySelector('[data-qr-name]');if(nm&&b.dataset.qrName){nm.textContent=b.dataset.qrName;}
    sheet.hidden=false;var left=60,cd=sheet.querySelector('[data-qr-count]');if(cd){cd.textContent='closes itself in '+left+' s';}
    qrTimer=setInterval(function(){left--;if(cd){cd.textContent='closes itself in '+left+' s';}if(left<=0){qrClose();}},1000);
    var c=sheet.querySelector('[data-qr-close]');if(c){c.focus();}});});
  document.querySelectorAll('[data-qr-close]').forEach(function(b){b.addEventListener('click',qrClose);});
  document.addEventListener('keydown',function(ev){if(ev.key==='Escape'&&sheet&&!sheet.hidden){qrClose();}});
  // Read again without a reload (Spec 007): fetch the page, swap the parts that came from the radio
  document.addEventListener('click',function(ev){var b=ev.target.closest('[data-read-again]');if(!b)return;b.disabled=true;
    var stamp=document.querySelector('[data-read-stamp]');if(stamp){stamp.textContent='reading the radio…';}
    fetch(window.location.pathname).then(function(r){return r.text();}).then(function(h){var d=new DOMParser().parseFromString(h,'text/html');
      ['channel-rows','radio-cards','read-line'].forEach(function(id){var n=d.getElementById(id),o=document.getElementById(id);if(n&&o){o.innerHTML=n.innerHTML;}});})
      .catch(function(){var st=document.querySelector('[data-read-stamp]');if(st){st.textContent=window.mmNoAnswer;}var bb=document.querySelector('[data-read-again]');if(bb){bb.disabled=false;}});});
  document.querySelectorAll('[data-copy]').forEach(function(b){b.addEventListener('click',function(){var t=b.dataset.copy;
    (navigator.clipboard?navigator.clipboard.writeText(t):Promise.reject()).then(function(){b.textContent='Copied';},function(){window.prompt('Copy the token:',t);});});});
})();
</script>"""


def _act(aid):
    return C.by_id(aid) or {"confirm": "", "risk": "read", "description": "", "inputs": [], "title": aid}


def read_line(res, page_path):
    at = hhmm(res.get("read_at")) if res.get("read_at") else hhmm()
    return (f"<p class='meta' id='read-line'><span data-read-stamp>read from the radio at {e(at)}</span> "
            + icon_button("refresh", "Read the radio again", "Read the radio again", "Asks the radio and refreshes this page in place") + "</p>")


def channel_rows(ch):
    chans = ch.get("channels", [])
    live = [c for c in chans if c.get("role") != "DISABLED"]
    rot, dele = _act("channel_rotate"), _act("channel_delete")
    rows = ""
    for c in live:
        i = int(c.get("index", 0))
        nm = c.get("name") or f"slot {i}"
        ctl = (f"<button class='line' data-action='channel_rotate' data-index='{i}' data-risk='{'unreachable' if i == 0 else 'change'}' data-refresh='channels:channel-rows' "
               f"data-confirm=\"{e(rot['confirm'] if i == 0 else 'A fresh key on ' + nm + '. Devices on it need the new QR.')}\">Rotate key</button>")
        if i >= 1:
            ctl += f" <button class='danger' data-action='channel_delete' data-index='{i}' data-refresh='channels:channel-rows' data-confirm=\"{e(dele['confirm'])}\">Delete</button>"
        if c.get("has_key"):
            ctl += icon_button("qr", f"Join QR for {nm}", "Join QR for this channel", "Shows the key as a QR; only to a device you mean to join", attrs=f"data-qr-open data-qr-index='{i}' data-qr-name='{e(nm)}'")
        push = (f"<details class='fold ctl' style='margin-top:var(--s2)'><summary>Push to the fleet</summary><form data-fleet-push='{i}' data-fleet-name='{e(nm)}' style='margin-top:var(--s2)'>"
                "<p class='meta'>This slot's name, key and role are copied to the same slot on every managed device, one after another over the air, each read back.</p>"
                + (f"<label class='check'><input type='checkbox' name='confirm_tick'><span>Slot 0 replaces each device's primary channel; a device whose key then differs from this radio's will not hear this mesh.</span></label>" if i == 0 else "")
                + f"<button type='submit' class='line'>Push slot {i} to every managed device</button><div class='res meta' role='status'></div><div class='fleet-out meta'></div></form></details>")
        rows += (f"<tr><td>{i}</td><td>{e(nm)}</td><td><span class='pill'>{e(c.get('role') or '')}</span></td>"
                 f"<td>{'set' if c.get('has_key') else 'no key'}</td><td><div class='row-actions'>{ctl}</div><div class='res meta' role='status'></div>{push}</td></tr>")
    free = 8 - len(live)
    rows += f"<tr><td colspan=5 class='meta'>{free} free slot{'s' if free != 1 else ''} of 8.</td></tr>"
    return rows


FLEET_PUSH_JS = r"""<script>
(function(){
  document.addEventListener('submit',function(ev){var f=ev.target.closest('form[data-fleet-push]');if(!f)return;ev.preventDefault();ev.stopImmediatePropagation();
    var idx=parseInt(f.dataset.fleetPush,10),name=f.dataset.fleetName||('slot '+idx),res=f.querySelector('.res'),out=f.querySelector('.fleet-out');
    if(idx===0&&!window.mmTick(f))return;
    fetch('/api/register').then(function(r){return r.json();}).then(function(j){
      var devs=(j.rows||[]).filter(function(r){return r.managed;});
      if(!devs.length){res.textContent='no managed device to push to: the bench is where a device becomes managed';res.className='res meta warn';return;}
      window.mmConfirm('Push slot '+idx+' ('+name+') to '+devs.length+' managed device'+(devs.length===1?'':'s')+' over the air, one after another; each is read back before it counts.',f,function(){
        out.textContent='';res.textContent='pushing to '+devs.length+'…';res.className='res meta warn';var ok=0,i=0;
        (function next(){if(i>=devs.length){res.textContent=ok+' of '+devs.length+' read back at '+window.mmNow();res.className='res meta '+(ok===devs.length?'ok':'warn');return;}
          var d=devs[i++],body={id:d.id,index:idx};if(idx===0){body.confirm=d.id;}
          fetch('/api/node_channel_push',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}).then(function(r){return r.json().then(function(x){return [r.status,x];});})
            .then(function(x){var nm=d.label||d.name||d.id;if(x[0]<400&&x[1].confirmed){ok++;out.textContent+=nm+': written and read back\n';}else{out.textContent+=nm+': '+(x[1].error||x[1].unconfirmed||'not confirmed')+'\n';}next();})
            .catch(function(){out.textContent+=(d.label||d.name||d.id)+': '+window.mmNoAnswer+'\n';next();});})();});
    }).catch(function(){res.textContent=window.mmNoAnswer;res.className='res meta bad';});},true);
  var seg=document.querySelector("form[data-action=channel_adopt] [data-seg=mode]");
  if(seg){var f=seg.closest('form');function mode(){var v=(f.querySelector('input[name=mode]:checked')||{}).value||'add';var rep=v==='replace';f.classList.toggle('danger',rep);f.dataset.risk=rep?'unreachable':'change';var t=f.querySelector('label.check');if(t){t.hidden=!rep;}}
    seg.addEventListener('change',mode);mode();}
})();
</script>"""


def channels_body(ch, own_id="?", st=None, rotation=None):
    st = st or {}
    primary = next((c.get("name") for c in ch.get("channels", []) if c.get("role") == "PRIMARY"), None) or st.get("primary_channel") or "the primary channel"
    err = f"<p class='warn'>{e(ch['error'])}</p>" if ch.get("error") else ""
    qr = ("<h2>Join a channel</h2><p class='meta'>The join QR carries the channel name, the key, the region and the modem preset; the key appears nowhere else on this screen. "
          "Show it only to a device you mean to join. Each channel row has its own QR.</p><button type='button' data-qr-open data-qr-index='0' data-qr-name='" + e(primary) + "'>Show the join QR for " + e(primary) + "</button>"
          f"<div class='sheet' id='qr-sheet' role='dialog' aria-modal='true' aria-label='Join QR' hidden><img src='/channels/qr.png' alt='Join QR for {e(primary)}' width='320' height='320'>"
          f"<div><b data-qr-name>{e(primary)}</b> · {e(st.get('region') or '?')} · {e(st.get('modem_preset') or '?')}</div><p class='meta'>Scan it in the Meshtastic app, or hold it up to a tracker's onboarding.</p>"
          "<span class='meta' data-qr-count></span>" + icon_button("close", "Close the join QR", "Close the join QR", cls="line icon close", attrs="data-qr-close") + "</div>"
          if ch.get("url") else "<p class='meta'>No primary channel with a key is readable yet.</p>")
    cre, ado = _act("channel_create"), _act("channel_adopt")
    create = (f"<form class='card' data-action='channel_create' data-risk='change' data-refresh='channels:channel-rows' data-clear='1' data-confirm=\"{e(cre['confirm'])}\">"
              f"<h2 style='margin-top:0'>{e(cre['title'])}</h2><p class='meta'>{e(cre['description'])}</p>"
              "<label>Name (11 bytes at most)<input type='text' name='name' maxlength='11' required></label>"
              "<label>Slot<select name='index'><option value=''>first free</option>" + "".join(f"<option value='{i}'>{i}</option>" for i in range(1, 8)) + "</select></label>"
              "<button type='submit'>Create the channel</button><div class='res meta' role='status'></div></form>")
    adopt = (f"<form class='card' data-action='channel_adopt' data-risk='change' data-refresh='channels:channel-rows' data-confirm=\"{e(ado['confirm'])}\">"
             f"<h2 style='margin-top:0'>{e(ado['title'])}</h2><p class='meta'>{e(ado['description'])}</p>"
             "<label>Join URL<input type='text' name='url' required autocomplete='off'></label>"
             "<button type='button' class='line' id='decode'>Read it first</button><div id='decoded' class='meta' style='margin:.4rem 0'></div>"
             "<div><span class='meta'>Mode</span><br>" + seg("mode", (("add", "Add to free slots"), ("replace", "Replace everything")), "add", danger=("replace",)) + "</div>"
             f"<label class='check' hidden><input type='checkbox' name='confirm_tick'><span>I understand a replace moves this radio to the URL's channels and region; devices on the old ones will not hear it. This radio is {e(own_id)}.</span></label>"
             "<button type='submit' disabled>Adopt these channels</button><div class='res meta' role='status'></div></form>")
    rot_open = bool((rotation or {}).get("rotation"))
    rot_block = f"<div id='rotation-body'>{rotation_section(rotation)}</div>"
    return (f"{err}{read_line(ch, '/channels')}<div class='tablewrap'><table><thead><tr><th>Slot</th><th>Name</th><th>Role</th><th>Key</th><th></th></tr></thead><tbody id='channel-rows'>{channel_rows(ch)}</tbody></table></div>"
            f"<p class='meta'>Every write is shown only once the radio has answered with it.</p>"
            + (rot_block if rot_open else "") + qr
            + f"<div class='cards' style='margin-top:1rem'>{create}{adopt}</div>" + ("" if rot_open else rot_block) + f"{ROTATION_JS}{FLEET_PUSH_JS}{WRITE_JS}")


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
            "<label>Long name (39 bytes at most)<input type='text' name='long_name' maxlength='39'></label><label>Short name (4 bytes at most)<input type='text' name='short_name' maxlength='4'></label>"
            "<label>Transmit power (dBm)<input type='number' name='tx_power' min='0' max='30'></label><label>Position every (seconds)<input type='number' name='position_broadcast_secs' min='32' max='86400'></label>"
            "<button type='submit'>Write over the air</button><div class='res meta' role='status'></div></form>")
    regf = (f"<form class='danger' data-action='node_set_region' data-risk='unreachable' data-target='{e(nid)}' data-confirm=\"{e(nr.get('confirm') or '')}\"><input type='hidden' name='id' value='{e(nid)}'>"
            f"<h2 style='margin-top:0;font-size:.95rem;color:var(--bad)'>{e(nr.get('title') or '')}</h2>"
            f"<label>Region{sel('region', ins.get('region', {}).get('values', []))}</label><label>Modem preset{sel('modem_preset', ins.get('modem_preset', {}).get('values', []))}</label><label>Role{sel('role', ins.get('role', {}).get('values', []))}</label>"
            f"<label class='check'><input type='checkbox' name='confirm_tick'><span>I understand this device may be unreachable over the air afterwards and reboots. This device is {e(nid)}.</span></label>"
            "<button type='submit' class='danger'>Write and reboot the device</button><div class='res meta' role='status'></div></form>")
    push = (f"<form data-action='node_channel_push' data-risk='unreachable' data-target='{e(nid)}' data-confirm=\"{e(npush.get('confirm') or '')}\"><input type='hidden' name='id' value='{e(nid)}'>"
            f"<h2 style='margin-top:0;font-size:.95rem'>{e(npush.get('title') or '')}</h2>"
            "<label>Slot<select name='index'>" + "".join(f"<option value='{i}'{' selected' if i == 1 else ''}>{i}{' (primary)' if i == 0 else ''}</option>" for i in range(8)) + "</select></label>"
            f"<label class='check'><input type='checkbox' name='confirm_tick'><span>For slot 0: I understand the device's primary channel is replaced. This device is {e(nid)}.</span></label>"
            "<button type='submit'>Push the channel</button><div class='res meta' role='status'></div></form>")
    reboot = (f"<form data-action='node_reboot' data-risk='unreachable' data-target='{e(nid)}' data-confirm=\"{e(nrb.get('confirm') or '')}\"><input type='hidden' name='id' value='{e(nid)}'>"
              f"<label class='check'><input type='checkbox' name='confirm_tick'><span>Reboot in ten seconds; off the mesh while it does. This device is {e(nid)}.</span></label>"
              "<button type='submit' class='line'>Reboot over the air</button><div class='res meta' role='status'></div></form>")
    return (f"<details class='fold ctl'><summary data-tip='Over the air' data-tip-more='Read and write this device through the mesh under this radio&#39;s admin key'>{ICONS['sliders']}Over the air</summary><div class='manage'><p class='meta' style='margin:0'>Every write is shown only once the device has answered with it.</p>{read}{setf}{regf}{push}{reboot}</div></details>")


def _avcell(a):
    """Spec 036: the heard-percentage cell, with the histogram in the tooltip."""
    if not a:
        return "<td class='meta'>·</td>"
    ser = a.get("series") or []
    blocks = "".join("▮" if v else "▯" for v in ser)
    pct = int(a.get("pct") or 0)
    tone = "ok" if pct >= 80 else ("warn" if pct >= 40 else "bad")
    return (f"<td data-tip='heard in {a.get('heard')} of {a.get('buckets')} {'hours' if a.get('bucket_secs') == 3600 else 'days'}' "
            f"data-tip-more='{e(blocks)}' style='font-variant-numeric:tabular-nums;color:var(--{tone})'>{pct}%</td>")


def _fwcell(r, iv):
    """Spec 043: the firmware a device itself reported, and whether the shelf holds something newer for it."""
    iv = iv or {}
    fw = r.get("firmware") or iv.get("firmware")
    if not fw:
        return f"<td class='hide-narrow'><span class='sub' data-tip='Firmware unknown' data-tip-more='Read the device on the bench or over the air; nothing is inferred from the hardware'>firmware unknown</span></td>"
    behind = iv.get("behind")
    pill = (f"<div><span class='pill behind' style='background:var(--warn);color:#fff;border-color:var(--warn)' data-tip='Behind the shelf' data-tip-more='{e(str(iv.get('behind_reason') or ''))}'>behind</span></div>" if behind
            else (f"<div class='sub'>{e(str(iv.get('behind_reason') or ''))}</div>" if iv.get("behind_reason") else ""))
    return f"<td class='hide-narrow'>{e(str(fw))}{pill}</td>"


def _keycell(nid, iv):
    """Spec 043: the key's fingerprint and since when; a change the operator has not accepted is an alarm."""
    iv = iv or {}
    fp = iv.get("fingerprint")
    if not fp:
        return "<td class='hide-narrow'><span class='sub'>no key seen</span></td>"
    since = f"<div class='sub'>since <time datetime='{e(str(iv.get('key_since') or ''))}' data-age>{e(age(iv.get('key_since') or ''))}</time></div>" if iv.get("key_since") else ""
    alarm = ""
    if iv.get("key_alarm"):
        ka = _act("key_accept")
        alarm = (f"<div><span class='pill key-changed' style='background:var(--bad);color:#fff;border-color:var(--bad)' data-tip='Key changed' data-tip-more='A changed key is a reflashed radio or an impostor; accept it only when you know which'>changed {e(str(iv.get('key_changed') or '')[:10])}</span> "
                 f"<form data-action='key_accept' data-risk='change' data-confirm=\"{e(ka.get('confirm') or '')}\" data-refresh='register:register-rows' style='display:inline'><input type='hidden' name='id' value='{e(nid)}'><button type='submit' class='line'>Accept the new key</button><div class='res meta' role='status'></div></form></div>")
    elif iv.get("key_changed"):
        alarm = f"<div class='sub'>changed {e(str(iv.get('key_changed') or '')[:10])}, accepted</div>"
    return f"<td class='hide-narrow'><code data-tip='Key fingerprint' data-tip-more='Twelve hex of the sha256 of the device&#39;s public key; the same radio keeps the same fingerprint'>{e(fp)}</code>{since}{alarm}</td>"


def register_rows(reg, availability=None, inv=None):
    availability = availability or {}
    inv = {r.get("id"): r for r in ((inv or {}).get("rows") or [])}
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
                  f"<input type='hidden' name='id' value='{e(nid)}'><div><span class='meta'>Its label and holder</span><br>" + seg("register", (("keep", "Keep the label"), ("drop", "Drop it")), "keep") + "</div>"
                  "<button type='submit' class='danger'>Forget this node</button><div class='res meta' role='status'></div></form></details>")
        rows += (f"<tr data-id='{e(nid)}'><td><b>{e(dname(r))}</b><div class='sub'>{e(nid)}{(' · ' + e(str(r.get('name') or ''))) if r.get('label') and r.get('name') else ''}</div>{forget}</td><td>{form}</td>"
                 f"<td class='hide-narrow'>{e(str(r.get('hw') or ''))}<div class='sub'>{e(str(r.get('role') or ''))}</div></td>{_fwcell(r, inv.get(nid))}{_keycell(nid, inv.get(nid))}"
                 f"<td>{managed}</td><td>{heard_html}</td>{_avcell(availability.get(str(r.get('id') or ''))).replace('<td ', '<td class=hide-narrow ', 1)}</tr>")
    return rows or "<tr><td colspan=8 class='meta'>No device yet. Plug one into the box by USB, then onboard it on the <a href='/bench'>Bench</a> page; it appears here.</td></tr>"


def groups_section(gs):
    """Spec 044: the groups on this box, each with its icon and its count; create, change an icon, remove."""
    gs = gs or {}
    gset, gdel = _act("group_set"), _act("group_delete")
    rows = ""
    for g in gs.get("groups") or []:
        nm = str(g.get("name") or "")
        rows += (f"<tr data-group='{e(nm)}'><td><span class='nodeicon'>{NODE_ICON_SVG.get(str(g.get('icon') or 'radio'), NODE_ICON_SVG['radio'])}</span><b>{e(nm)}</b></td><td>{int(g.get('count') or 0)} device{'s' if int(g.get('count') or 0) != 1 else ''}</td>"
                 f"<td><form data-action='group_set' data-risk='change' data-confirm=\"{e(gset.get('confirm') or '')}\" data-refresh='groups:groups-body' class='regform' style='grid-template-columns:1fr auto;min-width:320px'><input type='hidden' name='name' value='{e(nm)}'>{icon_picker('icon', str(g.get('icon') or 'radio'), inherit=False)}<button type='submit' class='line'>Set the icon</button><div class='res meta' role='status'></div></form></td>"
                 f"<td><form data-action='group_delete' data-risk='change' data-confirm=\"{e(gdel.get('confirm') or '')}\" data-refresh='groups:groups-body'><input type='hidden' name='name' value='{e(nm)}'><button type='submit' class='danger'>Remove the group</button><div class='res meta' role='status'></div></form></td></tr>")
    create = (f"<form data-action='group_set' class='card' data-risk='change' data-clear='1' data-confirm=\"{e(gset.get('confirm') or '')}\" data-refresh='groups:groups-body' style='margin-top:var(--s3)'><h2 style='margin-top:0'>Create a group</h2>"
              "<label>Name<input type='text' name='name' maxlength='40' required placeholder='e.g. Recce'></label><span class='meta'>Map icon</span>" + icon_picker("icon", "radio", inherit=False)
              + "<button type='submit' class='line'>Create the group</button><div class='res meta' role='status'></div></form>")
    return (f"<details class='fold' data-keep='groups'><summary>Groups</summary><p class='meta'>A group is a word you give devices (a section, a vehicle, the routers). Its icon is what its devices carry on the map unless one has its own; the map, the lists, the alerts and the exports filter by group. Kept on the box; nothing is written to any radio.</p>"
            f"<div class='tablewrap'><table><thead><tr><th>Group</th><th>Devices</th><th>Icon</th><th></th></tr></thead><tbody>{rows or '<tr><td colspan=4 class=meta>No group yet. Give a device a group on the Nodes page, or create one below.</td></tr>'}</tbody></table></div>{create}</details>")


def register_body(reg, drift=None, availability=None, inv=None, groups=None):
    availability = availability or {}
    inv = inv or {}
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
            + (f"<p class='meta'><b>{int(inv.get('behind') or 0)} behind the shelf</b>, <b class='{'bad' if inv.get('key_alarms') else ''}'>{int(inv.get('key_alarms') or 0)} changed key{'s' if int(inv.get('key_alarms') or 0) != 1 else ''}</b> to accept. <a href='/export/inventory.csv'>Export the inventory (CSV)</a>.</p>" if inv else "")
            + "<div class='tablewrap'><table><thead><tr><th>Device</th><th>Label · holder</th><th class='hide-narrow'>Hardware</th><th class='hide-narrow'>Firmware</th><th class='hide-narrow'>Key</th><th>Managed</th><th>Heard</th><th class='hide-narrow' data-tip='Heard %' data-tip-more='How much of the last 24 hours the node was heard for'>Heard %</th></tr></thead>"
            f"<tbody id='register-rows'>{register_rows(reg, availability, inv)}</tbody></table></div>{stale_form()}<div id='groups-body' style='margin-top:var(--s4)'>{groups_section(groups)}</div><div id='drift-body' style='margin-top:var(--s4)'>{drift_section(drift)}</div>{DRIFT_JS}{js}{WRITE_JS}")


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
            f"<label>Transmit power (dBm)<input type='number' name='tx_power' value='{v('tx_power')}' min='0' max='30'></label>"
            f"<label>Position every (seconds)<input type='number' name='position_broadcast_secs' value='{v('position_broadcast_secs')}' min='32' max='86400'></label>"
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
            f"<div class='tablewrap'><table><thead><tr><th>Device</th><th>Against the profile</th><th>Read</th><th></th></tr></thead><tbody>{rows or '<tr><td colspan=4 class=meta>No device in the register yet, so there is nothing to check against the profile.</td></tr>'}</tbody></table></div>")


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
            f"<div class='tablewrap'><table><thead><tr><th>Image</th><th>On this box</th><th>Note</th></tr></thead><tbody>{rows or '<tr><td colspan=3 class=meta>No firmware pinned in this release.</td></tr>'}</tbody></table></div></div>")


ROLE_HINTS = {"TRACKER": "sends its position, does not relay", "ROUTER": "relays for everyone, costs battery, best up high", "CLIENT": "talks and relays a little",
              "CLIENT_MUTE": "talks, never relays", "ROUTER_CLIENT": "relays like a router and can be used like a client", "REPEATER": "relays only, no position, no messages",
              "SENSOR": "sends telemetry, sleeps between", "TAK": "for a radio tethered to ATAK", "CLIENT_HIDDEN": "talks, never appears in node lists", "LOST_AND_FOUND": "broadcasts its position often to be found",
              "TAK_TRACKER": "a tracker for TAK, position only", "ROUTER_LATE": "relays after the others have, for the edge of a mesh"}


def bench_name(path):
    """The device as the operator calls it, from its by-id name: usb-Seeed_T1000-E_9F3A-if00 reads Seeed T1000-E."""
    n = os.path.basename(str(path or ""))
    n = re.sub(r"^usb-", "", n); n = re.sub(r"-if\d+$", "", n)
    parts = [p for p in n.split("_") if p]
    if len(parts) > 1 and re.fullmatch(r"[0-9A-Fa-f:]{4,}", parts[-1]):
        parts = parts[:-1]
    return " ".join(parts) or n


def recovery_steps(path, shelf):
    """The bootloader drill as three numbered steps, naming the file for that hardware."""
    n = os.path.basename(str(path or "")).upper()
    hw = "TRACKER_T1000_E" if "T1000" in n else ("RAK4631" if "RAK4631" in n else None)
    img = next((i for i in (shelf or {}).get("images", []) if hw and hw in (i.get("hw") or []) and i.get("recommended") and not str(i.get("version") or "").startswith("erase")), None)
    file = (f"<code>{e(str(img['file']))}</code>" if img and img.get("file") else "the pinned firmware UF2 from the shelf below")
    vol = str((img or {}).get("volume") or "")
    return (f"<ol class='steps'><li>Double-press reset.</li><li>Copy {file} onto the volume that appears{(' (' + e(vol) + ')') if vol else ''}.</li><li>Wait for it to come back.</li></ol>")


def bench_cards(d, shelf=None):
    onb = _act("bench_onboard")
    roles = next((i.get("values", []) for i in onb.get("inputs", []) if i["name"] == "role"), [])
    hints = json.dumps(ROLE_HINTS).replace("'", "&#39;")
    cards = ""
    for dev in d.get("devices", []):
        path, name = str(dev.get("path") or ""), os.path.basename(str(dev.get("path") or ""))
        head = f"<div class='k'>{e(dev.get('tty') or '')}</div><div class='v'>{e(bench_name(path))}</div><div class='sub'>{e(name)}</div>"
        if dev.get("bootloader"):
            cards += f"<div class='card' data-path='{e(path)}'>{head}<p class='bad'>In bootloader mode: it answers nothing.</p>{recovery_steps(path, shelf)}</div>"
            continue
        if dev.get("kind") == "gps":
            cards += f"<div class='card' data-path='{e(path)}'>{head}<p class='meta'>The box's own GPS receiver on the same USB bus: not a radio, so nothing here opens it.</p></div>"
            continue
        cards += (f"<div class='card' data-path='{e(path)}'>{head}"
                  "<div class='row-actions' style='margin:.5rem 0'>"
                  f"<form data-action='bench_read' data-method='get' data-render='device'><input type='hidden' name='path' value='{e(path)}'><button type='submit' class='line' data-tip='Read' data-tip-more='Opens the device on its cable and shows what it says about itself'>Read</button></form>"
                  "</div><div class='out meta' role='status'></div>"
                  f"<details class='fold ctl primary'><summary data-tip='Onboard' data-tip-more='Names, a role, this radio&#39;s channel, region and admin key, each read back'>{ICONS['onboard']}Onboard</summary><form data-action='bench_onboard' data-risk='change' data-confirm=\"{e(onb.get('confirm') or '')}\">"
                  f"<input type='hidden' name='path' value='{e(path)}'>"
                  "<label>Long name (39 bytes at most)<input type='text' name='long_name' maxlength='39' required></label>"
                  "<label>Short name (4 bytes at most)<input type='text' name='short_name' maxlength='4' required></label>"
                  f"<label>Role<select name='role' data-hints='{hints}' data-tip='What this device does'>" + "".join(f"<option value='{e(str(rv))}'{' selected' if rv == 'TRACKER' else ''}>{e(str(rv))}</option>" for rv in roles) + "</select></label><div class='meta role-hint' style='margin:-6px 0 var(--s2)'>" + e(ROLE_HINTS.get("TRACKER", "")) + "</div>"
                  "<label>Label (kept on the box)<input type='text' name='label' maxlength='80' placeholder='e.g. Recce lead'></label>"
                  "<label>Who holds it<input type='text' name='holder' maxlength='80'></label>"
                  "<button type='submit'>Onboard it</button><div class='res meta' role='status'></div></form></details>"
                  f"<details class='fold ctl' style='margin-top:var(--s2)'><summary>More for this device</summary>"
                  f"<form data-action='bench_export' data-method='get' style='margin-top:var(--s2)'><input type='hidden' name='path' value='{e(path)}'><button type='submit' class='line' data-tip='Export' data-tip-more='Saves its settings and keys on the box, readable only by root'>{ICONS['export']} Export its configuration</button></form>"
                  + restore_flash_forms(path, shelf) + "</details></div>")
    return cards or "<p class='meta'>No device on the bench: plug one into the box by USB. This box's own radio is never listed here.</p>"


def restore_flash_forms(path, shelf):
    res, fl = _act("bench_restore"), _act("bench_flash")
    pins = [i for i in (shelf or {}).get("images", []) if i.get("state") == "verified"]
    opts = "".join(f"<option value='{e(str(i['id']))}' data-hw='{e(','.join(i.get('hw') or []))}' data-note='{e(str(i.get('note') or ''))}' hidden>{e(str(i.get('version') or ''))} · {e(', '.join(i.get('hw') or []))}{' · recommended' if i.get('recommended') else ''}</option>"
                   for i in sorted(pins, key=lambda x: (not x.get("recommended"), x.get("version") or "")))
    gate = "<div class='meta' data-read-first>Read the device first.</div>"
    restore = (f"<details class='fold ctl' style='margin-top:var(--s2)'><summary data-tip='Restore' data-tip-more='Put a saved configuration back'>{ICONS['restore']}Restore</summary><form data-action='bench_restore' data-risk='unreachable' data-confirm=\"{e(res.get('confirm') or '')}\">"
               f"<input type='hidden' name='path' value='{e(path)}'>"
               "<label>Export on the box<select name='export' data-exports><option value=''>Read the device first, then pick one of its exports</option></select></label>"
               "<label class='check'><input type='checkbox' name='confirm_tick'><span>I understand the device's names, channels and settings are replaced by the export's; its own keys stay. Ticked, this also allows an export made from a different device (a clone).</span></label>"
               f"<button type='submit' data-needs-read disabled>Restore it</button>{gate}<div class='res meta' role='status'></div></form></details>")
    flash = (f"<details class='fold ctl bad' style='margin-top:var(--s2)'><summary data-tip='Flash' data-tip-more='Writes new firmware; the device is off the mesh while it does'>{ICONS['flash']}Flash</summary><form data-action='bench_flash' data-risk='unreachable' data-flash='1' data-confirm=\"{e(fl.get('confirm') or '')}\" style='border-left:4px solid var(--bad);padding-left:var(--s2)'>"
             f"<input type='hidden' name='path' value='{e(path)}'>"
             f"<label>Firmware from the shelf<select name='image' data-pins data-tip='Firmware from the shelf' data-tip-more='Only images this release pins and this box has verified, for this hardware'>{opts or NO_IMAGE_OPT}</select></label>"
             "<label class='check'><input type='checkbox' name='confirm_tick'><span>I understand: the configuration is exported first, then the device is flashed and reboots; a factory image loses every setting; a flash that does not come back needs the recovery step. This device is the one named on Read.</span></label>"
             f"<button type='submit' class='danger' data-needs-read disabled>Flash it</button>{gate}<div class='res meta' role='status'></div><div class='stages meta'></div></form></details>")
    return restore + flash


NO_IMAGE_OPT = "<option value=''>no verified image on the shelf: see The shelf below</option>"


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
    var pins=card.querySelector('select[data-pins]');if(pins){[].forEach.call(pins.options,function(o){var hw=(o.dataset.hw||'').split(',');o.hidden=!!o.value&&hw.indexOf(j.hw)<0;});}
    card.querySelectorAll('[data-needs-read]').forEach(function(b){b.disabled=false;});card.querySelectorAll('[data-read-first]').forEach(function(x){x.hidden=true;});});
  document.addEventListener('change',function(ev){var sel=ev.target.closest('select[name=role][data-hints]');if(!sel)return;var h=sel.closest('form').querySelector('.role-hint');if(!h)return;try{h.textContent=(JSON.parse(sel.dataset.hints)||{})[sel.value]||'';}catch(e){}});
})();
</script>"""
    return (f"<p class='meta'>Radios plugged into this box by USB. This box's own radio ({e(bench_name(d.get('gateway') or '') or 'none')}) is set on the Radio page and never opened here. "
            "Read first, then Onboard; Export, Restore and Flash are under More. Every write is shown only once the device has answered with it.</p>"
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
                   f"<td><span class='{ {'verified': 'ok', 'wrong': 'bad', 'missing': 'warn'}.get(i.get('state') or 'missing', '') }'>{e(str(i.get('state') or 'missing'))}</span></td><td class='meta'>{e(str(i.get('note') or ''))}</td></tr>" for i in shelf.get("images", [])) or "<tr><td colspan=3 class='meta'>No firmware pinned in this release.</td></tr>"
    states = ("<table><thead><tr><th>What the line says</th><th>What it means</th></tr></thead><tbody>"
              "<tr><td>sent hh:mm, waiting for the read-back</td><td>The write went to the device; nothing is shown as true until the device itself answers.</td></tr>"
              "<tr><td>written and read back at hh:mm</td><td>The device's own answer matched what was written. This is the only state that means done.</td></tr>"
              "<tr><td>unconfirmed: sent hh:mm, the radio reports …</td><td>The device answered with something else. What it reports is what it holds, whatever was sent.</td></tr>"
              "<tr><td>asked, no answer yet (sent hh:mm)</td><td>No answer in the window. Over LoRa that is slow and lossy, not failed: read the device again later.</td></tr>"
              "<tr><td>not written: …</td><td>Refused before anything was sent, and the reason.</td></tr></tbody></table>")
    where = (f"<p class='meta'>Units <code>mesh-manager-bridge</code> (owns the radio{', forwards to TAK' if st.get('tak') != 'off' else ''}) and <code>mesh-manager-web</code> (this screen). The bridge answers on <code>{e(str(st.get('socket') or ''))}</code>; "
             f"its state, the register, the exports and the firmware shelf live under <code>{e(str(st.get('state_dir') or ''))}</code>. When something is wrong: <code>journalctl -u mesh-manager-bridge -n 200</code>. "
             "A radio in bootloader mode presents a serial port and answers nothing; the bridge waits rather than restarting, and the Bench page names the recovery step.</p>")
    setup = ("<h2 style='margin-top:0'>Setting the kit up</h2><ol class='steps'>"
             "<li><a href='/radio'>Name this radio</a> and check its region and preset: every device on the mesh must share them.</li>"
             "<li><a href='/channels'>Mint a channel</a> of your own, or adopt a join URL you were given. The default key is everyone's key.</li>"
             "<li><a href='/channels'>Show the join QR</a> to a phone, or <a href='/bench'>onboard a device on the bench</a> by USB: names, a role, this radio's channel and admin key, each read back.</li>"
             "<li><a href='/settings#position'>Say where this box is</a> if it has no GPS receiver, so the map has a centre.</li>"
             "<li><a href='/'>Watch the picture</a>: nodes, signal, battery and who has gone quiet. <a href='/health'>Health</a> holds the alerts and their thresholds.</li>"
             + ("<li>Point it at TAK: the bridge speaks to the TAK Server the installer was given; the <a href='/settings'>standing brief</a> tells connected agents what this mesh is for.</li>" if st.get("tak") != "off"
                else "<li>This box runs without TAK: the mesh is managed from this screen alone; the <a href='/settings'>standing brief</a> tells connected agents what this mesh is for.</li>") + "</ol>")
    return (f"{setup}<h2>The kit</h2><div class='cards'>{card('This radio', e(str(own.get('name') or '?')) + ' <span class=pill>' + e(str(own.get('id') or '')) + '</span>')}"
            f"{card('Rides', e(str(st.get('region') or '?')) + ' · ' + e(str(st.get('modem_preset') or '?')) + '<div class=meta>primary ' + e(str(st.get('primary_channel') or '?')) + '</div>')}"
            f"{card('The fleet', str(len(rows)) + ' device' + ('s' if len(rows) != 1 else '') + '<div class=meta>' + str(len(managed)) + ' managed by this radio</div>')}"
            f"{card('The radio is at', e(str(st.get('radio') or '?')))}</div>"
            f"<div class='tablewrap' style='margin-top:1rem'><table><thead><tr><th>Device</th><th>Holder</th><th>Hardware · firmware</th><th>Managed</th></tr></thead><tbody>{fleet}</tbody></table></div>"
            f"<h2>Before the kit travels</h2>{region}"
            f"<h2>The shelf</h2><p class='meta'>Firmware the fleet may carry, pinned in this release; recovery images are the way back when a device will not boot.</p><div class='tablewrap'><table><thead><tr><th>Image</th><th>On this box</th><th>Note</th></tr></thead><tbody>{pins}</tbody></table></div>"
            f"<h2>What goes wrong</h2><p class='meta'>The same rules the connected agent reads; every one was paid for on a real mesh.</p>{lessons_html()}"
            f"<h2>The four states of a write</h2>{states}<p class='meta'>Every time on this screen is Zulu.</p><h2>Where things are</h2>{where}")


def update_box(web):
    rec = U.last_check(web.state_dir)
    mode = web.update_mode()
    tok = bool(web.github_token())
    if not rec:
        last = "never checked" if tok else "never checked: add a GitHub token on Settings when the box has internet"
    elif rec.get("error"):
        last = f"checked {hhmm(rec.get('checked'))}: {rec['error']}"
    elif U.is_available(rec):
        last = f"checked {hhmm(rec.get('checked'))}: <b>{e(str(rec.get('version')))} available</b> ({e(str(rec.get('tag') or ''))}, published {e(str(rec.get('published') or '')[:10])})"
    else:
        last = f"checked {hhmm(rec.get('checked'))}: up to date ({e(str(rec.get('version') or __version__))} is the newest on {e(str(rec.get('channel') or ''))})"
    notes = ""
    if U.is_available(rec) and rec.get("notes"):
        notes = f"<details class='fold ctl'><summary>What is in {e(str(rec.get('version')))}</summary><pre class='log' style='max-height:40vh'>{e(str(rec['notes']))}</pre></details>"
    apply_btn = (f"<button type='button' data-update-apply='{e(str(rec.get('version')))}'>Update now to {e(str(rec.get('version')))}</button>" if U.is_available(rec) else "")
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
    .catch(function(){chk.disabled=false;res(window.mmNoAnswer,'bad');});});}
  var ap=document.querySelector('[data-update-apply]');if(ap){ap.addEventListener('click',function(){var v=ap.dataset.updateApply;
    window.mmConfirm('Update to '+v+'? The release is downloaded and checked, then the bridge and this screen restart; the mesh is off TAK for about a minute.',ap.closest('.card'),function(){
    ap.disabled=true;res('downloading and checking '+v);
    fetch('/api/update/apply',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({version:v})}).then(function(r){return r.json();}).then(function(j){
      if(j.error){res('not applied: '+j.error,'bad');ap.disabled=false;return;}res('installing '+v+'; the screen will come back on the new version','warn');
      var t0=Date.now();(function poll(){setTimeout(function(){fetch('/healthz').then(function(r){return r.json();}).then(function(h){if(h.version&&h.version!==j.running){window.location.href='/about';}else if(Date.now()-t0<600000){poll();}else{res('the screen is back but still on '+h.version+': read the last update log below','bad');}}).catch(function(){if(Date.now()-t0<600000){poll();}else{res('the screen did not come back in ten minutes: ssh to the box and read journalctl -u mesh-manager-update','bad');}});},3000);})();})
    .catch(function(){res('the box went away mid-request; it may be restarting','warn');});});});}
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


def node_body(n, tel, msgs, npos, hours, env=None, availability=None):
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
    env = env or []
    envblock = ""
    if env:
        last = env[-1]
        bits = [f"{last['temperature']:.1f} °C" if last.get("temperature") is not None else None,
                f"{last['humidity']:.0f}% RH" if last.get("humidity") is not None else None,
                f"{last['pressure']:.0f} hPa" if last.get("pressure") is not None else None]
        last_ts = str(last.get("ts") or "")
        when_html = f"<div class='sub'><time datetime='{e(last_ts)}' data-age>{e(age(last_ts))}</time></div>"
        envblock = (f"<h2>Environment</h2><div class='cards'>{card('Latest reading', e(' · '.join(b for b in bits if b)) + when_html)}</div>"
                    f"<h3>Temperature</h3>{series_chart(env, 'temperature', ' °C', None, None, (), 'temperature')}")
    avblock = ""
    if availability and availability.get("series"):
        ser = availability["series"]; w = max(2, min(12, int(600 / max(1, len(ser)))))
        bars = "".join(f"<rect x='{i * w}' y='{2 if v else 16}' width='{w - 1}' height='{18 if v else 4}' class='{'on' if v else 'off'}'/>" for i, v in enumerate(ser))
        avblock = (f"<h2>Heard</h2><p class='meta'><b>{availability.get('pct')}%</b> of the window: heard in {availability.get('heard')} of {availability.get('buckets')} "
                   f"{'hours' if availability.get('bucket_secs') == 3600 else 'days'}.</p>"
                   f"<svg class='chart avail' viewBox='0 0 {len(ser) * w} 20' width='{len(ser) * w}' height='20' role='img' aria-label='heard per bucket'>{bars}</svg>")
    return (f"<div class='cards'>{facts}</div>{form}{avblock}"
            f"<h2>Battery</h2>{series_chart(levels, 'level', '%', 0, 100, ((20, 'bad'),), 'battery')}"
            + (f"<p class='meta'>On charge at {e(', '.join(charging[-6:]))}{' and earlier' if len(charging) > 6 else ''} (shown as 100%).</p>" if charging else "")
            + f"<h2>Voltage</h2>{series_chart(tel, 'voltage', ' V', None, None, ((3.3, 'bad'),), 'voltage')}"
            + envblock +
            f"<h2>Last messages</h2><div class='tablewrap'><table><thead><tr><th>When</th><th>To</th><th>Message</th></tr></thead><tbody>{rows or '<tr><td colspan=3 class=meta>No message from this node in the window.</td></tr>'}</tbody></table></div>")


def health_chart(h):
    """Channel utilisation by the hour, the 25 and 40 percent lines drawn."""
    pts = h.get("hourly") or []
    if len(pts) < 2:
        return "<p class='meta'>Not enough readings from this radio yet for a chart; it reports every few minutes.</p>"
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
        return f"<p class='bad'>{e(str((h or {}).get('error') or 'The bridge did not answer, so there are no health figures. The Mesh page says whether it is running.'))}</p>"
    v = h.get("verdict") or "unknown"
    cls = {"quiet": "ok", "normal": "ok", "busy": "warn", "saturated": "bad"}.get(v, "")
    ch = h.get("chutil"); air = h.get("airutil"); bud = h.get("budget_pct")
    air_txt = ("no reading" if air is None else f"{float(air):.1f}%") + (f"<div class='meta'>of a {bud:g}% budget on {e(h.get('region') or '')}: {h.get('air_share') if h.get('air_share') is not None else '?'}% used</div>" if bud else f"<div class='meta'>no duty-cycle limit on {e(h.get('region') or 'this region')}</div>")
    cards = (card("Channel utilisation", (f"{float(ch):.1f}%" if ch is not None else "no reading") + f" <span class='pill'>{e(v)}</span>", cls)
             + card("This radio's transmit time", air_txt, "bad" if (h.get("air_share") or 0) >= 80 else ("warn" if (h.get("air_share") or 0) >= 50 else ""))
             + card("Packets per hour", f"{h.get('packets_per_hour', 0)}<div class='meta'>{h.get('packets', 0)} in {h.get('hours')} h</div>")
             + card("Nodes heard", f"{h.get('nodes_heard', 0)}<div class='meta'>last {h.get('hours')} h, from the history</div>"))
    rows = ""
    for d in h.get("nodes") or []:
        rows += (f"<tr><td><b>{e(d.get('name') or d.get('id'))}</b>{' <span class=pill>this radio</span>' if d.get('own') else ''}<div class='sub'>{e(d.get('id') or '')}</div></td>"
                 f"<td>{d.get('packets', 0)}</td><td>{d.get('per_hour', 0)}</td>"
                 f"<td>{('%.1f%%' % float(d['chutil'])) if d.get('chutil') is not None else '<span class=sub>none</span>'}</td>"
                 f"<td>{('%.2f%%' % float(d['airutil'])) if d.get('airutil') is not None else '<span class=sub>none</span>'}</td>"
                 f"<td>{(str(int(d['battery'])) + '%') if d.get('battery') is not None and 0 <= int(d['battery']) <= 100 else ('on charge' if d.get('battery') is not None and int(d['battery']) > 100 else '<span class=sub>none</span>')}</td>"
                 f"<td class='meta'>{('<time datetime=' + chr(39) + e(d['last_telemetry']) + chr(39) + ' data-age>' + e(age(d['last_telemetry'])) + '</time>') if d.get('last_telemetry') else 'none'}</td></tr>")
    return (f"<div class='cards'>{cards}</div><h2>Channel utilisation by the hour</h2>{health_chart(h)}"
            "<h2>Per node</h2><p class='meta'>Packets this radio heard from each node in the window, and the last device metrics each reported. Utilisation is the share of air time the node's radio hears busy; air time is the share it spends transmitting.</p>"
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
            f"<div class='tablewrap'><table><thead><tr><th>Device</th><th>State</th><th>First heard after</th></tr></thead><tbody>{wait}{back or ''}{'' if (wait or back) else '<tr><td colspan=3 class=meta>No device was expected back.</td></tr>'}</tbody></table></div>{form}")


def alerts_section(al, tak_on=True):
    """Spec 026: what is open, what was, the thresholds, and the test. Spec 050: without TAK, no TAK chat and no test."""
    al = al or {}
    st = al.get("settings") or {}
    kinds = {"silent": "warn", "battery": "warn", "unknown": "bad", "fence": "bad", "geofence": "warn", "key": "bad"}
    open_rows = "".join(f"<tr><td><span class='pill' style='background:var(--{kinds.get(o.get('kind'), 'warn')});color:#fff;border-color:transparent'>{e(o.get('kind'))}</span></td><td>{e(o.get('text'))}</td><td class='meta'><time datetime='{e(o.get('since'))}' data-age>{e(age(o.get('since')))}</time></td></tr>" for o in al.get("open") or [])
    recent = "".join(f"<tr><td class='meta'><time datetime='{e(r.get('ts'))}' data-age>{e(age(r.get('ts')))}</time></td><td>{e(r.get('kind'))}</td><td>{e(r.get('text'))}</td><td class='meta'>{'cleared ' + e(hhmm(r.get('cleared'))) if r.get('state') == 'cleared' else 'open'}</td></tr>" for r in list(reversed(al.get("recent") or []))[:20])
    a = _act("alert_set"); t = _act("alert_test")
    def opt(name, on):
        return f"<option value='on'{' selected' if on else ''}>on</option><option value='off'{'' if on else ' selected'}>off</option>"
    onoff = (("on", "On"), ("off", "Off"))
    form = (f"<form data-action='alert_set' class='card' data-risk='change' data-confirm=\"{e(a.get('confirm') or '')}\" style='max-width:720px'><p class='meta'>{e(a['description'])}</p>"
            f"<div class='regform' style='grid-template-columns:1fr 1fr 1fr'><label>Silent after (minutes)<input type='number' name='silent_min' value='{int(st.get('silent_min', 30))}' min='1' max='1440'></label>"
            f"<label>Battery under (%)<input type='number' name='battery_pct' value='{int(st.get('battery_pct', 20))}' min='1' max='90'></label>"
            f"<label data-tip='Fence around this box' data-tip-more='A radius from the box&#39;s own position; drawn fences live on the map'>Fence around this box (metres, 0 is off)<input type='number' name='fence_m' value='{int(st.get('fence_m', 0))}' min='0' max='100000'></label>"
            f"<div><span class='meta'>Unknown nodes</span><br>{seg('unknown', onoff, 'on' if st.get('unknown', True) else 'off')}</div>"
            + (f"<div><span class='meta'>To TAK chat</span><br>{seg('to_tak', onoff, 'on' if st.get('to_tak', True) else 'off')}</div>" if tak_on else "<div></div>") +
            "<div></div></div><button class='line' style='margin-top:var(--s2)'>Save the thresholds</button><div class='res meta' role='status'></div></form>")
    test = "" if not tak_on else (f"<form data-action='alert_test' style='display:inline-block;margin-top:var(--s2)'><button class='quiet' data-tip='Send a test alert to TAK' data-tip-more='{e(t['description'])}'>{e(t['title'])}</button><div class='res meta' role='status'></div></form>")
    return (f"<h2 id='alerts' style='margin-top:0'>Alerts</h2><p class='meta'>A registered device gone quiet, a battery under the threshold, a node not in the register, a node outside a fence. Each is shown here" + (" and sent to All Chat Rooms on the TAK Server when To TAK chat is on." if tak_on else ".") + "</p>"
            f"<div class='tablewrap'><table><thead><tr><th>Open</th><th>What</th><th>Since</th></tr></thead><tbody>{open_rows or '<tr><td colspan=3 class=meta>Nothing open.</td></tr>'}</tbody></table></div>"
            f"<h2>Recent</h2><div class='tablewrap'><table><thead><tr><th>When</th><th>Kind</th><th>What</th><th>State</th></tr></thead><tbody>{recent or '<tr><td colspan=4 class=meta>None yet.</td></tr>'}</tbody></table></div>"
            f"<details class='fold' data-keep='thresholds'><summary>Thresholds</summary>{form}{test}</details>")


def health_body(h, al=None, tak_on=True):
    js = "<script>window.onMesh=function(d){if(d.kind==='status'){window.mmFrag('health','health-body');}if(d.kind==='alert'){window.mmFrag('alerts','alerts-body');}};</script>"
    js = js.replace("window.mmFrag('alerts','alerts-body');", "var o=document.querySelector('#alerts-body details'),was=!!(o&&o.open);window.mmFrag('alerts','alerts-body',function(){var n=document.querySelector('#alerts-body details');if(n&&was){n.open=true;}});")
    _out = ((f"<div id='alerts-body'>{alerts_section(al, tak_on)}</div>"
            "<h2>How busy the mesh is</h2><p class='meta'>From the history store. On LoRa the channel utilisation is the number that says whether the mesh is about to fall over: under 10% is quiet, under 25% normal, under 40% busy, above that saturated. On EU_868 this radio's own transmit air time must stay under the 10% duty-cycle limit.</p>"
            f"<div id='health-body'>{health_cards(h)}</div>{js}{WRITE_JS}"))
    return _out + export_section()

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



# ---- Spec 037: the history as a file ------------------------------------------------------------
EXPORT_KINDS = {"positions": ("gpx", "kml", "csv"), "messages": ("csv",), "packets": ("csv",), "telemetry": ("csv",), "environment": ("csv",)}


def export_csv(rows):
    import csv as _csv, io as _io
    cols = []
    for r in rows:
        for k in r:
            if k not in cols and k != "id":
                cols.append(k)
    out = _io.StringIO(); w = _csv.writer(out, lineterminator="\n")
    w.writerow(cols)
    for r in rows:
        w.writerow(["" if r.get(c) is None else r.get(c) for c in cols])
    return out.getvalue().encode("utf-8")


def export_gpx(rows, names=None):
    from xml.etree.ElementTree import Element, SubElement, tostring
    names = names or {}
    g = Element("gpx", {"version": "1.1", "creator": "Mesh Manager", "xmlns": "http://www.topografix.com/GPX/1/1"})
    by = {}
    for r in rows:
        if r.get("lat") is None or r.get("lon") is None:
            continue
        by.setdefault(str(r.get("node") or "?"), []).append(r)
    for node, pts in by.items():
        trk = SubElement(g, "trk"); SubElement(trk, "name").text = names.get(node) or node
        seg = SubElement(trk, "trkseg")
        for r in pts:
            p = SubElement(seg, "trkpt", {"lat": f"{float(r['lat']):.6f}", "lon": f"{float(r['lon']):.6f}"})
            if r.get("ts"):
                SubElement(p, "time").text = str(r["ts"])
    return b'<?xml version="1.0" encoding="UTF-8"?>\n' + tostring(g)


def export_kml(rows, names=None):
    from xml.etree.ElementTree import Element, SubElement, tostring
    names = names or {}
    k = Element("kml", {"xmlns": "http://www.opengis.net/kml/2.2"}); doc = SubElement(k, "Document")
    SubElement(doc, "name").text = "Mesh Manager positions"
    by = {}
    for r in rows:
        if r.get("lat") is None or r.get("lon") is None:
            continue
        by.setdefault(str(r.get("node") or "?"), []).append(r)
    for node, pts in by.items():
        pm = SubElement(doc, "Placemark"); SubElement(pm, "name").text = names.get(node) or node
        ls = SubElement(pm, "LineString"); SubElement(ls, "tessellate").text = "1"
        SubElement(ls, "coordinates").text = " ".join(f"{float(r['lon']):.6f},{float(r['lat']):.6f},0" for r in pts)
    return b'<?xml version="1.0" encoding="UTF-8"?>\n' + tostring(k)


def export_section():
    """Spec 037: the Export control on the Health page. The URL is built in the browser from the selects."""
    words = {"positions": "positions", "messages": "messages", "packets": "packets", "telemetry": "battery and voltage", "environment": "temperature, humidity, pressure"}
    kinds = "".join(f"<option value='{k}'>{e(words.get(k, k))}</option>" for k in EXPORT_KINDS)
    return ("<h2 id='export'>Export</h2><p class='meta'>The history as a file, for a report or for Pinecone: positions as GPX, KML or CSV; messages, packets, battery and environment as CSV. "
            "Only what the box already holds: ids, your labels, times, positions and the text already on the channels.</p>"
            f"<form class='controls' id='export-form'><label data-tip='What' data-tip-more='Comes from the box&#39;s history, as far back as the window'>What <select id='ex-kind'>{kinds}</select></label>"
            "<label>Window <select id='ex-hours'><option value='24'>24 h</option><option value='168'>7 d</option><option value='720'>30 d</option></select></label>"
            "<span><span class='meta'>Format</span> " + seg("fmt", (("gpx", "GPX"), ("kml", "KML"), ("csv", "CSV")), "gpx", attrs="id='ex-fmt'") + "</span>"
            "<button type='submit' class='line'>Download</button><span class='res meta' id='ex-res' role='status'></span></form>"
            "<script>(function(){var f=document.getElementById('export-form');if(!f)return;var k=document.getElementById('ex-kind'),fm=document.getElementById('ex-fmt');"
            "var allowed=" + json.dumps({k: list(v) for k, v in EXPORT_KINDS.items()}) + ";"
            "function chosen(){var c=fm.querySelector('input:checked');return c?c.value:'csv';}"
            "function fix(){var a=allowed[k.value]||['csv'];fm.querySelectorAll('input').forEach(function(o){o.disabled=a.indexOf(o.value)<0;});if(a.indexOf(chosen())<0){var first=fm.querySelector(\"input[value='\"+a[0]+\"']\");if(first)first.checked=true;}}"
            "k.addEventListener('change',fix);fix();"
            "f.addEventListener('submit',function(ev){ev.preventDefault();var h=document.getElementById('ex-hours').value;var r=document.getElementById('ex-res');if(r){r.textContent='asked the box for '+k.options[k.selectedIndex].text+', '+h+' h';r.className='res meta ok';}window.location.href='/export/'+k.value+'.'+chosen()+'?hours='+h;});})();</script>")


# ---- Spec 038: quick messages ----------------------------------------------------------------------
QUICK_DEFAULTS = ["Check in", "RTB", "Send your location"]


def quick_load(etc_dir):
    try:
        v = json.load(open(os.path.join(etc_dir, "quick.json")))
        return [str(x) for x in v if str(x).strip()] if isinstance(v, list) else list(QUICK_DEFAULTS)
    except (OSError, ValueError):
        return list(QUICK_DEFAULTS)


def quick_save(etc_dir, msgs):
    """Returns (list, error). Up to eight, each 200 bytes at most, none empty."""
    if not isinstance(msgs, list):
        return None, "messages must be a list"
    out = [str(m).strip() for m in msgs if str(m).strip()]
    if len(out) > 8:
        return None, "eight quick messages at most"
    for m in out:
        if len(m.encode()) > 200:
            return None, "a quick message is 200 bytes at most, like any mesh message"
    with open(os.path.join(etc_dir, "quick.json"), "w") as fh:
        json.dump(out, fh)
    return out, None


# ---- Spec 039: the packet inspector ---------------------------------------------------------------
def _snr_txt(v):
    return "" if v is None else f"{float(v):.1f} dB"


def packets_body(rows, hours, node, port, labels):
    counts = {}
    for r in rows:
        counts[str(r.get("port") or "?")] = counts.get(str(r.get("port") or "?"), 0) + 1
    ports = sorted(counts, key=lambda k: -counts[k])
    shown = [r for r in rows if (not node or r.get("node") == node) and (not port or str(r.get("port") or "") == port)]
    shown = list(reversed(shown))   # newest first
    body = "".join(
        f"<tr><td class='meta'><time datetime='{e(str(r.get('ts') or ''))}' data-age>{e(age(str(r.get('ts') or '')))}</time></td>"
        f"<td>{e(labels.get(str(r.get('node') or ''), str(r.get('node') or '?')))}<div class='sub'>{e(str(r.get('node') or ''))}</div></td>"
        f"<td><code>{e(str(r.get('port') or '?'))}</code></td>"
        f"<td style='font-variant-numeric:tabular-nums'>{_snr_txt(r.get('snr'))}</td>"
        f"<td>{'' if r.get('hops') is None else r.get('hops')}</td><td>{'' if r.get('size') is None else str(r.get('size')) + ' B'}</td></tr>"
        for r in shown[:500])
    hsel = "".join(f"<option value='{h}'{' selected' if h == hours else ''}>{t}</option>" for h, t in ((1, "1 h"), (6, "6 h"), (24, "24 h"), (48, "48 h"), (168, "7 d")))
    psel = "<option value=''>every port</option>" + "".join(f"<option value='{e(p)}'{' selected' if p == port else ''}>{e(p)}</option>" for p in ports)
    nodes = sorted({str(r.get("node") or "") for r in rows if r.get("node")})
    nsel = "<option value=''>every node</option>" + "".join(f"<option value='{e(n)}'{' selected' if n == node else ''}>{e(labels.get(n, n))}</option>" for n in nodes)
    chips = " ".join(f"<a class='pill' href='/packets?hours={hours}&port={e(p)}' data-portcount='{counts[p]}'>{e(p)} <b>{counts[p]}</b></a>" for p in ports)
    js = r"""<script>(function(){var last=0;function refresh(){var now=Date.now();if(now-last<2000)return;last=now;
  fetch(window.location.href).then(function(r){return r.text();}).then(function(h){var d=new DOMParser().parseFromString(h,'text/html');['pkt-rows','pkt-counts','pkt-cap'].forEach(function(id){var nb=d.getElementById(id),ob=document.getElementById(id);if(nb&&ob){ob.innerHTML=nb.innerHTML;}});}).catch(function(){});}
  window.onMesh=function(d){if(d.kind==='packet'){refresh();}};})();</script>"""
    cap = (f"the newest 500 of {len(shown)} in the window" if len(shown) > 500 else f"{len(shown)} in the window")
    js = js.replace("window.onMesh=function(d){if(d.kind==='packet'){refresh();}};",
                    "window.onMesh=function(d){if(d.kind==='packet'){refresh();}};"
                    "var fm=document.getElementById('pkt-filters');if(fm){fm.addEventListener('change',function(){var q=new URLSearchParams(new FormData(fm)).toString();history.replaceState(null,'',window.location.pathname+'?'+q);var n=document.getElementById('pkt-note');if(n){n.textContent='filtering…';}last=0;refresh();setTimeout(function(){if(n){n.textContent='';}},1500);});}")
    return (f"<p class='meta'>Every packet this radio heard in the window, newest first: <span id='pkt-cap'>{e(cap)}</span>. The port is what the packet carried; hops is how many relays it came through; a direct packet is 0.</p>"
            f"<form method='get' action='/packets' class='controls' id='pkt-filters'><label>Window <select name='hours'>{hsel}</select></label>"
            f"<label>Node <select name='node'>{nsel}</select></label><label data-tip='Port' data-tip-more='What the packet carried, in Meshtastic&#39;s own names'>Port <select name='port'>{psel}</select></label><span class='meta' id='pkt-note'></span></form>"
            f"<p id='pkt-counts'>{chips or '<span class=meta>nothing in the window</span>'}</p>"
            f"<div class='tablewrap'><table><thead><tr><th>When</th><th>From</th><th data-tip='Port' data-tip-more='What the packet carried, in Meshtastic&#39;s own names'>Port</th><th>SNR</th><th>Hops</th><th>Size</th></tr></thead><tbody id='pkt-rows'>{body or '<tr><td colspan=6 class=meta>No packets match. Widen the window, or set the node and port back to every.</td></tr>'}</tbody></table></div>{js}")


# ---- Spec 042: the mesh as a graph -------------------------------------------------------------------
def graph_body(nb, hours):
    import math
    edges = nb.get("edges") or []; own = nb.get("own")
    hsel = "".join(f"<option value='{h}'{' selected' if h == hours else ''}>{t}</option>" for h, t in ((1, "1 h"), (6, "6 h"), (24, "24 h"), (168, "7 d")))
    form = f"<form method='get' action='/graph' class='controls'><label>Window <select name='hours' onchange='this.form.submit()'>{hsel}</select></label></form>"
    if not edges:
        return (form + "<p class='meta'><b>No neighbour reports in the window.</b> This graph is drawn from the neighbour-info module, which a node broadcasts only when it is switched on. "
                "Turn neighbour info on in the Meshtastic app or on the bench, with an interval of a few minutes; there is no control for it on this screen yet. The edges appear as the reports arrive. "
                "This radio's own links to each node are on the map meanwhile.</p>")
    ids, names = [], {}
    for x in edges:
        for k, nk in (("from", "from_name"), ("to", "to_name")):
            if x.get(k) and x[k] not in names:
                ids.append(x[k]); names[x[k]] = x.get(nk) or x[k]
    n = len(ids); W_, H_ = 720, 480; cx, cy, r = W_ / 2, H_ / 2, min(W_, H_) / 2 - 60
    pos = {nid: (cx + r * math.cos(2 * math.pi * i / n - math.pi / 2), cy + r * math.sin(2 * math.pi * i / n - math.pi / 2)) for i, nid in enumerate(ids)}
    def tone(snr):
        if snr is None: return "--ink-muted"
        return "--ok" if snr >= 8 else ("--warn" if snr >= 2 else "--bad")
    lines = "".join(
        f"<line data-edge='{e(x['from'])}|{e(x['to'])}' x1='{pos[x['from']][0]:.0f}' y1='{pos[x['from']][1]:.0f}' x2='{pos[x['to']][0]:.0f}' y2='{pos[x['to']][1]:.0f}' "
        f"stroke='var({tone(x.get('snr'))})' stroke-width='{1.5 + max(0.0, min(6.0, (x.get('snr') or 0) / 2)):.1f}' stroke-opacity='0.85'><title>{e(names[x['from']])} hears {e(names[x['to']])} at {e(str(x.get('snr')))} dB</title></line>"
        for x in edges if x.get("from") in pos and x.get("to") in pos)
    dots = "".join(
        f"<g data-node='{e(nid)}'><circle cx='{pos[nid][0]:.0f}' cy='{pos[nid][1]:.0f}' r='{11 if nid == own else 8}' fill='var({'--gold' if nid == own else '--surface-raised'})' stroke='var(--accent)' stroke-width='2'/>"
        f"<text x='{pos[nid][0]:.0f}' y='{pos[nid][1] + 24:.0f}' text-anchor='middle'>{e(names[nid])}</text></g>"
        for nid in ids)
    return (form + f"<p class='meta'>{len(edges)} edge{'s' if len(edges) != 1 else ''} from {len({x.get('from') for x in edges})} reporting node{'s' if len({x.get('from') for x in edges}) != 1 else ''}. An edge points from the node that reported to the neighbour it heard, weighted by the SNR it heard it at; green is a comfortable link, amber marginal, red about to fail. The gold node is this box.</p>"
            f"<svg class='chart graph' viewBox='0 0 {W_} {H_}' width='{W_}' height='{H_}' role='img' aria-label='the mesh as a graph' style='max-width:100%;height:auto'>{lines}{dots}</svg>")


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
            "restart, so the mesh is off TAK for about a minute. The five most recent are kept; older ones "
            "are removed as new releases arrive.</p>"
            f"{warn}{items}<div class='res meta' id='rollback-res' role='status'></div></div>")


ROLLBACK_JS = r"""<script>
(function(){
  function res(t,c){var r=document.getElementById('rollback-res');if(r){r.textContent=t;r.className='res meta '+(c||'');}}
  document.querySelectorAll('[data-rollback]').forEach(function(b){b.addEventListener('click',function(){
    var v=b.getAttribute('data-rollback');
    window.mmConfirm('Roll back to '+v+'? The bridge and this screen restart, so the mesh is off TAK for about a minute. This returns the code, not the box\'s settings.',b.closest('.card'),function(){
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
      .catch(function(){b.disabled=false;res(window.mmNoAnswer,'bad');});});});});
})();
</script>"""


def about_body(st, web):
    return (f"{update_box(web)}{rollback_box(web)}{UPDATE_JS}{ROLLBACK_JS}<div class='cards' style='margin-top:1rem'>{card('Mesh Manager', e(__version__))}{card('Bridge', e(str(st.get('version') or 'not answering')))}"
            f"{card('Licence', 'GPL-3.0-or-later')}{card('Heartbeat file', e(st.get('state_dir') or '/var/lib/vantage-mesh') + '/heartbeat.json')}"
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
        web.messages.append({"ts": r.get("ts"), "from": r.get("node"), "name": r.get("name") or r.get("node"), "to": r.get("dest"), "mid": r.get("mid"), "ack": r.get("ack"), "sent": r.get("mid") is not None,
                             "channel": r.get("channel"), "text": r.get("text"), "stored": True})


def message_rows(web, labels=None):
    seed_messages(web)
    rows = ""
    labels = labels or {}
    for m in list(web.messages):
        who = labels.get(str(m.get("from") or "")) or str(m.get("name") or m.get("from") or "")
        ts = str(m.get("ts") or "")
        when = f"<time datetime='{e(ts)}' data-age>{e(age(ts))}</time>" if ts else ""
        ack = m.get("ack")
        if ack == "delivered":
            state = " <span class='pill' style='background:var(--ok);color:#fff;border-color:var(--ok)'>delivered</span>"
        elif ack:
            state = f" <span class='pill' style='background:var(--bad);color:#fff;border-color:var(--bad)' data-tip='The radio gave up' data-tip-more='{e(str(ack))}'>not delivered · {e(str(ack).replace('_', ' ').lower())}</span>"
        elif m.get("sent") or m.get("mid") is not None:
            state = f" <span class='pill'>handed to the radio {e(hhmm(ts))}</span>"
        else:
            state = ""
        rows += (f"<tr><td class='meta'>{when}</td><td>{e(who)}</td>"
                 f"<td>{e('everyone' if str(m.get('to') or '') == '^all' else str(m.get('to') or ''))}</td><td>{e(str(m.get('text') or ''))}</td><td>{state.strip() or ''}</td></tr>")
    return rows or "<tr><td colspan=5 class='meta'>Nothing heard on the channels since the bridge started. Anything you send shows here too.</td></tr>"


def messages_body(web, nodes, chans=None, st=None, groups=None):
    """Spec 048: Messages as a chat. The list of chats on the left, up to three open on the right, from
    what the box holds and hears; the page's script derives the conversations."""
    send = _act("send_text")
    live = [c for c in (chans or []) if c.get("role") != "DISABLED"] or [{"index": 0, "name": (st or {}).get("primary_channel") or "primary", "role": "PRIMARY"}]
    own = (st or {}).get("own") or {}
    heard = [n for n in nodes if n.get("heard_here", True)]
    quick = quick_load(web.etc_dir)
    glist = [dict(g, members=[n.get("id") for n in nodes if str(n.get("group") or "") == str(g.get("name"))]) for g in ((groups or {}).get("groups") or [])]
    nmap = {str(n.get("id")): {"name": dname(n), "icon": str(n.get("icon") or "radio"), "group": str(n.get("group") or ""), "db": not n.get("heard_here", True)} for n in nodes if n.get("id")}
    chips = "".join(f"<button type='button' class='line' data-quick='{e(m)}'>{e(m)}</button>" for m in quick)
    data = json.dumps({"own": own.get("id") or "", "own_name": own.get("name") or "this box", "channels": [{"index": int(c.get("index", 0)), "name": c.get("name") or f"slot {c.get('index')}", "role": c.get("role")} for c in live],
                       "groups": [{"name": g.get("name"), "icon": g.get("icon") or "radio", "count": int(g.get("count") or 0), "members": g.get("members") or []} for g in glist],
                       "nodes": nmap, "heard": len(heard), "icons": {k: NODE_ICON_SVG[k] for k in NODE_ICON_SVG}, "users": ICONS["users"], "hash": ICONS["menu"],
                       "dots": ICONS["dots"], "muted": ICONS["bell_off"], "pin": ICONS["pin"]}).replace("&", "&amp;").replace("'", "&#39;")
    tools = ("<div class='chat-tools'>" + icon_button("plus", "New message", "New message", "Start a chat with any radio, channel or group, spoken to or not", attrs="id='chat-new'")
             + icon_button("check_all", "Mark all read", "Mark all read", "Every chat's unread count to nought", attrs="id='chat-readall'")
             + "<span class='chat-total' id='chat-total' aria-live='polite'></span>"
             "<input type='search' id='chat-filter' placeholder='Find a chat or a line' aria-label='Find a chat or a line' autocomplete='off'>"
             "<button type='button' class='line' id='chat-hidden' hidden>Show hidden</button></div>")
    closex = "<svg viewBox='0 0 16 16' fill='none' stroke='currentColor' stroke-width='1.6' stroke-linecap='round' aria-hidden='true'><path d='M3.5 3.5l9 9M12.5 3.5l-9 9'/></svg>"
    picker = ("<template id='chat-picker'><div class='chat-picker'><div class='chat-head'><span class='nm'>New message</span>"
              f"<button type='button' class='line icon close' aria-label='Close' data-tip='Close'>{closex}</button></div>"
              "<input type='search' placeholder='Name or radio id' aria-label='Name or radio id' autocomplete='off'><div class='chat-picks' aria-live='polite'></div></div></template>")
    return (f"<div class='chat' id='chat' data-chat='{data}' data-confirm-channel='Send to everyone on {{channel}}, {{count}} devices heard here: “{{text}}”' "
            "data-confirm-direct='Send only to {node}: “{text}”. No one else on the mesh sees it.' "
            "data-confirm-group='Send to {group}: one direct message to each device, each with its own receipt: “{text}”'>"
            f"<aside class='chat-side' aria-label='Chats'>{tools}<div class='chat-list' id='chat-list'></div></aside><section class='chat-panes' id='chat-panes' aria-live='polite'></section></div>{picker}"
            f"<template id='chat-composer'><div class='chat-compose'>{('<div class=quick data-tip=Fills-the-box>' + chips + '</div>') if chips else ''}"
            f"<form data-action='send_text' data-chat-composer><input type='text' name='text' maxlength='200' required placeholder='{e(send['title'])} (200 bytes at most)' aria-label='Message' autocomplete='off'><button type='submit'>Send</button>"
            "<div class='note'></div><div class='res meta' role='status'></div></form></div></template>"
            f"<p class='meta' style='margin-top:var(--s3)'>{e(send['description'])} A direct message sends on Enter; a message to a channel or a group asks first, because every device hears it and it costs airtime. Receipts show on each bubble: handed to the radio, delivered, or not delivered and why; a message the radio gave up on can be sent again from its bubble. A message to everyone is never acknowledged. New message starts a chat with any radio, channel or group; each chat's menu marks it read or unread, pins, mutes or hides it; the field above the list finds a chat or a line.</p>"
            + CHAT_JS)


CHAT_JS = r"""<script>
(function(){
  var root=document.getElementById('chat');if(!root)return;var D=JSON.parse(root.dataset.chat||'{}'),own=D.own||'';
  /* chat:pure:start */
  function chatKey(m,own){var to=String(m.to||'^all'),from=String(m.from||'');if(to==='^all'||to==='!ffffffff'||to==='')return 'ch:'+((m.channel===undefined||m.channel===null)?0:m.channel);if(from===own)return 'dm:'+to;if(to===own)return 'dm:'+from;return 'dm:'+from;}
  function chatsFrom(msgs,own,channels,groups,seen){seen=seen||{};var by={};
    function ent(key,name,sub){if(!by[key])by[key]={key:key,name:name,sub:sub||'',last:'',ts:null,unread:0,count:0};return by[key];}
    (channels||[]).forEach(function(c){if(c.role==='DISABLED')return;ent('ch:'+c.index,c.name||('slot '+c.index),'everyone on the channel');});
    (groups||[]).forEach(function(g){ent('group:'+g.name,g.name,(g.count||0)+' device'+(g.count===1?'':'s')+', one message each');});
    (msgs||[]).forEach(function(m){var k=chatKey(m,own);var e=ent(k,k.indexOf('dm:')===0?(m.from===own?String(m.to):(m.name||m.from)):k,k.indexOf('dm:')===0?'direct':'');
      var ts=Date.parse(m.ts||'')||0;if(!e.ts||ts>=e.ts){e.ts=ts;e.last=m.text||'';}e.count++;if(m.from!==own&&ts>(seen[k]||0))e.unread++;});
    (groups||[]).forEach(function(g){var e=by['group:'+g.name];(g.members||[]).forEach(function(id){var d=by['dm:'+id];if(d&&d.ts&&(!e.ts||d.ts>e.ts)){e.ts=d.ts;e.last=d.last;}});});
    function rank(k){return k.indexOf('ch:')===0?0:(k.indexOf('group:')===0?1:2);}
    return Object.keys(by).map(function(k){return by[k];}).sort(function(a,b){if((b.ts||0)!==(a.ts||0))return (b.ts||0)-(a.ts||0);var r=rank(a.key)-rank(b.key);return r||String(a.name).localeCompare(String(b.name));});}
  function openPane(open,key,max){var o=(open||[]).filter(function(k){return k!==key;});o.push(key);while(o.length>max)o.shift();return o;}
  function unreadCount(msgs,key,own,seenTs){return (msgs||[]).filter(function(m){return chatKey(m,own)===key&&m.from!==own&&(Date.parse(m.ts||'')||0)>(seenTs||0);}).length;}
  function needsConfirm(key){return key.indexOf('ch:')===0||key.indexOf('group:')===0;}
  function recipients(nodes,channels,groups,own,q){q=String(q||'').trim().toLowerCase();var out=[];
    (channels||[]).forEach(function(c){if(c.role==='DISABLED')return;out.push({key:'ch:'+c.index,name:c.name||('slot '+c.index),sub:'everyone on the channel',kind:'channel'});});
    (groups||[]).forEach(function(g){out.push({key:'group:'+g.name,name:g.name,sub:(g.count||0)+' device'+(g.count===1?'':'s')+', one message each',kind:'group'});});
    Object.keys(nodes||{}).forEach(function(id){if(id===own)return;var n=nodes[id]||{};out.push({key:'dm:'+id,name:n.name||id,sub:id+(n.db?' · database only':''),kind:'node'});});
    if(q){out=out.filter(function(r){return String(r.name).toLowerCase().indexOf(q)>=0||String(r.sub).toLowerCase().indexOf(q)>=0||r.key.toLowerCase().indexOf(q)>=0;});
      if(/^!?[0-9a-f]{8}$/.test(q)){var id='!'+q.replace(/^!/,'');if(id!==own&&!out.some(function(r){return r.key==='dm:'+id;}))out.push({key:'dm:'+id,name:id,sub:'not in the register',kind:'node',unknown:true});}}
    return out;}
  function copySeen(seen){var s={};Object.keys(seen||{}).forEach(function(k){s[k]=seen[k];});return s;}
  function markRead(seen,key,msgs,own,now){var s=copySeen(seen),newest=0;(msgs||[]).forEach(function(m){if(chatKey(m,own)===key){var t=Date.parse(m.ts||'')||0;if(t>newest)newest=t;}});s[key]=newest||now||Date.now();return s;}
  function markUnread(seen,key,msgs,own){var s=copySeen(seen),last=0;(msgs||[]).forEach(function(m){if(chatKey(m,own)===key&&m.from!==own){var t=Date.parse(m.ts||'')||0;if(t>last)last=t;}});if(last)s[key]=last-1;return s;}
  function sortChats(chats,pins){pins=pins||[];return (chats||[]).slice().sort(function(a,b){var pa=pins.indexOf(a.key)>=0?0:1,pb=pins.indexOf(b.key)>=0?0:1;if(pa!==pb)return pa-pb;return (b.ts||0)-(a.ts||0);});}
  function visibleChats(chats,hidden,showHidden){hidden=hidden||[];return showHidden?(chats||[]).slice():(chats||[]).filter(function(c){return hidden.indexOf(c.key)<0;});}
  function unreadTotal(chats,muted,hidden){muted=muted||[];hidden=hidden||[];var n=0;(chats||[]).forEach(function(c){if(muted.indexOf(c.key)<0&&hidden.indexOf(c.key)<0)n+=(c.unread||0);});return n;}
  function filterChats(chats,msgs,own,q){q=String(q||'').trim().toLowerCase();return (chats||[]).map(function(c){var o={};Object.keys(c).forEach(function(k){o[k]=c[k];});o.hits=0;if(q){(msgs||[]).forEach(function(m){if(chatKey(m,own)===c.key&&String(m.text||'').toLowerCase().indexOf(q)>=0)o.hits++;});}return o;})
    .filter(function(c){return !q||c.hits>0||String(c.name||'').toLowerCase().indexOf(q)>=0||String(c.key).toLowerCase().indexOf(q)>=0;});}
  function firstUnreadIndex(list,own,seenTs){for(var i=0;i<(list||[]).length;i++){var m=list[i];if(m.from!==own&&(Date.parse(m.ts||'')||0)>(seenTs||0))return i;}return -1;}
  function canResend(m,own){return !!m&&m.from===own&&!!m.ack&&m.ack!=='delivered';}
  /* chat:pure:end */
  var msgs=[],open=[],seen={},pins=[],muted=[],hidden=[],showHidden=false,filterQ='',baseTitle=document.title,MAX=function(){return window.innerWidth<=700?1:3;};
  try{seen=JSON.parse(localStorage.getItem('mm-chat-seen')||'{}')||{};open=JSON.parse(localStorage.getItem('mm-chat-open')||'[]')||[];pins=JSON.parse(localStorage.getItem('mm-chat-pins')||'[]')||[];muted=JSON.parse(localStorage.getItem('mm-chat-muted')||'[]')||[];hidden=JSON.parse(localStorage.getItem('mm-chat-hidden')||'[]')||[];}catch(e){}
  function keep(){try{localStorage.setItem('mm-chat-seen',JSON.stringify(seen));localStorage.setItem('mm-chat-open',JSON.stringify(open));localStorage.setItem('mm-chat-pins',JSON.stringify(pins));localStorage.setItem('mm-chat-muted',JSON.stringify(muted));localStorage.setItem('mm-chat-hidden',JSON.stringify(hidden));}catch(e){}}
  function nodeName(id){var n=D.nodes[id];return n?n.name:id;}
  function chatName(c){if(c.key.indexOf('dm:')===0){return nodeName(c.key.slice(3));}return c.name;}
  function chatIcon(c){if(c.key.indexOf('ch:')===0)return D.hash||'';if(c.key.indexOf('group:')===0){var g=(D.groups||[]).filter(function(x){return 'group:'+x.name===c.key;})[0];return D.icons[(g&&g.icon)||'radio']||D.users;}var n=D.nodes[c.key.slice(3)];return D.icons[(n&&n.icon)||'radio']||'';}
  function chatSub(c){if(c.key.indexOf('dm:')===0){var id=c.key.slice(3),n=D.nodes[id];return id+(n&&n.group?' · '+n.group:'')+(n&&n.db?' · database only':'');}return c.sub;}
  function esc(t){var d=document.createElement('div');d.textContent=t==null?'':String(t);return d.innerHTML;}
  function hm(ts){return ts?window.mmHm(ts):'';}
  function dedupe(){var seenK={};msgs=msgs.filter(function(m){var k=(m.mid!==undefined&&m.mid!==null?'m'+m.mid:'')+'|'+(m.ts||'')+'|'+(m.from||'')+'|'+(m.to||'')+'|'+(m.text||'');if(seenK[k])return false;seenK[k]=true;return true;});msgs.sort(function(a,b){return (Date.parse(a.ts||'')||0)-(Date.parse(b.ts||'')||0);});}
  function msgsFor(key){if(key.indexOf('group:')===0){var g=(D.groups||[]).filter(function(x){return 'group:'+x.name===key;})[0];var mem=(g&&g.members)||[];return msgs.filter(function(m){var k=chatKey(m,own);return mem.indexOf(k.slice(3))>=0&&k.indexOf('dm:')===0;});}return msgs.filter(function(m){return chatKey(m,own)===key;});}
  function menuFor(key){var pinned=pins.indexOf(key)>=0,isMuted=muted.indexOf(key)>=0,isHidden=hidden.indexOf(key)>=0;return "<button type='button' data-act='read'>Mark as read</button><button type='button' data-act='unread'>Mark as unread</button><button type='button' data-act='pin'>"+(pinned?'Unpin':'Pin to the top')+"</button><button type='button' data-act='mute'>"+(isMuted?'Unmute':'Mute')+"</button><button type='button' data-act='hide'>"+(isHidden?'Show this chat again':'Hide this chat')+"</button>";}
  function act(key,what){if(what==='read'){seen=markRead(seen,key,msgs,own,Date.now());}
    else if(what==='unread'){seen=markUnread(seen,key,msgs,own);var w=document.querySelector("#chat-panes [data-key='"+key+"']");if(w){open=open.filter(function(k){return k!==key;});w.remove();root.classList.toggle('open',open.length>0);}}
    else if(what==='pin'){pins=pins.indexOf(key)>=0?pins.filter(function(k){return k!==key;}):pins.concat([key]);}
    else if(what==='mute'){muted=muted.indexOf(key)>=0?muted.filter(function(k){return k!==key;}):muted.concat([key]);}
    else if(what==='hide'){if(hidden.indexOf(key)>=0){hidden=hidden.filter(function(k){return k!==key;});}else{hidden.push(key);open=open.filter(function(k){return k!==key;});var w2=document.querySelector("#chat-panes [data-key='"+key+"']");if(w2)w2.remove();root.classList.toggle('open',open.length>0);}}
    keep();renderList();open.forEach(renderPane);}
  var ctx=null;function hideCtx(){if(ctx){ctx.remove();ctx=null;}}
  function showCtx(key,x,y){hideCtx();ctx=document.createElement('div');ctx.className='menu-list ctx';ctx.setAttribute('role','menu');ctx.innerHTML=menuFor(key);document.body.appendChild(ctx);var w=ctx.offsetWidth,h=ctx.offsetHeight;ctx.style.left=Math.max(4,Math.min(x,window.innerWidth-w-4))+'px';ctx.style.top=Math.max(4,Math.min(y,window.innerHeight-h-4))+'px';
    ctx.addEventListener('click',function(ev){var b=ev.target.closest('[data-act]');if(!b)return;var what=b.dataset.act;hideCtx();act(key,what);});}
  function closeMenus(except){document.querySelectorAll('details.chat-menu[open]').forEach(function(d){if(d!==except)d.open=false;});}
  document.addEventListener('click',function(ev){if(ctx&&!ctx.contains(ev.target))hideCtx();closeMenus(ev.target.closest?ev.target.closest('details.chat-menu'):null);},true);document.addEventListener('keydown',function(ev){if(ev.key==='Escape'){hideCtx();closeMenus(null);}});
  function renderList(){var list=document.getElementById('chat-list');var all=chatsFrom(msgs,own,D.channels,D.groups,seen);var total=unreadTotal(all,muted,hidden);
    var tot=document.getElementById('chat-total');if(tot){tot.textContent=total?total+' unread':'';}document.title=(total?'('+total+') ':'')+baseTitle;
    var hb=document.getElementById('chat-hidden');if(hb){hb.hidden=!hidden.length;hb.textContent=(showHidden?'Hide the hidden':'Show hidden')+' ('+hidden.length+')';}
    var chats=sortChats(visibleChats(filterChats(all,msgs,own,filterQ),hidden,showHidden),pins);list.innerHTML='';
    chats.forEach(function(c){var b=document.createElement('button');b.type='button';var isMuted=muted.indexOf(c.key)>=0,isPinned=pins.indexOf(c.key)>=0,isHidden=hidden.indexOf(c.key)>=0;b.className='chat-row'+(open.indexOf(c.key)>=0?' on':'')+(isHidden?' hid':'');b.dataset.key=c.key;
      b.innerHTML="<span class='nodeicon'>"+chatIcon(c)+"</span><span><span class='nm'>"+(isPinned?"<span class='mk' title='Pinned'>"+(D.pin||'')+"</span>":"")+"<span class='nmt'></span></span><span class='last'></span></span><span class='when'><span class='t'></span>"+(isMuted?"<span class='mark' title='Muted'>"+(D.muted||'')+"</span>":(c.unread?"<span class='unread'>"+c.unread+"</span>":""))+"</span>";
      b.querySelector('.nmt').textContent=chatName(c)+(isHidden?' (hidden)':'');b.querySelector('.last').textContent=(filterQ&&c.hits)?(c.hits+' line'+(c.hits===1?'':'s')+' match'):(c.last||chatSub(c));b.querySelector('.t').textContent=hm(c.ts?new Date(c.ts).toISOString():'');
      b.setAttribute('aria-label',chatName(c)+(isPinned?', pinned':'')+(isMuted?', muted':(c.unread?', '+c.unread+' unread':'')));b.addEventListener('click',function(){openChat(c.key);});b.addEventListener('contextmenu',function(ev){ev.preventDefault();showCtx(c.key,ev.clientX,ev.clientY);});list.appendChild(b);});
    if(!chats.length){list.innerHTML="<p class='meta' style='padding:var(--s3)'>"+(filterQ?'Nothing matches.':(hidden.length&&!showHidden?'Every chat is hidden.':'No channel is readable yet.'))+"</p>";}}
  function copyText(t,b){function done(){var was=b.textContent;b.textContent='Copied';setTimeout(function(){b.textContent=was;},1500);}function fallback(){var ta=document.createElement('textarea');ta.value=t;ta.setAttribute('readonly','');ta.style.position='fixed';ta.style.left='-999px';document.body.appendChild(ta);ta.select();try{document.execCommand('copy');done();}catch(e){}ta.remove();}
    if(navigator.clipboard&&navigator.clipboard.writeText){navigator.clipboard.writeText(t).then(done,fallback);}else{fallback();}}
  function sendBody(win,body,confirmText,needs,after){var res=win.querySelector('.res'),btn=win.querySelector('form button[type=submit]');
    function go(){res.textContent='sending';res.className='res meta warn';if(btn)btn.disabled=true;
      fetch('/api/send_text',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}).then(function(r){return r.json().then(function(j){return [r.status,j];});})
        .then(function(x){if(btn)btn.disabled=false;if(x[0]>=400){res.textContent='not sent: '+(x[1].error||x[0]);res.className='res meta bad';return;}res.textContent='';if(after)after();})
        .catch(function(){if(btn)btn.disabled=false;res.textContent=window.mmNoAnswer;res.className='res meta bad';});}
    if(needs){window.mmConfirm(confirmText,win.querySelector('.chat-compose'),go);}else{go();}}
  function openPicker(){var side=root.querySelector('.chat-side');if(!side||side.querySelector('.chat-picker'))return;side.appendChild(document.getElementById('chat-picker').content.cloneNode(true));side.classList.add('picking');
    var pk=side.querySelector('.chat-picker'),inp=pk.querySelector('input'),out=pk.querySelector('.chat-picks');
    function closePicker(){pk.remove();side.classList.remove('picking');}
    function draw(){var rs=recipients(D.nodes,D.channels,D.groups,own,inp.value);out.innerHTML='';rs.slice(0,80).forEach(function(r){var b=document.createElement('button');b.type='button';b.className='chat-row';b.innerHTML="<span class='nodeicon'>"+chatIcon({key:r.key})+"</span><span><span class='nm'></span><span class='last'></span></span><span></span>";b.querySelector('.nm').textContent=r.name;b.querySelector('.last').textContent=r.sub;b.addEventListener('click',function(){closePicker();openChat(r.key);});out.appendChild(b);});
      if(!rs.length){out.innerHTML="<p class='meta' style='padding:var(--s3)'>No one by that name. A full radio id, !ee000099, starts a chat with a radio the box has not heard.</p>";}}
    inp.addEventListener('input',draw);inp.addEventListener('keydown',function(ev){if(ev.key==='Escape'){closePicker();}else if(ev.key==='Enter'){ev.preventDefault();var f=out.querySelector('button');if(f)f.click();}});pk.querySelector('.close').addEventListener('click',closePicker);draw();inp.focus();}
  function receipt(m){if(m.from!==own)return '';if(m.ack==='delivered')return "<span class='pill'>delivered</span>";if(m.ack)return "<span class='pill' data-tip='The radio gave up' data-tip-more='"+esc(m.ack)+"'>not delivered · "+esc(String(m.ack).replace(/_/g,' ').toLowerCase())+"</span>";
    var to=String(m.to||'^all');if(to==='^all'||to==='!ffffffff')return "<span class='pill' data-tip='A message to everyone is never acknowledged'>sent to everyone</span>";return "<span class='pill'>handed to the radio</span>";}
  function renderPane(key){var panes=document.getElementById('chat-panes');var win=panes.querySelector("[data-key='"+key+"']");var chats=chatsFrom(msgs,own,D.channels,D.groups,seen);var c=chats.filter(function(x){return x.key===key;})[0]||{key:key,name:key,sub:''};
    if(!win){win=document.createElement('section');win.className='chat-win';win.dataset.key=key;win.dataset.seenAt=String(seen[key]||0);
      win.innerHTML="<div class='chat-head'><button type='button' class='line icon back' aria-label='Back to the chats' data-tip='Back to the chats'><svg viewBox='0 0 16 16' fill='none' stroke='currentColor' stroke-width='1.6' stroke-linecap='round' stroke-linejoin='round' aria-hidden='true'><path d='M10 3 5 8l5 5'/></svg></button><span class='nodeicon'>"+chatIcon(c)+"</span><span class='nm'><span class='name'></span><br><span class='sub'></span></span><details class='chat-menu'><summary class='line icon' aria-label='More for this chat' data-tip='More for this chat' data-tip-more='Mark read or unread, pin, mute, hide'>"+(D.dots||'&#8943;')+"</summary><div class='menu-list' role='menu'></div></details><button type='button' class='line icon close' aria-label='Close this chat' data-tip='Close this chat'><svg viewBox='0 0 16 16' fill='none' stroke='currentColor' stroke-width='1.6' stroke-linecap='round' aria-hidden='true'><path d='M3.5 3.5l9 9M12.5 3.5l-9 9'/></svg></button></div><div class='chat-msgs'></div>";
      win.appendChild(document.getElementById('chat-composer').content.cloneNode(true));
      win.querySelector('.close').addEventListener('click',function(){open=open.filter(function(k){return k!==key;});keep();win.remove();root.classList.toggle('open',open.length>0);renderList();});
      win.querySelector('.back').addEventListener('click',function(){open=open.filter(function(k){return k!==key;});keep();win.remove();root.classList.remove('open');renderList();});
      win.querySelector('.menu-list').addEventListener('click',function(ev){var b=ev.target.closest('[data-act]');if(!b)return;win.querySelector('details.chat-menu').open=false;act(key,b.dataset.act);});
      var f=win.querySelector('form'),note=win.querySelector('.note');
      note.textContent=key.indexOf('ch:')===0?'Everyone on the channel sees this; it is never acknowledged.':(key.indexOf('group:')===0?'One direct message per device, each with its own receipt.':'Only this radio sees it; the receipt shows on the bubble.');
      win.querySelectorAll('[data-quick]').forEach(function(b){b.addEventListener('click',function(){f.elements.text.value=b.getAttribute('data-quick');f.elements.text.focus();});});
      f.addEventListener('submit',function(ev){ev.preventDefault();ev.stopImmediatePropagation();var text=f.elements.text.value;if(!text.trim())return;
        var body={text:text,channel:0,to:'^all'},confirmText='';
        if(key.indexOf('ch:')===0){body.channel=parseInt(key.slice(3),10);body.to='^all';confirmText=root.dataset.confirmChannel.replace('{channel}',c.name).replace('{count}',String(D.heard||0)).replace('{text}',text);}
        else if(key.indexOf('group:')===0){body.to=key;confirmText=root.dataset.confirmGroup.replace('{group}',c.name).replace('{text}',text);}
        else{body.to=key.slice(3);}
        sendBody(win,body,confirmText,needsConfirm(key),function(){f.elements.text.value='';f.elements.text.focus();});},true);
      win.querySelector('.chat-msgs').addEventListener('click',function(ev){var b=ev.target.closest('[data-act]');if(!b)return;var bub=b.closest('.bubble');var m=msgsFor(key)[parseInt(bub.dataset.i,10)];if(!m)return;
        if(b.dataset.act==='copy'){copyText(m.text||'',b);}else if(b.dataset.act==='resend'){sendBody(win,{text:m.text||'',channel:(m.channel===undefined||m.channel===null)?0:m.channel,to:String(m.to||'^all')},'',false,null);}});
      panes.appendChild(win);}
    win.querySelector('.name').textContent=chatName(c);win.querySelector('.sub').textContent=chatSub(c);win.querySelector('.menu-list').innerHTML=menuFor(key);
    var box=win.querySelector('.chat-msgs');var atBottom=box.scrollTop+box.clientHeight>=box.scrollHeight-40||!box.children.length;box.innerHTML='';var lastDay='';
    var list=msgsFor(key),divAt=firstUnreadIndex(list,own,parseInt(win.dataset.seenAt||'0',10));
    list.forEach(function(m,i){var day=String(m.ts||'').slice(0,10);if(day&&day!==lastDay){lastDay=day;var d=document.createElement('div');d.className='chat-day';d.textContent=day;box.appendChild(d);}
      if(i===divAt){var nd=document.createElement('div');nd.className='chat-day new';nd.textContent='New messages';box.appendChild(nd);}
      var me=m.from===own;var b=document.createElement('div');b.className='bubble '+(me?'me':'them');b.dataset.i=i;b.tabIndex=0;
      b.innerHTML="<div class='who'></div><div class='text'></div><div class='meta'><span class='t'></span>"+receipt(m)+"<button type='button' class='act' data-act='copy'>Copy</button>"+(canResend(m,own)?"<button type='button' class='act' data-act='resend'>Send again</button>":"")+"</div>";
      if(me){b.querySelector('.who').remove();}else{b.querySelector('.who').textContent=nodeName(m.from);}
      if(key.indexOf('group:')===0&&me){var to=String(m.to||'');var w=b.querySelector('.meta');var sp=document.createElement('span');sp.textContent='to '+nodeName(to);w.insertBefore(sp,w.firstChild);}
      b.querySelector('.text').textContent=m.text||'';b.querySelector('.t').textContent=hm(m.ts);box.appendChild(b);});
    if(atBottom){box.scrollTop=box.scrollHeight;}
    var newest=0;list.forEach(function(m){var t=Date.parse(m.ts||'')||0;if(t>newest)newest=t;});if(newest>(seen[key]||0)){seen[key]=newest;keep();renderList();}}
  function renderPanes(){var panes=document.getElementById('chat-panes');Array.prototype.slice.call(panes.children).forEach(function(w){if(open.indexOf(w.dataset.key)<0)w.remove();});open.forEach(renderPane);
    Array.prototype.slice.call(panes.children).sort(function(a,b){return open.indexOf(a.dataset.key)-open.indexOf(b.dataset.key);}).forEach(function(w){panes.appendChild(w);});root.classList.toggle('open',open.length>0);}
  function openChat(key){if(hidden.indexOf(key)>=0){hidden=hidden.filter(function(k){return k!==key;});}open=openPane(open,key,MAX());keep();renderPanes();renderList();var w=document.querySelector("#chat-panes [data-key='"+key+"'] input[name=text]");if(w&&window.innerWidth>700)w.focus();}
  function load(){Promise.all([fetch('/api/messages').then(function(r){return r.json();}),fetch('/api/history?kind=messages&limit=2000').then(function(r){return r.json();}).catch(function(){return {};})]).then(function(x){
    msgs=(x[0].messages||[]).slice();(x[1].rows||[]).forEach(function(r){msgs.push({ts:r.ts,from:r.node,name:r.name||r.node,to:r.dest,channel:r.channel,text:r.text,mid:r.mid,ack:r.ack,sent:r.mid!==null&&r.mid!==undefined});});
    dedupe();try{var q=new URLSearchParams(window.location.search).get('open');if(q){open=q.split(',').map(function(k){return k.trim();}).filter(Boolean);}}catch(e){}
    open=open.filter(function(k){return k.indexOf('ch:')===0||k.indexOf('dm:')===0||k.indexOf('group:')===0;}).slice(-MAX());if(!open.length&&window.innerWidth>700){var first=chatsFrom(msgs,own,D.channels,D.groups,seen)[0];if(first)open=[first.key];}renderList();renderPanes();}).catch(function(){var l=document.getElementById('chat-list');if(l)l.innerHTML="<p class='meta bad' style='padding:var(--s3)'>"+window.mmNoAnswer+"</p>";});}
  window.onMesh=function(d){if(!d)return;if(d.kind==='text'){msgs.push(d);dedupe();renderList();open.forEach(function(k){if(chatKey(d,own)===k||k.indexOf('group:')===0)renderPane(k);});}
    if(d.kind==='ack'){msgs.forEach(function(m){if(m.mid!==undefined&&m.mid!==null&&m.mid===d.request_id){m.ack=d.ok?'delivered':(d.reason||'failed');}});open.forEach(renderPane);}};
  window.addEventListener('resize',function(){if(open.length>MAX()){open=open.slice(-MAX());keep();renderPanes();renderList();}});
  (function(){var nb=document.getElementById('chat-new'),ra=document.getElementById('chat-readall'),fi=document.getElementById('chat-filter'),hb=document.getElementById('chat-hidden');
    if(nb)nb.addEventListener('click',openPicker);
    if(ra)ra.addEventListener('click',function(){chatsFrom(msgs,own,D.channels,D.groups,seen).forEach(function(c){seen=markRead(seen,c.key,msgs,own,Date.now());});keep();renderList();});
    if(fi)fi.addEventListener('input',function(){filterQ=fi.value;renderList();});
    if(hb)hb.addEventListener('click',function(){showHidden=!showHidden;renderList();});})();
  load();
})();
</script>"""


def radio_body(cfg, own_id="?"):
    if not cfg or "long_name" not in cfg:
        return "<p class='warn'>The radio's settings are not readable yet. The bridge reads them when the radio connects; if the strip above says the radio is missing, check the USB cable.</p>"
    v = lambda k: e(str(cfg.get(k) if cfg.get(k) is not None else ""))
    rs, rr = _act("radio_set"), _act("radio_set_region")
    settings = (f"<form class='card' data-action='radio_set' data-risk='change' data-confirm=\"{e(rs['confirm'])}\">"
                f"<h2 style='margin-top:0'>{e(rs['title'])}</h2><p class='meta'>Each is written to the radio and shown here only once the radio has answered with it.</p>"
                f"<label>Long name (39 bytes at most)<input type='text' name='long_name' value='{v('long_name')}' maxlength='39'></label>"
                f"<label>Short name (4 bytes at most)<input type='text' name='short_name' value='{v('short_name')}' maxlength='4'></label>"
                f"<label>Transmit power (dBm)<span class='meta'> 0 is the region's maximum</span><input type='number' name='tx_power' value='{v('tx_power')}' min='0' max='30'></label>"
                f"<label>Position every (seconds)<input type='number' name='position_broadcast_secs' value='{v('position_broadcast_secs')}' min='32' max='86400'></label>"
                "<button type='submit'>Write to the radio</button><div class='res meta' role='status'></div></form>")
    def sel(name, values, cur):
        return f"<select name='{name}'><option value=''>leave as is ({e(str(cur))})</option>" + "".join(f"<option value='{x}'>{x}</option>" for x in values) + "</select>"
    ins = {i["name"]: i for i in rr["inputs"]}
    region = (f"<form class='card danger' data-action='radio_set_region' data-risk='unreachable' data-confirm=\"{e(rr['confirm'])}\">"
              f"<h2 style='margin-top:0'>{e(rr['title'])}</h2><p class='meta'>{e(rr['description'])}</p>"
              f"<label>Region{sel('region', ins['region']['values'], cfg.get('region'))}</label>"
              f"<label>Modem preset{sel('modem_preset', ins['modem_preset']['values'], cfg.get('modem_preset'))}</label>"
              f"<label>Role{sel('role', ins['role']['values'], cfg.get('role'))}</label>"
              f"<label class='check'><input type='checkbox' name='confirm_tick'><span>I understand: changing the region or preset moves this radio to another band; a fleet on the old setting will not hear it, and the radio reboots. This radio is {e(own_id)}.</span></label>"
              "<button type='submit' class='danger'>Write and reboot the radio</button><div class='res meta' role='status'></div></form>")
    return f"{read_line(cfg, '/radio')}<div class='cards' id='radio-cards'>{settings}{region}</div>{WRITE_JS}"


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



def peers_section(p):
    """Spec 052: this site, its peers, an invite and a join, on the Connections page."""
    p = p or {}
    if p.get("error"):
        return f"<h2 id='peers'>Peers</h2><p class='meta bad'>{e(str(p['error']))}</p>"
    site = p.get("site") or {}
    listening = (f"listening on port {e(str(site.get('port')))}" if site.get("listening") else "not listening: peers cannot join this site until the installer's --peer-bind is set")
    head = (f"<h2 id='peers'>Peers</h2><p class='meta'>Mesh Managers joined to this one over the internet (ADR 003). Each side sends its picture, nodes, positions and battery, and shows the other's, marked with where it came from. Nothing goes on the air, and channel keys never cross.</p>"
            f"<div class='cards'>{card('This site', e(str(site.get('name') or '?')) + ' <span class=pill>' + e(str(site.get('short') or '')) + '</span><div class=meta>' + e(str(site.get('address') or 'no address set: the invite names this machine by its hostname')) + ' · ' + e(listening) + '</div>', 'ok' if site.get('listening') else '')}</div>")
    rows = ""
    for q in p.get("peers") or []:
        lamp = "ok" if q.get("state") == "connected" else "warn"
        note = f" <span class='meta'>{e(str(q.get('note')))}</span>" if q.get("note") else ""
        rows += (f"<tr><td><i class='lamp lamp--{lamp}'></i> {e(str(q.get('name')))}<div class='meta'>{e(str(q.get('id') or '')[:12])} · {e(str(q.get('direction') or ''))}</div></td>"
                 f"<td>{e(str(q.get('state')))}{note}</td><td class='meta'>{('<time datetime=' + chr(39) + e(str(q.get('last_seen'))) + chr(39) + ' data-age>' + e(age(q.get('last_seen'))) + '</time>') if q.get('last_seen') else 'never'}</td>"
                 f"<td>{int(q.get('nodes') or 0)}</td><td><form data-action='peer_forget' data-risk='change' data-confirm='Forget {e(str(q.get('name')))}: its pin, its link and its picture leave this site.' style='display:inline'><input type='hidden' name='site' value='{e(str(q.get('id')))}'><button class='danger line'>Forget</button><div class='res meta' role='status'></div></form></td></tr>")
    table = (f"<div class='tablewrap'><table><thead><tr><th>Peer</th><th>State</th><th>Last seen</th><th>Nodes</th><th></th></tr></thead><tbody>{rows or '<tr><td colspan=5 class=meta>No peers yet. Invite one from here, or join another site with its invite.</td></tr>'}</tbody></table></div>")
    a_inv, a_join = _act("peer_invite"), _act("peer_join")
    invite = (f"<form data-action='peer_invite' class='card' data-risk='change' data-confirm=\"{e(a_inv.get('confirm') or '')}\" id='peer-invite'><h3 style='margin-top:0'>{e(a_inv['title'])}</h3><p class='meta'>{e(a_inv['description'])}</p>"
              f"<button class='line'{'' if site.get('listening') else ' disabled'}>Invite a peer</button><div class='res meta' role='status'></div><div class='invite-out' aria-live='polite'></div></form>")
    join = (f"<form data-action='peer_join' class='card' data-risk='change' data-confirm=\"{e(a_join.get('confirm') or '')}\" id='peer-join'><h3 style='margin-top:0'>{e(a_join['title'])}</h3><p class='meta'>{e(a_join['description'])}</p>"
            f"<label>Invite<input type='text' name='invite' required placeholder='host:port/code/fingerprint' autocomplete='off' spellcheck='false'></label><button class='line'>Join</button><div class='res meta' role='status'></div></form>")
    js = ("<script>document.addEventListener('mm-written',function(ev){var d=ev.detail||{},a=d.action,r=d.result||{};"
          "if(a==='peer_invite'){var o=document.querySelector('#peer-invite .invite-out');if(!o)return;o.innerHTML='';var pre=document.createElement('pre');pre.className='fleet-out';pre.style.userSelect='all';pre.textContent=r.invite||'';o.appendChild(pre);"
          "var m=document.createElement('p');m.className='meta';m.textContent=(r.note||'')+(r.expires?' · expires '+r.expires:'');o.appendChild(m);if(r.qr_svg){var q=document.createElement('div');q.style.maxWidth='240px';q.innerHTML=r.qr_svg;o.appendChild(q);}}"
          "if(a==='peer_join'||a==='peer_forget'){setTimeout(function(){fetch('/connections').then(function(r){return r.text();}).then(function(h){var d=new DOMParser().parseFromString(h,'text/html');var n=d.getElementById('peers-section'),o=document.getElementById('peers-section');if(n&&o){o.replaceWith(n);}});},1200);}});</script>")
    return f"<section id='peers-section'>{head}{table}<div class='cards'>{invite}{join}</div>{js}</section>"


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
    rows = rows or "<tr><td colspan=5 class='meta'>No connections yet. Add one below to let an agent read this mesh.</td></tr>"
    shown = ""
    if minted:
        cmd = f"claude mcp add mesh-manager --transport http http://{web.bind[0]}:{web.bind[1]}/mcp --header \"Authorization: Bearer {minted['token']}\""
        shown = (f"<div class='card' style='border-color:var(--gold)'><div class='k'>Token for {e(minted['name'])} ({e(minted['autonomy'])}), shown once</div>"
                 f"<div class='v'><code>{e(minted['token'])}</code></div><div class='row-actions' style='margin:.5rem 0'><button type='button' class='line' data-copy='{e(minted['token'])}'>Copy the token</button>"
                 f"<button type='button' class='line' data-copy='{e(cmd)}'>Copy the claude mcp add command</button></div><p class='meta'>Connect with: <code>{e(cmd)}</code></p></div>")
    form = ("<form method='post' action='/connections' class='card' id='mint'><h2 style='margin-top:0'>Add a connection</h2>"
            "<label>Name<input type='text' name='name' required maxlength='40'></label><input type='hidden' name='confirm' value=''>"
            "<label>Autonomy<select name='autonomy'><option value='observe'>observe: reads only</option><option value='propose' selected>propose: reads, on-air requests, and proposals for the rest</option><option value='act'>act: everything the screen can do</option></select></label>"
            "<button type='submit'>Add</button></form>")
    js = r"""<script>
(function(){
  function gate(f,text,then){if(f.dataset.ok==='1'){f.dataset.ok='';return true;}window.mmConfirm(text,f,function(){if(then)then();f.dataset.ok='1';f.requestSubmit?f.requestSubmit():f.submit();});return false;}
  document.querySelectorAll('form[data-name]').forEach(function(f){f.addEventListener('submit',function(ev){var a=f.elements.autonomy.value;
    if(!gate(f,'Change '+f.dataset.name+' to '+a+'? '+(a==='act'?'At act it does everything this screen can do, without asking each time.':a==='propose'?'At propose it reads, asks on air, and queues the rest for you.':'At observe it only reads.'),function(){f.elements.confirm.value=f.dataset.name;})){ev.preventDefault();}});});
  document.querySelectorAll('form[data-revoke]').forEach(function(f){f.addEventListener('submit',function(ev){if(!gate(f,'Revoke '+f.dataset.revoke+'? Its token stops working now.')){ev.preventDefault();}});});
  var m=document.getElementById('mint');m.addEventListener('submit',function(ev){if(m.elements.autonomy.value==='act'){var n=m.elements.name.value;
    if(!gate(m,'Add '+n+' at act? It will do everything this screen can do, without asking each time.',function(){m.elements.confirm.value=n;})){ev.preventDefault();}}});
})();
</script>"""
    return (f"{('<p class=bad>' + e(msg) + '</p>') if msg else ''}{shown}<div class='tablewrap'><table><thead><tr><th>Name</th><th>Autonomy</th><th>Created</th><th>Last used</th><th></th></tr></thead><tbody>{rows}</tbody></table></div><br>{form}"
            "<p class='meta'>The autonomy dial is yours: observe looks and reports; propose prepares and asks; act does deterministic work without asking each time. Every call is audited under the connection's name on the Activity page.</p>"
            f"{js}{WRITE_JS}")


def settings_body(web, saved=""):
    try:
        ctx = open(os.path.join(web.etc_dir, "context.md")).read()
    except OSError:
        ctx = ""
    saved = str(saved or "")
    brief_ok = f"<p class='ok'>Saved the brief at {e(hhmm())}.</p>" if saved == "brief" else ""
    return (f"<form method='post' action='/settings' class='card' style='max-width:none'><h2 style='margin-top:0'>Standing brief for connected agents</h2>"
            "<p class='meta'>What this mesh is for, who runs it, the region and channel policy, standing orders. Served verbatim to every connected agent as <code>mesh_context</code>; nothing in the product knows your fleet, this is where it learns it.</p>"
            f"<textarea name='context' rows='12' style='font:14px var(--mono)'>{e(ctx)}</textarea>"
            f"<div style='margin-top:.6rem'><button type='submit'>Save the brief</button>{brief_ok}</div></form>"
            + position_settings(web)
            + quick_settings(web, saved == "quick")
            + update_settings(web, saved == "update"))


def position_settings(web):
    """Where this box is, set on the screen: the map's centre when there is no receiver (5 Sep 2026 reviews:
    a box with no position lost the map, and the only remedy was a flag on an installer already run)."""
    a = _act("box_position_set")
    return (f"<form data-action='box_position_set' class='card' id='position' data-risk='change' data-confirm=\"{e(a.get('confirm') or '')}\" style='margin-top:1rem'><h2 style='margin-top:0'>Where this box is</h2>"
            f"<p class='meta'>{e(a.get('description') or '')}</p>"
            "<div class='regform' style='grid-template-columns:1fr 1fr'><label>Latitude<input type='text' name='lat' inputmode='decimal' placeholder='51.5000'></label><label>Longitude<input type='text' name='lon' inputmode='decimal' placeholder='-0.1200'></label></div>"
            "<div class='row-actions' style='margin:var(--s2) 0'><button type='button' class='line' id='pos-from-radio' data-tip='Take it from the radio&#39;s fix' data-tip-more='Fills the fields from where the box believes it is now'>Take it from the radio's fix</button><span class='meta' id='pos-note'></span></div>"
            "<label class='check'><input type='checkbox' name='clear' value='on'><span>Clear the declared position instead, and let the receivers and the devices place the box.</span></label>"
            "<button type='submit'>Save where this box is</button><div class='res meta' role='status'></div></form>"
            "<script>(function(){var b=document.getElementById('pos-from-radio');if(!b)return;b.addEventListener('click',function(){var n=document.getElementById('pos-note');n.textContent='asking the box';"
            "fetch('/api/links').then(function(r){return r.json();}).then(function(j){var o=j.own||{};if(o.lat===null||o.lat===undefined){n.textContent='the box has no position to take: no receiver fix, no declaration, nothing heard with a fix';return;}"
            "var f=document.getElementById('position');f.elements.lat.value=Number(o.lat).toFixed(5);f.elements.lon.value=Number(o.lon).toFixed(5);n.textContent='from '+(o.position_source||'the box')+'; press Save to keep it';}).catch(function(){n.textContent=window.mmNoAnswer;});});})();</script>")


def quick_settings(web, saved=False):
    """Spec 038: the preset messages, one per line."""
    msgs = quick_load(web.etc_dir)
    err = getattr(web, "_quick_err", "")
    return (f"<form method='post' action='/settings/quick' class='card' style='margin-top:1rem'><h2 style='margin-top:0'>Quick messages</h2>"
            "<p class='meta'>Up to eight, one per line, each 200 bytes at most. The Messages page offers them as buttons; a press fills the field and nothing is sent without the usual confirm.</p>"
            f"{'<p class=bad>' + e(err) + '</p>' if err else ''}"
            f"<textarea name='quick' rows='6' style='font:14px var(--mono)'>{e(chr(10).join(msgs))}</textarea>"
            f"<div style='margin-top:.6rem'><button type='submit'>Save the quick messages</button>{('<p class=ok>Saved the quick messages at ' + e(hhmm()) + '.</p>') if saved else ''}</div></form>")


def update_settings(web, saved=False):
    tok = bool(web.github_token())
    mode = web.update_mode()
    return (f"<form method='post' action='/settings/update' class='card' style='margin-top:1rem'><h2 style='margin-top:0'>Updates from GitHub</h2>"
            "<p class='meta'>The box reads releases of the repository with a fine-grained personal access token limited to that repository, contents read-only. It is kept on the box at 0600 and never shown again. "
            f"{'A token is on the box.' if tok else 'No token yet: updates cannot be checked until one is entered.'}</p>"
            "<label>GitHub token (write only)<input type='password' name='token' autocomplete='off' placeholder='github_pat_…'></label>"
            "<label>Mode<select name='mode'>" + "".join(f"<option value='{m}'{' selected' if m == mode else ''}>{m}: {d}</option>" for m, d in (("manual", "check daily, install on your press"), ("auto", "check daily and install on its own"), ("off", "never talk to GitHub"))) + "</select></label>"
            f"<button type='submit'>Save the update settings</button>{('<p class=ok>Saved the update settings at ' + e(hhmm()) + '.</p>') if saved else ''}</form>")


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

        def _members(self, group):
            """Spec 044: the ids in a group, from the nodes the bridge knows; None when no group is asked for."""
            group = (group or "").strip()
            if not group:
                return None
            return {n.get("id") for n in (self._ask("nodes").get("nodes") or []) if str(n.get("group") or "") == group}

        def _static(self, path):
            rel = os.path.normpath(urllib.parse.unquote(path[len("/static/"):]))
            fp = os.path.normpath(os.path.join(STATIC_DIR, rel))
            if rel.startswith("..") or os.path.isabs(rel) or not fp.startswith(STATIC_DIR + os.sep) or not os.path.isfile(fp):
                return self._send(404, "no such file", "text/plain")
            ctype = {".js": "text/javascript", ".css": "text/css", ".png": "image/png", ".svg": "image/svg+xml"}.get(os.path.splitext(fp)[1], "text/plain")
            with open(fp, "rb") as fh:
                return self._send(200, fh.read(), ctype + ("; charset=utf-8" if ctype.startswith("text") else ""), {"Cache-Control": "public, max-age=86400"})

        def _links(self):
            L = self._ask("links")
            if "nodes" not in L:          # an older bridge: the plain node list, no links, no routes
                L = {"own": {}, "nodes": self._ask("nodes").get("nodes", []), "routes": {}}
            return L

        # -- routes
        def _page(self, title, body, active="", own="", st=None, head=""):
            open_note = "" if (web.auth_on or web.bind[0] in ("127.0.0.1", "localhost", "::1")) else f"open on {web.bind[0]}:{web.bind[1]}, no sign-in"
            return page(title, body, active, own=own, st=st if st is not None else self._ask("status"), pending=len(K.proposals(web.etc_dir)), head=head, update=web.update_available(), notice=open_note)

        def do_GET(self):
            path = self.path.split("?")[0]
            if path == "/healthz":
                return self._json(200, {"ok": True, "bridge": web.client.reachable(), "version": __version__})
            if path == "/login":
                if not web.auth_on:
                    return self._redirect("/")
                return self._send(200, page("Sign in", login_body()))
            if path == "/manifest.webmanifest":
                # Spec 046: what makes the screen installable; nothing in it is secret, so it answers before sign-in,
                # as the icons do, because the phone fetches both while it installs
                return self._send(200, json.dumps(APP_MANIFEST), "application/manifest+json", {"Cache-Control": "public, max-age=3600"})
            if path.startswith("/static/icons/"):
                return self._static(path)
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
                                                                   tilesets_dir(web.config), tak_on=(self._ask("status") or {}).get("tak") != "off"), "/map", head=MAP_HEAD))
            if path == "/map/full":
                js = "<script>window.onMesh=function(d){if(d.kind==='packet'||d.kind==='route'||d.kind==='status'||d.kind==='forwarded'){window.mmFrag('map','map-box');if(window.mmOverlay){window.mmOverlay();}}};</script>"
                return self._send(200, bare_page("Map", mesh_views(self._links(), tile_sources(web.config, web.etc_dir), 800, bare=True, tak_on=(self._ask("status") or {}).get("tak") != "off") + js, head=MAP_HEAD))
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
                return self._static(path)
            if path == "/export/inventory.csv":
                import csv as _csv, io as _io
                cols = ["id", "name", "hw", "firmware", "fingerprint", "key_since", "key_changed", "key_ack", "managed", "behind", "behind_reason", "confirmed", "heard"]
                out = _io.StringIO(); w = _csv.writer(out, lineterminator="\n"); w.writerow(cols)
                for r in (self._ask("inventory").get("rows") or []):
                    w.writerow(["" if r.get(c) is None else r.get(c) for c in cols])
                data = out.getvalue().encode("utf-8")
                self.send_response(200); self.send_header("Content-Type", "text/csv; charset=utf-8"); self.send_header("Content-Disposition", f"attachment; filename=\"mesh-inventory-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.csv\"")
                self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data); return
            if path.startswith("/export/"):
                m = re.fullmatch(r"/export/([a-z]+)\.([a-z]+)", path)
                if not m or m.group(1) not in EXPORT_KINDS or m.group(2) not in EXPORT_KINDS[m.group(1)]:
                    return self._json(404, {"error": "export kinds: " + ", ".join(f"{k}.{'|'.join(v)}" for k, v in EXPORT_KINDS.items())})
                kind, fmt = m.group(1), m.group(2)
                q = urllib.parse.parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
                try:
                    hours = max(1, min(int(q.get("hours", ["24"])[0]), 24 * 30))
                except ValueError:
                    hours = 24
                node = (q.get("node", [""])[0] or "").strip() or None
                since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - hours * 3600))
                rows = self._ask("history", kind=kind, node=node, since=since, limit=5000).get("rows") or []
                members = self._members(q.get("group", [""])[0])
                if members is not None:
                    rows = [r for r in rows if r.get("node") in members]
                names = {n.get("id"): (n.get("label") or n.get("name") or n.get("id")) for n in (self._links().get("nodes") or [])}
                data = export_gpx(rows, names) if fmt == "gpx" else (export_kml(rows, names) if fmt == "kml" else export_csv(rows))
                ctype = {"gpx": "application/gpx+xml", "kml": "application/vnd.google-earth.kml+xml", "csv": "text/csv; charset=utf-8"}[fmt]
                fn = f"mesh-{kind}-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.{fmt}"
                self.send_response(200); self.send_header("Content-Type", ctype); self.send_header("Content-Disposition", f"attachment; filename=\"{fn}\"")
                self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data); return
            if path == "/packets":
                q = urllib.parse.parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
                try:
                    hours = int(q.get("hours", ["24"])[0])
                except ValueError:
                    hours = 24
                hours = hours if hours in (1, 6, 24, 48, 168) else 24
                node = (q.get("node", [""])[0] or "").strip(); port = (q.get("port", [""])[0] or "").strip()
                since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - hours * 3600))
                rows = self._ask("history", kind="packets", since=since, limit=5000).get("rows") or []
                labels = {str(n.get("id")): str(n.get("label") or n.get("name") or n.get("id")) for n in (self._links().get("nodes") or [])}
                for r in (self._ask("register").get("rows") or []):
                    if r.get("label"):
                        labels[str(r.get("id"))] = str(r["label"])
                return self._send(200, self._page("Packets", packets_body(rows, hours, node, port, labels), "/packets"))
            if path == "/graph":
                q = urllib.parse.parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
                try:
                    hours = int(q.get("hours", ["24"])[0])
                except ValueError:
                    hours = 24
                hours = hours if hours in (1, 6, 24, 168) else 24
                return self._send(200, self._page("Neighbours", graph_body(self._ask("neighbors", hours=hours), hours), "/graph"))
            if path == "/api/trails":
                # Spec 040: the positions in the window, the rows playback and the trails consume
                q = urllib.parse.parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
                try:
                    hours = max(0.25, min(float(q.get("hours", ["3"])[0]), 24 * 30))
                except ValueError:
                    hours = 3.0
                since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - hours * 3600))
                rows = self._ask("history", kind="positions", since=since, limit=5000).get("rows") or []
                members = self._members(q.get("group", [""])[0])
                if members is not None:
                    rows = [r for r in rows if r.get("node") in members]
                return self._json(200, {"hours": hours, "since": since, "rows": [{"ts": r.get("ts"), "node": r.get("node"), "lat": r.get("lat"), "lon": r.get("lon"), "snr": r.get("snr")} for r in rows]})
            if path == "/api/waypoints":
                return self._json(200, self._ask("waypoints"))
            if path == "/api/fences":
                return self._json(200, self._ask("fences"))
            if path == "/api/neighbors":
                q = urllib.parse.parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
                nb = self._ask("neighbors", hours=(q.get("hours", ["24"])[0]))
                members = self._members(q.get("group", [""])[0])
                if members is not None and isinstance(nb, dict):
                    nb = dict(nb, edges=[x for x in (nb.get("edges") or []) if x.get("from") in members and x.get("to") in members])
                return self._json(200, nb)
            if path == "/nodes":
                L = self._links()
                q = urllib.parse.parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
                want_group = (q.get("group", [""])[0] or "").strip()
                nodes_ = [n for n in (L.get("nodes") or []) if not want_group or str(n.get("group") or "") == want_group]
                groups_ = sorted({str(g.get("name")) for g in (self._ask("groups").get("groups") or []) if g.get("name")} | {str(n.get("group")) for n in (L.get("nodes") or []) if n.get("group")})
                return self._send(200, self._page("Nodes", nodes_body(nodes_, routes=L.get("routes"), silent_min=_silent_min(web), groups=groups_) + "<script>window.onMesh=function(d){if(d.kind==='packet'||d.kind==='forwarded'||d.kind==='status'){window.mmNodes();}if(d.kind==='route'&&window.mmRoute){window.mmRoute(d);}if(d.kind==='position'&&window.mmPosition){window.mmPosition(d);}if(d.kind==='telemetry'&&window.mmTelemetry){window.mmTelemetry(d);}};</script>", "/nodes"))
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
                seed_messages(web)
                return self._send(200, self._page("Messages", messages_body(web, self._ask("nodes").get("nodes", []), self._ask("channels").get("channels", []), st, groups=self._ask("groups")), "/messages", st=st))
            if path == "/radio":
                st = self._ask("status")
                own = (st.get("own") or {}).get("id") or "?"
                return self._send(200, self._page("This radio", radio_body(self._ask("config"), own), "/radio", own=own, st=st))
            if path == "/node":
                q = urllib.parse.parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
                nid = (q.get("id", [""])[0] or "").strip()
                if not re.fullmatch(r"![0-9a-f]{8}", nid):
                    return self._send(404, self._page("Not found", "<p class='meta'>No such node. It may have been forgotten, or the id is wrong. The <a href='/nodes'>Nodes</a> page lists what this radio hears.</p>"))
                try:
                    hours = int(q.get("hours", ["24"])[0])
                except ValueError:
                    hours = 24
                hours = hours if hours in (24, 168) else 24
                node = next((n for n in (self._links().get("nodes") or []) if n.get("id") == nid), None)
                if not node:
                    return self._send(404, self._page("Not found", "<p class='meta'>No such node. It may have been forgotten, or the id is wrong. The <a href='/nodes'>Nodes</a> page lists what this radio hears.</p>"))
                since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - hours * 3600))
                tel = self._ask("history", kind="telemetry", node=nid, since=since, limit=2000).get("rows") or []
                msgs = self._ask("history", kind="messages", node=nid, since=since, limit=200).get("rows") or []
                npos = len(self._ask("history", kind="positions", node=nid, since=since, limit=5000).get("rows") or [])
                env = self._ask("history", kind="environment", node=nid, since=since, limit=2000).get("rows") or []
                av = next((r for r in (self._ask("availability", hours=hours).get("nodes") or []) if r.get("id") == nid), None)
                return self._send(200, self._page(dname(node), node_body(node, tel, msgs, npos, hours, env=env, availability=av), "/nodes"))
            if path == "/health":
                q = urllib.parse.parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
                al = self._ask("alerts")
                members = self._members(q.get("group", [""])[0])
                if members is not None and isinstance(al, dict):
                    al = dict(al, open=[o for o in (al.get("open") or []) if o.get("node") in members], recent=[r for r in (al.get("recent") or []) if r.get("node") in members])
                return self._send(200, self._page("Health", health_body(self._ask("health", hours=24), al, tak_on=(self._ask("status") or {}).get("tak") != "off"), "/health"))
            if path == "/fragment/health":
                return self._send(200, health_cards(self._ask("health", hours=24)), "text/html; charset=utf-8")
            if path == "/fragment/drift":
                return self._send(200, drift_section(self._ask("drift")), "text/html; charset=utf-8")
            if path == "/fragment/rotation":
                return self._send(200, rotation_section(self._ask("rotation_status")), "text/html; charset=utf-8")
            if path == "/fragment/alerts":
                return self._send(200, alerts_section(self._ask("alerts"), tak_on=(self._ask("status") or {}).get("tak") != "off"), "text/html; charset=utf-8")
            if path == "/register":
                av = {r.get("id"): r for r in (self._ask("availability", hours=24).get("nodes") or [])}
                return self._send(200, self._page("Register", register_body(self._ask("register"), drift=self._ask("drift"), availability=av, inv=self._ask("inventory"), groups=self._ask("groups")), "/register"))
            if path == "/bench":
                return self._send(200, self._page("Bench", bench_body(self._ask("bench_devices"), self._ask("firmware_shelf")), "/bench"))
            if path == "/activity":
                return self._send(200, self._page("Activity", activity_body(web), "/activity"))
            if path == "/connections":
                return self._send(200, self._page("Connections", connections_body(web) + peers_section(self._ask("peers")), "/connections"))
            if path == "/settings":
                return self._send(200, self._page("Settings", settings_body(web) + WRITE_JS, "/settings"))
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
                if name == "groups":
                    return self._send(200, groups_section(self._ask("groups")), "text/html; charset=utf-8")
                if name == "register":
                    return self._send(200, register_rows(self._ask("register"), {r.get("id"): r for r in (self._ask("availability", hours=24).get("nodes") or [])}, self._ask("inventory")))
                if name == "bench":
                    return self._send(200, bench_cards(self._ask("bench_devices"), self._ask("firmware_shelf")))
                if name.startswith("route/"):
                    nid = urllib.parse.unquote(name[len("route/"):])
                    return self._send(200, route_bar(self._ask("route", id=nid).get("route")))
                return self._send(404, "", "text/plain")
            if path == "/channels/qr.png":
                q = urllib.parse.parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
                idx = (q.get("index", [""])[0] or "").strip()
                if idx and idx != "0":
                    # one channel alone, for a device joining a secondary channel; the bridge builds it, the key never crosses the API
                    url = self._ask("channel_url", index=int(idx)).get("url") if idx.isdigit() and int(idx) < 8 else None
                else:
                    url = self._ask("channels").get("url")
                if not url:
                    return self._send(404, "no channel url", "text/plain")
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
                    if not U.is_available(rec) or (want and want != rec.get("version")):
                        rec = U.check(web.config, web.github_token(), web.state_dir, api=web.config.get("UPDATE_API"))
                    if not U.is_available(rec):
                        return self._json(400, {"error": rec.get("error") or "nothing newer to apply", "running": __version__})
                    d = U.download(rec, web.github_token(), web.state_dir)
                    if not d.get("ready"):
                        K.audit(web.etc_dir, who="operator", event="update-refused", version=rec.get("version"), error=d.get("error"))
                        return self._json(400, {"error": d.get("error"), "running": __version__})
                    U.prune_staged(web.state_dir, keep=5, running=__version__, arch=web.arch)
                    a = U.apply(web.state_dir, rec["version"])
                    K.audit(web.etc_dir, who="operator", event="update-apply", version=rec.get("version"), started=a.get("started"), error=a.get("error"))
                    return self._json(200 if a.get("started") else 500, dict(a, running=__version__))
                if path == "/settings/update":
                    tok = str(body.get("token") or "").strip()
                    if tok:
                        web.set_github_token(tok)
                        K.audit(web.etc_dir, who="operator", event="github-token-set")
                    web.set_update_mode(str(body.get("mode") or "manual"))
                    return self._send(200, self._page("Settings", settings_body(web, saved="update"), "/settings"))
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
                if path == "/settings/quick":
                    out, err = quick_save(web.etc_dir, [ln for ln in str(body.get("quick", "")).splitlines()])
                    web._quick_err = err or ""
                    if not err:
                        K.audit(web.etc_dir, who="operator", event="quick-saved", count=len(out))
                    return self._send(400 if err else 200, self._page("Settings", settings_body(web, saved="" if err else "quick"), "/settings"))
                if path == "/settings":
                    ctx = str(body.get("context", ""))[:20000]
                    with open(os.path.join(web.etc_dir, "context.md"), "w") as fh:
                        fh.write(ctx)
                    K.audit(web.etc_dir, who="operator", event="context-saved", bytes=len(ctx.encode()))
                    return self._send(200, self._page("Settings", settings_body(web, saved="brief"), "/settings"))
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
