---
name: mesh-onboard
description: Bring a device into the fleet on the bench, coaching the person at the cable. Use when the operator has a new tracker or radio in hand, when a device must be read or exported before a change, or when a device that was managed has stopped answering over the air. Works through Mesh Manager's bench actions; every write is read back from the device.
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

# Onboarding on the bench

Load `mesh-lessons` first. The person is at the box with a cable; you are at the screen or
the connector. Say one step at a time and wait for what they see.

## 1. Find the device

`bench_devices`. It lists the USB devices by their by-id name, never the gateway's own radio.
Ask the person to plug the device in if the list is empty, then read it again. A device
marked `bootloader` answers nothing: read them the `recovery` step and stop there until it
comes back as a normal port.

## 2. Read it before you change it

`bench_read` with the device's `path`. Say what it is: the names, hardware, `firmware`,
region and preset, `role`, its channels by name, and whether it is `managed` (this radio's
key among its `admin_keys`). If it holds three foreign admin keys, onboarding will be
refused; say so now, not after.

## 3. Keep what it had

`bench_export` with the `path`. The export lands on the box, keys included, at a path the
answer names; nothing of it comes back to you, and you never ask for its content.

## 4. Onboard

Agree the `long_name` (up to 39 bytes), the `short_name` (4) and the `role` with the operator
in their words, then `bench_onboard`. It writes the names and role, this radio's primary
channel and key, its region and preset, and this radio's public key as an admin key, and
reads every one back from the device. Read the answer back to the person: `confirmed` true
and what `read_back` says, or `unconfirmed` with the reason. Never say "done" for an
unconfirmed write.

## 5. Check the register

`register`: the device now carries `managed` true and an `onboarded_at`. If the operator
wants a `label` or a `holder` on it, `register_set`. The device is now one you can reach
over the air (`mesh-operate`, step 4a) once it is heard on the mesh.

## What you never do

Open the gateway's own radio from the bench, it is set from the Radio page. Read a key out
of an export or an answer. Treat a name the device came with as anything but a label.
