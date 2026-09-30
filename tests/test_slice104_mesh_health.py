#!/usr/bin/env python3
"""Spec 104: airtime and mesh health over time. The Health page counts every packet in
its window, draws the window hour by hour for this radio and for the mesh, says how far packets
travel and how the box's own messages fared; a node's page draws its utilisation and air time.
These are the acceptance tests. They were committed failing, and the build does not edit them."""
import http.client
import os
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(__file__))
from _common import ROOT, check, check_true, finish  # noqa: E402
sys.path.insert(0, os.path.join(ROOT, "src"))
import fakegw_lib  # noqa: E402
fakegw_lib.install()
from fakebridge_lib import start_fake_bridge  # noqa: E402
from mesh_manager import bridge as B, web as W  # noqa: E402
from mesh_manager.history import utc  # noqa: E402

T0 = 1_789_999_200.0            # on the hour, so every bucket below is exact
NOW = T0 + 23 * 3600 + 1800     # half past the twenty-fourth hour of the window
_real_time = time.time
time.time = lambda: NOW

state = tempfile.mkdtemp()
br = B.Bridge({"SERIAL": "/dev/serial/by-id/usb-fake-test-radio-if00", "HISTORY_DAYS": 30}, socket_path=os.path.join(state, "b.sock"),
              state_dir=state, observe=True, gps_reader=False)
h = br.history
own = (br._own() or {}).get("id")
A, BRK = "!aa000001", "!bb000002"
HOPS = [0, 0, 1, 2, 4]

# 6000 packets this radio heard from A over the last twenty hours, ten with no hop count, and 50 a
# broker carried from BRK: more than the 5000 rows a single query returns.
for i in range(6000):
    h.packet(A, port="POSITION_APP", snr=5.0, hops=HOPS[i % 5], size=20, ts=utc(NOW - 20 * 3600 + i * 12), rssi=-90.0, via_mqtt=0)
for i in range(10):
    h.packet(A, port="TEXT_MESSAGE_APP", snr=5.0, hops=None, size=10, ts=utc(NOW - 600 + i), rssi=None, via_mqtt=0)
for i in range(50):
    h.packet(BRK, port="POSITION_APP", ts=utc(NOW - 3600 + i * 60), via_mqtt=1)
# telemetry in one hour, hour 10 of the window: the box's own radio and A
H10 = T0 + 10 * 3600
for off, ch, air in ((600, 10.0, 1.0), (2400, 20.0, 3.0)):
    h.telemetry(own, level=100, voltage=4.2, chutil=ch, airutil=air, ts=utc(H10 + off))
for off, ch in ((900, 4.0), (2700, 6.0)):
    h.telemetry(A, level=80, voltage=4.0, chutil=ch, airutil=0.2, ts=utc(H10 + off))
# what the box itself sent, and what the radio said became of each
sends = [("delivered", 3000)] * 5 + [("MAX_RETRANSMIT", 3000)] * 2 + [("NO_RESPONSE", 3000)] + [(None, 3000)] * 2 + [(None, 120)]
for k, (ack, ago) in enumerate(sends):
    h.message(own, f"from the box {k}", name="this box", dest="^all", channel=0, mid=1000 + k, ack=ack, ts=utc(NOW - ago))
h.message(A, "from someone else", name="Tracker9", dest="^all", channel=0, mid=5000, ack="delivered", ts=utc(NOW - 3000))

hl = br.op_health(hours=24)
by = {d["id"]: d for d in hl.get("nodes") or []}

# ---- AC1 every packet in the window is counted, however many there are -----------------------
check("AC1 every packet in the window counts, past the 5000 rows one query returns", hl.get("packets"), 6060)
check("AC1 and every packet of a node", (by.get(A) or {}).get("packets"), 6010)
check("AC1 the packets a broker carried are counted apart", hl.get("via_broker"), 50)

# ---- AC2 the window hour by hour --------------------------------------------------------------
ser = hl.get("series") or []
check("AC2 one entry for every hour of the window", len(ser), 24)
check_true("AC2 oldest first, ending with this hour", bool(ser) and ser[-1].get("hour") == utc(NOW)[:13] + ":00Z" and ser[0].get("hour") == utc(T0)[:13] + ":00Z",
           repr((ser[0].get("hour"), ser[-1].get("hour")) if ser else None))
