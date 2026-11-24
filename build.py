"""Rebuild index.html from src/page.html and the embedded data files, and pin the page's
inline script in vercel.json's Content-Security-Policy by its SHA-256 hash.

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


def inline_scripts(html, where):
    """The inline scripts the CSP must pin. Refuses pages the regex would silently misread."""
    scripts = re.findall(r"<script>(.*?)</script>", html, re.S)
    opens = len(re.findall(r"<script\b", html, re.I))
    if not scripts:
        fail(where + ": no inline <script>...</script> found; refusing to emit a script-src without a hash")
    if opens != len(scripts):
        fail("%s: %d <script open tag(s) but only %d plain <script>...</script> block(s) captured; "
             "use a bare <script> tag and a lowercase </script> close so the hash covers every script" % (where, opens, len(scripts)))
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
    {"key": "Cache-Control", "value": "public, max-age=300, must-revalidate"},
    {"key": "Content-Security-Policy", "value": csp},
    {"key": "X-Content-Type-Options", "value": "nosniff"},
    {"key": "Referrer-Policy", "value": "strict-origin-when-cross-origin"},
    {"key": "X-Frame-Options", "value": "DENY"},
    {"key": "Cross-Origin-Opener-Policy", "value": "same-origin"},
    {"key": "Permissions-Policy", "value": "camera=(), microphone=(), geolocation=(), payment=(), usb=(), browsing-topics=()"},
]
block = [{"source": "/(.*)", "headers": headers}]

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
