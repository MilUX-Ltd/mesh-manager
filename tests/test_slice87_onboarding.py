#!/usr/bin/env python3
"""Spec 087: how to onboard the agent and the skills into your own tool.

Matt landed on the public repository's /skills, saw four directories and no explanation. The
consumption is not in the app: the Connections page mints a token and prints a claude mcp add
line, which carries the endpoint and nothing else. Installing the role and the skills happens in
the operator's own tool and the product says nothing about how.

Every lookup here is guarded, so a missing chapter is one verdict rather than a crash that hides
the next twenty.
"""
import http.client, io, os, re, sys, tempfile, threading, time, zipfile
sys.path.insert(0, os.path.dirname(__file__))
from _common import ROOT, check, check_true, finish, read, skip  # noqa: E402
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "tests"))
from fakebridge_lib import start_fake_bridge  # noqa: E402
from mesh_manager import web as W  # noqa: E402

SKILLS = ["mesh-join", "mesh-lessons", "mesh-onboard", "mesh-operate"]
CHAPTER = "Working with an agent"
guide = read("docs/GUIDE.md") or ""
heads = re.findall(r"^## (.+)$", guide, re.M)
bodies = dict(zip(heads, re.split(r"^## .+$", guide, flags=re.M)[1:]))
chapter = bodies.get(CHAPTER, "")

# ---- AC1 the chapter is in the guide, with its picture, and the guide suite knows its name -------
check_true(f"AC1 the guide has a {CHAPTER!r} chapter", CHAPTER in heads, str(heads))
check_true("AC1 the chapter carries a screenshot", "![" in chapter)
_shots = [i for i in re.findall(r"!\[[^\]]*\]\(([^)]+)\)", chapter)
          if not os.path.exists(os.path.join(ROOT, "docs", i))]
check("AC1 and the screenshot exists", _shots, [])
check_true("AC1 the guide suite names the chapter, so it cannot be dropped",
           CHAPTER in (read("tests/test_slice49_guide.py") or ""))

# ---- AC2 the four steps --------------------------------------------------------------------------
for want, label in ((r"Connections", "mint a connection"), (r"claude mcp add|MCP server", "add the MCP server"),
                    (r"~/\.claude|Customize", "install the role and the skills"),
                    (r"Activity", "what the dial gave away, and the audit")):
    check_true(f"AC2 the chapter covers: {label}", re.search(want, chapter) is not None)

# ---- AC3 the exact mechanics, and when they were checked -------------------------------------------
check_true("AC3 Claude Code's skills path, exactly", "~/.claude/skills/" in chapter)
check_true("AC3 Claude Code's agents path, exactly", "~/.claude/agents/" in chapter)
check_true("AC3 the upload route's UI path", re.search(r"Customize\s*>\s*Skills", chapter) is not None)
check_true("AC3 and it says the upload wants one zip per skill",
           re.search(r"one zip per skill|a zip .*each skill|per skill", chapter, re.I) is not None)
check_true("AC3 the chapter dates what was checked, because these move",
           re.search(r"[Cc]hecked[^.\n]{0,60}(September|October|November|December)\s+2026", chapter) is not None,
           chapter[:0] or "no dated 'checked' line")

# ---- AC4 the READMEs where a person actually lands ---------------------------------------------------
for p in ("agents/README.md", "skills/README.md"):
    t = read(p) or ""
    check_true(f"AC4 {p} exists and says something", len(t) > 400, f"{len(t)} bytes")
    check_true(f"AC4 {p} says where the files go", "~/.claude" in t or "Customize" in t)
    check_true(f"AC4 {p} points at the guide chapter", "GUIDE.md" in t)
check_true("AC4 skills/README.md names every skill", all(s in (read("skills/README.md") or "") for s in SKILLS))
cut = read("release/cut-public.sh")
if cut is None:
    skip("AC4 the public cut carries the READMEs", "release tooling is not in this tree")
