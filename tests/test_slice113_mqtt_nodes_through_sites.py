#!/usr/bin/env python3
"""Spec 113: a hub sees the nodes its sites hear over MQTT (decision D8, option B).

Found while writing the spec: the site's peer snapshot already carries them. `_snapshot_item`
builds from op_nodes(), which filters nothing, and keeps via_mqtt, mqtt_at and heard_here. What
goes wrong is at the other end. A node a site reached only over MQTT arrives with heard_here
false, and every reader of that flag files it as "in the radio's database, not heard": the Nodes
page puts it in the database table and says "never", the map's own-position draw leaves it off,
and a stale snr a site might carry is shown as if somebody measured it. Two sites reporting the
same node make two rows.

So this suite is mostly about the receiving end. AC1 and AC6 are guards on what already holds.

Every block is guarded: a missing helper is one verdict, not a traceback hiding the rest.
"""
import http.client, json, os, re, shutil, subprocess, sys, tempfile, threading, time
sys.path.insert(0, os.path.dirname(__file__))
from _common import ROOT, check, check_true, finish, read, skip  # noqa: E402
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "tests"))
import fakegw_lib  # noqa: E402
fakegw_lib.install()
from mesh_manager.bridge import Bridge  # noqa: E402
from mesh_manager import peers as P  # noqa: E402
from mesh_manager import web as W  # noqa: E402


def iso(ago_s):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - ago_s))


SITE_A, SITE_B, HUB = "a1" * 32, "b2" * 32, "c3" * 32
AIR, MQTT, DBONLY = "!ee000051", "!ee000052", "!ee000053"


class Peering:
    def __init__(self, pid, name):
        self.id, self.name, self.port = pid, name, None
        self.sent = []

    def connected(self):
        return {}

    def broadcast(self, frame):
        self.sent.append(frame)


class Link:
    def __init__(self, peer_id, name="x"):
        self.peer_id, self.peer_name = peer_id, name
        self.sent = []

    def send(self, frame):
        self.sent.append(frame)
        return True


class Q:
    def __getattr__(self, _):
        return lambda *a, **k: None


class Box(Bridge):
    """A site with a radio (mesh_nodes given) or a hub (no radio). No gateway, no socket."""
    def __init__(self, pid, name, nodes=None):
        self.state_dir = tempfile.mkdtemp()
        self.interface = type("I", (), {"nodes": {}})() if nodes is not None else None
        self._nodes = nodes or []
        self.box_mode = "server" if nodes is not None else "hub"
        self.peering = Peering(pid, name)
        self.history = None
        self.logger = Q()
        self.events = []
        self._emit = lambda kind=None, **k: self.events.append(dict(k, kind=kind))
        self._peers_lock = threading.Lock()
        self.remote_nodes, self.remote_waypoints, self.remote_alerts = {}, {}, {}
        self.batteries, self.direct, self.links, self.routes = {}, {}, {}, {}

    def mesh_nodes(self):
        return [dict(n) for n in self._nodes]

    def _own(self):
        return {"id": "!ee000001", "name": self.peering.name}


def site_nodes(air_snr=8.0, mqtt_ago=60):
    return [
        {"id": AIR, "name": "Air one", "battery": 80, "lat": 51.2, "lon": -1.5, "heard": iso(30), "snr": air_snr, "hops": 0,
         "via_mqtt": False, "mqtt_at": None, "heard_here": True},
        {"id": MQTT, "name": "Far one", "battery": 60, "lat": 53.4, "lon": -2.2, "heard": None, "snr": None, "hops": None,
         "via_mqtt": True, "mqtt_at": iso(mqtt_ago), "heard_here": False},
        {"id": DBONLY, "name": "Old one", "battery": 0, "heard": None, "snr": None, "hops": None,
         "via_mqtt": False, "mqtt_at": None, "heard_here": False},
    ]


