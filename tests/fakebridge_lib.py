"""A fake bridge as a library for the suites: the Spec 002 protocol on a unix socket, with a
plausible mesh, a call log, and a way to raise events."""
import json
import os
import socket
import tempfile
import threading

NODES = [{"id": "!aa000001", "name": "Tracker9", "battery": 77, "snr": 12.5, "hops": 0, "heard": "2026-09-03T02:00:00Z", "hw": "TRACKER_T1000_E", "lat": 51.2, "lon": -1.5, "heard_here": True},
         {"id": "!bb000002", "name": "Tracker2", "battery": 9, "snr": 3.0, "hops": 1, "heard": "2026-09-03T01:50:00Z", "hw": "TRACKER_T1000_E", "heard_here": True},
         {"id": "!cc000003", "name": "OldTracker", "battery": None, "snr": None, "hops": None, "heard": None, "hw": "HELTEC_V3", "heard_here": False, "last_heard_db": "2026-08-30T10:00:00Z"}]
STATUS = {"alerts_open": 1, "version": "0.1.0", "radio": "/dev/serial/by-id/usb-x-if00", "radio_present": True, "connected": True,
          "observe": False, "mode": "tak-server", "tak": "on", "last_activity": "2026-09-03T02:00:00Z", "last_forwarded": "2026-09-03T01:59:00Z",
          "nodes_seen": 2, "nodes_db": 3, "own": {"id": "!00000001", "name": "TAK Gateway", "short": "TAKG"}, "region": "EU_868", "modem_preset": "SHORT_FAST",
          "primary_channel": "MILUX-TAK", "uptime": 120, "watchdog": "pinging", "state_dir": "/tmp/x", "socket": "/tmp/x.sock"}
CHANNELS = {"channels": [{"index": 0, "name": "MILUX-TAK", "role": "PRIMARY", "has_key": True}, {"index": 1, "name": "", "role": "DISABLED", "has_key": False}],
            "url": "https://meshtastic.org/e/#SECRET-URL-WITH-KEY-ZZZ"}
ROUTE = {"dest": "!aa000001", "ts": "2026-09-03T02:05:00Z", "hops": 1,
         "towards": [{"id": "!bb000002", "name": "Tracker2", "snr": 3.0}, {"id": "!aa000001", "name": "Tracker9", "snr": 7.25}],
         "back": [{"id": "!bb000002", "name": "Tracker2", "snr": 2.5}, {"id": "!00000001", "name": "TAK Gateway", "snr": None}]}
_EXTRA = {"!aa000001": {"direct_snr": 12.5, "history": [["2026-09-03T01:40:00Z", 12.5, 0], ["2026-09-03T01:50:00Z", 11.0, 0], ["2026-09-03T02:00:00Z", 12.5, 0]]},
          "!bb000002": {"direct_snr": None, "history": [["2026-09-03T01:50:00Z", 3.0, 1]]},
          "!cc000003": {"direct_snr": None, "history": []}}
LINKS = {"own": {"id": "!00000001", "name": "TAK Gateway", "lat": 51.21, "lon": -1.5, "position_source": "radio"},
         "nodes": [dict(n, **_EXTRA.get(n["id"], {"direct_snr": None, "history": []})) for n in NODES],
         "routes": {"!aa000001": ROUTE}}


def links_now():
    """The links answer built from NODES as they are now (a suite may label a node after import)."""
    return dict(LINKS, nodes=[dict(n, **_EXTRA.get(n["id"], {"direct_snr": None, "history": []})) for n in NODES])
