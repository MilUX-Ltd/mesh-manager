# Reporting a vulnerability

**Do not open a public issue for a security fault.** Mesh Manager sits on the box that
carries a deployment's gateway radio and bridges that mesh into TAK, so a fault in it can
expose positions and traffic that matter to whoever is carrying the radios. A public issue
tells everyone at once, including them.

**Use GitHub's private reporting:** the **Security** tab of this repository, then **Report a
vulnerability**. It opens a private thread that only the maintainers can see. If that is
unavailable to you, email `matt@milux.co.uk` with `mesh-manager security` in the subject.

Tell us what you can: the version (the About page, or the `VERSION` file), what you did, what
happened, and what you think it lets an attacker do. A proof of concept helps and is not
required. Report it even if you are unsure it is a fault.

**What to expect.** An acknowledgement within three working days, and an assessment within ten.
If we agree it is a fault we will tell you what we intend to do and roughly when. If we do not
agree we will say why, and you are free to disagree in public afterwards. We will credit you
in the release notes unless you would rather we did not. There is no bounty.

Please give us a reasonable chance to release a fix before describing the fault publicly.
Ninety days is the usual courtesy, sooner if it is already being exploited, and we would
rather you published than sat on something we had gone quiet about.

## Which versions get fixes

The newest release, and nothing else. The version line is `0.x` and moves quickly; there are
no maintained branches behind it. A box updates from the About page in about ten seconds, so
the fix for almost anything is to take the current release.

## What this software is, in security terms

Worth knowing before you look, so you can judge what counts as a fault:

- **The screen binds to `127.0.0.1` by default** and asks for a password. An operator can bind
  it to the box's LAN address, and is expected to know what that means. A password is on by
  default and can be turned off deliberately (`--no-auth`). Reaching an operator-exposed screen
  from the operator's own LAN is not a fault; getting past the password is.
- **There is no TLS.** The screen is plain HTTP, on the loopback interface or a local network
  the operator controls. Putting it on an untrusted network is out of the design.
- **The radio is a trusted peripheral.** A device on the mesh channel is inside the trust
  boundary by design, because it holds the channel key. Anything a mesh peer can do to the
  bridge by sending well-formed traffic is expected; anything it can do by sending malformed
  traffic is a fault worth reporting.
- **Channel keys, admin keys and the update token are secrets** and live in the box's state and
  config directories, not in this repository. Any path by which the screen, the API or the MCP
  endpoint discloses one is a fault.
- **The MCP endpoint runs at an autonomy the operator sets** (`observe`, `propose`, `act`) with
  token-attributed audit. A connected agent exceeding the set autonomy, or acting without an
  audit record, is a fault.
- **The update path** takes a release from GitHub, verifies its SHA-256 against the published
  hash, and hands it to a root helper unit that may install only that unit's own payload.
  Anything that gets unverified content through it is a serious fault.

## The tree you are reading

This repository is **generated**. It is cut from a private source repository by a script that
strips the firm's own working record and refuses to publish a tree that still carries it, so
commits here are whole-release snapshots and pull requests against them would be overwritten
by the next cut. Send a **pull request only if you have been asked to**; otherwise open an
issue, or say what you would change in your vulnerability report, and it will be made in the
source and arrive in the next release with credit.
