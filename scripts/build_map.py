"""
Build the map of Gotham: web/map/gotham.geojson, from wayne/engine/gotham.json.

    venv/bin/pip install shapely        # dev-time only; the console never imports it
    venv/bin/python scripts/build_map.py

The layout file holds what a person decides — the coastline, where the districts
sit and which way their streets run, the parks, lakes and rivers, the named
roads, bridges and docks, and every place people go. This turns it into a city:

  * coastlines roughened, then smoothed into curves, so nothing reads as a polygon;
  * districts as the cells around their centres, clipped to the land, each with a
    street grid of its own at its own angle and block size — warped in the old
    quarters, staggered so not every street runs through — and the borders
    between them as the avenues they'd be;
  * the named roads, a harbour drive around the island and a drive around the
    park, highways, bridges and rail, all smoothed;
  * every block between the streets cut into lots and given a height from its
    district — tallest downtown, around Wayne Tower — for the 3D view;
  * piers along the docks, lakes, rivers, and the labels and landmarks on top.

Deterministic: the same layout always builds the same city. Coordinates stay in
the layout's units (two decimals); the page turns them into map positions.
"""
import json
import math
import random
from pathlib import Path

import shapely
from shapely import affinity
from shapely.geometry import LineString, MultiPoint, Point, Polygon, box, mapping
from shapely.ops import split, substring, unary_union

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "wayne" / "engine" / "gotham.json"
OUT = ROOT / "web" / "map" / "gotham.geojson"
# Buildings go in a file of their own, compact — [height, x, y, x, y, ...] in
# hundredths — and load after the city, so the first look is quick.
BUILDINGS = ROOT / "web" / "map" / "gotham-buildings.json"

# Districts with docks and wharves along them keep their edge hard; elsewhere a
# strip of waterfront park runs along the shore.
NO_WATERFRONT = {"Tricorner", "Amusement Mile", "Chinatown", "City Hall District", "Robinsville"}
# Districts with almost nothing built: parkland, asylum grounds, an abandoned funfair.
SPARSE = {"Arkham Island", "Paris Island"}


# --- shapes ---------------------------------------------------------------------

def roughen(points, seed, amp=0.32, levels=2, closed=True):
    """Midpoint displacement: a hand-drawn coast gets the irregularity of a real one."""
    r = random.Random(seed)
    pts = [tuple(p) for p in points]
    for _ in range(levels):
        out, n = [], len(pts)
        for i in range(n if closed else n - 1):
            a, b = pts[i], pts[(i + 1) % n]
            out.append(a)
            dx, dy = b[0] - a[0], b[1] - a[1]
            length = math.hypot(dx, dy)
            if 0.8 < length < 40:
                d = r.uniform(-1, 1) * amp * min(1.0, length / 4)
                out.append(((a[0] + b[0]) / 2 - dy / length * d, (a[1] + b[1]) / 2 + dx / length * d))
        if not closed:
            out.append(pts[-1])
        pts, amp = out, amp * 0.55
    return pts


def chaikin(points, iterations=3, closed=True):
    """Corner cutting into curves. Long edges (the mainland's far frame) stay put."""
    pts = [tuple(p) for p in points]
    for _ in range(iterations):
        out, n = [], len(pts)
        if not closed:
            out.append(pts[0])
        for i in range(n if closed else n - 1):
            a, b = pts[i], pts[(i + 1) % n]
            if math.hypot(b[0] - a[0], b[1] - a[1]) > 60:
                out += [a, b]
                continue
            out.append((0.75 * a[0] + 0.25 * b[0], 0.75 * a[1] + 0.25 * b[1]))
            out.append((0.25 * a[0] + 0.75 * b[0], 0.25 * a[1] + 0.75 * b[1]))
        if not closed:
            out.append(pts[-1])
        pts = out
    return pts


def landmass(points, seed):
    return Polygon(chaikin(roughen(points, seed), 3)).buffer(0)


def curve(points, iterations=3):
    return LineString(chaikin(points, iterations, closed=False))


def ellipse(at, rx, ry):
    return affinity.scale(Point(at).buffer(1, 32), rx, ry)


