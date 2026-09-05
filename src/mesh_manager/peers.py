"""Spec 052 (ADR 003): sites, pairing and the link between Mesh Managers.

A site is one Mesh Manager: an EC P-256 key and a self-signed certificate made at first start, the site id
the SHA-256 of the certificate's public key. Two sites join by an invite: a one-time code read off the
listening site's screen and typed into the other. The dialling site checks the listener's certificate
against the invite's fingerprint; the listener checks the code once, then pins the dialler's certificate and
proves every later connection by a signed challenge. Frames are JSON lines over TLS. Nothing here touches the
radio; what crosses is decided by the bridge's sharing rules, never here."""
import base64
import datetime
import hashlib
import json
import os
import secrets
import socket
import ssl
import threading
import time

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

INVITE_TTL = 600          # a code lives ten minutes
PING_EVERY = 20           # seconds between pings on an idle link
AWAY_AFTER = 60           # no frame for this long and the link is dropped and redialled
BACKOFF = (2, 4, 8, 16, 30, 60)


def fingerprint(cert):
    """The site id: SHA-256 of the certificate's public key (SubjectPublicKeyInfo), hex."""
    return hashlib.sha256(cert.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)).hexdigest()


def make_identity(state_dir, name):
    """The site's key and certificate under the state directory; made once, read back after. Returns
    (key_path, cert_path, id)."""
    os.makedirs(state_dir, exist_ok=True)
    key_p, crt_p = os.path.join(state_dir, "site.key"), os.path.join(state_dir, "site.crt")
    if not (os.path.exists(key_p) and os.path.exists(crt_p)):
        key = ec.generate_private_key(ec.SECP256R1())
        subj = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, (name or "mesh-manager")[:60])])
        now = datetime.datetime.now(datetime.timezone.utc)
        cert = (x509.CertificateBuilder().subject_name(subj).issuer_name(subj).public_key(key.public_key())
                .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1))
                .not_valid_after(now + datetime.timedelta(days=3650))
                .add_extension(x509.SubjectAlternativeName([x509.DNSName("mesh-manager")]), critical=False)
                .sign(key, hashes.SHA256()))
        fd = os.open(key_p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        with open(crt_p, "wb") as fh:
            fh.write(cert.public_bytes(serialization.Encoding.PEM))
    os.chmod(key_p, 0o600)
    with open(crt_p, "rb") as fh:
        cert = x509.load_pem_x509_certificate(fh.read())
    return key_p, crt_p, fingerprint(cert)


def sign(key_path, data):
    with open(key_path, "rb") as fh:
        key = serialization.load_pem_private_key(fh.read(), password=None)
    return base64.b64encode(key.sign(data, ec.ECDSA(hashes.SHA256()))).decode()


def verify(cert_pem, data, sig_b64):
    try:
        cert = x509.load_pem_x509_certificate(cert_pem.encode() if isinstance(cert_pem, str) else cert_pem)
        cert.public_key().verify(base64.b64decode(sig_b64), data, ec.ECDSA(hashes.SHA256()))
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


def parse_invite(text):
    """`<host>:<port>/<code>/<fingerprint>`; the code may be empty (a pinned site redialling)."""
    text = str(text or "").strip()
    try:
        addr, code, fp = text.split("/", 2)
        host, port = addr.rsplit(":", 1)
        port = int(port)
    except ValueError:
        return None
    fp = fp.strip().lower()
    if not host or not (1 <= port <= 65535) or len(fp) != 64 or any(c not in "0123456789abcdef" for c in fp):
        return None
    return {"host": host.strip(), "port": port, "code": code.strip(), "fingerprint": fp}


def accept_item(item, my_id):
    """The loop guard: an item that has already passed through this site is dropped."""
    return my_id not in (item.get("path") or []) and item.get("origin") != my_id


class Link:
    """One authenticated connection, either direction. Reads JSON lines on its own thread and hands each frame
    to `on_frame(link, frame)`; `send` writes one frame."""
    def __init__(self, sock, peer_id, peer_name, direction, on_frame, on_close):
        self.sock, self.peer_id, self.peer_name, self.direction = sock, peer_id, peer_name, direction
        self.on_frame, self.on_close = on_frame, on_close
        self.since = time.time(); self.last_seen = time.time(); self.sent = 0; self.received = 0
        self._wlock = threading.Lock(); self._closed = False
        self._rf = sock.makefile("rb")
        threading.Thread(target=self._read_loop, name=f"peer-{peer_name}", daemon=True).start()
        threading.Thread(target=self._ping_loop, name=f"peer-ping-{peer_name}", daemon=True).start()

    def send(self, frame):
        try:
            with self._wlock:
                self.sock.sendall((json.dumps(frame, separators=(",", ":")) + "\n").encode())
            self.sent += 1
            return True
        except (OSError, ValueError):
            self.close(); return False

    def _read_loop(self):
        try:
            while not self._closed:
                line = self._rf.readline()
                if not line:
                    break
                self.last_seen = time.time(); self.received += 1
                try:
                    frame = json.loads(line.decode())
                except ValueError:
                    continue
                if isinstance(frame, dict) and "ping" not in frame:
                    self.on_frame(self, frame)
        except (OSError, ValueError):
            pass
        self.close()

    def _ping_loop(self):
        while not self._closed:
            time.sleep(PING_EVERY / 4)
            if self._closed:
                break
            if time.time() - self.last_seen > AWAY_AFTER:
                self.close(); break
            if time.time() - getattr(self, "_last_ping", 0) >= PING_EVERY:
                self._last_ping = time.time(); self.send({"ping": int(time.time())})

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            self.sock.close()
        except OSError:
            pass
        try:
            self.on_close(self)
        except Exception:  # noqa: BLE001
            pass


def _read_frame(rf):
    line = rf.readline()
    if not line:
        return None
    try:
        f = json.loads(line.decode())
    except ValueError:
        return None
    return f if isinstance(f, dict) else None


class Peering:
    """The site's identity, its pins, its listener and its dialled links. The bridge owns one of these and
    supplies: `name`, `state_dir`, a `logger`, `admit(peer_id)` (is this site pinned, and with what
    certificate), `pin(peer_id, name, cert_pem, direction)`, `check_code(code)`, and `on_item(link, item)`."""
    def __init__(self, bridge, conf):
        self.b = bridge; self.conf = conf
        self.name = str(conf.get("SITE_NAME") or socket.gethostname() or "mesh-manager").strip()[:60]
        self.key_path, self.cert_path, self.id = make_identity(bridge.state_dir, self.name)
        with open(self.cert_path) as fh:
            self.cert_pem = fh.read()
        self.links = {}            # peer id -> Link
        self.dialers = {}          # peer id -> stop Event
        self.refusals = {}         # peer id -> the last refusal in words
        self.lock = threading.RLock()
        self.server = None; self.port = None; self._stop = threading.Event()
        bind = str(conf.get("PEER_BIND") or "").strip()
        if bind:
            self._listen(bind, int(conf.get("PEER_PORT") or 8094))

    # ---- the listener ----------------------------------------------------------------------------
    def _listen(self, bind, port):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); ctx.load_cert_chain(self.cert_path, self.key_path)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM); srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((bind, port)); srv.listen(16)
        self.server, self.port, self._ctx = srv, srv.getsockname()[1], ctx
        threading.Thread(target=self._accept_loop, name="peer-listen", daemon=True).start()
        self.b.logger.info(f"peers: listening on {bind}:{self.port} (TLS, site {self.id[:12]})")

    def _accept_loop(self):
        while not self._stop.is_set():
            try:
                raw, addr = self.server.accept()
            except OSError:
                break
            threading.Thread(target=self._serve_one, args=(raw, addr), daemon=True).start()

    def _serve_one(self, raw, addr):
        try:
            raw.settimeout(20)
            sock = self._ctx.wrap_socket(raw, server_side=True)
            rf = sock.makefile("rb")
            hello = _read_frame(rf)
            if not hello or "hello" not in hello:
                sock.close(); return
            claimed, cname, cert_pem = str(hello.get("site") or ""), str(hello.get("name") or "")[:60], str(hello.get("cert") or "")
            nonce = secrets.token_bytes(32)
            sock.sendall((json.dumps({"challenge": base64.b64encode(nonce).decode(), "site": self.id, "name": self.name}) + "\n").encode())
            auth = _read_frame(rf)
            if not auth or "auth" not in auth:
                sock.close(); return
            pinned = self.b.peer_pinned(claimed)
            if pinned:
                if not verify(pinned.get("cert", ""), nonce, auth.get("auth", "")):
                    self._refuse(sock, "the signature does not match the pinned certificate"); return
                self.b.peer_touch(claimed, cname)
            else:
                code = str(auth.get("code") or "")
                why = self.b.peer_check_code(code)
                if why:
                    self._refuse(sock, why); return
                try:
                    cert = x509.load_pem_x509_certificate(cert_pem.encode())
                except ValueError:
                    self._refuse(sock, "no certificate offered"); return
                if fingerprint(cert) != claimed or not verify(cert_pem, nonce, auth.get("auth", "")):
                    self._refuse(sock, "the certificate does not match the site id"); return
                self.b.peer_pin(claimed, cname, cert_pem, "in")
            sock.sendall((json.dumps({"ok": True, "site": self.id, "name": self.name}) + "\n").encode())
            sock.settimeout(AWAY_AFTER + 10)
            self._attach(Link(sock, claimed, cname, "in", self._on_frame, self._on_close))
        except (OSError, ssl.SSLError, ValueError) as ex:
            self.b.logger.info(f"peers: a connection from {addr[0]} ended early: {type(ex).__name__}")
            try:
                raw.close()
            except OSError:
                pass

    def _refuse(self, sock, why):
        try:
            sock.sendall((json.dumps({"error": why}) + "\n").encode())
        except OSError:
            pass
        sock.close()

    # ---- dialling ----------------------------------------------------------------------------------
    def dial(self, peer_id, host, port, code="", first_result=None):
        """Keep a link up to a peer that listens; the first connection may carry a pairing code."""
        stop = threading.Event()
        with self.lock:
            old = self.dialers.pop(peer_id, None)
            if old: old.set()
            self.dialers[peer_id] = stop
        threading.Thread(target=self._dial_loop, args=(peer_id, host, port, code, stop, first_result), name=f"peer-dial-{peer_id[:8]}", daemon=True).start()

    def _dial_loop(self, peer_id, host, port, code, stop, first_result):
        n = 0
        while not stop.is_set() and not self._stop.is_set():
            ok, why, answer = self._dial_once(peer_id, host, port, code)
            if first_result is not None:
                first_result.append((ok, why, answer)); first_result = None
            if ok:
                code = ""
                n = 0
                # the link is up; wait until it closes
                while not stop.is_set() and peer_id in self.links:
                    time.sleep(0.5)
                continue
            self.refusals[peer_id] = why
            if why and "code" in why and "used" in why:
                return  # a spent code will not work next time either
            time.sleep(BACKOFF[min(n, len(BACKOFF) - 1)]); n += 1

    def _dial_once(self, peer_id, host, port, code):
        try:
            raw = socket.create_connection((host, port), timeout=12)
        except OSError as ex:
            return False, f"no answer from {host}:{port} ({type(ex).__name__})", None
        try:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
            ctx.minimum_version = ssl.TLSVersion.TLSv1_2
            sock = ctx.wrap_socket(raw, server_hostname="mesh-manager")
            der = sock.getpeercert(binary_form=True)
            got = fingerprint(x509.load_der_x509_certificate(der))
            if got != peer_id:
                sock.close(); return False, "the site that answered is not the one the invite names", None
            rf = sock.makefile("rb")
            sock.sendall((json.dumps({"hello": 1, "site": self.id, "name": self.name, "cert": self.cert_pem}) + "\n").encode())
            ch = _read_frame(rf)
            if not ch or "challenge" not in ch:
                sock.close(); return False, "no challenge from the site", None
            sig = sign(self.key_path, base64.b64decode(ch["challenge"]))
            sock.sendall((json.dumps({"auth": sig, "code": code or ""}) + "\n").encode())
            ans = _read_frame(rf)
            if not ans or not ans.get("ok"):
                sock.close(); return False, (ans or {}).get("error") or "refused", None
            name = str(ans.get("name") or "")[:60]
            self.b.peer_pin(peer_id, name, None, "out")  # a dialled peer is pinned by the invite's fingerprint
            sock.settimeout(AWAY_AFTER + 10)
            self._attach(Link(sock, peer_id, name, "out", self._on_frame, self._on_close))
            self.refusals.pop(peer_id, None)
            return True, "", {"site": ans.get("site"), "name": name}
        except (OSError, ssl.SSLError, ValueError) as ex:
            try:
                raw.close()
            except OSError:
                pass
            return False, f"the link failed: {type(ex).__name__}", None

    def stop_dial(self, peer_id):
        with self.lock:
            ev = self.dialers.pop(peer_id, None)
        if ev: ev.set()
        self.drop(peer_id)

    # ---- links -------------------------------------------------------------------------------------
    def _attach(self, link):
        with self.lock:
            old = self.links.get(link.peer_id)
            self.links[link.peer_id] = link
        if old and old is not link:
            old.on_close = lambda l: None; old.close()
        self.b.logger.info(f"peers: {link.peer_name or link.peer_id[:12]} connected ({link.direction})")
        self.b.peer_connected(link)

    def _on_close(self, link):
        with self.lock:
            if self.links.get(link.peer_id) is link:
                del self.links[link.peer_id]
        self.b.logger.info(f"peers: {link.peer_name or link.peer_id[:12]} away")

    def _on_frame(self, link, frame):
        item = frame.get("item")
        if isinstance(item, dict):
            self.b.peer_item(link, item)

    def drop(self, peer_id):
        with self.lock:
            link = self.links.get(peer_id)
        if link:
            link.close()

    def broadcast(self, frame, exclude=None):
        with self.lock:
            links = list(self.links.values())
        for l in links:
            if exclude is not None and l.peer_id == exclude:
                continue
            l.send(frame)

    def connected(self):
        with self.lock:
            return dict(self.links)

    def stop(self):
        self._stop.set()
        for ev in list(self.dialers.values()):
            ev.set()
        if self.server:
            try:
                self.server.close()
            except OSError:
                pass
        for l in list(self.links.values()):
            l.close()