# ---- AC1 the site's snapshot carries the node it reached over MQTT, marked so (guard: holds today) --
a = Box(SITE_A, "Edge laptop", site_nodes())
try:
    item = a._snapshot_item()
except Exception as ex:  # noqa: BLE001
    item = {}
    check_true("AC1 the site builds a snapshot", False, f"{type(ex).__name__}: {ex}")
rows = {r.get("id"): r for r in (item.get("data") or [])}
m = rows.get(MQTT) or {}
check_true("AC1 the snapshot includes the node reached only over MQTT", MQTT in rows, str(sorted(rows)))
check("AC1 marked as reached over MQTT, not heard on this site's air",
      (m.get("via_mqtt"), m.get("heard_here"), bool(m.get("mqtt_at"))), (True, False, True))
check("AC1 and carrying no signal or hops of its own", (m.get("snr"), m.get("hops")), (None, None))
check("AC1 the snapshot carries nothing from the never-list", P.carries_never(item), False)

# ---- a hub receives both sites' pictures ----------------------------------------------------------------
def picture(site, name, nodes):
    s = Box(site, name, nodes)
    return s._snapshot_item()


def hub_with(*pictures):
    h = Box(HUB, "Dev hub")
    for it in pictures:
        h.peer_item(Link(it["origin"], it["origin_name"]), json.loads(json.dumps(it)))
    return h


# Site A hears AIR on its radio and reaches MQTT over the broker. Site B reaches both over MQTT.
b_nodes = site_nodes()
b_nodes[0] = dict(b_nodes[0], heard=None, snr=None, hops=None, heard_here=False, via_mqtt=True, mqtt_at=iso(300))
b_nodes[1] = dict(b_nodes[1], mqtt_at=iso(300))
hub = hub_with(picture(SITE_A, "Edge laptop", site_nodes()), picture(SITE_B, "Brize box", b_nodes))
hrows = hub.op_nodes().get("nodes") or []
by = {}
for r in hrows:
    by.setdefault(r.get("id"), []).append(r)

# ---- AC2 one node, one row, across sites ----------------------------------------------------------------
check("AC2 a node two sites report is one row on the hub", len(by.get(MQTT, [])), 1)
check("AC2 and so is a node one site hears and another reaches over MQTT", len(by.get(AIR, [])), 1)
_air = (by.get(AIR) or [{}])[0]
check("AC2 the row kept is the freshest contact: here, the site that heard it on the air",
      (_air.get("origin"), _air.get("heard_here"), _air.get("via_mqtt")), (SITE_A, True, False))
# the other way round: A heard it on the air two hours ago, B has it over MQTT a minute ago. Keeping
# A's row would show a current node as quiet, which is the defect this card exists to prevent.
a_old = site_nodes(); a_old[0] = dict(a_old[0], heard=iso(7200))
b_new = site_nodes(); b_new[0] = dict(b_new[0], heard=None, snr=None, hops=None, heard_here=False, via_mqtt=True, mqtt_at=iso(60))
_h = hub_with(picture(SITE_A, "Edge laptop", a_old), picture(SITE_B, "Brize box", b_new))
_k = [r for r in (_h.op_nodes().get("nodes") or []) if r.get("id") == AIR]
check("AC2 an old air contact gives way to a current MQTT one, so a current node never reads quiet",
      [(r.get("origin"), r.get("via_mqtt")) for r in _k], [(SITE_B, True)])
_mq = (by.get(MQTT) or [{}])[-1]
check("AC2 for a node only MQTT carries, the row with the newest contact is kept",
      _mq.get("origin"), SITE_A)
check_true("AC2 the kept row says it came over MQTT and when",
           _mq.get("via_mqtt") is True and bool(_mq.get("mqtt_at")), json.dumps(_mq)[:200])

