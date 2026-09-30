#!/usr/bin/env python3
"""Spec 108: the words and the look (1.5.0).

Three things, all about what an operator sees rather than what the product does:

1. The words. The design README lists what never appears on an operator screen: peer, connection,
   online, offline, systemd, journalctl, installer options, spec or ADR numbers. A laptop is never the
   box (D2 keeps "the box" for the Linux mini computer). Checked over the visible text of every screen
   route, in the laptop, box and hub shapes, with the fixture set so the conditional paths that carry
   those words today actually render (LESSONS 48: a check that passes against an empty page is not a check).
2. The type. IBM Plex Sans, Sans Condensed and Mono, bundled as woff2 with the OFL text and served before
   sign-in, because the sign-in page uses them and the kit is often offline.
3. The phone. Under 700 px the rail becomes five bottom tabs: Home, Map, Nodes, Messages, More.

Visible text is the page with <script>, <style> and <svg> removed and the tags stripped, plus the text of
the attributes a person reads (data-tip, data-tip-more, title, placeholder, aria-label). Node names and
log lines come from the fixture, which carries none of the retired words.

Every block is guarded: a missing piece is one verdict, never a traceback hiding the rest.
"""
import glob, html as H, http.client, json, os, re, sys, tempfile, threading, time
sys.path.insert(0, os.path.dirname(__file__))
from _common import ROOT, check, check_true, finish, read  # noqa: E402
sys.path.insert(0, os.path.join(ROOT, "src"))
try:
    import fakebridge_lib as fb  # noqa: E402
    from mesh_manager import web as W  # noqa: E402
except Exception as ex:  # noqa: BLE001
    print(f"FAIL imports                                                         {type(ex).__name__}: {ex}")
    print("\nFAILURES: 1"); sys.exit(1)

ROUTES = ["/", "/map", "/map/full", "/nodes", "/node?id=!aa000001", "/graph", "/register", "/bench", "/channels",
          "/messages", "/connections", "/activity", "/health", "/log", "/packets", "/radio", "/settings", "/about", "/help"]

RETIRED = [("peer", r"\bpeers?\b", re.I), ("connection", r"\bconnections?\b", re.I),
           ("online", r"\bonline\b", re.I), ("offline", r"\boffline\b", re.I),
           ("systemd", r"\bsystemd\b", re.I), ("journalctl", r"\bjournalctl\b", re.I),
           ("installer or command-line option", r"(?<![\w-])--[a-z][a-z0-9]*(?:-[a-z0-9]+)*", 0),
           ("spec number", r"\bspec\s*\d{2,3}\b", re.I), ("ADR", r"\bADR\b", 0)]

# ---- the fixture, set so today's conditional words render --------------------------------------------
W.PRUNE_ON_START = False           # keep the staged release below, so About shows its roll back card


class _Json:
    """The fake's peers answer says the site is listening; this one says it is not, so the words for a
    site that cannot be joined render too. Only the peers answer is touched."""
    loads = staticmethod(json.loads)

    @staticmethod
    def dumps(o, *a, **k):
        if isinstance(o, dict) and "peers" in o and isinstance(o.get("site"), dict):
            o = dict(o, site=dict(o["site"], listening=False))
        return json.dumps(o, *a, **k)


fb.json = _Json
BASE_STATUS = dict(fb.STATUS)
SHAPES = [("laptop", {"MODE": "desktop"}, {"mode": "desktop", "site": None}),
          ("box", {"MODE": "tak-server", "ROUTE_HOST": "edge.example"},
           {"mode": "tak-server", "site": {"id": "ab" * 32, "name": "Edge kit"}, "peer_port": 8094, "peer_bind": "0.0.0.0"}),
          ("hub", {"MODE": "hub", "ROUTE_HOST": "dev.example"},
           {"mode": "hub", "site": {"id": "cd" * 32, "name": "Dev hub"}, "radio": None, "radio_present": False,
            "peer_port": 8094, "peer_bind": "0.0.0.0"})]


def serve(cfg, stage=False):
    b = fb.start_fake_bridge()
    sd = tempfile.mkdtemp()
    s = W.make_server(bind="127.0.0.1", port=0, socket_path=b.path, etc_dir=tempfile.mkdtemp(),
                      config=dict(cfg), state_dir=sd)
    if stage:                      # a release still on disk behind the running one: About offers a roll back
        arch = getattr(getattr(s, "web", None), "arch", "amd64")
        d = os.path.join(sd, "updates", "0.0.1"); os.makedirs(d)
        for f in (f"mesh-manager-0.0.1-{arch}.tgz", f"mesh-manager-0.0.1-{arch}.tgz.sha256", "install.sh"):
            open(os.path.join(d, f), "w").write("x")
    threading.Thread(target=s.serve_forever, daemon=True).start()
    time.sleep(0.3)
    return s.server_address[1]


