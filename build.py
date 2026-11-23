"""Rebuild index.html from src/page.html and the embedded data files."""
from pathlib import Path
src = Path(__file__).parent / "src"
page = (src / "page.html").read_text()
for tag, name in [("/*GEO*/", "geo.js"), ("/*CREST*/", "crest.js"), ("/*DEM*/", "dem.js"), ("/*TOPO*/", "topo.js")]:
    page = page.replace(tag, (src / name).read_text())
(Path(__file__).parent / "index.html").write_text(page)
print("built index.html", len(page), "bytes")
