"""A fake gateway and radio for the write suites. The radio keeps real protobuf state and
answers admin requests (channel, config section, owner) from that state, the way the real one
does, so the bridge's read-back is proven against an answer and never against a cache."""
import logging
import sys
import threading
import time
import types

from meshtastic.protobuf import admin_pb2, channel_pb2, localonly_pb2, mesh_pb2


class FakeSock:
    def __init__(self): self.sent = []
    def connect(self, addr): self.addr = addr
    def send(self, b): self.sent.append(b); return len(b)
    def sendto(self, b, a): self.sent.append(b); return len(b)
    def close(self): pass


def _ch(index, role, name="", psk=b""):
    c = channel_pb2.Channel(); c.index = index; c.role = role; c.settings.name = name; c.settings.psk = psk
    return c


class FakeNode:
    """interface.localNode: the library's cache (channels, localConfig) plus the device's own
    state, which only admin requests reveal."""
    def __init__(self):
        self.channels = [_ch(0, 1, "MILUX-TAK", b"\x01" * 32)] + [_ch(i, 0) for i in range(1, 8)]
        self.localConfig = localonly_pb2.LocalConfig()
        self.localConfig.lora.region = 3; self.localConfig.lora.modem_preset = 6; self.localConfig.lora.tx_power = 14
        self.localConfig.device.role = 0
        self.localConfig.position.position_broadcast_secs = 900
        # the device: what a write actually lands on, and what an admin request answers from
        self.device_channels = [channel_pb2.Channel() for _ in range(8)]
        for i in range(8): self.device_channels[i].CopyFrom(self.channels[i])
        self.device_config = localonly_pb2.LocalConfig(); self.device_config.CopyFrom(self.localConfig)
        self.device_owner = mesh_pb2.User(); self.device_owner.long_name = "Bench"; self.device_owner.short_name = "BNCH"
        self.calls = []
        self.readback_delay = 0.0        # seconds before an answer; None = the radio never answers

    def getURL(self, includeAll=True):
        return "https://meshtastic.org/e/#SECRET-" + self.channels[0].settings.psk.hex()[:8]

    def writeChannel(self, channelIndex, adminIndex=0):
        self.calls.append(("writeChannel", channelIndex))
        self.device_channels[channelIndex].CopyFrom(self.channels[channelIndex])

    def deleteChannel(self, channelIndex):
        self.calls.append(("deleteChannel", channelIndex))
        self.channels[channelIndex] = _ch(channelIndex, 0)
        self.device_channels[channelIndex].CopyFrom(self.channels[channelIndex])

    def setURL(self, url, addOnly=False):
        self.calls.append(("setURL", url, addOnly))
        if addOnly:
            free = next(i for i in range(1, 8) if self.channels[i].role == 0)
            self.channels[free] = _ch(free, 2, "ADOPTED", b"\x02" * 32)
            self.device_channels[free].CopyFrom(self.channels[free])
        else:
            self.channels[0] = _ch(0, 1, "REPLACED", b"\x03" * 32)
            self.device_channels[0].CopyFrom(self.channels[0])
            self.localConfig.lora.region = 1; self.device_config.lora.region = 1

    def writeConfig(self, config_name):
        self.calls.append(("writeConfig", config_name))
        getattr(self.device_config, config_name).CopyFrom(getattr(self.localConfig, config_name))

    def removeNode(self, nid):
        self.calls.append(("removeNode", nid))

    def setOwner(self, long_name=None, short_name=None, is_licensed=False, is_unmessagable=None):
        self.calls.append(("setOwner", long_name, short_name))
        if long_name: self.device_owner.long_name = long_name
        if short_name: self.device_owner.short_name = short_name

    def _sendAdmin(self, p, wantResponse=True, onResponse=None, adminIndex=0):
        """Answer the request from the DEVICE state, after readback_delay, or never."""
        self.calls.append(("admin", p.WhichOneof("payload_variant")))
        if self.readback_delay is None or onResponse is None:
            return
        resp = admin_pb2.AdminMessage()
        which = p.WhichOneof("payload_variant")
        if which == "get_channel_request":
            resp.get_channel_response.CopyFrom(self.device_channels[p.get_channel_request - 1])
        elif which == "get_config_request":
            name = admin_pb2.AdminMessage.ConfigType.Name(p.get_config_request).replace("_CONFIG", "").lower()
            getattr(resp.get_config_response, name).CopyFrom(getattr(self.device_config, name))
        elif which == "get_owner_request":
            resp.get_owner_response.CopyFrom(self.device_owner)
        elif which == "get_device_metadata_request":
            resp.get_device_metadata_response.firmware_version = getattr(self, "device_firmware", "2.6.11")
            resp.get_device_metadata_response.hw_model = 43   # TRACKER_T1000_E
        else:
            return
        def deliver():
            time.sleep(self.readback_delay)
            onResponse({"decoded": {"admin": {"raw": resp}}})
        threading.Thread(target=deliver, daemon=True).start()