check_true("AC2 each hour carries its figures", bool(ser) and all(set(x) >= {"hour", "chutil", "airutil", "mesh_chutil", "packets", "via_broker"} for x in ser))
check("AC2 the hours add up to the packets heard over the air", sum(int(x.get("packets") or 0) for x in ser), 6010)
check("AC2 and to those a broker carried", sum(int(x.get("via_broker") or 0) for x in ser), 50)
h10 = next((x for x in ser if x.get("hour") == utc(H10)[:13] + ":00Z"), {})
check("AC2 this radio's utilisation and air time, as the hour's mean", (h10.get("chutil"), h10.get("airutil")), (15.0, 2.0))
check("AC2 the mesh's utilisation, the mean of what the other nodes reported", h10.get("mesh_chutil"), 5.0)
check("AC2 an hour with no reading says none, not nought", (ser[0].get("chutil"), ser[0].get("mesh_chutil")) if ser else None, (None, None))

# ---- AC3 how far packets travel, from what this radio heard ---------------------------------
check("AC3 the spread of hops, broker-carried packets left out", hl.get("hops"), {"0": 2400, "1": 1200, "2": 1200, "3+": 1200, "unknown": 10})

# ---- AC4 how the box's own messages fared -----------------------------------------------------
ak = hl.get("acks") or {}
check("AC4 what the box sent in the window, and only that", ak.get("sent"), 11)
check("AC4 delivered, failed, no word and still waiting", (ak.get("delivered"), ak.get("failed"), ak.get("no_word"), ak.get("waiting")), (5, 3, 2, 1))
check("AC4 the failures by the radio's own reason", ak.get("reasons"), {"MAX_RETRANSMIT": 2, "NO_RESPONSE": 1})
check("AC4 the failure rate is of those answered", ak.get("rate"), 37.5)
empty = B.Bridge({"SERIAL": "/dev/serial/by-id/usb-fake-test-radio-if00"}, socket_path=os.path.join(tempfile.mkdtemp(), "e.sock"),
                 state_dir=tempfile.mkdtemp(), observe=True, gps_reader=False).op_health(hours=24)
check("AC4 with nothing answered there is no rate, not nought", ((empty.get("acks") or {}).get("rate"), (empty.get("acks") or {}).get("sent")), (None, 0))

# ---- the existing figures are unchanged --------------------------------------------------------
check_true("the existing hourly chart still carries only hours this radio reported", [x["hour"] for x in hl.get("hourly") or []] == [utc(H10)[:13] + ":00Z"])
time.time = _real_time

# ---- AC5 the screen ------------------------------------------------------------------------------
fb = start_fake_bridge()
srv = W.make_server(bind="127.0.0.1", port=0, socket_path=fb.path, etc_dir=tempfile.mkdtemp(), config={"AUTH": "off"})
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()


def get(p):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10); c.request("GET", p); r = c.getresponse(); b = r.read().decode(); c.close(); return r.status, b


s, page = get("/health")
check_true("AC5 Health draws utilisation over the window for this radio and the mesh", s == 200 and "Channel utilisation over the window" in page and "this radio" in page and "the mesh" in page)
check_true("AC5 and transmit air time over the window", "Transmit air time over the window" in page)
check_true("AC5 and packets heard over the air per hour, with the broker's apart", "Heard over the air, per hour" in page and "6 more carried by a broker" in page)
check_true("AC5 how far packets travel", "How far packets travel" in page and all(w in page for w in ("direct", "1 hop", "2 hops", "3 or more")))
check_true("AC5 how this radio's own messages fared", "What this radio sent" in page and "9 were delivered" in page and "2 failed" in page and "18.2%" in page)
s, node = get("/node?id=!aa000001")
check_true("AC5 a node's page draws its channel utilisation with the 25 and 40 percent lines", s == 200 and "<h2>Channel utilisation</h2>" in node and "25%" in node and "40%" in node)
check_true("AC5 and its transmit air time", "<h2>Transmit air time</h2>" in node)
srv.shutdown()

finish()
