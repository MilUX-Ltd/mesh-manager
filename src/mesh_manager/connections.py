"""Agent connections (hashed bearer tokens at an operator-set autonomy), the proposal queue and
the audit log (Spec 005). All files live in the etc directory beside passwd."""
import datetime
import hashlib
import json
import os
import secrets
import threading

_lock = threading.RLock()


def _now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _path(etc, name):
    return os.path.join(etc, name)


def _load(etc, name, default):
    try:
        with open(_path(etc, name)) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def _save(etc, name, data):
    tmp = _path(etc, name) + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(data, fh, indent=1)
    os.replace(tmp, _path(etc, name))
    try:
        os.chmod(_path(etc, name), 0o600)
    except OSError:
        pass


def _hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


# ---- connections --------------------------------------------------------------------------------
def list_connections(etc):
    return [dict(c, hash=None) for c in _load(etc, "connections.json", [])]


def mint(etc, name, autonomy):
    if autonomy not in ("observe", "propose", "act"):
        raise ValueError("autonomy must be observe, propose or act")
    name = str(name).strip()[:40] or "unnamed"
    token = "mm_" + secrets.token_urlsafe(24)
    with _lock:
        conns = _load(etc, "connections.json", [])
        rec = {"id": secrets.token_hex(6), "name": name, "autonomy": autonomy, "hash": _hash(token),
               "created": _now(), "revoked": None, "last_used": None}
        conns.append(rec)
        _save(etc, "connections.json", conns)
    audit(etc, who="operator", event="connection-mint", name=name, autonomy=autonomy, id=rec["id"])
    return {"id": rec["id"], "name": name, "autonomy": autonomy, "token": token}


def find_by_token(etc, token):
    if not token:
        return None
    h = _hash(token)
    with _lock:
        conns = _load(etc, "connections.json", [])
        for c in conns:
            if c.get("hash") == h and not c.get("revoked"):
                c["last_used"] = _now()
                _save(etc, "connections.json", conns)
                return dict(c)
    return None


def set_autonomy(etc, cid, autonomy):
    if autonomy not in ("observe", "propose", "act"):
        return False
    with _lock:
        conns = _load(etc, "connections.json", [])
        hit = None
        for c in conns:
            if c.get("id") == cid:
                c["autonomy"] = autonomy
                hit = c
        if hit:
            _save(etc, "connections.json", conns)
    if hit:
        audit(etc, who="operator", event="connection-autonomy", id=cid, autonomy=autonomy)
        return True
    return False


def revoke(etc, cid):
    with _lock:
        conns = _load(etc, "connections.json", [])
        hit = None
        for c in conns:
            if c.get("id") == cid and not c.get("revoked"):
                c["revoked"] = _now()
                hit = c
        if hit:
            _save(etc, "connections.json", conns)
    if hit:
        audit(etc, who="operator", event="connection-revoke", id=cid, name=hit.get("name"))
        return True
    return False


# ---- audit ---------------------------------------------------------------------------------------
def audit(etc, **entry):
    line = json.dumps({"ts": _now(), **entry}, default=str)
    with _lock:
        with open(_path(etc, "audit.log"), "a") as fh:
            fh.write(line + "\n")


def audit_tail(etc, n=200):
    try:
        with open(_path(etc, "audit.log")) as fh:
            lines = fh.read().splitlines()
    except OSError:
        return []
    out = []
    for ln in lines[-n:]:
        try:
            out.append(json.loads(ln))
        except ValueError:
            pass
    return out


# ---- proposals ---------------------------------------------------------------------------------
def propose(etc, who, action, args, rationale):
    with _lock:
        props = _load(etc, "proposals.json", [])
        rec = {"id": secrets.token_hex(5), "who": who, "action": action, "arguments": args,
               "rationale": str(rationale or "")[:500], "created": _now()}
        props.append(rec)
        _save(etc, "proposals.json", props)
    audit(etc, who=who, event="proposal", id=rec["id"], action=action, arguments=args, rationale=rec["rationale"])
    return rec


def proposals(etc):
    return _load(etc, "proposals.json", [])


def proposal_take(etc, pid):
    with _lock:
        props = _load(etc, "proposals.json", [])
        keep, hit = [], None
        for p in props:
            (keep if p.get("id") != pid else [None]).append(p)
            if p.get("id") == pid:
                hit = p
        if hit:
            _save(etc, "proposals.json", [p for p in props if p.get("id") != pid])
    return hit
