# The Mesh Manager skills

Four skills that travel with the product. Each is a directory with a `SKILL.md` in it. They are what the
[agent role](../agents/README.md) leans on, and they are useful on their own to any agent connected to a box.

| Skill | What it is for |
|---|---|
| `mesh-operate` | Running a mesh day to day: reading what the screen shows, working out what is actually wrong, and carrying the fix through at the autonomy you were given. |
| `mesh-onboard` | Bringing a new device onto the mesh and onto the register, so it is managed rather than merely present. |
| `mesh-lessons` | The mistakes already paid for on real meshes, and the rules that came out of them. The box reads this one itself, on the Help page. |
| `mesh-join` | Joining two boxes so two meshes are one picture, one chat and one set of waypoints. |

Install all four. The role names them in its frontmatter, which is a list for a person and not a mechanism:
nothing wires them together, so if you install one you get one.

## Where they go

| Your tool | Where |
|---|---|
| Claude Code | `~/.claude/skills/<name>/SKILL.md`, one directory per skill, or a project's `.claude/skills/`. Copy them; there is no install command. |
| Cowork, Claude Desktop, claude.ai | Customize > Skills > Add, uploading **one zip per skill**, with the skill's own folder as the archive's root. Your account needs code execution enabled. |

A box running 1.1.0 or later serves these itself, on the **Connections** page: one download with everything
at the paths Claude Code reads, and one download per skill already shaped the way the upload wants. Take them
from the box rather than from GitHub. That copy is the one the box is actually running, it is stamped with
the box's version, and if the copy in your tool later says an older version you know to fetch it again.

## Before you use them

Each carries audit frontmatter: `audited`, `audit_verdict`, `cautions_accepted`, `audited_with`, `origin`,
`source`, `maintainer`. Read it before you install, the way you would with any file that tells an agent what
to do. A skill whose verdict is not a pass is held back from a release rather than shipped quietly.

The full walk-through, including what the autonomy dial does to one real request, is **Working with an
agent** in [docs/GUIDE.md](../docs/GUIDE.md).
