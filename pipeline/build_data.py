"""Build Bay Area map and elevation data for sfofloodweek.com.

Runs in GitHub Actions (internet access). Writes everything to data/:
  bay.json            geometry (page coordinates), raster placement, stats, stations
  tides.json          NOAA high/low predictions and datums per station
  topo.png            ground height classes, about 80 m cells
  low_<zone>.png      low-ground classes, about 30 m cells, one per tide zone

Page coordinates: x = (lon - LON0) * 1e4 * cos(37.61 deg), y = (LAT1 - lat) * 1e4.
The hand-built peninsula layers in the page use the same scale, shifted by
(300 * cos(37.61 deg), 1440), and cover OLD (lon/lat box) below.
"""
import io, json, math, os, sys, time, traceback, zipfile

import numpy as np
import requests
import shapefile
from PIL import Image, ImageDraw
from shapely import make_valid
from shapely.geometry import LineString, MultiLineString, MultiPolygon, Point, Polygon, box, shape
from shapely.ops import linemerge, transform, unary_union

OUT = "data"
os.makedirs(OUT, exist_ok=True)
T0 = time.time()
UA = {"User-Agent": "sfofloodweek.com data build (github.com/mrubash1/sfofloodweek)"}

LON0, LON1, LAT1 = -122.56, -121.90, 37.96
CELL = 0.0003                      # low-ground cell, degrees
NX = round((LON1 - LON0) / CELL)   # 2200
NY = 1867
LAT0 = LAT1 - NY * CELL            # 37.3999
CO = math.cos(math.radians(37.61))
S = 1e4
W, H = (LON1 - LON0) * S * CO, (LAT1 - LAT0) * S
OLD = (-122.53, -122.30403, 37.564, 37.816)   # lon0, lon1, lat0, lat1

STATIONS = {  # id: short name
    "9414290": "sf", "9414392": "op", "9414750": "ala", "9414863": "rich",
    "9414523": "rwc", "9414575": "coy",
}
BAY_POINT = (-122.30, 37.70)
OUTPUT = {"W": W, "H": H, "LON0": LON0, "LAT1": LAT1, "LAT0": LAT0, "LON1": LON1, "CO": CO,
          "OLD": OLD, "errors": []}


def log(*a):
    print(f"[{time.time() - T0:6.1f}s]", *a, flush=True)


def get(url, tries=4, timeout=180, **kw):
    for i in range(tries):
        try:
            r = requests.get(url, headers=UA, timeout=timeout, **kw)
            r.raise_for_status()
            return r
        except Exception as e:
            log("retry", i + 1, url[:120], e)
            time.sleep(4 * (i + 1))
    raise RuntimeError("failed " + url)


def step(name):
    def wrap(fn):
        def run(*a, **k):
            log("==", name)
            try:
                return fn(*a, **k)
            except Exception as e:
                OUTPUT["errors"].append(f"{name}: {e}")
                traceback.print_exc()
                return None
        return run
    return wrap


def pxy(lon, lat):
    return ((lon - LON0) * S * CO, (LAT1 - lat) * S)


def to_px(g):
    return transform(lambda x, y, z=None: ((np.asarray(x) - LON0) * S * CO, (LAT1 - np.asarray(y)) * S), g)


def path_of(g, closed=True):
    """SVG path with absolute first point and relative integer moves."""
    parts = []
    def ring(coords, close):
        pts = [(round(x), round(y)) for x, y in coords]
        dedup = [pts[0]]
        for p in pts[1:]:
            if p != dedup[-1]:
                dedup.append(p)
        if len(dedup) < 2:
            return
        s = f"M{dedup[0][0]} {dedup[0][1]}"
        px0, py0 = dedup[0]
        rel = []
        for x, y in dedup[1:]:
            rel.append(f"{x - px0} {y - py0}")
            px0, py0 = x, y
        s += "l" + " ".join(rel)
        parts.append(s + ("z" if close else ""))
    geoms = getattr(g, "geoms", [g])
    for gg in geoms:
        if gg.is_empty:
            continue
        if gg.geom_type == "Polygon":
            ring(gg.exterior.coords, True)
            for i in gg.interiors:
                ring(i.coords, True)
        elif gg.geom_type == "LineString":
            ring(gg.coords, False)
        elif hasattr(gg, "geoms"):
            parts.append(path_of(gg, closed))
    return "".join(parts)


