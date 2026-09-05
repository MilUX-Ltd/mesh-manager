#!/usr/bin/env python3
"""Spec 006: channels and this radio's settings as writes, against a fake gateway and radio."""
import http.client
import json
import os
import socket
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(__file__))
from _common import ROOT, check, check_true, finish  # noqa: E402
sys.path.insert(0, os.path.join(ROOT, "src"))
import fakegw_lib  # noqa: E402
fakegw_lib.install()
try:
    from mesh_manager import bridge as B, web as W, catalogue as C, connections as K
except Exception as e:  # noqa: BLE001
    print(f"FAIL imports                                                     {type(e).__name__}: {e}")
    print("\nFAILURES: 1"); sys.exit(1)

sd = tempfile.mkdtemp(); sock_path = os.path.join(sd, "b.sock")
conf = {"SERIAL": "/dev/serial/by-id/usb-x-if00", "EXTRA_ARGS": "", "ip": None, "debug": False, "BIND": "127.0.0.1", "PORT": 8093}
br = B.Bridge(conf, socket_path=sock_path, state_dir=sd, observe=True, silence_limit=600)
br.READBACK_S = 2          # the fake radio answers at once or never; never must not take 30 s here
threading.Thread(target=br.serve_forever, daemon=True).start()
deadline = time.time() + 5
while not os.path.exists(sock_path) and time.time() < deadline:
    time.sleep(0.05)
node = br.gateway.interface.localNode
OWN = "!00000001"


def ask(op, **args):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); s.settimeout(10); s.connect(sock_path)
    s.sendall((json.dumps({"op": op, **args}) + "\n").encode())
    buf = b""
    while not buf.endswith(b"\n"):
        c = s.recv(1 << 20)
        if not c: break
        buf += c
    s.close(); return json.loads(buf.decode() or "{}")


# AC1 create
r = ask("channel_create", name="RECCE")
check("AC1 channel_create lands in the first free slot as SECONDARY", (r.get("index"), r.get("read_back", {}).get("name"), r.get("read_back", {}).get("role"), r.get("confirmed")), (1, "RECCE", "SECONDARY", True))
check("AC1 the slot's key is 32 bytes", len(node.channels[1].settings.psk), 32)
check_true("AC1 the reply never carries the key", "psk" not in json.dumps(r) and node.channels[1].settings.psk.hex() not in json.dumps(r))
check_true("AC1 channels lists it", any(c["name"] == "RECCE" and c["index"] == 1 for c in ask("channels")["channels"]))

# AC2 rotate
old_key = node.channels[0].settings.psk
r = ask("channel_rotate", index=0)
check_true("AC2 rotating the primary without confirm is refused, naming the radio", "error" in r and OWN in r.get("error", ""))
check("AC2 ...and nothing was written", node.channels[0].settings.psk, old_key)
r = ask("channel_rotate", index=0, confirm=OWN)
check_true("AC2 rotating the primary with confirm writes a new key and reads it back", r.get("confirmed") is True and node.channels[0].settings.psk != old_key and ("writeChannel", 0) in node.calls)
r = ask("channel_rotate", index=1)
check_true("AC2 rotating a secondary needs no confirm", r.get("confirmed") is True)

# AC3 adopt
r = ask("channel_adopt", url="not a url", mode="add")
check_true("AC3 a URL that does not decode is refused", "error" in r)
good = "https://meshtastic.org/e/#CgcSAQEoATABEg8IATgBQANIAVAeaAHABgE"
r = ask("channel_adopt", url=good, mode="add")
check_true("AC3 adopt add appends to a free slot", r.get("confirmed") is True and any(c.settings.name == "ADOPTED" for c in node.channels))
r = ask("channel_adopt", url=good, mode="replace")
check_true("AC3 adopt replace without confirm is refused and names the URL's region", "error" in r and "region" in json.dumps(r).lower())
r = ask("channel_adopt", url=good, mode="replace", confirm=OWN)
check_true("AC3 adopt replace with confirm replaces the set", r.get("confirmed") is True and node.channels[0].settings.name == "REPLACED")
check_true("AC3 the key from the URL appears in no reply", "CgcSAQEo" not in json.dumps(r))

# AC4 delete
check_true("AC4 deleting the primary is refused", "error" in ask("channel_delete", index=0))
r = ask("channel_delete", index=2)
check("AC4 deleting slot 2 reads back DISABLED", (r.get("read_back", {}).get("role"), node.channels[2].role), ("DISABLED", 0))

# AC5 radio_set
r = ask("radio_set", long_name="Gateway North", short_name="GWN", tx_power=20, position_broadcast_secs=600)
check_true("AC5 owner written by setOwner and read back", ("setOwner", "Gateway North", "GWN") in node.calls and r.get("read_back", {}).get("long_name") == "Gateway North")
check_true("AC5 tx power and position written by section", ("writeConfig", "lora") in node.calls and ("writeConfig", "position") in node.calls and node.localConfig.lora.tx_power == 20 and node.localConfig.position.position_broadcast_secs == 600)
check("AC5 confirmed", r.get("confirmed"), True)
check_true("AC5 an out-of-range tx power is refused", "error" in ask("radio_set", tx_power=99))

