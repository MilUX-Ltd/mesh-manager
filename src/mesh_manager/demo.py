#!/usr/bin/env python3
"""A demo bridge: the screen with no radio behind it (docs/DEMO.md).

It answers every action the real bridge does, with made-up devices and a few hours of one
tracker's morning, so the map, the node pages and the health page have something to show.
Nothing here touches hardware; every write is answered, none is sent.

    python3 -m mesh_manager.demo /tmp/mm-demo.sock
"""
import json
import secrets
import os
import random
import socket
import sys
import threading
import time

from mesh_manager import channel as CH

PATH = sys.argv[1] if len(sys.argv) > 1 else "/tmp/fake-bridge.sock"
NODES = [
    {"id": "!ee000004", "name": "Tracker 4", "battery": 81, "lat": 51.500000, "lon": -0.120000, "heard": "2026-09-03T01:23:55Z", "snr": 13.0, "hops": 0, "heard_here": True, "hw": "TRACKER_T1000_E", "short": "TR4"},
    {"id": "!ee000002", "name": "Tracker 2", "battery": 9, "lat": 51.500180, "lon": -0.119700, "heard": "2026-09-03T01:20:11Z", "snr": 9.5, "hops": 0, "heard_here": True, "hw": "TRACKER_T1000_E", "short": "TR2"},
    {"id": "!ee000003", "name": "Handset", "battery": 29, "lat": 51.499910, "lon": -0.120400, "heard": "2026-09-03T01:22:16Z", "snr": 11.25, "hops": 0, "heard_here": True, "hw": "RAK4631", "short": "HAND"},
    {"id": "!ee000006", "name": "Relay 2", "battery": None, "heard": None, "snr": None, "hops": None, "heard_here": False, "hw": "TRACKER_T1000_E", "short": "T2"},
    {"id": "!ee000007", "name": "Edge tracker", "battery": 64, "lat": 51.4520, "lon": -0.9781, "heard": "2026-09-05T09:20:00Z", "snr": 7.5, "hops": 0, "heard_here": True, "hw": "TRACKER_T1000_E", "short": "EDG1", "remote": True, "origin": "ed" * 32, "origin_name": "Edge laptop"},  # Spec 052: a peer's picture
]
STATUS = {"version": "0.1.0", "uptime": 5400, "radio": "/dev/serial/by-id/usb-Espressif_USB_JTAG_serial_debug_unit_A4:CB:8F:EE:00:01-if00",
          "radio_present": True, "bootloader": False, "connected": True, "last_activity": "2026-09-03T01:24:06Z",
          "last_forwarded": "2026-09-03T01:24:06Z", "nodes_seen": sum(1 for n in NODES if n.get("heard_here", True)), "nodes_db": len(NODES), "observe": False,
          "own": {"id": "!ee000001", "name": "Gateway", "short": "TAKG"}, "region": "EU_868", "modem_preset": "SHORT_FAST",
          "gps": {"reachable": True, "fix": True, "seen": 11, "used": 8, "checked": "2026-09-03T01:24:00Z", "via": "gpsd://127.0.0.1:2947"},
          "primary_channel": "MESH-DEMO", "watchdog": "pinging", "state_dir": "/var/lib/vantage-mesh", "socket": PATH}
DEMO_MODE = os.environ.get("MODE", "").strip().lower() if os.environ.get("MODE", "").strip().lower() in ("server", "hub", "desktop") else "tak-server"   # Spec 050 and 052: MODE=server or MODE=hub shapes the demo
STATUS["mode"] = DEMO_MODE; STATUS["tak"] = "off" if DEMO_MODE in ("server", "hub", "desktop") else "on"
STATUS["site"] = {"id": "ee" * 32, "name": "Demo box"}; STATUS["peers"] = 1; STATUS["peer_port"] = 8094; STATUS["peer_bind"] = "0.0.0.0"   # Spec 052
if DEMO_MODE in ("server", "hub", "desktop"):
    STATUS["last_forwarded"] = None
if DEMO_MODE == "hub":
    STATUS["radio"] = None; STATUS["radio_present"] = False; STATUS["connected"] = False; STATUS["site"] = {"id": "ee" * 32, "name": "Demo hub"}
CHANNELS = {"channels": [{"index": 0, "name": "MESH-DEMO", "role": "PRIMARY", "has_key": True},
                         {"index": 1, "name": "", "role": "DISABLED", "has_key": False}],
            "url": "https://meshtastic.org/e/#CgcSAQEoATABEg8IATgBQANIAVAeaAHABgE"}
