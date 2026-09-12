#!/usr/bin/env python3
"""Spec 084: the agent's brief, true, audited, and gated on the path that matters.

Matt, looking at the public repository: "I also want to challenge your assertion that there was
nothing shipped, as I can see the following: .../agents and .../skills"

He was right. cut-public.sh copies agents/ and skills/ unconditionally, so the public repository
has carried them all along; cut-release.sh applies R-28 and ships empty directories to a box. The
gate sits on the path where it does least and is absent from the path where people take the files.
"""
import os, re, shutil, subprocess, sys, tempfile
sys.path.insert(0, os.path.dirname(__file__))
from _common import ROOT, check, check_true, finish, read, skip  # noqa: E402

BRIEF = sorted(
    [os.path.join("skills", d, "SKILL.md") for d in sorted(os.listdir(os.path.join(ROOT, "skills")))
     if os.path.isdir(os.path.join(ROOT, "skills", d))]
    + [os.path.join("agents", f) for f in sorted(os.listdir(os.path.join(ROOT, "agents"))) if f.endswith(".md")]
)


def front(path):
    t = read(path) or ""
    m = re.match(r"---\n(.*?)\n---\n", t, re.S)
    return dict(re.findall(r"^([a-z_]+):\s*(.*)$", m.group(1), re.M)) if m else {}


VERSION = (read("VERSION") or "").strip()

# ---- AC1 one gate, two callers -----------------------------------------------------------------
gate = read("release/skill-gate.sh")
check_true("AC1 there is one gate implementation", gate is not None)
for caller in ("release/cut-release.sh", "release/cut-public.sh"):
    src = read(caller) or ""
    check_true(f"AC1 {os.path.basename(caller)} sources it rather than carrying its own copy",
               "skill-gate.sh" in src)
check_true("AC1 and neither caller still decides for itself",
           sum(1 for c in ("release/cut-release.sh", "release/cut-public.sh")
               if re.search(r'audit_verdict[^\n]*==\s*"pass"', read(c) or "")) == 0)

# ---- AC2 to AC5 the rule, exercised ------------------------------------------------------------
if gate is None:
    skip("AC2 to AC5 the gate's decisions", "release/skill-gate.sh is not there yet")
else:
    work = tempfile.mkdtemp()
    shutil.copy(os.path.join(ROOT, "release", "skill-gate.sh"), work)

    def verdict_of(body):
        """Ask the gate about one file. Returns (ships, reason)."""
        f = os.path.join(work, "SKILL.md")
        open(f, "w").write(body)
        p = subprocess.run(["bash", "-c", f'source "{work}/skill-gate.sh"; if skill_ships "{f}"; then echo SHIPS; '
                                          f'else echo "HELD: $(skill_held_reason "{f}")"; fi'],
                           capture_output=True, text=True, timeout=30)
        out = (p.stdout or "").strip()
        return out.startswith("SHIPS"), out

    def doc(**kw):
        kw.setdefault("name", "a-skill")
        return "---\n" + "".join(f"{k}: {v}\n" for k, v in kw.items()) + "---\n\nbody\n"

    ships, _ = verdict_of(doc(audit_verdict="pass"))
    check("AC2 a clean pass ships", ships, True)
    ships, _ = verdict_of(doc(audit_verdict="pass with cautions", cautions_accepted="2026-09-04, Matt"))
    check("AC3 pass with cautions plus an acceptance ships", ships, True)
    ships, why = verdict_of(doc(audit_verdict="pass with cautions"))
    check("AC4 pass with cautions and no acceptance is held back", ships, False)
    check_true("AC4 and the reason names the missing acceptance", "accept" in why.lower(), why)
    for bad, label in ((doc(audit_verdict="fail"), "a fail"),
                       (doc(audit_verdict="pass with cautions", cautions_accepted=""), "an empty acceptance"),
                       (doc(), "no verdict at all"),
                       (doc(audit_verdict="probably fine"), "a verdict nobody defined")):
        ships, why = verdict_of(bad)
        check(f"AC5 {label} is held back", ships, False)
    ships, why = verdict_of(doc(audit_verdict="probably fine"))
    check_true("AC5 an unknown verdict is named, not silently dropped", "probably fine" in why, why)

