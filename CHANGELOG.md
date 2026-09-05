# Changelog

## Unreleased

## 0.8.0 (5 September 2026) Matt: "it would be great if it worked more like a normal chat app", and "I also want a
user guide with screenshots created for the public repo." Fifty-three suites green.

- **Messages is a chat (Spec 048).** A list of chats on the left, one per live channel, one per
  radio spoken to directly and one per group, newest first with the last line, its time and an
  unread count. A click opens the chat on the right; up to three side by side on a laptop, one at a
  time on a phone. Bubbles carry the receipt (handed to the radio, delivered, not delivered and
  why); a message to everyone says it is never acknowledged. A direct message sends on Enter; a
  channel or group message asks first, because every device hears it. The conversations are derived
  in the browser from what the box holds and hears; the pure functions run under node in the suite.
  The newest chat opens itself on a wide screen. The demo carries a direct exchange.
- **The user guide (Spec 049).** `docs/GUIDE.md` travels with the public cut: one section per page
  in the order a new user meets them, fifteen screenshots from the demo at desktop and phone widths,
  taken by `release/guide-shots.sh` and committed under `assets/guide/`. The README links it.
- **The stranger check waits for a release that is still appearing.** Two verify runs had failed on
  one check, the tree ahead of the newest release, once because the tree was pushed after the
  release was created and once because GitHub's releases API had not yet listed a release published
  seconds before. The check now waits up to two minutes when the tree is ahead, then judges.
- **ADR 002 (proposed): deployment shapes and a Windows and macOS build.** An assessment for the
  owner's decision: three shapes as one setting (`tak-server`, `server`, `desktop`), what in the
  bridge is Linux-specific and how deep it goes, the packaging routes and the signing costs, the
  order to do it in. Nothing built.
- The messages API is seeded from the store again after a restart (it had been seeded by the page
  that no longer renders the rows).

## 0.7.0 (5 September 2026)

The UX pass and five features, one release. Four reviews (product manager, user
researcher, content designer, interaction designer) read every rendered page and `web.py`; their
ranked findings were built where they agreed, recorded on the card where they split. Fifty-one
suites green; every one of the five features had its spec at Definition of Ready and its suite
committed failing first.

- **Icons carry their words.** One helper builds every icon control with a name, a tip and a
  hidden word; the header's *Words on buttons* switch shows the word beside every glyph, on by
  default on a phone, remembered. Tooltips work on touch (a press held 450 ms shows the tip and
  swallows the tap), on Escape, and reach assistive technology; the native `title` attributes are
  gone. Destructive controls (Delete, Forget, Rotate key) and the primary nav stay words.
- **No browser dialogs.** Every confirm is an in-page panel (Yes, do it / No; focus on No; Escape
  cancels). The consequence tick points at itself. Read again fetches in place, no reload.
- **Reachable on a phone.** 44 px targets, 3:1 control edges on the brand blue-grey, focus rings,
  the state strip folding behind a chevron, More on the bottom bar opening upwards, the join QR
  as a dialog above the header, folding register columns.
- **The nav the operator lives on:** Mesh, Nodes, Messages, Channels, Health; Radio in More with
  the rest grouped. Home is the map, three cards and the rest folded; the node table lives on
  Nodes only. The map controls fold into *Map layers* (open on a wide screen); rings are a
  switch; a waypoint is placed by pressing the map or typing a grid reference.
- **Nodes** has a filter row (find, quiet, battery low, no fix, an order) and a *quiet* verdict
  against the Health threshold; the ask line reads as English. **Messages** sends first, has a
  State column, the button names the destination, a broadcast says it is never acknowledged.
  **Channels** says Slot and *no key*, has a join QR per channel (`channel_url`, over the
  socket, never the API), pushes a slot to every managed device with a per-device read-back, and
  dresses the adopt form as danger only when Replace is chosen. **Bench** names the device by
  what it is, Read then Onboard (label and holder written to the register in the same act), the
  rest under More, nothing flashes before a read, a role hint, the bootloader drill as three
  steps naming the file. **Health** puts alerts first and thresholds behind a fold with On/Off
  switches; the export is in words. **Settings** has three saves that say which, and *Where this
  box is* (`box_position_set`, kept on the box, a receiver's fix still first). **Help** starts
  with setting the kit up. Clocks are Zulu everywhere. Units, byte limits, empty states and
  broken states rewritten; one name for the radio.
- **Fleet inventory (Spec 043).** The register keeps each node's public key, hardware and role
  from the radio's database, written only when they change. A key that differs from the one on
  file raises alert kind `key` until `key_accept`. `inventory` lists every radio with the
  firmware it itself reported, the shelf's verdict in words (behind, on the shelf's version, no
  image, unknown) and the key's fingerprint; the Register page carries the columns and
  `/export/inventory.csv`.
