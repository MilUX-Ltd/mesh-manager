<p align="center">
  <img src="assets/mesh-manager-banner.svg" alt="Mesh Manager" width="820">
</p>

<p align="center"><strong>Manage the mesh from the box that carries the radio.</strong></p>

Mesh Manager runs on the computer the Meshtastic gateway radio is plugged into. It shows the
mesh as it is now, manages the devices on it, and bridges that mesh into TAK. One screen for
the radio layer under a deployment: what is out there, how well it is heard, where it is, what
is left in its battery, and what to do about any of it.

Most tools in this space watch a mesh. This one manages it: devices set up on the bench and
administered over the air, firmware from a verified shelf, channels minted and rotated, and
an agent that can do everything the screen can under rules you set.

**Status: 0.x.** In daily use on its author's own deployable kit and released often. The
version line stays 0.x until the interface settles; the
[releases page](../../releases) has the current one.

## What it does

- **The bridge.** Owns the gateway radio and forwards mesh traffic to a TAK Server as CoT,
  the tracker path and the phone path both. Its liveness is measured at the serial read loop,
  so a dead serial handle restarts it instead of leaving a live process and a silent mesh.
- **The mesh, live.** Every node the radio hears, with signal, hops, position, battery and a
  history behind each figure. Nothing on the screen reloads.
- **The map.** Nodes and links over Google, OpenStreetMap, the box's own offline MBTiles, or
  any ATAK custom map source you already carry. Trails that fade with age, a coverage layer
  of every position heard coloured by signal, range rings that follow the zoom, MGRS beside
  every position with a 1 km grid, and a pop-out window for a wall display.
- **The fleet.** A register of the devices you own. On the bench over USB: read, export,
  onboard, restore, and flash from a shelf verified by hash. Over the air: rename, region,
  channel push, reboot, every write read back before it is shown as done.
- **Health and alerts.** Channel utilisation with a plain verdict, transmit air time against
  the region's duty-cycle budget, packets per hour. A device gone quiet, a flat battery, an
  unknown node on the channel or a node outside a fence each raise an alert on the screen and
  a chat message into TAK.
- **Updates.** The screen checks for a release, verifies its hash, and applies it on one
  press. The box downloads nothing else.
- **An AI surface.** An MCP endpoint, an agent role and a set of skills, all derived from the
  same action catalogue the screen uses. Anything a person can do on the screen an agent can
  do through a connector, at the autonomy you set, and nothing else.

## What it needs

A Linux box with a Meshtastic radio on USB, Python 3.11 or later, and a TAK Server to forward
to if you want the bridge. Everything else travels in the release. The reference deployment is
a mini PC with a Heltec V4 gateway radio and Seeed T1000-E trackers, but nothing about the
hardware is hard-coded.

## Install

Take the release tarball and its `install.sh` from the
[releases page](../../releases), then, as root on the box:

```bash
./install.sh mesh-manager-<version>-amd64.tgz \
  --serial /dev/serial/by-id/<your radio> \
  --filter-group <your TAK group>
```

`--help` lists the rest: where to bind, whether to ask for a password, where the map tiles
live, how the box knows where it is, and how it updates. A dry run prints every line it would
write and changes nothing. The box builds its environment from wheels inside the tarball, so
the install works with no internet.

After that, updates are a press on the About page.

## Try it without a radio

[`docs/DEMO.md`](docs/DEMO.md) runs the whole screen against a demo bridge on any machine
with Python. No hardware, no TAK Server, nothing to undo afterwards:

```bash
python3 -m mesh_manager.demo /tmp/mm-demo.sock &
python3 -m mesh_manager.web --config /nonexistent --socket /tmp/mm-demo.sock \
    --etc "$(mktemp -d)" --bind 127.0.0.1 --port 8095 --no-auth
```

## Licence

**GPL-3.0-or-later.** Mesh Manager stands on the Meshtastic Python library and on
TAK-Meshtastic-Gateway, both GPL-3.0-or-later, so Mesh Manager is too. You may run it, study
it, change it and pass it on under the same terms. If you distribute it, or a device with it
on, the source goes with it.

Third-party work it stands on, each under its own licence with attribution kept, is listed in
[`NOTICE`](NOTICE); the licence texts travel inside every release under `LICENSES/`.

## Not affiliated

Meshtastic is a registered trademark of Meshtastic LLC. TAK, ATAK and the TAK Product Center
are the property of their respective owners. This project is not affiliated with, endorsed by
or supported by either, and ships neither TAK Server nor device firmware: you supply both.

## What travels with it

`CHANGELOG.md` is the record of what changed and why. `NOTICE` and `THIRD-PARTY.md` name the
third-party work this stands on, with the licence texts under `LICENSES/`. `agents/` and
`skills/` hold the agent role and the skills for the AI surface, and `install/` holds the
installer and the systemd units. The design notes and the build record stay in the private
repository this is cut from: they are about the firm that builds it, not about running it.

---

Built by [MilUX Ltd](https://milux.co.uk).