def polys_only(g):
    ps = [x for x in getattr(g, "geoms", [g]) if x.geom_type in ("Polygon", "MultiPolygon")]
    out = []
    for x in ps:
        out += list(getattr(x, "geoms", [x]))
    return MultiPolygon([x for x in out if not x.is_empty])


def shp_from_zip(content):
    z = zipfile.ZipFile(io.BytesIO(content))
    names = z.namelist()
    base = [n[:-4] for n in names if n.endswith(".shp")][0]
    r = shapefile.Reader(shp=io.BytesIO(z.read(base + ".shp")), dbf=io.BytesIO(z.read(base + ".dbf")),
                         shx=io.BytesIO(z.read(base + ".shx")))
    return r


BBOX = box(LON0, LAT0, LON1, LAT1)
OLDBOX = box(OLD[0], OLD[2], OLD[1], OLD[3])
TIGER = "https://www2.census.gov/geo/tiger/TIGER2023"
COUNTIES = ["001", "013", "041", "075", "081", "085"]
G = {}


@step("stations and tides")
def stations_and_tides():
    tides = {}
    for sid, nm in STATIONS.items():
        meta = get(f"https://api.tidesandcurrents.noaa.gov/mdapi/prod/webapi/stations/{sid}.json").json()
        st = meta["stations"][0]
        lat, lon = st["lat"], st["lng"]
        navd = msl = None
        try:
            d = get(f"https://api.tidesandcurrents.noaa.gov/mdapi/prod/webapi/stations/{sid}/datums.json?units=english").json()
            vals = {x["name"]: x["value"] for x in d.get("datums", [])}
            if "NAVD88" in vals and "MLLW" in vals:
                navd = round(vals["NAVD88"] - vals["MLLW"], 3)   # NAVD88 height above MLLW
            if "MSL" in vals and "MLLW" in vals:
                msl = round(vals["MSL"] - vals["MLLW"], 3)
        except Exception as e:
            log("datums", sid, e)
        p = get("https://api.tidesandcurrents.noaa.gov/api/prod/datagetter", params=dict(
            product="predictions", interval="hilo", datum="MLLW", station=sid, begin_date="20261121",
            end_date="20261130", time_zone="lst_ldt", units="english", format="json",
            application="sfofloodweek")).json()
        hilo = [[x["t"], float(x["v"]), x["type"]] for x in p.get("predictions", [])]
        tides[nm] = {"id": sid, "name": st["name"], "lat": lat, "lon": lon, "navd_above_mllw": navd, "msl_above_mllw": msl, "hilo": hilo}
        log(sid, nm, st["name"], lat, lon, "navd", navd, "events", len(hilo))
    # Stations without a NAVD88 tie: carry the MSL-to-NAVD88 gap over from the nearest tied station
    for nm, v in tides.items():
        if v["navd_above_mllw"] is None and v["msl_above_mllw"] is not None:
            refs = [(((v["lon"] - r["lon"]) * CO) ** 2 + (v["lat"] - r["lat"]) ** 2, r) for k, r in tides.items()
                    if r["navd_above_mllw"] is not None and r["msl_above_mllw"] is not None]
            if refs:
                r = min(refs, key=lambda x: x[0])[1]
                v["navd_above_mllw"] = round(v["msl_above_mllw"] - (r["msl_above_mllw"] - r["navd_above_mllw"]), 3)
                v["navd_note"] = f"estimated from MSL and {r['name']}"
                log("navd estimate", nm, v["navd_above_mllw"], v["navd_note"])
    json.dump(tides, open(f"{OUT}/tides.json", "w"), indent=1)
    G["stations"] = {k: (v["lon"], v["lat"]) for k, v in tides.items()}
    OUTPUT["stations"] = {k: {"lon": v["lon"], "lat": v["lat"], "name": v["name"], "id": v["id"]} for k, v in tides.items()}


