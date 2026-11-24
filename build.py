"""Rebuild index.html from src/page.html and the embedded data files, and pin the page's
inline script in vercel.json's Content-Security-Policy by its SHA-256 hash, and generate vercel.json's
Cache-Control rules (one per response; data/*.png rasters long-cached, see below).

python3 build.py          write index.html and the headers block of vercel.json
python3 build.py --check  write nothing; exit 1 if index.html or vercel.json is stale
                          or the CSP does not pin the inline script(s) in index.html"""
import base64, hashlib, json, re, sys
from pathlib import Path
root = Path(__file__).parent
src = root / "src"
CHECK = "--check" in sys.argv[1:]


def fail(msg):
    print("build.py: ERROR: " + msg, file=sys.stderr)
    sys.exit(1)


INLINE = re.compile(r"<script>(.*?)</script>", re.S)
# An external script is allowed only as <script [defer|async] src="/same/origin/path"></script> with an empty body:
# script-src 'self' already covers same-origin files (e.g. /_vercel/insights/script.js), so it needs no hash.
# src must start with exactly one "/" (no scheme, no "//" or "/\" host), so nothing off-site can slip in.
EXTERNAL = re.compile(r'<script((?:\s+(?:defer|async|src="/(?![/\\])[^"\\\s<>]*"))+)></script>')


def inline_scripts(html, where):
    """The inline scripts the CSP must pin. Refuses pages the regex would silently misread:
    every <script open tag must be a bare inline <script>...</script> or a same-origin external tag (see EXTERNAL)."""
    scripts = []
    for m in re.finditer(r"<script\b", html, re.I):
        i = m.start()
        inline, ext = INLINE.match(html, i), EXTERNAL.match(html, i)
        if inline and not re.search(r"</?script", inline.group(1), re.I):
            scripts.append(inline.group(1))
        elif ext and len(re.findall(r'\ssrc="', ext.group(1))) == 1:
            pass
        else:
            fail("%s: unsupported script tag %r; use a bare <script> tag with a lowercase </script> close (hashed into the CSP), "
                 "or an empty <script defer src=\"/same-origin/path.js\"></script>" % (where, html[i:i + 80]))
    if not scripts:
        fail(where + ": no inline <script>...</script> found; refusing to emit a script-src without a hash")
    return scripts


def sha(s):
    return "'sha256-%s'" % base64.b64encode(hashlib.sha256(s.encode("utf-8")).digest()).decode()


page = (src / "page.html").read_text()
for tag, name in [("/*GEO*/", "geo.js"), ("/*CREST*/", "crest.js"), ("/*DEM*/", "dem.js"), ("/*TOPO*/", "topo.js")]:
    page = page.replace(tag, (src / name).read_text())
scripts = inline_scripts(page, "src/page.html (built)")
if not CHECK:
    (root / "index.html").write_text(page)
    print("built index.html", len(page), "bytes")

hashes = " ".join(sha(s) for s in scripts)
csp = "; ".join([
    "default-src 'self'",
    "script-src 'self' " + hashes,
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
    "font-src 'self' https://fonts.gstatic.com",
    "img-src 'self' data: blob:",
    "connect-src 'self' https://photon.komoot.io https://nominatim.openstreetmap.org",
    "object-src 'none'",
    "base-uri 'none'",
    "form-action 'self'",
    "frame-ancestors 'none'",
    "upgrade-insecure-requests",
])
cfg_path = root / "vercel.json"
cfg = json.loads(cfg_path.read_text())
headers = [
    {"key": "Content-Security-Policy", "value": csp},
    {"key": "X-Content-Type-Options", "value": "nosniff"},
    {"key": "Referrer-Policy", "value": "strict-origin-when-cross-origin"},
    {"key": "X-Frame-Options", "value": "DENY"},
    {"key": "Cross-Origin-Opener-Policy", "value": "same-origin"},
    {"key": "Permissions-Policy", "value": "camera=(), microphone=(), geolocation=(), payment=(), usb=(), browsing-topics=()"},
]
# Cache-Control: every response gets it exactly once. Vercel applies EVERY matching headers rule, and does not document
# which value wins when two rules set the same key, so the three Cache-Control rules below have mutually exclusive
# sources and the security rule on /(.*) sets no Cache-Control at all.
#   - the data rasters on disk right now: 7 days, immutable. Safe because the page requests them as ?v=<bay.json built>,
#     so a data rebuild changes every raster URL. Listing only files that exist means a missing raster's 404 is never
#     cached for a week; a new raster added without rerunning build.py falls back to the 5-minute rule (and --check fails).
#   - favicon and apple-touch-icon: 1 day.
#   - everything else (HTML, JSON, og.jpg, 404s, redirects): 5 minutes, must-revalidate, so corrections propagate fast.
RASTERS = sorted(p.stem for p in (root / "data").glob("*.png"))
for r in RASTERS:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", r):
        fail("data/%s.png: raster names must be [A-Za-z0-9_-] to go into a vercel.json source pattern" % r)
if not RASTERS:
    fail("no data/*.png rasters found")
ras = "(?:%s)" % "|".join(RASTERS)
ICONS = "(?:favicon|apple-touch-icon)"
CC_SHORT, CC_RASTER, CC_ICON = "public, max-age=300, must-revalidate", "public, max-age=604800, immutable", "public, max-age=86400"
block = [
    {"source": "/(.*)", "headers": headers},
    {"source": "/((?!data/%s\\.png$|%s\\.png$).*)" % (ras, ICONS), "headers": [{"key": "Cache-Control", "value": CC_SHORT}]},
    {"source": "/data/(%s\\.png)" % ras, "headers": [{"key": "Cache-Control", "value": CC_RASTER}]},
    {"source": "/(%s\\.png)" % ICONS, "headers": [{"key": "Cache-Control", "value": CC_ICON}]},
]

if CHECK:
    problems = []
    committed = (root / "index.html").read_text()
    if committed != page:
        problems.append("index.html does not match a fresh build of src/ (edited by hand, or src/ changed without running build.py)")
    if cfg.get("headers") != block:
        problems.append("vercel.json headers do not match what build.py generates (stale CSP hash or edited headers)")
    pinned = set(re.findall(r"'sha256-[A-Za-z0-9+/=]+'", next((h["value"] for b in cfg.get("headers", []) for h in b.get("headers", [])
                                                                if h.get("key") == "Content-Security-Policy"), "")))
    live = inline_scripts(committed, "index.html")
    missing = [i for i, s in enumerate(live) if sha(s) not in pinned]
    if missing:
        problems.append("vercel.json CSP does not pin inline script(s) #%s of index.html; browsers would block them" % ", #".join(str(i + 1) for i in missing))
    if problems:
        fail("stale build, run python3 build.py and commit index.html and vercel.json together:\n  - " + "\n  - ".join(problems))
    print("build check OK: index.html and vercel.json match src/, CSP pins", len(live), "inline script(s)")
    sys.exit(0)

cfg["headers"] = block
cfg_path.write_text(json.dumps(cfg, indent=2) + "\n")
print("vercel.json CSP pins", len(scripts), "inline script(s)")