- **Groups, tags and icons (Spec 044).** A node belongs to a group, carries tags and a map icon
  of its own or its group's, from a set of fourteen declared once and drawn once. The map, the
  node list, the trails, the neighbours, the alerts and the exports narrow by `group=`; a
  message to `group:<name>` is one direct message per member, each with its own receipt. Groups
  are made on the Register page; the node row's fold sets name, group, tags and icon in one save.
- **Geofence alerts (Spec 045).** Fences drawn on the map (press the corners and Finish, or a
  circle from a centre and a radius), named, with a crossing rule and a group. The alert pass
  keeps the last side of every fence and node and raises `geofence` on a crossing that matches;
  the opposite crossing clears it. The radius fence on Health is now named *around this box*.
- **Installable (Spec 046).** A manifest, icons at 192 and 512 (any and maskable), an Apple touch
  icon and an SVG, served before sign-in; drawn by `release/make-icons.py` in the brand's colours.
  No service worker. On iOS, Add to Home Screen opens it standalone over plain http; Android
  Chrome installs it as an app only over https, and offers a home-screen shortcut otherwise.
- **Playback with a timeline (Spec 047).** A row per node and a tick per report under the map,
  seekable by press or drag; play, to the start, reverse, speeds to 1000x, fit; space, the
  arrows, `[` `]`, `r` and `f`. Nothing interpolated; a node is hollow with its age when its last
  report is older than four times its median interval; the run since its last gap is bold. The
  trails window gains 3 d.
- The role's autonomy table was regenerated for the nine new actions; its R-28 re-audit is due.

## 0.6.0 (5 September 2026)

Nine features every tool in this class is expected to have, built one at a time in a single
release, each with a spec at Definition of Ready and its acceptance tests committed
failing first. Forty-six suites green. Routes on the map, which was on the list, was already built
and is not here.

- **Delivery receipts (Spec 034).** A text is handed to the radio; the radio now says whether it
  arrived. `send_text` asks for an ack and keeps the packet id; the routing answer becomes an
  `ack` event and the message row reads delivered, or the radio's own reason (`MAX_RETRANSMIT`,
  `NO_ROUTE`). The history keeps the outcome. The page had rendered an ack pill and listened for
  the event since 0.2; nothing had ever set either.
- **Environment sensors (Spec 035).** Temperature, humidity and pressure from nodes with a
  sensor board, on the air and in answers to an ask, in a new `environment` table; the node page
  shows the latest reading and temperature over time, only for a node that has ever reported one.
- **Node availability (Spec 036).** How much of the window each node was actually heard for, in
  hourly buckets to two days and daily beyond. A Heard % column on the register with the
  histogram in the tooltip, and the histogram on the node page. A node with nothing in the window
  is 0% and still listed.
- **Export (Spec 037).** `/export/<kind>.<format>`: positions as GPX, KML or CSV; messages,
  packets, telemetry and environment as CSV; a window and a node filter; a Download control on
  Health. Only what the box already holds.
- **Quick messages (Spec 038).** Up to eight presets on the box, edited on Settings, offered as
  buttons above the send form; a press fills the field, nothing is sent without the usual
  confirm. `quick_messages` and `quick_messages_set` in the catalogue.
- **The packet inspector (Spec 039).** A Packets page: every packet in the window, newest first,
  filtered by node, port and window, with per-port counts, live as packets arrive.
- **Playback (Spec 040).** A slider on the map replays the trails window: every node where it
  last was at that instant, the trail it had walked by then, the instant shown in UTC, and a play
  button. Built from the rows the map already fetches; `/api/trails` names the contract.
