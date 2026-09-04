---
name: mesh-lessons
description: Diagnose a Meshtastic mesh through Mesh Manager and know which signals to believe. Use before concluding anything is broken, when nodes vanish, when a radio looks alive but says nothing, or when positions are coarse. Names the tools behind each check.
audited:
audit_verdict:
audited_with:
audit_sha:
origin: mesh-manager/skills
source: MilUX Ltd
maintainer: MilUX Ltd
license: GPL-3.0-or-later
category: operations
---

# Mesh lessons: which signals to believe

Every rule here was paid for on a real mesh. Read `status` and `nodes` first; then apply these
in order.

**A quiet mesh is not a broken bridge.** `status` carries the last activity on the serial loop
and the last packet forwarded. Device chatter reaches the loop roughly every minute even with
no node talking, so a recent last activity with an old last forwarded means the radio is alive
and the mesh is quiet. Say that, and wait for a tracker to speak. Only silence on the loop with
the radio present is a hang, and the bridge's watchdog restarts it for you: `status` says
`not pinging` while that is happening.

**Database only is not on the mesh.** A node with `heard_here` false is in the radio's stored
database and has not been heard by this radio since the bridge started. Report it as not heard,
never as "no GPS fix" or "offline"; its name may be months stale.

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

**Two hills away is slow and lossy.** A `traceroute` or a `request_position` may take a minute
to answer, or not answer at all. Wait, then say what you saw on `log` and `messages` before you
call it a fault.

**The proof of a bridge is a marker on a client that signed in normally**, not a counter. If the
operator asks whether TAK is seeing the mesh, `status`'s last packet forwarded is the bridge's
half of the answer; the client screen is theirs.