# ---- AC6 both paths carry them -----------------------------------------------------------------
rel = read("release/cut-release.sh") or ""
pub = read("release/cut-public.sh") or ""
check_true("AC6 the release cut copies a file the gate passes", "skill_ships" in rel)
check_true("AC6 the public cut asks the gate before copying", "skill_ships" in pub)
check_true("AC6 the public cut no longer copies skills and agents blindly",
           re.search(r"for d in src bridge install agents skills", pub) is None)

# ---- AC7 every file says which product it describes ---------------------------------------------
for p in BRIEF:
    f = front(p)
    check(f"AC7 {p} declares the product version", f.get("product_version"), VERSION)

# ---- AC8 the fourth skill ------------------------------------------------------------------------
names = [front(p).get("name") for p in BRIEF]
check("AC8 there are five files: four skills and the role", len(BRIEF), 5)
check_true("AC8 mesh-join is one of them", "mesh-join" in names, str(names))
join = read("skills/mesh-join/SKILL.md") or ""
check_true("AC8 it covers joining sites", "peer" in join.lower() and "sharing" in join.lower())
check_true("AC8 and MQTT", "mqtt" in join.lower() and "broker" in join.lower())
check_true("AC8 including that the radio has no wifi of its own, so the box carries it",
           "wifi" in join.lower())

# ---- AC9 no lesson assumes TAK -------------------------------------------------------------------
lessons = read("skills/mesh-lessons/SKILL.md") or ""
check_true("AC9 the bridge lesson names the box shape rather than assuming TAK",
           "server" in lessons.lower() and "mode" in lessons.lower())
check_true("AC9 the old unconditional claim is gone",
           "The proof of a bridge is a marker on a client that signed in normally**, not a counter." not in lessons)
for p in BRIEF:
    body = read(p) or ""
    if "TAK" in body:
        check_true(f"AC9 {p} qualifies TAK where it mentions it",
                   re.search(r"(no TAK|without TAK|server mode|MODE=server|box that bridges)", body) is not None)

# ---- AC10 peers, and one definition of quiet ------------------------------------------------------
check_true("AC10 the brief knows a row can come from another site",
           "origin_name" in lessons or "another site" in lessons)
# prose wraps, so a check on prose must not care where the line broke
flat = re.sub(r"\s+", " ", lessons)
check_true("AC10 and carries the product's one definition of quiet",
           "four times its own median interval" in flat and "floor of two minutes" in flat)

# ---- AC11 the role promises nothing it cannot wire --------------------------------------------------
role = read("agents/mesh-manager-agent.md") or ""
check_true("AC11 the role says where the skills come from",
           re.search(r"(installed alongside|beside this file|same place|from the box)", role) is not None)
check_true("AC11 and what to do without them",
           re.search(r"(if you do not have|if they are not|without them)", role, re.I) is not None)

# ---- AC12 the three defects -------------------------------------------------------------------------
check("AC12 the stray table separator is gone", role.count("|---|---|"), 1)
check_true("AC12 audit_sha is no longer stale", front("agents/mesh-manager-agent.md").get("audit_sha") != "stale")
check_true("AC12 and the role no longer claims MQTT actions the catalogue does not carry",
           "gateway_mqtt_set" not in role and "node_mqtt_set" not in role)

# ---- AC13 the existing controls still hold -----------------------------------------------------------
sys.path.insert(0, os.path.join(ROOT, "src"))
from mesh_manager import catalogue as C  # noqa: E402
named = set(re.findall(r"`([a-z_]+)`", role))
check("AC13 the role still names every catalogue action", sorted(a["id"] for a in C.ACTIONS if a["id"] not in named), [])
for p in BRIEF:
    body = read(p) or ""
    check_true(f"AC13 {p} names no radio id", re.search(r"![0-9a-f]{8}", body) is None)
    check_true(f"AC13 {p} names no serial path", "/dev/serial/by-id/" not in body and "ttyACM" not in body)
    check_true(f"AC13 {p} names no coordinates", re.search(r"\b-?\d{1,2}\.\d{4,}\b", body) is None)
_priv = os.path.join(ROOT, "release", "private-strings.txt")
if os.path.exists(_priv):
    for bad in open(_priv).read().strip().split("|"):
        if re.fullmatch(r"[A-Za-z0-9_-]+", bad):
            for p in BRIEF:
                check_true(f"AC13 {p} names no fleet ({bad})", bad not in (read(p) or ""))

finish()