- **Waypoints across the bridge (Spec 041).** A waypoint heard on the mesh is kept, drawn on the
  map as a pin, and forwarded to TAK as a spot marker. `waypoint_send` drops one on the mesh from
  the Map page or from an agent at `propose`. TAK to mesh is unchanged and out of scope.
- **The mesh as a graph (Spec 042).** Neighbour-info reports become edges: who hears whom, at what
  SNR. A Graph page draws them, the map has a graph toggle, and where no node has the module on
  the page says so and how to turn it on.
- **The More menu no longer disappears behind the map.** Leaflet's controls sit at z-index 800;
  the menu was at 6, so with More open over the map the tiles painted over its lower half. The
  header, its menu and tooltips now sit above every Leaflet layer.
- The role's autonomy table is regenerated from the catalogue and its audit stamp is marked stale
  on purpose: the body changed, so R-28 says re-audit.

## 0.5.1 (4 September 2026)

Two things the kit showed the moment 0.5.0 was on it.

- **About offered to update the box to the version it was already running.** Whether a release
  is available was decided when the check ran and stored with it, and never reconsidered, so
  after an update the record still said yes. It is now judged against the version running now,
  everywhere it is read: the About card, the header pill and the apply route.
- **Staged releases grew without limit.** Every release the box takes leaves its tarball behind,
  about 20 MB, and nothing removed them: the kit had twelve, a quarter of a gigabyte. They are
  what a roll back returns to, so the five most recent are kept and older ones are removed as
  new releases arrive. The running version is never removed, and the page says so.

## 0.5.0 (4 September 2026)

The four slices carried on. Thirty-seven suites green.

- **Roll back a release (Spec 030).** An update is one press and ten seconds, so a bad release
  arrives just as fast, and the way back was an SSH session and a hand-run installer. Nothing
  new is downloaded: a successful apply removes only the READY marker, so every release the box
  has taken is still on disk. About lists them, checks the tarball's hash and re-applies one
  through the same root unit an update uses, beside the last update log. It returns the code and
  not the box's config, and says so. On a box in auto mode it says the checker will roll forward
  again. `update_staged` and `update_rollback` join the catalogue.
- **A GPS lamp on the state strip (Spec 031).** Where the box thinks it is decides where every
  node is drawn relative to it and what position goes on its own CoT. The bridge already recorded
  what each read of the receiver established and none of it reached the operator, so a box with
  no fix looked exactly like a box with a good one until the map was wrong. Not answering, no fix
  with satellites seen, or a fix with satellites used; the source and the time in the tooltip. A
  box with no receiver says nothing rather than showing a lamp that can never go green.
- **Ask a node for its name (Spec 032).** A device renamed over the air kept its old name until
  it next broadcast, which on a quiet mesh is an hour, and the only way out was Forget, which
  throws away everything the box has heard from a node to fix a label. A NODEINFO_APP sendData
  with a handler, never a blocking helper; the node learns the gateway's name in the same
  exchange. Fourth icon on the node row, after the three already there.
- **A bench radio's own fix (Spec 033).** `bench_read` and `bench_export` carry what the device
  says about its own receiver: the fix with its MGRS, age, satellites and altitude, or no fix, or
  position switched off in its config, which is a setting and not a fault. The Bench page shows
  it and offers the map at that point (`/map#at=lat,lon`).

Also: sizes read as `20.6 MB` rather than `0 KB`; the update architecture is a config value
(`UPDATE_ARCH`, default amd64) rather than the machine's own, because only amd64 is built and
sniffing would send every arm box looking for a release that does not exist.

## 0.4.4 (4 September 2026)

- **The demo carried real device identifiers.** Its sample nodes were built from captures of a
  live fleet and kept four real Meshtastic node ids and the gateway radio's real MAC in its USB
  path, and the demo ships with the product. The suite had listed one of those ids as a string
  that must never appear in the agent role, so the fault was recorded in one place and shipped
  from another. The demo now uses the synthetic `!ee0000NN` block, and the cut whitelists
  device identifiers rather than blacklisting known-bad ones, so a real id introduced tomorrow
  stops the cut instead of being published.