else:
    # Was: assert the words "agents" and "skills" appear somewhere in the script. They always did,
    # the check always passed, and the READMEs still did not reach the public tree: the gate-driven
    # copy walks skill_files, which skips READMEs on purpose. Caught at the release gate on
    # 12 September 2026. Now it names the copy itself.
    check_true("AC4 the public cut copies agents/README.md by name", "agents/README.md" in cut)
    check_true("AC4 and skills/README.md", "skills/README.md" in cut)

# ---- AC6, AC12 the brief ships inside the package, and cannot drift from the root -------------------
PKG = os.path.join(ROOT, "src", "mesh_manager")
brief_role = os.path.join(PKG, "brief", "agents", "mesh-manager-agent.md")
check_true("AC6 the role ships inside the package", os.path.exists(brief_role))
_missing = [s for s in SKILLS if not os.path.exists(os.path.join(PKG, "brief", "skills", s, "SKILL.md"))]
check("AC6 and every skill with it", _missing, [])
_drift = []
for src, dst in [("agents/mesh-manager-agent.md", brief_role)] + \
                [(f"skills/{s}/SKILL.md", os.path.join(PKG, "brief", "skills", s, "SKILL.md")) for s in SKILLS]:
    a, b = read(src), (read(os.path.relpath(dst, ROOT)) if os.path.exists(dst) else None)
    if a != b:
        _drift.append(src)
check("AC6 the packaged copy is the repository's, not a fork of it", _drift, [])
_pp = read("pyproject.toml") or ""
check_true("AC6 the packaging config carries it", "brief/" in _pp, _pp[_pp.find("package-data"):][:200])
# The Help page reads the package path and the agent reads the skill path. It is a symlink today,
# so they cannot differ; the check is here so that replacing it with a copy fails rather than rots.
check("AC12 the packaged lessons path holds the skill's own bytes",
      read("src/mesh_manager/mesh-lessons.md") == read("skills/mesh-lessons/SKILL.md"), True)

# ---- the screen ---------------------------------------------------------------------------------------
fb = start_fake_bridge()
etc = tempfile.mkdtemp()
srv = W.make_server(bind="127.0.0.1", port=0, socket_path=fb.path, etc_dir=etc,
                    config={"AUTH": "off"}, state_dir=tempfile.mkdtemp())
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
time.sleep(0.3)


def get(path, port=port):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
    c.request("GET", path)
    r = c.getresponse()
    body = r.read()
    c.close()
    return r.status, dict(r.getheaders()), body


# ---- AC5 the Connections page carries the next step -------------------------------------------------
st, _, raw = get("/connections")
page = raw.decode("utf-8", "replace")
check("AC5 the Connections page answers", st, 200)
check_true("AC5 it says the agent needs the role and the skills too",
           re.search(r"role and the skills|role and skills", page) is not None)
check_true("AC5 and links the chapter itself", "docs/GUIDE.md" in page or "/guide" in page, page[:0] or "no guide link")

# ---- AC7 one download for the copy route -------------------------------------------------------------
st, hdr, raw = get("/agent-brief.zip")
check("AC7 the box serves the brief", st, 200)
if st == 200:
    try:
        z = zipfile.ZipFile(io.BytesIO(raw))
        names = z.namelist()
    except zipfile.BadZipFile:
        names = []
        check_true("AC7 and it is a zip", False, raw[:60].decode("latin-1"))
    check_true("AC7 the role is in it, where Claude Code reads it",
               "agents/mesh-manager-agent.md" in names, str(names[:8]))
    check("AC7 and every skill, at its own path",
          [s for s in SKILLS if f"skills/{s}/SKILL.md" not in names], [])
    # AC9 the stamp: the running box's version, in the zip itself
    check_true("AC9 the download is stamped with the box's version",
               any(W.__version__ in (z.read(n).decode("utf-8", "replace")[:400] if n.endswith((".md", ".txt")) else "")
                   for n in names) or any("VERSION" in n for n in names), str(names[:8]))

