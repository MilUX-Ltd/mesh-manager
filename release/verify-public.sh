#!/usr/bin/env bash
# verify-public.sh [repo] - check the published product as the stranger who receives it.
#
# LESSONS 25: three faults reached the public cut because everything was checked as the author,
# with the author's access and the author's tree. This runs with no token, no clone of the
# private repository and nothing from this machine's checkout: it asks GitHub for the newest
# release the way an installed box does, downloads what a person would download, and follows
# the instructions the tree gives them. It is the last step of publishing, not a nicety.
#
# Exits non-zero on the first thing a stranger would find broken.
set -uo pipefail
REPO="${1:-MilUX-Ltd/mesh-manager}"
API="https://api.github.com"; RAW="https://raw.githubusercontent.com"
WORK=$(mktemp -d); trap 'rm -rf "$WORK"' EXIT
FAIL=0; N=0
ok()   { N=$((N+1)); printf '  ok    %s\n' "$1"; }
bad()  { N=$((N+1)); FAIL=$((FAIL+1)); printf '  FAIL  %s\n' "$1"; }
note() { printf '        %s\n' "$1"; }

# No credentials, whoever runs this. A token in the environment would hide exactly the fault
# this script exists to catch: a public thing that only works for someone signed in.
unset GITHUB_TOKEN GH_TOKEN GITHUB_USER GH_HOST 2>/dev/null || true
export GIT_TERMINAL_PROMPT=0 GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null
CURL=(curl -sSL --retry 3 --retry-delay 2 --max-time 300 -H "Accept: application/vnd.github+json")

echo "verify-public: $REPO, with no credentials at all"
echo

echo "the release, as an installed box asks for it"
if ! "${CURL[@]}" "$API/repos/$REPO/releases?per_page=20" -o "$WORK/rels.json"; then
    bad "the releases API is not reachable anonymously"; echo; echo "FAILED"; exit 1
fi
read -r TAG VER ASSETS < <(python3 - "$WORK/rels.json" <<'PY'
import json, sys
rels = json.load(open(sys.argv[1]))
if not isinstance(rels, list) or not rels:
    print("- - -"); raise SystemExit
r = rels[0]
print(r.get("tag_name", "-"), r.get("tag_name", "-").lstrip("v"),
      ",".join(a["name"] for a in r.get("assets", [])) or "-")
PY
)
if [[ "$TAG" == "-" ]]; then
    bad "no release is visible without a token"; echo; echo "FAILED"; exit 1
fi
ok "the newest release is visible anonymously: $TAG"
note "assets: ${ASSETS//,/  }"

TGZ="mesh-manager-$VER-amd64.tgz"
for a in "$TGZ" "$TGZ.sha256" install.sh; do
    case ",$ASSETS," in *",$a,"*) ok "the release carries $a" ;;
        *) bad "the release has no asset named $a, which the README and the notes tell people to download" ;;
    esac
done

echo
echo "downloading what a person downloads"
DL="https://github.com/$REPO/releases/download/$TAG"
for a in "$TGZ" "$TGZ.sha256" install.sh; do
    code=$(curl -sL -o "$WORK/$a" -w '%{http_code}' --max-time 300 "$DL/$a" || echo 000)
    [[ "$code" == 200 ]] && ok "$a  HTTP 200  $(wc -c < "$WORK/$a" | tr -d ' ') bytes" \
                         || bad "$a  HTTP $code"
done

if [[ -s "$WORK/$TGZ" && -s "$WORK/$TGZ.sha256" ]]; then
    want=$(awk '{print $1}' "$WORK/$TGZ.sha256")
    got=$( (sha256sum "$WORK/$TGZ" 2>/dev/null || shasum -a 256 "$WORK/$TGZ") | awk '{print $1}')
    [[ "$want" == "$got" ]] && ok "the published hash matches the published tarball" \
                            || { bad "hash mismatch: published $want, downloaded $got"; }
fi
[[ -s "$WORK/install.sh" ]] && { bash -n "$WORK/install.sh" 2>/dev/null \
    && ok "the installer parses" || bad "the installer does not parse"; }

echo
echo "the tree, cloned the way anyone clones it"
if git clone -q --depth 1 "https://github.com/$REPO.git" "$WORK/repo" 2>/dev/null; then
    ok "the repository clones anonymously"
else
    bad "the repository does not clone anonymously"
fi