def get(port, path, hops=2, raw=False):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    c.request("GET", path)
    r = c.getresponse(); body = r.read(); hd = {k.lower(): v for k, v in r.getheaders()}
    c.close()
    if r.status in (301, 302, 303, 307, 308) and hops and not raw and (hd.get("location") or "").startswith("/"):
        return get(port, hd["location"], hops - 1)
    return r.status, hd, (body if raw else body.decode("utf-8", "replace"))


def visible(page):
    body = re.sub(r"(?is)<script\b.*?</script>|<style\b.*?</style>", " ", page)
    attrs = " ".join(H.unescape(m.group(3)) for m in re.finditer(
        r"\s(data-tip|data-tip-more|title|placeholder|aria-label)=(['\"])(.*?)\2", body, re.S))
    t = re.sub(r"(?is)<script\b.*?</script>|<style\b.*?</style>|<svg\b.*?</svg>", " ", page)
    t = H.unescape(re.sub(r"(?s)<[^>]+>", " ", t))
    return re.sub(r"\s+", " ", t + " " + attrs)


def element(src, start):
    m = re.match(r"<([a-zA-Z0-9]+)", src[start:])
    if not m:
        return ""
    tag, depth = m.group(1).lower(), 0
    for t in re.finditer(r"<(/?)%s\b[^>]*>" % tag, src[start:], re.I):
        depth += -1 if t.group(1) else 1
        if depth == 0:
            return src[start:start + t.end()]
    return src[start:]


def summary(bad):
    return "; ".join(bad[:4]) + (f" ... ({len(bad)} pages)" if len(bad) > 4 else "")


# ---- render ------------------------------------------------------------------------------------------
TEXT = {}
for shape, cfg, st in SHAPES:
    fb.STATUS.clear(); fb.STATUS.update(BASE_STATUS); fb.STATUS.update(st)
    port = serve(dict(cfg, AUTH="off"), stage=True)
    TEXT[shape] = {r: get(port, r) for r in ROUTES}
    aport = serve(dict(cfg, AUTH="on"))
    TEXT[shape]["/login"] = get(aport, "/login")
fb.STATUS.clear(); fb.STATUS.update(BASE_STATUS)

# the fixture has to have done its job, or AC1 is checking an empty page
box_about = visible(TEXT["box"]["/about"][2])
check_true("fixture: the box's About shows a roll back to 0.0.1", "0.0.1" in box_about, "" if "0.0.1" in box_about else "the staged release did not render")
_not200 = [(s, r, v[0]) for s in TEXT for r, v in TEXT[s].items() if v[0] != 200]
check_true("fixture: every route answered 200 in every shape", not _not200, str(_not200[:4]) if _not200 else "")

# ---- AC1 no retired word in what an operator reads ---------------------------------------------------
for shape, _, _ in SHAPES:
    for label, pat, flags in RETIRED:
        bad = []
        for r, (code, _, page) in TEXT[shape].items():
            hits = re.findall(pat, visible(page), flags)
            if hits:
                bad.append(f"{r} x{len(hits)}")
        check_true(f"AC1 {shape}: no {label} on any screen", not bad, summary(bad))

# ---- AC2 a laptop is never the box; the box still is ------------------------------------------------
bad = []
for r, (code, _, page) in TEXT["laptop"].items():
    hits = re.findall(r"\b(?:the|this)\s+box(?:'s)?\b", visible(page), re.I)
    if hits:
        bad.append(f"{r} x{len(hits)}")
check_true("AC2 laptop: never called the box, on any screen", not bad, summary(bad))
check_true("AC2 guard (D2): a box is still called the box",
           any(re.search(r"\b(?:the|this)\s+box\b", visible(p), re.I) for _, _, p in TEXT["box"].values()))

# ---- AC3 the three families bundled, with the licence, and named in the CSS --------------------------
FONTS = os.path.join(ROOT, "src", "mesh_manager", "static", "fonts")
woff = sorted(glob.glob(os.path.join(FONTS, "*.woff2")))
FAMILIES = [("IBM Plex Sans", r"^IBMPlexSans-"), ("IBM Plex Sans Condensed", r"^IBMPlexSansCondensed-"),
            ("IBM Plex Mono", r"^IBMPlexMono-")]
