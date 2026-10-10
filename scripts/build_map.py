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
import os
import random
from pathlib import Path

import shapely
import shapely.ops
import shapely.prepared
from shapely import affinity
from shapely.geometry import LineString, MultiPoint, Point, Polygon, box, mapping
from shapely.ops import split, substring, unary_union

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "wayne" / "engine" / "gotham.json"
OUT = ROOT / "web" / "map" / "gotham.geojson"
# Buildings go in a file of their own, compact — [height, x, y, x, y, ...] in
# hundredths — and load after the city, so the first look is quick.
BUILDINGS = ROOT / "web" / "map" / "gotham-buildings.json"
# The streets as a network, for the server: how someone gets from one place to
# another — by road, over the bridges, on a ferry — instead of jumping there.
ROADS = ROOT / "wayne" / "engine" / "roads.json"

# Districts with docks and wharves along them keep their edge hard; elsewhere a
# strip of waterfront park runs along the shore.
NO_WATERFRONT = {"Tricorner", "Amusement Mile", "Chinatown", "City Hall District", "Robinsville"}
# Districts with almost nothing built: parkland, asylum grounds, an abandoned funfair.
SPARSE = {"Arkham Island", "Paris Island", "Blackgate Isle"}
# What a district's buildings are like, beyond how tall: the old quarters'
# brownstone and stone, downtown's glass, the works — and the suburbs' houses.
OLD_QUARTERS = {"Old Gotham", "Tricorner", "Crime Alley", "The Bowery", "Burnley", "Robinsville", "Chinatown",
                "The Narrows", "Coventry", "Cherry Hills", "Amusement Mile", "Fort Joseph", "Waterloo Docks",
                "Port's Park", "Burnside", "University District"}
GLASS = {"Diamond District", "Fashion District", "City Hall District", "Upper East Side", "New Town", "Otisburg",
         "Central Business District", "Halyard Square", "Upper West Side"}
SUBURBS = {"Bristol", "Kane Heights"}
# Where the city's still growing: building sites with their cranes.
GROWING = {"New Town", "Otisburg", "Burnside", "Central Business District", "Upper East Side", "Fashion District"}


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


def footprint_at(p):
    """Where a place's building stands — its own point, unless it gives another ("at")."""
    return p.get("at", [p["x"], p["y"]])


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
    if name == "GCPD Central":
        return [(rect(x, y, 0.95, 0.72), 0, 58), (square(x + 0.2, y - 0.1, 0.3), 58, 67)]
    if name == "City Hall":
        return [(rect(x, y, 1.15, 0.72), 0, 22), (disc(x, y, 0.3), 22, 30), (disc(x, y, 0.26), 30, 35),
                (disc(x, y, 0.19), 35, 39), (disc(x, y, 0.1), 39, 43), (disc(x, y, 0.03), 43, 50)]
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
    if name == "Gotham Observatory":
        return [(disc(x, y, 0.3), 0, 10), (disc(x, y, 0.25), 10, 16), (disc(x, y, 0.15), 16, 20)]
    if name == "The Funhouse":
        # Squat and wide, with a pointed turret over the clown's-mouth door.
        return [(rect(x, y, 0.95, 0.5), 0, 13), (rect(x - 0.12, y, 0.55, 0.36), 13, 18),
                (square(x + 0.32, y, 0.2), 0, 26), (square(x + 0.32, y, 0.09), 26, 32)]
    if name == "Monarch Theatre":
        return [(rect(x, y, 0.72, 0.46), 0, 17)]
    complex_ = COMPLEXES.get(name)
    if complex_:
        return [(g, base, top) for g, base, top, _kind in complex_(x, y)["tiers"]]
    return []


def landmark_kinds(name, x, y):
    """The style of each of a complex's tiers, in step with landmark_shapes — glass, works, a tank..."""
    complex_ = COMPLEXES.get(name)
    return [kind for _g, _b, _t, kind in complex_(x, y)["tiers"]] if complex_ else None


def landmark_grounds(name, x, y):
    """A complex's own ground: a campus's lawns and paths, a plant's yard, a stadium's car parks."""
    complex_ = COMPLEXES.get(name)
    return complex_(x, y).get("ground", []) if complex_ else []


def _local(x, y, angle):
    """Shapes drawn about (0, 0) in units, placed at (x, y) and turned to the district's grid."""
    def place(g):
        return affinity.rotate(affinity.translate(g, x, y), angle, origin=(x, y))
    return place


def gotham_university(x, y):
    """
    Gotham University on its hill, fronting University Avenue: the old main
    quad — stone ranges round a lawn crossed by its paths, a gate tower, the
    domed library at its head and the bell tower — then the glass science quad,
    the dorms, the student union, and the stadium with its track.
    """
    at = _local(x, y + 0.4, -2.3)            # square to the avenue it fronts
    tiers = [(rect(0, -0.78, 1.7, 0.24), 0, 20, "~old"), (square(0, -0.78, 0.22), 0, 34, "~old"),
             (rect(0, 0.78, 1.7, 0.24), 0, 18, "~old"), (rect(-1.08, 0, 0.24, 1.3), 0, 18, "~old"),
             (rect(1.15, 0, 0.44, 0.92), 0, 22, "~old"), (disc(1.15, 0, 0.21), 22, 30, "~landmark"),
             (disc(1.15, 0, 0.15), 30, 35, "~landmark"), (disc(1.15, 0, 0.07), 35, 39, "~landmark"),
             (square(0.95, -0.8, 0.15), 0, 56, "~old"), (square(0.95, -0.8, 0.07), 56, 67, "~landmark"),
             (rect(2.15, -0.72, 1.0, 0.22), 0, 34, "~glass"), (rect(2.15, 0.72, 1.0, 0.22), 0, 28, "~glass"),
             (rect(2.88, 0, 0.26, 0.92), 0, 42, "~glass"), (rect(0.6, 1.42, 0.62, 0.34), 0, 14, "")]
    tiers += [(rect(-1.6 + 0.55 * i, 1.48, 0.42, 0.22), 0, 26, "~old") for i in range(4)]
    tiers += [(rect(-2.86, 0.2, 0.12, 0.95), 0, 9, ""), (rect(-1.94, 0.2, 0.12, 0.95), 0, 9, "")]
    lawns = [box(-0.85, -0.55, 0.85, 0.55), box(1.6, -0.5, 2.62, 0.5), box(-1.4, 1.05, 0.15, 1.25)]
    paths = [LineString([(-0.85, -0.55), (0.85, 0.55)]), LineString([(-0.85, 0.55), (0.85, -0.55)]),
             LineString([(0, -0.66), (0, 0.66)]), LineString([(-0.96, 0), (0.92, 0)]),
             LineString([(1.38, 0), (2.74, 0)])]
    ground = [(at(lw), "campus") for lw in lawns] + [(at(pt), "path") for pt in paths]
    ground += [(at(g), "pitch:" + k) for g, k in pitch("track", -2.4, 0.2, 90)]
    return {"tiers": [(at(g), b, t, k) for g, b, t, k in tiers], "ground": ground,
            "lawn": unary_union([at(lw) for lw in lawns])}


def ace_chemicals(x, y):
    """
    Ace Chemicals on the Ironworks shore: the process hall and the office with
    the sign on it, the open vats with their catwalks, the tank farm, cracking
    towers, two stacks, warehouses — and the pipe rack out to the outfall in
    the river.
    """
    at = _local(x, y, -4)
    tiers = [(rect(-0.9, 0.2, 1.4, 0.7), 0, 24, "~works"), (rect(-1.0, -0.66, 0.9, 0.3), 0, 18, "~works"),
             (rect(-1.0, -0.66, 0.84, 0.04), 18, 25, "~sign")]
    tiers += [(disc(0.5 + 0.36 * i, -0.75 + 0.36 * j, 0.13), 0, 9, "~vat") for i in range(3) for j in range(2)]
    tiers += [(disc(1.7 + 0.56 * i, -0.92 + 0.56 * j, 0.24), 0, 19, "~tank") for i in range(2) for j in range(2)]
    tiers += [(disc(0.62, 0.66, 0.075), 0, 48, "~works"), (disc(0.92, 0.66, 0.075), 0, 44, "~works"),
              (disc(1.22, 0.66, 0.045), 0, 38, "~works"), (disc(-1.62, 0.62, 0.062), 0, 76, "~stack"),
              (disc(-1.3, 0.94, 0.052), 0, 66, "~stack")]
    for a, b in (((-0.18, -0.39), (1.38, -0.39)), ((0.5, -1.02), (0.5, -0.1)), ((1.22, -1.02), (1.22, -0.1)),
                 ((0.86, -1.02), (0.86, 0.3))):
        tiers.append((LineString([a, b]).buffer(0.018, cap_style="flat"), 10, 11, "~deck"))
    # The pipe rack runs on over the expressway to the outfall at the river's edge.
    tiers += [(LineString([(0.1, 1.12), (6.2, 1.12)]).buffer(0.035, cap_style="flat"), 6, 7.5, "~deck"),
              (rect(-1.85, -0.3, 0.6, 0.9), 0, 12, "~works"), (rect(2.1, 0.62, 0.8, 0.45), 0, 10, "~works")]
    yard = box(-2.25, -1.45, 2.6, 1.45)
    return {"tiers": [(at(g), b, t, k) for g, b, t, k in tiers], "ground": [(at(yard), "works")]}


def star_labs(x, y):
    """S.T.A.R. Labs: the round main building under its glass dome, the ring round it, two lab wings, dishes."""
    at = _local(x, y, 6)
    tiers = [(disc(0, 0, 0.5).difference(disc(0, 0, 0.21)), 0, 24, "~glass"), (disc(0, 0, 0.21), 0, 31, "~landmark"),
             (disc(0, 0, 0.86).difference(disc(0, 0, 0.79)), 6, 8, "~deck"),
             (rect(-1.0, 0.05, 0.62, 0.36), 0, 18, "~glass"), (rect(1.02, 0.3, 0.5, 0.42), 0, 14, "~glass"),
             (disc(0.22, -0.3, 0.07), 24, 26.5, "~pad"), (disc(-0.25, 0.28, 0.06), 24, 26, "~pad")]
    return {"tiers": [(at(g), b, t, k) for g, b, t, k in tiers], "ground": [(at(disc(0, 0, 1.15)), "plaza")]}


def hospital(x, y, angle, tower, scale=1.0):
    """A city hospital: the tower with a helipad on its roof, two wings, the ER and its bay, the garage."""
    at = _local(x, y, angle)
    k = scale
    tiers = [(rect(0, 0, 0.72 * k, 0.36 * k), 0, tower, ""), (disc(0, 0, 0.13), tower, tower + 0.8, "~pad"),
             (rect(-0.56 * k, 0.22 * k, 0.3 * k, 0.9 * k), 0, tower * 0.52, ""),
             (rect(0.56 * k, 0.22 * k, 0.3 * k, 0.9 * k), 0, tower * 0.52, ""),
             (rect(0, 0.48 * k, 0.82 * k, 0.24 * k), 0, tower * 0.4, ""),
             (rect(0, -0.43 * k, 0.6 * k, 0.15 * k), 0, 5, "~deck"),
             (rect(1.28 * k, 0.12 * k, 0.6 * k, 0.46 * k), 0, 15, "~garage")]
    return {"tiers": [(at(g), b, t, kd) for g, b, t, kd in tiers]}


def power_station(x, y):
    """Gotham Power Station: the brick boiler house with a chimney at each corner, its turbine hall, the switchyard."""
    at = _local(x, y, -22)
    tiers = [(rect(0, 0, 1.3, 0.75), 0, 38, "~old"), (rect(0, 0.6, 1.3, 0.36), 0, 26, "~old")]
    tiers += [(disc(sx * 0.6, sy * 0.33, 0.078), 0, 98, "~stack") for sx in (-1, 1) for sy in (-1, 1)]
    return {"tiers": [(at(g), b, t, k) for g, b, t, k in tiers], "ground": [(at(box(-0.95, 0.85, 0.95, 1.55)), "works")]}


def knightsdome(x, y):
    """The Knightsdome: the bowl and its roof, the plaza round it, a car park on every side."""
    tiers = [(disc(x, y, 1.0), 0, 34, "~landmark"), (disc(x, y, 0.92), 34, 38, "~landmark"),
             (disc(x, y, 0.76), 38, 42, "~landmark"), (disc(x, y, 0.55), 42, 45, "~landmark"),
             (disc(x, y, 0.3), 45, 47, "~landmark")]
    ring = disc(x, y, 2.15).difference(disc(x, y, 1.3))
    aisles = unary_union([rect(x, y, 4.6, 0.2), rect(x, y, 0.2, 4.6)])
    lots = [g for g in polys_of(ring.difference(aisles)) if g.area > 0.3]
    return {"tiers": tiers, "ground": [(disc(x, y, 1.3), "plaza")] + [(g, "lot") for g in lots]}


def union_station(x, y):
    """Union Station: the hall, its train shed behind, and the garage."""
    at = _local(x, y, 0)
    tiers = [(rect(0, 0, 1.45, 0.56), 0, 20, "~old"), (rect(0, 0, 1.45, 0.3), 20, 27, "~old"),
             (rect(0, 0.62, 1.7, 0.62), 0, 14, "~deck"), (rect(-1.25, 0.75, 0.6, 0.5), 0, 18, "~garage")]
    return {"tiers": [(at(g), b, t, k) for g, b, t, k in tiers]}


def _tiers(at, tiers):
    return [(at(g), b, t, k) for g, b, t, k in tiers]


def gothic_church(x, y, angle, nave=0.95, tower=46, spire=62):
    """Nave, transept, the tower at the west end and its spire."""
    at = _local(x, y, angle)
    return {"tiers": _tiers(at, [(rect(0, 0, nave, 0.32), 0, 22, "~old"), (rect(0.15, 0, 0.3, 0.7), 0, 22, "~old"),
                                 (square(-nave / 2 + 0.05, 0, 0.18), 0, tower, "~old"),
                                 (square(-nave / 2 + 0.05, 0, 0.08), tower, spire, "~landmark")])}