class FakeIface:
    def __init__(self):
        self.localNode = FakeNode()
        self.texts, self.traces, self.positions, self.data = [], [], [], []
        self.waypoints = []
        self.position = None
        self.nodes = {"!aa000001": {"user": {"longName": "Tracker9", "shortName": "TR9", "hwModel": "TRACKER_T1000_E"}, "lastHeard": int(time.time()) - 5}}
    def getMyNodeInfo(self):
        info = {"num": 1, "user": {"id": "!00000001", "longName": self.localNode.device_owner.long_name, "shortName": self.localNode.device_owner.short_name}}
        if self.position:
            info["position"] = dict(self.position)
        return info
    def sendData(self, data, destinationId="^all", portNum=None, wantResponse=False, onResponse=None, channelIndex=0, hopLimit=3, **kw):
        self.data.append({"data": data, "dest": destinationId, "portNum": portNum, "wantResponse": wantResponse, "onResponse": onResponse, "hopLimit": hopLimit})
    def sendWaypoint(self, name, description, icon, expire, waypoint_id=None, latitude=0.0, longitude=0.0, destinationId="^all", wantAck=True, **kw):
        self.waypoints.append((name, description, latitude, longitude, expire, waypoint_id))
        self._pid = getattr(self, "_pid", 1000) + 1
        return types.SimpleNamespace(id=self._pid)

    def _nodeNumToId(self, num, isDest=True):
        return f"!{int(num):08x}"
    def sendText(self, text, destinationId="^all", channelIndex=0, wantAck=False, onResponse=None, **kw):
        self.texts.append((text, destinationId, channelIndex))
        self._pid = getattr(self, "_pid", 1000) + 1
        self.data.append({"data": text, "dest": destinationId, "portNum": "TEXT_MESSAGE_APP", "wantAck": wantAck, "onResponse": onResponse, "channelIndex": channelIndex})
        return types.SimpleNamespace(id=self._pid)
    def sendTraceRoute(self, dest, hopLimit=7, channelIndex=0): self.traces.append((dest, hopLimit))
    def sendPosition(self, destinationId="^all", wantResponse=False, channelIndex=0, **kw): self.positions.append(destinationId)
    def close(self): pass


class TAKMeshtasticGateway:
    def __init__(self, ip=None, serial_device=None, mesh_ip=None, tak_client_ip="localhost", tx_interval=30,
                 dm_port=4243, log_file=None, debug=False):
        self.ip, self.serial_device = ip, serial_device
        self.meshtastic_devices = {"!aa000001": {"long_name": "Tracker9", "battery": 77, "meshtastic_id": "!aa000001", "last_lat": 51.2, "last_lon": -1.5}}
        self._mesh_radio = {"!aa000001": {"heard": "2026-09-03T02:00:00Z", "snr": 12.5, "hops": 0}}
        self._hb_last = 0.0
        self.meshtastic_connected = True
        self.logger = logging.getLogger("TAK Meshtastic Gateway"); self.logger.setLevel(logging.INFO)
        self.socket_client = FakeSock()
        self.interface = FakeIface()
        self.main_calls = 0
    def mesh_nodes(self):
        # the shape of the MilUX patch's mesh_nodes: one entry per radio id, joined from the
        # gateway's device records and what the radio itself heard; never heard = database only
        out = []
        for key, d in self.meshtastic_devices.items():
            nid = str(d.get("meshtastic_id") or key)
            r = self._mesh_radio.get(nid, {})
            m = {"id": nid, "name": str(d.get("long_name") or nid), "battery": int(d.get("battery") or 0)}
            if d.get("last_lat") and d.get("last_lon"):
                m["lat"], m["lon"] = d["last_lat"], d["last_lon"]
            m["heard"], m["snr"], m["hops"], m["heard_here"] = r.get("heard"), r.get("snr"), r.get("hops"), bool(r)
            out.append(m)
        return out
    def heartbeat(self): pass
    def main(self): self.main_calls += 1; time.sleep(0.2)