for fam, pat in FAMILIES:
    files = [f for f in woff if re.match(pat, os.path.basename(f))]
    real = [f for f in files if open(f, "rb").read(4) == b"wOF2"]
    check_true(f"AC3 {fam} is bundled as woff2", bool(files) and real == files,
               f"{len(files)} files, {len(real)} with the woff2 signature, in static/fonts/")
lic = [f for f in glob.glob(os.path.join(FONTS, "*.txt"))
       if re.search(r"SIL OPEN FONT LICEN[CS]E\s+Version 1\.1", open(f, encoding="utf-8", errors="replace").read(), re.I)]
check_true("AC3 the OFL 1.1 text sits beside them", bool(lic), "no .txt in static/fonts/ holding the SIL OFL 1.1")
css = W.CSS
faces = re.findall(r"@font-face\s*\{([^}]*)\}", css)
for fam, _ in FAMILIES:
    mine = [f for f in faces if re.search(r"font-family:\s*['\"]%s['\"]" % re.escape(fam), f)]
    urls = [u for f in mine for u in re.findall(r"url\(['\"]?(/static/fonts/[^'\")]+\.woff2)['\"]?\)", f)]
    ok = bool(mine) and bool(urls) and all(os.path.isfile(os.path.join(FONTS, os.path.basename(u))) for u in urls)
    check_true(f"AC3 @font-face for {fam}, from /static/fonts, files present", ok, f"{len(mine)} faces, urls {urls[:2]}")
body = re.search(r"body\{[^}]*font:[^;}]*", css)
check_true("AC3 the body type is IBM Plex Sans first", bool(body) and re.search(r"font:[^;]*?\d+px(?:/[\d.]+)?\s+['\"]?IBM Plex Sans['\"]?[,;]", body.group(0) + ";") is not None,
           body.group(0)[-80:] if body else "no body font rule")
check_true("AC3 --mono is IBM Plex Mono first", re.search(r"--mono:\s*['\"]IBM Plex Mono['\"]", css) is not None)
check_true("AC3 Manrope and Roboto Mono are gone from the CSS", "Manrope" not in css and "Roboto Mono" not in css)
check_true("AC3 guard: nothing is fetched from Google Fonts", all("fonts.googleapis" not in p and "fonts.gstatic" not in p
                                                                   for s in TEXT for _, _, p in TEXT[s].values()))

# ---- AC4 served before sign-in, as font/woff2 -------------------------------------------------------
aport = serve({"AUTH": "on", "MODE": "tak-server"})
if not woff:
    check_true("AC4 each font answers before sign-in as font/woff2", False, "no woff2 files to fetch")
for f in woff:
    code, hd, data = get(aport, "/static/fonts/" + os.path.basename(f), raw=True)
    check("AC4 " + os.path.basename(f)[:40] + " before sign-in",
          (code, hd.get("content-type", "").split(";")[0], data[:4], "max-age" in hd.get("cache-control", "")),
          (200, "font/woff2", b"wOF2", True))
for f in lic[:1]:
    code, _, data = get(aport, "/static/fonts/" + os.path.basename(f), raw=True)
    check_true("AC4 the licence answers before sign-in too", code == 200 and b"SIL" in data, str(code))
code, _, _ = get(aport, "/static/leaflet/leaflet.js", raw=True)
check_true("AC4 guard: only the fonts and icons were opened, Leaflet still needs sign-in", code != 200, str(code))
code, _, _ = get(aport, "/static/fonts/../leaflet/leaflet.js", raw=True)
check_true("AC4 guard: and a way round through fonts/.. is refused", code != 200, str(code))

# ---- AC5 packaged and attributed ---------------------------------------------------------------------
try:
    import tomllib
    pd = (tomllib.loads(read("pyproject.toml") or "").get("tool", {}).get("setuptools", {})
          .get("package-data", {}).get("mesh_manager", []))
except Exception as ex:  # noqa: BLE001
    pd = []
    print(f"     pyproject did not parse: {type(ex).__name__}: {ex}")
