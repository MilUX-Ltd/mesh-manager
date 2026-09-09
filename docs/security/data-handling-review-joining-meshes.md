# Data-handling review: joining meshes (ADR 003, slices 1 to 5)

Reviewed 5 September 2026 (Spec 056) against the code as released in 0.14.0 and the never-list in ADR 003. The
rule holds: what a box shares with another box is decided on the box, per peer, by its operator, and nothing crosses
by default that could open a mesh.

## What crosses

Per class, only when the sending side's table has Out for that peer and the receiving side's has In:

| Class | Fields | Default |
|---|---|---|
| nodes (the picture) | id, name, short, label, hardware, battery, voltage, charging, lat, lon, heard, snr, hops, icon, group, role | both ways |
| messages | from, name, to (broadcasts only), channel, channel name, text, time, the sender's request id | Out off, In on |
| waypoints | id, name, description, lat, lon, expiry, node, gone | both ways |
| alerts | state, node, kind, text, since | both ways |
| receipts | the request id, aired or not, why | with messages |

Every item carries `origin` (the site id, a hash of the site's public key), `origin_name` and `path` (the sites it
passed through). A hub relays items to its other peers under their own tables; the path stops loops.

## What never crosses

Direct messages (the class accepts broadcasts only, at both ends). Channel keys and channel URLs, admin keys, tokens,
passwords, device configuration exports and firmware. From 0.15.0 this is enforced in code, not convention:
`peers.NEVER_KEYS` names the keys, `Link.send` refuses an item carrying one at any depth and counts the refusal, and
`accept_item` refuses one that arrives, whoever sent it. A hostile or faulty peer cannot move a secret through a table.
Positions cross as part of the picture; that is the point of the picture, and In can be switched off per peer.

## At rest

The site identity (`site.key`, EC P-256) sits in the state directory at 0600; the certificate beside it. The peers
file holds each peer's id, name, address, pinned certificate fingerprint and table; no code survives its single use.
Remote messages, since 0.14.0, are rows in the history store (SQLite in the state directory) with their origin;
remote pictures, waypoints and alerts are held in memory only and rebuilt from the peers on connection. The GitHub
token for updates is a separate file at 0600, read by the screen; it is never on a link.

## In flight

TLS 1.2 or better on the peer port (8094 by default). Pairing is by a one-time code good for ten minutes; after it, the
peer's certificate is pinned and a changed certificate is refused. Frames are JSON lines; each side pings every 20
seconds and calls the other away after 60. The hub screens are on loopback and reached over an SSH tunnel; a TLS
route for them is the next item.

## Retention

The history store trims to its window (days, a setting). Catch-up reaches back 24 hours and 200 messages at most.
Remote pictures expire with the peer's link. Nothing of a peer is kept beyond the store's own window.

## What the exports and the agent see

The messages export (`/export/messages.csv`) carries every column of the store, so a remote row shows its `origin`,
`origin_name` and `channel_name` and can be told from the box's own. The history operation the screen and the agent
use returns remote rows the same way; an operator asking the agent about the last hour will be told what other
sites said, marked as theirs. The agent's own actions on peers (invite, join, forget, table changes, sending to a
remote chat) stay at propose in the role table.

## Forgetting a peer

Forgetting drops the peer record and, from 0.15.0, the picture, waypoints and open alerts held from that site. The
history keeps what it said: that is the operator's record of the mesh as it was, and the guide says so. A forgotten
peer that dials again is refused until a new invite is exchanged.

## Findings

1. The never-list was policy in the ADR and a whitelist in the picture, with no guard on the link itself: a bug in a
   later slice could have shared a field it should not. **fixed here** (the guard on send and accept, counted per link).
2. Forgetting a peer left its waypoints and open alerts on the screen until they expired or the box restarted.
   **fixed here**.
3. The messages export gained the origin columns with 0.14.0 by the store's own shape, unannounced; this review
   records it and the suite checks it. **fixed here** (the check).
4. Remote messages persist in the history after a peer is forgotten. **accepted**: it is the operator's record, and
   the store's window bounds it; stated in the guide.
5. The hub screens are reached over an SSH tunnel; the peer port is the only TLS surface. **next** (a TLS route for the
   screens, Caddy in front, on the roadmap).
6. A peer learns the ids of the sites on the path of a relayed item. **accepted**: an id is a hash of a public key and
   names no address.
