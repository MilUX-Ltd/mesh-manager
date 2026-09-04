"""The history store (Spec 020): positions, telemetry, messages and packets that survive a
restart. SQLite from the standard library, WAL, one file in the state directory. Writes never
raise into the caller: the receive path must keep going whatever the disk does."""
import os
import sqlite3
import threading
import time

TABLES = {
    "positions": "ts TEXT NOT NULL, node TEXT NOT NULL, lat REAL NOT NULL, lon REAL NOT NULL, snr REAL, hops INTEGER",
    "telemetry": "ts TEXT NOT NULL, node TEXT NOT NULL, level INTEGER, voltage REAL, chutil REAL, airutil REAL, uptime INTEGER",
    "messages": "ts TEXT NOT NULL, node TEXT, name TEXT, dest TEXT, channel INTEGER, text TEXT NOT NULL, snr REAL",
    "packets": "ts TEXT NOT NULL, node TEXT, port TEXT, snr REAL, hops INTEGER, size INTEGER",
    "alerts": "ts TEXT NOT NULL, node TEXT, kind TEXT NOT NULL, text TEXT NOT NULL, state TEXT NOT NULL, cleared TEXT",
    "environment": "ts TEXT NOT NULL, node TEXT NOT NULL, temperature REAL, humidity REAL, pressure REAL, gas REAL, lux REAL, iaq REAL, wind_dir REAL, wind_speed REAL",
    "waypoints": "ts TEXT NOT NULL, node TEXT, wid INTEGER, name TEXT, description TEXT, lat REAL, lon REAL, expire INTEGER, gone INTEGER",
    "neighbors": "ts TEXT NOT NULL, node TEXT NOT NULL, neighbor TEXT NOT NULL, snr REAL",
}
CAP = 200_000


def utc(t=None):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t if t is not None else time.time()))


