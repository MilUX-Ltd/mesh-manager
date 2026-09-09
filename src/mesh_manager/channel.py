"""Spec 060: the channel between the bridge and the screen. On Linux and macOS it is the Unix socket it has
always been, owned by the box and unreachable from the network. Windows Python has no Unix socket, so there the
bridge listens on the loopback interface on a port the operating system picks and writes the address and a
one-run token to the same path, and the screen reads that file and proves the token before it asks anything.
Nothing about this reaches beyond the machine. Part of Mesh Manager, GPL-3.0-or-later."""
import json
import os
import socket
import socketserver
import stat
import secrets
import sys

HAVE_UNIX = hasattr(socket, "AF_UNIX")


def use_tcp():
    """The loopback channel is used on Windows, where there is no Unix socket at all, or when asked for (the
    suites ask). Windows Python does define AF_UNIX, so the constant is not the test: a Windows runner proved
    that on 5 Sep 2026. A person who asks for the socket by name gets it and owns the outcome."""
    forced = str(os.environ.get("MESH_MANAGER_CHANNEL") or "").strip().lower()
    if forced in ("tcp", "loopback"):
        return True
    if forced in ("unix", "socket"):
        return False
    return (not HAVE_UNIX) or sys.platform.startswith("win")


def _rendezvous(path):
    with open(path) as fh:
        d = json.load(fh)
    host, port = str(d["addr"]).rsplit(":", 1)
    return host, int(port), str(d.get("token") or "")


def listen(path, handler_cls):
    """The bridge's end. Returns a server whose `expect_token` says whether a caller must prove itself first."""
    d = os.path.dirname(path)
    if d and not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)
    if os.path.exists(path):
        os.unlink(path)
    if not use_tcp():
        class UnixServer(socketserver.ThreadingUnixStreamServer):
            daemon_threads = True
            allow_reuse_address = True
            expect_token = None
        srv = UnixServer(path, handler_cls)
        try:
            os.chmod(path, 0o660)
        except OSError:
            pass
        return srv

    token = secrets.token_hex(16)

    class LoopbackServer(socketserver.ThreadingTCPServer):
        daemon_threads = True
        allow_reuse_address = True
        expect_token = token
    srv = LoopbackServer(("127.0.0.1", 0), handler_cls)
    host, port = srv.server_address[0], srv.server_address[1]
    tmp = path + ".new"
    with open(tmp, "w") as fh:
        json.dump({"addr": f"{host}:{port}", "token": token, "pid": os.getpid()}, fh)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)
    return srv


def connect(path, timeout=5):
    """The screen's end: a connected socket, its caller already proven where the channel asks for it."""
    if not use_tcp() and HAVE_UNIX:
        try:
            if stat.S_ISSOCK(os.stat(path).st_mode):
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.settimeout(timeout)
                s.connect(path)
                return s
        except FileNotFoundError:
            raise
        except OSError:
            pass
    host, port, token = _rendezvous(path)
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    s.connect((host, port))
    s.sendall((json.dumps({"hello": token}) + "\n").encode())
    return s


def check_hello(server, rfile):
    """The bridge's side of the same handshake: True when this caller may go on."""
    token = getattr(server, "expect_token", None)
    if not token:
        return True
    line = rfile.readline(4096)
    try:
        said = json.loads(line.decode("utf-8", "replace")).get("hello")
    except Exception:  # noqa: BLE001
        return False
    return secrets.compare_digest(str(said or ""), str(token))


def where(path):
    """What to tell a person about the channel, one line."""
    if use_tcp():
        try:
            host, port, _ = _rendezvous(path)
            return f"{host}:{port} (loopback, one-run token)"
        except Exception:  # noqa: BLE001
            return "loopback, one-run token"
    return path

def listen_raw(path):
    """A plain listening socket for a loop that accepts for itself (the demo bridge), and the token a caller
    must say first, or None on a Unix socket."""
    d = os.path.dirname(path)
    if d and not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)
    if os.path.exists(path):
        os.unlink(path)
    if not use_tcp():
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(path); srv.listen(8)
        try:
            os.chmod(path, 0o660)
        except OSError:
            pass
        return srv, None
    token = secrets.token_hex(16)
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0)); srv.listen(8)
    host, port = srv.getsockname()
    tmp = path + ".new"
    with open(tmp, "w") as fh:
        json.dump({"addr": f"{host}:{port}", "token": token, "pid": os.getpid()}, fh)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)
    return srv, token


def said_hello(rfile, token):
    """True when a caller on the loopback channel proved itself; always true where there is no token."""
    if not token:
        return True
    try:
        return secrets.compare_digest(str(json.loads(rfile.readline(4096).decode("utf-8", "replace")).get("hello") or ""), str(token))
    except Exception:  # noqa: BLE001
        return False