if [[ -d "$WORK/repo" ]]; then
    # Is anything of the publisher's own showing? That check needs the publisher's list of
    # internal names, which is not something to publish, so it lives in a file beside this
    # script that the cut does not carry. Without it this step is skipped and said to be.
    mkdir -p "$WORK/un" && tar -xzf "$WORK/$TGZ" -C "$WORK/un" 2>/dev/null || true
    PRIV="$(cd "$(dirname "$0")" && pwd)/private-strings.txt"
    if [[ -r "$PRIV" ]]; then
        LEAK=$(head -1 "$PRIV")
        hits=$(grep -rIlnE "$LEAK" "$WORK/repo" --exclude-dir=.git 2>/dev/null | head -10 || true)
        [[ -z "$hits" ]] && ok "the published tree carries nothing of the publisher's" \
                         || { bad "the published tree carries the publisher's own detail"; echo "$hits" | sed "s|$WORK/repo/|          |"; }
        hits=$(grep -rIlnE "$LEAK" "$WORK/un" 2>/dev/null | grep -v '\.whl$' | head -10 || true)
        [[ -z "$hits" ]] && ok "the release tarball carries nothing of the publisher's" \
                         || { bad "the release tarball carries the publisher's own detail"; echo "$hits" | sed "s|$WORK/un/|          |"; }
    else
        note "skipped: the private-strings check needs the publisher's own list, which is not published"
    fi

    # Device identifiers, which anyone can check: everything in a published tree should come
    # from the demo block, because a real node id or radio MAC in it came from someone's fleet.
    stray=$( { grep -rIhoE '![0-9a-f]{8}' "$WORK/repo" --exclude-dir=.git 2>/dev/null
               grep -rIhoE '![0-9a-f]{8}' "$WORK/un" 2>/dev/null; } | sort -u \
             | grep -vE '^!(ee0000[0-9]{2}|1a2b3c4d|aa000001|bb000002|cc000003|dd000004|00000001|ffffffff|0000beef|deadbeef|z1)$' || true)
    [[ -z "$stray" ]] && ok "every device identifier is from the demo block" \
                      || { bad "a device identifier that is not from the demo block is published"; echo "$stray" | sed 's/^/          /'; }

    # The front page is the first thing a stranger reads. Every path it names must exist.
    missing=$(cd "$WORK/repo" && python3 - <<'PY'
import re, os
s = open("README.md", encoding="utf-8").read()
paths = set(re.findall(r'\]\(([^)#]+)\)', s)) | set(re.findall(r'src="([^"]+)"', s))
bad = [p for p in paths
       if not p.startswith(("http://", "https://", "../", "mailto:")) and not os.path.exists(p)]
print("\n".join(sorted(bad)))
PY
)
    [[ -z "$missing" ]] && ok "every path the README names exists in the tree" \
                        || { bad "the README names paths that are not there"; echo "$missing" | sed 's/^/          /'; }

    # A version in the tree that disagrees with the release it came from means a stale cut.
    tv=$(tr -d ' \n' < "$WORK/repo/VERSION" 2>/dev/null || echo "?")
    [[ "$tv" == "$VER" ]] && ok "the tree's VERSION ($tv) is the newest release" \
                          || bad "the tree says $tv but the newest release is $VER: the cut is behind"

    for f in LICENSE SECURITY.md NOTICE THIRD-PARTY.md tests/run.sh .github/workflows/tests.yml; do
        [[ -f "$WORK/repo/$f" ]] && ok "$f is present" || bad "$f is missing"
    done
    n=$(ls "$WORK/repo"/tests/test_*.py 2>/dev/null | wc -l | tr -d ' ')
    (( n >= 40 )) && ok "the suites travel with the product ($n of them)" || bad "only $n suites in the public tree"
    grep -qi "report a vulnerability\|security" "$WORK/repo/README.md" \
        && ok "the README points at the disclosure route" \
        || bad "the README does not tell a finder where to report a fault"
fi

echo
echo "how the repository is set up, as GitHub reports it publicly"
"${CURL[@]}" "$API/repos/$REPO" -o "$WORK/repo.json" 2>/dev/null || true
python3 - "$WORK/repo.json" <<'PY' || true
import json, sys
try: d = json.load(open(sys.argv[1]))
except Exception: raise SystemExit
print("        visibility: %s   licence: %s" % (d.get("visibility"), (d.get("license") or {}).get("spdx_id")))
print("        issues: %s   description: %s" % (d.get("has_issues"), "set" if d.get("description") else "MISSING"))
PY

echo
if (( FAIL )); then echo "FAILED: $FAIL of $N checks. A stranger would hit this."; exit 1; fi
echo "PASSED: $N checks, all of them as someone with no access to anything of ours."