# ---- AC8 one download per skill, shaped the way the upload wants ---------------------------------------
for s in SKILLS:
    st, _, raw = get(f"/skill/{s}.zip")
    check(f"AC8 the box serves {s} on its own", st, 200)
    if st != 200:
        continue
    try:
        names = zipfile.ZipFile(io.BytesIO(raw)).namelist()
    except zipfile.BadZipFile:
        check_true(f"AC8 {s} is a zip", False)
        continue
    # the upload route wants the skill folder as the zip's root, not a subfolder
    check(f"AC8 {s}'s zip is rooted at its own folder",
          [n for n in names if not n.startswith(f"{s}/")], [])
    check_true(f"AC8 {s}/SKILL.md is in it, at the casing we actually serve",
               f"{s}/SKILL.md" in names, str(names))

# ---- AC9 the page says how to tell a stale copy ----------------------------------------------------------
_blk = page[max(0, page.find("agent-brief.zip") - 1200):page.find("agent-brief.zip") + 1200] if "agent-brief.zip" in page else ""
check_true("AC9 the page says, beside the download, how to tell a copy that has gone stale",
           re.search(r"product_version|stale|older", _blk) is not None, _blk[:0] or "no download block")
check_true("AC9 and the chapter says it too", re.search(r"product_version|stale|older", chapter) is not None)

# ---- AC10 the downloads sit behind the same gate as the screen ---------------------------------------------
pw = os.path.join(etc, "passwd")
try:
    W.write_password(pw, "a-long-enough-operator-password", iterations=1000)
except (AttributeError, OSError, TypeError) as ex:
    skip("AC10 the downloads are behind the session gate", f"cannot set a password here: {ex}")
else:
    srv2 = W.make_server(bind="127.0.0.1", port=0, socket_path=fb.path, etc_dir=etc,
                         config={"AUTH": "on"}, state_dir=tempfile.mkdtemp())
    p2 = srv2.server_address[1]
    threading.Thread(target=srv2.serve_forever, daemon=True).start()
    time.sleep(0.3)
    for path in ("/agent-brief.zip", "/skill/mesh-operate.zip"):
        s2, h2, _ = get(path, port=p2)
        check_true(f"AC10 {path} is not served to a stranger", s2 in (302, 303, 401, 403), str(s2))

# ---- AC6, the part that actually matters: does an installed box have the files ----------------------------
# A symlink that setuptools does not resolve would leave a box with five dangling links and a
# download of nothing. Built here rather than assumed, the way the help suite does for the lessons.
import glob as _glob, subprocess as _sub, zipfile as _zf  # noqa: E402
_wd = tempfile.mkdtemp()
_out = _sub.run([sys.executable, "-m", "pip", "wheel", ROOT, "--no-deps", "-w", _wd, "-q"],
                capture_output=True, text=True)
_wheels = sorted(_glob.glob(os.path.join(_wd, "mesh_manager-*.whl")), key=os.path.getmtime)
if not _wheels:
    skip("AC6 the brief ships in the wheel", f"no wheel built here: {(_out.stderr or '')[-160:]}")
else:
    _z = _zf.ZipFile(_wheels[-1])
    _n = _z.namelist()
    check("AC6 every skill ships in the wheel",
          [s for s in SKILLS if f"mesh_manager/brief/skills/{s}/SKILL.md" not in _n], [])
    check_true("AC6 and the role with it", "mesh_manager/brief/agents/mesh-manager-agent.md" in _n)
    # a link that was packed as a link rather than resolved would be a few bytes of path
    _bodies = [_z.read(n) for n in _n if "/brief/" in n]
    check_true("AC6 the wheel carries the content, not five dangling links",
               all(b.startswith(b"---") and len(b) > 500 for b in _bodies),
               str([len(b) for b in _bodies]))

# ---- AC11 the worked example ---------------------------------------------------------------------------------
_dial = [w for w in ("observe", "propose", "act") if w not in chapter]
check("AC11 the worked example shows the same request at all three levels", _dial, [])
check_true("AC11 and it is a worked example, not a list of definitions",
           re.search(r"[Ff]or example|Worked example|Say you ask|Ask it to", chapter) is not None)

finish()
