"""Rebuild index.html from src/page.html and the embedded data files, and pin the page's
inline script in vercel.json's Content-Security-Policy by its SHA-256 hash."""
import base64, hashlib, json, re
from pathlib import Path
root = Path(__file__).parent
src = root / "src"
page = (src / "page.html").read_text()
for tag, name in [("/*GEO*/", "geo.js"), ("/*CREST*/", "crest.js"), ("/*DEM*/", "dem.js"), ("/*TOPO*/", "topo.js")]:
    page = page.replace(tag, (src / name).read_text())
(root / "index.html").write_text(page)
print("built index.html", len(page), "bytes")

scripts = re.findall(r"<script>(.*?)</script>", page, re.S)
hashes = " ".join("'sha256-%s'" % base64.b64encode(hashlib.sha256(s.encode("utf-8")).digest()).decode() for s in scripts)
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
cfg["headers"] = [{"source": "/(.*)", "headers": headers}]
cfg_path.write_text(json.dumps(cfg, indent=2) + "\n")
print("vercel.json CSP pins", len(scripts), "inline script(s)")