LOG = ["[01:23:55] INFO Sending <event uid=\"!ee000004\" type=\"a-f-G-U-C\"> Tracker 4",
       "[01:24:02] INFO [radio] AGC reset (fixed) DC Cal. Mode 1",
       "[01:24:06] INFO Sending <event uid=\"ANDROID-7b40d9b887a4ec20\" callsign=\"MilUX\"> (TAK V2, port 78)"]
clients = []
HISTORY = {n["id"]: [] for n in NODES}
for _n in NODES[:3]:
    for _k in range(12):
        HISTORY[_n["id"]].append([time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - (12 - _k) * 60)), round(random.uniform(5, 13), 1), 0])
ROUTES = {"!ee000004": {"dest": "!ee000004", "ts": "2026-09-03T01:20:00Z", "hops": 1,
                        "towards": [{"id": "!ee000002", "name": "Tracker 2", "snr": 9.5}, {"id": "!ee000004", "name": "Tracker 4", "snr": 6.25}],
                        "back": [{"id": "!ee000002", "name": "Tracker 2", "snr": 8.0}, {"id": "!ee000001", "name": "Gateway", "snr": None}]}}


REG = {"!ee000004": {"label": "Tracker 4", "holder": "the operator", "managed": True, "firmware": "2.6.11", "role": "TRACKER", "managed_at": "2026-09-02T10:00:00Z"},
       "!ee000003": {"label": "Handset", "holder": "the operator", "managed": False}}


def register():
    rows = []
    for n in NODES:
        r = REG.get(n["id"], {})
        rows.append(dict(n, label=r.get("label", ""), holder=r.get("holder", ""), firmware=r.get("firmware"), role=r.get("role"), managed=bool(r.get("managed")), managed_at=r.get("managed_at")))
    for nid, r in REG.items():
        if nid not in {n["id"] for n in NODES}:
            rows.append({"id": nid, "name": r.get("name") or nid, "heard_here": False, "heard": None, "label": r.get("label", ""), "holder": r.get("holder", ""), "hw": r.get("hw"),
                         "firmware": r.get("firmware"), "role": r.get("role"), "managed": bool(r.get("managed")), "managed_at": r.get("managed_at"), "bench_only": True})
    return {"rows": rows, "count": len(rows)}


def links():
    nodes = [dict(n, direct_snr=(n.get("snr") if n.get("hops") == 0 else None), history=HISTORY.get(n["id"], [])) for n in NODES]
    return {"own": {"id": "!ee000001", "name": "Gateway", "lat": 51.5000, "lon": -0.1200, "position_source": "config"}, "nodes": nodes, "routes": ROUTES}


def answer_traceroute(dest):
    time.sleep(2.5)
    node = next((n for n in NODES if n["id"] == dest), None)
    rec = {"dest": dest, "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "hops": 0,
           "towards": [{"id": dest, "name": node["name"] if node else dest, "snr": round(random.uniform(4, 13), 2)}],
           "back": [{"id": "!ee000001", "name": "Gateway", "snr": round(random.uniform(4, 13), 2)}]}
    ROUTES[dest] = rec
    for c in list(clients):
        try:
            c.sendall((json.dumps(dict(rec, kind="route")) + "\n").encode())
        except OSError:
            clients.remove(c)


