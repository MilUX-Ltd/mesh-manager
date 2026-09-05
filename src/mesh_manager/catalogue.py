"""The one action catalogue (Spec 005). Every operation Mesh Manager performs is an entry here;
the screen renders its forms and API routes from it and the MCP derives its tools from it, so
anything a person can do on the screen an agent can do through a connector, and nothing else.

risk:  read         reads local state, nothing leaves the box
       air          an on-air request that changes no device (a message, a traceroute)
       change       writes a device's configuration            (later slices)
       unreachable  a write that may take a device off the mesh (later slices)
       flash        firmware on the bench                       (later slices)
floor: the lowest autonomy a connection needs to call it directly."""
import re

from .common import NODE_ICONS

AUTONOMY = ("observe", "propose", "act")
FLOOR = {"read": "observe", "air": "propose", "change": "act", "unreachable": "act", "flash": "act"}

NODE = re.compile(r"^![0-9a-f]{8}$")

ACTIONS = [
    {"id": "status", "title": "Mesh status", "risk": "read", "op": "status", "inputs": [],
     "description": "The bridge and the radio as they are now: radio present, connected, last activity on the serial loop, last packet forwarded, nodes seen, region, preset, primary channel, watchdog."},
    {"id": "nodes", "title": "Nodes", "risk": "read", "op": "nodes", "inputs": [],
     "description": "Every node the radio knows of, joined on radio id: name, hardware, battery, position (only with a fix), last heard, SNR, hops, and whether it was heard here or is only in the radio's stored database."},
    {"id": "node", "title": "One node", "risk": "read", "op": "node",
     "inputs": [{"name": "id", "type": "node", "required": True, "description": "the radio id, !hex"}],
     "description": "One node's record by radio id."},
    {"id": "links", "title": "Links", "risk": "read", "op": "links", "inputs": [],
     "description": "The mesh as links: this box with its position and where that came from, every node with the SNR of its last direct packet and its recent SNR history, and the last traceroute answer per node."},
    {"id": "route", "title": "Last route to a node", "risk": "read", "op": "route",
     "inputs": [{"name": "id", "type": "node", "required": True, "description": "the radio id, !hex"}],
     "description": "The last traceroute answer for one node: the hops out and back with the SNR at each hop. Ask with traceroute first; the answer arrives as a route event."},
    {"id": "register", "title": "Fleet register", "risk": "read", "op": "register", "inputs": [],
     "description": "Every node the radio knows of joined with the box's register on radio id: the node's own name beside the operator's label, who holds it, hardware, firmware, role, whether it is managed (its admin keys hold this radio's public key, as read from the device itself), last heard."},
    {"id": "inventory", "title": "Fleet inventory", "risk": "read", "op": "inventory", "inputs": [],
     "description": "One row per radio the register or the radio's database knows: hardware, firmware (only when read from that device, with when), role, the fingerprint of its public key and since when, whether the key has changed and been accepted, whether the firmware is behind the shelf's verified image for that hardware, said in words."},
    {"id": "key_accept", "title": "Accept a device's changed key", "risk": "change", "op": "key_accept",
     "inputs": [{"name": "id", "type": "node", "required": True, "description": "the radio id, !hex"}],
     "confirm": "The key now on file stands as this device's key and the alarm clears. Do this only when you know why it changed: a reflash, or a new radio under the old id.",
     "description": "Clear the changed-key alarm for one device: the key it now presents is accepted as its key. Nothing is sent to the device."},
    {"id": "register_set", "title": "Label a device in the register", "risk": "change", "op": "register_set",
     "inputs": [{"name": "id", "type": "text", "required": True, "max_bytes": 12, "description": "the radio id, !hex"},
                {"name": "label", "type": "text", "required": False, "max_bytes": 80, "description": "the operator's label for the device"},
                {"name": "holder", "type": "text", "required": False, "max_bytes": 80, "description": "who holds it"},
                {"name": "note", "type": "text", "required": False, "max_bytes": 200, "description": "a note"},
                {"name": "group", "type": "text", "required": False, "max_bytes": 40, "description": "the group the device belongs to (a section, a vehicle, the routers); blank leaves it in none"},
                {"name": "tags", "type": "text", "required": False, "max_bytes": 300, "description": "tags, comma separated, ten at most"},
                {"name": "icon", "type": "enum", "values": list(NODE_ICONS) + ["inherit"], "required": False, "description": "the map icon for this device, or inherit for its group's"}],
     "confirm": "",
     "description": "Write the operator's label, holder, note, group, tags and map icon for a device into the box's register. Changes nothing on any radio."},
    {"id": "groups", "title": "Groups", "risk": "read", "op": "groups", "inputs": [],
     "description": "Every group the register knows: its name, its map icon, how many devices are in it. A group is a word the operator gives devices (a section, a vehicle, the routers); the map, the lists, the alerts and the exports filter by it."},
    {"id": "group_set", "title": "Create a group or set its icon", "risk": "change", "op": "group_set",
     "inputs": [{"name": "name", "type": "text", "required": True, "max_bytes": 40, "description": "the group's name"},
                {"name": "icon", "type": "enum", "values": list(NODE_ICONS), "required": False, "description": "the map icon its devices carry unless one has its own"}],
     "confirm": "The group and its icon change on this box now; devices in it redraw on the map. Nothing is written to any radio.",
     "description": "Create a group with a map icon, or change an existing group's icon. Kept on the box."},
    {"id": "group_delete", "title": "Remove a group", "risk": "change", "op": "group_delete",
     "inputs": [{"name": "name", "type": "text", "required": True, "max_bytes": 40, "description": "the group's name"}],
     "confirm": "The group goes; its devices keep everything else and simply belong to no group. Nothing is written to any radio.",
     "description": "Remove a group from the box; its members lose the group and nothing else."},
    {"id": "bench_devices", "title": "Devices on the bench", "risk": "read", "op": "bench_devices", "inputs": [],
     "description": "The serial devices plugged into the box by their /dev/serial/by-id/ path, other than the gateway's own radio, with whether each sits in bootloader mode and the step that recovers it."},
    {"id": "bench_read", "title": "Read a device on the bench", "risk": "read", "op": "bench_read",
     "inputs": [{"name": "path", "type": "text", "required": True, "max_bytes": 200, "description": "the device's /dev/serial/by-id/ path"}],
     "description": "Open a device on its USB cable, read its id, names, hardware, firmware, region, preset, role, channels (names and roles, never keys) and whether this radio's public key is among its admin keys, then close it. Refuses the gateway radio's own path and a device in bootloader mode."},
    {"id": "bench_onboard", "title": "Onboard a device on the bench", "risk": "change", "op": "bench_onboard",
     "inputs": [{"name": "path", "type": "text", "required": True, "max_bytes": 200, "description": "the device's /dev/serial/by-id/ path"},
                {"name": "long_name", "type": "text", "required": True, "max_bytes": 39, "description": "the device's long name"},
                {"name": "short_name", "type": "text", "required": True, "max_bytes": 4, "description": "the short name, 4 bytes at most"},
                {"name": "role", "type": "enum", "values": ["CLIENT", "CLIENT_MUTE", "ROUTER", "ROUTER_CLIENT", "REPEATER", "TRACKER", "SENSOR", "TAK", "CLIENT_HIDDEN", "LOST_AND_FOUND", "TAK_TRACKER", "ROUTER_LATE"], "required": True, "description": "the device role"},
                {"name": "label", "type": "text", "required": False, "max_bytes": 80, "description": "the operator's label for the device, written to the box's register"},
                {"name": "holder", "type": "text", "required": False, "max_bytes": 80, "description": "who holds it, written to the box's register"}],
     "confirm": "The device on the cable gets these names and role, this radio's primary channel and key, this radio's region and preset, and this radio's public key as an admin key; its configuration is exported to the box first. Every one is read back from the device before it shows here.",
     "description": "Bring a device into the fleet on its USB cable: names and role, the gateway's primary channel and key into slot 0, the gateway's region and preset, the gateway's public key among its admin keys (refused when three foreign keys already fill the list), every one read back from the device, its configuration exported to the box, and the register updated. Keys never appear in the answer."},
    {"id": "bench_export", "title": "Export a bench device's configuration", "risk": "read", "op": "bench_export",
     "inputs": [{"name": "path", "type": "text", "required": True, "max_bytes": 200, "description": "the device's /dev/serial/by-id/ path"}],
     "description": "Read a device on its cable and save its owner, configuration and channels (keys included) under the box's state directory at mode 0600; answers the file's path and size, never its content."},
    {"id": "node_read", "title": "Read a device over the air", "risk": "read", "op": "node_read",
     "inputs": [{"name": "id", "type": "node", "required": True, "description": "the radio id, !hex"}],
     "description": "Ask a device on the mesh, through this radio, for its names, region, preset, role, power, position interval, whether this radio's key is still among its admin keys, and its first channel slots (names and roles, never keys). Every figure is the device's own answer; over LoRa this takes seconds to a minute. Refreshes the register."},
    {"id": "node_set", "title": "Set a device's names, power and position settings over the air", "risk": "change", "op": "node_set",
     "inputs": [{"name": "id", "type": "node", "required": True, "description": "the radio id, !hex"},
                {"name": "long_name", "type": "text", "required": False, "max_bytes": 39, "description": "the long name"},
                {"name": "short_name", "type": "text", "required": False, "max_bytes": 4, "description": "the short name, 4 bytes at most"},
                {"name": "tx_power", "type": "int", "required": False, "min": 0, "max": 30, "description": "transmit power in dBm (0 = the region's maximum)"},
                {"name": "position_broadcast_secs", "type": "int", "required": False, "min": 32, "max": 86400, "description": "seconds between position broadcasts"}],
     "confirm": "Written over the air to the device under this box's admin key and read back from the device; over LoRa that can take a minute.",
     "description": "Write a managed device's names, transmit power or position interval over the air and read each back from the device. Only for a device the register holds as managed; anything else is refused with 'bring it to the bench'."},
    {"id": "node_set_region", "title": "Set a device's region, preset or role over the air", "risk": "unreachable", "op": "node_set_region",
     "inputs": [{"name": "id", "type": "node", "required": True, "description": "the radio id, !hex"},
                {"name": "region", "type": "enum", "values": ["EU_868", "US", "EU_433", "ANZ", "IN", "JP", "KR", "TW", "RU", "CN", "NZ_865", "TH", "UA_433", "UA_868", "MY_433", "MY_919", "SG_923", "LORA_24"], "required": False, "description": "the LoRa region"},
                {"name": "modem_preset", "type": "enum", "values": ["LONG_FAST", "LONG_SLOW", "VERY_LONG_SLOW", "MEDIUM_SLOW", "MEDIUM_FAST", "SHORT_SLOW", "SHORT_FAST", "LONG_MODERATE", "SHORT_TURBO"], "required": False, "description": "the modem preset"},
                {"name": "role", "type": "enum", "values": ["CLIENT", "CLIENT_MUTE", "ROUTER", "ROUTER_CLIENT", "REPEATER", "TRACKER", "SENSOR", "TAK", "CLIENT_HIDDEN", "LOST_AND_FOUND", "TAK_TRACKER", "ROUTER_LATE"], "required": False, "description": "the device role"},
                {"name": "confirm", "type": "confirm", "required": False, "description": "the device's own radio id, to confirm"}],
     "confirm": "Changing the region, preset or role moves that device to another band or role; it may be unreachable over the air afterwards and reboots. Confirm by naming the device's id.",
     "description": "Write a managed device's region, modem preset or role over the air and read them back. The device may then be on another band and unreachable from here: confirm = the device's own id is required."},
    {"id": "node_channel_push", "title": "Push one of this radio's channels to a device over the air", "risk": "change", "op": "node_channel_push",
     "inputs": [{"name": "id", "type": "node", "required": True, "description": "the radio id, !hex"},
                {"name": "index", "type": "int", "required": True, "min": 0, "max": 7, "description": "the slot, copied from this radio's same slot"},
                {"name": "confirm", "type": "confirm", "required": False, "description": "the device's own radio id, to confirm slot 0"}],
     "confirm": "This radio's channel slot (name, key and role) is copied to the same slot on the device and read back. Slot 0 replaces its primary channel, after which it may not hear this mesh if the keys differ: that one needs confirm = the device's id.",
     "description": "Copy this radio's channel slot to the same slot on a managed device over the air and read it back by name and role. Slot 0 needs confirm = the device's own id. The key is never shown."},
    {"id": "node_reboot", "title": "Reboot a device over the air", "risk": "change", "op": "node_reboot",
     "inputs": [{"name": "id", "type": "node", "required": True, "description": "the radio id, !hex"},
                {"name": "confirm", "type": "confirm", "required": False, "description": "the device's own radio id, to confirm"}],
     "confirm": "The device reboots in ten seconds and is off the mesh while it does. Confirm by naming the device's id.",
     "description": "Ask a managed device to reboot in ten seconds. The answer says it was asked, never that it rebooted: watch for it to be heard again. confirm = the device's own id is required."},
    {"id": "bench_exports", "title": "A device's exports on the box", "risk": "read", "op": "bench_exports",
     "inputs": [{"name": "id", "type": "text", "required": True, "max_bytes": 12, "description": "the radio id, !hex"}],
     "description": "The configuration exports the box holds for a device: path, time and size, never content."},
    {"id": "bench_restore", "title": "Restore a device from an export on the bench", "risk": "change", "op": "bench_restore",
     "inputs": [{"name": "path", "type": "text", "required": True, "max_bytes": 200, "description": "the device's /dev/serial/by-id/ path"},
                {"name": "export", "type": "text", "required": True, "max_bytes": 300, "description": "the export's path on the box, from bench_exports"},
                {"name": "confirm", "type": "confirm", "required": False, "description": "the device's own radio id, to restore an export made from a different device (a clone)"}],
     "confirm": "The device on the cable gets the export's names, channels, region, preset, role and position settings, each read back from the device; its own keys and admin keys stay as they are.",
     "description": "Write an export's owner, channels and lora, device and position sections to a device on its cable and read each back. The export must be that device's own unless confirm names the device. The security section is never restored."},
    {"id": "firmware_shelf", "title": "The firmware shelf", "risk": "read", "op": "firmware_shelf", "inputs": [],
     "description": "Every firmware image the fleet may carry, from the pins in the release: hardware, version, method, and whether the file on the box is present and verified against its sha256, wrong, or missing (with the path to put it at)."},
    {"id": "bench_flash", "title": "Flash a device on the bench", "risk": "flash", "op": "bench_flash",
     "inputs": [{"name": "path", "type": "text", "required": True, "max_bytes": 200, "description": "the device's /dev/serial/by-id/ path"},
                {"name": "image", "type": "text", "required": True, "max_bytes": 60, "description": "the pin's id from firmware_shelf"},
                {"name": "confirm", "type": "confirm", "required": True, "description": "the device's own radio id"}],
     "confirm": "The device's configuration is exported first; then the pinned image is written (nRF52 through its UF2 bootloader, ESP32 with esptool) and the version read back after it reboots. A factory image loses every setting. A flash that does not come back is reported with the recovery step, never as done. Confirm by naming the device.",
     "description": "Flash a pinned, verified image to a device on its cable: export first, the pin's hardware must match the device's, the file's sha256 must match the pin, confirm = the device's own id. The stages arrive as flash events. Never over the air."},
    {"id": "node_forget", "title": "Forget a node", "risk": "change", "op": "node_forget",
     "inputs": [{"name": "id", "type": "node", "required": True, "description": "the radio id, !hex"},
                {"name": "register", "type": "enum", "values": ["keep", "drop"], "required": False, "description": "keep the register's label and holder for when it is heard again, or drop them"}],
     "confirm": "The node leaves this radio's database and the box's lists. It comes back if it is heard again; only its label and holder are kept or dropped as you choose.",
     "description": "Remove a node from the gateway radio's database and from the box's own lists (a duplicate, a radio no longer used). Nothing is sent to the node; it comes back if it is heard again. register=drop also removes the box's label and holder for it."},
    {"id": "nodes_forget_stale", "title": "Forget the stale", "risk": "change", "op": "nodes_forget_stale",
     "inputs": [{"name": "days", "type": "int", "required": False, "min": 1, "max": 365, "description": "forget every node not heard for this many days (7 if not given)"}],
     "confirm": "Every node the radio has not heard for that many days leaves its database and the box's lists. Each comes back if it is heard again; labels and holders are kept.",
     "description": "Forget every node in the gateway radio's database not heard for a number of days (7 by default): dead radios, old ids, duplicates. Nothing is sent. A node comes back if it is heard again."},
    {"id": "channels", "title": "Channels", "risk": "read", "op": "channels", "inputs": [],
     "description": "The radio's channel set: index, name, role, whether a key is set. Never the key."},
    {"id": "config", "title": "This radio's settings", "risk": "read", "op": "config", "inputs": [],
     "description": "The gateway radio's own configuration: names, role, region, modem preset, TX power, position broadcast interval. Read only in this release."},
    {"id": "log", "title": "Bridge log", "risk": "read", "op": "log",
     "inputs": [{"name": "n", "type": "int", "required": False, "min": 1, "max": 500, "description": "how many lines, newest last (default 200)"}],
     "description": "The bridge's recent log: what it forwarded, what the radio said, what the watchdog decided."},
    {"id": "availability", "title": "Availability", "risk": "read", "op": "availability",
     "inputs": [{"name": "hours", "type": "int", "required": False, "min": 1, "max": 720, "description": "the window in hours; hourly buckets to 48, daily beyond"}],
     "description": "How much of the window each node was actually heard for: buckets of an hour (or a day past two days), a bucket counting if any packet from the node landed in it, as a percentage and a series for a histogram. A node with nothing in the window is 0% and still listed."},
    {"id": "waypoints", "title": "Waypoints", "risk": "read", "op": "waypoints", "inputs": [],
     "description": "Waypoints heard on the mesh and still live: id, who sent it, name, description, position and expiry. Each is also forwarded to TAK as a spot marker when it arrives."},
    {"id": "waypoint_send", "title": "Drop a waypoint on the mesh", "risk": "air", "op": "waypoint_send",
     "inputs": [{"name": "name", "type": "text", "required": True, "max_bytes": 30, "description": "the name shown on every device, 30 bytes at most"},
                {"name": "description", "type": "text", "required": False, "max_bytes": 100, "description": "what it is, 100 bytes at most"},
                {"name": "lat", "type": "text", "required": True, "max_bytes": 12, "description": "latitude, decimal degrees"},
                {"name": "lon", "type": "text", "required": True, "max_bytes": 12, "description": "longitude, decimal degrees"},
                {"name": "expire_min", "type": "int", "required": False, "min": 1, "max": 10080, "description": "minutes until it expires, 60 by default"}],
     "confirm": "This reaches every device on the primary channel and is forwarded to TAK as a marker.",
     "description": "Send a waypoint to every device on the mesh: name, description, position, expiry. It is a broadcast, so it costs airtime and everyone sees it; it is also forwarded to TAK as a spot marker."},
    {"id": "neighbors", "title": "Who hears whom", "risk": "read", "op": "neighbors",
     "inputs": [{"name": "hours", "type": "int", "required": False, "min": 1, "max": 720, "description": "the window in hours"}],
     "description": "The mesh as a graph, from the neighbour-info reports nodes broadcast where the module is on: for each report, the neighbours that node heard and the SNR it heard them at, with both ends named. Empty when no node has the module on."},
    {"id": "quick_messages", "title": "Quick messages", "risk": "read", "op": "web:quick_messages", "inputs": [],
     "description": "The preset messages the Messages page offers as buttons, kept on the box. Up to eight, each 200 bytes at most."},
    {"id": "quick_messages_set", "title": "Set the quick messages", "risk": "change", "op": "web:quick_messages_set",
     "inputs": [{"name": "messages", "type": "object", "required": True, "description": "a list of up to eight strings, each 200 bytes at most"}],
     "confirm": "",
     "description": "Replace the preset messages on the box. Nothing is sent; these are what the operator can press to fill the message field."},
    {"id": "update_staged", "title": "Releases this box could return to", "risk": "read", "op": "web:update_staged", "inputs": [],
     "description": "Every release whose tarball, hash and installer are still staged on the box, newest first, with the running one marked. These are what a roll back can return to; nothing is downloaded."},
    {"id": "update_rollback", "title": "Roll back to a staged release", "risk": "change", "op": "web:update_rollback",
     "inputs": [{"name": "version", "type": "text", "required": True, "max_bytes": 16, "description": "the version to return to, as update_staged lists it"}],
     "confirm": "This restarts the bridge and the screen, so the mesh is off TAK for about a minute. It returns the code, not the box's settings.",
     "description": "Re-apply a release the box already has: its tarball is checked against its own hash, then the same root unit an update uses installs it. Refuses the running version, a version the box has not got, and a tarball whose hash no longer matches."},
    {"id": "box_position_set", "title": "Say where this box is", "risk": "change", "op": "box_position_set",
     "inputs": [{"name": "lat", "type": "text", "required": False, "max_bytes": 12, "description": "latitude, decimal degrees"},
                {"name": "lon", "type": "text", "required": False, "max_bytes": 12, "description": "longitude, decimal degrees"},
                {"name": "clear", "type": "enum", "values": ["on", "off"], "required": False, "description": "on clears the declared position instead"}],
     "confirm": "The box's declared position changes on this box now; the map re-centres. A GPS receiver's fix, when there is one, still comes first.",
     "description": "Declare where this box is, for the map's centre and the range rings when the box has no GPS receiver. Kept on the box, above the installer's flags and below any receiver's fix; nothing is written to any radio."},
    {"id": "map_sources", "title": "Map sources", "risk": "read", "op": "web:map_sources", "inputs": [],
     "description": "Every source the map can draw: the built-in ones, the box's own MBTiles, ATAK custom map sources in the box's map folder, and any added on this screen."},
    {"id": "map_source_add", "title": "Add a map source", "risk": "change", "op": "web:map_source_add",
     "inputs": [{"name": "name", "type": "text", "required": False, "max_bytes": 60, "description": "what to call it in the layer control"},
                {"name": "xml", "type": "text", "required": False, "max_bytes": 8000, "description": "a whole ATAK <customMapSource> XML, as ATAK carries it"},
                {"name": "url", "type": "text", "required": False, "max_bytes": 500, "description": "or a tile URL template with {z}, {x} and {y} (ATAK writes them {$z}, {$x}, {$y})"},
                {"name": "minzoom", "type": "int", "required": False, "min": 0, "max": 22, "description": "the lowest zoom the source has tiles for"},
                {"name": "maxzoom", "type": "int", "required": False, "min": 0, "max": 22, "description": "the highest zoom the source has tiles for"}],
     "confirm": "The source is saved on this box and offered to every browser that opens this screen. Tiles load in the viewer's browser, not on the box.",
     "description": "Add a map source, from an ATAK custom map source XML or a tile URL template. Kept on the box."},
    {"id": "map_source_remove", "title": "Remove a map source", "risk": "change", "op": "web:map_source_remove",
     "inputs": [{"name": "id", "type": "text", "required": True, "max_bytes": 64, "description": "the source's id, from Map sources"}],
     "confirm": "The source leaves this screen's layer control. Only a source added here can be removed; the built-in ones and the box's own files stay.",
     "description": "Remove a map source that was added on this screen."},
    {"id": "profile", "title": "Fleet profile", "risk": "read", "op": "profile", "inputs": [],
     "description": "The settings every managed device should carry: role, transmit power, position broadcast interval, region, modem preset. An unset field is not enforced."},
    {"id": "profile_set", "title": "Set the fleet profile", "risk": "change", "op": "profile_set",
     "inputs": [{"name": "role", "type": "text", "required": False, "max_bytes": 24, "description": "the device role, e.g. TRACKER, CLIENT; blank leaves it unenforced"},
                {"name": "tx_power", "type": "int", "required": False, "min": 0, "max": 30, "description": "transmit power in dBm"},
                {"name": "position_broadcast_secs", "type": "int", "required": False, "min": 32, "max": 86400, "description": "seconds between position broadcasts"},
                {"name": "region", "type": "text", "required": False, "max_bytes": 12, "description": "the LoRa region, e.g. EU_868"},
                {"name": "modem_preset", "type": "text", "required": False, "max_bytes": 24, "description": "the modem preset, e.g. SHORT_FAST"}],
     "confirm": "The profile changes on this box; nothing is written to any device until a fix is pressed.",
     "description": "Change the fleet profile the drift check compares every device against. Kept on the box."},
    {"id": "drift", "title": "Config drift", "risk": "read", "op": "drift", "inputs": [],
     "description": "Every registered device's last read-back against the fleet profile: in line, drifted (with each field, what it is and what it should be), or unread."},
    {"id": "drift_fix", "title": "Bring a device into line", "risk": "unreachable", "op": "drift_fix",
     "inputs": [{"name": "id", "type": "node", "required": True, "description": "the managed device, !hex"},
                {"name": "scope", "type": "enum", "values": ["safe", "all"], "required": False, "description": "safe writes power and interval; all also writes region, preset and role"}],
     "confirm": "Writes the profile to the device over the air. With scope all, a region, preset or role change moves the device to another band and every device on the old one stops hearing it; confirm by naming the device.",
     "description": "Write the fleet profile to one managed device over the air, each field read back before it is shown as done. Safe writes power and interval; all also writes region, preset and role, which needs the confirm naming the device."},
    {"id": "rotation_status", "title": "Key rotation checklist", "risk": "read", "op": "rotation_status", "inputs": [],
     "description": "Since the last marked key rotation: which expected devices have been heard on the new key (back, with the time), which have not (waiting), and the counts."},
    {"id": "rotation_mark", "title": "Mark a key rotation done elsewhere", "risk": "change", "op": "rotation_mark",
     "inputs": [{"name": "index", "type": "int", "required": False, "min": 0, "max": 7, "description": "the channel slot rotated (0 if not given)"},
                {"name": "note", "type": "text", "required": False, "max_bytes": 120, "description": "how it was done, for the record"}],
     "confirm": "The checklist restarts from now: every registered device and everyone heard in the last week is expected back on the new key.",
     "description": "Record that a channel key was changed outside this screen (the phone app, the CLI), so the checklist can count devices back. A rotation from this screen marks itself."},
    {"id": "alerts", "title": "Alerts", "risk": "read", "op": "alerts",
     "inputs": [{"name": "limit", "type": "int", "required": False, "min": 1, "max": 500, "description": "recent alerts at most (50 if not given)"}],
     "description": "The alerts open now (silent, battery, unknown, fence) and the recent ones, with the thresholds in force."},
    {"id": "alert_settings", "title": "Alert settings", "risk": "read", "op": "alert_settings", "inputs": [],
     "description": "The thresholds the box judges by: minutes of silence, the battery percentage, whether unknown nodes alert, the fence radius (0 off), and whether alerts go to TAK chat."},
    {"id": "alert_set", "title": "Set the alert thresholds", "risk": "change", "op": "alert_set",
     "inputs": [{"name": "silent_min", "type": "int", "required": False, "min": 1, "max": 1440, "description": "minutes without a packet before a registered device is silent"},
                {"name": "battery_pct", "type": "int", "required": False, "min": 1, "max": 90, "description": "the battery percentage under which a device alerts"},
                {"name": "unknown", "type": "enum", "values": ["on", "off"], "required": False, "description": "alert when a node not in the register is heard"},
                {"name": "fence_m", "type": "int", "required": False, "min": 0, "max": 100000, "description": "metres from the box beyond which a node alerts; 0 turns the fence off"},
                {"name": "to_tak", "type": "enum", "values": ["on", "off"], "required": False, "description": "send each alert to All Chat Rooms on the TAK Server"}],
     "confirm": "The thresholds change on this box now; alerts already open stay open until their condition clears.",
     "description": "Change what the box alerts on. Kept on the box; the screen and the MCP read the same settings."},
    {"id": "fences", "title": "Fences", "risk": "read", "op": "fences", "inputs": [],
     "description": "The areas drawn on the map: id, name, polygon points or a circle's centre and radius, whether a crossing in (enter), out (leave) or either alerts, the group it applies to (or everyone), and whether it is on."},
    {"id": "fence_set", "title": "Draw or change a fence", "risk": "change", "op": "fence_set",
     "inputs": [{"name": "id", "type": "text", "required": False, "max_bytes": 16, "description": "an existing fence's id, to change it; blank makes a new one"},
                {"name": "name", "type": "text", "required": False, "max_bytes": 40, "description": "what to call it"},
                {"name": "kind", "type": "enum", "values": ["polygon", "circle"], "required": False, "description": "a drawn outline or a circle"},
                {"name": "points", "type": "text", "required": False, "max_bytes": 8000, "description": "the outline as JSON, a list of [lat, lon] pairs, three at least"},
                {"name": "lat", "type": "text", "required": False, "max_bytes": 12, "description": "a circle's centre latitude"},
                {"name": "lon", "type": "text", "required": False, "max_bytes": 12, "description": "a circle's centre longitude"},
                {"name": "radius_m", "type": "int", "required": False, "min": 10, "max": 100000, "description": "a circle's radius in metres"},
                {"name": "rule", "type": "enum", "values": ["enter", "leave", "both"], "required": False, "description": "alert on coming in, going out, or either"},
                {"name": "group", "type": "text", "required": False, "max_bytes": 40, "description": "only this group's devices; blank means everyone"},
                {"name": "enabled", "type": "enum", "values": ["on", "off"], "required": False, "description": "off keeps the fence drawn but silent"}],
     "confirm": "Alerts on this box and in TAK chat when a device crosses. Nothing is sent to any device.",
     "description": "Draw a fence on the map, or change one: a named area, a crossing rule, a group it applies to. Kept on the box; the alert pass raises geofence alerts as devices cross."},
    {"id": "fence_delete", "title": "Remove a fence", "risk": "change", "op": "fence_delete",
     "inputs": [{"name": "id", "type": "text", "required": True, "max_bytes": 16, "description": "the fence's id"}],
     "confirm": "The fence goes from the map and no longer alerts. Nothing is sent to any device.",
     "description": "Remove a fence from the box."},
    {"id": "alert_test", "title": "Send a test alert to TAK", "risk": "air", "op": "alert_test", "inputs": [],
     "description": "One GeoChat to All Chat Rooms saying it is a test, to prove the path from this box to the TAK Server's chat. In observe mode it is counted, not sent."},
    {"id": "health", "title": "Mesh health", "risk": "read", "op": "health",
     "inputs": [{"name": "hours", "type": "int", "required": False, "min": 1, "max": 168, "description": "the window in hours (24 if not given)"}],
     "description": "How busy the mesh is: the gateway radio's channel utilisation with its verdict (quiet, normal, busy, saturated), its transmit air time against the region's duty-cycle budget (10 percent on EU_868), packets per hour, and per node the packets heard, its last utilisation, air time and battery."},
    {"id": "history", "title": "History", "risk": "read", "op": "history",
     "inputs": [{"name": "kind", "type": "enum", "values": ["positions", "telemetry", "messages", "packets"], "required": False, "description": "which table (positions if not given)"},
                {"name": "node", "type": "text", "required": False, "max_bytes": 12, "description": "one radio id, !hex; every node if not given"},
                {"name": "since", "type": "text", "required": False, "max_bytes": 20, "description": "a UTC time, YYYY-MM-DDTHH:MM:SSZ; only rows from then"},
                {"name": "limit", "type": "int", "required": False, "min": 1, "max": 5000, "description": "rows at most (500 if not given)"}],
     "description": "What the box has heard over time, from the history store that survives a restart: positions, device telemetry, messages or packets, per node or for all, newest last."},
    {"id": "history_summary", "title": "History summary", "risk": "read", "op": "history_summary", "inputs": [],
     "description": "Row counts and the time span of each history table, and the store's size on disk."},
    {"id": "messages", "title": "Messages", "risk": "read", "op": "web:messages", "inputs": [],
     "description": "Channel chat the bridge has heard or sent since it started, newest last."},
    {"id": "send_text", "title": "Send a message", "risk": "air", "op": "send_text",
     "inputs": [{"name": "text", "type": "text", "required": True, "max_bytes": 200, "description": "the message, 200 bytes at most"},
                {"name": "channel", "type": "int", "required": False, "min": 0, "max": 7, "description": "channel index (default 0, the primary)"},
                {"name": "to", "type": "node_or_all", "required": False, "description": "a node's radio id for a direct message, ^all (default) for the channel, or group:<name> for one direct message to each member"}],
     "confirm": "Every device on the channel will see this message.",
     "description": "Send a text to the mesh: to a channel (every device on it sees it), to one node, or to a group (one direct message per member, each with its own receipt; the answer lists the members and their packet ids)."},
    {"id": "traceroute", "title": "Traceroute", "risk": "air", "op": "traceroute",
     "inputs": [{"name": "dest", "type": "node", "required": True, "description": "the node's radio id"}],
     "description": "Ask the mesh for the route to a node; the answer arrives on the log and the event stream. Changes nothing."},
    {"id": "request_position", "title": "Ask for a position", "risk": "air", "op": "request_position",
     "inputs": [{"name": "dest", "type": "node", "required": True, "description": "the node's radio id"}],
     "description": "Ask a node to send its position now. Changes nothing."},
    {"id": "request_telemetry", "title": "Ask for a battery", "risk": "air", "op": "request_telemetry",
     "inputs": [{"name": "dest", "type": "node", "required": True, "description": "the node's radio id"}],
     "description": "Ask a node for its device metrics now: battery level, voltage, uptime. Changes nothing. The box also asks every node it has heard in the last day, every half hour, on its own."},
    # ---- writes to this radio (Spec 006). An `unreachable` action needs confirm = this radio's id.
    {"id": "request_nodeinfo", "title": "Ask for its name", "risk": "air", "op": "request_nodeinfo",
     "inputs": [{"name": "dest", "type": "node", "required": True, "description": "the radio id, !hex"}],
     "description": "Ask one node what it calls itself, sending the box's own name in the same exchange. A device renamed over the air keeps its old name until it next broadcasts; this brings the new one back in seconds without forgetting the node. The answer arrives as a nodeinfo event."},
    {"id": "survey_start", "title": "Start a coverage survey", "risk": "air", "op": "survey_start",
     "inputs": [{"name": "dest", "type": "node", "required": True, "description": "the node to ask, !hex"},
                {"name": "interval", "type": "int", "required": False, "min": 5, "max": 120, "description": "seconds between asks (15 if not given)"},
                {"name": "minutes", "type": "int", "required": False, "min": 1, "max": 120, "description": "how long to keep asking (10 if not given)"}],
     "description": "Ask one node for its position every few seconds while someone walks it: each answer lands in the history with its signal, and the map's coverage layer fills in. One survey at a time; it stops itself when the minutes are up."},
    {"id": "survey_stop", "title": "Stop the coverage survey", "risk": "air", "op": "survey_stop", "inputs": [],
     "description": "End the running survey now."},
    {"id": "survey_status", "title": "Coverage survey status", "risk": "read", "op": "survey_status", "inputs": [],
     "description": "Whether a survey runs, for which node, how many asks so far and how many answers landed in the history since it started."},
    {"id": "channel_decode", "title": "Read a join URL", "risk": "read", "op": "channel_decode",
     "inputs": [{"name": "url", "type": "text", "required": True, "max_bytes": 1024, "description": "a meshtastic.org join URL"}],
     "description": "What a join URL carries: channel names, roles, the region and the preset. Never the key. Read this before adopting anything that arrived by export rather than by authorship."},
    {"id": "channel_create", "title": "Create a channel", "risk": "change", "op": "channel_create",
     "inputs": [{"name": "name", "type": "text", "required": True, "max_bytes": 11, "description": "the channel name, 11 bytes at most"},
                {"name": "index", "type": "int", "required": False, "min": 1, "max": 7, "description": "the slot, 1 to 7 (default: the first free one)"}],
     "confirm": "A new secondary channel with a fresh 256-bit key. Devices join it by scanning its QR.",
     "description": "Mint a 256-bit key on the box and write a secondary channel to a free slot on this radio; read it back."},
    {"id": "channel_rotate", "title": "Rotate a channel's key", "risk": "unreachable", "op": "channel_rotate",
     "inputs": [{"name": "index", "type": "int", "required": True, "min": 0, "max": 7, "description": "the slot"},
                {"name": "confirm", "type": "confirm", "required": False, "description": "this radio's id, to confirm a rotation of the primary channel"}],
     "confirm": "Rotating the key on the primary channel drops every device that has not scanned the new QR. This radio is {own}.",
     "description": "Write a fresh key to a channel slot and read it back. On the primary channel (slot 0) every device that has not scanned the new QR drops off the mesh, so that one needs confirm = this radio's id."},
    {"id": "channel_adopt", "title": "Adopt a join URL", "risk": "unreachable", "op": "channel_adopt",
     "inputs": [{"name": "url", "type": "text", "required": True, "max_bytes": 1024, "description": "a meshtastic.org join URL"},
                {"name": "mode", "type": "enum", "values": ["add", "replace"], "required": True, "description": "add its channels to the free slots, or replace this radio's whole channel set and region from it"},
                {"name": "confirm", "type": "confirm", "required": False, "description": "this radio's id, to confirm a replace"}],
     "confirm": "Replacing takes this radio's channels and region from the URL; devices on the old channels will not hear it. This radio is {own}.",
     "description": "Take channels from a join URL: add them to free slots, or replace this radio's channel set and region with them (that one needs confirm = this radio's id). The key is never shown."},
    {"id": "channel_delete", "title": "Delete a channel", "risk": "change", "op": "channel_delete",
     "inputs": [{"name": "index", "type": "int", "required": True, "min": 1, "max": 7, "description": "the slot, 1 to 7 (the primary cannot be deleted)"}],
     "confirm": "The slot goes back to disabled. Devices on that channel lose it.",
     "description": "Disable a secondary channel slot on this radio and read it back."},
    {"id": "radio_set", "title": "Set this radio's names, power and position settings", "risk": "change", "op": "radio_set",
     "inputs": [{"name": "long_name", "type": "text", "required": False, "max_bytes": 39, "description": "the long name"},
                {"name": "short_name", "type": "text", "required": False, "max_bytes": 4, "description": "the short name, 4 bytes at most"},
                {"name": "tx_power", "type": "int", "required": False, "min": 0, "max": 30, "description": "transmit power in dBm (0 = the region's maximum)"},
                {"name": "position_broadcast_secs", "type": "int", "required": False, "min": 32, "max": 86400, "description": "seconds between position broadcasts"}],
     "confirm": "Written to this radio and read back.",
     "description": "Write this radio's names, transmit power and position broadcast interval, and read each back. Position precision is a channel setting, not a radio one, and comes with the channel settings slice."},
    {"id": "radio_set_region", "title": "Set this radio's region, preset or role", "risk": "unreachable", "op": "radio_set_region",
     "inputs": [{"name": "region", "type": "enum", "values": ["EU_868", "US", "EU_433", "ANZ", "IN", "JP", "KR", "TW", "RU", "CN", "NZ_865", "TH", "UA_433", "UA_868", "MY_433", "MY_919", "SG_923", "LORA_24"], "required": False, "description": "the LoRa region, a legal setting"},
                {"name": "modem_preset", "type": "enum", "values": ["LONG_FAST", "LONG_SLOW", "VERY_LONG_SLOW", "MEDIUM_SLOW", "MEDIUM_FAST", "SHORT_SLOW", "SHORT_FAST", "LONG_MODERATE", "SHORT_TURBO"], "required": False, "description": "the modem preset; every device on the mesh must share it"},
                {"name": "role", "type": "enum", "values": ["CLIENT", "CLIENT_MUTE", "ROUTER", "ROUTER_CLIENT", "REPEATER", "TRACKER", "SENSOR", "TAK", "CLIENT_HIDDEN", "LOST_AND_FOUND", "TAK_TRACKER", "ROUTER_LATE"], "required": False, "description": "the device role"},
                {"name": "confirm", "type": "confirm", "required": False, "description": "this radio's id"}],
     "confirm": "Changing the region or preset moves this radio to another band; a fleet on the old setting will not hear it, and the radio reboots. This radio is {own}.",
     "description": "Write this radio's region, modem preset or role and read them back. The radio may reboot and will be on another band or role: confirm = this radio's id is required."},
]
for _a in ACTIONS:
    _a.setdefault("floor", FLOOR[_a["risk"]])