- The `*.png` rule in `.gitignore`, which exists to keep channel QRs out of git, silently kept
  the new screenshots out too, so a clean checkout would have cut a README with four broken
  images. That is the second time that rule has done this (LESSONS 23). The cut now refuses a
  README that names a file the staged tree does not have, whatever the cause.
- Four screenshots in the README, taken from the demo: the mesh on the map, the node list, the
  register, and health.

- `SECURITY.md`: a private route for reporting a fault, what to expect and when, which
  versions get fixes, and what counts as a fault in software that sits on the box carrying a
  deployment's radio. The public repository had no way for a finder to tell us privately,
  which pushed them towards telling everyone at once.
- `release/verify-public.sh` checks a published release as the stranger who receives it, with
  no credentials: the releases API, the downloads, the hash, the installer, an anonymous
  clone, the leak scan on both tree and tarball, every path the README names, and the tree's
  version against the newest release. It runs weekly and on every release, and it travels
  with the product so anyone can check what they downloaded.
- CODEOWNERS, so the gates and the script that decides what may leave the building cannot be
  changed without review.

## 0.4.3 (4 September 2026)

- A release is one anyone can install, so a box checks the public repository by default; a box
  that should take its releases from somewhere else names it in `UPDATE_REPO`. The previous
  release pointed every installation at a repository only its author can read.
- The public README pointed at five documents that do not travel with the product (the
  decision records, the specs, the lessons, the contributing gates and the context note), so
  its closing section now names what the tree actually carries. Its status line no longer
  carries a version number that goes stale on the next release.
- The banner sits at `assets/`, so it draws on the front page of the public repository, and
  the public cut no longer carries build detritus.

## 0.4.2 (4 September 2026)

- The source repository is now `MilUX-Ltd/milux-mesh-manager`, so the firm's own repositories
  carry the `milux-` prefix and the product's own name is free for the public one, as
  `the MilUX gateway repository` is to `vantage`. A box looks there for its updates; the public repository
  carries the product, not the update channel. A box installed before this keeps working
  either way, because the old name redirects until the public repository takes it, and this
  release moves the default before that happens.
  The name had been written in two places, the config's defaults and the update module's, and
  moving one left a box still checking the other. There is one now, and a check that says so.

## 0.4.1 (4 September 2026)

- The node rows carry three ask icons again. Adding the coverage survey as an action that
  takes a node put a fifth button on every row with its title as the label, which broke the
  layout; the row now takes only the quick asks, those whose one input is the node, and only
  those with an icon. The survey's own form is on the Map page, where it was.
- The map no longer abandons the imagery on one bad tile. A single transient error used to
  switch to whichever of the box's own map sets came first, which on a box carrying sets for
  another country left a blank map and no obvious way back. It now waits for several failures
  in a row, picks a set whose bounds actually cover where you are looking, and says which one
  it chose; if none covers you it says that instead and leaves the map alone.

- The demo is part of the product: `python3 -m mesh_manager.demo` runs the screen with no
  radio behind it, with made-up devices rather than a copy of anyone's estate. It was a test
  file; it ships now, so the instruction to try it without hardware works from a clean
  checkout.
- `release/cut-public.sh` assembles the public tree, the product and its user documents, and
  refuses to finish if the staged files still carry an estate address, a board reference, a
  local path or the name of the private repository. `release/third-party.py` writes
  `THIRD-PARTY.md` and `LICENSES/` from the release that actually ships, naming the three
  dependencies whose wheels carry no licence text rather than passing over them.
- Said plainly in the installer, the screen and the firmware pins: what used to name an
  internal repository now names the thing itself. One of those lines was on the Bench page,
  where it told an operator their firmware came from somewhere they have no access to.

## 0.4.0 (4 September 2026)

- The public face: a mark and a banner in the house style (four nodes, every link drawn, the
  gateway radio at the centre in the accent colour), the strapline "Manage the mesh from the
  box that carries the radio", and a README written for a reader who has never seen the
  product, with what it does, what it needs, how to install and update it, how to try it
  without a radio, the licence and why it is GPL, and the trademark position.

## 0.3.9 (4 September 2026)