REGISTER = {"rows": [
    {"id": "!aa000001", "name": "Tracker9", "label": "Tracker 9 (recce)", "holder": "Cpl Smith", "hw": "TRACKER_T1000_E", "firmware": "2.6.11", "role": "TRACKER",
     "managed": True, "managed_at": "2026-09-02T10:00:00Z", "onboarded_at": "2026-09-02T10:00:00Z", "heard": "2026-09-03T02:00:00Z", "heard_here": True, "battery": 77},
    {"id": "!bb000002", "name": "Tracker2", "label": "", "holder": "", "hw": "TRACKER_T1000_E", "firmware": None, "role": None,
     "managed": False, "heard": "2026-09-03T01:50:00Z", "heard_here": True, "battery": 9},
    {"id": "!cc000003", "name": "OldTracker", "label": "", "holder": "", "hw": "HELTEC_V3", "firmware": None, "role": None,
     "managed": False, "heard": None, "heard_here": False, "battery": None}]}
BENCH = {"gateway": "/dev/serial/by-id/usb-x-if00",
         "devices": [{"path": "/dev/serial/by-id/usb-Seeed_T1000-E_9F3A-if00", "tty": "ttyACM3", "bootloader": False},
                     {"path": "/dev/serial/by-id/usb-RAKwireless_WisCore_RAK4631_Board_BOOT-if00", "tty": "ttyACM4", "bootloader": True,
                      "recovery": "double-press reset, copy the pinned firmware UF2 onto the volume that appears, wait for it to come back"}]}
BENCH_READ = {"path": "/dev/serial/by-id/usb-Seeed_T1000-E_9F3A-if00", "id": "!ee000005", "long_name": "New Device", "short_name": "NEW", "hw": "TRACKER_T1000_E",
              "firmware": "2.6.11", "region": "UNSET", "modem_preset": "LONG_FAST", "role": "CLIENT", "managed": False, "admin_keys": 0,
              "channels": [{"index": 0, "name": "LongFast", "role": "PRIMARY", "has_key": True}]}
NODE_READ = {"id": "!aa000001", "long_name": "Tracker9", "short_name": "TR9", "region": "EU_868", "modem_preset": "SHORT_FAST", "role": "TRACKER", "tx_power": 27,
             "position_broadcast_secs": 900, "managed": True, "admin_keys": 1, "firmware": "2.6.11", "read_at": "2026-09-03T03:00:00Z",
             "channels": [{"index": 0, "name": "MILUX-TAK", "role": "PRIMARY", "has_key": True}]}
SHELF = {"dir": "/var/lib/vantage-mesh/firmware", "images": [
    {"id": "t1000e-2.6.11", "hw": ["TRACKER_T1000_E"], "version": "2.6.11", "method": "uf2", "file": "firmware-tracker-t1000-e-2.6.11.60ec05e.uf2", "recommended": True, "state": "verified", "path": "/var/lib/vantage-mesh/firmware/firmware-tracker-t1000-e-2.6.11.60ec05e.uf2", "note": "The fleet's tracker firmware. Later versions lost the GPS fix on the T1000-E."},
    {"id": "heltec-v4-2.7.26", "hw": ["HELTEC_V4"], "version": "2.7.26", "method": "esptool", "file": "firmware-heltec-v4-2.7.26.54e0d8d.bin", "recommended": True, "state": "missing", "path": "/var/lib/vantage-mesh/firmware/firmware-heltec-v4-2.7.26.54e0d8d.bin"},
    {"id": "factory-erase-s140-7.3.0", "hw": ["TRACKER_T1000_E", "RAK4631"], "version": "erase 7.3.0", "method": "uf2", "file": "factory-erase-S140-7.3.0.uf2", "recommended": False, "state": "verified", "path": "/var/lib/vantage-mesh/firmware/factory-erase-S140-7.3.0.uf2"}]}
EXPORTS = {"id": "!ee000005", "exports": [{"path": "/var/lib/vantage-mesh/exports/!ee000005/2026-09-03T03-00-00Z.json", "when": "2026-09-03T03:00:00Z", "bytes": 1840}]}
CONFIG = {"long_name": "TAK Gateway", "short_name": "TAKG", "role": "CLIENT", "region": "EU_868", "modem_preset": "SHORT_FAST", "tx_power": 14, "position_broadcast_secs": 900}