def steeple_church(x, y, angle):
    """A colonial meeting house: a plain hall and a tall white steeple in stages."""
    at = _local(x, y, angle)
    return {"tiers": _tiers(at, [(rect(0, 0, 0.7, 0.32), 0, 16, "~old"), (square(-0.42, 0, 0.14), 0, 30, "~old"),
                                 (square(-0.42, 0, 0.09), 30, 42, "~landmark"), (square(-0.42, 0, 0.04), 42, 58, "~landmark")])}


def wheel(x, y, axis_deg, hub, radius, spokes=28):
    """A big wheel standing up: rim, gondolas, hub and tower, in tiers in the air."""
    axis = math.radians(axis_deg)
    across = radius / 150
    out = []
    for k in range(spokes):
        a = k * 2 * math.pi / spokes
        cx, cy = x + math.cos(axis) * math.cos(a) * across, y + math.sin(axis) * math.cos(a) * across
        z = hub + math.sin(a) * radius
        out.append((square(cx, cy, 0.03 if k % 4 == 0 else 0.015), z - (3 if k % 4 == 0 else 0.9),
                    z + (3 if k % 4 == 0 else 0.9), "~ride" if k % 4 == 0 else "~wheel"))
    return out + [(square(x, y, 0.028), 0, hub + 2, "~wheel"), (disc(x, y, 0.04), hub - 2, hub + 2, "~ride")]


def star(cx, cy, outer, inner, points=5, turn=0.0):
    pts = []
    for k in range(points * 2):
        r = outer if k % 2 == 0 else inner
        a = turn + k * math.pi / points
        pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    return Polygon(pts)


def gotham_academy(x, y):
    """Gotham Academy on the Coventry bank: the main hall and its clock tower, the chapel, the cloister round its garth."""
    at = _local(x, y, 0)
    cloister = rect(-0.5, 0.62, 0.9, 0.7).difference(rect(-0.5, 0.62, 0.6, 0.4))
    return {"tiers": _tiers(at, [(rect(0, 0, 1.3, 0.36), 0, 24, "~old"), (square(0, -0.04, 0.22), 0, 44, "~old"),
                                 (square(0, -0.04, 0.1), 44, 57, "~landmark"), (rect(0.85, 0.48, 0.32, 0.62), 0, 20, "~old"),
                                 (square(0.85, 0.2, 0.07), 20, 31, "~landmark"), (cloister, 0, 12, "~old")]),
            "ground": [(at(rect(-0.5, 0.62, 0.58, 0.38)), "campus")]}


def brentwood_academy(x, y):
    """Brentwood Academy: brick ranges round a quad, and the chapel with its spire."""
    at = _local(x, y, 10)
    quad = rect(0, 0, 1.0, 0.72).difference(rect(0, 0, 0.7, 0.46))
    return {"tiers": _tiers(at, [(quad, 0, 16, "~old"), (rect(0.76, -0.1, 0.26, 0.5), 0, 18, "~old"),
                                 (square(0.76, -0.3, 0.07), 18, 34, "~landmark")]),
            "ground": [(at(rect(0, 0, 0.68, 0.44)), "campus")]}


def burnside_college(x, y):
    """Burnside College in the old textile mills: three long brick mills and the mill chimney."""
    at = _local(x, y, 4)
    return {"tiers": _tiers(at, [(rect(0, -0.46, 1.4, 0.26), 0, 22, "~old"), (rect(0, 0, 1.4, 0.26), 0, 18, "~old"),
                                 (rect(0, 0.46, 1.4, 0.26), 0, 20, "~old"), (disc(0.86, -0.12, 0.05), 0, 42, "~stack")])}


def fort_dumas(x, y):
    """Fort Dumas: the star fort the Statue of Justice stands on — its walls, and the casemates' lawn inside."""
    walls = star(x, y, 0.8, 0.5, 5, 0.3)
    return {"tiers": [(walls.difference(walls.buffer(-0.12)), 0, 7, "~old")],
            "ground": [(walls.buffer(-0.12).difference(disc(x, y, 0.34)), "campus")]}


def little_paris(x, y):
    """Little Paris, the dead amusement park: the carousel, a small wheel, the coaster's trestles, the pavilion."""
    tiers = [(disc(x - 0.4, y + 0.1, 0.17), 0, 6, "~ride"), (disc(x - 0.4, y + 0.1, 0.1), 6, 9, "~ride"),
             (rect(x + 0.1, y - 0.45, 0.6, 0.2), 0, 8, "~old")]
    tiers += wheel(x + 0.45, y + 0.2, 70, 20, 16, 20)
    for k in range(10):
        a = k * 2 * math.pi / 10
        tiers.append((square(x + 0.05 + 0.45 * math.cos(a), y + 0.55 + 0.2 * math.sin(a), 0.012), 0,
                      6 + 6 * abs(math.sin(2 * a)), "~wheel"))
    return {"tiers": tiers}