- Map sources from TAK (Spec 029): any ATAK `<customMapSource>` XML in the box's map folder
  becomes a layer, so imagery that works in ATAK works here without conversion (ATAK's
  `{$z}/{$x}/{$y}` placeholders and its zoom and tile-type fields are honoured, and a
  quadkey source is listed with the reason it cannot be drawn). A form on the Map page adds
  one from a pasted XML or a tile URL template, saved on the box so every browser sees it;
  `map_sources`, `map_source_add` and `map_source_remove` join the catalogue. Google hybrid
  stays the default and the operator switches in the layer control.

## 0.3.8 (4 September 2026)

- Config drift (Spec 028): a fleet profile on the box (role, power, position interval,
  region, preset; unset fields unenforced), every read of a device leaves a snapshot in the
  register, and the Register page compares each device against the profile: in line,
  drifted with each field's is and should, or never read; a press brings a managed device
  into line over the air, power and interval by default, region, preset and role with the
  confirm naming the device.

## 0.3.7 (4 September 2026)

- The key rotation checklist (Spec 027): a rotation from the screen marks itself, one done
  elsewhere is marked by hand (`rotation_mark`), and the Channels page then counts every
  expected device (the register plus anyone heard in the last week) back on the new key as
  the radio hears it (`rotation_status`), refreshed as packets arrive.

## 0.3.6 (4 September 2026)

- Alerts that reach TAK (Spec 026): every minute the box judges four conditions, a
  registered device silent past a threshold, a battery under a threshold, a node not in the
  register, a node outside a fence around the box; each is one row in the history, one event
  for the screen and one GeoChat to All Chat Rooms on the TAK Server (counted in observe
  mode); thresholds set on the Health page (`alert_set`), a test button (`alert_test`), the
  open count on the state strip.

## 0.3.5 (4 September 2026)

- Telemetry over time (Spec 025): a node page (`/node?id=`), reached from the node's name on
  the Nodes table, with its facts, battery and voltage charts over 24 h or 7 d from the
  history store (the 20% line drawn, on-charge stretches noted), its last messages and its
  positions in the window.

## 0.3.4 (4 September 2026)

- Mesh health (Spec 024): a Health page (More) with the gateway's channel utilisation and
  its verdict (quiet, normal, busy, saturated), its transmit air time against the region's
  duty-cycle budget (10% on EU_868), packets per hour, nodes heard, a chart of utilisation
  by the hour and a per-node table; `health` in the catalogue; a Mesh health card on the
  overview.

## 0.3.3 (4 September 2026)

- MGRS and the grid (Spec 023): every position on the screen carries its MGRS beside the
  degrees (the node rows, the map legend, a readout on the map following the mouse); a grid
  control draws 1 km UTM lines from zoom 13 (10 km below) with the kilometre digits along
  the edges. The arithmetic is in `mgrs.py` and mirrored in the overlay.

## 0.3.2 (4 September 2026)

- Coverage survey (Spec 022): a coverage layer on the map, every heard position as a dot
  coloured by its signal band over a window (off, 3 h, 24 h, 7 d), hollow when it came
  through a relay; and survey mode, which asks one node for its position on an interval while
  someone walks it, so the layer fills in (`survey_start`, `survey_stop`, `survey_status`).

## 0.3.1 (4 September 2026)

- Track trails (Spec 021): each node's positions over a window (1, 3, 12 or 24 hours, or
  off) drawn under the markers, fading with age, a colour per node, hover for the node and
  the time; a jump over 2 km is not drawn.

## 0.3.0 (4 September 2026)

- The history store (Spec 020): positions, device telemetry, messages and packets the box
  hears are kept in SQLite under the state directory (30 days, 200 000 rows a table) and
  survive a restart; `history` and `history_summary` join the catalogue; the Messages page
  seeds from the store after a restart; About shows what the box remembers. The first of the
  0.3 kit slices.

## 0.2.12 (4 September 2026)

- A map fitted while its container had no size (a background tab, a hidden view, a page
  still laying out) showed the whole world at zoom 0 and stayed there. The first fit now
  waits for the container to have a size, and a resize, the tab coming back or the view
  being shown refits it.
- Centre on me (Matt): a button under the zoom control gives a one-kilometre view with this
  box in the middle; disabled, with a tooltip saying so, while the box has no position.

## 0.2.11 (4 September 2026)

