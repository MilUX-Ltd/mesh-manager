#!/usr/bin/env python3
"""Spec 088: one action to connect, and no secret in the artefact.

The Connections page mints a token and prints a terminal command carrying the endpoint and nothing
else. A plugin carries the MCP server, the role and the skills together, and needs no secret in it:
userConfig holds the token in the client's secure storage and substitutes it into the header.

AC7 runs the real `claude` CLI rather than asserting a shape. The last two cards were both bitten by
a reasonable assumption about someone else's product, and probing this one turned up a required
field the documentation does not mention.
"""
import json, os, re, shutil, subprocess, sys, tempfile
sys.path.insert(0, os.path.dirname(__file__))
from _common import ROOT, check, check_true, finish, read, skip  # noqa: E402

PLUGIN = os.path.join(ROOT, "plugin")
SKILLS = sorted(d for d in os.listdir(os.path.join(ROOT, "skills"))
                if os.path.isdir(os.path.join(ROOT, "skills", d)))
VERSION = (read("VERSION") or "").strip()


def js(path):
    try:
        return json.loads(read(path) or "")
    except (ValueError, TypeError):
        return None


# ---- AC1 the plugin exists and is named -----------------------------------------------------------
man = js("plugin/.claude-plugin/plugin.json")
check_true("AC1 the plugin has a manifest", man is not None)
man = man or {}
check("AC1 it is named mesh-manager", man.get("name"), "mesh-manager")
check_true("AC1 it has a description", len(str(man.get("description") or "")) > 20)

# ---- AC11 the version cannot drift ------------------------------------------------------------------
check("AC11 the plugin's version is the product's", man.get("version"), VERSION)

# ---- AC2 it carries the role and every skill, as the repository's own bytes ---------------------------
role_in = os.path.join(PLUGIN, "agents", "mesh-manager-agent.md")
check_true("AC2 the role travels in the plugin", os.path.exists(role_in))
_missing = [s for s in SKILLS if not os.path.exists(os.path.join(PLUGIN, "skills", s, "SKILL.md"))]
check("AC2 and every skill with it", _missing, [])
_drift = []
for src, dst in [("agents/mesh-manager-agent.md", role_in)] + \
                [(f"skills/{s}/SKILL.md", os.path.join(PLUGIN, "skills", s, "SKILL.md")) for s in SKILLS]:
    a = read(src)
    b = read(os.path.relpath(dst, ROOT)) if os.path.exists(dst) else None
    if a != b:
        _drift.append(src)
check("AC2 the plugin's copy is the repository's, not a fork of it", _drift, [])

# ---- AC3 the box declared as a remote server ----------------------------------------------------------
mcp = js("plugin/.mcp.json") or {}
srv = (mcp.get("mcpServers") or {})
check_true("AC3 the plugin declares one MCP server", len(srv) == 1, str(sorted(srv)))
one = next(iter(srv.values()), {}) if srv else {}
check("AC3 it is an http server, not a local command", one.get("type"), "http")
check_true("AC3 there is no command to run on the operator's machine", "command" not in one)
check_true("AC3 the URL comes from the operator's own configuration",
           "user_config" in str(one.get("url") or ""), str(one.get("url")))
_auth = str(((one.get("headers") or {}).get("Authorization")) or "")
check_true("AC3 the token comes from the operator's own configuration",
           "user_config" in _auth and _auth.startswith("Bearer "), _auth)

# ---- AC4 userConfig, including the field the CLI requires and the docs omit -----------------------------
uc = man.get("userConfig") or {}
check_true("AC4 the plugin asks for the box address", any("url" in k or "address" in k or "box" in k for k in uc), str(sorted(uc)))
_tok = next((k for k in uc if "token" in k), None)
check_true("AC4 and for the token", _tok is not None, str(sorted(uc)))
if _tok:
    check("AC4 the token is marked sensitive, so the client holds it in secure storage",
          uc[_tok].get("sensitive"), True)
    check("AC4 and it is required", uc[_tok].get("required"), True)
_nodesc = sorted(k for k, v in uc.items() if not str((v or {}).get("description") or "").strip())
check("AC4 every field has a description, which the CLI requires and the docs do not mention", _nodesc, [])
_named = {k for k in uc}
_used = set(re.findall(r"\$\{user_config\.([a-z0-9_]+)\}", json.dumps(mcp)))
check("AC4 every value the server needs is a field the operator is asked for", sorted(_used - _named), [])

