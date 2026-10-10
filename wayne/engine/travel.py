"""
Getting about Gotham: from one place to another along the roads, over the
bridges, on a ferry where there's no bridge — never a jump across the map.

The streets come from the map's own build: scripts/build_map.py writes
roads.json, every road noded where it meets another, and each place tied to
the nearest junction it can reach without crossing water. A journey is the
quickest way through that network, timed at each road's own speed; someone
on one is somewhere along it at every moment until they arrive.
"""
import heapq
import json
import math
import threading
from functools import lru_cache
from pathlib import Path

# Map units a minute (one is ~150 m): the expressways near 60 km/h, the grid
# under 30 with its lights, a lane slower still.
SPEED = {"highway": 7.0, "bridge": 6.0, "primary": 4.5, "secondary": 4.0, "avenue": 4.0, "street": 3.0,
         "lane": 2.5, "drive": 2.0}
FERRY, FERRY_WAIT = 2.2, 6.0          # a ferry's pace, and the wait for one
ON_FOOT = 0.6                         # the last stretch, from the road to the door
SETTING_OFF = 2.0                     # keys, stairs, the car: minutes before anyone's moving
PATROL = 1.35                         # on patrol they cut across the roofs: quicker than the traffic

_lock = threading.Lock()
_graph = None
_ROADS = Path(__file__).with_name("roads.json")


def _load():
    global _graph
    with _lock:
        if _graph is not None:
            return _graph
        data = json.loads(_ROADS.read_text()) if _ROADS.exists() else {}
        nodes = [tuple(n) for n in data.get("nodes", [])]
        adj = [[] for _ in nodes]
        for edge in data.get("edges", []):
            a, b, cls = edge[0], edge[1], edge[2]
            via = [tuple(p) for p in edge[3]] if len(edge) > 3 else []
            pts = [nodes[a]] + via + [nodes[b]]
            minutes = sum(math.dist(pts[i], pts[i + 1]) for i in range(len(pts) - 1)) / SPEED.get(cls, 3.0)
            adj[a].append((b, minutes, via))
            adj[b].append((a, minutes, via[::-1]))
        for a, b, _name in data.get("ferries", []):
            minutes = math.dist(nodes[a], nodes[b]) / FERRY + FERRY_WAIT
            adj[a].append((b, minutes, []))
            adj[b].append((a, minutes, []))
        _graph = {"nodes": nodes, "adj": adj, "places": data.get("places", {})}
        return _graph


def _nearest(graph, x, y):
    best, found = None, None
    for i, (nx, ny) in enumerate(graph["nodes"]):
        d = (nx - x) ** 2 + (ny - y) ** 2
        if best is None or d < best:
            best, found = d, i
    return found


def _thin(pts, gap=0.12):
    """Fewer points for the page: none closer than `gap` to the last kept, the ends always."""
    out = [pts[0]]
    for p in pts[1:-1]:
        if math.dist(p, out[-1]) >= gap:
            out.append(p)
    return out + [pts[-1]] if len(pts) > 1 else out


@lru_cache(maxsize=1024)
def _route(a, b, name_a, name_b):
    graph = _load()
    if not graph["nodes"]:
        return (a, b), math.dist(a, b) / SPEED["street"]
    s = graph["places"].get(name_a) if name_a else None
    t = graph["places"].get(name_b) if name_b else None
    s = _nearest(graph, *a) if s is None else s
    t = _nearest(graph, *b) if t is None else t
    nodes, adj = graph["nodes"], graph["adj"]
    fastest = max(SPEED.values())
    best, came, heap = {s: 0.0}, {}, [(0.0, s)]
    while heap:
        _, k = heapq.heappop(heap)
        if k == t:
            break
        base = best[k]
        for nxt, minutes, via in adj[k]:
            cost = base + minutes
            if cost < best.get(nxt, 1e18):
                best[nxt], came[nxt] = cost, (k, via)
                heapq.heappush(heap, (cost + math.dist(nodes[nxt], nodes[t]) / fastest, nxt))
    if t != s and t not in came:
        return (a, b), math.dist(a, b) / SPEED["street"]
    path, k = [nodes[t]], t
    while k != s:
        prev, via = came[k]
        path.extend(reversed(via))
        path.append(nodes[prev])
        k = prev
    path.reverse()
    pts = [a] + path + [b]
    minutes = best.get(t, 0.0) + (math.dist(a, nodes[s]) + math.dist(nodes[t], b)) / ON_FOOT
    return tuple(_thin(pts)), minutes


def route(a, b, name_a=None, name_b=None, patrol=False):
    """
    ([(x, y), ...], minutes) from a to b by the quickest way — `name_a` and
    `name_b` the places they are, if they're places, for the right junction.
    On patrol it's quicker, and nobody has to find the car first.
    """
    a = (round(a[0], 2), round(a[1], 2))
    b = (round(b[0], 2), round(b[1], 2))
    pts, minutes = _route(a, b, name_a or "", name_b or "")
    minutes = minutes / PATROL if patrol else minutes + SETTING_OFF
    return [list(p) for p in pts], max(1.0, min(minutes, 75.0))


def position(trip, t):
    """Where someone on `trip` is at time t: (x, y), along its road at an even pace."""
    pts = trip["pts"]
    if len(pts) < 2 or t >= trip["end"]:
        return tuple(pts[-1])
    if t <= trip["start"]:
        return tuple(pts[0])
    legs = [math.dist(pts[i], pts[i + 1]) for i in range(len(pts) - 1)]
    left = (t - trip["start"]) / (trip["end"] - trip["start"]) * sum(legs)
    for i, leg in enumerate(legs):
        if left <= leg:
            f = left / leg if leg else 1.0
            return (pts[i][0] + (pts[i + 1][0] - pts[i][0]) * f, pts[i][1] + (pts[i + 1][1] - pts[i][1]) * f)
        left -= leg
    return tuple(pts[-1])
