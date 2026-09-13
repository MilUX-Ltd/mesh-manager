---
name: mesh-join
description: Reach past this mesh's own radio, either by joining another Mesh Manager site over the internet or by putting the radio's traffic on an MQTT broker. Use when the operator asks about seeing another site's nodes, sharing messages or waypoints between sites, connecting a phone or another application to the mesh, or when something that should have crossed has not. The two are different things and are routinely confused.
audited: 2026-09-12
audit_verdict: pass with cautions
cautions_accepted: 2026-09-12, Matt Odell, MilUX Ltd
audited_with: skill-safety-audit (MilUX meta-skills)
audit_sha: b2d5487ff1c390ae
product_version: 1.1.1
origin: mesh-manager/skills
source: MilUX Ltd
maintainer: MilUX Ltd
license: GPL-3.0-or-later
category: operations
---

# Reaching past this radio

Load `mesh-lessons` first. There are two ways a mesh reaches past the radio on this box, they
both feel like "connections" to an operator, and they answer different questions. Work out which
one is being asked about before you read anything.

**Joining sites** puts two Mesh Managers in touch over the internet, box to box, with no broker in
the middle. It carries the picture, messages, waypoints and alerts, by agreement, each way.

**MQTT** puts this radio's own traffic on a broker, where anything else on that broker can see it,
a phone running the Meshtastic application included. It carries packets, not a curated picture,
and it has nothing to do with the other sites.

## Joining sites

`peers` lists what this box is joined to and what each link is doing. Each peer carries a
**sharing table** saying what leaves here and what is accepted from there, one row per class:
the picture (nodes, positions, battery, signal), messages, waypoints and alerts. Read it before
you answer any question about why something did or did not cross; the answer is usually a row set
to off, and that is a decision somebody made rather than a fault.

**Say which site a thing came from, every time.** A node, a message or a waypoint that arrived
over a link carries `origin` and `origin_name`. It is the far site's reading, as old as the last
catch-up, and this radio never heard it. An operator who thinks a peer's tracker is on their own
air will go looking for it with a directional antenna.

**What never crosses is enforced in code, not by convention.** Direct messages do not cross. Keys
do not cross. A device's configuration does not cross. Do not offer to arrange any of it, and do
not describe the boundary as a setting.

**A site with a radio can put a peer's messages on its own air**, if its table says so. That costs
airtime on the local mesh and reaches every device on the channel, so treat it as you would any
broadcast: say what is about to go out and why.

**After a gap, catch-up fills the hole.** A link that was down does not lose the window; it
reconciles when it comes back. Silence on a link for an hour is not proof of loss, so read
`peers` for when the link was last good before calling anything missing.

`peer_invite`, `peer_join`, `peer_forget` and `peer_sharing_set` are the writes, at `act` or
through `propose`. Joining a site is a standing arrangement between two operators, not a
configuration change: propose it with the name of the far site and what the table would let out,
and let the operator answer.

## MQTT

The settings live **on the radio**, which is why a phone paired to it picks them up. The radio on
a box is on a USB cable and has no wifi of its own, so it cannot reach a broker by itself: the box
holds the connection and carries the radio's MQTT for it. Nothing on the radio needs a network.

So when MQTT is not working, the questions in order are whether the box's proxy is running,
whether it is connected to the broker, what it has carried each way, and only then anything about
the radio. A radio with the settings written and no proxy running is silent and correct.

Turning it off is the same form as turning it on, and it writes that to the radio too. A password
is write-only everywhere it appears: the screen says whether one is set and never what it is, and
you never ask for it, repeat it, or put it in a proposal.

## The question to ask first

If the operator wants **another site's people on their map**, that is joining. If they want
**their own mesh visible to something else**, a phone, another application, a broker somebody else
runs, that is MQTT. If they want both, they are two pieces of work and the second does not follow
from the first.