def lines_of(geom):
    if geom.is_empty:
        return []
    if geom.geom_type == "LineString":
        return [geom]
    if hasattr(geom, "geoms"):
        return [g for part in geom.geoms for g in lines_of(part)]
    return []


def polys_of(geom):
    if geom.is_empty:
        return []
    if geom.geom_type == "Polygon":
        return [geom]
    if hasattr(geom, "geoms"):
        return [g for part in geom.geoms for g in polys_of(part)]
    return []


# --- streets ----------------------------------------------------------------------

def street_grid(area, angle, block, warp, seed):
    """Two families of streets across a district: the long blocks one way, the short
    the other, staggered so the cross streets don't all run through."""
    r = random.Random(seed)
    if area.is_empty:
        return []
    minx, miny, maxx, maxy = area.bounds
    cx, cy = (minx + maxx) / 2, (miny + maxy) / 2
    reach = math.hypot(maxx - minx, maxy - miny) / 2 + 2
    raw = []      # (line, class)
    for family, (deg, spacing, staggered) in enumerate((
            (angle, block, False), (angle + 90, block * r.uniform(1.45, 1.85), True))):
        a = math.radians(deg)
        ux, uy = math.cos(a), math.sin(a)
        vx, vy = -uy, ux
        t, n = -reach, 0
        while t < reach:
            n += 1
            cls = "avenue" if family == 0 and n % 4 == 0 else "street"
            steps = max(2, int(2 * reach / 0.7)) if warp else 1
            pts = []
            for k in range(steps + 1):
                s = -reach + 2 * reach * k / steps
                x, y = cx + vx * t + ux * s, cy + vy * t + uy * s
                if warp:
                    off = warp * math.sin(s / 2.7 + t * 0.41 + family * 1.7)
                    x, y = x + vx * off, y + vy * off
                pts.append((x, y))
            line = LineString(pts)
            if staggered:
                pos, length = 0.0, line.length
                while pos < length:
                    seg = r.uniform(2.6, 7.5)
                    if r.random() > 0.17:
                        raw.append((substring(line, pos, min(length, pos + seg)), cls))
                    pos += seg
            else:
                raw.append((line, cls))
            t += spacing * r.uniform(0.84, 1.16)
    streets = []
    for line, cls in raw:
        for part in lines_of(line.intersection(area)):
            if part.length > 0.35:
                streets.append((part, cls))
    return streets


# --- buildings ---------------------------------------------------------------------

def subdivide(poly, most, r, depth=0):
    """A block cut into lots across its long side, until each is a building's worth."""
    if poly.area <= most or depth > 4:
        return [poly]
    rect = list(poly.minimum_rotated_rectangle.exterior.coords)
    e1 = (rect[1][0] - rect[0][0], rect[1][1] - rect[0][1])
    e2 = (rect[2][0] - rect[1][0], rect[2][1] - rect[1][1])
    long = e1 if math.hypot(*e1) >= math.hypot(*e2) else e2
    length = math.hypot(*long) or 1
    ux, uy = long[0] / length, long[1] / length
    c = poly.centroid
    shift = r.uniform(-0.18, 0.18) * length
    ox, oy = c.x + ux * shift, c.y + uy * shift
    cut = LineString([(ox + uy * 60, oy - ux * 60), (ox - uy * 60, oy + ux * 60)])
    try:
        parts = split(poly, cut).geoms
    except Exception:
        return [poly]
    out = []
    for part in parts:
        if part.geom_type == "Polygon" and part.area > 0.01:
            out += subdivide(part, most, r, depth + 1)
    return out or [poly]




# --- landmarks, built to their own shapes -------------------------------------------

def square(cx, cy, size):
    return box(cx - size / 2, cy - size / 2, cx + size / 2, cy + size / 2)


def rect(cx, cy, w, h):
    return box(cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)


def disc(cx, cy, r):
    return Point(cx, cy).buffer(r, 20)


