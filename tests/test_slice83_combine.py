#!/usr/bin/env python3
"""Spec 083: many radios in one place, and one that has gone quiet.

Matt: "Group the nodes and show only the centre of mass, drill down on click, with stale-node
handling."

The centre of mass is a claim that these are here, now. A node last heard six hours ago is not
there now, so it must not move the average, and must not vanish either.
"""
import http.client, json, os, re, shutil, subprocess, sys, tempfile, threading, time
sys.path.insert(0, os.path.dirname(__file__))
from _common import ROOT, check, check_true, finish, read, skip  # noqa: E402
sys.path.insert(0, os.path.join(ROOT, "src"))
from fakebridge_lib import start_fake_bridge  # noqa: E402
from mesh_manager import web as W  # noqa: E402

fb = start_fake_bridge()
srv = W.make_server(bind="127.0.0.1", port=0, socket_path=fb.path, etc_dir=tempfile.mkdtemp(),
                    config={"AUTH": "off"}, state_dir=tempfile.mkdtemp())
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
time.sleep(0.3)
c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
c.request("GET", "/")
r = c.getresponse()
body = r.read().decode()
c.close()
check("the map page still answers", r.status, 200)

web = read("src/mesh_manager/web.py") or ""
i = web.index("CSS = ")
css = web[i:web.index('"""', web.index('"""', i) + 3)]

# AC10 the control
check_true("AC10 there is a control for it", "id='combine-on'" in body)
check_true("AC10 it starts on, because the pile is the problem being solved",
           re.search(r"id='combine-on'[^>]*checked", body) is not None)
check_true("AC10 it says what it does", "centre of mass" in body)
check_true("AC10 remembered under its own key", "mm-combine" in body)

# AC2 reckoned again when the zoom changes
check_true("AC2 the grouping is worked out again on zoom", re.search(r"map\.on\('zoomend'", body) is not None)

# AC4 the box is drawn its own way and is not a member
cl = body.split("function clusterNodes(", 1)
check_true("AC4 only the node points are grouped, never the box",
           "clusterNodes(pts," in body and "L.circleMarker(c,{radius:9" in body)

# AC9 pressing one says who is in it
check_true("AC9 a combined marker can be pressed", "bindPopup(" in body)
check_true("AC9 and the list carries each member's colour, name and age",
           "mm-cl-row" in body and "mm-cl-row" in css)

# AC6/AC7 a quiet node is drawn, not hidden
check_true("AC6 a quiet single node is drawn as stale", "'stale'" in body and ".mm-pin.stale" in css)
check_true("AC7 a wholly quiet marker is drawn as stale too", "allStale" in body)
check_true("AC7 and says so in words", "not heard lately" in body)

# AC11 the pure functions
for fn in ("clusterNodes", "nodeStale", "staleGap", "clusterWord"):
    check_true(f"AC11 {fn} sits in the map's pure block",
               re.search(r"/\* pure:start \*/[\s\S]*?function " + fn + r"\([\s\S]*?/\* pure:end \*/", body) is not None)
check_true("AC8 stale is the rule playback already uses, not a second one",
           re.search(r"function staleGap\([^)]*\)\{[^}]*gapFor\(", body) is not None)

m = re.search(r"/\* pure:start \*/([\s\S]*?)/\* pure:end \*/", body)
node = shutil.which("node")
if not node:
    skip("AC5, AC7, AC8, AC11 the pure functions under node", "node is not installed here; the workflow runner has it")
