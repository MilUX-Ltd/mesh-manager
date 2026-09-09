---
name: mesh-operate
description: Triage a Meshtastic mesh through Mesh Manager and prepare the fix to the gate. Use when the operator says the mesh is down, a tracker has vanished, nobody is getting messages, or before taking the kit somewhere new. Runs reads, asks the mesh what only the mesh can answer, and hands over the decision.
audited: 2026-09-04
audit_verdict: pass with cautions
audited_with: skill-safety-audit (MilUX meta-skills)
audit_sha: 53ff2a010736d1e0
origin: mesh-manager/skills
source: MilUX Ltd
maintainer: MilUX Ltd
license: GPL-3.0-or-later
category: operations
---

# Operating the mesh: triage to the gate

Load `mesh-lessons` first. Then work top down; stop at the first thing that explains the
symptom, and say what you did not check.

## 1. The bridge and the radio

`status`. In this order: `radio_present`, `bootloader`, `connected`, `watchdog`,
`last_activity`, `last_forwarded`, `nodes_seen`. A false at the top explains everything below
it; report that one line and the physical action it needs.

## 2. Who is on the mesh, and how well

`links`, which is `nodes` with the picture: `direct_snr` is the SNR of the last packet that
came straight from that node, the only figure that describes its link to this radio; `history`
is the last two hundred readings, so a link getting worse shows before it fails. Split the
nodes: heard here recently, heard here but stale, database only (`heard_here` false). A node
at `hops` two or three is at the edge; a tracker at five per cent `battery` is about to leave.
Say which nodes are which, by id and name. `route` gives the last traceroute answer for a
node, hop by hop with the SNR at each hop, `towards` and `back`.

## 3. What they ride

`channels` and `config`: the primary channel, the region, the modem preset. If the operator's
brief (`mesh_context`) names a different region or channel, that is the finding.

## 4. Ask the mesh, at propose or above

Only what changes nothing: `traceroute` to a node that should be there (the answer arrives as
a route; read it with `route`), `request_position` from a tracker with no fix. Say what you
are sending and why first. Watch `log` and `messages` for the answer, and wait a minute
before reading silence as a fault.

## 4a. What the fleet is, and what you may change on it

`register`: every node the radio knows of joined with the box's register on radio id, the
operator's `label` and `holder` beside the node's own name, and `managed`, which is true only
when a read of the device itself showed this radio's public key among its admin keys. A
device that is not managed cannot be changed from here at all; the only thing to say about
it is "bring it to the bench". For a managed device, `node_read` asks it over the air for what
it holds now; `node_set`, `node_set_region`, `node_channel_push` and `node_reboot` are the
writes, at `act` or through `propose`. Every write answers `confirmed` true only when the
device's own answer matched; `unconfirmed` with a reason otherwise; a write over LoRa can
take a minute and silence is not failure. Region, slot 0 and reboot need `confirm` set to the
device's own id, because afterwards it may be unreachable over the air.

## 5. Hand over the decision

Anything that would change a device, a channel or a region is above the on-air line unless
you hold `act`. Use
`propose` with the exact action and arguments and a rationale in the operator's words, and
say what changes if they say yes and what the way back is. A region change before travel needs
two QRs: one for the destination and one for the way home; say so.

## Before the kit goes somewhere new

Read `config` for the region. Read the brief for where the kit is going. If they differ, the
first proposal is the region, and nothing else until the operator answers.