# AC6 region
r = ask("radio_set_region", region="US")
check_true("AC6 region without confirm is refused, naming the radio", "error" in r and OWN in r.get("error", ""))
r = ask("radio_set_region", region="US", modem_preset="LONG_FAST", role="TAK", confirm=OWN)
check_true("AC6 with confirm lora and device are written and read back", ("writeConfig", "lora") in node.calls and ("writeConfig", "device") in node.calls and r.get("read_back", {}).get("region") == "US" and r.get("read_back", {}).get("role") == "TAK" and r.get("confirmed") is True)
check_true("AC6 an unknown region is refused", "error" in ask("radio_set_region", region="MOON", confirm=OWN))

# AC7 no read-back
node.readback_delay = None
r = ask("radio_set", tx_power=17)
check_true("AC7 a read-back that never arrives is unconfirmed with a sent time, not an error", r.get("confirmed") is False and r.get("unconfirmed") and r.get("sent") and "error" not in r)
node.readback_delay = 0.0
# AC7b the read-back is the radio's answer, not the cache: make the device disagree with the write
real_write = node.writeConfig
node.writeConfig = lambda name: node.calls.append(("writeConfig", name))     # the device ignores the write
r = ask("radio_set", tx_power=21)
check_true("AC7b a write the device did not take reads back as unconfirmed with the device's value", r.get("confirmed") is False and r.get("read_back", {}).get("tx_power") == 17)
node.writeConfig = real_write
node.device_config.lora.tx_power = 21

# ---- the screen and the MCP ------------------------------------------------------------------
etc = tempfile.mkdtemp()
W.write_password(os.path.join(etc, "passwd"), "correct horse")
srv = W.make_server(bind="127.0.0.1", port=0, socket_path=sock_path, etc_dir=etc)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
time.sleep(0.3)


def req(method, path, body=None, cookie=None, ctype="application/x-www-form-urlencoded", token=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    h = {}
    if cookie: h["Cookie"] = cookie
    if token: h["Authorization"] = "Bearer " + token
    if body is not None: h["Content-Type"] = ctype
    c.request(method, path, body=body, headers=h)
    r = c.getresponse(); data = r.read(); hd = dict((k.lower(), v) for k, v in r.getheaders())
    c.close(); return r.status, hd, data


st, hd, _ = req("POST", "/login", body="password=correct+horse")
cookie = hd.get("set-cookie", "").split(";")[0]
st, _, ch = req("GET", "/channels", cookie=cookie); ch = ch.decode()
for aid in ("channel_create", "channel_adopt", "channel_rotate", "channel_delete"):
    check_true(f"AC8 /channels carries a control for {aid}", f"data-action='{aid}'" in ch)
st, _, rp = req("GET", "/radio", cookie=cookie); rp = rp.decode()
check_true("AC8 /radio carries the settings form and the region form", "data-action='radio_set'" in rp and "data-action='radio_set_region'" in rp)
check_true("AC8 the region form's confirm names the consequence and the radio", "another band" in rp and OWN in rp)
st, _, _ = req("POST", "/api/channel_rotate", body=json.dumps({"index": 0}), cookie=cookie, ctype="application/json")
check("AC8 rotate the primary without confirm answers 400", st, 400)
st, _, _ = req("POST", "/api/channel_rotate", body=json.dumps({"index": 0, "confirm": OWN}), cookie=cookie, ctype="application/json")
check("AC8 ...with confirm answers 200", st, 200)


def rpc(method, params=None, token=None):
    st, _, data = req("POST", "/mcp", body=json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}), ctype="application/json", token=token)
    try: return st, json.loads(data)
    except ValueError: return st, {}


six = {"channel_create", "channel_rotate", "channel_adopt", "channel_delete", "radio_set", "radio_set_region"}
tok_act = K.mint(etc, "t-act", "act")["token"]; tok_prop = K.mint(etc, "t-prop", "propose")["token"]
st, r = rpc("tools/list", token=tok_act)
check_true("AC9 act lists the six write tools", six <= {t["name"] for t in r.get("result", {}).get("tools", [])})
st, r = rpc("tools/list", token=tok_prop)
check_true("AC9 propose does not list them", not (six & {t["name"] for t in r.get("result", {}).get("tools", [])}))
st, r = rpc("tools/call", {"name": "channel_rotate", "arguments": {"index": 0}}, token=tok_prop)
check_true("AC9 channel_rotate at propose is refused naming the autonomy", "propose" in json.dumps(r) and r.get("result", {}).get("isError"))
st, r = rpc("tools/call", {"name": "channel_rotate", "arguments": {"index": 0}}, token=tok_act)
check_true("AC9 at act without confirm the named refusal comes back", r.get("result", {}).get("isError") and OWN in json.dumps(r))
srv.shutdown(); br.stop()

# AC7c the radio's answer about its owner is what the config shows afterwards, whatever the library's cache says
r = br.op_radio_set(long_name="Renamed Radio")
check("AC7c rename confirmed from the radio's answer", (r.get("confirmed"), (r.get("read_back") or {}).get("long_name")), (True, "Renamed Radio"))
check("AC7c op_config shows the radio's word, not the cache", br.op_config().get("long_name"), "Renamed Radio")
check_true("AC7c op_config says when the radio was last read", bool(br.op_config().get("read_at")))
finish()