elif m:
    js = m.group(1) + r"""
var NOW=Date.parse('2026-09-10T12:00:00Z');
function iso(ms){return new Date(ms).toISOString().replace('.000Z','Z');}
function n(id,lat,lon,agoMs,group){return {id:id,lat:lat,lon:lon,group:group||'',heard:agoMs===null?null:iso(NOW-agoMs)};}
// a project that puts a degree at a thousand pixels, so the pixel sums are easy to reason about
function P(lat,lon){return {x:lon*1000,y:-lat*1000};}
function never(){return false;}
var A=n('a',51.5000,-0.1200,10000), B=n('b',51.5002,-0.1197,10000), C=n('c',51.4999,-0.1204,10000);
var FAR=n('f',52.5000,-0.1200,10000);   // a degree north: 1000 px away on this stub, well outside the radius
var G1=n('g1',51.5000,-0.1200,10000,'Recce'), G2=n('g2',51.5000,-0.1200,10000,'Fires');
// staleness: four times a node's own median, floor two minutes; no history means no rhythm to read
var fast=[NOW-120000,NOW-90000,NOW-60000,NOW-30000];   // every 30 s
var slow=[NOW-10800000,NOW-7200000,NOW-3600000];        // hourly
var out={
  three: (function(){var cs=clusterNodes([A,B,C],P,400,never);return cs.map(function(c){return [c.count,c.fresh,c.stale,c.allStale];});})(),
  centre:(function(){var c=clusterNodes([A,B,C],P,400,never)[0];return [Math.round(c.lat*1e6),Math.round(c.lon*1e6)];})(),
  split: clusterNodes([A,B,C,FAR],P,400,never).map(function(c){return c.count;}),
  groups:clusterNodes([G1,G2],P,400,never).map(function(c){return [c.group,c.count];}),
  // a quiet member is counted and drawn, but does not move the centre of mass
  withStale:(function(){var cs=clusterNodes([A,B,C],P,400,function(x){return x.id==='c';});var c=cs[0];
    return {count:c.count,fresh:c.fresh,stale:c.stale,allStale:c.allStale,
            lat:Math.round(c.lat*1e6),lon:Math.round(c.lon*1e6),
            members:c.members.map(function(x){return x.id;})};})(),
  freshOnlyCentre:(function(){return [Math.round(((A.lat+B.lat)/2)*1e6),Math.round(((A.lon+B.lon)/2)*1e6)];})(),
  // nothing fresh: it centres on the last it knew and says so
  allStale:(function(){var c=clusterNodes([A,B,C],P,400,function(){return true;})[0];
    return {allStale:c.allStale,fresh:c.fresh,stale:c.stale,lat:Math.round(c.lat*1e6)};})(),
  everyCentre:Math.round(((A.lat+B.lat+C.lat)/3)*1e6),
  // the antimeridian: an average of +179.9 and -179.9 is 180, never 0
  anti:(function(){var l=n('l',10,179.9,1000),r=n('r',10,-179.9,1000);
    var c=clusterNodes([l,r],function(){return {x:0,y:0};},10,never)[0];
    return Math.round(Math.abs(c.lon)*10)/10;})(),
  single:clusterNodes([A],P,400,never).map(function(c){return [c.count,c.lat===A.lat&&c.lon===A.lon];}),
  none:clusterNodes([],P,400,never).length,
  gapFast:staleGap(fast,600000), gapSlow:staleGap(slow,600000), gapNone:staleGap([],600000),
  freshFast:nodeStale(n('x',1,1,60000),NOW,fast,600000),
  quietFast:nodeStale(n('x',1,1,300000),NOW,fast,600000),
  freshSlow:nodeStale(n('x',1,1,3600000),NOW,slow,600000),
  quietSlow:nodeStale(n('x',1,1,20000000),NOW,slow,600000),
  noHistNear:nodeStale(n('x',1,1,300000),NOW,[],600000),
  noHistFar: nodeStale(n('x',1,1,1200000),NOW,[],600000),
  neverHeard:nodeStale(n('x',1,1,null),NOW,[],600000),
  // A node the broker is carrying is current; it is the radio link that is absent, not the node.
  // Reporting it as not heard lately sent an operator looking for a fault that was not there.
  // Raised 14 September 2026.
  mqttFresh:nodeStale({id:'m',heard:iso(NOW-20000000),mqtt_at:iso(NOW-60000)},NOW,fast,600000),
  mqttAlsoQuiet:nodeStale({id:'m',heard:iso(NOW-20000000),mqtt_at:iso(NOW-20000000)},NOW,fast,600000),
  mqttNeverOnAir:nodeStale({id:'m',heard:null,mqtt_at:iso(NOW-60000)},NOW,fast,600000),
  overMqtt:[overMqtt({via_mqtt:true}),overMqtt({via_mqtt:false}),overMqtt({})],
  words:[clusterWord({count:1,stale:0,allStale:false}),
         clusterWord({count:5,stale:0,allStale:false}),
         clusterWord({count:5,stale:2,allStale:false}),
         clusterWord({count:3,stale:3,allStale:true})]
};
console.log(JSON.stringify(out));
"""
    p = subprocess.run([node, "-e", js], capture_output=True, text=True)
    if p.returncode != 0:
        check_true("AC11 the pure functions run", False)
        print(p.stderr[:800])
    else:
        g = json.loads(p.stdout)
        # AC1 one marker, carrying the number
        check("AC1 three on top of each other make one marker of three", g["three"], [[3, 3, 0, False]])
        check("AC1 at the average of the three", g["centre"], [g["everyCentre"], -120033])
        check("AC1 one far off stays its own", g["split"], [3, 1])
        check("AC3 two groups on the same spot are never merged", g["groups"], [["Recce", 1], ["Fires", 1]])
        # AC5/AC6 the quiet one is counted and drawn but does not move the centre
        w = g["withStale"]
        check("AC6 the quiet one is still counted", [w["count"], w["fresh"], w["stale"]], [3, 2, 1])
        check("AC6 and still in the list, so it is still drawn", w["members"], ["a", "b", "c"])
        check("AC5 the centre of mass is the fresh ones only", [w["lat"], w["lon"]], g["freshOnlyCentre"])
        # AC7 nothing fresh at all
        a = g["allStale"]
        check("AC7 a wholly quiet marker says so", [a["allStale"], a["fresh"], a["stale"]], [True, 0, 3])
        check("AC7 and centres on the last it knew rather than nothing", a["lat"], g["everyCentre"])
        check("AC11 an average across the antimeridian is 180, not 0", g["anti"], 180.0)
        check("AC1 one node is just that node, where it is", g["single"], [[1, True]])
        check("AC1 nothing in, nothing out", g["none"], 0)
        # AC8 the staleness rule
        check("AC8 a node reporting every 30 s is quiet after four of them, floored at two minutes",
              g["gapFast"], 120000)
        check("AC8 an hourly node is not quiet until four hours", g["gapSlow"], 4 * 3600000)
        check("AC8 no history means no rhythm to read, so a stated floor", g["gapNone"], 600000)
        check("AC8 a fast node heard a minute ago is fine", g["freshFast"], False)
        check("AC8 a fast node quiet five minutes has gone quiet", g["quietFast"], True)
        check("AC8 an hourly node heard an hour ago is fine", g["freshSlow"], False)
        check("AC8 an hourly node quiet five hours has gone quiet", g["quietSlow"], True)
        check("AC8 without history, five minutes is inside the floor", g["noHistNear"], False)
        check("AC8 and twenty minutes is not", g["noHistFar"], True)
        check("AC8 never heard at all is quiet, because nothing is known", g["neverHeard"], True)
        check("a node the broker is carrying is not quiet", g["mqttFresh"], False)
        check("and one nothing has carried for hours still is", g["mqttAlsoQuiet"], True)
        check("and one this radio never heard is current if the broker has it", g["mqttNeverOnAir"], False)
        check("and the screen can tell which route it came by", g["overMqtt"], [True, False, False])
        check("AC9 the words", g["words"],
              ["", "5 nodes", "5 nodes, 2 not heard lately", "3 nodes, none heard lately"])

finish()