class FakeBridge:
    def __init__(self):
        self.path = os.path.join(tempfile.mkdtemp(), "b.sock")
        self.calls = []
        self.clients = []
        self._srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._srv.bind(self.path); self._srv.listen(16)
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            c, _ = self._srv.accept()
            threading.Thread(target=self._one, args=(c,), daemon=True).start()

    def _one(self, c):
        f = c.makefile("rb")
        try:
            req = json.loads(f.readline().decode())
        except Exception:  # noqa: BLE001
            c.sendall(b'{"error": "bad json"}\n'); c.close(); return
        op = req.pop("op", None)
        if op == "events":
            c.sendall(b'{"kind": "hello"}\n'); self.clients.append(c); return
        if op in ("profile", "profile_set", "drift", "drift_fix"):
            rep = {"profile": {"role": "TRACKER", "tx_power": 20, "position_broadcast_secs": 900, "region": "EU_868", "modem_preset": "SHORT_FAST"},
                   "profile_set": {"written": {k: req.get(k) for k in ("role", "tx_power", "position_broadcast_secs", "region", "modem_preset")}, "confirmed": True},
                   "drift": {"profile": {"role": "TRACKER", "tx_power": 20, "position_broadcast_secs": 900, "region": "EU_868", "modem_preset": "SHORT_FAST"}, "enforced": ["role", "tx_power", "position_broadcast_secs", "region", "modem_preset"],
                             "devices": [{"id": "!aa000001", "name": "Recce lead", "state": "drifted", "diffs": [{"field": "tx_power", "is": 27, "should": 20}], "read_at": "2026-09-03T03:00:00Z", "managed": True},
                                         {"id": "!bb000002", "name": "Tracker2", "state": "unread", "diffs": [], "managed": False},
                                         {"id": "!ee000005", "name": "New Device", "state": "in line", "diffs": [], "read_at": "2026-09-03T03:00:00Z", "managed": True}],
                             "counts": {"in_line": 1, "drifted": 1, "unread": 1}},
                   "drift_fix": {"id": req.get("id"), "safe": {"written": ["tx_power"], "confirmed": True, "read_back": {"tx_power": 20}}, "hard": None, "skipped": [], "confirmed": True}}[op]
            self.calls.append((op, dict(req)))
            c.sendall((json.dumps(rep) + "\n").encode()); c.close(); return
        if op in ("rotation_status", "rotation_mark"):
            rep = {"rotation_status": {"rotation": {"ts": "2026-09-03T21:00:00Z", "index": 0, "name": "MILUX-TAK", "source": "rotated from the screen", "note": None},
                                       "back": [{"id": "!aa000001", "name": "Tracker9", "heard": "2026-09-03T21:05:00Z"}], "waiting": [{"id": "!bb000002", "name": "Tracker2"}],
                                       "counts": {"expected": 2, "back": 1, "waiting": 1}},
                   "rotation_mark": {"marked": "2026-09-03T22:00:00Z", "index": int(req.get("index") or 0), "name": "MILUX-TAK", "expected": 2, "confirmed": True}}[op]
            self.calls.append((op, dict(req)))
            c.sendall((json.dumps(rep) + "\n").encode()); c.close(); return
        if op in ("alerts", "alert_settings", "alert_set", "alert_test"):
            rep = {"alerts": {"open": [{"node": "!ee000099", "kind": "silent", "text": "Tracker1 silent for 40 min", "since": "2026-09-03T21:50:00Z", "origin": "cd" * 32, "origin_name": "Edge laptop"}, {"node": "!aa000001", "kind": "battery", "text": "Tracker9 battery 9%", "since": "2026-09-03T22:00:00Z"}],
                              "recent": [{"ts": "2026-09-03T21:00:00Z", "node": "!bb000002", "kind": "silent", "text": "Tracker2 silent for 45 min", "state": "cleared", "cleared": "2026-09-03T21:30:00Z"}],
                              "settings": {"silent_min": 30, "battery_pct": 20, "unknown": True, "fence_m": 0, "to_tak": True}},
                   "alert_settings": {"silent_min": 30, "battery_pct": 20, "unknown": True, "fence_m": 0, "to_tak": True},
                   "alert_set": {"written": {k: req.get(k) for k in ("silent_min", "battery_pct", "unknown", "fence_m", "to_tak") if req.get(k) is not None}, "confirmed": True},
                   "alert_test": {"sent": True, "observe": False, "note": "a GeoChat to All Chat Rooms"}}[op]
            self.calls.append((op, dict(req)))
            c.sendall((json.dumps(rep) + "\n").encode()); c.close(); return
        if op in ("fences", "fence_set", "fence_delete"):
            rep = {"fences": {"fences": [{"id": "f1a2b3c4", "name": "Compound", "kind": "polygon", "points": [[51.21, -1.51], [51.21, -1.49], [51.19, -1.49], [51.19, -1.51]], "rule": "both", "group": "", "enabled": True, "created": "2026-09-03T02:00:00Z"}]},
                   "fence_set": {"id": req.get("id") or "0badf00d", "fence": {"id": req.get("id") or "0badf00d", "name": req.get("name"), "kind": req.get("kind") or "polygon", "rule": req.get("rule") or "both"}, "confirmed": True},
                   "fence_delete": {"removed": req.get("id"), "confirmed": True}}[op]
            self.calls.append((op, dict(req)))
            c.sendall((json.dumps(rep) + "\n").encode()); c.close(); return
        if op in ("groups", "group_set", "group_delete"):
            gs = sorted({n.get("group") for n in NODES if n.get("group")})
            rep = {"groups": {"groups": [{"name": g, "icon": next((n.get("icon") for n in NODES if n.get("group") == g), "radio"), "count": sum(1 for n in NODES if n.get("group") == g), "declared": True} for g in gs], "icons": ["radio", "person", "vehicle", "router", "repeater", "base", "drone", "boat", "bike", "dog", "box", "medic", "flag", "star"]},
                   "group_set": {"group": {"name": req.get("name"), "icon": req.get("icon") or "radio"}, "confirmed": True},
                   "group_delete": {"removed": req.get("name"), "cleared": [], "confirmed": True}}[op]
            self.calls.append((op, dict(req)))
            c.sendall((json.dumps(rep) + "\n").encode()); c.close(); return
        if op in ("inventory", "key_accept", "peers", "peer_invite", "peer_join", "peer_forget", "peer_sharing_set"):
            rep = {"peers": {"site": {"id": "ab" * 32, "short": "abababababab", "name": "Dev hub", "address": "dev.example", "listening": True, "port": 8094},
                             "peers": [{"id": "cd" * 32, "name": "Edge laptop", "state": "connected", "direction": "in", "since": "2026-09-03T01:00:00Z", "last_seen": "2026-09-03T02:00:00Z", "added": "2026-09-01T00:00:00Z", "nodes": 1, "sharing": {"nodes": {"out": True, "in": True}, "messages": {"out": False, "in": True, "channels": []}, "waypoints": {"out": True, "in": True}, "alerts": {"out": True, "in": True}}, "note": None}],
                             "invites": [], "pictures": [{"origin": "cd" * 32, "name": "Edge laptop", "nodes": 1, "ts": "2026-09-03T02:00:00Z"}]},
                   "peer_invite": {"invite": "dev.example:8094/ABCD2345/" + "ab" * 32, "code": "ABCD2345", "expires": "2026-09-03T02:10:00Z", "fingerprint": "ab" * 32, "qr_svg": None, "note": "read once, good for 10 minutes, one use"},
                   "peer_join": {"joined": True, "site": "ef" * 32, "name": "Far hub", "confirmed": True}, "peer_forget": {"forgotten": True, "site": req.get("site")},
                   "peer_sharing_set": {"written": {"out": True, "in": True}, "site": req.get("site"), "cls": req.get("cls"), "confirmed": True},
                   "inventory": {"rows": [
                        {"id": "!aa000001", "name": "Tracker9", "hw": "TRACKER_T1000_E", "firmware": "2.6.11", "role": "TRACKER", "fingerprint": "3f9a1c0d7e2b", "key_since": "2026-09-02T10:00:00Z", "key_changed": None, "key_ack": None, "key_alarm": False, "managed": True, "behind": False, "behind_reason": "on the shelf's version", "confirmed": "2026-09-03T03:00:00Z", "heard": "2026-09-03T02:00:00Z"},
                        {"id": "!bb000002", "name": "Tracker2", "hw": "TRACKER_T1000_E", "firmware": "2.5.20", "role": None, "fingerprint": "aa11bb22cc33", "key_since": "2026-09-01T08:00:00Z", "key_changed": "2026-09-03T01:00:00Z", "key_ack": None, "key_alarm": True, "managed": False, "behind": True, "behind_reason": "behind the shelf's 2.6.11", "confirmed": "2026-09-03T01:00:00Z", "heard": "2026-09-03T01:50:00Z"},
                        {"id": "!cc000003", "name": "OldTracker", "hw": "HELTEC_V3", "firmware": None, "role": None, "fingerprint": None, "key_since": None, "key_changed": None, "key_ack": None, "key_alarm": False, "managed": False, "behind": None, "behind_reason": "firmware unknown: read the device on the bench or over the air", "confirmed": None, "heard": None}],
                        "count": 3, "behind": 1, "key_alarms": 1},
                   "key_accept": {"accepted": req.get("id"), "confirmed": True}}[op]
            self.calls.append((op, dict(req)))
            c.sendall((json.dumps(rep) + "\n").encode()); c.close(); return
        if op in ("channel_url", "box_position_set"):
            rep = {"channel_url": {"index": int(req.get("index") or 0), "name": "MILUX-TAK" if int(req.get("index") or 0) == 0 else "SECOND", "url": "https://meshtastic.org/e/#FAKE-SECRET-URL-WITH-KEY"},
                   "box_position_set": ({"cleared": True, "confirmed": True} if req.get("clear") == "on" else {"written": {"lat": req.get("lat"), "lon": req.get("lon")}, "confirmed": True, "source": "declared"})}[op]
            self.calls.append((op, dict(req)))
            c.sendall((json.dumps(rep) + "\n").encode()); c.close(); return
        if op in ("survey_start", "survey_stop", "survey_status"):
            rep = {"survey_start": {"started": True, "dest": req.get("dest"), "interval": int(req.get("interval") or 15), "minutes": int(req.get("minutes") or 10)},
                   "survey_stop": {"stopped": True, "dest": "!aa000001", "asked": 3}, "survey_status": {"running": False, "asked": 3, "answers": 2}}[op]
            self.calls.append((op, dict(req)))
            c.sendall((json.dumps(rep) + "\n").encode()); c.close(); return
        if op in ("send_text", "traceroute", "request_position"):
            args = dict(req)
            if op == "send_text":
                args = {"text": req.get("text"), "channel": int(req.get("channel", 0)), "to": req.get("to") or "^all"}
            self.calls.append((op, args))
            rep = {"sent": req.get("text")} if op == "send_text" else {"requested": op, "dest": req.get("dest")}
        else:
            rep = {"status": STATUS, "nodes": {"nodes": NODES, "count": len(NODES)}, "channels": CHANNELS,
                   "log": {"lines": ["[02:00:01] INFO Connected to the Meshtastic Device", "[02:00:02] DEBUG DEBUG | ??:??:?? 12 [Router] Rx from 0xaa000001"], "total": 2}, "config": CONFIG,
                   "node": {"node": NODES[0]}, "links": links_now(), "register": REGISTER, "node_read": dict(NODE_READ, id=req.get("id")),
                   "node_set": {"written": [k for k in ("long_name", "short_name", "tx_power", "position_broadcast_secs") if req.get(k) is not None], "confirmed": True, "sent": "2026-09-03T03:00:00Z",
                                "read_back": {k: req.get(k) for k in ("long_name", "short_name", "tx_power", "position_broadcast_secs") if req.get(k) is not None}},
                   "node_set_region": {"written": ["region"], "confirmed": True, "sent": "2026-09-03T03:00:00Z", "read_back": {"region": req.get("region")}},
                   "node_channel_push": {"written": ["channel"], "confirmed": True, "sent": "2026-09-03T03:00:00Z", "read_back": {"index": req.get("index"), "name": "MILUX-TAK"}},
                   "node_reboot": {"asked": "2026-09-03T03:00:00Z", "id": req.get("id"), "secs": 10}, "bench_devices": BENCH, "bench_read": BENCH_READ,
                   "register_set": {"written": {"id": req.get("id"), "label": req.get("label"), "holder": req.get("holder"), "group": req.get("group"), "tags": req.get("tags"), "icon": req.get("icon")}, "confirmed": True},
                   "bench_export": {"export": "/var/lib/vantage-mesh/exports/!ee000005/2026-09-03T03-00-00Z.json", "bytes": 1840, "id": "!ee000005"},
                   "bench_exports": dict(EXPORTS, id=req.get("id")), "firmware_shelf": SHELF,
                   "bench_restore": {"written": ["owner", "channels", "lora", "device", "position"], "confirmed": True, "sent": "2026-09-03T03:00:00Z", "read_back": {"long_name": "New Device", "channels": 1}},
                   "bench_flash": {"stages": ["exported", "in bootloader", "copied", "back", "version read"], "confirmed": True, "sent": "2026-09-03T03:00:00Z", "version": "2.6.11", "export": "/var/lib/vantage-mesh/exports/!ee000005/2026-09-03T03-00-00Z.json"},
                   "bench_onboard": {"written": ["long_name", "short_name", "role", "channel0", "lora", "admin_key"], "confirmed": True, "sent": "2026-09-03T03:00:00Z",
                                     "read_back": {"long_name": req.get("long_name"), "short_name": req.get("short_name"), "role": req.get("role"), "channel0": "MILUX-TAK", "region": "EU_868", "modem_preset": "SHORT_FAST", "managed": True},
                                     "export": "/var/lib/vantage-mesh/exports/!ee000005/2026-09-03T03-00-00Z.json", "register": {"id": "!ee000005", "managed": True}},
                   "route": {"route": LINKS["routes"].get(str(req.get("id") or ""))},
                   "availability": AVAILABILITY, "neighbors": NEIGHBORS, "waypoints": WAYPOINTS,
                   "waypoint_send": {"sent": True, "name": req.get("name"), "wid": 5151},
                   "quick_messages": None,
                   "history": {"kind": req.get("kind") or "positions", "rows": [r for r in HISTORY.get(req.get("kind") or "positions", []) if (not req.get("node") or r.get("node") == req.get("node")) and (not req.get("since") or r.get("ts", "") >= str(req.get("since")))][-int(req.get("limit") or 500):], "count": 0},
                   "health": {"hours": int(req.get("hours") or 24), "region": "EU_868", "budget_pct": 10.0, "chutil": 12.5, "airutil": 0.8, "verdict": "normal", "air_share": 8.0, "packets": 240, "packets_per_hour": 10.0, "nodes_heard": 2,
                              "nodes": [{"id": "!aa000001", "name": "Tracker9", "packets": 200, "per_hour": 8.3, "chutil": 11.0, "airutil": 0.5, "battery": 77, "last_telemetry": "2026-09-03T21:50:00Z", "own": False},
                                        {"id": "!ee000025", "name": "this box", "packets": 40, "per_hour": 1.7, "chutil": 12.5, "airutil": 0.8, "battery": 101, "last_telemetry": "2026-09-03T21:55:00Z", "own": True}],
                              "hourly": [{"hour": "2026-09-03T19:00Z", "chutil": 8.0}, {"hour": "2026-09-03T20:00Z", "chutil": 14.5}, {"hour": "2026-09-03T21:00Z", "chutil": 12.5}]},
                   "history_summary": {"ok": True, "path": "/var/lib/vantage-mesh/history.db", "days": 30, "bytes": 24576,
                                       "tables": {k: {"rows": len(v), "oldest": (v[0]["ts"] if v else None), "newest": (v[-1]["ts"] if v else None)} for k, v in HISTORY.items()}}}.get(op, {"error": f"unknown op {op}"})
        c.sendall((json.dumps(rep) + "\n").encode()); c.close()

    def emit(self, ev):
        for c in list(self.clients):
            try:
                c.sendall((json.dumps(ev) + "\n").encode())
            except OSError:
                self.clients.remove(c)