def landmark_shapes(name, x, y):
    """
    The few buildings anyone would know from the skyline, each as tiers of
    (footprint, base, top) in metres: Wayne Tower's setbacks and spire, the
    Clocktower's shaft, the Cathedral's cross and towers, Arkham's wings,
    Blackgate's yard, City Hall's dome, Ace's tanks, the Statue on its plinth.
    """
    if name == "Wayne Tower":
        return [(square(x, y, 0.95), 0, 95), (square(x, y, 0.76), 95, 205), (square(x, y, 0.56), 205, 292),
                (square(x, y, 0.34), 292, 348), (square(x, y, 0.09), 348, 412)]
    if name == "The Clocktower":
        return [(square(x, y, 0.62), 0, 38), (square(x, y, 0.34), 38, 104), (square(x, y, 0.25), 104, 126),
                (square(x, y, 0.1), 126, 142)]
    if name == "Gotham Cathedral":
        return [(rect(x, y, 1.45, 0.42), 0, 30), (rect(x + 0.25, y, 0.42, 1.05), 0, 30),
                (square(x - 0.62, y - 0.16, 0.22), 0, 66), (square(x - 0.62, y + 0.16, 0.22), 0, 66),
                (square(x + 0.25, y, 0.13), 30, 80)]
    if name == "Arkham Asylum":
        return [(rect(x, y - 0.55, 1.7, 0.42), 0, 24), (rect(x - 0.65, y + 0.05, 0.4, 1.2), 0, 20),
                (rect(x + 0.65, y + 0.05, 0.4, 1.2), 0, 20), (square(x, y - 0.55, 0.3), 24, 46),
                (rect(x - 1.7, y + 0.9, 0.55, 0.32), 0, 12), (rect(x + 1.6, y + 0.95, 0.5, 0.34), 0, 10),
                (square(x, y + 1.3, 0.38), 0, 16)]
    if name == "Blackgate Penitentiary":
        yard = square(x, y, 2.0).difference(square(x, y, 1.3))
        towers = [(square(x + dx, y + dy, 0.24), 0, 34) for dx in (-1, 1) for dy in (-1, 1)]
        return [(yard, 0, 20), (square(x, y, 0.5), 0, 26)] + towers
    if name == "Knightsdome":
        return [(disc(x, y, 1.0).difference(disc(x, y, 0.62)), 0, 36)]
    if name == "GCPD Central":
        return [(rect(x, y, 0.95, 0.72), 0, 58), (square(x + 0.2, y - 0.1, 0.3), 58, 67)]
    if name == "City Hall":
        return [(rect(x, y, 1.15, 0.72), 0, 22), (disc(x, y, 0.3), 22, 30), (disc(x, y, 0.26), 30, 35),
                (disc(x, y, 0.19), 35, 39), (disc(x, y, 0.1), 39, 43), (disc(x, y, 0.03), 43, 50)]
    if name == "Ace Chemicals":
        tanks = [(disc(x + 0.35 + 0.38 * i, y + dy, 0.16), 0, 16) for i in range(3) for dy in (-0.25, 0.25)]
        return [(rect(x - 0.35, y, 0.9, 0.55), 0, 22), (disc(x - 0.7, y - 0.25, 0.07), 0, 54)] + tanks
    if name == "Statue of Justice":
        return [(square(x, y, 0.46), 0, 12), (square(x, y, 0.26), 12, 18), (square(x, y, 0.1), 18, 46),
                (square(x + 0.04, y, 0.04), 46, 56)]
    if name == "Cape Carmine Lighthouse":
        return [(disc(x, y, 0.15), 0, 6), (disc(x, y, 0.08), 6, 34), (disc(x, y, 0.11), 34, 39)]
    if name == "Iceberg Lounge":
        return [(disc(x, y, 0.5), 0, 8), (disc(x, y, 0.38), 8, 16), (disc(x, y, 0.22), 16, 25)]
    if name == "Gotham Opera House":
        return [(disc(x, y, 0.56), 0, 18), (disc(x, y, 0.4), 18, 26), (disc(x, y, 0.2), 26, 31)]
    if name == "Union Station":
        return [(rect(x, y, 1.45, 0.56), 0, 20), (rect(x, y, 1.45, 0.3), 20, 27)]
    if name == "Gotham University":
        quad = [(rect(x, y - 0.5, 1.0, 0.22), 0, 18), (rect(x, y + 0.5, 1.0, 0.22), 0, 18),
                (rect(x - 0.5, y, 0.22, 1.0), 0, 18), (rect(x + 0.5, y, 0.22, 1.0), 0, 18)]
        return quad + [(square(x + 0.5, y - 0.5, 0.2), 0, 44)]
    if name == "Gotham Observatory":
        return [(disc(x, y, 0.3), 0, 10), (disc(x, y, 0.25), 10, 16), (disc(x, y, 0.15), 16, 20)]
    if name == "Gotham General Hospital":
        return [(rect(x, y, 1.1, 0.34), 0, 40), (rect(x, y, 0.34, 1.0), 0, 40)]
    if name == "Gotham Power Station":
        return [(rect(x, y, 1.0, 0.6), 0, 24)] + [(disc(x - 0.3 + 0.3 * i, y - 0.55, 0.1), 0, 72) for i in range(3)]
    if name == "Monarch Theatre":
        return [(rect(x, y, 0.72, 0.46), 0, 17)]
    return []