def install():
    pkg = types.ModuleType("tak_meshtastic_gateway"); pkg.__version__ = "1.1.0"
    mod = types.ModuleType("tak_meshtastic_gateway.tak_meshtastic_gateway"); mod.TAKMeshtasticGateway = TAKMeshtasticGateway
    pkg.tak_meshtastic_gateway = mod
    sys.modules["tak_meshtastic_gateway"] = pkg
    sys.modules["tak_meshtastic_gateway.tak_meshtastic_gateway"] = mod


class FakeBenchIface:
    """A fresh device on a by-id path, as a second serial interface: its own node with device
    state, a node info, and a close() the bridge must call."""
    opened = []

    def __init__(self, path):
        self.path = path
        self.localNode = FakeNode()
        self.localNode.device_owner.long_name = "New Device"; self.localNode.device_owner.short_name = "NEW"
        self.localNode.localConfig.lora.region = 0; self.localNode.device_config.lora.region = 0
        self.localNode.localConfig.lora.modem_preset = 0; self.localNode.device_config.lora.modem_preset = 0   # a fresh device: LONG_FAST
        for ch in (self.localNode.channels[0], self.localNode.device_channels[0]):
            ch.settings.name = "LongFast"; ch.settings.psk = b"\x01"
        self.closed = False
        self.nodes = {}
        self.position = None          # Spec 033: what the device says about its own receiver
        FakeBenchIface.opened.append(self)

    def getMyNodeInfo(self):
        info = {"num": 0xee000005, "user": {"id": "!ee000005", "longName": self.localNode.device_owner.long_name,
                                            "shortName": self.localNode.device_owner.short_name, "hwModel": "TRACKER_T1000_E"}}
        if self.position:
            info["position"] = dict(self.position)
        return info

    def close(self):
        self.closed = True


class FakeRemoteNode:
    """A device somewhere on the mesh, reached through the gateway: answers admin reads from its
    own state and honours writes only when the gateway's key is among its admin keys, as a real
    device does. One instance per radio id, kept in FakeRemoteNode.devices."""
    devices = {}

    @classmethod
    def device(cls, nid, ours=None):
        if nid not in cls.devices:
            n = FakeNode()
            n.device_owner.long_name = f"Remote {nid[-4:]}"; n.device_owner.short_name = nid[-4:].upper()
            if ours:
                n.localConfig.security.admin_key.append(ours); n.device_config.security.admin_key.append(ours)
            n.session_calls = 0
            cls.devices[nid] = n
        return cls.devices[nid]

    def __init__(self, iface, nid, ours=None):
        self.iface, self.nodeNum = iface, nid
        self.dev = FakeRemoteNode.device(nid, ours)
        self.localConfig = localonly_pb2.LocalConfig()
        self.channels = None
        self.calls = []

    def _honours(self):
        ours = bytes(self.iface.localNode.localConfig.security.public_key or b"")
        return bool(ours) and ours in [bytes(k) for k in self.dev.device_config.security.admin_key]

    def ensureSessionKey(self):
        self.dev.session_calls += 1; self.calls.append("session")

    def setOwner(self, long_name=None, short_name=None, **kw):
        self.calls.append(("setOwner", long_name, short_name))
        if self._honours():
            if long_name: self.dev.device_owner.long_name = long_name
            if short_name: self.dev.device_owner.short_name = short_name

    def writeConfig(self, name):
        self.calls.append(("writeConfig", name))
        if self._honours():
            getattr(self.dev.device_config, name).CopyFrom(getattr(self.localConfig, name))

    def writeChannel(self, index, adminIndex=0):
        self.calls.append(("writeChannel", index))
        if self._honours() and self.channels:
            self.dev.device_channels[index].CopyFrom(self.channels[index])

    def reboot(self, secs=10):
        self.calls.append(("reboot", secs))

    def _sendAdmin(self, p, wantResponse=True, onResponse=None, adminIndex=0):
        which = p.WhichOneof("payload_variant")
        self.calls.append(("admin", which))
        if which == "set_channel":
            if self._honours():
                self.dev.device_channels[p.set_channel.index].CopyFrom(p.set_channel)
            return
        if which == "reboot_seconds":
            self.calls.append(("reboot", p.reboot_seconds)); return
        return self.dev._sendAdmin(p, wantResponse=wantResponse, onResponse=onResponse, adminIndex=adminIndex)