check_true("AC5 pyproject package-data names the woff2 files", any(re.fullmatch(r"static/fonts/\*(\.woff2)?", p) for p in pd), str(pd))
check_true("AC5 and the licence beside them", any(re.fullmatch(r"static/fonts/(\*|\*\.txt|[\w.-]+\.txt)", p) for p in pd), str(pd))
notice = read("NOTICE") or ""
check_true("AC5 NOTICE names IBM Plex under the SIL OFL 1.1",
           "IBM Plex" in notice and re.search(r"Open Font Licen[cs]e,?\s*(Version\s*)?1\.1", notice) is not None)
check_true("AC5 NOTICE no longer claims Manrope or Roboto Mono", "Manrope" not in notice and "Roboto Mono" not in notice)

# ---- AC6 on a phone: five bottom tabs, More holding the rest -----------------------------------------
LEAD = ["Home", "Map", "Nodes", "Messages", "More"]
REST = ["Devices and channels", "Connect", "Health", "This computer", "Help"]


def phone(page):
    m = re.search(r"<nav\b[^>]*aria-label=['\"]Phone menu['\"][^>]*>", page)
    return element(page, m.start()) if m else None


def labels(frag):
    out = []
    for m in re.finditer(r"<(a|summary|button)\b[^>]*>(.*?)</\1>", frag, re.S | re.I):
        t = re.sub(r"\s+", " ", H.unescape(re.sub(r"(?s)<[^>]+>", " ", re.sub(r"(?is)<svg\b.*?</svg>", " ", m.group(2))))).strip()
        out.append(re.sub(r"\s*\d+$", "", t))
    return out


for shape, _, _ in SHAPES:
    bad = []
    for r, (code, _, page) in TEXT[shape].items():
        if r in ("/map/full", "/login"):
            continue
        ph = phone(page)
        if ph is None:
            bad.append(f"{r}: no Phone menu"); continue
        ls = labels(ph)
        pos = [ls.index(x) if x in ls else -1 for x in LEAD]
        if -1 in pos or pos != sorted(pos):
            bad.append(f"{r}: tabs {ls[:6]}"); continue
        after = ls[pos[-1] + 1:]
        if any(x not in after for x in REST) or any(x in ls[:pos[-1]] for x in REST):
            bad.append(f"{r}: More holds {after}")
    check_true(f"AC6 {shape}: Home, Map, Nodes, Messages, More; More holds the other four and Help", not bad, summary(bad))

page = TEXT["box"]["/"][2]
ph = phone(page)
rail_m = re.search(r"<nav\b[^>]*aria-label=['\"]Menu['\"][^>]*>", page)


def hooks(tag):
    cls = re.search(r"class=['\"]([^'\"]*)['\"]", tag or "")
    lab = re.search(r"aria-label=['\"]([^'\"]*)['\"]", tag or "")
    return ["." + c for c in (cls.group(1).split() if cls else [])] + ([f"aria-label={lab.group(1)}", f'aria-label="{lab.group(1)}"', f"aria-label='{lab.group(1)}'"] if lab else [])


def block_at(src, i):
    """The body of the brace block opening at or after i."""
    j = src.find("{", i); d = 0
    for k in range(j, len(src)):
        d += {"{": 1, "}": -1}.get(src[k], 0)
        if d == 0:
            return src[j:k + 1]
    return src[j:]


narrow = "".join(block_at(css, m.start()) for m in re.finditer(r"@media\s*\(max-width:\s*700px\)", css))
wide = css
for m in re.finditer(r"@media\s*\(max-width:\s*700px\)", css):
    prelude = css[m.start():css.find("{", m.start())]
    wide = wide.replace(prelude + block_at(css, m.start()), "")
ph_hooks = hooks(re.match(r"<nav\b[^>]*>", ph).group(0)) if ph else []
rail_hooks = hooks(rail_m.group(0)) if rail_m else []
check_true("AC6 under 700 px the CSS styles the phone tabs", bool(ph_hooks) and any(h in narrow for h in ph_hooks),
           f"hooks {ph_hooks}" if ph else "no Phone menu")
check_true("AC6 and hides the rail there", bool(rail_hooks) and any(
    re.search(re.escape(h) + r"[^{]*\{[^}]*display:\s*none", narrow) for h in rail_hooks), f"hooks {rail_hooks}" if rail_m else "no rail")
check_true("AC6 above 700 px the phone tabs are hidden", bool(ph_hooks) and any(
    re.search(re.escape(h) + r"[^{,]*\{[^}]*display:\s*none", wide) for h in ph_hooks), f"hooks {ph_hooks}" if ph else "no Phone menu")

finish()