# ---- AC5 no secret anywhere in the plugin ----------------------------------------------------------------
_files, _bad = [], []
for base, _dirs, names in os.walk(PLUGIN):
    for n in names:
        p = os.path.join(base, n)
        _files.append(p)
        try:
            t = open(p, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        # a minted token is a long opaque run of base64-ish characters; a template is not
        for m in re.findall(r"[A-Za-z0-9_\-]{28,}", t):
            if "user_config" in m or m in ("mesh-manager", "mesh_manager"):
                continue
            _bad.append(f"{os.path.relpath(p, ROOT)}: {m[:16]}...")
check_true("AC5 there are files to check", len(_files) > 4, str(len(_files)))
check("AC5 nothing in the plugin looks like a minted token", _bad[:4], [])
_bearer = [os.path.relpath(p, ROOT) for p in _files
           if "Bearer " in (open(p, encoding="utf-8", errors="replace").read() if os.path.isfile(p) else "")
           and "user_config" not in (open(p, encoding="utf-8", errors="replace").read() if os.path.isfile(p) else "")]
check("AC5 Bearer appears only as a template", _bearer, [])

# ---- AC6 the marketplace ------------------------------------------------------------------------------------
mk = js(".claude-plugin/marketplace.json")
check_true("AC6 the repository is a marketplace", mk is not None)
mk = mk or {}
check_true("AC6 the marketplace is named", bool(mk.get("name")))
check_true("AC6 and has an owner", bool(mk.get("owner")))
_entry = next((p for p in (mk.get("plugins") or []) if p.get("name") == "mesh-manager"), None)
check_true("AC6 it lists the plugin", _entry is not None, str(mk.get("plugins")))
if _entry:
    check_true("AC6 and points at it", "plugin" in str(_entry.get("source") or ""), str(_entry.get("source")))

# ---- AC8 the public cut carries both, named by path ----------------------------------------------------------
cut = read("release/cut-public.sh")
if cut is None:
    skip("AC8 the public cut carries the plugin", "release tooling is not in this tree")
else:
    # named by path on purpose: the last card's check looked for a word, passed throughout, and the
    # files still did not reach the public repository.
    check_true("AC8 the cut copies the plugin by name", "plugin/" in cut or '"plugin"' in cut)
    check_true("AC8 and the marketplace by name", ".claude-plugin/marketplace.json" in cut)

# ---- AC9 the Connections page offers it ------------------------------------------------------------------------
web = read("src/mesh_manager/web.py") or ""
check_true("AC9 the page names the marketplace command",
           "plugin marketplace add" in web, "no marketplace command on the page")
check_true("AC9 and says the token is pasted in, not carried",
           re.search(r"paste[^<]{0,80}token|token[^<]{0,80}paste", web, re.I) is not None)

# ---- AC10 the guide -----------------------------------------------------------------------------------------------
guide = read("docs/GUIDE.md") or ""
bodies = dict(zip(re.findall(r"^## (.+)$", guide, re.M),
                  re.split(r"^## .+$", guide, flags=re.M)[1:]))
chap = bodies.get("Working with an agent", "")
check_true("AC10 the chapter covers the plugin", "marketplace" in chap.lower(), "no plugin route in the chapter")
check_true("AC10 and says the plugin's copy can go stale while the box's cannot",
           re.search(r"stale", chap) is not None and re.search(r"plugin", chap) is not None)

# ---- AC7 the real CLI, not a shape I believe in ------------------------------------------------------------------
claude = shutil.which("claude")
if not claude:
    skip("AC7 the real CLI accepts the plugin", "no claude CLI on this machine; a skip is not a pass")
else:
    work = tempfile.mkdtemp()
    added = installed = False
    try:
        src = os.path.join(work, "repo")
        # symlinks=True on purpose: the repository commits these as links (mode 120000) and a clone
        # restores them as links, so dereferencing here would prove the CLI works with a shape we do
        # not publish. The plugin's links point outside the plugin directory, which is the risk.
        shutil.copytree(ROOT, src, symlinks=True,
                        ignore=shutil.ignore_patterns(".git", ".venv", "release", "assets", "__pycache__"))
        subprocess.run(["git", "init", "-q", "."], cwd=src, check=False, timeout=60)
        subprocess.run(["git", "add", "-A"], cwd=src, check=False, timeout=120)
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "suite"],
                       cwd=src, check=False, timeout=120)
        _mk = os.path.join(src, ".claude-plugin", "marketplace.json")
        # a missing marketplace is one verdict, not a traceback that hides every check after it
        name = (js(".claude-plugin/marketplace.json") or {}).get("name") if os.path.exists(_mk) else None
        if not name:
            raise RuntimeError("no marketplace to offer the CLI")
        a = subprocess.run([claude, "plugin", "marketplace", "add", src, "--scope", "local"],
                           cwd=work, capture_output=True, text=True, timeout=300)
        added = a.returncode == 0
        check_true("AC7 the real CLI accepts the marketplace", added, (a.stdout + a.stderr)[-300:])
        if added:
            i = subprocess.run([claude, "plugin", "install", f"mesh-manager@{name}", "--scope", "local"],
                               cwd=work, capture_output=True, text=True, timeout=300)
            installed = i.returncode == 0
            check_true("AC7 and installs the plugin", installed, (i.stdout + i.stderr)[-400:])
            if installed:
                cache = os.path.expanduser("~/.claude/plugins")
                hits = []
                for base, _d, names in os.walk(cache):
                    if "mesh-manager-agent.md" in names and "plugin" not in base.split(os.sep)[-1:]:
                        hits.append(os.path.join(base, "mesh-manager-agent.md"))
                # the installed copy must carry readable content, not a link that resolved to nothing
                good = [h for h in hits if os.path.getsize(h) > 500]
                _sizes = sorted(os.path.getsize(h) for h in hits)
                check_true("AC7 the installed copy carries the role as real content, not a dangling link",
                           bool(good),
                           f"{len(hits)} found, sizes {_sizes}" if hits else "role not found in the installed plugin")
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as ex:
        check_true("AC7 the real CLI accepts the plugin", False, f"{type(ex).__name__}: {ex}")
    finally:
        if installed:
            subprocess.run([claude, "plugin", "uninstall", "mesh-manager", "--scope", "local"],
                           cwd=work, capture_output=True, text=True, timeout=120)
        if added:
            try:
                nm = json.loads(read(".claude-plugin/marketplace.json") or "{}").get("name")
                if nm:
                    subprocess.run([claude, "plugin", "marketplace", "remove", nm],
                                   cwd=work, capture_output=True, text=True, timeout=120)
            except ValueError:
                pass
        shutil.rmtree(work, ignore_errors=True)

finish()