import math as _math
def _demo_history():
    """A morning's worth: Tracker 4 walking a loop, telemetry every ten minutes, a few messages."""
    pos, tel, msg, pk = [], [], [], []
    t0 = time.time() - 3 * 3600
    for i in range(0, 180):
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t0 + i * 60))
        a = i / 180 * 2 * _math.pi
        lat, lon = 51.50000 + 0.004 * _math.sin(a), -0.12001 + 0.006 * _math.cos(a)
        snr = 12 - 9 * abs(_math.sin(a * 2))
        pos.append({"ts": ts, "node": "!ee000004", "lat": round(lat, 6), "lon": round(lon, 6), "snr": round(snr, 1), "hops": 0})
        pk.append({"ts": ts, "node": "!ee000004", "port": "POSITION_APP", "snr": round(snr, 1), "hops": 0, "size": 24})
        if i % 10 == 0:
            tel.append({"ts": ts, "node": "!ee000004", "level": max(5, 90 - i // 2), "voltage": round(4.1 - i * 0.003, 2), "chutil": round(2 + 3 * abs(_math.sin(a)), 1), "airutil": round(0.3 + 0.5 * abs(_math.cos(a)), 2), "uptime": 3600 + i * 60})
        if i in (5, 60, 120):
            msg.append({"ts": ts, "node": "!ee000004", "name": "Tracker 4", "dest": "^all", "channel": 0, "text": ["at the start point", "halfway, all well", "heading back"][[5, 60, 120].index(i)], "snr": round(snr, 1)})
    # a direct exchange too, so Messages has more than one chat: the handset asks, the box answers, the radio confirms
    if msg:
        t_ask = msg[1]["ts"]
        msg.insert(2, {"ts": t_ask, "node": "!ee000003", "name": "Handset", "dest": "!ee000001", "channel": 0, "text": "at the RV early, hold here?", "snr": 9.5})
        msg.insert(3, {"ts": t_ask, "node": "!ee000001", "name": "Gateway", "dest": "!ee000003", "channel": 0, "text": "hold at the RV, we are ten minutes out", "snr": None, "mid": 4242, "ack": "delivered"})
        msg.insert(4, {"ts": t_ask, "node": "!ee000001", "name": "Gateway", "dest": "!ee000002", "channel": 0, "text": "Tracker 2, check in when you can", "snr": None, "mid": 4243, "ack": "MAX_RETRANSMIT"})  # Spec 051: a line the radio gave up on
    return {"positions": pos, "telemetry": tel, "messages": msg, "packets": pk}
DEMO_HISTORY = _demo_history()


DEMO_HISTORY.setdefault("environment", [{"ts": f"2026-09-03T{h:02d}:10:00Z", "node": "!ee000003", "temperature": round(14 + 5 * ((h % 12) / 12), 1), "humidity": float(70 - h), "pressure": 1011.0 + (h % 3), "gas": None, "lux": None, "iaq": None, "wind_dir": None, "wind_speed": None} for h in range(1, 24)])
DEMO_HISTORY.setdefault("waypoints", [])
DEMO_HISTORY.setdefault("neighbors", [])

def answer_nodeinfo(dest):
    """Spec 032: the node answers with what it calls itself, a couple of seconds later."""
    time.sleep(2.0)
    node = next((n for n in NODES if n["id"] == dest), None)
    if not node:
        return
    rec = {"kind": "nodeinfo", "id": dest, "name": node["name"], "short": node.get("short"),
           "hw": node.get("hw"), "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    for c in list(clients):
        try:
            c.sendall((json.dumps(rec) + "\n").encode())
        except OSError:
            pass


def serve_one(c):
    f = c.makefile("rb")
    if not CH.said_hello(f, TOKEN):   # Spec 060
        c.sendall(b'{"error": "not this bridge\'s screen"}\n'); c.close(); return
    try:
        req = json.loads(f.readline().decode())
    except Exception:  # noqa: BLE001
        c.sendall(b'{"error": "bad request"}\n'); c.close(); return
    op = req.get("op")
    if op == "events":
        c.sendall(b'{"kind": "hello"}\n'); clients.append(c)
        # Spec 053: one message that arrived over the link, so the Messages page shows a remote chat
        c.sendall((json.dumps({"kind": "text", "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "from": "!ee000007", "name": "Edge tracker", "to": "^all", "channel": 0,
                               "channel_name": "MILUX-TAK", "text": "at the edge RV, all well", "origin": "ed" * 32, "origin_name": "Edge laptop"}) + "\n").encode()); return
    if op == "traceroute":
        threading.Thread(target=answer_traceroute, args=(req.get("dest"),), daemon=True).start()
        c.sendall((json.dumps({"requested": "traceroute", "dest": req.get("dest"), "asked": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}) + "\n").encode()); c.close(); return
    if op == "request_telemetry":
        c.sendall((json.dumps({"requested": "telemetry", "dest": req.get("dest")}) + "\n").encode()); c.close(); return
    if op in ("survey_start", "survey_stop", "survey_status"):
        rep = {"survey_start": {"started": True, "dest": req.get("dest"), "interval": int(req.get("interval") or 15), "minutes": int(req.get("minutes") or 10)},
               "survey_stop": {"stopped": True, "dest": "!ee000004", "asked": 4}, "survey_status": {"running": False}}[op]
        c.sendall((json.dumps(rep) + "\n").encode()); c.close(); return
    if op == "request_nodeinfo":
        threading.Thread(target=answer_nodeinfo, args=(req.get("dest"),), daemon=True).start()
        c.sendall((json.dumps({"requested": "nodeinfo", "dest": req.get("dest"), "asked": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}) + "\n").encode()); c.close(); return
    if op == "health":
        tel = DEMO_HISTORY["telemetry"]; hourly = {}
        for r in tel: hourly.setdefault(r["ts"][:13], []).append(r["chutil"])
        c.sendall((json.dumps({"hours": 24, "region": "EU_868", "budget_pct": 10.0, "chutil": tel[-1]["chutil"], "airutil": tel[-1]["airutil"], "verdict": "normal", "air_share": round(tel[-1]["airutil"] / 10 * 100, 1), "packets": len(DEMO_HISTORY["packets"]), "packets_per_hour": round(len(DEMO_HISTORY["packets"]) / 24, 1), "nodes_heard": 1,
                               "nodes": [{"id": "!ee000004", "name": "Tracker 4", "packets": len(DEMO_HISTORY["packets"]), "per_hour": round(len(DEMO_HISTORY["packets"]) / 24, 1), "chutil": tel[-1]["chutil"], "airutil": tel[-1]["airutil"], "battery": tel[-1]["level"], "last_telemetry": tel[-1]["ts"], "own": False}],
                               "hourly": [{"hour": k + ":00Z", "chutil": round(sum(v) / len(v), 1)} for k, v in sorted(hourly.items())]}) + "\n").encode()); c.close(); return
    if op in ("profile", "profile_set", "drift", "drift_fix"):
        prof = {"role": "TRACKER", "tx_power": 20, "position_broadcast_secs": 900, "region": "EU_868", "modem_preset": "SHORT_FAST"}
        rep = {"profile": prof, "profile_set": {"written": {k: req.get(k) for k in prof}, "confirmed": True},
               "drift": {"profile": prof, "enforced": list(prof), "counts": {"in_line": 1, "drifted": 1, "unread": 2},
                         "devices": [{"id": "!ee000004", "name": "Tracker 4", "state": "in line", "diffs": [], "read_at": "2026-09-03T10:00:00Z", "managed": True},
                                     {"id": "!ee000002", "name": "Tracker 2", "state": "drifted", "diffs": [{"field": "tx_power", "is": 27, "should": 20}, {"field": "position_broadcast_secs", "is": 300, "should": 900}], "read_at": "2026-09-03T10:05:00Z", "managed": True},
                                     {"id": "!ee000003", "name": "Handset", "state": "unread", "diffs": [], "managed": False},
                                     {"id": "!ee000006", "name": "Relay 2", "state": "unread", "diffs": [], "managed": False}]},
               "drift_fix": {"id": req.get("id"), "safe": {"written": ["tx_power", "position_broadcast_secs"], "confirmed": True, "read_back": {"tx_power": 20, "position_broadcast_secs": 900}}, "hard": None, "skipped": [], "confirmed": True}}[op]
        c.sendall((json.dumps(rep) + "\n").encode()); c.close(); return
    if op in ("rotation_status", "rotation_mark"):
        rep = {"rotation_status": {"rotation": {"ts": DEMO_HISTORY["messages"][0]["ts"], "index": 0, "name": "MESH-DEMO", "source": "rotated from the screen", "note": None},
                                   "back": [{"id": "!ee000004", "name": "Tracker 4", "heard": DEMO_HISTORY["messages"][1]["ts"]}, {"id": "!ee000003", "name": "Handset", "heard": DEMO_HISTORY["messages"][2]["ts"]}],
                                   "waiting": [{"id": "!ee000002", "name": "Tracker 2"}, {"id": "!ee000006", "name": "Relay 2"}], "counts": {"expected": 4, "back": 2, "waiting": 2}},
               "rotation_mark": {"marked": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "index": int(req.get("index") or 0), "name": "MESH-DEMO", "expected": 4, "confirmed": True}}[op]
        c.sendall((json.dumps(rep) + "\n").encode()); c.close(); return
    if op in ("alerts", "alert_settings", "alert_set", "alert_test"):
        rep = {"alerts": {"open": [{"node": "!ee000007", "kind": "silent", "text": "Edge tracker silent for 35 min", "since": "2026-09-05T09:40:00Z", "origin": "ed" * 32, "origin_name": "Edge laptop"}], "recent": [{"ts": DEMO_HISTORY["telemetry"][-1]["ts"], "node": "!ee000004", "kind": "battery", "text": "Tracker 4 battery 5%", "state": "open", "cleared": None}], "settings": {"silent_min": 30, "battery_pct": 20, "unknown": True, "fence_m": 0, "to_tak": True}},
               "alert_settings": {"silent_min": 30, "battery_pct": 20, "unknown": True, "fence_m": 0, "to_tak": True},
               "alert_set": {"written": {k: req.get(k) for k in ("silent_min", "battery_pct", "unknown", "fence_m", "to_tak") if req.get(k) is not None}, "confirmed": True},
               "alert_test": {"sent": True, "observe": False, "note": "a GeoChat to All Chat Rooms"}}[op]
        c.sendall((json.dumps(rep) + "\n").encode()); c.close(); return
    if op == "availability":
        rep = {"hours": int(req.get("hours") or 24), "bucket_secs": 3600, "buckets": 24, "nodes": [
            {"id": "!ee000004", "name": "Tracker 4", "buckets": 24, "heard": 23, "pct": 96, "bucket_secs": 3600, "series": [1] * 23 + [0]},
            {"id": "!ee000002", "name": "Tracker 2", "buckets": 24, "heard": 15, "pct": 62, "bucket_secs": 3600, "series": [1, 1, 0, 1, 1, 0, 1, 1, 1, 0, 1, 1, 1, 1, 0, 0, 1, 1, 0, 1, 1, 0, 1, 0]},
            {"id": "!ee000003", "name": "Handset", "buckets": 24, "heard": 6, "pct": 25, "bucket_secs": 3600, "series": [0] * 18 + [1] * 6},
            {"id": "!ee000006", "name": "Relay 2", "buckets": 24, "heard": 0, "pct": 0, "bucket_secs": 3600, "series": [0] * 24}]}
        c.sendall((json.dumps(rep) + "\n").encode()); c.close(); return
    if op == "neighbors":
        rep = {"hours": 24, "own": "!ee000001", "edges": [{"from": "!ee000004", "from_name": "Tracker 4", "to": "!ee000001", "to_name": "Gateway", "snr": 9.5, "ts": "2026-09-03T01:20:00Z"},
                                                          {"from": "!ee000004", "from_name": "Tracker 4", "to": "!ee000002", "to_name": "Tracker 2", "snr": 4.0, "ts": "2026-09-03T01:20:00Z"},
                                                          {"from": "!ee000002", "from_name": "Tracker 2", "to": "!ee000003", "to_name": "Handset", "snr": 6.25, "ts": "2026-09-03T01:18:00Z"},
                                                          {"from": "!ee000003", "from_name": "Handset", "to": "!ee000001", "to_name": "Gateway", "snr": 11.0, "ts": "2026-09-03T01:22:00Z"},
                                                          {"from": "!ee000002", "from_name": "Tracker 2", "to": "!ee000001", "to_name": "Gateway", "snr": 1.5, "ts": "2026-09-03T01:19:00Z"}]}
        c.sendall((json.dumps(rep) + "\n").encode()); c.close(); return
    if op == "waypoints":
        rep = {"waypoints": [{"wid": 4242, "node": "!ee000003", "name": "RV Alpha", "description": "meet here", "lat": 51.5012, "lon": -0.1188, "expire": 4102444800, "ts": "2026-09-03T01:15:00Z"},
                             {"wid": 7777, "node": "!ee000007", "name": "Edge RV", "description": "from the edge", "lat": 51.4535, "lon": -0.9760, "expire": 4102444800, "ts": "2026-09-05T09:25:00Z", "origin": "ed" * 32, "origin_name": "Edge laptop"}], "count": 2}
        c.sendall((json.dumps(rep) + "\n").encode()); c.close(); return
    if op == "send_text":  # Spec 051: the demo sends too, so the composer, the picker and Send again can be tried with no radio
        text = str(req.get("text") or ""); to = str(req.get("to") or "^all"); ch = int(req.get("channel") or 0)
        if not text.strip():
            c.sendall(b'{"error": "empty message"}\n'); c.close(); return
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()); mid = 5000 + len(DEMO_HISTORY["messages"])
        everyone = to in ("^all", "!ffffffff")
        DEMO_HISTORY["messages"].append({"ts": now, "node": "!ee000001", "name": "Gateway", "dest": to, "channel": ch, "text": text, "snr": None, "mid": mid, "ack": None if everyone else "delivered"})
        ev = json.dumps({"kind": "text", "ts": now, "from": "!ee000001", "name": "Gateway", "to": to, "channel": ch, "text": text, "mid": mid, "sent": True, "ack": None if everyone else "delivered"}) + "\n"
        for k in list(clients):
            try:
                k.sendall(ev.encode())
            except OSError:
                clients.remove(k)
        c.sendall((json.dumps({"sent": True, "mid": mid, "to": to, "channel": ch, "asked": now}) + "\n").encode()); c.close(); return
    if op == "waypoint_send":
        c.sendall((json.dumps({"sent": True, "name": req.get("name"), "wid": 5151, "asked": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}) + "\n").encode()); c.close(); return
    if op == "history":
        kind = req.get("kind") or "positions"
        rows = DEMO_HISTORY.get(kind, [])
        if req.get("node"): rows = [r for r in rows if r.get("node") == req.get("node")]
        c.sendall((json.dumps({"kind": kind, "rows": rows[-int(req.get("limit") or 500):], "count": len(rows)}) + "\n").encode()); c.close(); return
    if op == "history_summary":
        c.sendall((json.dumps({"ok": True, "path": "/var/lib/vantage-mesh/history.db", "days": 30, "bytes": 1200000, "tables": {k: {"rows": len(v), "oldest": (v[0]["ts"] if v else None), "newest": (v[-1]["ts"] if v else None)} for k, v in DEMO_HISTORY.items()}}) + "\n").encode()); c.close(); return
    if op == "request_position":
        c.sendall((json.dumps({"requested": "position", "dest": req.get("dest")}) + "\n").encode()); c.close(); return
    if op == "register_set":
        REG.setdefault(req.get("id"), {}).update({k: req.get(k) for k in ("label", "holder", "note") if req.get(k) is not None})
        c.sendall((json.dumps({"written": {"id": req.get("id"), **REG.get(req.get("id"), {})}, "confirmed": True}) + "\n").encode()); c.close(); return
    if op == "bench_onboard":
        time.sleep(3)
        rep = {"written": ["long_name", "short_name", "role", "channel0", "lora", "admin_key"], "confirmed": True, "sent": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
               "read_back": {"long_name": req.get("long_name"), "short_name": req.get("short_name"), "role": req.get("role"), "channel0": "MESH-DEMO", "region": "EU_868", "modem_preset": "SHORT_FAST", "managed": True, "admin_keys": 1},
               "export": "/var/lib/vantage-mesh/exports/!ee000005/2026-09-03T04-00-00Z.json", "register": {"id": "!ee000005", "managed": True, "label": req.get("long_name")}}
        REG["!ee000005"] = {"label": req.get("long_name"), "holder": "", "managed": True, "name": req.get("long_name"), "hw": "TRACKER_T1000_E", "firmware": "2.6.11", "role": req.get("role"), "onboarded_at": rep["sent"], "managed_at": rep["sent"]}
        c.sendall((json.dumps(rep) + "\n").encode()); c.close(); return
    if op == "node_read":
        time.sleep(2.5)
        node = next((n for n in NODES if n["id"] == req.get("id")), None)
        rep = ({"id": req.get("id"), "long_name": node["name"], "short_name": node.get("short") or node["name"][:4].upper(), "region": "EU_868", "modem_preset": "SHORT_FAST", "role": "TRACKER",
                "tx_power": 27, "position_broadcast_secs": 900, "managed": bool(REG.get(req.get("id"), {}).get("managed")), "admin_keys": 1, "firmware": "2.6.11",
                "read_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "channels": [{"index": 0, "name": "MESH-DEMO", "role": "PRIMARY", "has_key": True}], "missing": []}
               if node else {"error": f"{req.get('id')} did not answer over the air within 30 s"})
        c.sendall((json.dumps(rep) + "\n").encode()); c.close(); return
    if op in ("node_set", "node_set_region", "node_channel_push", "node_reboot"):
        if not REG.get(req.get("id"), {}).get("managed"):
            c.sendall((json.dumps({"error": f"{req.get('id')} is not managed: bring it to the bench first"}) + "\n").encode()); c.close(); return
        time.sleep(4)
        sent = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        if op == "node_reboot":
            rep = {"asked": sent, "id": req.get("id"), "secs": 10, "note": "asked; watch for it to be heard again"}
        else:
            fields = {"node_set": ("long_name", "short_name", "tx_power", "position_broadcast_secs"), "node_set_region": ("region", "modem_preset", "role"), "node_channel_push": ("index",)}[op]
            written = [k for k in fields if req.get(k) not in (None, "")]
            if op == "node_set" and req.get("long_name"):
                for n in NODES:
                    if n["id"] == req.get("id"):
                        n["name"] = req["long_name"]
            rb = {k: req.get(k) for k in written}
            if op == "node_channel_push":
                rb = {"index": req.get("index"), "name": "MESH-DEMO" if int(req.get("index") or 0) == 0 else "DEMO-2", "role": "PRIMARY" if int(req.get("index") or 0) == 0 else "SECONDARY", "has_key": True}
            rep = {"written": written, "sent": sent, "confirmed": True, "read_back": rb, "unconfirmed": None}
        c.sendall((json.dumps(rep) + "\n").encode()); c.close(); return
    if op == "firmware_shelf":
        rep = {"dir": "/var/lib/vantage-mesh/firmware", "images": [
            {"id": "t1000e-2.6.11", "hw": ["TRACKER_T1000_E"], "version": "2.6.11", "method": "uf2", "file": "firmware-tracker-t1000-e-2.6.11.60ec05e.uf2", "recommended": True, "state": "verified", "path": "/var/lib/vantage-mesh/firmware/firmware-tracker-t1000-e-2.6.11.60ec05e.uf2", "note": "The fleet's tracker firmware. Later versions lost the GPS fix on the T1000-E (the regression check of LG9); stay here until a version is proven on the map."},
            {"id": "t1000e-2.7.26", "hw": ["TRACKER_T1000_E"], "version": "2.7.26", "method": "uf2", "file": "firmware-tracker-t1000-e-2.7.26.54e0d8d.uf2", "recommended": False, "state": "missing", "path": "/var/lib/vantage-mesh/firmware/firmware-tracker-t1000-e-2.7.26.54e0d8d.uf2", "note": "Held for the regression check only."},
            {"id": "heltec-v4-2.7.26", "hw": ["HELTEC_V4"], "version": "2.7.26", "method": "esptool", "file": "firmware-heltec-v4-2.7.26.54e0d8d.bin", "recommended": True, "state": "verified", "path": "/var/lib/vantage-mesh/firmware/firmware-heltec-v4-2.7.26.54e0d8d.bin"},
            {"id": "factory-erase-s140-7.3.0", "hw": ["TRACKER_T1000_E", "RAK4631"], "version": "erase 7.3.0", "method": "uf2", "file": "factory-erase-S140-7.3.0.uf2", "recommended": False, "state": "wrong", "path": "/var/lib/vantage-mesh/firmware/factory-erase-S140-7.3.0.uf2", "note": "The nRF52 factory erase: the recovery step before the pinned image."}]}
        c.sendall((json.dumps(rep) + "\n").encode()); c.close(); return
    if op == "bench_exports":
        rep = {"id": req.get("id"), "exports": [{"path": f"/var/lib/vantage-mesh/exports/{req.get('id')}/2026-09-03T03-00-00Z.json", "when": "2026-09-03T03-00-00Z", "bytes": 1840},
                                                {"path": f"/var/lib/vantage-mesh/exports/{req.get('id')}/2026-09-02T18-20-00Z.json", "when": "2026-09-02T18-20-00Z", "bytes": 1812}]}
        c.sendall((json.dumps(rep) + "\n").encode()); c.close(); return
    if op == "bench_restore":
        time.sleep(3)
        rep = {"written": ["owner", "lora", "device", "position", "channels"], "confirmed": True, "sent": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
               "read_back": {"long_name": "New Device", "short_name": "NEW", "region": "EU_868", "modem_preset": "SHORT_FAST", "role": "TRACKER", "channels": 2}, "unconfirmed": None}
        c.sendall((json.dumps(rep) + "\n").encode()); c.close(); return
    if op == "bench_flash":
        def flash_answer():
            time.sleep(8)
            try:
                c.sendall((json.dumps({"stages": ["exported", "in bootloader", "copied", "back", "version read"], "confirmed": True, "sent": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                       "version": "2.6.11", "export": "/var/lib/vantage-mesh/exports/!ee000005/2026-09-03T04-00-00Z.json"}) + "\n").encode())
            except OSError:
                pass
            c.close()
        threading.Thread(target=flash_answer, daemon=True).start()
        for stage in ("exported", "in bootloader", "copied", "back", "version read"):
            time.sleep(1.5)
            ev = {"kind": "flash", "id": "!ee000005", "stage": stage, "image": req.get("image")}
            if stage == "version read":
                ev["version"] = "2.6.11"
            for cc in list(clients):
                try:
                    cc.sendall((json.dumps(ev) + "\n").encode())
                except OSError:
                    clients.remove(cc)
        return
    if op in ("bench_read", "bench_export"):
        time.sleep(2)
        rep = ({"path": req.get("path"), "id": "!ee000005", "long_name": REG.get("!ee000005", {}).get("name") or "New Device", "short_name": "NEW", "hw": "TRACKER_T1000_E", "firmware": "2.6.11",
                "region": "UNSET", "modem_preset": "LONG_FAST", "role": "CLIENT", "managed": bool(REG.get("!ee000005", {}).get("managed")), "admin_keys": 1 if REG.get("!ee000005", {}).get("managed") else 0,
                "channels": [{"index": 0, "name": "LongFast", "role": "PRIMARY", "has_key": True}],
                "position": {"enabled": True, "fix": True, "lat": 51.500600, "lon": -0.119200, "alt": 21, "sats": 9,
                             "mgrs": "30U XC 99928 09420", "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 900)),
                             "state": "a fix"}}
               if op == "bench_read" else {"export": "/var/lib/vantage-mesh/exports/!ee000005/2026-09-03T04-00-00Z.json", "bytes": 1840, "id": "!ee000005"})
        c.sendall((json.dumps(rep) + "\n").encode()); c.close(); return
    rep = {"status": STATUS, "nodes": {"nodes": NODES, "count": len(NODES)}, "channels": CHANNELS, "links": links(), "register": register(),
           "peers": {"site": {"id": "ee" * 32, "short": "eeeeeeeeeeee", "name": "Demo box", "address": "demo.example", "listening": True, "port": 8094},
                     "peers": [{"id": "ed" * 32, "name": "Edge laptop", "state": "connected", "direction": "in", "since": "2026-09-05T09:00:00Z", "last_seen": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "added": "2026-09-05T08:00:00Z", "nodes": 1, "sharing": {"nodes": {"out": True, "in": True}, "messages": {"out": True, "in": True, "channels": [0], "air": True, "air_channel": 0}, "waypoints": {"out": True, "in": True, "air": False}, "alerts": {"out": True, "in": True}}, "aired": {"count": 3, "last": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}, "note": None}],
                     "invites": [], "pictures": [{"origin": "ed" * 32, "name": "Edge laptop", "nodes": 1, "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}]},
           "peer_invite": {"invite": "demo.example:8094/" + "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(8)) + "/" + "ee" * 32, "code": "DEMO", "expires": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 600)), "fingerprint": "ee" * 32, "qr_svg": None, "note": "read once, good for 10 minutes, one use"},
           "peer_join": {"joined": True, "site": "ef" * 32, "name": "Far hub", "confirmed": True}, "peer_forget": {"forgotten": True, "site": req.get("site")},
           "peer_sharing_set": {"written": {"out": True, "in": True}, "site": req.get("site"), "cls": req.get("cls"), "confirmed": True},
           "peer_send_text": {"sent": True, "mid": 8181, "site": req.get("site"), "channel": int(req.get("channel") or 0)},
           "bench_devices": {"gateway": STATUS["radio"], "devices": [
               {"path": "/dev/serial/by-id/usb-Seeed_T1000-E_9F3A-if00", "tty": "ttyACM3", "bootloader": False},
               {"path": "/dev/serial/by-id/usb-RAKwireless_WisCore_RAK4631_Board_BOOT-if00", "tty": "ttyACM4", "bootloader": True,
                "recovery": "double-press reset, copy the pinned firmware UF2 onto the volume that appears, wait for it to come back"}]},
           "route": {"route": ROUTES.get(str(req.get("id") or ""))},
           "config": {"long_name": "Gateway", "short_name": "TAKG", "role": "CLIENT", "region": "EU_868", "modem_preset": "SHORT_FAST", "tx_power": 14},
           "log": {"lines": LOG, "total": len(LOG)}}.get(op, {"error": f"unknown op {op}"})
    c.sendall((json.dumps(rep) + "\n").encode()); c.close()


def ticker():
    while True:
        time.sleep(4)
        n = random.choice(NODES[:3])
        n["snr"] = round(random.uniform(6, 14), 1)
        n["heard"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        HISTORY[n["id"]].append([n["heard"], n["snr"], 0]); del HISTORY[n["id"]][:-200]
        line = f"[{time.strftime('%H:%M:%S')}] INFO Sending <event uid=\"{n['id']}\"> {n['name']}"
        LOG.append(line)
        for ev in ({"kind": "packet", "from": n["id"], "port": "POSITION_APP", "snr": n["snr"], "hops": 0}, {"kind": "log", "line": line}, {"kind": "forwarded"}):
            for c in list(clients):
                try:
                    c.sendall((json.dumps(ev) + "\n").encode())
                except OSError:
                    clients.remove(c)


if os.path.exists(PATH):
    os.unlink(PATH)
srv, TOKEN = CH.listen_raw(PATH)   # Spec 060: a Unix socket, or loopback with a one-run token
threading.Thread(target=ticker, daemon=True).start()
print("fake bridge on", CH.where(PATH), flush=True)
while True:
    c, _ = srv.accept()
    threading.Thread(target=serve_one, args=(c,), daemon=True).start()