- The icon buttons say what they do the moment you hover or focus them (Matt): an instant
  tooltip with the name and a one-line description, placed by script so no table clips it.

## 0.2.10 (4 September 2026)

- Shorter node rows, so more nodes fit without scrolling (Matt): the three asks and the
  Name control are icon buttons with the words in their tooltips and labels; the battery's
  voltage and age share one small line; the sparkline's last, best and worst figures are
  its tooltip; an empty result line takes no room. A live refresh of the Nodes table now
  keeps an open Name control and never replaces a row the operator is typing in.

## 0.2.9 (3 September 2026)

- A density pass on the screen (Matt: "everything is quite big"): base type 14 px, the tap
  token 32 px (buttons, inputs, fold controls), table cells at 4/8 px, smaller headings,
  labels, pills and sparklines. Spec 007's 44 px targets give way to the operator's ask; the
  tokens make it one line to change back.

## 0.2.8 (3 September 2026)

- The range rings read better on imagery: a gold line of weight 2 over a deep-green halo, dashed 4/6, both under the slider.

## 0.2.7 (3 September 2026)

- Batteries that are current (Spec 019): the radio's node database is a fallback only for a node
  heard in the last day, and the figure carries that time as its age; a node heard weeks ago
  reads "no reading". The bridge's battery store survives a restart. "Ask for a battery" on every
  row (`request_telemetry`), and the bridge asks every node heard in the last day on its own,
  every half hour (`TELEMETRY_ASK_SECS`, 0 to turn it off).
- "Forget the stale" on the Register page (`nodes_forget_stale`): every node not heard for a
  number of days leaves the radio's database in one press.
- The map in a window of its own: `/map/full` and a Pop out control on the Map page.
- The range rings follow the zoom (three rings inside the view, at 1, 2 or 5 x 10^n metres) and a
  slider sets their opacity from solid to invisible.

## 0.2.6 (3 September 2026)

- Battery truth (Spec 018): the bridge keeps each node's battery from the telemetry it hears,
  newest wins (the vendored gateway kept the highest of two records), with the voltage and
  the time; the library's node database and the gateway's figure are the fallbacks; a level
  above 100 means on external power and reads "on charge", never "101%".
- A stored or manually set radio position is no longer a source for the box's own position:
  nothing real means no position, and the plan view places nodes by hops.

## 0.2.5 (3 September 2026)

- gpsd's answer stands, fix or no fix: a reachable gpsd without a fix (indoors) no longer sends
  the bridge to open the port gpsd holds, so the once-a-minute "multiple access" warning goes;
  the map says "GPS receiver connected, no fix yet (N satellites seen, M used): placed among
  the devices it hears". A receiver that loses its fix stops placing the box after two misses.

## 0.2.4 (3 September 2026)

- Forget a node (Spec 017): a duplicate or a radio no longer used leaves the gateway radio's
  database and the box's lists from the Register page, its label and holder kept or dropped.
- Every Ask control says "asking the box" the moment it is pressed; a position request no
  longer blocks the bridge (the library's blocking wait, as with traceroute) and the answer
  lands in the row as "position received".
- The receiver is read through gpsd when the box runs it (the kit does, and it holds the
  port), else from the port as before.
- The screen's unit can write under /etc/mesh-manager and the state directory (ProtectSystem
  had made /etc read-only: the audit, the brief, minted connections and the GitHub token could
  not be written on a real box).
- Leaflet's images ship in the wheel, so the layer control has its icon.

## 0.2.3 (3 September 2026)

- Updates from GitHub (Spec 015): the screen checks the repository's releases daily with a
  read-only token entered on Settings (or `--github-token-file` at install), shows an update
  in the header and on About with its notes, and on Update now downloads the release, checks
  its hash and starts `mesh-manager-update.service`, a root helper the installer adds under a
  polkit rule that names that one unit, which installs it with the box's config kept; `auto`
  does the same on its own; `off` never talks to GitHub. `release/publish-release.sh`
  publishes every merged version as a pre-release with the three files.
- Display names (Spec 016): the register's label is shown wherever a node is named, with the
  radio's own name kept in the second line; a Name control on every node row.