# ---- AC3 the Nodes page files it as reached, with its age, not as a database-only node -----------------------
mq_row = dict(_mq)
db_row = dict((by.get(DBONLY) or [{}])[0])
air_row = dict(_air)
try:
    heard_html, db_html, n_heard, n_db = W.nodes_tables([air_row, mq_row, db_row], silent_min=30)
except Exception as ex:  # noqa: BLE001
    heard_html, db_html, n_heard, n_db = "", "", 0, 0
    check_true("AC3 the Nodes tables render", False, f"{type(ex).__name__}: {ex}")
check_true("AC3 a node reached over MQTT sits with the nodes being heard", f"data-id='{MQTT}'" in heard_html, "not in the heard table")
check_true("AC3 and not in the radio's database table", f"data-id='{MQTT}'" not in db_html, "filed as database-only")
check_true("AC3 a database-only node still is", f"data-id='{DBONLY}'" in db_html, "the database-only node moved")
fresh = W.node_row(dict(mq_row, mqtt_at=iso(60)), silent_min=30)
old = W.node_row(dict(mq_row, mqtt_at=iso(7200)), silent_min=30)
check_true("AC3 its last-heard cell carries the MQTT time, not 'never'",
           iso(60)[:16] in fresh and ">never<" not in fresh, re.sub(r"\s+", " ", fresh)[-600:-300])
check_true("AC3 recent over MQTT is not called quiet", "data-quiet='0'" in fresh, "")
check_true("AC3 two hours without a packet by either route is quiet", "data-quiet='1'" in old, "an old MQTT contact is not quiet")

# ---- AC4 the words, and no signal that belongs to somebody else's link --------------------------------------
foreign = W.node_row(dict(mq_row, origin_name="Brize box", snr=13.25, hops=0), silent_min=30)
check_true("AC4 a hub row says how it arrived: via MQTT, through the site",
           "via MQTT, through Brize box" in foreign, re.sub(r"<[^>]+>", " ", foreign)[:300])
check_true("AC4 and shows no signal figure, even if the site sent one", "13.25" not in foreign and "direct" not in foreign,
           "another gateway's SNR or a nought-hop 'direct' is on the row")
air_html = W.node_row(dict(air_row, origin_name="Edge laptop"), silent_min=30)
check_true("AC4 a node the site heard on its air still reads via the site, with no MQTT word",
           "via Edge laptop" in air_html and "MQTT" not in re.sub(r"<[^>]+>", " ", air_html), "")

# ---- AC5 the map plots it, whether or not the hub knows where it is itself ------------------------------------
from fakebridge_lib import start_fake_bridge  # noqa: E402
fb = start_fake_bridge()
srv = W.make_server(bind="127.0.0.1", port=0, socket_path=fb.path, etc_dir=tempfile.mkdtemp(),
                    config={"AUTH": "off"}, state_dir=tempfile.mkdtemp())
threading.Thread(target=srv.serve_forever, daemon=True).start()
time.sleep(0.3)
c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
c.request("GET", "/")
body = c.getresponse().read().decode()
c.close()
pm = re.search(r"/\* pure:start \*/([\s\S]*?)/\* pure:end \*/", body)
pure = pm.group(1) if pm else ""
for fn in ("plotted", "routeWord"):
    check_true(f"AC5 {fn} sits in the map's pure block", re.search(r"function " + fn + r"\(", pure) is not None, "")
outside = body.replace(pure, "")
check_true("AC5 the live draw asks plotted() rather than dropping every heard_here=false node",
           outside.count("plotted(") >= 2 and "n.heard_here===false)return" not in outside,
           "the draw still drops nodes the site reached over MQTT")
node = shutil.which("node")
if not node:
    skip("AC5 plotted and routeWord under node", "node is not installed here; the workflow runner has it")
