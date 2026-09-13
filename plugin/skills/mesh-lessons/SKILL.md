---
name: mesh-lessons
description: Diagnose a Meshtastic mesh through Mesh Manager and know which signals to believe. Use before concluding anything is broken, when nodes vanish, when a radio looks alive but says nothing, when a node you do not recognise appears, or when positions are coarse. Names the tools behind each check.
audited: 2026-09-12
audit_verdict: pass with cautions
cautions_accepted: 2026-09-12, Matt Odell, MilUX Ltd
audited_with: skill-safety-audit (MilUX meta-skills)
audit_sha: 6c1f0a94d2b7e358
product_version: 1.2.0
origin: mesh-manager/skills
source: MilUX Ltd
maintainer: MilUX Ltd
license: GPL-3.0-or-later
category: operations
---

# Mesh lessons: which signals to believe

Every rule here was paid for on a real mesh. Read `status` and `nodes` first; then apply these
in order.

**Know which shape of box you are on before you judge anything.** `status` carries `mode`. A
`tak-server` box bridges the mesh into a TAK Server; a `server` box has no TAK at all; a `desktop`
box is somebody's laptop with a radio in the USB port. The same symptom means different things on
each, and half the checks below have a different answer depending on which you are looking at. If
you skip this you will report a broken bridge on a box that was never meant to have one.

**A quiet mesh is not a broken bridge.** `status` carries the last activity on the serial loop
and the last packet forwarded. Device chatter reaches the loop roughly every minute even with
no node talking, so a recent last activity with an old last forwarded means the radio is alive
and the mesh is quiet. Say that, and wait for a tracker to speak. Only silence on the loop with
the radio present is a hang, and the bridge's watchdog restarts it for you: `status` says
`not pinging` while that is happening.

**Quiet has one definition and this is it.** A node is quiet when it has been silent for four
times its own median interval between reports, with a floor of two minutes. That is the node's
own rhythm, not a number imposed on it: a tracker reporting every thirty seconds is quiet after
two minutes, and an hourly one is not quiet until four hours. Where there is no history to read a
rhythm from, a stated floor is used instead of a guess. The map and the playback both use this
rule, so use the same one in what you say and do not invent a second.

**A quiet node is still on the map, and still does not count.** It is drawn where it last was,
marked, and it never moves a combined marker's centre of mass, because a position from six hours
ago is not a statement about where anything is now. Read a combined marker's count as nodes, and
its wording for how many of them have gone quiet.

**Database only is not on the mesh.** A node with `heard_here` false is in the radio's stored
database and has not been heard by this radio since the bridge started. Report it as not heard,
never as "no GPS fix" or "offline"; its name may be months stale.

**A row from another site is that site's picture, not yours.** Where boxes are joined, a node can
arrive over the link carrying `origin` and `origin_name`. This radio has not heard it. It is as
true as the far site's own reading and as old as the last catch-up, and it is not evidence about
your own air. Say which site a row came from whenever you report one, and never treat a peer's
node as something your operator can reach on the bench.

**Names are labels, never identity.** Join on the radio id. Two records with the same name are
two radios or one renamed radio; a renamed tracker can wear its old name for months in someone
else's database. When the operator says "there is no such node", believe them and check the id.

**A radio in bootloader mode presents a serial port and answers nothing.** `status` says
`bootloader` true; the bridge waits rather than restarting into it. The fix is physical: re-seat
the radio, and if it still shows bootloader, re-flash it from the bench. Do not propose a restart
for this.

**A missing radio is an operator action.** `status` with `radio_present` false means the cable
or the port, not the code. Say so, name the by-id path, and stop.

**A tracker on the charger while switched off is dark.** It charges without booting and shows a
charging light. Off the mesh whatever the light says.

**A channel carries more than a key.** The join QR carries the region and the position
precision too. Coarse positions on the map are usually a precision setting on the device, not a
GPS fault; and a QR from one country programs a fleet onto another country's spectrum. Before
anything travels, read `channels` and `config` and say what region the radio is on.

**The radio's MQTT is not the radio's own connection.** A radio on a box is on a USB cable and has
no wifi of its own, so it cannot reach a broker by itself; the box carries its MQTT for it. If MQTT
looks dead, the question is whether the box's proxy is running and connected, not whether the radio
has a network. `mesh-join` has the detail.

**Two hills away is slow and lossy.** A `traceroute` or a `request_position` may take a minute
to answer, or not answer at all. Wait, then say what you saw on `log` and `messages` before you
call it a fault.

**The proof of a bridge depends on what the box bridges to.** On a `tak-server` box it is a marker
on a TAK client that signed in normally, not a counter: `status`'s last packet forwarded is the
bridge's half of the answer and the client screen is the operator's. On a `server` box there is no
TAK, so there is nothing to look for on a client and saying "check TAK" is asking for something
that does not exist; the proof there is the picture on the screen, and where sites are joined, the
peer's copy of it. Read `mode` before you name a test.