- Google satellite and terrain join hybrid and roads on the map's layer control.
- The screen's directory under /etc is writable by the screen's user, so connections, the
  brief, the audit and the token can be written on a real box.

## 0.2.2 (3 September 2026)

- The box knows where it is (Spec 014): a fix from the box's own GPS receiver (found by its
  by-id name, or `--gps`), else the gateway radio's own GPS fix, else the position declared at
  install, else an estimate among the devices it hears, else the radio's stored position; the
  map says which. The receiver is never a bench device. `install.sh --no-map-position` clears a
  declared position so the receiver or the devices place the box.

## 0.2.1 (3 September 2026), the first beta

- The map overlay (Spec 013): the mesh drawn over tiles, Google hybrid by default, Google roads
  and OpenStreetMap on the layer control, and the box's own MBTiles sets served by the screen
  from `MAP_MBTILES_DIR` (`/opt/tak-maps` on the kit) with their attribution; the map switches
  to the box's own tiles when the internet ones fail; the plan view stays one press away and is
  what shows when the box has no position. Leaflet 1.9.4 vendored (BSD-2-Clause). The installer
  takes `--tiles` and `--mbtiles-dir`.

- The position declared at install (`--map-lat`/`--map-lon`) beats the gateway radio's own
  stored fix on the map; the kit's Heltec, with no GPS, carried a fix 330 km from the bench.
- The bench names the box's GPS receiver as what it is and offers nothing on it.
- A new release restarts the bridge (LESSONS 22).

- The Help page (Spec 012): the kit guide, the region check against what the installer
  declared, the shelf with the recovery images marked, the mesh lessons from the file the
  agent reads (shipped in the package), the four states of a write, where things are.

- Restore and firmware on the bench (Spec 010): a device's exports listed and restored to it
  (owner, channels, lora, device and position, each read back; its own keys untouched; an
  export from another device only as a deliberate clone); the firmware shelf from the
  release's pins (`firmware/PINS.json`: T1000-E 2.6.11 and 2.7.26, Heltec V3 and V4 2.7.26
  with their factory images, the nRF52 factory-erase images), each verified against its
  sha256 on the box; a flash from the Bench page that exports first, refuses a pin for other
  hardware, an unverified image or a missing confirm, then writes nRF52 devices through
  their UF2 bootloader (udisks, with a polkit rule the installer adds for the bridge's
  user) or ESP32 devices with esptool from the release's wheels, and reads the version back;
  a flash that does not come back names the recovery step and never reads as done.

## 0.2.0 (3 September 2026)

- Over the air (Spec 011): a managed device is read and written from the box through the
  gateway radio under its admin key. `node_read` asks the device itself for names, region,
  preset, role, power, position interval, whether our key is still among its admin keys and
  its first channel slots; `node_set`, `node_set_region`, `node_channel_push` and
  `node_reboot` write and read back from the device's own answers, the session passkey asked
  for first, every answer in the four states; an unmanaged device gets "bring it to the
  bench" and nothing on the air; region, slot 0 and reboot confirm by naming the device.
  The Register page opens Manage on a managed row; several admin round trips to one device
  now go out together and are waited for in one window.

- The register and the bench (Spec 009): a fleet register on the box (label, holder, note,
  hardware, firmware, role, managed) joined with the radio's node list on radio id and nothing
  else, the node's own name beside the operator's label; a Bench page listing the USB devices
  by their by-id name with the gateway's own radio never offered and a bootloader device
  flagged with its recovery step; Read, Export (owner, config and channels with keys, under
  the state directory at 0600) and Onboard (names, role, the gateway's primary channel and
  key, its region and preset, its public key as an admin key, every one read back from the
  device, the register updated, refused when three foreign keys fill the list). Six new
  catalogue actions, so agents get them as tools.

