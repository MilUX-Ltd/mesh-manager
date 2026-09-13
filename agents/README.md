# The Mesh Manager agent role

One file: `mesh-manager-agent.md`. It is the standing role for any AI connected to a Mesh Manager box over
MCP. It says how to read a mesh, what to do about what it finds, and where to stop and hand back.

A connection token gets an agent the tools. This file is what makes it behave like someone who has run a
mesh before.

## Where it goes

| Your tool | Where |
|---|---|
| Claude Code | `~/.claude/agents/mesh-manager-agent.md`, or a project's `.claude/agents/`. Copy it; there is no install command. |
| Cowork | Sub-agents arrive with a plugin rather than a single file. Until Mesh Manager ships one, paste the body of this file into your own agent definition. |

A box running 1.1.0 or later serves this file itself, on the **Connections** page, along with the skills. Take
it from there rather than from GitHub: that copy is the one the box is actually running, and it is stamped
with the box's version.

## The autonomy dial

The role ships at `propose` and never argues for more. What it can actually do is set per connection on the
box, not here:

- **observe** reads and reports, and nothing else.
- **propose** also asks the mesh what only the mesh can answer, and queues anything further for a person on
  the Activity page.
- **act** carries deterministic changes through itself. That includes flashing firmware on the bench,
  restoring a configuration, forgetting nodes and rolling the software back, so grant it deliberately and per
  connection.

Every call is audited under the connection's name on Activity, whatever the dial says.

## Before you use it

It carries audit frontmatter: `audited`, `audit_verdict`, `cautions_accepted`, `audited_with`, `origin`,
`source`, `maintainer`. It is `pass with cautions`, and the caution is the one above about `act`. Read the
frontmatter before you install it, the way you would with any file that tells an agent what to do.

The full walk-through, including the skills and what the dial does to one real request, is
**Working with an agent** in [docs/GUIDE.md](../docs/GUIDE.md).