@step("land and water")
def land_and_water():
    cz = get(f"{TIGER}/COUNTY/tl_2023_us_county.zip", timeout=600).content
    r = shp_from_zip(cz)
    fields = [f[0] for f in r.fields[1:]]
    polys = []
    for sr in r.iterShapeRecords():
        rec = dict(zip(fields, sr.record))
        if rec["STATEFP"] == "06" and rec["COUNTYFP"] in COUNTIES:
            polys.append(make_valid(shape(sr.shape.__geo_interface__)))
    counties = unary_union(polys).intersection(BBOX)
    log("counties", counties.area)
    water = []
    for c in COUNTIES:
        z = get(f"{TIGER}/AREAWATER/tl_2023_06{c}_areawater.zip").content
        wr = shp_from_zip(z)
        n = 0
        for sr in wr.iterShapes():
            g = make_valid(shape(sr.__geo_interface__))
            if not g.intersects(BBOX):
                continue
            if g.area < 4e-6:
                continue
            water.append(g)
            n += 1
        log("water", c, n)
    water_u = unary_union(water).intersection(BBOX)
    land = make_valid(counties.difference(water_u)).buffer(0)
    land = land.buffer(-0.00002).buffer(0.00002)
    land = MultiPolygon([p for p in polys_only(land).geoms if p.area > 2e-7])
    G["land_ll"] = land
    sea = make_valid(BBOX.difference(land))
    bay = None
    for g in getattr(sea, "geoms", [sea]):
        if g.contains(Point(*BAY_POINT)):
            bay = g
    G["bay_ll"] = bay
    log("land area", land.area, "bay", bay.area if bay else None)
    lp = to_px(land).simplify(3, preserve_topology=True)
    lp_new = polys_only(make_valid(lp.difference(to_px(OLDBOX))).buffer(0))
    OUTPUT["land"] = path_of(lp_new)
    log("land path chars", len(OUTPUT["land"]))


def zone_of(lon, lat):
    """Tide zone for a point: the hand-built peninsula rule inside OLD, else nearest station."""
    if OLD[0] <= lon <= OLD[1] and OLD[2] <= lat <= OLD[3]:
        return "op" if (lat < 37.712 and lon > -122.46) else "sf"
    if lon < -122.40 and lat < 37.78:
        return "sf"   # Pacific coast
    best, bd = None, 1e9
    for k, (slon, slat) in G["stations"].items():
        d = ((lon - slon) * CO) ** 2 + (lat - slat) ** 2
        if d < bd:
            best, bd = k, d
    return best


@step("shoreline")
def shoreline():
    bay = G["bay_ll"]
    edge = bay.boundary.intersection(BBOX.buffer(-0.0005))
    edge = edge.difference(OLDBOX)
    lines = []
    for g in getattr(edge, "geoms", [edge]):
        if g.geom_type == "LineString":
            lines.append(g)
        elif hasattr(g, "geoms"):
            lines += [x for x in g.geoms if x.geom_type == "LineString"]
    merged = linemerge(lines) if lines else MultiLineString()
    zones = {}
    for ln in getattr(merged, "geoms", [merged]):
        ln = to_px(ln).simplify(2)
        pts = list(ln.coords)
        cur, run = None, []
        for x, y in pts:
            lon, lat = x / (S * CO) + LON0, LAT1 - y / S
            z = zone_of(lon, lat)
            if z != cur and run:
                run.append((x, y))
                zones.setdefault(cur, []).append(LineString(run) if len(run) > 1 else None)
                run = [(x, y)]
            else:
                run.append((x, y))
            cur = z
        if len(run) > 1:
            zones.setdefault(cur, []).append(LineString(run))
    OUTPUT["shore"] = {z: path_of(MultiLineString([l for l in ls if l is not None]), False) for z, ls in zones.items()}
    log("shore zones", {z: len(p) for z, p in OUTPUT["shore"].items()})


@step("roads")
def roads():
    z = get(f"{TIGER}/PRIMARYROADS/tl_2023_us_primaryroads.zip", timeout=600).content
    r = shp_from_zip(z)
    fields = [f[0] for f in r.fields[1:]]
    keep = []
    for sr in r.iterShapeRecords():
        g = shape(sr.shape.__geo_interface__)
        if not g.intersects(BBOX):
            continue
        g = g.intersection(BBOX).difference(OLDBOX)
        if not g.is_empty:
            keep.append(g)
    u = linemerge(unary_union(keep))
    u = to_px(u).simplify(6)
    u = MultiLineString([l for l in getattr(u, "geoms", [u]) if l.geom_type == "LineString" and l.length > 25])
    OUTPUT["roads"] = path_of(u, False)
    log("roads chars", len(OUTPUT["roads"]))


def nominatim(q):
    r = get("https://nominatim.openstreetmap.org/search", params=dict(q=q, format="jsonv2", polygon_geojson=1, limit=3))
    for it in r.json():
        g = shape(it["geojson"])
        if g.geom_type in ("Polygon", "MultiPolygon"):
            return make_valid(g)
    return None


