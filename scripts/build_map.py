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
    raw = []
    for family, (deg, spacing, staggered) in enumerate((
            (angle, block, False), (angle + 90, block * r.uniform(1.45, 1.85), True))):
        a = math.radians(deg)
        ux, uy = math.cos(a), math.sin(a)
        vx, vy = -uy, ux
        t = -reach
        while t < reach:
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
                        raw.append(substring(line, pos, min(length, pos + seg)))
                    pos += seg
            else:
                raw.append(line)
            t += spacing * r.uniform(0.84, 1.16)
    streets = []
    for line in raw:
        for part in lines_of(line.intersection(area)):
            if part.length > 0.35:
                streets.append(part)
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


# --- the city ----------------------------------------------------------------------

def build():
    src = json.loads(SOURCE.read_text())
    features, buildings = [], []

    def building(geom, h, landmark=""):
        for poly in polys_of(shapely.set_precision(geom, 0.01)):
            ring = list(poly.exterior.coords)[:-1]
            if len(ring) >= 3:
                row = [int(h)] + [round(v * 100) for pt in ring for v in pt]
                buildings.append(row + ([landmark] if landmark else []))

    def add(geom, layer, **props):
        if geom is None or geom.is_empty:
            return
        g = shapely.set_precision(geom, 0.01)
        if g.is_empty:
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
    park_union = unary_union(parks)

    # Districts: the cells around their centres, clipped to the land they're on.
    ds = src["districts"]
    island_ds = [d for d in ds if "limit" not in d and "city" not in d]
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
    areas["The Narrows"] = others["The Narrows"]
    areas["Blackgate Isle"] = others["Blackgate Isle"]
    areas["Paris Island"] = others["Paris Island"]
    grid_of = {d["name"]: d for d in ds}
    grid_of.setdefault("The Narrows", {"angle": 22, "block": 0.8, "tall": 12, "warp": 0.4})
    grid_of.setdefault("Blackgate Isle", {"angle": 8, "block": 2.2, "tall": 12})
    grid_of.setdefault("Paris Island", {"angle": -4, "block": 2.4, "tall": 6})

    # Waterfront park along the shore, wherever there aren't docks.
    shore = island_land.difference(island_land.buffer(-0.75))
    for name, area in areas.items():
        if name in NO_WATERFRONT or name not in {d["name"] for d in island_ds}:
            continue
        parks.append(shore.intersection(area))
    park_union = unary_union(parks)
    for p, spec in zip(parks, src["parks"] + [{"name": ""}] * (len(parks) - len(src["parks"])), strict=True):
        add(p, "park", n=spec["name"])

    outer = {d["name"] for d in ds if "limit" in d or "city" in d}
    for i, (name, area) in enumerate(areas.items()):
        # Mainland districts melt into the land around them; their streets give them shape.
        add(area, "district", n=name, t=i % 12, **({"k": "outer"} if name in outer else {}))

    # Streets, district by district.
    streets = []
    for i, (name, area) in enumerate(areas.items()):
        g = grid_of[name]
        open_ground = area.difference(park_union).difference(water_cut).buffer(-0.12)
        streets += street_grid(open_ground, g["angle"], g["block"], g.get("warp", 0), 100 + i)
    streets = [s.simplify(0.02) for s in streets]

    # The borders between districts are avenues; the coast isn't.
    borders = unary_union([a.boundary for n, a in areas.items() if n in {d["name"] for d in island_ds}
                           or grid_of.get(n, {}).get("city")])
    borders = borders.difference(land.boundary.buffer(0.55)).difference(water_cut.buffer(0.2))
    # Softened a little, so district borders read as avenues that bend, not ruled lines.
    secondary = [LineString(chaikin(list(s.coords), 2, closed=False)).simplify(0.03)
                 for s in lines_of(shapely.line_merge(borders)) if s.length > 0.6]

    named = []
    for road in src["roads"]:
        line = curve(road["line"])
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
    bridges = [(curve(b["line"], 2), b["name"]) for b in src["bridges"]]

    for s in streets:
        add(s, "road", c="street")
    for s in secondary:
        add(s, "road", c="secondary")
    for line, cls, name in named:
        add(line, "road", c=cls, n=name)
    for line, name in bridges:
        add(line, "road", c="bridge", n=name)

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

    # Buildings: the blocks between the streets, cut into lots, given heights.
    cuts = unary_union([s.buffer(0.085, quad_segs=2) for s in streets]
                       + [s.buffer(0.15, quad_segs=2) for s in secondary]
                       + [ln.buffer(0.24 if c == "highway" else 0.2 if c == "primary" else 0.15, quad_segs=2)
                          for ln, c, _ in named if c != "rail"]
                       + [ln.buffer(0.22) for ln, _ in bridges])
    tower = next(p for p in src["places"] if p["name"] == "Wayne Tower")
    core = Point(tower["x"], tower["y"])
    count = 0
    for i, (name, area) in enumerate(areas.items()):
        g = grid_of[name]
        r = random.Random(500 + i)
        if name in SPARSE:
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
                    pull = 1 + 2.2 * math.exp(-piece.centroid.distance(core) ** 2 / 90)
                    h = g["tall"] * r.lognormvariate(0, 0.5) * (pull if g["tall"] >= 30 else 1)
                    building(piece.simplify(0.04), round(max(4, min(240, h))))
                    count += 1
    # A few buildings that are themselves landmarks.
    for name, size, h in (("Wayne Tower", 0.75, 330), ("The Clocktower", 0.4, 118),
                          ("GCPD Central", 0.8, 60), ("Arkham Asylum", 1.3, 34),
                          ("Blackgate Penitentiary", 1.6, 30), ("Knightsdome", 1.8, 46)):
        p = next(q for q in src["places"] if q["name"] == name)
        shape = (Point(p["x"], p["y"]).buffer(size / 2, 24) if name == "Knightsdome"
                 else box(p["x"] - size / 2, p["y"] - size / 2, p["x"] + size / 2, p["y"] + size / 2))
        building(shape, h, name)

    # Labels and landmarks.
    for p in src["places"]:
        add(Point(p["x"], p["y"]), "place", n=p["name"], k=p["kind"], i=p.get("icon", ""), a=p["area"])
    for w in src["water"]:
        add(Point(w["at"]), "water_label", n=w["name"], r=w.get("rotate", 0))
    for name, at in (("Gotham County", (-6, 50)), ("Blackgate Isle", (69, 126.6)), ("Blüdhaven", (121, -22)),
                     ("Gotham", (52, 2))):
        add(Point(at), "area_label", n=name)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"type": "FeatureCollection", "features": features},
                              ensure_ascii=False, separators=(",", ":")))
    BUILDINGS.write_text(json.dumps(buildings, ensure_ascii=False, separators=(",", ":")))
    print(f"{len(features)} features ({OUT.stat().st_size / 1e6:.2f} MB), {len(buildings)} buildings "
          f"({BUILDINGS.stat().st_size / 1e6:.2f} MB)")


if __name__ == "__main__":
    build()
