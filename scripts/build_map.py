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
import shapely.ops
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


def landmass(points, seed, amp=0.32, levels=2):
    return Polygon(chaikin(roughen(points, seed, amp=amp, levels=levels), 3)).buffer(0)


def curve(points, iterations=3):
    return LineString(chaikin(points, iterations, closed=False))


def ellipse(at, rx, ry, seed=0):
    """
    A lake as water really sits: lobes and inlets, at an angle of its own —
    never the ellipse it was sketched as.
    """
    r = random.Random(seed)
    phases = [r.uniform(0, 6.3) for _ in range(3)]
    pts = []
    for i in range(36):
        a = i * 2 * math.pi / 36
        k = (1 + 0.22 * math.sin(2 * a + phases[0]) + 0.13 * math.sin(3 * a + phases[1])
             + 0.07 * math.sin(5 * a + phases[2]))
        pts.append((math.cos(a) * rx * k, math.sin(a) * ry * k))
    shape = affinity.rotate(Polygon(pts), r.uniform(-35, 35), origin=(0, 0))
    shape = affinity.translate(shape, at[0], at[1])
    ring = list(shape.exterior.coords)[:-1]
    return Polygon(chaikin(roughen(ring, seed, amp=0.1 * min(rx, ry), levels=2), 2)).buffer(0)


def span(line, land):
    """
    A bridge from shore to shore: extended until it reaches land at both ends,
    then trimmed to the water it crosses with a footing on each bank. Drawn
    from a sketch, it stopped short of a coast that had since been roughened.
    """
    coords = list(line.coords)

    def stretch(a, b, by):
        dx, dy = b[0] - a[0], b[1] - a[1]
        n = math.hypot(dx, dy) or 1
        return (b[0] + dx / n * by, b[1] + dy / n * by)
    long = LineString([stretch(coords[1], coords[0], 12)] + coords + [stretch(coords[-2], coords[-1], 12)])
    wet = long.difference(land)
    crossings = [p for p in lines_of(wet) if p.length > 0.3]
    if not crossings:
        return None
    # The water it was meant to cross: the stretch nearest the sketch's middle.
    mid = line.interpolate(0.5, normalized=True)
    water = min(crossings, key=lambda p: p.distance(mid))
    a, b = water.coords[0], water.coords[-1]
    return LineString([stretch(water.coords[1], a, 0.45)] + list(water.coords) + [stretch(water.coords[-2], b, 0.45)])


def organic_cells(districts, clip, seed):
    """
    Districts as the land nearest each centre — but measured from a cloud of
    points around it, so borders wind and step the way real ones do instead
    of running ruler-straight between two centres.
    """
    r = random.Random(seed)
    points, owner = [], []
    for d in districts:
        cx, cy = d["at"]
        others = [math.dist(d["at"], e["at"]) for e in districts if e is not d] or [10]
        reach = min(others) * 0.44
        points.append((cx, cy))
        owner.append(d["name"])
        for _ in range(46):
            a, u = r.uniform(0, 2 * math.pi), r.random() ** 0.6
            points.append((cx + math.cos(a) * reach * u, cy + math.sin(a) * reach * u))
            owner.append(d["name"])
    cells = shapely.voronoi_polygons(MultiPoint(points), extend_to=box(-300, -300, 400, 400))
    pieces = {d["name"]: [] for d in districts}
    tree = shapely.STRtree([Point(p) for p in points])
    for cell in cells.geoms:
        hit = tree.query(cell, predicate="contains")
        if len(hit):
            pieces[owner[int(hit[0])]].append(cell)
    return {name: unary_union(cs).intersection(clip) for name, cs in pieces.items()}