HISTORY = {
    "messages": [{"ts": "2026-09-03T21:58:00Z", "node": "!aa000001", "name": "Tracker9", "dest": "^all", "channel": 0, "text": "stored before the restart", "snr": 9.5}],
    "positions": [{"ts": "2026-09-03T21:50:00Z", "node": "!aa000001", "lat": 51.2, "lon": -1.5, "snr": 9.5, "hops": 0},
                  {"ts": "2026-09-03T21:55:00Z", "node": "!aa000001", "lat": 51.2004, "lon": -1.5006, "snr": 8.0, "hops": 0}],
    "telemetry": [{"ts": "2026-09-03T20:50:00Z", "node": "!aa000001", "level": 90, "voltage": 4.1, "chutil": 2.0, "airutil": 0.3, "uptime": 400},
                  {"ts": "2026-09-03T21:20:00Z", "node": "!aa000001", "level": 85, "voltage": 4.05, "chutil": 2.5, "airutil": 0.35, "uptime": 700},
                  {"ts": "2026-09-03T21:50:00Z", "node": "!aa000001", "level": 80, "voltage": 4.0, "chutil": 3.1, "airutil": 0.4, "uptime": 1000},
                  {"ts": "2026-09-03T21:55:00Z", "node": "!bb000002", "level": 50, "voltage": 3.8, "chutil": 1.0, "airutil": 0.1, "uptime": 10}],
    "packets": [{"ts": "2026-09-03T21:50:00Z", "node": "!aa000001", "port": "POSITION_APP", "snr": 9.5, "hops": 0, "size": 20}],
    "environment": [], "waypoints": [], "neighbors": [],
}
NEIGHBORS = {"hours": 24, "edges": [{"from": "!aa000001", "from_name": "Tracker9", "to": "!bb000002", "to_name": "Tracker2", "snr": 7.5, "ts": "2026-09-03T21:50:00Z"}]}
AVAILABILITY = {"hours": 24, "bucket_secs": 3600, "buckets": 24, "nodes": [
    {"id": "!aa000001", "name": "Tracker 9 (recce)", "buckets": 24, "heard": 21, "pct": 88, "bucket_secs": 3600, "series": [1] * 21 + [0] * 3},
    {"id": "!bb000002", "name": "Tracker2", "buckets": 24, "heard": 2, "pct": 8, "bucket_secs": 3600, "series": [0] * 22 + [1, 1]}]}
WAYPOINTS = {"waypoints": [{"wid": 7777, "node": "!ee000099", "name": "Far RV", "description": "from the edge", "lat": 51.45, "lon": -0.97, "expire": 4102444800, "ts": "2026-09-03T02:00:00Z", "origin": "cd" * 32, "origin_name": "Edge laptop"}, {"wid": 4242, "node": "!aa000001", "name": "RV Alpha", "description": "meet here", "lat": 51.2015, "lon": -1.4985, "expire": 4102444800, "ts": "2026-09-03T21:40:00Z"}]}


def start_fake_bridge():
    return FakeBridge()