# words a skill may put in backticks that are not tools or actions
KNOWN_WORDS = {"observe", "propose", "act", "read", "air", "change", "unreachable", "flash",
               "mesh_context", "id", "text", "channel", "to", "dest", "n",
               # fields the reads return, which a skill may name
               "heard_here", "snr", "hops", "heard", "battery", "radio_present", "bootloader", "connected",
               "last_activity", "last_forwarded", "nodes_seen", "watchdog", "region", "modem_preset",
               "primary_channel", "tx_power", "not_pinging", "pinging",
               "managed", "direct_snr", "history", "label", "holder", "note", "admin_keys", "firmware", "role", "path",
               "confirmed", "unconfirmed", "read_back", "written", "sent", "export", "index", "long_name", "short_name",
               "position_broadcast_secs", "bench_only", "nodes_db", "position_source", "recovery", "towards", "back", "confirm", "onboarded_at", "managed_at", "export_at"}


def by_id(aid):
    for a in ACTIONS:
        if a["id"] == aid:
            return a
    return None


def rank(autonomy):
    return AUTONOMY.index(autonomy) if autonomy in AUTONOMY else -1


def visible(autonomy):
    r = rank(autonomy)
    return [a for a in ACTIONS if rank(a["floor"]) <= r]


def validate(action, args, known_nodes=None):
    """Clean arguments against the entry's inputs. Returns (clean, error)."""
    args = dict(args or {})
    clean = {}
    names = {i["name"] for i in action["inputs"]}
    unknown = sorted(set(args) - names)
    if unknown:
        return None, f"unknown argument(s) for {action['id']}: {', '.join(unknown)}"
    for i in action["inputs"]:
        v = args.get(i["name"])
        if v in (None, ""):
            if i.get("required"):
                return None, f"{i['name']} is required"
            continue
        t = i["type"]
        if t == "text":
            v = str(v)
            if not v.strip():
                return None, f"{i['name']} is empty"
            if len(v.encode()) > i.get("max_bytes", 200):
                return None, f"{i['name']} is longer than {i.get('max_bytes', 200)} bytes"
        elif t == "int":
            try:
                v = int(v)
            except (TypeError, ValueError):
                return None, f"{i['name']} must be a whole number"
            if v < i.get("min", -10**9) or v > i.get("max", 10**9):
                return None, f"{i['name']} must be between {i.get('min')} and {i.get('max')}"
        elif t == "enum":
            v = str(v).strip()
            if v not in i.get("values", []):
                return None, f"{i['name']} must be one of: {', '.join(i.get('values', []))}"
        elif t == "confirm":
            v = str(v).strip()
        elif t in ("node", "node_or_all"):
            v = str(v).strip()
            if t == "node_or_all" and (v == "^all" or re.fullmatch(r"group:.{1,40}", v)):
                pass
            elif not NODE.match(v):
                return None, f"{i['name']} must be a radio id like !1a2b3c4d"
            elif known_nodes is not None and v not in known_nodes:
                return None, f"{i['name']}: no node {v} is known to this radio"
        clean[i["name"]] = v
    return clean, None


