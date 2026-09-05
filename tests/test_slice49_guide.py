#!/usr/bin/env python3
"""Spec 049: the user guide travels with the product, every screenshot it names is there."""
import os, re, subprocess, sys
sys.path.insert(0, os.path.dirname(__file__))
from _common import ROOT, check, check_true, finish, read, skip  # noqa: E402

g = read("docs/GUIDE.md")
check_true("AC1 docs/GUIDE.md exists", g is not None)
g = g or ""
SECTIONS = ["Setting up", "The mesh and the map", "Nodes", "Messages", "Channels", "Bench", "Register and groups", "Health and alerts", "Settings", "Updates", "Connections and agents", "Help"]
heads = re.findall(r"^## (.+)$", g, re.M)
check("AC1 every section is there", [s for s in SECTIONS if s not in heads], [])
imgs = re.findall(r"!\[[^\]]*\]\(([^)]+)\)", g)
check_true("AC2 the guide carries screenshots", len(imgs) >= 12, str(len(imgs)))
missing = [i for i in imgs if not os.path.exists(os.path.join(ROOT, "docs", i)) and not os.path.exists(os.path.join(ROOT, i.replace("../", "")))]
check("AC2 every image the guide names exists", missing, [])
check("AC2 every image lives under assets/guide", [i for i in imgs if "assets/guide/" not in i], [])
try:
    tracked = subprocess.run(["git", "ls-files", "assets/guide"], cwd=ROOT, capture_output=True, text=True, timeout=20).stdout.split()
    check("AC2 every image is tracked", [i for i in imgs if i.replace("../", "") not in tracked], [])
except (OSError, subprocess.SubprocessError) as ex:
    skip("AC2 every image is tracked", f"git not usable here: {ex}")
# each section has at least one image between its heading and the next
parts = re.split(r"^## .+$", g, flags=re.M)[1:]
check("AC3 every section has a screenshot", [SECTIONS[i] for i, p in enumerate(parts[:len(SECTIONS)]) if "![" not in p] if len(parts) >= len(SECTIONS) else ["(too few sections)"], [])
readme = read("README.md") or ""
check_true("AC4 the README links the guide", "docs/GUIDE.md" in readme)
cut = read("release/cut-public.sh")
if cut is None:
    skip("AC5 the cut copies the guide", "release tooling is not in this tree")
else:
    check_true("AC5 the cut copies the guide", "docs/GUIDE.md" in cut)
check_true("AC6 no radio id outside the demo block", all(re.fullmatch(r"!(ee0000[0-9]{2}|aa000001|bb000002|cc000003|dd000004|00000001|ffffffff)", x) for x in re.findall(r"![0-9a-f]{8}", g)), str(re.findall(r"![0-9a-f]{8}", g)[:5]))
check_true("AC6 no serial path or port", "/dev/serial/by-id/" not in g and "ttyACM" not in g)
finish()