@step("airports")
def airports():
    oak = nominatim("Oakland San Francisco Bay Airport") or nominatim("Oakland International Airport")
    time.sleep(1.2)
    sfo = nominatim("San Francisco International Airport")
    G["oak"], G["sfo"] = oak, sfo
    if oak is not None:
        OUTPUT["oak"] = path_of(polys_only(to_px(oak).simplify(2)))
        c = oak.centroid
        OUTPUT["oak_center"] = [c.x, c.y]
        OUTPUT["oak_bounds"] = list(oak.bounds)
    if sfo is not None:
        OUTPUT["sfo_bounds"] = list(sfo.bounds)
    log("oak", oak.bounds if oak is not None else None, "sfo", sfo.bounds if sfo is not None else None)


def export(bbox, w, h, interp):
    q = dict(bbox=",".join(map(str, bbox)), bboxSR=4326, imageSR=4326, size=f"{w},{h}", format="bsq",
             pixelType="F32", noData=-9999, interpolation=interp, f="image")
    r = get("https://elevation.nationalmap.gov/arcgis/rest/services/3DEPElevation/ImageServer/exportImage",
            params=q, timeout=300)
    a = np.frombuffer(r.content[: w * h * 4], dtype="<f4").reshape(h, w).copy()
    return a


@step("topo raster")
def topo():
    fine = G["fine"]
    B = 9
    h, w = fine.shape[0] // B, fine.shape[1] // B
    f = fine[: h * B, : w * B]
    ok = (f > -9000) & (np.abs(f) >= 0.02)
    v = np.where(ok, f, 0).reshape(h, B, w, B).sum(axis=(1, 3))
    n = ok.reshape(h, B, w, B).sum(axis=(1, 3))
    a = np.where(n > 0, v / np.maximum(n, 1), 0)
    ft = a * 3.28084
    ed = np.array([5, 10, 15, 25, 50, 100, 200, 400, 700])
    c = np.searchsorted(ed, ft, side="right")
    g = np.round((c + 0.5) * 255 / 10).astype(np.uint8)
    Image.fromarray(g, "L").save(f"{OUT}/topo.png", optimize=True)
    x0, y0 = pxy(LON0, LAT1)
    x1, y1 = pxy(LON0 + w * B * CELL / 3, LAT1 - h * B * CELL / 3)
    OUTPUT["topo"] = {"file": "data/topo.png", "x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0, "classes": 10}
    log("topo", w, h, os.path.getsize(f"{OUT}/topo.png"))


@step("low-ground raster")
def low():
    K = 3
    fx, fy = NX * K, NY * K
    fine = np.empty((fy, fx), dtype=np.float32)
    TX, TY = 4, 4
    xs = np.linspace(0, fx, TX + 1).astype(int) // K * K
    ys = np.linspace(0, fy, TY + 1).astype(int) // K * K
    xs[-1], ys[-1] = fx, fy
    fc = CELL / K
    for j in range(TY):
        for i in range(TX):
            a0, a1, b0, b1 = xs[i], xs[i + 1], ys[j], ys[j + 1]
            bb = (LON0 + a0 * fc, LAT1 - b1 * fc, LON0 + a1 * fc, LAT1 - b0 * fc)
            fine[b0:b1, a0:a1] = export(bb, a1 - a0, b1 - b0, "RSP_NearestNeighbor")
            log("tile", i, j)
    G["fine"] = fine
    wet = (fine < -9000) | (np.abs(fine) < 0.02)
    v = np.where(wet, 0, fine).reshape(NY, K, NX, K)
    n = (~wet).reshape(NY, K, NX, K).sum(axis=(1, 3))
    s = v.sum(axis=(1, 3))
    avg = np.where(n >= 3, s / np.maximum(n, 1), np.nan)
    ft = avg * 3.28084
    c = np.full(ft.shape, 41, dtype=np.uint8)
    ok = ~np.isnan(ft)
    c[ok & (ft < 4)] = 0
    mid = ok & (ft >= 4) & (ft < 14)
    c[mid] = (1 + np.floor((ft[mid] - 4) / 0.25)).astype(np.uint8)
    G["cls"] = c
    # zones per cell
    lon = LON0 + (np.arange(NX) + 0.5) * CELL
    lat = LAT1 - (np.arange(NY) + 0.5) * CELL
    LON, LAT = np.meshgrid(lon, lat)
    zid = np.zeros((NY, NX), dtype=np.uint8)
    names = list(G["stations"].keys())
    st = np.array([G["stations"][k] for k in names])
    d = ((LON[..., None] - st[:, 0]) * CO) ** 2 + (LAT[..., None] - st[:, 1]) ** 2
    zid = np.argmin(d, axis=2).astype(np.uint8)
    sf, op = names.index("sf"), names.index("op")
    zid[(LON < -122.40) & (LAT < 37.78)] = sf
    inold = (LON >= OLD[0]) & (LON <= OLD[1]) & (LAT >= OLD[2]) & (LAT <= OLD[3])
    zid[inold] = np.where((LAT[inold] < 37.712) & (LON[inold] > -122.46), op, sf)
    G["zid"], G["znames"] = zid, names
    gray = np.round((c.astype(np.float32) + 0.5) * 255 / 42).astype(np.uint8)
    OUTPUT["low"] = {"classes": 42, "lo": 4, "step": 0.25, "cell": CELL, "zones": {}}
    for k, nm in enumerate(names):
        m = (zid == k) & (c < 41)
        if not m.any():
            continue
        rows, cols = np.where(m)
        r0, r1, c0, c1 = rows.min(), rows.max() + 1, cols.min(), cols.max() + 1
        la = np.zeros((r1 - r0, c1 - c0, 2), dtype=np.uint8)
        la[..., 0] = gray[r0:r1, c0:c1]
        la[..., 1] = np.where(m[r0:r1, c0:c1], 255, 0)
        fn = f"{OUT}/low_{nm}.png"
        Image.fromarray(la, "LA").save(fn, optimize=True)
        x0, y0 = c0 * CELL * S * CO, r0 * CELL * S
        OUTPUT["low"]["zones"][nm] = {"file": f"data/low_{nm}.png", "x": x0, "y": y0,
                                      "w": (c1 - c0) * CELL * S * CO, "h": (r1 - r0) * CELL * S,
                                      "bytes": os.path.getsize(fn)}
        log("low", nm, c1 - c0, r1 - r0, os.path.getsize(fn))


def poly_mask(g):
    img = Image.new("L", (NX, NY), 0)
    dr = ImageDraw.Draw(img)
    for p in getattr(g, "geoms", [g]):
        if p.geom_type != "Polygon":
            continue
        ext = [((x - LON0) / CELL, (LAT1 - y) / CELL) for x, y in p.exterior.coords]
        dr.polygon(ext, fill=255)
        for i in p.interiors:
            dr.polygon([((x - LON0) / CELL, (LAT1 - y) / CELL) for x, y in i.coords], fill=0)
    return np.array(img) > 0


@step("airport stats")
def airport_stats():
    st = {}
    for nm in ("sfo", "oak"):
        g = G.get(nm)
        if g is None or "cls" not in G:
            continue
        m = poly_mask(g)
        h = np.bincount(G["cls"][m], minlength=42)
        st[nm] = {"hist": h.tolist(), "n": int(m.sum())}
        log(nm, "cells", int(m.sum()))
    OUTPUT["stats"] = st


@step("oak dike crest")
def oak_crest():
    oak, fine, bay = G.get("oak"), G.get("fine"), G.get("bay_ll")
    if oak is None or fine is None:
        return
    fc = CELL / 3
    ring = oak.exterior if oak.geom_type == "Polygon" else max(oak.geoms, key=lambda p: p.area).exterior
    near = bay.buffer(0.0015) if bay is not None else None
    crest = []
    L = ring.length
    for t in np.arange(0, L, 0.0002):
        p = ring.interpolate(t)
        if near is not None and not near.contains(p):
            continue
        ci, ri = int((p.x - LON0) / fc), int((LAT1 - p.y) / fc)
        win = fine[max(0, ri - 4): ri + 5, max(0, ci - 4): ci + 5]
        win = win[(win > -9000) & (np.abs(win) > 0.02)]
        if win.size:
            crest.append(float(win.max()) * 3.28084)
    if crest:
        a = np.array(crest)
        OUTPUT["oak_crest_ft"] = {"n": len(a), "min": round(float(a.min()), 1), "p10": round(float(np.percentile(a, 10)), 1),
                                  "median": round(float(np.median(a)), 1)}
        log("oak crest", OUTPUT["oak_crest_ft"])


def main():
    stations_and_tides()
    land_and_water()
    if G.get("bay_ll") is not None and G.get("stations"):
        shoreline()
    roads()
    airports()
    if G.get("stations"):
        low()
    if G.get("fine") is not None:
        topo()
    airport_stats()
    oak_crest()
    OUTPUT["built"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    json.dump(OUTPUT, open(f"{OUT}/bay.json", "w"))
    log("done; bay.json bytes", os.path.getsize(f"{OUT}/bay.json"), "errors", OUTPUT["errors"])


if __name__ == "__main__":
    main()