def kane_industries(x, y):
    """Kane Industries: the brick headquarters with KANE in iron letters on the roof, and its tank farm."""
    at = _local(x, y, -25)
    tiers = [(rect(0, 0, 0.9, 0.4), 0, 22, "~old"), (rect(0, 0, 0.6, 0.03), 22, 27, "~sign")]
    tiers += [(disc(0.85 + 0.36 * (k % 3), -0.3 + 0.6 * (k // 3), 0.15), 0, 14, "~tank") for k in range(6)]
    return {"tiers": _tiers(at, tiers), "ground": [(at(box(-0.55, -0.55, 1.75, 0.55)), "works")]}


def port_adams(x, y):
    """Port Adams Container Terminal: boxes stacked in rows on the quay, gantry cranes along the water."""
    at = _local(x, y, -4)
    tiers = []
    # A long quay between the expressway and the river: stacks in rows along it,
    # the gantries on the water's edge with their booms out over the ships.
    for r in range(8):
        for c in range(3):
            tiers.append((rect(-0.42 + c * 0.38, -1.4 + r * 0.4, 0.33, 0.11), 0, (3, 6, 9, 6)[(r + c) % 4], "~container"))
    for k in range(4):
        cy = -1.2 + k * 0.8
        tiers += [(square(0.62, cy - 0.08, 0.035), 0, 38, "~crane"), (square(0.62, cy + 0.08, 0.035), 0, 38, "~crane"),
                  (rect(0.95, cy, 0.85, 0.05), 38, 42, "~crane")]
    return {"tiers": _tiers(at, tiers), "ground": [(at(box(-0.66, -1.65, 0.7, 1.6)), "works")]}


def worlds_fair(x, y):
    """The old World's Fair grounds: the steel globe on its plinth, the reflecting pool, the pavilion."""
    tiers = [(disc(x, y, 0.15), 0, 3, "~old"), (disc(x, y, 0.07), 3, 6, "~works"), (disc(x, y, 0.1), 6, 10, "~works"),
             (disc(x, y, 0.115), 10, 15, "~works"), (disc(x, y, 0.1), 15, 19, "~works"), (disc(x, y, 0.07), 19, 22, "~works"),
             (rect(x, y - 0.7, 0.8, 0.28), 0, 10, "~old")]
    return {"tiers": tiers, "ground": [(rect(x, y + 0.62, 1.1, 0.26), "pool")]}


def gotham_downs(x, y):
    """Gotham Downs: the dirt oval, the infield, the old grandstand, the stables."""
    at = _local(x, y, 8)
    outer = box(-0.75, -0.42, 0.75, 0.42).union(Point(-0.75, 0).buffer(0.42)).union(Point(0.75, 0).buffer(0.42))
    tiers = [(rect(0, -0.62, 1.2, 0.16), 0, 18, ""), (rect(0, -0.62, 1.2, 0.05), 18, 21, "~old")]
    tiers += [(rect(-0.6 + 0.4 * k, 0.66, 0.32, 0.12), 0, 6, "~old") for k in range(4)]
    return {"tiers": _tiers(at, tiers),
            "ground": [(at(outer.buffer(0.18)), "grounds"), (at(outer.difference(outer.buffer(-0.11))), "pitch:dirt"),
                       (at(outer.buffer(-0.11)), "pitch:turf")]}


def slaughter_swamp(x, y):
    """Slaughter Swamp: black water and marsh where the Kane spreads out below the heights."""
    marsh = blob(x, y, 2.0, 4100)
    ponds = [blob(x + dx, y + dy, r, 4200 + k) for k, (dx, dy, r) in
             enumerate(((-0.6, 0.3, 0.4), (0.5, -0.5, 0.32), (0.8, 0.7, 0.26), (-0.2, -0.9, 0.22), (-1.0, -0.4, 0.2)))]
    return {"tiers": [], "ground": [(marsh, "marsh")] + [(p_.intersection(marsh.buffer(-0.2)), "pool") for p_ in ponds]}


def ferry_terminal(x, y):
    """The Gotham Ferry Company's terminal: the green-iron hall and its clock tower, its slips out into the harbour."""
    return {"tiers": [(rect(x, y, 1.0, 0.42), 0, 14, "~copper"), (square(x - 0.32, y - 0.08, 0.13), 0, 27, "~copper")]}


def gotham_stadium(x, y):
    """Gotham Stadium, the Rogues' open bowl: stands round the field, a rim, four light towers, car parks."""
    at = _local(x, y, -22)
    bowl = affinity.scale(Point(0, 0).buffer(1.0), 1.25, 0.95)
    field = affinity.scale(Point(0, 0).buffer(1.0), 0.78, 0.55)
    tiers = [(bowl.difference(field), 0, 30, "~landmark"), (bowl.difference(bowl.buffer(-0.1)), 30, 35, "~landmark")]
    tiers += [(square(sx * 1.08, sy * 0.86, 0.045), 0, 58, "~works") for sx in (-1, 1) for sy in (-1, 1)]
    ring = affinity.scale(Point(0, 0).buffer(1.0), 2.0, 1.6).difference(bowl.buffer(0.12))
    lots = [g for g in polys_of(ring.difference(unary_union([rect(0, 0, 4.4, 0.2), rect(0, 0, 0.2, 3.6)]))) if g.area > 0.2]
    ground = [(at(field.buffer(-0.04)), "pitch:turf")] + [(at(g), "pitch:line") for g, k in pitch("football", 0, 0, 0)
                                                           if k == "line"]
    ground += [(at(g), "lot") for g in lots]
    return {"tiers": _tiers(at, tiers), "ground": ground}


def stagg_tower(x, y):
    """Stagg Enterprises: a glass tower with a mooring mast on its roof, for the airship."""
    at = _local(x, y, 6)
    return {"tiers": _tiers(at, [(square(0, 0, 0.46), 0, 132, "~glass"), (square(0, 0, 0.3), 132, 150, "~glass"),
                                 (disc(0, 0, 0.03), 150, 192, "~works"),
                                 (disc(0, 0, 0.13).difference(disc(0, 0, 0.1)), 184, 188, "~works")])}


COMPLEXES = {
    "Gotham Stadium": gotham_stadium,
    "Stagg Enterprises": stagg_tower,
    "Gotham University": gotham_university,
    "Ace Chemicals": ace_chemicals,
    "S.T.A.R. Labs": star_labs,
    "Gotham General Hospital": lambda x, y: hospital(x, y, 174, 72, 1.1),     # its garage away from Broadway
    "Mercy Hospital": lambda x, y: hospital(x, y, 20, 48, 0.85),
    "Gotham Power Station": power_station,
    "Knightsdome": knightsdome,
    "Union Station": union_station,
    "Gotham Academy": gotham_academy,
    "Brentwood Academy": brentwood_academy,
    "Burnside College": burnside_college,
    "Fort Dumas": fort_dumas,
    "Little Paris": little_paris,
    "Kane Industries": kane_industries,
    "Port Adams Container Terminal": port_adams,
    "World's Fair Grounds": worlds_fair,
    "Gotham Downs": gotham_downs,
    "Slaughter Swamp": slaughter_swamp,
    "Gotham Ferry Terminal": ferry_terminal,
    "Sacred Martyr Church": lambda x, y: gothic_church(x, y, -14),
    "First Church of Gotham City": lambda x, y: steeple_church(x, y, 28),
    "Our Lady of the Narrows": lambda x, y: gothic_church(x, y, 22, nave=0.7, tower=30, spire=40),
    "Narrows Monorail Station": lambda x, y: {"tiers": _tiers(_local(x, y, 22), [
        (rect(0, 0, 1.1, 0.16), 10, 12, "~deck"), (rect(0, 0, 0.8, 0.2), 12, 14.5, "~works"),
        (rect(-1.35, 0, 0.5, 0.07), 10.5, 11.5, "~deck"), (rect(1.35, 0, 0.5, 0.07), 10.5, 11.5, "~deck")]
        + [(square(dx, dy, 0.06), 0, 10, "~works") for dx in (-1.5, 1.5) for dy in (-0.12, 0.12)])},
    "Narrows Precinct House": lambda x, y: {"tiers": [(rect(x, y, 0.6, 0.42), 0, 16, "~old"),
                                                      (disc(x + 0.2, y - 0.1, 0.025), 16, 40, "~works")]},
    "Hamilton Hill High School": lambda x, y: {"tiers": _tiers(_local(x, y, 12), [
        (rect(0, 0, 0.9, 0.3), 0, 16, ""), (rect(0.3, 0.38, 0.3, 0.5), 0, 16, ""), (rect(-0.52, 0.38, 0.46, 0.4), 0, 12, "~works")])},
    "Pinkney Orphanage": lambda x, y: {"tiers": _tiers(_local(x, y, 12), [
        (rect(0, -0.3, 1.0, 0.22), 0, 20, "~old"), (rect(-0.39, 0.06, 0.22, 0.6), 0, 18, "~old"),
        (rect(0.39, 0.06, 0.22, 0.6), 0, 18, "~old"), (square(0, -0.3, 0.16), 0, 34, "~old"),
        (square(0, -0.3, 0.07), 34, 45, "~landmark")])},
    "Lacey Towers": lambda x, y: {"tiers": [(square(x, y, 0.5), 0, 64, "~old"), (square(x, y, 0.38), 64, 92, "~old"),
                                            (square(x, y, 0.26), 92, 112, "~old"), (square(x, y, 0.12), 112, 124, "~landmark")]},
    "Paris Island Incinerator": lambda x, y: {"tiers": [(rect(x, y, 0.7, 0.4), 0, 18, "~old"),
                                                        (disc(x + 0.46, y, 0.06), 0, 61, "~stack")]},
    "Paris Island Quarantine Hospital": lambda x, y: {"tiers": [(rect(x, y + dy, 0.62, 0.14), 0, 10, "~old")
                                                                for dy in (-0.3, 0, 0.3)] + [(rect(x - 0.25, y, 0.12, 0.74), 0, 8, "~old")]},
    "Cherry Hills Library": lambda x, y: {"tiers": _tiers(_local(x, y, 18), [
        (rect(0, 0, 0.55, 0.38), 0, 14, "~old"), (rect(0, -0.24, 0.3, 0.1), 0, 12, "~old"),
        (disc(0, 0, 0.12), 14, 19, "~landmark")])},
    "GBC Broadcast Center": lambda x, y: {"tiers": _tiers(_local(x, y, -22), [
        (square(0, 0, 0.42), 0, 118, "~glass"), (square(0, 0, 0.26), 118, 126, "~glass"),
        (disc(0, 0, 0.022), 126, 176, "~works")])},
    "New Town Bus Terminal": lambda x, y: {"tiers": _tiers(_local(x, y, -22), [
        (rect(0, 0, 1.5, 0.6), 0, 18, "~works"), (rect(0.92, 0, 0.32, 0.5), 6, 8, "~deck")])},
    "Arkham Mansion": lambda x, y: {"tiers": [(rect(x, y, 0.85, 0.36), 0, 16, "~old"), (rect(x + 0.35, y + 0.3, 0.3, 0.4), 0, 14, "~old"),
                                              (square(x - 0.3, y - 0.1, 0.18), 0, 28, "~old"), (square(x - 0.3, y - 0.1, 0.08), 28, 37, "~landmark")]},
    "Arkham Botanical Gardens": lambda x, y: {"tiers": [(rect(x, y, 0.9, 0.4), 0, 12, "~glass"), (disc(x, y, 0.24), 12, 18, "~glass"),
                                                        (disc(x, y, 0.13), 18, 21, "~glass")]},
    "Wayne Mining Building": lambda x, y: {"tiers": _tiers(_local(x, y, 28), [
        (rect(0, 0, 0.55, 0.4), 0, 46, "~old"), (rect(0, 0, 0.45, 0.3), 46, 53, "~copper")])},
    "Wayne Center for Children": lambda x, y: {"tiers": _tiers(_local(x, y, 6), [
        (rect(0, 0, 0.8, 0.4), 0, 12, "~glass"), (rect(0.3, 0.32, 0.3, 0.3), 0, 8, "~glass")])},
    "Solomon Wayne Courthouse": lambda x, y: {"tiers": _tiers(_local(x, y, -14), [
        (rect(0, 0, 0.8, 0.5), 0, 20, "~old"), (rect(0, -0.32, 0.5, 0.14), 0, 16, "~old"), (disc(0, 0, 0.16), 20, 26, "~landmark"),
        (disc(0, 0, 0.1), 26, 29, "~landmark"), (disc(0, 0, 0.04), 29, 33, "~landmark")])},
    "Gotham City Morgue": lambda x, y: {"tiers": _tiers(_local(x, y, 25), [(rect(0, 0, 0.5, 0.36), 0, 12, "~works")])},
    "Elliot Memorial Hospital": lambda x, y: hospital(x, y, -13, 40, 0.75),
    "Gotham Public Library": lambda x, y: {"tiers": _tiers(_local(x, y, -10), [
        (rect(0, 0, 1.3, 0.6), 0, 24, "~old"), (rect(0, 0, 0.6, 0.35), 24, 30, "~old"), (rect(0, -0.42, 1.0, 0.16), 0, 3, "~old")])},
    "Lacey's": lambda x, y: {"tiers": _tiers(_local(x, y, -10), [
        (rect(0, 0, 0.9, 0.7), 0, 46, "~old"), (rect(0, -0.36, 0.6, 0.03), 46, 50, "~sign")])},
    "Gotham Globe": lambda x, y: {"tiers": _tiers(_local(x, y, 20), [
        (rect(0, 0, 0.5, 0.5), 0, 78, "~old"), (square(0, 0, 0.36), 78, 92, "~old"), (disc(0, 0, 0.11), 92, 95, "~landmark"),
        (disc(0, 0, 0.16), 95, 101, "~landmark"), (disc(0, 0, 0.11), 101, 105, "~landmark")])},
    "GCFD Headquarters": lambda x, y: {"tiers": [(rect(x, y, 0.5, 0.35), 0, 14, "~old"),
                                                 (square(x + 0.2, y - 0.1, 0.12), 0, 26, "~old")]},
    "Daggett Industries": lambda x, y: {"tiers": _tiers(_local(x, y, -22), [
        (square(0, 0, 0.5), 0, 150, "~glass"), (square(0, 0, 0.38), 150, 170, "~glass"), (square(0, 0, 0.3), 170, 178, "~lit")])},
    "GothCorp": lambda x, y: {"tiers": _tiers(_local(x, y, 6), [
        (rect(0, 0, 1.0, 0.4), 0, 22, "~glass"), (rect(-0.6, 0.32, 0.4, 0.34), 0, 14, "~glass"),
        (disc(0.62, 0.42, 0.12), 0, 12, "~tank"), (disc(0.9, 0.42, 0.12), 0, 12, "~tank")])},
    "Gotham National Bank": lambda x, y: {"tiers": _tiers(_local(x, y, 6), [
        (rect(0, 0, 0.7, 0.55), 0, 30, "~old"), (rect(0, -0.33, 0.5, 0.1), 0, 22, "~old")])},
    "Gotham Merchants Bank": lambda x, y: {"tiers": [(rect(x, y, 0.6, 0.5), 0, 24, "~old"), (disc(x, y, 0.12), 24, 29, "~landmark")]},
    "Blackgate Prison Industries": lambda x, y: {"tiers": _tiers(_local(x, y, 8), [
        (rect(0, 0, 0.9, 0.4), 0, 14, "~works"), (rect(0.25, 0.4, 0.4, 0.3), 0, 10, "~works"),
        (disc(-0.35, -0.12, 0.05), 0, 30, "~stack")])},
    "Blackgate Causeway Gate": lambda x, y: {"tiers": [(square(x - 0.42, y, 0.12), 0, 12, "~works"),
                                                       (square(x + 0.42, y, 0.12), 0, 12, "~works")]},
    "Blackgate Ferry Landing": lambda x, y: {"tiers": [(rect(x, y, 0.45, 0.22), 0, 6, "~works")]},
    "Monarch Playing Card Company": lambda x, y: {"tiers": _tiers(_local(x, y, -4), [
        (rect(0, 0, 1.1, 0.5), 0, 16, "~old"), (disc(0.45, -0.15, 0.045), 0, 36, "~stack")])},
    "Gotham State Penitentiary": lambda x, y: {"tiers": [
        (square(x, y, 2.2).difference(square(x, y, 2.04)), 0, 10, "~old"), (square(x, y, 0.5), 0, 20, "~old")]
        + [(rect(x, y + dy, 1.4, 0.22), 0, 16, "~old") for dy in (-0.75, -0.42, 0.42, 0.75)]
        + [(square(x + sx * 1.06, y + sy * 1.06, 0.13), 0, 24, "~old") for sx in (-1, 1) for sy in (-1, 1)]},
    "Federal Building": lambda x, y: {"tiers": _tiers(_local(x, y, 20), [(rect(0, 0, 0.6, 0.5), 0, 72, "")])},
    "Hall of Records": lambda x, y: {"tiers": _tiers(_local(x, y, 20), [
        (rect(0, 0, 0.8, 0.5), 0, 22, "~old"), (rect(0, 0, 0.5, 0.3), 22, 26, "~old")])},
    "The Gotham Plaza": lambda x, y: {"tiers": _tiers(_local(x, y, -13), [
        (rect(0, 0, 0.8, 0.5), 0, 64, "~old"), (rect(0, 0, 0.7, 0.4), 64, 74, "~copper")])},
    "Kane Heights Galleria": lambda x, y: {"tiers": _tiers(_local(x, y, 8), [
        (rect(0, 0, 2.0, 1.0), 0, 14, "~works"), (rect(0, 0, 0.6, 0.9), 14, 19, "~glass")]),
        "ground": [(_local(x, y, 8)(rect(0, -1.15, 2.6, 0.9)), "lot"), (_local(x, y, 8)(rect(0, 1.15, 2.6, 0.9)), "lot")]},
    "Tricorner Fish Market": lambda x, y: {"tiers": _tiers(_local(x, y, -25), [
        (rect(0, 0, 1.6, 0.4), 0, 10, "~works"), (rect(1.0, 0.4, 0.4, 0.3), 0, 8, "~works")])},
}


# --- terrain ---------------------------------------------------------------------------

# The page's projection: one layout unit is K degrees, centred on the island.
K = 0.0013475
TERRAIN = ROOT / "web" / "map" / "terrain"
# Hills that rise out of the city, as (x, y, radius, metres).
HILLS = [(38.0, 25.5, 6.5, 46), (21.0, 92.0, 5.5, 34), (27.0, 66.5, 4.5, 20), (33.5, 46.0, 4.0, 24),
         (44.0, 72.0, 5.0, 9), (28.0, 124.0, 4.0, 11), (4.0, 20.0, 8.0, 72), (131.0, -17.0, 6.0, 42),
         (69.0, 122.6, 2.2, 14), (57.6, 130.4, 1.0, 6)]


# --- water: routes that keep off the land ---------------------------------------

class Waters:
    """
    The open water as a grid, to find routes across it that keep off the land —
    ferries between their piers, ships in from the sea. A route is the shortest
    way round, smoothed: the Statue Ferry swings round Paris Island instead of
    sailing through it, and nothing is drawn by hand to cross a headland.
    """

    def __init__(self, land, piers=None, box=(-45.0, -45.0, 165.0, 195.0), step=0.5, clearance=0.5):
        """The shore kept at `clearance`; piers and islets (`piers`) only at arm's length, or no boat could reach a berth."""
        import numpy as np
        x0, y0, x1, y1 = box
        self.x0, self.y0, self.step = x0, y0, step
        self.nx, self.ny = int((x1 - x0) / step) + 1, int((y1 - y0) / step) + 1
        gx, gy = np.meshgrid(x0 + np.arange(self.nx) * step, y0 + np.arange(self.ny) * step)
        blocked = land.buffer(clearance)
        solid = land
        if piers is not None and not piers.is_empty:
            blocked = blocked.union(piers.buffer(0.2))
            solid = solid.union(piers)
        self.wet = (~shapely.contains_xy(blocked, gx.ravel(), gy.ravel())).tolist()
        self.solid = shapely.prepared.prep(solid)
        # The open water as one body: a pocket between two piers is no place to start a voyage.
        self.open = bytearray(len(self.wet))
        seen = bytearray(len(self.wet))
        for k0 in range(len(self.wet)):
            if not self.wet[k0] or seen[k0]:
                continue
            body, todo = [k0], [k0]
            seen[k0] = 1
            while todo:
                k = todo.pop()
                i, j = k % self.nx, k // self.nx
                for kk in (k - 1 if i > 0 else -1, k + 1 if i < self.nx - 1 else -1,
                           k - self.nx if j > 0 else -1, k + self.nx if j < self.ny - 1 else -1):
                    if kk >= 0 and self.wet[kk] and not seen[kk]:
                        seen[kk] = 1
                        body.append(kk)
                        todo.append(kk)
            if len(body) > 2000:
                for k in body:
                    self.open[k] = 1
        self.near = sorted(((di, dj) for di in range(-24, 25) for dj in range(-24, 25)),
                           key=lambda o: o[0] * o[0] + o[1] * o[1])

    def _cell(self, x, y):
        """The nearest open water reachable from (x, y) in a straight line — not across a spit or a pier."""
        i0, j0 = round((x - self.x0) / self.step), round((y - self.y0) / self.step)
        here = Point(x, y).buffer(0.35)
        fallback = None
        for di, dj in self.near:
            i, j = i0 + di, j0 + dj
            if 0 <= i < self.nx and 0 <= j < self.ny and self.open[j * self.nx + i]:
                k = j * self.nx + i
                fallback = k if fallback is None else fallback
                leg = LineString([(x, y), (self.x0 + i * self.step, self.y0 + j * self.step)]).difference(here)
                if leg.is_empty or not self.solid.intersects(leg):
                    return k
        return fallback

    def route(self, a, b, berth=False):
        """
        [a, ..., b] through the water — the ends may be on a pier or a quay. A
        ship `berth`s: it stops in open water off the pier heads, not among them.
        """
        import heapq
        start, goal = self._cell(*a), self._cell(*b)
        if start is None or goal is None:
            return [tuple(a), tuple(b)]
        nx, gi, gj = self.nx, goal % self.nx, goal // self.nx
        best, came, heap = {start: 0.0}, {}, [(0.0, start)]
        moves = [(1, 0, 1.0), (-1, 0, 1.0), (0, 1, 1.0), (0, -1, 1.0),
                 (1, 1, 1.414), (1, -1, 1.414), (-1, 1, 1.414), (-1, -1, 1.414)]
        while heap:
            _, k = heapq.heappop(heap)
            if k == goal:
                break
            i, j, base = k % nx, k // nx, best[k]
            for di, dj, c in moves:
                ii, jj = i + di, j + dj
                if 0 <= ii < nx and 0 <= jj < self.ny:
                    kk = jj * nx + ii
                    if di and dj and not (self.wet[j * nx + ii] and self.wet[jj * nx + i]):
                        continue            # no squeezing diagonally past the end of a pier
                    if self.wet[kk] and base + c < best.get(kk, 1e18):
                        best[kk], came[kk] = base + c, k
                        heapq.heappush(heap, (base + c + math.hypot(ii - gi, jj - gj), kk))
        if goal != start and goal not in came:
            if os.environ.get("DEBUG_FERRY"):
                def reach(k0):
                    seen, todo = {k0}, [k0]
                    while todo:
                        k = todo.pop()
                        i, j = k % nx, k // nx
                        for di, dj, _ in moves:
                            ii, jj = i + di, j + dj
                            kk = jj * nx + ii
                            if 0 <= ii < nx and 0 <= jj < self.ny and self.wet[kk] and kk not in seen:
                                seen.add(kk)
                                todo.append(kk)
                    return len(seen)
                print("  NO PATH", a, b, "start cell", (self.x0 + start % nx * self.step, self.y0 + start // nx * self.step),
                      "reach", reach(start), "goal cell", (self.x0 + goal % nx * self.step, self.y0 + goal // nx * self.step),
                      "reach", reach(goal))
            return [tuple(a), tuple(b)]
        cells = [goal]
        while cells[-1] != start:
            cells.append(came[cells[-1]])
        pts = [tuple(a)] + [(self.x0 + (k % nx) * self.step, self.y0 + (k // nx) * self.step)
                            for k in reversed(cells)] + ([] if berth else [tuple(b)])
        line = LineString(pts)
        # Straight where it can be, curving round the land where it can't —
        # checked clear of the shore once smoothed, away from its own two piers.
        for tol in (1.6, 1.0, 0.6, 0.3, 0.15):
            simple = list(line.simplify(tol).coords)
            smooth = LineString(chaikin(simple, 2, closed=False)) if len(simple) > 2 else LineString(simple)
            ends = min(0.35, smooth.length / 4)
            if not self.solid.intersects(substring(smooth, ends, smooth.length - ends)):
                return [(round(x, 2), round(y, 2)) for x, y in smooth.coords]
        return [(round(x, 2), round(y, 2)) for x, y in line.coords]


# --- the city's grounds: pitches, courts, golf, parking ---------------------------

def pitch(kind, cx, cy, angle):
    """
    A playing field as its markings, in map units (one is ~150 m), turned to
    `angle`: [(geometry, part)] — the turf, then the lines. Soccer and American
    football are rectangles with their own markings; a baseball diamond is a
    fan with its infield; a court is small and hard.
    """
    def turn(g):
        return affinity.rotate(affinity.translate(g, cx, cy), angle, origin=(cx, cy))
    parts = []
    if kind == "soccer":                         # 105 x 68 m
        w, h = 0.70, 0.45
        parts += [(box(-w / 2, -h / 2, w / 2, h / 2), "turf"), (box(-w / 2, -h / 2, w / 2, h / 2).exterior, "line"),
                  (LineString([(0, -h / 2), (0, h / 2)]), "line"), (Point(0, 0).buffer(0.061).exterior, "line")]
        for side in (-1, 1):
            parts.append((box(min(side * w / 2, side * (w / 2 - 0.11)), -0.135, max(side * w / 2, side * (w / 2 - 0.11)),
                              0.135).exterior, "line"))
            parts.append((box(min(side * w / 2, side * (w / 2 - 0.037)), -0.061, max(side * w / 2, side * (w / 2 - 0.037)),
                              0.061).exterior, "line"))
    elif kind == "football":                     # 110 x 49 m, a yard line every ten
        w, h = 0.73, 0.33
        parts += [(box(-w / 2, -h / 2, w / 2, h / 2), "turf"), (box(-w / 2, -h / 2, w / 2, h / 2).exterior, "line")]
        for k in range(1, 12):
            x = -w / 2 + k * w / 12
            parts.append((LineString([(x, -h / 2), (x, h / 2)]), "line"))
    elif kind == "track":                        # a 400 m oval, and a football field inside it
        a, rad = 0.28, 0.305
        oval = box(-a, -rad, a, rad).union(Point(-a, 0).buffer(rad)).union(Point(a, 0).buffer(rad))
        parts += [(oval, "track"), (oval.buffer(-0.062), "turf"), (oval.buffer(-0.062).exterior, "line")]
        parts += [(g, k) for g, k in pitch("football", 0, 0, 0) if k == "line"]
    elif kind == "baseball":                     # a fan: the infield diamond and the outfield
        r = 0.62
        fan = Polygon([(0, 0)] + [(r * math.cos(a), -r * math.sin(a))
                                  for a in [math.radians(45 + k * 90 / 16) for k in range(17)]])
        diamond = Polygon([(0, 0), (0.12, -0.12), (0, -0.24), (-0.12, -0.12)])
        parts += [(fan, "turf"), (diamond.buffer(0.05), "dirt"), (diamond.exterior, "line"),
                  (LineString([(0, 0), (r * math.cos(math.radians(45)), -r * math.sin(math.radians(45)))]), "line"),
                  (LineString([(0, 0), (-r * math.cos(math.radians(45)), -r * math.sin(math.radians(45)))]), "line")]
        parts = [(affinity.translate(g, 0, 0.3), k) for g, k in parts]
    elif kind in ("basketball", "tennis"):       # 28 x 15 m; 24 x 11 m — and its neighbours
        w, h = (0.19, 0.1) if kind == "basketball" else (0.16, 0.075)
        for k in (-1, 0, 1):
            dx = k * (w + 0.04)
            parts += [(box(dx - w / 2, -h / 2, dx + w / 2, h / 2), "court"),
                      (box(dx - w / 2, -h / 2, dx + w / 2, h / 2).exterior, "line"),
                      (LineString([(dx, -h / 2), (dx, h / 2)]), "line")]
    return [(turn(g), part) for g, part in parts if not g.is_empty]


def bays(lot, angle, aisle=0.12):
    """A car park's rows: lines along it, drawn dashed as stalls, an aisle apart."""
    c = lot.centroid
    flat = affinity.rotate(lot, -angle, origin=c)
    minx, miny, maxx, maxy = flat.bounds
    rows = []
    y = miny + aisle / 2
    while y < maxy:
        row = LineString([(minx - 1, y), (maxx + 1, y)]).intersection(flat.buffer(-0.03))
        for part in lines_of(row):
            if part.length > 0.1:
                rows.append(affinity.rotate(part, angle, origin=c))
        y += aisle
    return rows


def _along(a, b, f):
    return (a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f)


def oriented(cx, cy, w, h, angle):
    """A w x h rectangle centred on (cx, cy), its long side turned to `angle` degrees."""
    return affinity.rotate(box(cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2), angle, origin=(cx, cy))


def airport_layout(spec):
    """
    Archie Goodwin International as an airport rather than two strips and a
    slab: the terminal and its concourses in the wedge between the runways,
    gates with jet bridges, taxiways alongside both runways, approach lights
    out past the thresholds; landside a frontage road, the parking garage, the
    long-stay lots and a hotel; across the main runway the cargo apron, its
    hangars, the private hangars and the fuel farm; the tower where it can see
    both runways. Returns {"ground": [(geom, layer)], "buildings": [(geom, top,
    kind, base)], "roads": [(line, cls, name)], "gates": [(x, y, heading)],
    "lots": [(polygon, angle)]}.
    """
    (w1, e1), (s2, n2) = spec["runways"]
    r1, r2 = LineString([w1, e1]), LineString([s2, n2])
    cross = r1.intersection(r2)
    c = (cross.x, cross.y)
    clear = unary_union([r1.buffer(1.45), r2.buffer(1.45)])
    out = {"ground": [], "buildings": [], "roads": [], "gates": [], "lots": []}
    # The landside line between the runways' far ends, and the way into the wedge.
    mx, my = _along(n2, e1, 0.5)
    dx, dy = e1[0] - n2[0], e1[1] - n2[1]
    span = math.hypot(dx, dy)
    d = (dx / span, dy / span)
    n = (-d[1], d[0]) if (c[0] - mx) * -d[1] + (c[1] - my) * d[0] > 0 else (d[1], -d[0])
    along_deg = math.degrees(math.atan2(d[1], d[0]))
    t = (mx - d[0] * 1.5 + n[0] * 1.8, my - d[1] * 1.5 + n[1] * 1.8)       # the terminal's middle
    terminal = oriented(t[0], t[1], 7.0, 0.72, along_deg)
    out["buildings"] += [(terminal, 22, "~glass", 0),
                         (oriented(t[0] - n[0] * 0.08, t[1] - n[1] * 0.08, 6.2, 0.36, along_deg), 28, "~glass", 22)]
    apron_parts = [terminal.buffer(0.9)]
    for k in (-2.1, 0.0, 2.1):
        base = (t[0] + d[0] * k + n[0] * 0.36, t[1] + d[1] * k + n[1] * 0.36)
        length = 2.9
        while length > 1.0:
            tip = (base[0] + n[0] * length, base[1] + n[1] * length)
            pier = LineString([base, tip]).buffer(0.17, cap_style="flat")
            if not pier.buffer(0.62).intersects(clear):
                break
            length -= 0.2
        out["buildings"].append((pier, 14, "~glass", 0))
        apron_parts.append(pier.buffer(0.95))
        # Gates down both sides: a jet bridge, and room for a plane nosed in.
        g = 0.5
        while g < length - 0.1:
            for side in (1, -1):
                at = (base[0] + n[0] * g + d[0] * side * 0.17, base[1] + n[1] * g + d[1] * side * 0.17)
                stand = (at[0] + d[0] * side * 0.42, at[1] + d[1] * side * 0.42)
                if not Point(stand).buffer(0.3).intersects(clear):
                    out["buildings"].append((LineString([at, (at[0] + d[0] * side * 0.16, at[1] + d[1] * side * 0.16)])
                                             .buffer(0.025, cap_style="flat"), 6, "~deck", 4))
                    out["gates"].append((round(stand[0], 2), round(stand[1], 2),
                                         round(math.degrees(math.atan2(-d[0] * side, d[1] * side)), 1)))
            g += 0.62
    wedge = Polygon([c, n2, e1]).buffer(0.6)
    apron = unary_union(apron_parts).intersection(wedge).difference(clear.buffer(-0.35))
    out["ground"].append((apron, "apron"))
    # Taxiways: alongside each runway on the wedge side, the cargo side too, and
    # the links between them and the runways.
    side1 = -1 if r1.offset_curve(-1.0).distance(Point(t)) < r1.offset_curve(1.0).distance(Point(t)) else 1
    side2 = 1 if r2.offset_curve(1.0).distance(Point(t)) < r2.offset_curve(-1.0).distance(Point(t)) else -1
    taxi = [r1.offset_curve(side1 * 1.15), r2.offset_curve(side2 * 1.15), r1.offset_curve(-side1 * 1.3)]
    taxi = [line.intersection(Point(c).buffer(16)).difference(Point(c).buffer(1.7)) for line in taxi]
    links = []
    for rw, lane in ((r1, taxi[0]), (r2, taxi[1]), (r1, taxi[2])):
        for part in lines_of(lane):
            for f in (0.1, 0.5, 0.9):
                q = part.interpolate(f, normalized=True)
                links.append(LineString([q, rw.interpolate(rw.project(q))]))
    for part in lines_of(apron.boundary):
        for f in (0.2, 0.55, 0.85):
            q = part.interpolate(f, normalized=True)
            near = min((ln for ln in taxi if not ln.is_empty), key=lambda ln: ln.distance(q))
            if near.distance(q) < 2.0:
                links.append(LineString([q, near.interpolate(near.project(q))]))
    for line in taxi + links:
        for part in lines_of(line):
            if part.length > 0.2:
                out["ground"].append((part, "taxiway"))
    # Approach lights: a row of lights out past each threshold, and the edge lights.
    for rw in (r1, r2):
        (ax, ay), (bx, by) = rw.coords
        L = rw.length
        ux, uy = (bx - ax) / L, (by - ay) / L
        out["ground"].append((LineString([(ax - ux * 0.3, ay - uy * 0.3), (ax - ux * 3.2, ay - uy * 3.2)]), "approach"))
        out["ground"].append((LineString([(bx + ux * 0.3, by + uy * 0.3), (bx + ux * 3.2, by + uy * 3.2)]), "approach"))
        for off in (0.36, -0.36):
            out["ground"].append((rw.offset_curve(off), "edgelights"))
    # Landside: the frontage road, the garage, the lots, the hotel.
    front = LineString([(t[0] - d[0] * 3.9 - n[0] * 0.7, t[1] - d[1] * 3.9 - n[1] * 0.7),
                        (t[0] + d[0] * 3.9 - n[0] * 0.7, t[1] + d[1] * 3.9 - n[1] * 0.7)])
    back = LineString([(t[0] + d[0] * 3.9 - n[0] * 2.25, t[1] + d[1] * 3.9 - n[1] * 2.25),
                       (t[0] - d[0] * 4.4 - n[0] * 2.25, t[1] - d[1] * 4.4 - n[1] * 2.25)])
    loop = LineString(list(front.coords) + list(back.coords) + [front.coords[0]])
    out["roads"].append((LineString(chaikin(list(loop.coords), 2, closed=False)), "secondary", "Terminal Drive"))
    out["entry"] = back.coords[-1]
    garage = oriented(t[0] - n[0] * 1.45, t[1] - n[1] * 1.45, 4.6, 0.95, along_deg)
    out["buildings"].append((garage, 21, "~garage", 0))
    for k, (fw, fh) in ((-2.5, (3.6, 1.5)), (2.4, (3.4, 1.5))):
        lot = oriented(t[0] + d[0] * k - n[0] * 3.3, t[1] + d[1] * k - n[1] * 3.3, fw, fh, along_deg)
        out["lots"].append((lot, along_deg))
    hotel = (t[0] - d[0] * 5.4 - n[0] * 1.6, t[1] - d[1] * 5.4 - n[1] * 1.6)
    out["buildings"] += [(oriented(hotel[0], hotel[1], 0.9, 0.36, along_deg + 90), 48, "~glass", 0)]
    # The tower: airside, at the end of the terminal, where it sees both runways.
    spots = [(t[0] + d[0] * a + n[0] * b, t[1] + d[1] * a + n[1] * b)
             for a in (-4.2, 4.2, -3.8, 3.8, -3.4) for b in (0.9, 0.5, 0.0, -0.4)]
    tw = next((q for q in spots if not Point(q).buffer(0.45).intersects(clear)
               and not Point(q).buffer(0.25).intersects(terminal)), spots[-1])
    out["buildings"] += [(disc(tw[0], tw[1], 0.12), 58, "~landmark", 0), (disc(tw[0], tw[1], 0.2), 66, "~landmark", 58),
                         (disc(tw[0], tw[1], 0.03), 76, "~landmark", 66)]
    # Across the main runway: cargo, the hangars, the private hangars, the fuel farm.
    south = 1 if side1 == -1 else -1
    r1_deg = math.degrees(math.atan2(e1[1] - w1[1], e1[0] - w1[0]))
    ux, uy = (e1[0] - w1[0]) / r1.length, (e1[1] - w1[1]) / r1.length
    nx_, ny_ = -uy * south, ux * south
    if nx_ * 0 + ny_ < 0:
        nx_, ny_ = -nx_, -ny_
    mid = r1.interpolate(r1.project(Point(c)) + 6.4)
    cargo_c = (mid.x + nx_ * 3.4, mid.y + ny_ * 3.4)
    cargo = oriented(cargo_c[0], cargo_c[1], 8.6, 2.0, r1_deg)
    out["ground"].append((cargo, "apron"))
    for k in range(4):
        hx = cargo_c[0] + ux * (-3.0 + k * 1.75) + nx_ * 1.55
        hy = cargo_c[1] + uy * (-3.0 + k * 1.75) + ny_ * 1.55
        out["buildings"].append((oriented(hx, hy, 1.45, 0.95, r1_deg), 27 if k < 3 else 16, "~works", 0))
    out["buildings"].append((oriented(cargo_c[0] + ux * 4.6 + nx_ * 1.4, cargo_c[1] + uy * 4.6 + ny_ * 1.4,
                                      2.0, 0.8, r1_deg), 16, "~works", 0))
    for k in range(5):
        px_ = cargo_c[0] + ux * (5.6 + k * 0.72) + nx_ * 0.2
        py_ = cargo_c[1] + uy * (5.6 + k * 0.72) + ny_ * 0.2
        out["buildings"].append((oriented(px_, py_, 0.58, 0.5, r1_deg), 11, "~works", 0))
    out["hangar"] = (round(cargo_c[0] + ux * 8.48 + nx_ * 0.2, 2), round(cargo_c[1] + uy * 8.48 + ny_ * 0.2, 2))
    fuel = (cargo_c[0] - ux * 5.6 + nx_ * 2.1, cargo_c[1] - uy * 5.6 + ny_ * 2.1)
    for k in range(6):
        out["buildings"].append((disc(fuel[0] + (k % 3) * 0.42, fuel[1] + (k // 3) * 0.42, 0.17), 13, "~tank", 0))
    cargo_end = (cargo_c[0] + ux * 9.6 + nx_ * 2.6, cargo_c[1] + uy * 9.6 + ny_ * 2.6)
    out["roads"].append((LineString([(cargo_c[0] - ux * 4.4 + nx_ * 2.6, cargo_c[1] - uy * 4.4 + ny_ * 2.6), cargo_end]),
                         "secondary", "Cargo Road"))
    # Round the far end of the main runway, clear of its lights, to the terminal's front.
    round_end = [cargo_end, (e1[0] + ux * 4.2 + nx_ * 1.8, e1[1] + uy * 4.2 + ny_ * 1.8),
                 (e1[0] + ux * 4.6 - nx_ * 1.2, e1[1] + uy * 4.6 - ny_ * 1.2), tuple(front.coords[-1])]
    out["roads"].append((LineString(chaikin(round_end, 2, closed=False)), "secondary", "Perimeter Road"))
    out["clear"] = clear
    return out


def _noded(lines):
    """Every crossing made a junction — on a fine grid first, so a road meeting another meets it exactly."""
    return shapely.node(shapely.set_precision(lines, 0.001))


def reaching(a, b, past=0.03):
    """A line from a to b carried a hair past b, so it truly crosses the road it meets and joins it there."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    n = math.hypot(dx, dy) or 1
    return LineString([a, (b[0] + dx / n * past, b[1] + dy / n * past)])


def _pieces(lines):
    """The road network's separate pieces: (junctions, {piece: [junction indices]})."""
    ids, nodes, parent = {}, [], []

    def node(pt):
        key = (round(pt[0], 2), round(pt[1], 2))
        if key not in ids:
            ids[key] = len(nodes)
            nodes.append(key)
            parent.append(len(parent))
        return ids[key]

    def root(k):
        while parent[k] != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k
    for seg in _noded(shapely.MultiLineString([list(ln.coords) for ln in lines])).geoms:
        a, b = root(node(seg.coords[0])), root(node(seg.coords[-1]))
        if a != b:
            parent[a] = b
    groups = {}
    for k in range(len(nodes)):
        groups.setdefault(root(k), []).append(k)
    return nodes, groups


def connections(lines, land, islands, blocked):
    """
    The joins that make the roads one network: every dead end that stops just
    short of a road meets it; every piece cut off from the rest — a town's
    grid that never reaches the highway, a bridge that lands beside a street —
    gets the shortest spur over dry land to the nearest other piece, until
    they're one; and an island with no streets of its own gets a drive from its
    landing to each building on it. Returns [(line, class)] to draw and build
    round like any other road.
    """
    import numpy as np
    out, made = [], set()

    def join(a, b, cls):
        key = tuple(sorted(((round(a[0], 2), round(a[1], 2)), (round(b[0], 2), round(b[1], 2)))))
        if key not in made and key[0] != key[1]:
            made.add(key)
            out.append((LineString([a, b]), cls))
    dry = land.buffer(0.05)
    for landing, targets in islands:
        # Each building's door: the edge of its footprint nearest the landing.
        doors = [shapely.ops.nearest_points(Point(landing), foot.buffer(0.18).boundary)[1] for foot in targets]
        reached = [Point(landing)]
        todo = list(range(len(doors)))
        while todo:
            best = None
            for k in todo:
                for r in reached:
                    leg = LineString([r, doors[k]])
                    if dry.contains(leg) and not leg.intersects(blocked.buffer(-0.12)):
                        if best is None or leg.length < best[0]:
                            best = (leg.length, k, r)
            if best is None:
                break
            a_, b_ = (best[2].x, best[2].y), (doors[best[1]].x, doors[best[1]].y)
            # A drive bends as drives do: a gentle curve, the same way each build.
            dx, dy = b_[0] - a_[0], b_[1] - a_[1]
            bend = (0.18 if (int(a_[0] * 7 + b_[1] * 3) % 2) else -0.18) * math.hypot(dx, dy)
            mid = ((a_[0] + b_[0]) / 2 - dy / (math.hypot(dx, dy) or 1) * bend,
                   (a_[1] + b_[1]) / 2 + dx / (math.hypot(dx, dy) or 1) * bend)
            curved = LineString([((1 - t) ** 2 * a_[0] + 2 * (1 - t) * t * mid[0] + t * t * b_[0],
                                  (1 - t) ** 2 * a_[1] + 2 * (1 - t) * t * mid[1] + t * t * b_[1])
                                 for t in [k / 14 for k in range(15)]])
            if dry.contains(curved) and not curved.intersects(blocked.buffer(-0.12)):
                out.append((curved, "drive"))
            else:
                join(a_, b_, "drive")
            reached.append(doors[best[1]])
            todo.remove(best[1])
    every = [ln for ln, _ in lines if ln.length > 0.05]
    noded = list(_noded(shapely.MultiLineString([list(ln.coords) for ln in every + [ln for ln, _ in out]])).geoms)
    ends = {}
    for seg in noded:
        for pt in (seg.coords[0], seg.coords[-1]):
            key = (round(pt[0], 2), round(pt[1], 2))
            ends[key] = ends.get(key, 0) + 1
    tree = shapely.STRtree(noded)
    for (x, y), degree in ends.items():
        if degree != 1:
            continue
        here = Point(x, y)
        best = None
        for idx in tree.query(here.buffer(0.5)):
            seg = noded[int(idx)]
            d = seg.distance(here)
            if d > 0.02 and d < 0.5 and (best is None or d < best[0]):
                best = (d, seg)
        if best:
            q = shapely.ops.nearest_points(here, best[1])[1]
            if dry.contains(LineString([here, q])) and not LineString([here, q]).intersects(blocked):
                join((x, y), reaching((x, y), (q.x, q.y)).coords[-1], "street")
    # Whatever's still in pieces: each joined to its nearest neighbour over dry land.
    for _ in range(14):
        nodes, groups = _pieces(every + [ln for ln, _ in out])
        if len(groups) <= 1:
            break
        xy = np.array(nodes)
        joined = False
        for members in sorted(groups.values(), key=len)[:-1]:
            if len(members) < 2:
                continue
            mine = set(members)
            others = np.array([k for k in range(len(nodes)) if k not in mine])
            best = None
            for k in members:
                x, y = xy[k]
                d = np.hypot(xy[others, 0] - x, xy[others, 1] - y)
                for o in np.argsort(d)[:6]:
                    if d[o] > 6.0:
                        break
                    spur = LineString([(x, y), tuple(xy[others[o]])])
                    if (best is None or d[o] < best[0]) and dry.contains(spur) and not spur.intersects(blocked):
                        best = (d[o], (x, y), tuple(xy[others[o]]))
                        break
            if best:
                join(best[1], best[2], "street" if best[0] > 1.2 else "link")
                joined = True
        if not joined:
            break
    return out


def road_graph(lines, ferries, places, land):
    """
    Every road noded where it meets another, as {"nodes": [[x, y]], "edges":
    [[a, b, class]], "ferries": [[a, b, [[x, y], ...]]], "places": {name: node}}.
    A place's node is the nearest it can reach without crossing water.
    """
    import numpy as np
    noded = _noded(shapely.MultiLineString([list(ln.coords) for ln, _ in lines if ln.length > 0.05]))
    index = shapely.STRtree([ln for ln, _ in lines])
    classes = [c for _, c in lines]
    nodes, ids, edges = [], {}, []

    def node(pt):
        key = (round(pt[0], 2), round(pt[1], 2))
        if key not in ids:
            ids[key] = len(nodes)
            nodes.append(list(key))
        return ids[key]
    for seg in noded.geoms:
        coords = list(seg.coords)
        if len(coords) < 2 or seg.length < 0.01:
            continue
        mid = seg.interpolate(0.5, normalized=True)
        near = index.query_nearest(mid)
        cls = classes[int(near[0])] if len(near) else "street"
        a, b = node(coords[0]), node(coords[-1])
        if a != b:
            edges.append([a, b, cls] + ([[[round(x, 2), round(y, 2)] for x, y in coords[1:-1]]] if len(coords) > 2 else []))
    xy = np.array(nodes)
    wet_free = land.buffer(0.08)

    def reach(x, y, most=4.0):
        order = np.argsort((xy[:, 0] - x) ** 2 + (xy[:, 1] - y) ** 2)[:40]
        for k in order:
            nx_, ny_ = xy[k]
            if math.hypot(nx_ - x, ny_ - y) > most:
                break
            if wet_free.contains(LineString([(x, y), (nx_, ny_)])):
                return int(k)
        return int(order[0])
    links = []
    for name, stops in ferries:
        for a, b in zip(stops, stops[1:], strict=False):
            links.append([reach(*a, most=3.0), reach(*b, most=3.0), name])
    import base64
    x0, y0, step, nx_, ny_ = -45.0, -45.0, 0.5, 421, 481
    gx, gy = np.meshgrid(x0 + np.arange(nx_) * step, y0 + np.arange(ny_) * step)
    bits = np.packbits(shapely.contains_xy(land, gx.ravel(), gy.ravel()).astype(np.uint8))
    return {"nodes": nodes, "edges": edges, "ferries": links,
            "places": {p["name"]: reach(p["x"], p["y"]) for p in places},
            "land": {"x0": x0, "y0": y0, "step": step, "nx": nx_, "ny": ny_,
                     "bits": base64.b64encode(bits.tobytes()).decode()}}


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

    built, heights = [], []

    def solid_pieces(poly):
        if not poly.interiors:
            return [poly]
        hole = Polygon(poly.interiors[0]).representative_point()
        minx, miny, maxx, maxy = poly.bounds
        halves = split(poly, LineString([(hole.x, miny - 1), (hole.x, maxy + 1)]))
        return [q for g in halves.geoms if g.geom_type == "Polygon" for q in solid_pieces(g)]

    def building(geom, h, kind="", base=0):
        for poly in (q for g in polys_of(shapely.set_precision(geom, 0.01)) for q in solid_pieces(g)):
            ring = list(poly.exterior.coords)[:-1]
            if len(ring) >= 3:
                row = [int(h), int(base)] + [round(v * 100) for pt in ring for v in pt]
                buildings.append(row + ([kind] if kind else []))
                if base == 0:
                    built.append(poly)
                    heights.append(h)

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
    # The islands nobody builds on: Arkham's grounds, Paris Island gone to woods,
    # the lawns round the Statue — planted, not bare.
    for isle in ("Arkham Island", "Paris Island", "Justice Island", "Blackgate Isle"):
        if areas.get(isle) is not None:
            parks.append(areas[isle].buffer(-0.22).difference(water_cut))
            park_names.append("")
    # The big complexes' own ground — a campus's lawns are parkland, a plant's
    # yard, a stadium's car parks and plaza keep the streets and blocks off.
    grounds = [(g, layer) for pl in src["places"] for g, layer in landmark_grounds(pl["name"], *footprint_at(pl))]
    dry_land = land.difference(water_cut)
    grounds = [(g if layer in ("pool", "pier") else g.intersection(dry_land), layer) for g, layer in grounds]
    grounds = [(g, layer) for g, layer in grounds if not g.is_empty]
    for g, layer in grounds:
        if layer == "campus":
            parks.append(g.difference(water_cut))
            park_names.append("")
    ground_block = unary_union([g for g, layer in grounds if layer in ("plaza", "lot", "works", "grounds", "marsh")
                                or (layer.startswith("pitch:") and layer != "pitch:line")]
                               or [Point(0, 0).buffer(0)])
    golf = None
    if src.get("golf"):
        golf = Polygon(chaikin(roughen(src["golf"]["coast"], 77, amp=0.4), 3)).buffer(0).difference(water_cut)
        parks.append(golf)
        park_names.append(src["golf"]["name"])
    park_union = unary_union(parks)
    # The big parks — the railway goes under these, and through the strips by the water.
    big_parks = unary_union([p for p, nm in zip(parks, park_names, strict=True) if nm and p.area > 20]
                            or [Point(0, 0).buffer(0)])
    for p, name in zip(parks, park_names, strict=True):
        add(p.difference(water_cut), "park", n=name)       # a lake in a park is water, not lawn
    for p in plazas:
        add(p, "plaza")
    # Ponds in the bigger parks — never under a landmark.
    ponds = []
    # What stands on the ground: a monorail platform or a pipe rack in the air doesn't cut the street under it.
    footprints = unary_union([t[0] for pl in src["places"] for t in landmark_shapes(pl["name"], *footprint_at(pl))
                              if t[1] < 4 and t[0].area >= 0.02]
                             or [Point(0, 0)])
    standing = footprints.buffer(0.5)
    for i, p in enumerate(parks[:2]):
        c = p.representative_point()
        for k in range(2):
            pond = blob(c.x + pr.uniform(-2, 2), c.y + pr.uniform(-3, 3), pr.uniform(0.3, 0.6), 900 + i * 5 + k)
            if p.buffer(-0.3).contains(pond) and not pond.intersects(water_cut) and not pond.intersects(standing):
                add(pond, "water", n="")
                ponds.append(pond)
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
    bare = unary_union([areas[a].buffer(0.6) for a in SPARSE if areas.get(a) is not None] or [Point(0, 0).buffer(0)])
    for part in lines_of(LineString(chaikin(list(harbor.coords), 2, closed=False))
                         .difference(water_cut.buffer(0.25)).difference(bare)):
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
    # Rail runs under the big parks, not across them.
    named = [(p, c, n) for ln, c, n in named
             for p in (lines_of(ln.difference(big_parks)) if c == "rail" else [ln]) if p.length > 0.2]
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
    # And the city's streets give way to its landmarks: a grid street that ran
    # through Wayne Tower now stops at the plaza round it.
    plazas_round = footprints.buffer(0.22).union(ground_block.buffer(0.08))
    streets = [(p, c) for s, c in streets
               for p in lines_of(s.difference(holes).difference(plaza_union).difference(plazas_round))
               if p.length > 0.2]
    secondary = [p for s in secondary for p in lines_of(s.difference(holes).difference(plazas_round)) if p.length > 0.2]
    named = [(p, c, n) for ln, c, n in named
             for p in (lines_of(ln.difference(holes)) if n not in [rb["name"] for rb in src.get("roundabouts", [])]
                       else [ln]) if p.length > 0.15]
    bridges = [(span(curve(b["line"], 2), land), b["name"]) for b in src["bridges"]]
    bridges = [(line, name) for line, name in bridges if line is not None]
    # A road that runs into water carries on over it: wherever one crosses a river
    # with no bridge drawn, it gets one — the expressway over the Kane's mouth.
    spans_ = unary_union([line.buffer(0.8) for line, _ in bridges]) if bridges else Point(0, 0).buffer(0)
    for road in src["roads"]:
        if road["class"] == "rail":
            continue
        full = curve(road["line"])
        for wet in lines_of(full.difference(land)):
            if wet.length < 0.2 or wet.length > 14 or wet.intersects(spans_):
                continue
            a = max(0.0, full.project(Point(wet.coords[0])) - 0.3)
            b = min(full.length, full.project(Point(wet.coords[-1])) + 0.3)
            if a > b:
                a, b = b, a
            bridges.append((substring(full, a, b), f"{road['name']} Bridge"))
    air = airport_layout(src["airport"]) if src.get("airport") else None
    if air:
        named += air["roads"]

    # The sprawl: past the towns the mainland goes on — lanes and low houses
    # thinning out into the dark, a city that doesn't stop at its limits.
    towns = {n: a for n, a in areas.items() if n in outer}
    town_union = unary_union(list(towns.values()))
    open_land = unary_union([m.difference(water_cut) for m in mainland.values()])
    airfield = Point(0, 0).buffer(0)
    if air:
        # The whole airport, airside and land: no sprawl on the runways or in the car parks.
        airfield = unary_union([g.buffer(1.0) for g, layer in air["ground"] if layer == "apron"]
                               + [LineString(rw).buffer(1.6) for rw in src["airport"]["runways"]]
                               + [g.buffer(0.5) for g, *_ in air["buildings"]] + [lot.buffer(0.4) for lot, _ in air["lots"]]
                               + [ln.buffer(0.4) for ln, _, _ in air["roads"]])
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
    # The lanes give way to what stands out there too — the prison's walls, the mall, the track.
    lanes = [p for ln in lanes for p in lines_of(ln.difference(plazas_round)) if p.length > 0.2]

    # Each bridge's ends run on to the nearest road, so nothing dead-ends at a bank.
    network = unary_union([ln for ln, c, _ in named if c != "rail"] + secondary + [s for s, _ in streets])
    for line, _name in list(bridges):
        for end in (Point(line.coords[0]), Point(line.coords[-1])):
            near = shapely.ops.nearest_points(end, network)[1] if not network.is_empty else None
            if near is not None and 0.15 < end.distance(near) < 6.0 and land.buffer(0.08).contains(LineString([end, near])):
                named.append((reaching((end.x, end.y), (near.x, near.y)), "primary", ""))
    # And then the whole network joined up: dead ends to the road beside them,
    # stranded pieces to the rest, the bare islands' drives from their landings.
    landings = {}
    for line, _name in bridges:
        for end in (line.coords[0], line.coords[-1]):
            for area in SPARSE:
                if areas.get(area) is not None and areas[area].buffer(0.3).contains(Point(end)):
                    landings[area] = end
    for ferry in src.get("ferries", []):
        for stop in ferry.get("stops") or []:
            for area in SPARSE:
                if area not in landings and areas.get(area) is not None and areas[area].buffer(1.2).contains(Point(stop)):
                    q = shapely.ops.nearest_points(Point(stop), areas[area].buffer(-0.3))[1]
                    landings[area] = (q.x, q.y)
    islands = []
    for area in landings:
        feet = [unary_union([t[0] for t in landmark_shapes(pl["name"], *footprint_at(pl))])
                for pl in src["places"] if pl["area"] == area and pl["kind"] != "district"]
        islands.append((landings[area], [f for f in feet if not f.is_empty]))
    joins = connections([(ln, c) for ln, c, _ in named if c != "rail"] + [(s_, "secondary") for s_ in secondary]
                        + [(s_, c) for s_, c in streets] + [(s_, "lane") for s_ in lanes]
                        + [(ln, "bridge") for ln, _ in bridges], land, islands, footprints.buffer(0.08))
    # The spurs and the island drives are roads to draw; a dead end's step to the
    # road beside it is too short to see — it joins the network, not the map.
    named += [(ln, c, "") for ln, c in joins if c in ("drive", "street") and ln.length > 1.2 or c == "drive"]
    unseen_joins = [ln for ln, c in joins if not (c in ("drive", "street") and ln.length > 1.2 or c == "drive")]
    print(f"{len(joins)} joins in the road network ({len(joins) - len(unseen_joins)} drawn)")
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

    # Below and around it: the subway, its stations — and the trains' lines as
    # wholes, with where they run in tunnel and where they cross the water.
    for line in src.get("subway", []):
        pts = [(st[1], st[2]) for st in line["stations"]]
        route_ = curve(pts, 3)
        add(route_, "subway", n=line["name"], col=line["color"],
            st=[round(route_.project(Point(st[1], st[2])) / route_.length, 4) for st in line["stations"]])
        for st in line["stations"]:
            add(Point(st[1], st[2]), "station", n=st[0], col=line["color"], line=line["name"])
    for road in src["roads"]:
        if road["class"] != "rail":
            continue
        full = curve(road["line"])
        length = full.length

        def spans(geom, whole=full, total=length):
            out = []
            for part in lines_of(geom):
                a, b = whole.project(Point(part.coords[0])) / total, whole.project(Point(part.coords[-1])) / total
                out.append([round(min(a, b), 4), round(max(a, b), 4)])
            return sorted(out)
        tunnels = spans(full.intersection(big_parks))
        # A crossing in tunnel (under the Reservoir) is no bridge.
        crossings = [c for c in spans(full.difference(land))
                     if not any(a - 0.002 <= c[0] and c[1] <= b + 0.002 for a, b in tunnels)]
        stops = [[n, f] for n, f in src.get("rail_stations", {}).get(road["name"], [])]
        add(full, "railroute", n=road["name"], tun=tunnels, br=crossings, st=stops)
        for a, b in crossings:
            add(substring(full, a * length, b * length), "railbridge", n=road["name"])
        for n, f in stops:
            q = full.interpolate(f * length)
            add(Point(q.x, q.y), "railstation", n=n, line=road["name"])
    # Piers along the docks: finger piers square to a straight stretch of quay —
    # never on a bend, where they'd fan into each other — none crossing another
    # or a bridge, and on the long ones a shed.
    placed_piers, candidate_piers = [], []
    # The streets along the quays: a jetty starts at the water's edge, beyond them.
    quayside = unary_union([s_.buffer(0.12 if c == "avenue" else 0.09) for s_, c in streets]
                           + [s_.buffer(0.15) for s_ in secondary]
                           + [ln.buffer(0.24 if c == "highway" else 0.2 if c == "primary" else 0.15) for ln, c, _ in named
                              if c != "rail"]).buffer(0.03)
    bridge_zone = unary_union([ln.buffer(0.6) for ln, _ in bridges]) if bridges else Point(0, 0).buffer(0)
    channels = []
    for f in src.get("ferries", []):
        for stop in f.get("stops") or []:
            here = Point(stop)
            shore = shapely.ops.nearest_points(here, land.boundary)[1]
            dx, dy = here.x - shore.x, here.y - shore.y
            if land.contains(here):
                dx, dy = -dx, -dy
            n = math.hypot(dx, dy) or 1
            channels.append(LineString([(here.x - dx / n * 0.4, here.y - dy / n * 0.4),
                                        (here.x + dx / n * 3.0, here.y + dy / n * 3.0)]).buffer(0.7))
            channels.append(here.buffer(1.3))
    slips = unary_union(channels or [Point(0, 0).buffer(0)])
    bridge_zone = bridge_zone.union(slips)
    for zone in src["piers"]:
        (x0, y0), (x1, y1) = zone["box"]
        r = random.Random(zone["name"])
        frame = box(x0, y0, x1, y1)
        # The dock's one direction: the outward normal its quay mostly faces.
        sx = sy = 0.0
        for edge in lines_of(land.boundary.intersection(frame)):
            cs = list(edge.coords)
            for (ax_, ay_), (bx_, by_) in zip(cs, cs[1:], strict=False):
                ex, ey = bx_ - ax_, by_ - ay_
                mx_, my_ = (ax_ + bx_) / 2, (ay_ + by_) / 2
                nx0, ny0 = -ey, ex
                if land.contains(Point(mx_ + nx0 * 0.4 / (math.hypot(nx0, ny0) or 1), my_ + ny0 * 0.4 / (math.hypot(nx0, ny0) or 1))):
                    nx0, ny0 = -nx0, -ny0
                sx, sy = sx + nx0, sy + ny0
        dock_n = math.hypot(sx, sy) or 1
        dock_dir = (sx / dock_n, sy / dock_n)
        for edge in lines_of(land.boundary.intersection(frame)):
            pos = 0.5
            while pos < edge.length - 0.5:
                a, p_, b = (edge.interpolate(max(0.0, pos - 0.35)), edge.interpolate(pos),
                            edge.interpolate(min(edge.length, pos + 0.35)))
                h1, h2 = math.atan2(p_.y - a.y, p_.x - a.x), math.atan2(b.y - p_.y, b.x - p_.x)
                bend = abs((h2 - h1 + math.pi) % (2 * math.pi) - math.pi)
                if bend > math.radians(14):
                    pos += 0.3
                    continue
                dx, dy = b.x - a.x, b.y - a.y
                n = math.hypot(dx, dy) or 1
                nx, ny = -dy / n, dx / n
                if land.contains(Point(p_.x + nx * 0.4, p_.y + ny * 0.4)):
                    nx, ny = -nx, -ny
                if nx * dock_dir[0] + ny * dock_dir[1] < math.cos(math.radians(28)):
                    pos += 0.3
                    continue                    # this stretch faces elsewhere: no jetty fanning off it
                nx, ny = dock_dir                # every jetty in the dock parallel, as built
                length = zone["length"] * r.uniform(0.8, 1.1)
                pier = LineString([(p_.x - nx * 0.02, p_.y - ny * 0.02),
                                   (p_.x + nx * length, p_.y + ny * length)]).buffer(0.12, cap_style="flat")
                pier = max(polys_of(pier.difference(quayside)), key=lambda g: g.area, default=pier)
                if (pier.intersection(land).area < 0.03 and not pier.intersects(bridge_zone)
                        and not any(pier.buffer(0.12).intersects(o) for o, _ in candidate_piers)):
                    shed = None
                    if length > 1.3 and r.random() < 0.65:
                        shed = (LineString([(p_.x + nx * 0.25, p_.y + ny * 0.25),
                                            (p_.x + nx * (length - 0.15), p_.y + ny * (length - 0.15))]).buffer(0.085, cap_style="flat"),
                                r.choice((10, 11, 13)))
                    candidate_piers.append((pier, shed))
                pos += r.uniform(0.9, 1.25)

    # Ferries between their piers and ships in from the sea, by way of the water.
    for pier, shed in sorted(candidate_piers, key=lambda ps: -ps[0].area):
        if any(pier.buffer(0.08).intersects(o) for o in placed_piers):
            continue
        building(pier, 2, "~pier")
        placed_piers.append(pier)
        if shed is not None and pier.buffer(0.01).contains(shed[0]):
            building(shed[0], shed[1], "~works", base=2)
    # Rocks and islets in the open water: the Devil's Teeth off Blackgate, the Gull
    # Islets under the north shore, the Sentinel Rocks out at the harbour mouth.
    islets, ir = [], random.Random(4700)
    lines_kept_clear = unary_union([ln.buffer(1.0) for ln, _ in bridges] or [Point(0, 0).buffer(0)])
    def tooth(x, y, angle, size):
        """A sharp sliver of rock, narrow and pointed, like a fang."""
        return affinity.rotate(Polygon([(x - size * 0.35, y + size), (x, y - size * 1.2), (x + size * 0.35, y + size)]),
                               angle, origin=(x, y))
    shapes = {
        # A curving row of sharp, narrow rocks — the reef that gave the Teeth their name.
        "The Devil's Teeth": lambda cx, cy: [(tooth(cx + math.cos(t) * 1.2, cy + math.sin(t) * 0.6, math.degrees(t) + 90,
                                                    ir.uniform(0.09, 0.16)), ir.choice((7, 9, 12)))
                                              for t in [math.radians(200 + k * 24) for k in range(7)]],
        # Low, round islets where the gulls sit.
        "Gull Islets": lambda cx, cy: [(blob(cx + dx, cy + dy, r, 4900 + int(dx * 10)), 2)
                                       for dx, dy, r in ((-0.6, 0.1, 0.28), (0.1, -0.3, 0.22), (0.7, 0.25, 0.18), (0.2, 0.55, 0.12))],
        # Lone sea stacks standing up out of the water, like sentries.
        "Sentinel Rocks": lambda cx, cy: [(disc(cx + dx, cy + dy, r), h)
                                          for dx, dy, r, h in ((0.0, 0.0, 0.11, 34), (0.8, 0.45, 0.08, 26), (-0.7, 0.6, 0.07, 22))],
    }
    for name, (cx, cy) in (("The Devil's Teeth", (79.0, 124.0)), ("Gull Islets", (40.0, 13.0)), ("Sentinel Rocks", (40.0, 139.0))):
        made = [(g, h) for g, h in shapes[name](cx, cy)
                if g.distance(land) > 0.5 and not g.intersects(lines_kept_clear)]
        for g, h in made:
            add(g, "land", n=name, k="islet")
            building(g.buffer(-0.02) if g.area > 0.02 else g, h, "~rock")
        if made:
            add(Point(cx, cy + 1.3), "water_label", n=name, r=0, s=1)
        islets += [g for g, _ in made]
    slips_built = [g for g, layer in grounds if layer == "pier"]
    obstacles = land.union(unary_union(islets).buffer(0.2)) if islets else land
    waters = Waters(obstacles, unary_union(placed_piers + slips_built) if (placed_piers or slips_built) else None)
    obstacles = obstacles.union(unary_union(placed_piers + slips_built)) if (placed_piers or slips_built) else obstacles
    for ferry in src.get("ferries", []):
        stops = ferry.get("stops") or [ferry["line"][0], ferry["line"][-1]]
        pts, marks = [], [0.0]
        for a, b in zip(stops, stops[1:], strict=False):
            leg = waters.route(a, b)
            if os.environ.get("DEBUG_FERRY"):
                core = substring(LineString(leg), 0.6, max(0.6, LineString(leg).length - 0.6))
                print("FERRY", ferry["name"], a, b, len(leg), "crosses:", round(core.intersection(obstacles).length, 2),
                      [tuple(round(v, 1) for v in c) for c in leg[:3]], [tuple(round(v, 1) for v in c) for c in leg[-3:]])
            pts += leg if not pts else leg[1:]
            marks.append(LineString(pts).length)
        total = LineString(pts).length
        add(LineString(pts), "ferry", n=ferry["name"], st=[round(m / total, 4) for m in marks])
    for lane in src.get("shipping", []):
        add(LineString(waters.route(lane["from"], lane["to"], berth=True)), "lane", n=lane["name"])
    if air:
        # The airport: runways with their markings and lights, the taxiways,
        # aprons, the terminal and its gates, the car parks and the cargo side.
        for a, b in src["airport"]["runways"]:
            strip = LineString([a, b])
            add(strip.buffer(0.42, cap_style="flat"), "runway")
            add(strip, "runway_line")
        for g, layer in air["ground"]:
            add(g, layer)
        for g, top, kind, base in air["buildings"]:
            building(g, top, kind, base=base)
        for lot, angle in air["lots"]:
            add(lot, "lot")
            for row in bays(lot, angle):
                add(row, "bay")
        for gx, gy, heading in air["gates"]:
            add(Point(gx, gy), "gate", r=heading)

    # Container yards at the docks: their own ground, boxes stacked in rows.
    yards = []
    roadbed = unary_union([s_.buffer(0.12 if c == "avenue" else 0.09) for s_, c in streets]
                          + [s_.buffer(0.15) for s_ in secondary]
                          + [ln.buffer(0.24 if c == "highway" else 0.2 if c == "primary" else 0.15) for ln, c, _ in named])
    for i, yard in enumerate(src.get("yards", [])):
        ground = (blob(yard["at"][0], yard["at"][1], yard["r"], 1100 + i).intersection(land)
                  .difference(water_cut).difference(roadbed))
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
        tiers = landmark_shapes(name, *footprint_at(p))
        kinds = landmark_kinds(name, *footprint_at(p)) or ["~landmark"] * len(tiers)
        for (shape, base, top), kind in zip(tiers, kinds, strict=True):
            building(shape, top, kind, base=base)
        if tiers:
            reserved.append(unary_union([t[0] for t in tiers]).buffer(0.4 if name not in COMPLEXES else 0.18))
    reserved.append(ground_block.buffer(0.05))
    # Arkham behind its wall, a gate where the drive comes in; Blackgate's outer fence.
    for pl in src["places"]:
        if pl["name"] not in ("Arkham Asylum", "Blackgate Penitentiary"):
            continue
        foot = unary_union([t[0] for t in landmark_shapes(pl["name"], *footprint_at(pl))])
        arkham_ = pl["name"] == "Arkham Asylum"
        if arkham_:
            # The asylum's gate, as the game has it: a gatehouse astride the drive from
            # the bridge, wall running off either side a little way — no ring wall.
            drives = [ln for ln, c, _n in named if c == "drive" and ln.distance(foot) < 1.6]
            if drives:
                drive_ = min(drives, key=lambda ln: ln.distance(Point(pl["x"], pl["y"])))
                at_ = drive_.interpolate(drive_.project(foot.centroid) - 1.0 if drive_.project(foot.centroid) > 1.0 else 0.5)
                ahead_ = drive_.interpolate(drive_.project(at_) + 0.1)
                hx, hy = ahead_.x - at_.x, ahead_.y - at_.y
                hn = math.hypot(hx, hy) or 1
                px_, py_ = -hy / hn, hx / hn
                for side in (-1, 1):
                    building(square(at_.x + px_ * 0.22 * side, at_.y + py_ * 0.22 * side, 0.15), 16, "~old")
                    run_ = LineString([(at_.x + px_ * 0.3 * side, at_.y + py_ * 0.3 * side),
                                       (at_.x + px_ * 1.4 * side, at_.y + py_ * 1.4 * side)]).buffer(0.035)
                    run_ = run_.difference(roadbed.buffer(0.14))
                    building(run_, 7, "~old")
                    reserved.append(run_.buffer(0.15))
                building(LineString([(at_.x - px_ * 0.3, at_.y - py_ * 0.3), (at_.x + px_ * 0.3, at_.y + py_ * 0.3)])
                         .buffer(0.05), 15, "~old", base=10)         # the arch over the drive
            continue
        ring = foot.convex_hull.buffer(0.75).buffer(-0.3).buffer(0.3)
        line_ = ring.exterior
        # A gate wherever a road or drive passes through it — with a gatehouse.
        gates = line_.intersection(roadbed.buffer(0.05))
        wall = line_.buffer(0.035).difference(roadbed.buffer(0.14))
        building(wall, 7 if arkham_ else 5, "~old" if arkham_ else "~works")
        if arkham_:
            for g_ in (gates.geoms if hasattr(gates, "geoms") else [gates]):
                if not g_.is_empty:
                    c_ = g_.centroid
                    for side in (-0.34, 0.34):
                        q = line_.interpolate(line_.project(c_) + side)
                        lodge = square(q.x, q.y, 0.12)
                        if not lodge.intersects(roadbed.buffer(0.04)):
                            building(lodge, 11, "~old")
            for f_ in (0.125, 0.375, 0.625, 0.875):
                q = line_.interpolate(f_, normalized=True)
                tower = square(q.x, q.y, 0.13)
                if not tower.intersects(roadbed.buffer(0.05)):
                    building(tower, 15, "~old")
        reserved.append(wall.buffer(0.15))
    rk = random.Random(4400)
    keep_water_clear = unary_union([ln.buffer(0.45) for ln, _ in bridges] or [Point(0, 0).buffer(0)])
    for isle, every, reach in (("Arkham Island", 1.6, (0.35, 1.1)), ("Paris Island", 1.4, (0.3, 0.8))):
        if areas.get(isle) is None:
            continue
        edge = areas[isle].exterior
        d_ = rk.uniform(0, every)
        while d_ < edge.length:
            q = edge.interpolate(d_)
            ahead = edge.interpolate(min(edge.length, d_ + 0.1))
            nx_, ny_ = -(ahead.y - q.y), ahead.x - q.x
            n_ = math.hypot(nx_, ny_) or 1
            nx_, ny_ = nx_ / n_, ny_ / n_
            if areas[isle].contains(Point(q.x + nx_ * 0.3, q.y + ny_ * 0.3)):
                nx_, ny_ = -nx_, -ny_
            out_ = rk.uniform(*reach)
            cx_, cy_ = q.x + nx_ * out_, q.y + ny_ * out_
            r_ = rk.uniform(0.05, 0.12)
            jag = Polygon([(cx_ + math.cos(a) * r_ * rk.uniform(0.55, 1.25), cy_ + math.sin(a) * r_ * rk.uniform(0.55, 1.25))
                           for a in [k * 2 * math.pi / 7 + rk.uniform(-0.3, 0.3) for k in range(7)]]).buffer(0)
            if not jag.intersects(land) and not jag.intersects(keep_water_clear):
                building(jag, rk.choice((3, 4, 6, 8)), "~rock")
            d_ += every * rk.uniform(0.5, 1.5)
    for g, layer in grounds:
        if layer in ("plaza", "works", "grounds", "marsh"):
            add(g, {"plaza": "plaza", "works": "works", "grounds": "plaza", "marsh": "marsh"}[layer])
        elif layer == "pool":
            add(g, "water", n="")
        elif layer == "pier":
            add(g, "pier")
        elif layer == "lot":
            add(g, "lot")
            for row in bays(g, 0):
                add(row, "bay")
        elif layer == "path":
            add(g, "path")
        elif layer.startswith("pitch:"):
            part = layer.split(":", 1)[1]
            add(g, "pitch_line" if part == "line" else "pitch", k=part)
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
    building_sites = []
    lots = []           # (district, centroid, height, area) — for venues and rooftops
    roofs = {}          # centroid -> the footprint itself, so what sits on a roof stays on it
    yard_trees = []     # suburban yards, planted with the parks below
    for i, (name, area) in enumerate(areas.items()):
        g = grid_of[name]
        r = random.Random(500 + i)
        if name in SPARSE or name == "Justice Island":
            continue
        ground = area.difference(park_union).difference(water_cut).difference(cuts)
        ground = ground.difference(land.boundary.buffer(0.3))
        suburb = name in SUBURBS
        for blk in polys_of(ground):
            if blk.area < 0.08:
                continue
            most = 1.6 if g.get("industrial") else 0.6 if g["tall"] >= 30 else 0.26 if suburb else 0.45
            for lot in subdivide(blk, most, r):
                # A house sits back on its lot, with a yard round it; in town a
                # building fills its lot to the pavement.
                foot = lot.buffer(-0.1 if suburb else -0.06, join_style="mitre")
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
                    if suburb:
                        h = r.choice((5, 6, 6, 7, 8, 9))    # two storeys, three at most
                    # Some windows lit, some dark: the city at night — and each
                    # quarter's own stuff: stone, glass, steel.
                    h = round(max(4, min(260, h)))
                    if name in GROWING and piece.area > 0.22 and len(building_sites) < 12 and r.random() < 0.03:
                        building_sites.append(piece)          # cleared for something new: a site, not a building
                        continue
                    style = ("~lit" if r.random() < 0.14 else "~works" if g.get("industrial")
                             else "~glass" if name in GLASS and h >= 40 else "~old" if name in OLD_QUARTERS
                             else "~house" if suburb else "")
                    building(piece.simplify(0.04), h, style)
                    lots.append((name, piece.centroid, h, piece.area))
                    roofs[id(lots[-1][1])] = piece
                    count += 1
                    if suburb and r.random() < 0.7:
                        # A tree in the yard, behind the house.
                        yard = lot.difference(piece.buffer(0.05))
                        if not yard.is_empty and yard.area > 0.02:
                            spot = yard.representative_point()
                            yard_trees.append((spot.x, spot.y))

    # Houses along the lanes out in the sprawl: low, scattered, fewer the further out.
    hr = random.Random(1700)
    keep_clear = unary_union([cuts, park_union.buffer(0.1), water_cut.buffer(0.15), airfield,
                              unary_union(lanes).buffer(0.1) if lanes else Point(0, 0).buffer(0)])
    houses = 0
    town_built = shapely.STRtree(list(built))      # the towns' own buildings, which the sprawl keeps clear of
    near_houses = {}
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
                if (open_land.contains(house) and not house.intersects(keep_clear)
                        and not town_built.query(house.buffer(0.03), predicate="intersects").size
                        and not any(house.buffer(0.03).intersects(o) for o in near_houses.get((round(c.x), round(c.y)), []))):
                    building(house, hr.choice((4, 4, 5, 6, 7, 9)), "~lit" if hr.random() < 0.18 else "")
                    near_houses.setdefault((round(c.x), round(c.y)), []).append(house)
                    for ddx in (-1, 0, 1):
                        for ddy in (-1, 0, 1):
                            if ddx or ddy:
                                near_houses.setdefault((round(c.x) + ddx, round(c.y) + ddy), []).append(house)
                    houses += 1
            pos += hr.uniform(0.32, 0.55)

    # On the roofs: water towers on the mid-rises, helipads on the tallest.
    rr = random.Random(1300)
    for _d, c, h, area in lots:
        if 14 <= h <= 70 and area > 0.08 and rr.random() < 0.07:
            tank = disc(c.x + rr.uniform(-0.05, 0.05), c.y + rr.uniform(-0.05, 0.05), 0.055)
            if roofs[id(c)].buffer(-0.01).contains(tank):         # on the roof, not over its edge
                building(tank, h + 7, "~tank", base=h)
    for _d, c, h, area in sorted(lots, key=lambda lot: -lot[2])[:36]:
        pad = disc(c.x, c.y, 0.17)
        if area > 0.12 and roofs[id(c)].buffer(-0.01).contains(pad):
            building(pad, h + 0.8, "~pad", base=h)
    # Building sites with their cranes, where the city is still growing: each on
    # its own cleared lot, its jib swung where it passes over nothing at all.
    footprints_now = shapely.STRtree(built)
    for site in building_sites:
        add(site, "site")
        c = site.representative_point()
        mast = rr.randint(55, 90)
        clear_jib = None
        for reach in (0.95, 0.7, 0.45):
            for jib in sorted(range(0, 360, 15), key=lambda _a: rr.random()):
                boom = affinity.rotate(box(c.x - 0.22, c.y - 0.025, c.x + reach, c.y + 0.025), jib, origin=(c.x, c.y))
                if not footprints_now.query(boom, predicate="intersects").size and not boom.intersects(cuts):
                    clear_jib = boom
                    break
            if clear_jib is not None:
                break
        if clear_jib is None:
            continue
        building(square(c.x, c.y, 0.06), mast, "~crane")
        building(clear_jib, mast - 2, "~crane", base=mast - 5)

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
        "school": (["Gotham Heights High", "Robinson High", "PS 117", "St. Mary's School", "Cherry Hills Prep"],
                   {"Upper West Side": 1, "Coventry": 1, "Cherry Hills": 1, "Kane Heights": 1, "Bristol": 1}),
        # And how people live between the work and the night: coffee, the gym,
        # a film, a room for the night, something to buy.
        "cafe": (["Common Grounds", "The Daily Grind", "Kettle & Crow", "Grindhouse Coffee", "Perk", "The Roastery",
                  "Black Cat Café", "Steam", "Cup of Joe's", "Moka", "Third Wave", "Café Lune", "Drip", "Pour House"],
                 {"Burnside": 3, "University District": 2, "Old Gotham": 1, "Upper West Side": 1, "Fashion District": 1,
                  "Diamond District": 1, "Coventry": 1, "Cherry Hills": 1, "Avalon Heights": 1, "Halyard Square": 1}),
        "gym": (["Iron Works Gym", "Knuckle House", "Peak Fitness", "Southpaw Boxing", "Wildcat Gym", "The Body Shop",
                 "Crossfire"], {"Burnside": 1, "New Town": 1, "Upper East Side": 1, "Tricorner": 1, "Fashion District": 1,
                               "Central Business District": 1}),
        "cinema": (["The Paramount", "Odeon Gotham", "Starlite Drive-In", "The Bijou", "Cineplex 12", "The Rex", "Majestic"],
                   {"Diamond District": 1, "New Town": 1, "Burnside": 1, "Fashion District": 1, "Old Gotham": 1,
                    "Kane Heights": 1, "Halyard Square": 1}),
        "hotel": (["The Regency", "Hotel Ventura", "The Excelsior", "Harbor Inn", "Hotel Noir", "The Belvedere",
                   "The Ashcroft", "Night & Day Motel"], {"Diamond District": 2, "Fashion District": 1, "Upper East Side": 1,
                                                          "City Hall District": 1, "Old Gotham": 1,
                                                          "Central Business District": 1, "New Town": 1}),
        "shop": (["Vintage Vault", "Gotham Books", "The Record Room", "Ellison's Jewelers", "Rook & Pawn",
                  "Bluebird Boutique", "Madame Lacroix", "Second Story Books", "Hart & Sons Tailors"],
                 {"Fashion District": 3, "Burnside": 2, "Diamond District": 1, "Upper East Side": 1, "Old Gotham": 1,
                  "Chinatown": 1}),
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

    # Where the city plays: ball fields and pitches in the parks, courts in the
    # small ones — none on a pond, a landmark, a path or another field.
    fields = [g for g, layer in grounds if layer.startswith("pitch:") and layer != "pitch:line"]
    fr = random.Random(2100)
    keep_off = unary_union([water_cut, footprints.buffer(0.35), ground_block, unary_union(ponds or [Point(0, 0).buffer(0)]),
                            unary_union([ln.buffer(0.3) for ln, c, _ in named if c != "rail"]),
                            unary_union([Point(pl["x"], pl["y"]).buffer(0.7) for pl in src["places"]])])

    def place_fields(area, kinds):
        minx, miny, maxx, maxy = area.bounds
        inner = area.buffer(-0.25)
        for kind in kinds:
            for _ in range(160):
                cx, cy = fr.uniform(minx, maxx), fr.uniform(miny, maxy)
                if not inner.contains(Point(cx, cy)):
                    continue
                parts = pitch(kind, cx, cy, fr.uniform(0, 180))
                turf = unary_union([g for g, k in parts if k != "line"])
                if (inner.contains(turf) and not turf.buffer(0.2).intersects(keep_off)
                        and not any(turf.buffer(0.25).intersects(f) for f in fields)):
                    for g, k in parts:
                        add(g, "pitch_line" if k == "line" else "pitch", k=k)
                    fields.append(turf)
                    # A footpath to it from the park's drive, round no pond.
                    drives = [ln for ln, c, nm in named if c in ("secondary", "primary") and ln.distance(turf) < 3.0]
                    if drives:
                        near = min(drives, key=lambda ln: ln.distance(turf))
                        a_, b_ = shapely.ops.nearest_points(turf.buffer(0.05), near)
                        walk = LineString([a_, b_])
                        if not walk.intersects(water_cut) and not walk.intersects(keep_off.difference(near.buffer(0.35))):
                            add(walk, "path")
                    break
    plan = {"Robinson Park": ["baseball", "baseball", "soccer", "soccer", "tennis", "basketball"],
            "Burnside Park": ["soccer", "baseball", "basketball"], "Old Gotham Common": ["basketball", "tennis"]}
    for park, name in zip(parks, park_names, strict=True):
        if name in plan:
            place_fields(park, plan[name])
        elif not name and park.area > 1.2:
            place_fields(park, [fr.choice(["soccer", "basketball", "baseball", "tennis", "basketball"])])
    # The country club: nine fairways back and forth across it, greens, bunkers,
    # tees and the clubhouse — the woods left standing between the holes.
    if golf is not None:
        gr = random.Random(2200)
        minx, miny, maxx, maxy = golf.bounds
        inner = golf.buffer(-0.3)
        x, row = minx + 0.9, 0
        while x < maxx - 0.7 and row < 9:
            top, bottom = miny + 0.7, maxy - 0.7
            tee = (x, (top if row % 2 == 0 else bottom) + gr.uniform(-0.15, 0.15))
            green = (x + gr.uniform(0.35, 0.8), (bottom if row % 2 == 0 else top) + gr.uniform(-0.25, 0.25))
            fairway = LineString([tee, green]).buffer(0.17).intersection(inner)
            if not fairway.is_empty and fairway.area > 0.3:
                add(fairway, "golf", k="fairway")
                add(disc(green[0], green[1], 0.11), "golf", k="green")
                add(square(tee[0], tee[1], 0.08), "golf", k="tee")
                for _ in range(2):
                    b = blob(green[0] + gr.uniform(-0.28, 0.28), green[1] + gr.uniform(-0.22, 0.22), 0.07, 2300 + row)
                    if not b.intersects(disc(green[0], green[1], 0.12)):
                        add(b, "golf", k="bunker")
                fields.append(fairway.union(disc(green[0], green[1], 0.2)))
            x += 1.08
            row += 1
        club = golf.representative_point()
        ex = golf.bounds[2] - 0.6
        building(rect(ex, club.y, 0.5, 0.26), 11, "~old")
        fields.append(rect(ex, club.y, 0.9, 0.6))

    # Trees, through the parks — wholly inside them, crown and all, so none
    # leans out over a pavement or a roof at the park's edge.
    trees = []
    tr = random.Random(77)
    fields_union = shapely.prepared.prep(unary_union(fields).buffer(0.16)) if fields else None
    pools = [g for g, layer in grounds if layer == "pool"]
    wet_spots = shapely.prepared.prep(unary_union(ponds + pools).buffer(0.13)) if (ponds or pools) else None
    # Clear of every road by its width and a whole crown — a tree placed by its
    # trunk alone still spread over the kerb — and of every lane and the boardwalk.
    crown = 0.14
    paths = unary_union([ln.buffer(0.22 + crown) for ln, c, _ in named]
                        + [s_.buffer(0.18 + crown) for s_ in secondary]
                        + [s_.buffer(0.14 + crown) for s_, _ in streets] + [ln.buffer(0.25 + crown) for ln, _ in bridges]
                        + [s_.buffer(0.1 + crown) for s_ in lanes] + reserved)
    paths = shapely.prepared.prep(paths)
    arkham_lawns = shapely.prepared.prep(areas.get("Arkham Island", Point(0, 0).buffer(0)))
    justice_lawns = shapely.prepared.prep(areas.get("Justice Island", Point(0, 0).buffer(0)))
    for park in polys_of(park_union):
        minx, miny, maxx, maxy = park.bounds
        inside = shapely.prepared.prep(park.buffer(-0.13))
        step = 0.34
        y = miny
        while y < maxy:
            x = minx
            while x < maxx:
                px, py = x + tr.uniform(-0.12, 0.12), y + tr.uniform(-0.12, 0.12)
                pt = Point(px, py)
                density = 0.72
                if arkham_lawns.contains(pt):
                    density = 0.3
                elif justice_lawns.contains(pt):
                    density = 0.18
                if tr.random() < density and inside.contains(pt) and not water_cut.contains(pt) \
                        and not paths.contains(pt) and not (fields_union and fields_union.contains(pt)) \
                        and not (wet_spots and wet_spots.contains(pt)):
                    trees.append([round(px * 100), round(py * 100), tr.randint(7, 19), tr.randint(6, 13)])
                x += step
            y += step
    # Tree-lined streets where the city is leafy: the suburbs, the brownstone
    # quarters, the boulevards — every tree on the pavement, clear of the road,
    # the houses and each other.
    leafy = SUBURBS
    footprint_tree = shapely.STRtree(built)
    road_room = shapely.prepared.prep(cuts.buffer(0.02))
    sr_ = random.Random(91)
    planted = 0
    for name, area in areas.items():
        if name not in leafy:
            continue
        here = shapely.prepared.prep(area.buffer(-0.1))
        for line, cls in streets:
            if not here.contains(line.interpolate(0.5, normalized=True)):
                continue
            off = 0.2 if cls == "avenue" else 0.16
            for side in (off, -off):
                edge = line.offset_curve(side)
                if edge.is_empty:
                    continue
                d_ = sr_.uniform(0.04, 0.1)
                while d_ < edge.length:
                    q = edge.interpolate(d_)
                    r = sr_.randint(4, 6)
                    crown = q.buffer(r / 100)
                    if (here.contains(q) and not road_room.intersects(crown) and not water_cut.contains(q)
                            and not footprint_tree.query(crown, predicate="intersects").size):
                        trees.append([round(q.x * 100), round(q.y * 100), sr_.randint(6, 10), r])
                        planted += 1
                    d_ += sr_.uniform(0.24, 0.34)
    print(f"{planted} street trees")
    for x, y in yard_trees:
        h, r = tr.randint(6, 12), tr.randint(5, 9)
        crown = Point(x, y).buffer(r / 100)
        if (not paths.contains(Point(x, y)) and not road_room.intersects(crown)
                and not footprint_tree.query(crown, predicate="intersects").size):
            trees.append([round(x * 100), round(y * 100), h, r])

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

    # The network for getting about: every road but the railways, the bridges, the ferries.
    graph = road_graph([(ln, c) for ln, c, _ in named if c != "rail"] + [(s_, "secondary") for s_ in secondary]
                       + [(s_, c) for s_, c in streets] + [(s_, "lane") for s_ in lanes]
                       + [(ln, "bridge") for ln, _ in bridges] + [(ln, "street") for ln in unseen_joins],
                       [(f["name"], f.get("stops") or [f["line"][0], f["line"][-1]]) for f in src.get("ferries", [])],
                       src["places"], land)
    ROADS.write_text(json.dumps(graph, ensure_ascii=False, separators=(",", ":")))
    print(f"road network: {len(graph['nodes'])} junctions, {len(graph['edges'])} roads ({ROADS.stat().st_size / 1e6:.2f} MB)")
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
