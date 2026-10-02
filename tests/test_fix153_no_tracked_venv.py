#!/usr/bin/env python3
"""1.5.3: a fresh clone carries no link to another computer's virtual environment, and runs its suites without one.

The repository tracked `.venv` as a symlink to a path on one Mac. `.gitignore` listed `.venv/`, and a pattern with a
trailing slash matches a directory only, so a symlink of that name was never ignored and was committed. Every other
clone then held a dangling link, and a worktree removed with its owner's checkout took the real environment with it
(LESSONS 32).

  AC1  the repository tracks no `.venv`.
  AC2  `.gitignore` ignores a `.venv` that is a symlink, as well as one that is a directory.
  AC3  tests/run.sh, run on a tree whose `.venv` is a dangling link and with no PYTHON set, falls through to the
       python3 on the path and runs its suites.

Every block is guarded: a tool that is not here is one verdict said out loud, not a traceback hiding the rest.
"""
import os, shutil, subprocess, sys, tempfile
sys.path.insert(0, os.path.dirname(__file__))
from _common import ROOT, check, check_true, finish, skip  # noqa: E402

GIT = shutil.which("git")


def git(*a, cwd=ROOT):
    return subprocess.run([GIT, *a], cwd=cwd, capture_output=True, text=True)


# ---- AC1 nothing named .venv is tracked ------------------------------------------------------------------
inside = bool(GIT) and git("rev-parse", "--is-inside-work-tree").stdout.strip() == "true"
if inside:
    tracked = [p for p in git("ls-files").stdout.splitlines() if p == ".venv" or p.startswith(".venv/")]
    check("AC1 the repository tracks no .venv", tracked, [])
else:
    skip("AC1 the repository tracks no .venv", "not a git work tree here (the public tree has no .git)")

# ---- AC2 the ignore rule covers a symlink ---------------------------------------------------------------
gi = os.path.join(ROOT, ".gitignore")
if GIT and os.path.exists(gi):
    d = tempfile.mkdtemp()
    git("init", "-q", cwd=d)
    shutil.copy(gi, os.path.join(d, ".gitignore"))
    os.symlink("/nonexistent/other-computers-venv", os.path.join(d, ".venv"))
    ign = git("check-ignore", "-q", ".venv", cwd=d)
    check("AC2 a .venv that is a symlink is ignored (git check-ignore exits 0)", ign.returncode, 0)
    shutil.rmtree(os.path.join(d, ".venv")) if os.path.isdir(os.path.join(d, ".venv")) and not os.path.islink(os.path.join(d, ".venv")) else os.remove(os.path.join(d, ".venv"))
    os.makedirs(os.path.join(d, ".venv", "bin"))
    open(os.path.join(d, ".venv", "bin", "python3"), "w").close()
    check("AC2 and a .venv that is a directory still is", git("check-ignore", "-q", ".venv", cwd=d).returncode, 0)
    # a comment on the same line as a pattern is part of the pattern (LESSONS 23): the rule has none
    rule = [ln for ln in open(gi, encoding="utf-8").read().splitlines() if ln.strip().startswith(".venv")]
    check_true("AC2 the .venv rule carries no trailing comment (LESSONS 23)", all("#" not in ln for ln in rule), str(rule))
else:
    skip("AC2 .gitignore ignores a .venv symlink", "git or .gitignore is not here")

# ---- AC3 the suite runner does not need the link ---------------------------------------------------------
run_sh = os.path.join(ROOT, "tests", "run.sh")
if os.path.exists(run_sh):
    t = tempfile.mkdtemp()
    os.makedirs(os.path.join(t, "tests"))
    shutil.copy(run_sh, os.path.join(t, "tests", "run.sh"))
    with open(os.path.join(t, "tests", "test_probe.py"), "w") as fh:
        fh.write("print('probe suite ran')\n")
    os.symlink("/nonexistent/other-computers-venv", os.path.join(t, ".venv"))   # dangling, as in a clone made on another Mac
    env = {k: v for k, v in os.environ.items() if k != "PYTHON"}
    env["PATH"] = os.path.dirname(sys.executable) + os.pathsep + env.get("PATH", "")
    r = subprocess.run(["bash", os.path.join(t, "tests", "run.sh")], cwd=t, capture_output=True, text=True, env=env, timeout=120)
    check("AC3 run.sh with a dangling .venv and no PYTHON exits 0", r.returncode, 0)
    check_true("AC3 and ran the suite it was given", "probe suite ran" in r.stdout and "suites: 1  failing: 0" in r.stdout, r.stdout.strip().splitlines()[-1] if r.stdout.strip() else r.stderr[:80])
else:
    skip("AC3 run.sh falls through to python3", "tests/run.sh is not here")

finish()