elif re.search(r"function plotted\(", pure) and re.search(r"function routeWord\(", pure):
    js = pure + r"""
var NOW=Date.now();function iso(ago){return new Date(NOW-ago).toISOString().replace(/\.\d+Z$/,'Z');}
var out={
  mqttRemote:plotted({id:'m',lat:53.4,lon:-2.2,heard:null,heard_here:false,via_mqtt:true,mqtt_at:iso(60000),remote:true,origin_name:'Brize box'}),
  mqttOwn:plotted({id:'m',lat:53.4,lon:-2.2,heard:null,heard_here:false,via_mqtt:true,mqtt_at:iso(60000)}),
  airRemote:plotted({id:'a',lat:51.2,lon:-1.5,heard:iso(30000),heard_here:true,remote:true,origin_name:'Edge laptop'}),
  dbOnly:plotted({id:'d',lat:51.0,lon:-1.0,heard:null,heard_here:false,via_mqtt:false}),
  noPos:plotted({id:'n',heard:iso(30000),heard_here:true}),
  wRemote:routeWord({via_mqtt:true,heard_here:false,remote:true,origin_name:'Brize box'}),
  wOwn:routeWord({via_mqtt:true,heard_here:false}),
  wAir:routeWord({heard_here:true,remote:true,origin_name:'Edge laptop'}),
  wHere:routeWord({heard_here:true})
};console.log(JSON.stringify(out));"""
    p = subprocess.run([node, "-e", js], capture_output=True, text=True)
    if p.returncode != 0:
        check_true("AC5 the pure functions run", False, p.stderr[:300])
    else:
        g = json.loads(p.stdout)
        check("AC5 a node a site reached over MQTT is plotted", (g["mqttRemote"], g["mqttOwn"]), (True, True))
        check("AC5 so is one a site heard on its air", g["airRemote"], True)
        check("AC5 a database-only node and one with no position are not", (g["dbOnly"], g["noPos"]), (False, False))
        check("AC5 the label names the route", [g["wRemote"], g["wOwn"], g["wAir"], g["wHere"]],
              ["via MQTT, through Brize box", "over MQTT", "via Edge laptop", ""])
else:
    check_true("AC5 plotted and routeWord run under node", False, "not in the pure block yet")

# ---- AC6 the sharing table governs it, and nothing secret crosses (guards: hold today) --------------------------
a2 = Box(SITE_A, "Edge laptop", site_nodes())
os.makedirs(a2.state_dir, exist_ok=True)
json.dump({"peers": {HUB: {"sharing": {"nodes": {"out": False, "in": True}}}}, "invites": {}},
          open(os.path.join(a2.state_dir, "peers.json"), "w"))
lk = Link(HUB, "Dev hub")
a2._peer_send_snapshot(lk)
check("AC6 nodes Out off at the site: no picture goes to that hub",
      [f for f in lk.sent if (f.get("item") or {}).get("class") == "nodes"], [])
h2 = Box(HUB, "Dev hub")
json.dump({"peers": {SITE_A: {"sharing": {"nodes": {"out": True, "in": False}}}}, "invites": {}},
          open(os.path.join(h2.state_dir, "peers.json"), "w"))
h2.peer_item(Link(SITE_A), picture(SITE_A, "Edge laptop", site_nodes()))
check("AC6 nodes In off at the hub: the picture is dropped", [r.get("id") for r in h2.op_nodes().get("nodes") or []], [])
bad = picture(SITE_A, "Edge laptop", site_nodes())
bad["data"][1]["psk"] = "00" * 32
h3 = hub_with(bad)
check("AC6 a picture carrying a key field is refused whole", [r.get("id") for r in h3.op_nodes().get("nodes") or []], [])
src = read("src/mesh_manager/bridge.py") or ""
check_true("AC6 the MQTT proxy starts only where there is a radio, so a hub subscribes to no broker",
           re.search(r"if not radioless:\s*\n(\s*#[^\n]*\n)*\s*threading\.Thread\(target=self\._mqtt_proxy_start", src) is not None,
           "the proxy start is no longer guarded by radioless")

finish()
