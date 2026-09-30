"""The history store (Spec 020): positions, telemetry, messages and packets that survive a
restart. SQLite from the standard library, WAL, one file in the state directory. Writes never
raise into the caller: the receive path must keep going whatever the disk does."""
import calendar
import math
import os
import sqlite3
import threading
import time

TABLES = {
    "positions": "ts TEXT NOT NULL, node TEXT NOT NULL, lat REAL NOT NULL, lon REAL NOT NULL, snr REAL, hops INTEGER, rssi REAL, via_mqtt INTEGER",
    "telemetry": "ts TEXT NOT NULL, node TEXT NOT NULL, level INTEGER, voltage REAL, chutil REAL, airutil REAL, uptime INTEGER",
    "messages": "ts TEXT NOT NULL, node TEXT, name TEXT, dest TEXT, channel INTEGER, text TEXT NOT NULL, snr REAL",
    "packets": "ts TEXT NOT NULL, node TEXT, port TEXT, snr REAL, hops INTEGER, size INTEGER, rssi REAL, via_mqtt INTEGER",
    "alerts": "ts TEXT NOT NULL, node TEXT, kind TEXT NOT NULL, text TEXT NOT NULL, state TEXT NOT NULL, cleared TEXT",
    "environment": "ts TEXT NOT NULL, node TEXT NOT NULL, temperature REAL, humidity REAL, pressure REAL, gas REAL, lux REAL, iaq REAL, wind_dir REAL, wind_speed REAL",
    "waypoints": "ts TEXT NOT NULL, node TEXT, wid INTEGER, name TEXT, description TEXT, lat REAL, lon REAL, expire INTEGER, gone INTEGER",
    "neighbors": "ts TEXT NOT NULL, node TEXT NOT NULL, neighbor TEXT NOT NULL, snr REAL",
    "reboots": "ts TEXT NOT NULL, node TEXT NOT NULL, booted TEXT NOT NULL, uptime INTEGER, asked INTEGER",   # Spec 103
}
CAP = 200_000


RSSI_RANGE = (-200.0, 20.0)   # Spec 102: dBm; nothing a LoRa radio reports falls outside this


def plausible_rssi(v):
    """Spec 102: a received signal strength as a float, or None when it is not one a radio could report.
    A packet's fields are data from the air: a string, a boolean, NaN or a structure is stored as nothing."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    v = float(v)
    if not math.isfinite(v) or not RSSI_RANGE[0] <= v <= RSSI_RANGE[1]:
        return None
    return v


REBOOT_SLACK_S = 600          # Spec 103: a start time that moves by less than this is the same boot
UPTIME_MAX_S = 10 * 365 * 86400


def plausible_uptime(v):
    """Spec 103: a node's uptime in whole seconds, or None when it is not one a node could report."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    if isinstance(v, float) and (not math.isfinite(v) or v != int(v)):
        return None
    v = int(v)
    return v if 0 <= v <= UPTIME_MAX_S else None