- The map and the link bar (Spec 008): the Mesh page opens on the mesh as a picture, this box
  at the centre, every heard node about it by geography when the box has a position (the
  radio's fix, or MAP_LAT/MAP_LON set at install with `--map-lat` and `--map-lon`) and by hop
  rings when it has not; links coloured by the SNR of the last direct packet with the figure on
  them, dashed for relayed-only nodes, none for database-only ones; each traceroute answer
  drawn hop to hop; a Map page under More; `/fragment/map` and `/fragment/route/<id>`.
- Traceroute no longer blocks: the bridge sends the request with its own handler and answers
  "asked" at once; the answer becomes a route record (hops out and back with quarter-dB
  figures, unknown as null) and a `route` event; the node row shows it as a link bar.
- The bridge keeps a link store: 200 SNR readings per node and the last direct SNR; `links`
  and `route` join the catalogue as read actions, so agents get them as tools.
- A sparkline of the last 40 readings beside each heard node's signal glyph, with last, best
  and worst.

**Slices 1 to 3 built overnight, 3 September 2026**.

- **The bridge carried in.** The patched TAK-Meshtastic-Gateway tree from the MilUX gateway repository's V2
  branch (commit 6e072c7) lives under `bridge/` as three patches with provenance headers,
  plus the TAK V2 dictionaries with pinned hashes. `release/cut-release.sh` cuts
  `mesh-manager-<ver>+milux.<rev>-<arch>.tgz` (the gateway wheel and the product's wheel built
  here, every dependency wheel at the estate-proven pins, licences, `RELEASE.json`);
  `--check` verifies the tree with no network. `install/install.sh` installs with no
  network, creates the TAK input with its filter group only if absent, keeps the heartbeat
  path as the health contract, adopts a box running the earlier gateway (stops and disables
  the old unit, keeps its file as the rollback), and never runs a firewall tool. Rehearsed
  offline on the deployable kit in a throwaway venv: the venv built from the bundled wheels
  alone and the live V2 capture decoded on the box.
- **The bridge as a package.** `mesh-manager-bridge` subclasses the gateway, runs its loop on a
  thread, waits for an absent radio instead of crashing, keeps a log ring, writes the
  heartbeat to a configurable state directory, and answers on a unix socket: status, nodes,
  channels (never the key), config, log, send_text, traceroute, request_position, and a live
  event stream. Liveness is measured at the serial read loop and reported to systemd's
  watchdog; a radio that is absent or in bootloader mode is waited for, not restarted into.
  Observe mode listens only. Proven live on the kit's second radio as an unprivileged user:
  ten nodes with fresh signal, the database-only node flagged as not heard, the running
  gateway untouched.
- **The screen.** `mesh-manager-web`, loopback by default: sign-in with a PBKDF2 hash and a
  signed cookie, throttling, Overview, Nodes, Log and Channels live over SSE, the primary
  channel's QR rendered server-side with the key and the join URL in no page, URL or log,
  About, a JSON API. MilUX palette. Proven on the kit on loopback and in a browser on this
  machine against a fake bridge.
- **Messages and on-air requests from the screen (slice 5, the on-air half).** A Messages
  page with the chat the bridge has heard or sent and a form to send to a channel or a node,
  200 bytes at most, with the every-device confirm; Traceroute and Ask-position controls per
  node; a Radio page showing this radio's settings, read only. Proven on the kit: a message
  sent from the screen path went out on the primary channel from the second radio, and a
  traceroute was asked of a live tracker.
- **The AI surface (slice 8).** One catalogue drives the screen's routes and forms and the
  MCP's tools, with a parity test. `/mcp` (JSON-RPC 2.0) with hashed bearer tokens minted on
  the Connections page or by `mesh-manager-web --mint-connection` at an operator-set
  autonomy: observe sees the reads and `mesh_context`; propose adds the on-air actions and
  `propose`, which queues anything for a person on the Activity page; act everything the
  catalogue carries. Every call, proposal, run and dismissal is audited under the connection's
  name and shown on Activity. Settings holds the standing brief served as `mesh_context`.
  The role and the first two skills are in the repository, unaudited and held back from the
  release until Matt audits them. Proven on the kit: a connection minted, `initialize`,
  `tools/list` (twelve tools at propose), `status`, `mesh_context` and `nodes` called, a
  message proposed by the agent and run by a person from the Activity path, all audited.
- **Found by the rehearsal, not the suites.** The bridge subscribed to pubsub before the
  gateway and declared the topic differently, so the gateway's own subscription failed
  (LESSONS 16). The QR library sat in the wheel set with nothing declaring it (LESSONS 6,
  again). The radio's debug log arrives with colour codes; they are stripped.