class History:
    def __init__(self, state_dir, days=30, logger=None):
        self.path = os.path.join(state_dir, "history.db")
        self.days = int(days or 30)
        self.logger = logger
        self._lock = threading.Lock()
        self._conn = None
        self._last_trim = 0.0
        try:
            os.makedirs(state_dir, exist_ok=True)
            self._conn = sqlite3.connect(self.path, check_same_thread=False, timeout=5)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            for name, cols in TABLES.items():
                self._conn.execute(f"CREATE TABLE IF NOT EXISTS {name} (id INTEGER PRIMARY KEY, {cols})")
            # Spec 034: columns added after the store first shipped; a store that predates them
            # gains them here, once. SQLite cannot add a column that exists, so check first.
            have = {r[1] for r in self._conn.execute("PRAGMA table_info(messages)").fetchall()}
            for col, typ in (("mid", "INTEGER"), ("ack", "TEXT")):
                if col not in have:
                    self._conn.execute(f"ALTER TABLE messages ADD COLUMN {col} {typ}")
                self._conn.execute(f"CREATE INDEX IF NOT EXISTS {name}_ts ON {name}(ts)")
                self._conn.execute(f"CREATE INDEX IF NOT EXISTS {name}_node ON {name}(node, ts)")
            self._conn.commit()
            self.trim(force=True)
        except Exception as e:  # noqa: BLE001
            self._conn = None
            if logger:
                logger.warning(f"history store unavailable: {type(e).__name__}: {e}")

    @property
    def ok(self):
        return self._conn is not None

    def _write(self, table, row):
        if not self._conn:
            return False
        cols = ", ".join(row.keys())
        marks = ", ".join("?" for _ in row)
        try:
            with self._lock:
                self._conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", list(row.values()))
                self._conn.commit()
            if time.time() - self._last_trim > 3600:
                self.trim()
            return True
        except Exception as e:  # noqa: BLE001
            if self.logger:
                self.logger.debug(f"history write to {table} failed: {type(e).__name__}: {e}")
            return False

    def position(self, node, lat, lon, snr=None, hops=None, ts=None):
        return self._write("positions", {"ts": ts or utc(), "node": node, "lat": float(lat), "lon": float(lon), "snr": snr, "hops": hops})

    def telemetry(self, node, level=None, voltage=None, chutil=None, airutil=None, uptime=None, ts=None):
        return self._write("telemetry", {"ts": ts or utc(), "node": node, "level": level, "voltage": voltage, "chutil": chutil, "airutil": airutil, "uptime": uptime})

    def message(self, node, text, name=None, dest=None, channel=0, snr=None, ts=None, mid=None, ack=None):
        return self._write("messages", {"ts": ts or utc(), "node": node, "name": name, "dest": dest, "channel": channel, "text": str(text), "snr": snr, "mid": mid, "ack": ack})

    def set_ack(self, mid, ack):
        """Spec 034: the radio said whether a sent message arrived; keep that with the message."""
        if not self._conn or mid is None:
            return False
        try:
            with self._lock:
                self._conn.execute("UPDATE messages SET ack=? WHERE mid=?", (str(ack), int(mid)))
                self._conn.commit()
            return True
        except Exception:  # noqa: BLE001
            return False

    def packet(self, node, port=None, snr=None, hops=None, size=None, ts=None):
        return self._write("packets", {"ts": ts or utc(), "node": node, "port": port, "snr": snr, "hops": hops, "size": size})

    def environment(self, node, temperature=None, humidity=None, pressure=None, gas=None, lux=None, iaq=None, wind_dir=None, wind_speed=None, ts=None):
        return self._write("environment", {"ts": ts or utc(), "node": node, "temperature": temperature, "humidity": humidity, "pressure": pressure,
                                           "gas": gas, "lux": lux, "iaq": iaq, "wind_dir": wind_dir, "wind_speed": wind_speed})

    def waypoint(self, node, wid, name=None, description=None, lat=None, lon=None, expire=None, gone=0, ts=None):
        return self._write("waypoints", {"ts": ts or utc(), "node": node, "wid": wid, "name": name, "description": description, "lat": lat, "lon": lon, "expire": expire, "gone": int(gone)})

    def neighbor(self, node, neighbor, snr=None, ts=None):
        return self._write("neighbors", {"ts": ts or utc(), "node": node, "neighbor": neighbor, "snr": snr})

    def alert(self, node, kind, text, ts=None):
        return self._write("alerts", {"ts": ts or utc(), "node": node, "kind": kind, "text": str(text), "state": "open", "cleared": None})

    def alert_clear(self, node, kind, ts=None):
        if not self._conn:
            return False
        try:
            with self._lock:
                self._conn.execute("UPDATE alerts SET state='cleared', cleared=? WHERE node=? AND kind=? AND state='open'", (ts or utc(), node, kind))
                self._conn.commit()
            return True
        except Exception:  # noqa: BLE001
            return False

    def query(self, kind, node=None, since=None, limit=500):
        """Rows of one table, newest last, filtered by node and by a utc() lower bound."""
        if kind not in TABLES or not self._conn:
            return []
        limit = max(1, min(int(limit or 500), 5000))
        where, args = [], []
        if node:
            where.append("node = ?"); args.append(node)
        if since:
            where.append("ts >= ?"); args.append(str(since))
        sql = f"SELECT * FROM {kind}" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        try:
            with self._lock:
                cur = self._conn.execute(sql, args)
                cols = [c[0] for c in cur.description]
                rows = [dict(zip(cols, r)) for r in cur.fetchall()]
            rows.reverse()
            return rows
        except Exception as e:  # noqa: BLE001
            if self.logger:
                self.logger.debug(f"history query {kind} failed: {type(e).__name__}: {e}")
            return []

    def summary(self):
        out = {"path": self.path, "days": self.days, "ok": self.ok, "tables": {}}
        if not self._conn:
            return out
        try:
            with self._lock:
                for name in TABLES:
                    n, lo, hi = self._conn.execute(f"SELECT COUNT(*), MIN(ts), MAX(ts) FROM {name}").fetchone()
                    out["tables"][name] = {"rows": n, "oldest": lo, "newest": hi}
                out["bytes"] = os.path.getsize(self.path) if os.path.exists(self.path) else 0
        except Exception as e:  # noqa: BLE001
            out["error"] = f"{type(e).__name__}: {e}"
        return out

    def trim(self, force=False):
        """Rows older than the retention, and beyond the cap, go."""
        if not self._conn:
            return
        self._last_trim = time.time()
        cutoff = utc(time.time() - self.days * 86400)
        try:
            with self._lock:
                for name in TABLES:
                    self._conn.execute(f"DELETE FROM {name} WHERE ts < ?", (cutoff,))
                    self._conn.execute(f"DELETE FROM {name} WHERE id <= (SELECT id FROM {name} ORDER BY id DESC LIMIT 1 OFFSET ?)", (CAP,))
                self._conn.commit()
        except Exception as e:  # noqa: BLE001
            if self.logger:
                self.logger.debug(f"history trim failed: {type(e).__name__}: {e}")

    def close(self):
        try:
            if self._conn:
                self._conn.close()
        except Exception:  # noqa: BLE001
            pass
        self._conn = None
