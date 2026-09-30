#!/usr/bin/env python3
"""Spec 109, from its independent review (30 September 2026): a short or empty session secret is never
used; a cookie with a non-ASCII signature is refused, not a traceback; a password change that cannot write the
secret changes nothing; and --password-stdin with nothing on standard input stops rather than carrying on.
Written with the fixes, after the review, not before them; the review's findings are the specification."""
import base64, hashlib, hmac, os, subprocess, sys, tempfile
sys.path.insert(0, os.path.dirname(__file__))
from _common import ROOT, check, check_true, finish  # noqa: E402
sys.path.insert(0, os.path.join(ROOT, "src"))
from mesh_manager import web as W  # noqa: E402

etc = tempfile.mkdtemp()
open(os.path.join(etc, "web.secret"), "wb").close()          # zero length, as after a power cut
s = W.Sessions(etc)
check_true("an empty web.secret is replaced, never signed with", len(s.secret) >= 32 and len(open(os.path.join(etc, "web.secret"), "rb").read()) >= 32)
body = "abcdef.9999999999." + base64.urlsafe_b64encode(b"forged").decode().rstrip("=")
forged = body + "." + base64.urlsafe_b64encode(hmac.new(b"", body.encode(), hashlib.sha256).digest()).decode().rstrip("=")
check("a session signed with an empty key is refused", s.verify(forged), None)

raw = os.urandom(31) + b" "                                   # whitespace at an edge is part of the key
open(os.path.join(etc, "web.secret"), "wb").write(os.urandom(1) + raw)
check("the secret is read whole, nothing stripped", len(W.Sessions(etc).secret), 33)

ok = s.issue("Sgt Patel")
check("a non-ASCII signature is refused without an exception", s.verify(ok[:-1] + "é"), None)

before = s.secret
real = W._replace_file
W._replace_file = lambda *a, **k: (_ for _ in ()).throw(OSError(28, "No space left on device"))
try:
    s.rotate()
    raised = False
except OSError:
    raised = True
W._replace_file = real
check_true("a rotation that cannot write the file says so", raised)
check_true("and leaves the secret in memory as it was", s.secret == before)

r = subprocess.run(["bash", os.path.join(ROOT, "install", "install.sh"), "/nonexistent/mesh-manager-1.5.0-amd64.tgz", "--password-stdin", "--dry-run"],
                   input="", capture_output=True, text=True, timeout=60, env={**os.environ, "MESH_MANAGER_ROOT": tempfile.mkdtemp()})
check_true("--password-stdin with nothing on standard input stops, and changes nothing",
           r.returncode == 2 and "no password arrived" in r.stderr, f"{r.returncode} {r.stderr[-200:]}")
src = open(os.path.join(ROOT, "install", "install.sh")).read()
check_true("a password the installer writes ends every session (web.secret is rewritten)", 'PASSWORD_WRITTEN:-0}" == 1' in src)

finish()