# --- terrain ---------------------------------------------------------------------------

# The page's projection: one layout unit is K degrees, centred on the island.
K = 0.0013475
TERRAIN = ROOT / "web" / "map" / "terrain"
# Hills that rise out of the city, as (x, y, radius, metres).
HILLS = [(38.0, 25.5, 6.5, 46), (21.0, 92.0, 5.5, 34), (27.0, 66.5, 4.5, 20), (33.5, 46.0, 4.0, 24),
         (44.0, 72.0, 5.0, 9), (28.0, 124.0, 4.0, 11), (4.0, 20.0, 8.0, 72), (131.0, -17.0, 6.0, 42),
         (69.0, 122.6, 2.2, 14), (57.6, 130.4, 1.0, 6)]


def _png(path, rgb):
    """A minimal RGB PNG, no imaging library needed."""
    import struct
    import zlib
    h, w = len(rgb), len(rgb[0]) // 3

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + bytes(row) for row in rgb)
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def terrain_tiles(land):
    """
    The ground under the city, as terrain-RGB tiles: the island low and flat
    downtown, rising into Cherry Hills and Gotham Heights; the mainland climbing
    west into a ridge; the water below it all. Drawn as hillshade, and as real
    relief when the map tilts.
    """
    import numpy as np
    rng = np.random.default_rng(7)
    lattice = rng.random((257, 257))

    def noise(x, y, scale):
        gx, gy = x / scale, y / scale
        x0, y0 = np.floor(gx).astype(int), np.floor(gy).astype(int)
        fx, fy = gx - x0, gy - y0
        fx, fy = fx * fx * (3 - 2 * fx), fy * fy * (3 - 2 * fy)
        def at(i, j):
            return lattice[i % 256, j % 256]
        top = at(x0, y0) * (1 - fx) + at(x0 + 1, y0) * fx
        bottom = at(x0, y0 + 1) * (1 - fx) + at(x0 + 1, y0 + 1) * fx
        return top * (1 - fy) + bottom * fy

    shapely.prepare(land)
    if TERRAIN.exists():
        import shutil
        shutil.rmtree(TERRAIN)
    lon0, lat0, lon1, lat1 = -0.48, -0.36, 0.38, 0.50
    count = 0
    for z in (9, 10, 11):
        n = 2 ** z
        tx0, tx1 = int((lon0 + 180) / 360 * n), int((lon1 + 180) / 360 * n)
        def ty(lat, n=n):
            r = math.radians(lat)
            return int((1 - math.log(math.tan(r) + 1 / math.cos(r)) / math.pi) / 2 * n)
        for tx in range(tx0, tx1 + 1):
            for tyy in range(ty(lat1), ty(lat0) + 1):
                px = (np.arange(256) + 0.5) / 256
                lon = (tx + px) / n * 360 - 180
                lat = np.degrees(np.arctan(np.sinh(np.pi * (1 - 2 * (tyy + px) / n))))
                LON, LAT = np.meshgrid(lon, lat)
                X, Y = LON / K + 55, 65 - LAT / K
                on_land = shapely.contains_xy(land, X, Y)
                fbm = noise(X, Y, 9.0) * 0.6 + noise(X, Y, 3.0) * 0.3 + noise(X, Y, 1.2) * 0.1
                h = 3 + fbm * 4
                for cx, cy, r, hh in HILLS:
                    h = h + hh * np.exp(-((X - cx) ** 2 + (Y - cy) ** 2) / (r * r))
                west = np.clip(8 - X, 0, None)
                h = h + west * 3.2 * (0.7 + fbm * 0.6)
                north = np.where((X > 14) & (X < 100), np.clip(8 - Y, 0, None) * 2.2, 0)
                h = h + north * (0.6 + fbm * 0.8)
                h = np.clip(h, 1, 320)
                h = np.where(on_land, h, -10 - fbm * 6)
                v = np.round((h + 10000) * 10).astype(np.int64)
                rgb = np.stack([(v >> 16) & 255, (v >> 8) & 255, v & 255], axis=-1).astype(np.uint8)
                path = TERRAIN / str(z) / str(tx)
                path.mkdir(parents=True, exist_ok=True)
                _png(path / f"{tyy}.png", rgb.reshape(256, 256 * 3).tolist())
                count += 1
    return count, [lon0, lat0, lon1, lat1]