def rebooted(prev_rx, prev_uptime, rx, uptime):
    """Spec 103: whether a node started again between two readings, from when each was received and
    the uptime each carried. A node has rebooted when it says it started after it was last heard; the
    slack absorbs a packet that arrived late or was heard twice, which moves the start time by its delay."""
    if None in (prev_rx, prev_uptime, rx, uptime):
        return False
    try:
        return (float(rx) - float(uptime)) - (float(prev_rx) - float(prev_uptime)) > REBOOT_SLACK_S
    except (TypeError, ValueError):
        return False


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
            # Spec 034 and Spec 055: columns added after the store first shipped; a store that predates them
            # gains them here, once. SQLite cannot add a column that exists, so check first.
            # Spec 102 adds the signal strength and the broker mark to positions and packets the same way.
            for table, added in (("messages", (("mid", "INTEGER"), ("ack", "TEXT"), ("origin", "TEXT"), ("origin_name", "TEXT"), ("channel_name", "TEXT"), ("aired_from", "TEXT"))),
                                 ("positions", (("rssi", "REAL"), ("via_mqtt", "INTEGER"))),
                                 ("packets", (("rssi", "REAL"), ("via_mqtt", "INTEGER")))):
                have = {r[1] for r in self._conn.execute(f"PRAGMA table_info({table})").fetchall()}
                for col, typ in added:
                    if col not in have:
                        self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
            # Spec 102: every table, after the columns exist. These lines once sat inside the loop
            # above, where `name` still held the last table, so only neighbors was ever indexed.
            for name in TABLES:
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

    def position(self, node, lat, lon, snr=None, hops=None, ts=None, rssi=None, via_mqtt=None):
        """Spec 102: via_mqtt is 1 for a position a broker carried, 0 for one this radio heard, None when not known."""
        return self._write("positions", {"ts": ts or utc(), "node": node, "lat": float(lat), "lon": float(lon), "snr": snr, "hops": hops, "rssi": rssi, "via_mqtt": via_mqtt})

    def telemetry(self, node, level=None, voltage=None, chutil=None, airutil=None, uptime=None, ts=None):
        return self._write("telemetry", {"ts": ts or utc(), "node": node, "level": level, "voltage": voltage, "chutil": chutil, "airutil": airutil, "uptime": uptime})

    def message(self, node, text, name=None, dest=None, channel=0, snr=None, ts=None, mid=None, ack=None, origin=None, origin_name=None, channel_name=None, aired_from=None):
        """A message heard, sent or (Spec 055) received from a peer; origin names the site it came from, aired_from the peer whose words went on this air."""
        return self._write("messages", {"ts": ts or utc(), "node": node, "name": name, "dest": dest, "channel": channel, "text": str(text), "snr": snr, "mid": mid, "ack": ack,
                                        "origin": origin, "origin_name": origin_name, "channel_name": channel_name, "aired_from": aired_from})

    def newest_message(self, origin=None):
        """Spec 055: the latest time held of a peer's messages (one origin, or any remote origin), or None."""
        if not self._conn:
            return None
        try:
            with self._lock:
                if origin:
                    row = self._conn.execute("SELECT MAX(ts) FROM messages WHERE origin = ?", (str(origin),)).fetchone()
                else:
                    row = self._conn.execute("SELECT MAX(ts) FROM messages WHERE origin IS NOT NULL").fetchone()
            return row[0] if row else None
        except Exception:  # noqa: BLE001
            return None

    def has_message(self, origin, ts, node, text):
        """Spec 055: is this remote message (same origin, time, sender and words) already held?"""
        if not self._conn:
            return False
        try:
            with self._lock:
                row = self._conn.execute("SELECT 1 FROM messages WHERE origin = ? AND ts = ? AND node IS ? AND text = ? LIMIT 1", (str(origin), str(ts), node, str(text))).fetchone()
            return row is not None
        except Exception:  # noqa: BLE001
            return False

    def catchup(self, since, exclude_origin, limit=200):
        """Spec 055: broadcasts since a time, oldest first, for a peer to catch up on: this box's own and those held from
        other sites, never the asker's own and never words that came off a link and went on this air."""
        if not self._conn:
            return []
        limit = max(1, min(int(limit or 200), 1000))
        try:
            with self._lock:
                cur = self._conn.execute("SELECT * FROM messages WHERE ts >= ? AND (dest IS NULL OR dest IN ('^all', '!ffffffff', '')) AND aired_from IS NULL"
                                         " AND (origin IS NULL OR origin != ?) ORDER BY id ASC LIMIT ?", (str(since), str(exclude_origin), limit))
                cols = [c[0] for c in cur.description]
                return [dict(zip(cols, r)) for r in cur.fetchall()]
        except Exception:  # noqa: BLE001
            return []

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

    def reboot(self, node, booted, uptime=None, asked=False, ts=None):
        """Spec 103: a node seen to have started again; booted is when, from its uptime."""
        return self._write("reboots", {"ts": ts or utc(), "node": node, "booted": booted, "uptime": uptime, "asked": 1 if asked else 0})

    def last_uptime(self, node):
        """Spec 103: (received, uptime) of the node's newest telemetry row with a plausible uptime, as epoch
        seconds and seconds, or None. What the reboot rule compares with after the bridge restarts."""
        if not self._conn:
            return None
        try:
            with self._lock:
                rows = self._conn.execute("SELECT ts, uptime FROM telemetry WHERE node = ? AND uptime IS NOT NULL ORDER BY id DESC LIMIT 50", (node,)).fetchall()
        except Exception:  # noqa: BLE001
            return None
        for ts, up in rows:
            u = plausible_uptime(up)
            if u is None:
                continue
            try:
                return (calendar.timegm(time.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")), u)
            except (TypeError, ValueError):
                continue
        return None

    def packet(self, node, port=None, snr=None, hops=None, size=None, ts=None, rssi=None, via_mqtt=None):
        return self._write("packets", {"ts": ts or utc(), "node": node, "port": port, "snr": snr, "hops": hops, "size": size, "rssi": rssi, "via_mqtt": via_mqtt})

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

    def query(self, kind, node=None, since=None, limit=500, origin=None, over_air=False):
        """Rows of one table, newest last, filtered by node, by a utc() lower bound, (Spec 055, messages) by origin
        and (Spec 102, positions and packets) to what this radio heard itself. over_air leaves out rows a broker
        carried and rows too old to say, since neither describes this radio's reception."""
        if kind not in TABLES or not self._conn:
            return []
        limit = max(1, min(int(limit or 500), 5000))
        where, args = [], []
        if node:
            where.append("node = ?"); args.append(node)
        if since:
            where.append("ts >= ?"); args.append(str(since))
        if origin and kind == "messages":
            where.append("origin = ?"); args.append(str(origin))
        if over_air and kind in ("positions", "packets"):
            where.append("via_mqtt = 0")
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

    # ---- Spec 104: counts and means in the database, so a window is the window whatever its size -----
    def _rows(self, sql, args=()):
        if not self._conn:
            return []
        try:
            with self._lock:
                return self._conn.execute(sql, list(args)).fetchall()
        except Exception as e:  # noqa: BLE001
            if self.logger:
                self.logger.debug(f"history aggregate failed: {type(e).__name__}: {e}")
            return []

    def packet_counts(self, since):
        """Every packet row since then: the total, per node, and how many a broker carried."""
        by = {n: c for n, c in self._rows("SELECT node, COUNT(*) FROM packets WHERE ts >= ? AND node IS NOT NULL GROUP BY node", (since,))}
        total = (self._rows("SELECT COUNT(*) FROM packets WHERE ts >= ?", (since,)) or [(0,)])[0][0]
        broker = (self._rows("SELECT COUNT(*) FROM packets WHERE ts >= ? AND via_mqtt = 1", (since,)) or [(0,)])[0][0]
        return {"total": int(total or 0), "by_node": by, "via_broker": int(broker or 0)}

    def packets_by_hour(self, since):
        """Per clock hour (the ts to the hour), packets heard over the air, those a broker carried, and those
        stored before the broker mark existed (Spec 102), which cannot be told apart and are counted as such."""
        out = {}
        for hour, air, broker, unmarked in self._rows("SELECT substr(ts, 1, 13), SUM(CASE WHEN via_mqtt = 0 THEN 1 ELSE 0 END), "
                                                      "SUM(CASE WHEN via_mqtt = 1 THEN 1 ELSE 0 END), SUM(CASE WHEN via_mqtt IS NULL THEN 1 ELSE 0 END) "
                                                      "FROM packets WHERE ts >= ? GROUP BY 1", (since,)):
            out[hour] = {"packets": int(air or 0), "via_broker": int(broker or 0), "unmarked": int(unmarked or 0)}
        return out

    def hop_spread(self, since):
        """Packets this radio heard over the air, by hop count; broker-carried ones are not this radio's."""
        out = {"0": 0, "1": 0, "2": 0, "3+": 0, "unknown": 0}
        for hops, n in self._rows("SELECT hops, COUNT(*) FROM packets WHERE ts >= ? AND via_mqtt = 0 GROUP BY hops", (since,)):
            if hops is None:
                out["unknown"] += int(n)
            else:
                try:
                    k = int(hops)
                except (TypeError, ValueError):
                    out["unknown"] += int(n); continue
                out[str(k) if 0 <= k <= 2 else ("3+" if k >= 3 else "unknown")] += int(n)
        return out

    def telemetry_by_hour(self, since, own):
        """Per clock hour, the mean of this radio's utilisation and air time, and of every other node's utilisation."""
        out = {}
        for hour, ch, air in self._rows("SELECT substr(ts, 1, 13), AVG(chutil), AVG(airutil) FROM telemetry WHERE ts >= ? AND node = ? GROUP BY 1", (since, own)):
            out.setdefault(hour, {})["chutil"], out[hour]["airutil"] = ch, air
        # the mesh: each node this radio heard over the air in the window gets one vote per hour (the mean of
        # its own readings), so a node that reports often does not outweigh the rest, and a node only a broker
        # carries, whose utilisation describes another mesh's air, is left out
        for hour, ch in self._rows("SELECT hour, AVG(nm) FROM (SELECT substr(ts, 1, 13) AS hour, node, AVG(chutil) AS nm FROM telemetry "
                                   "WHERE ts >= ? AND node != ? AND chutil IS NOT NULL "
                                   "AND node IN (SELECT DISTINCT node FROM packets WHERE ts >= ? AND via_mqtt = 0) GROUP BY 1, 2) GROUP BY hour",
                                   (since, own or "", since)):
            out.setdefault(hour, {})["mesh_chutil"] = ch
        return out

    def last_telemetry(self, since):
        """Each node's newest telemetry row in the window."""
        cols = ("node", "chutil", "airutil", "level", "ts")
        return [dict(zip(cols, r)) for r in self._rows(
            "SELECT node, chutil, airutil, level, ts FROM telemetry WHERE id IN (SELECT MAX(id) FROM telemetry WHERE ts >= ? GROUP BY node)", (since,))]

    def sent_since(self, own, since):
        """(ts, ack) of each message this box sent with a packet id, in the window."""
        return self._rows("SELECT ts, ack FROM messages WHERE node = ? AND mid IS NOT NULL AND ts >= ?", (own, since))

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
