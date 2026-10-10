"""
Where he is — Bruce himself, on the map like the rest of them.

He starts the night at the Manor. Sent somewhere — a place, a report off the
scanner — he goes there the way he would: by day in the car, by night in the
Batmobile (the roads, and faster than anyone on them) or over the roofs when
it's close. A report he goes to is a case he's on: he joins whoever's working
it, and the odds of it going well are better for it (see engine.outcomes).

The family see where he is the way they'd know it: Alfred and Barbara on their
screens, whoever's on a case with him because he's there.
"""
import json
import math
import threading
import time

from .. import paths
from ..memory.store import atomic_write, read_text
from . import places, travel

HOME = "Wayne Manor"
BATMOBILE = 1.7          # how much faster than the traffic, and nobody stops it
_lock = threading.Lock()


def _path():
    return paths.DATA_DIR / "_bruce.json"


def _load():
    try:
        return json.loads(read_text(_path()) or "{}")
    except ValueError:
        return {}


def night(t=None):
    hour = time.localtime(t or time.time()).tm_hour
    return hour >= 20 or hour < 5


def state(t=None):
    """{"where", "spot": {name, x, y, area}, "route": {...} while he's travelling, "suit": bool, "case": id}."""
    t = t or time.time()
    data = _load()
    where = data.get("where") or HOME
    spot = places.resolve(where) or places.resolve(HOME)
    if data.get("x") is not None:
        spot = {**(spot or {}), "x": data["x"], "y": data["y"], "name": where}
    out = {"where": where, "spot": spot, "suit": night(t), "case": data.get("case") or ""}
    trip = data.get("trip")
    if trip and trip.get("end", 0) > t:
        out["route"] = {k: trip[k] for k in ("pts", "start", "end", "by", "from") if k in trip}
    return out


def position(t=None):
    """Where he is right now: along his route if he's on one."""
    t = t or time.time()
    data = _load()
    trip = data.get("trip")
    if trip and trip.get("start", 0) <= t < trip.get("end", 0):
        return travel.position(trip, t)
    s = state(t)
    return (s["spot"]["x"], s["spot"]["y"]) if s.get("spot") else None


def go(where, x=None, y=None, case="", t=None):
    """
    He sets off for somewhere — a place by name, or a point (a report's). Returns
    the trip, or None if there's nowhere to go. Mid-journey, he turns from where he is.
    """
    t = t or time.time()
    here = position(t)
    there = places.resolve(where) if x is None else {"name": where, "x": x, "y": y}
    if here is None or there is None:
        return None
    suited = night(t)
    if math.dist(here, (there["x"], there["y"])) < 0.6:
        pts, minutes, by = [list(here), [there["x"], there["y"]]], 1.0, "on foot"
    elif (suited and travel._widest_water(travel._load(), here, (there["x"], there["y"])) <= travel.GLIDE
          and math.dist(here, (there["x"], there["y"])) < 7):
        pts, minutes = travel.route(here, (there["x"], there["y"]), None, None, patrol=True)
        by = "over the rooftops"
    else:
        pts, minutes = travel.route(here, (there["x"], there["y"]), None, there.get("name"))
        if suited:
            minutes, by = max(1.0, minutes / BATMOBILE), "in the Batmobile"
        else:
            by = "driving"
    trip = {"to": there["name"], "from": "", "pts": pts, "start": t, "end": t + minutes * 60, "by": by}
    with _lock:
        data = _load()
        data.update({"where": there["name"], "x": there["x"], "y": there["y"], "trip": trip, "case": case})
        atomic_write(_path(), json.dumps(data, ensure_ascii=False))
    return trip


def note(t=None):
    """A line for the ones who'd know where he is — Alfred and Barbara on their screens."""
    t = t or time.time()
    s = state(t)
    if s.get("route"):
        left = max(1, round((s["route"]["end"] - t) / 60))
        return f"Bruce: on his way to {s['where']}, {s['route'].get('by', 'travelling')}, about {left} min out"
    return f"Bruce: at {s['where']}" + (" — suited up" if s["suit"] and s["where"] != HOME else "")