def tool_schema(action):
    props, req = {}, []
    for i in action["inputs"]:
        p = {"description": i.get("description", "")}
        p["type"] = "integer" if i["type"] == "int" else "string"
        if i["type"] == "int":
            if "min" in i: p["minimum"] = i["min"]
            if "max" in i: p["maximum"] = i["max"]
        props[i["name"]] = p
        if i.get("required"):
            req.append(i["name"])
    s = {"type": "object", "properties": props}
    if req:
        s["required"] = req
    return s


def parity_problems(actions, routes, tool_names):
    """What is out of step between the catalogue, the screen's routes and the MCP's tools."""
    ids = {a["id"] for a in actions}
    problems = []
    for aid in sorted(ids - set(routes)):
        problems.append(f"catalogue entry {aid} has no /api route")
    for r in sorted(set(routes) - ids):
        problems.append(f"route /api/{r} has no catalogue entry")
    tools = set(tool_names) - {"propose", "mesh_context"}
    for aid in sorted(ids - tools):
        problems.append(f"catalogue entry {aid} is not an MCP tool")
    for t in sorted(tools - ids):
        problems.append(f"MCP tool {t} has no catalogue entry")
    return problems
KNOWN_WORDS = set(KNOWN_WORDS) | {"telemetry", "battery", "batteries", "voltage", "uptime", "stale", "days", "ids", "dead", "metrics", "half", "hour"}
KNOWN_WORDS = set(KNOWN_WORDS) | {"history", "table", "positions", "packets", "rows", "span", "disk", "utc", "hex", "restart"}
KNOWN_WORDS = set(KNOWN_WORDS) | {"survey", "coverage", "walks", "walk", "asks", "answers", "landed", "itself", "minutes", "seconds"}
KNOWN_WORDS = set(KNOWN_WORDS) | {"health", "busy", "quiet", "normal", "saturated", "verdict", "utilisation", "duty", "cycle", "budget", "percent", "transmit"}
KNOWN_WORDS = set(KNOWN_WORDS) | {"alerts", "alert", "silent", "silence", "fence", "radius", "thresholds", "threshold", "percentage", "unknown", "chat", "rooms", "test", "prove", "geochat"}
KNOWN_WORDS = set(KNOWN_WORDS) | {"rotation", "rotated", "checklist", "expected", "waiting", "back", "elsewhere", "restarts", "week"}
KNOWN_WORDS = set(KNOWN_WORDS) | {"profile", "drift", "drifted", "unread", "enforced", "unenforced", "fleet", "line", "preset", "pressed", "blank"}
KNOWN_WORDS = set(KNOWN_WORDS) | {"atak", "xml", "template", "tiles", "tile", "zoom", "quadkey", "imagery", "folder", "browsers", "browser", "viewer", "layer", "sources", "source"}