def blob(cx, cy, radius, seed):
    """A small organic patch: a pocket park, a plaza, a pond."""
    r = random.Random(seed)
    pts = [(cx + math.cos(a) * radius * r.uniform(0.65, 1.25), cy + math.sin(a) * radius * r.uniform(0.65, 1.25))
           for a in [i * math.pi / 5 for i in range(10)]]
    return Polygon(chaikin(pts, 3)).buffer(0)


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
    warp = max(warp, 0.16)      # nowhere is ruler-straight
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
            steps = max(2, int(2 * reach / 0.7))
            bend, phase = warp * r.uniform(0.5, 1.6), r.uniform(0, 6.3)
            kink = r.uniform(-0.05, 0.05)           # a line a few degrees off the grid
            pts = []
            for k in range(steps + 1):
                s = -reach + 2 * reach * k / steps
                x, y = cx + vx * t + ux * s, cy + vy * t + uy * s
                off = bend * math.sin(s / r.uniform(2.4, 3.2) + t * 0.41 + family * 1.7 + phase) + kink * s
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


def junction(rb, scale):
    """A roundabout's footprint at a scale: ring, oval, rounded square or star."""
    x, y, r = rb["at"][0], rb["at"][1], rb["r"] * scale
    shape = rb.get("shape", "ring")
    if shape == "oval":
        return affinity.rotate(affinity.scale(Point(x, y).buffer(1, 24), r * 1.5, r * 0.8), 25)
    if shape == "square":
        return box(x - r * 0.8, y - r * 0.8, x + r * 0.8, y + r * 0.8).buffer(r * 0.25, quad_segs=6)
    return Point(x, y).buffer(r, 28)


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
        # Plinth, pedestal, the robe narrowing to the waist, shoulders, head —
        # and the arm raised with the scales, the sword held low at her side.
        return [(square(x, y, 0.62), 0, 8), (square(x, y, 0.36), 8, 22), (disc(x, y, 0.13), 22, 30),
                (disc(x, y, 0.1), 30, 38), (rect(x, y, 0.2, 0.09), 38, 41), (disc(x, y, 0.045), 41, 45),
                (rect(x + 0.1, y, 0.035, 0.035), 36, 52), (rect(x + 0.1, y, 0.12, 0.03), 52, 53.5),
                (rect(x - 0.11, y + 0.02, 0.025, 0.025), 24, 37)]
    if name == "Cape Carmine Lighthouse":
        return [(disc(x, y, 0.15), 0, 6), (disc(x, y, 0.08), 6, 34), (disc(x, y, 0.11), 34, 39)]
    if name == "Iceberg Lounge":
        return [(disc(x, y, 0.5), 0, 8), (disc(x, y, 0.38), 8, 16), (disc(x, y, 0.22), 16, 25)]
    if name == "Gotham Opera House":
        # A hall with its fly tower and a colonnaded front, not a cake.
        return [(rect(x, y + 0.1, 1.1, 0.7), 0, 22), (rect(x, y - 0.05, 0.6, 0.42), 22, 38),
                (rect(x, y + 0.55, 1.2, 0.18), 0, 14)]
    if name == "Wayne Manor":
        # The house: a long main range, two wings, a tower at the centre, the
        # conservatory and the garages off to one side.
        return [(rect(x, y, 1.5, 0.42), 0, 16), (rect(x - 0.62, y + 0.36, 0.34, 0.9), 0, 14),
                (rect(x + 0.62, y + 0.36, 0.34, 0.9), 0, 14), (square(x, y - 0.05, 0.3), 16, 26),
                (rect(x + 1.25, y + 0.2, 0.55, 0.3), 0, 6), (rect(x - 1.1, y - 0.55, 0.6, 0.26), 0, 5)]
    if name == "Drake Manor":
        return [(rect(x, y, 0.9, 0.36), 0, 12), (rect(x + 0.35, y + 0.3, 0.3, 0.5), 0, 10)]
    if name == "Falcone Estate":
        return [(rect(x, y, 1.0, 0.5), 0, 13), (square(x - 0.55, y, 0.28), 0, 17),
                (box(x - 1.2, y - 0.9, x + 1.2, y + 0.9).difference(box(x - 1.12, y - 0.82, x + 1.12, y + 0.82)), 0, 3)]
    if name == "Gotham Stock Exchange":
        return [(rect(x, y, 0.9, 0.62), 0, 34), (rect(x, y + 0.38, 0.9, 0.14), 0, 26)]
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
    if name == "The Funhouse":
        # Squat and wide, with a pointed turret over the clown's-mouth door.
        return [(rect(x, y, 0.95, 0.5), 0, 13), (rect(x - 0.12, y, 0.55, 0.36), 13, 18),
                (square(x + 0.32, y, 0.2), 0, 26), (square(x + 0.32, y, 0.09), 26, 32)]
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
    lakes = [ellipse(lake["at"], lake["rx"], lake["ry"], 300 + i) for i, lake in enumerate(src["lakes"])]
    water_cut = unary_union(rivers + lakes)

    # Coasts with coves and headlands, not smooth outlines; the mainland rougher still.
    island = landmass(src["coast"], 1, amp=0.8, levels=3)
    others = {i["name"]: landmass(i["coast"], 10 + n, amp=0.45, levels=3) for n, i in enumerate(src["islands"])}
    mainland = {m["name"]: landmass(m["coast"], 20 + n, amp=1.3, levels=3) for n, m in enumerate(src["mainland"])}
    island_land = island.difference(water_cut)
    land = unary_union([island_land] + [g.difference(water_cut) for g in others.values()]
                       + [g.difference(water_cut) for g in mainland.values()])
    for name, geom in [("Gotham", island_land), *others.items(), *mainland.items()]:
        add(geom.difference(water_cut), "land", n=name, k="mainland" if name in mainland else "island")
    # Rivers and lakes are where the land isn't: no outline of their own to
    # cross the mouth of a river. Their names go on as labels.
    for lake, spec in zip(lakes, src["lakes"], strict=True):
        if spec["name"]:
            c = lake.representative_point()
            add(Point(c.x, c.y), "water_label", n=spec["name"], r=0, s=1)

    parks = [Polygon(chaikin(roughen(p["coast"], 400 + i, amp=0.35), 3)).buffer(0)
             for i, p in enumerate(src["parks"])]
    park_names = [p["name"] for p in src["parks"]]

    # Districts: the cells around their centres, clipped to the land they're on.
    ds = src["districts"]
    island_ds = [d for d in ds if "limit" not in d and "city" not in d]
    island_names = {d["name"] for d in island_ds}
    areas = organic_cells(island_ds, island_land, 31)
    # Arkham Island is the piece of land its asylum stands on, all of it.
    seat = Point(island_ds[[d["name"] for d in island_ds].index("Arkham Island")]["at"])
    arkham = next(p for p in polys_of(island_land) if p.buffer(0.01).contains(seat))
    for name in list(areas):
        if name != "Arkham Island":
            areas[name] = areas[name].difference(arkham)
    stray = areas["Arkham Island"].difference(arkham)
    areas["Arkham Island"] = arkham
    if not stray.is_empty:
        nearest = min((d for d in island_ds if d["name"] != "Arkham Island"),
                      key=lambda d: Point(d["at"]).distance(stray.centroid))
        areas[nearest["name"]] = areas[nearest["name"]].union(stray)
    def to_the_shore(edge, host):
        """A town runs down to its own waterfront: the stretch of shore beside it is its."""
        band = host.difference(host.buffer(-3.2))
        grown = edge.union(band.intersection(edge.buffer(3.2)))
        return grown.buffer(0.8).buffer(-0.8).intersection(host)

    blued = [d for d in ds if d.get("city") == "Blüdhaven"]
    # Blüdhaven spreads along its shore and frays at the edges, like Burnside.
    limits = to_the_shore(Polygon(chaikin(roughen(src["bluedhaven_limits"], 33, amp=1.6, levels=3), 3)).buffer(0),
                          mainland["Gotham County"]).difference(water_cut)
    areas.update(organic_cells(blued, limits, 37))
    for i, d in enumerate(ds):
        if "limit" in d:
            host = mainland["Burnside"] if d["name"] == "Burnside" else mainland["Gotham County"]
            # A town grows along the shore and thins at its edges: never a stamped shape.
            edge = Polygon(chaikin(roughen(d["limit"], 60 + i, amp=1.0, levels=3), 3)).buffer(0)
            areas[d["name"]] = to_the_shore(edge, host).difference(water_cut)
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
    shapes = ["ring", "star", "oval", "square", "star"]
    for i, rb in enumerate(src.get("roundabouts", [])):
        rb.setdefault("shape", shapes[i % len(shapes)])
        parks.append(junction(rb, 0.7))
        park_names.append("")
    plazas = []
    pr = random.Random(91)
    for i, (name, area) in enumerate(areas.items()):
        if name not in island_names or name in SPARSE:
            continue
        minx, miny, maxx, maxy = area.bounds
        for k in range(pr.randint(1, 3)):
            for _ in range(12):
                x, y = pr.uniform(minx, maxx), pr.uniform(miny, maxy)
                spot = blob(x, y, pr.uniform(0.35, 0.95), 700 + i * 10 + k)
                if area.buffer(-0.4).contains(spot) and not spot.intersects(unary_union(parks)):
                    if pr.random() < 0.35:
                        plazas.append(spot)
                    else:
                        parks.append(spot)
                        park_names.append("")
                    break
    parks.append(Polygon(chaikin(roughen(src["estate"], 88, amp=0.6, levels=2), 3)).buffer(0).difference(water_cut))
    park_names.append("Wayne Estate")
    park_union = unary_union(parks)
    for p, name in zip(parks, park_names, strict=True):
        add(p.difference(water_cut), "park", n=name)       # a lake in a park is water, not lawn
    for p in plazas:
        add(p, "plaza")
    # Ponds in the bigger parks — never under a landmark.
    standing = unary_union([t[0] for pl in src["places"] for t in landmark_shapes(pl["name"], pl["x"], pl["y"])]
                           or [Point(0, 0)]).buffer(0.5)
    for i, p in enumerate(parks[:2]):
        c = p.representative_point()
        for k in range(2):
            pond = blob(c.x + pr.uniform(-2, 2), c.y + pr.uniform(-3, 3), pr.uniform(0.3, 0.6), 900 + i * 5 + k)
            if p.buffer(-0.3).contains(pond) and not pond.intersects(water_cut) and not pond.intersects(standing):
                add(pond, "water", n="")
    plaza_union = unary_union(plazas)

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
    secondary = [LineString(chaikin(roughen(list(s.coords), 50 + i, amp=0.22, closed=False), 2, closed=False))
                 .simplify(0.03) for i, s in enumerate(lines_of(shapely.line_merge(borders))) if s.length > 0.6]

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
        named.append((LineString(junction(rb, 1.0).exterior.coords), "primary", rb["name"]))
        if rb["shape"] == "star":
            # A star: avenues leaving the circle like spokes.
            for k in range(7):
                a = k * 2 * math.pi / 7 + 0.3
                x0, y0 = rb["at"][0] + math.cos(a) * rb["r"], rb["at"][1] + math.sin(a) * rb["r"]
                spoke = LineString([(x0, y0), (rb["at"][0] + math.cos(a) * rb["r"] * 3.4,
                                               rb["at"][1] + math.sin(a) * rb["r"] * 3.4)])
                named.append((spoke.intersection(land), "secondary", ""))
    # Rail runs under the parks, not across them.
    named = [(p, c, n) for ln, c, n in named
             for p in (lines_of(ln.difference(park_union)) if c == "rail" else [ln]) if p.length > 0.2]
    # A grid street that would run alongside an avenue for a stretch gives way
    # to it: two roads drawn on top of each other read as a mess, not a city.
    avenues = unary_union([ln for ln, c, _ in named if c in ("primary", "highway")] + secondary).buffer(0.32)
    kept = []
    for s_, c in streets:
        inside = s_.intersection(avenues)
        alongside = unary_union([p.buffer(0.05) for p in lines_of(inside) if p.length > 0.55])
        for p in lines_of(s_.difference(alongside) if not alongside.is_empty else s_):
            if p.length > 0.25:
                kept.append((p, c))
    streets = kept
    # Gotham's one beach, under Amusement Mile: sand along the open shore (built on below).
    fun = src.get("funfair")
    sand = Point(0, 0).buffer(0)
    if fun:
        frame = Polygon(fun["beach"])
        sand = island_land.difference(island_land.buffer(-fun["depth"])).intersection(frame)
        sand = sand.buffer(0.15).buffer(-0.15)
    # Nothing drives straight across a roundabout: everything meets the ring.
    holes = unary_union([junction(rb, 0.97) for rb in src.get("roundabouts", [])] + [sand.buffer(0.05)])
    streets = [(p, c) for s, c in streets for p in lines_of(s.difference(holes).difference(plaza_union))
               if p.length > 0.2]
    secondary = [p for s in secondary for p in lines_of(s.difference(holes)) if p.length > 0.2]
    named = [(p, c, n) for ln, c, n in named
             for p in (lines_of(ln.difference(holes)) if n not in [rb["name"] for rb in src.get("roundabouts", [])]
                       else [ln]) if p.length > 0.15]
    bridges = [(span(curve(b["line"], 2), land), b["name"]) for b in src["bridges"]]
    bridges = [(line, name) for line, name in bridges if line is not None]

    # The sprawl: past the towns the mainland goes on — lanes and low houses
    # thinning out into the dark, a city that doesn't stop at its limits.
    towns = {n: a for n, a in areas.items() if n in outer}
    town_union = unary_union(list(towns.values()))
    open_land = unary_union([m.difference(water_cut) for m in mainland.values()])
    airfield = Point(0, 0).buffer(0)
    if src.get("airport"):
        airfield = unary_union([Polygon(src["airport"]["apron"]).buffer(1.2)]
                               + [LineString(rw).buffer(1.4) for rw in src["airport"]["runways"]])
    fringe = (open_land.intersection(town_union.buffer(7.5)).difference(town_union.buffer(0.15))
              .difference(park_union).difference(airfield))
    lanes, lr, claimed = [], random.Random(1500), Point(0, 0).buffer(0)
    for i, (name, area) in enumerate(towns.items()):
        patch = fringe.intersection(area.buffer(7.5)).difference(claimed)
        claimed = claimed.union(patch)
        g = grid_of[name]
        for line, _c in street_grid(patch, g["angle"] + lr.uniform(-25, 25), 2.3, 0.7, 1600 + i):
            for part in lines_of(line):
                far = part.centroid.distance(town_union)
                if lr.random() < 1.05 - far / 7.5:          # thinner the further out
                    lanes.append(part.simplify(0.03))

    # Each bridge's ends run on to the nearest road, so nothing dead-ends at a bank.
    network = unary_union([ln for ln, c, _ in named if c != "rail"] + secondary + [s for s, _ in streets])
    for line, _name in list(bridges):
        for end in (Point(line.coords[0]), Point(line.coords[-1])):
            near = shapely.ops.nearest_points(end, network)[1] if not network.is_empty else None
            if near is not None and 0.15 < end.distance(near) < 3.0:
                named.append((LineString([end, near]), "primary", ""))
    for s, c in streets:
        add(s, "road", c=c)
    for s in lanes:
        add(s, "road", c="lane")
    for s in secondary:
        add(s, "road", c="secondary")
    for line, cls, name in named:
        props = {"c": cls, **({"n": name} if name else {}), **({"e": 1} if name == "Gotham Skyway" else {})}
        add(line, "road", **props)
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

    # Container yards at the docks: their own ground, boxes stacked in rows.
    yards = []
    for i, yard in enumerate(src.get("yards", [])):
        ground = blob(yard["at"][0], yard["at"][1], yard["r"], 1100 + i).intersection(land).difference(water_cut)
        if ground.is_empty:
            continue
        yards.append(ground)
        add(ground, "yard")
        yr = random.Random(1200 + i)
        minx, miny, maxx, maxy = ground.bounds
        y = miny + 0.2
        while y < maxy:
            x = minx + 0.2
            while x < maxx:
                box_ = box(x, y, x + 0.34, y + 0.11)
                if ground.buffer(-0.08).contains(box_) and yr.random() < 0.8:
                    building(box_, yr.choice((3, 3, 6, 6, 9)), "~container")
                x += 0.4
            y += 0.2 + (0.35 if yr.random() < 0.18 else 0)       # the odd lane between rows
    yard_union = unary_union(yards) if yards else Point(0, 0).buffer(0)
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
    # Amusement Mile: Gotham's one beach along the open shore, the boardwalk
    # behind it, and on the Mile the big wheel and the old wooden coaster —
    # built in the air, so in 3D the wheel stands up and the coaster climbs.
    if fun:
        add(sand, "beach")
        # The boardwalk: the sand's landward edge, not its ends or the waterline.
        walk = sand.boundary.difference(island_land.boundary.buffer(0.12)).difference(frame.boundary.buffer(0.12))
        for part in lines_of(shapely.line_merge(walk) if walk.geom_type == "MultiLineString" else walk):
            if part.length > 0.6:
                add(part.simplify(0.02), "boardwalk")
        wx, wy = fun["wheel"]
        axis = math.radians(fun.get("wheel_axis", 0))
        hub, radius = 36, 30                 # metres: hub height, rim radius
        across = radius / 150                # map units (one is about 150 m)
        for k in range(40):
            a = k * 2 * math.pi / 40
            cx, cy = wx + math.cos(axis) * math.cos(a) * across, wy + math.sin(axis) * math.cos(a) * across
            z = hub + math.sin(a) * radius
            if k % 4 == 0:
                building(square(cx, cy, 0.034), z + 3, "~ride", base=z - 3)      # a gondola
            else:
                building(square(cx, cy, 0.016), z + 0.9, "~wheel", base=z - 0.9)  # the rim
        building(square(wx, wy, 0.03), hub + 2, "~wheel")                         # the tower
        building(disc(wx, wy, 0.045), hub + 2.5, "~ride", base=hub - 2.5)         # the hub
        ox, oy = fun["coaster"]
        track = []
        for k in range(120):
            t = k * 2 * math.pi / 120
            track.append((ox + 1.35 * math.cos(t) + 0.32 * math.cos(3 * t), oy + 0.62 * math.sin(t) + 0.16 * math.sin(2 * t)))
        for k in range(120):
            t = k * 2 * math.pi / 120
            z = 7 + 16 * max(0.0, math.sin(2 * t + 0.4)) + 9 * max(0.0, math.sin(3 * t - 1.1))   # hills and drops
            seg = LineString([track[k], track[(k + 1) % 120]]).buffer(0.014, cap_style="flat")
            building(seg, z + 0.8, "~ride", base=z - 0.8)
            if k % 6 == 0:
                building(square(track[k][0], track[k][1], 0.012), z - 0.8, "~wheel")    # a trestle
        fair = unary_union([LineString(track + [track[0]]).buffer(0.35), Point(wx, wy).buffer(0.38),
                            Point(*fun["funhouse"]).buffer(0.6)]).buffer(0.25).buffer(-0.25)
        add(fair.difference(sand), "fair")
        reserved += [fair.buffer(0.15), sand.buffer(0.1)]      # nothing built on the sand

    for line in elevated:
        # The skyway stands on piers above the street: a deck in the air.
        for part in lines_of(line.intersection(land)):
            building(part.buffer(0.17, cap_style="flat"), 19, "~deck", base=15)

    # Buildings: the blocks between the streets, cut into lots, given heights —
    # skyscrapers clustering in more than one place, as in any real city.
    cuts = unary_union([s.buffer(0.12 if c == "avenue" else 0.085, quad_segs=2) for s, c in streets]
                       + [s.buffer(0.15, quad_segs=2) for s in secondary]
                       + [ln.buffer(0.24 if c == "highway" else 0.2 if c == "primary" else 0.15, quad_segs=2)
                          for ln, c, _ in named]
                       + [ln.buffer(0.22) for ln, _ in bridges] + reserved + [plaza_union, yard_union])
    cores = src.get("cores", []) + [[d["at"][0], d["at"][1], 0.55, 2.6] for d in ds]
    count = 0
    lots = []           # (district, centroid, height, area) — for venues and rooftops
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
            most = 1.6 if g.get("industrial") else 0.6 if g["tall"] >= 30 else 0.45
            for lot in subdivide(blk, most, r):
                foot = lot.buffer(-0.06, join_style="mitre")
                for piece in polys_of(foot):
                    if piece.area < 0.05:
                        continue
                    c = piece.centroid
                    pull = 1 + sum(k * math.exp(-((c.x - cx) ** 2 + (c.y - cy) ** 2) / (rad * rad))
                                   for cx, cy, k, rad in cores)
                    h = g["tall"] * r.lognormvariate(0, 0.5) * pull
                    if r.random() < 0.05 and not g.get("industrial"):
                        h *= r.uniform(2.0, 3.6)            # the odd tower, anywhere
                    if g.get("industrial"):
                        h = r.choice((7, 9, 11, 14))        # sheds and works, not towers
                    # Some windows lit, some dark: the city at night.
                    h = round(max(4, min(260, h)))
                    building(piece.simplify(0.04), h, "~lit" if r.random() < 0.14 else "")
                    lots.append((name, piece.centroid, h, piece.area))
                    count += 1

    # Houses along the lanes out in the sprawl: low, scattered, fewer the further out.
    hr = random.Random(1700)
    keep_clear = unary_union([cuts, park_union.buffer(0.1), water_cut.buffer(0.15), airfield,
                              unary_union(lanes).buffer(0.1) if lanes else Point(0, 0).buffer(0)])
    houses = 0
    for lane in lanes:
        far = lane.centroid.distance(town_union)
        pos = hr.uniform(0.1, 0.4)
        while pos < lane.length - 0.2:
            a, b = lane.interpolate(pos), lane.interpolate(min(lane.length, pos + 0.05))
            angle = math.degrees(math.atan2(b.y - a.y, b.x - a.x))
            for side in (1, -1):
                if hr.random() > 0.75 - far / 12:
                    continue
                nx, ny = -math.sin(math.radians(angle)) * side, math.cos(math.radians(angle)) * side
                c = Point(a.x + nx * 0.27, a.y + ny * 0.27)
                w, d_ = hr.uniform(0.16, 0.26), hr.uniform(0.12, 0.18)
                house = affinity.rotate(box(c.x - w / 2, c.y - d_ / 2, c.x + w / 2, c.y + d_ / 2), angle, origin=c)
                if open_land.contains(house) and not house.intersects(keep_clear):
                    building(house, hr.choice((4, 4, 5, 6, 7, 9)), "~lit" if hr.random() < 0.18 else "")
                    houses += 1
            pos += hr.uniform(0.32, 0.55)

    # On the roofs: water towers on the mid-rises, helipads on the tallest.
    rr = random.Random(1300)
    for _d, c, h, area in lots:
        if 14 <= h <= 70 and area > 0.08 and rr.random() < 0.07:
            building(disc(c.x + rr.uniform(-0.05, 0.05), c.y + rr.uniform(-0.05, 0.05), 0.055), h + 7, "~tank", base=h)
    for _d, c, h, area in sorted(lots, key=lambda lot: -lot[2])[:36]:
        if area > 0.12:
            building(disc(c.x, c.y, 0.17), h + 0.8, "~pad", base=h)
    # Building sites with their cranes, where the city is still growing.
    growing = {"New Town", "Otisburg", "Burnside", "Central Business District", "Upper East Side", "Fashion District"}
    sites = [lot for lot in lots if lot[0] in growing and lot[3] > 0.2]
    for _d, c, _h, _a in rr.sample(sites, min(9, len(sites))):
        add(disc(c.x, c.y, 0.32), "site")
        mast, jib = rr.randint(60, 95), rr.uniform(0, 180)
        building(square(c.x, c.y, 0.06), mast, "~crane")
        boom = affinity.rotate(box(c.x - 0.25, c.y - 0.025, c.x + 0.95, c.y + 0.025), jib, origin=(c.x, c.y))
        building(boom, mast - 2, "~crane", base=mast - 5)

    # The life of the place: clubs and bars where the night is, diners
    # everywhere, churches in the old quarters, fire stations, schools — each
    # with a name, on a real lot.
    venues_by = {
        "club": (["The Laughing Fish", "The Stacked Deck", "Club Noir", "The Gilded Cage", "Neon Saint", "Babylon",
                  "Low Tide", "The Rookery", "Sixth Sin", "Catacombs", "The Blue Mask", "Velvet Hour", "Static",
                  "The Gutter", "Ace of Clubs", "Smoke & Mirrors", "The Pit", "Midnight Mass", "Grin", "The Aviary"],
                 {"Crime Alley": 3, "The Bowery": 3, "Amusement Mile": 3, "Fashion District": 2, "Diamond District": 2,
                  "Old Gotham": 2, "Chinatown": 1, "Burnside": 2, "Central Business District": 2, "New Town": 1}),
        "bar": (["The Crooked Mile", "O'Malley's", "The Lantern", "Last Call", "The Drowned Man", "Kane's Tap",
                 "The Iron Stool", "Dock 9", "The Rusty Anchor", "Gin Alley", "The Nightjar", "Bleak House",
                 "The Wet Rat", "Shamrock", "The Corner Pocket", "Saint Mike's", "Copper Kettle", "The Hollow"],
                {"Crime Alley": 2, "The Bowery": 2, "Tricorner": 2, "Old Gotham": 2, "Burnley": 2, "The Narrows": 2,
                 "Robinsville": 1, "Chinatown": 1, "Waterloo Docks": 2, "Port's Park": 1, "Burnside": 2, "Ironworks": 1}),
        "diner": (["Jackie's", "Sal's All-Night", "The Midnight Diner", "Dixon's", "Ma Gunn's", "The Blue Plate",
                   "Rosie's", "Nite Owl", "Big Belly Burger", "Lucky Seven", "Harbor Grill", "Pearl's"],
                  {"Old Gotham": 1, "Crime Alley": 1, "Upper West Side": 1, "Coventry": 1, "Chinatown": 1,
                   "Burnley": 1, "Tricorner": 1, "City Hall District": 1, "Burnside": 1, "Halyard Square": 1,
                   "Kane Heights": 1, "University District": 1}),
        "church": (["St. Aidan's", "Our Lady of the Harbor", "St. Swithin's", "Holy Trinity", "St. Jude's",
                    "Grace Chapel"], {"Old Gotham": 2, "Tricorner": 1, "Coventry": 1, "Burnley": 1, "Bristol": 1}),
        "fire": (["Engine 9", "Engine 23", "Ladder 4", "Engine 41", "Ladder 17"],
                 {"Old Gotham": 1, "New Town": 1, "Upper West Side": 1, "Robinsville": 1, "Burnside": 1}),
        "school": (["Gotham Heights High", "Robinson Academy", "PS 117", "St. Mary's School", "Brentwood Prep"],
                   {"Upper West Side": 1, "Coventry": 1, "Cherry Hills": 1, "Kane Heights": 1, "Bristol": 1}),
    }
    by_district = {}
    for lot in lots:
        by_district.setdefault(lot[0], []).append(lot)
    vr = random.Random(1400)
    for kind, (names, where) in venues_by.items():
        pool = list(names)
        vr.shuffle(pool)
        for district, n in where.items():
            for _ in range(n):
                if not pool or not by_district.get(district):
                    break
                name, c, h, area = vr.choice(by_district[district])
                add(Point(round(c.x, 2), round(c.y, 2)), "venue", n=pool.pop(), k=kind, a=district)

    # Trees, through the parks.
    trees = []
    tr = random.Random(77)
    paths = unary_union([ln.buffer(0.22) for ln, c, _ in named] + [s_.buffer(0.18) for s_ in secondary]
                        + [s_.buffer(0.14) for s_, _ in streets] + [ln.buffer(0.25) for ln, _ in bridges]
                        + reserved)
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
                    trees.append([round(px * 100), round(py * 100), tr.randint(7, 19), tr.randint(6, 13)])
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