# --- the city ----------------------------------------------------------------------

def build():
    src = json.loads(SOURCE.read_text())
    features, buildings = [], []

    def building(geom, h, kind="", base=0):
        for poly in polys_of(shapely.set_precision(geom, 0.01)):
            ring = list(poly.exterior.coords)[:-1]
            if len(ring) >= 3:
                row = [int(h), int(base)] + [round(v * 100) for pt in ring for v in pt]
                buildings.append(row + ([kind] if kind else []))

    def add(geom, layer, **props):
        if geom is None or geom.is_empty:
            return
        g = shapely.set_precision(geom, 0.01)
        if g.is_empty:
            return
        if g.geom_type == "LinearRing":
            g = LineString(g.coords)
        if g.geom_type == "GeometryCollection":
            # One bad shape would sink the whole layer on the page: keep what draws.
            for part in g.geoms:
                add(part, layer, **props)
            return
        features.append({"type": "Feature", "geometry": mapping(g), "properties": {"l": layer, **props}})

    # Water first: rivers and lakes cut the land they run through.
    rivers = [curve(r["line"], 3).buffer(r["width"] / 2, quad_segs=8) for r in src["rivers"]]
    lakes = [ellipse(lake["at"], lake["rx"], lake["ry"]) for lake in src["lakes"]]
    water_cut = unary_union(rivers + lakes)

    island = landmass(src["coast"], 1)
    others = {i["name"]: landmass(i["coast"], 10 + n) for n, i in enumerate(src["islands"])}
    mainland = {m["name"]: landmass(m["coast"], 20 + n) for n, m in enumerate(src["mainland"])}
    island_land = island.difference(water_cut)
    land = unary_union([island_land] + [g.difference(water_cut) for g in others.values()]
                       + [g.difference(water_cut) for g in mainland.values()])
    for name, geom in [("Gotham", island_land), *others.items(), *mainland.items()]:
        add(geom.difference(water_cut), "land", n=name, k="mainland" if name in mainland else "island")
    for river, spec in zip(rivers, src["rivers"], strict=True):
        add(river.intersection(box(-400, -400, 400, 400)), "water", n=spec["name"])
    for lake, spec in zip(lakes, src["lakes"], strict=True):
        add(lake, "water", n=spec["name"])

    parks = [Polygon(chaikin(p["coast"], 3)).buffer(0) for p in src["parks"]]
    park_names = [p["name"] for p in src["parks"]]

    # Districts: the cells around their centres, clipped to the land they're on.
    ds = src["districts"]
    island_ds = [d for d in ds if "limit" not in d and "city" not in d]
    island_names = {d["name"] for d in island_ds}
    cells = shapely.voronoi_polygons(MultiPoint([tuple(d["at"]) for d in island_ds]),
                                     extend_to=box(-60, -60, 200, 200))
    areas = {}
    for d in island_ds:
        cell = next(c for c in cells.geoms if c.contains(Point(d["at"])))
        areas[d["name"]] = cell.intersection(island_land)
    blued = [d for d in ds if d.get("city") == "Blüdhaven"]
    limits = Polygon(src["bluedhaven_limits"]).intersection(mainland["Gotham County"]).difference(water_cut)
    bcells = shapely.voronoi_polygons(MultiPoint([tuple(d["at"]) for d in blued]),
                                      extend_to=box(60, -80, 220, 60))
    for d in blued:
        cell = next(c for c in bcells.geoms if c.contains(Point(d["at"])))
        areas[d["name"]] = cell.intersection(limits)
    for d in ds:
        if "limit" in d:
            host = mainland["Burnside"] if d["name"] == "Burnside" else mainland["Gotham County"]
            areas[d["name"]] = Polygon(chaikin(d["limit"], 2)).buffer(0).intersection(host).difference(water_cut)
    for extra in ("The Narrows", "Blackgate Isle", "Paris Island", "Justice Island"):
        areas[extra] = others[extra]
    grid_of = {d["name"]: d for d in ds}
    grid_of.setdefault("The Narrows", {"angle": 22, "block": 0.8, "tall": 12, "warp": 0.4})
    grid_of.setdefault("Blackgate Isle", {"angle": 8, "block": 2.2, "tall": 12})
    grid_of.setdefault("Paris Island", {"angle": -4, "block": 2.4, "tall": 6})
    grid_of.setdefault("Justice Island", {"angle": 0, "block": 3.0, "tall": 4})
    crime = {d["name"]: d.get("crime", 0.4) for d in ds} | src.get("district_crime_extra", {})

    # Waterfront park along the shore, wherever there aren't docks; a green
    # centre in every roundabout; the grounds of the Manor.
    shore = island_land.difference(island_land.buffer(-0.75))
    for name, area in areas.items():
        if name in NO_WATERFRONT or name not in island_names:
            continue
        parks.append(shore.intersection(area))
        park_names.append("")
    for rb in src.get("roundabouts", []):
        parks.append(disc(rb["at"][0], rb["at"][1], rb["r"] * 0.7))
        park_names.append("")
    parks.append(Polygon(chaikin([[-7, 3], [7, 2], [9, 15], [-6, 17]], 3)).buffer(0).difference(water_cut))
    park_names.append("Wayne Estate")
    park_union = unary_union(parks)
    for p, name in zip(parks, park_names, strict=True):
        add(p, "park", n=name)

    outer = {d["name"] for d in ds if "limit" in d or "city" in d}
    for i, (name, area) in enumerate(areas.items()):
        # Mainland districts melt into the land around them; their streets give them shape.
        add(area, "district", n=name, t=i % 12, cr=crime.get(name, 0.4), **({"k": "outer"} if name in outer else {}))

    # Streets, district by district — run right up to the borders, so every
    # street meets the avenue at its edge.
    streets = []
    for i, (name, area) in enumerate(areas.items()):
        g = grid_of[name]
        open_ground = area.buffer(0.05).intersection(land).difference(park_union).difference(water_cut)
        streets += street_grid(open_ground, g["angle"], g["block"], g.get("warp", 0), 100 + i)
    streets = [(s.simplify(0.02), c) for s, c in streets]

    # The borders between districts are avenues; the coast isn't.
    borders = unary_union([a.boundary for n, a in areas.items() if n in island_names
                           or grid_of.get(n, {}).get("city")])
    borders = borders.difference(land.boundary.buffer(0.55)).difference(water_cut.buffer(0.2))
    # Softened a little, so district borders read as avenues that bend, not ruled lines.
    secondary = [LineString(chaikin(list(s.coords), 2, closed=False)).simplify(0.03)
                 for s in lines_of(shapely.line_merge(borders)) if s.length > 0.6]

    named, elevated = [], []
    for road in src["roads"]:
        line = curve(road["line"])
        if road.get("elevated"):
            elevated.append(line)
        if road["class"] != "rail":
            line = line.intersection(land.buffer(0.05))
        for part in lines_of(line):
            named.append((part, road["class"], road["name"]))
    harbor = island.buffer(-1.15).exterior
    for part in lines_of(LineString(chaikin(list(harbor.coords), 2, closed=False))
                         .difference(water_cut.buffer(0.25))):
        if part.length > 2:
            named.append((part, "primary", "Harbor Drive"))
    park_drive = parks[0].buffer(-0.9).exterior
    named.append((LineString(park_drive.coords), "secondary", "Park Drive"))
    for rb in src.get("roundabouts", []):
        named.append((LineString(disc(rb["at"][0], rb["at"][1], rb["r"]).exterior.coords), "primary", rb["name"]))
    bridges = [(curve(b["line"], 2), b["name"]) for b in src["bridges"]]

    for s, c in streets:
        add(s, "road", c=c)
    for s in secondary:
        add(s, "road", c="secondary")
    for line, cls, name in named:
        add(line, "road", c=cls, n=name, **({"e": 1} if name == "Gotham Skyway" else {}))
    for line, name in bridges:
        add(line, "road", c="bridge", n=name)

    # Below and around it: the subway, the ferries, the airport.
    for line in src.get("subway", []):
        pts = [(st[1], st[2]) for st in line["stations"]]
        add(curve(pts, 3), "subway", n=line["name"], col=line["color"])
        for st in line["stations"]:
            add(Point(st[1], st[2]), "station", n=st[0], col=line["color"], line=line["name"])
    for ferry in src.get("ferries", []):
        add(curve(ferry["line"], 2), "ferry", n=ferry["name"])
    airport = src.get("airport")
    if airport:
        add(Polygon(airport["apron"]), "apron")
        for a, b in airport["runways"]:
            strip = LineString([a, b])
            add(strip.buffer(0.42, cap_style="flat"), "runway")
            add(strip, "runway_line")
        building(Polygon(airport["terminal"]), 18, "~landmark")
        building(disc(-12.8, 121.4, 0.14), 48, "~landmark")
        building(disc(-12.8, 121.4, 0.22), 54, "~landmark", base=48)

    # Piers along the docks, pointing out to the water.
    for zone in src["piers"]:
        (x0, y0), (x1, y1) = zone["box"]
        r = random.Random(zone["name"])
        frame = box(x0, y0, x1, y1)
        for edge in lines_of(land.boundary.intersection(frame)):
            pos = 0.4
            while pos < edge.length - 0.4:
                p = edge.interpolate(pos)
                q = edge.interpolate(min(edge.length, pos + 0.05))
                dx, dy = q.x - p.x, q.y - p.y
                n = math.hypot(dx, dy) or 1
                nx, ny = -dy / n, dx / n
                if land.contains(Point(p.x + nx * 0.4, p.y + ny * 0.4)):
                    nx, ny = -nx, -ny
                length = zone["length"] * r.uniform(0.7, 1.15)
                pier = LineString([(p.x - nx * 0.1, p.y - ny * 0.1),
                                   (p.x + nx * length, p.y + ny * length)]).buffer(0.13, cap_style="flat")
                if pier.intersection(land).area < 0.08:
                    add(pier, "pier")
                pos += r.uniform(0.8, 1.3)

    # Landmarks first, so the blocks around them leave them room.
    places_by = {p["name"]: p for p in src["places"]}
    reserved = []
    for name in places_by:
        p = places_by[name]
        tiers = landmark_shapes(name, p["x"], p["y"])
        for shape, base, top in tiers:
            building(shape, top, "~landmark", base=base)
        if tiers:
            reserved.append(unary_union([t[0] for t in tiers]).buffer(0.4))
    for line in elevated:
        # The skyway stands on piers above the street: a deck in the air.
        for part in lines_of(line.intersection(land)):
            building(part.buffer(0.17, cap_style="flat"), 19, "~deck", base=15)

    # Buildings: the blocks between the streets, cut into lots, given heights —
    # skyscrapers clustering in more than one place, as in any real city.
    cuts = unary_union([s.buffer(0.12 if c == "avenue" else 0.085, quad_segs=2) for s, c in streets]
                       + [s.buffer(0.15, quad_segs=2) for s in secondary]
                       + [ln.buffer(0.24 if c == "highway" else 0.2 if c == "primary" else 0.15, quad_segs=2)
                          for ln, c, _ in named if c != "rail"]
                       + [ln.buffer(0.22) for ln, _ in bridges] + reserved)
    cores = src.get("cores", [])
    count = 0
    for i, (name, area) in enumerate(areas.items()):
        g = grid_of[name]
        r = random.Random(500 + i)
        if name in SPARSE or name == "Justice Island":
            continue
        ground = area.difference(park_union).difference(water_cut).difference(cuts)
        ground = ground.difference(land.boundary.buffer(0.3))
        for blk in polys_of(ground):
            if blk.area < 0.08:
                continue
            for lot in subdivide(blk, 0.6 if g["tall"] >= 30 else 0.45, r):
                foot = lot.buffer(-0.06, join_style="mitre")
                for piece in polys_of(foot):
                    if piece.area < 0.05:
                        continue
                    c = piece.centroid
                    pull = 1 + sum(k * math.exp(-((c.x - cx) ** 2 + (c.y - cy) ** 2) / (rad * rad))
                                   for cx, cy, k, rad in cores)
                    h = g["tall"] * r.lognormvariate(0, 0.5) * pull
                    if r.random() < 0.025:
                        h *= 2.4            # the odd tower, anywhere
                    building(piece.simplify(0.04), round(max(4, min(260, h))))
                    count += 1

    # Trees, through the parks.
    trees = []
    tr = random.Random(77)
    paths = unary_union([ln.buffer(0.16) for ln, c, _ in named if c in ("primary", "secondary")])
    for park in polys_of(park_union):
        minx, miny, maxx, maxy = park.bounds
        step = 0.34
        y = miny
        while y < maxy:
            x = minx
            while x < maxx:
                px, py = x + tr.uniform(-0.12, 0.12), y + tr.uniform(-0.12, 0.12)
                if tr.random() < 0.72 and park.contains(Point(px, py)) and not water_cut.contains(Point(px, py)) \
                        and not paths.contains(Point(px, py)):
                    trees.append([round(px * 100), round(py * 100), tr.randint(8, 17)])
                x += step
            y += step

    # Water that catches the light.
    import numpy as np
    sr = np.random.default_rng(3)
    xs, ys = sr.uniform(-6, 104, 9000), sr.uniform(4, 142, 9000)
    wet = ~shapely.contains_xy(land, xs, ys)
    for x, y, phase in zip(xs[wet][:2600], ys[wet][:2600], sr.uniform(0, 6.283, 2600), strict=False):
        add(Point(round(float(x), 2), round(float(y), 2)), "sparkle", p=round(float(phase), 2))

    # Labels and landmarks.
    for p in src["places"]:
        add(Point(p["x"], p["y"]), "place", n=p["name"], k=p["kind"], i=p.get("icon", ""), a=p["area"])
    for w in src["water"]:
        add(Point(w["at"]), "water_label", n=w["name"], r=w.get("rotate", 0))
    for name, at in (("Gotham County", (-6, 50)), ("Blackgate Isle", (69, 127.4)), ("Blüdhaven", (121, -22)),
                     ("Justice Island", (57.6, 132.8))):
        add(Point(at), "area_label", n=name)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"type": "FeatureCollection", "features": features},
                              ensure_ascii=False, separators=(",", ":")))
    BUILDINGS.write_text(json.dumps(buildings, ensure_ascii=False, separators=(",", ":")))
    (OUT.parent / "gotham-trees.json").write_text(json.dumps(trees, separators=(",", ":")))
    tiles, bounds = terrain_tiles(land)
    size = sum(f.stat().st_size for f in TERRAIN.rglob("*.png"))
    print(f"{len(features)} features ({OUT.stat().st_size / 1e6:.2f} MB), {len(buildings)} buildings "
          f"({BUILDINGS.stat().st_size / 1e6:.2f} MB), {len(trees)} trees, {tiles} terrain tiles "
          f"({size / 1e6:.2f} MB, bounds {bounds})")


if __name__ == "__main__":
    build()
