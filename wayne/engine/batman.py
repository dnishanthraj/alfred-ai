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
    trip = data.get("trip")
    travelling = bool(trip and trip.get("end", 0) > t)
    follow = data.get("follow") or ""
    if follow and not travelling:
        # With one of them now: where they go, he goes.
        found = _with(follow, t)
        if found:
            spot = {**(spot or {}), "x": found[0], "y": found[1], "name": where}
    # In the suit on a case, any hour, and every night; by day, otherwise, he's Bruce Wayne.
    suited = night(t) or bool(data.get("case"))
    if travelling and "suit" in trip:
        suited = trip["suit"]
    out = {"where": where, "spot": spot, "suit": suited, "case": data.get("case") or "", "follow": follow}
    if travelling:
        out["route"] = {k: trip[k] for k in ("pts", "start", "end", "by", "from", "pickup") if k in trip}
    return out


def position(t=None):
    """Where he is right now: along his route if he's on one (at the pickup, waiting on the Batwing)."""
    t = t or time.time()
    data = _load()
    trip = data.get("trip")
    if trip and trip.get("left", trip.get("start", 0)) <= t < trip.get("end", 0):
        return travel.position(trip, t)
    if data.get("follow"):
        found = _with(data["follow"], t)
        if found:
            return found
    s = state(t)
    return (s["spot"]["x"], s["spot"]["y"]) if s.get("spot") else None


def go(where, x=None, y=None, case="", t=None):
    """
    He sets off for somewhere — a place by name, or a point (a pin, a report's).
    To a case it's Batman, whatever the hour: over the roofs, the Batmobile, or
    the Batwing if it's near and saves real time. Anywhere else it's Batman by
    night and Bruce Wayne by day — the car, or a walk round the corner. Returns
    the trip, or None if there's nowhere to go. Mid-journey, he turns from where he is.
    """
    t = t or time.time()
    here = position(t)
    there = places.resolve(where) if x is None else {"name": where, "x": x, "y": y}
    if here is None or there is None:
        return None
    suited = bool(case) or night(t)
    goal = (there["x"], there["y"])
    if suited:
        trip = travel.fastest("bruce", here, goal, there.get("name"), t, book=True)
    elif math.dist(here, goal) < travel.WALK_UNDER:
        trip = {"pts": [list(here), list(goal)], "start": t, "end": t + max(1.0, math.dist(here, goal) / travel.WALKING) * 60,
                "by": "on foot"}
    else:
        pts, minutes = travel.route(here, goal, None, there.get("name"))
        trip = {"pts": pts, "start": t, "end": t + minutes * 60, "by": "driving"}
    trip = {**trip, "to": there["name"], "from": "", "left": t, "suit": suited}
    with _lock:
        data = _load()
        data.update({"where": there["name"], "x": there["x"], "y": there["y"], "trip": trip, "case": case,
                     "follow": ""})
        atomic_write(_path(), json.dumps(data, ensure_ascii=False))
    return trip


def _with(contact_id, t):
    """Where one of them is, for going with them — only if they let him see it."""
    from ..contacts import directory
    from . import presence
    contact = directory().get(contact_id)
    if contact is None or not getattr(contact, "shares_location", True):
        return None
    return presence.of(contact).position(t)


def join(contact_id, t=None):
    """
    He goes to one of them — to where they'll be when he gets there, if they're on
    the move — and from then on he's with them: where they go, he goes, until he
    goes somewhere else. Returns the trip, or None if he can't see where they are.
    """
    from ..contacts import directory
    t = t or time.time()
    contact = directory().get(contact_id)
    there = _with(contact_id, t)
    if contact is None or there is None:
        return None
    here = position(t) or there
    trip = go(f"with {contact.name}", there[0], there[1], t=t)
    if trip and math.dist(here, there) > 0.6:
        # Where they'll be by the time he's there, not where they were when he set off.
        later = _with(contact_id, trip["end"]) or there
        if math.dist(later, there) > 0.4:
            trip = go(f"with {contact.name}", later[0], later[1], t=t)
    with _lock:
        data = _load()
        data["follow"] = contact_id
        atomic_write(_path(), json.dumps(data, ensure_ascii=False))
    return trip


def off_case(case_id):
    """The case he went to is over: whatever he does next, he's not on it."""
    with _lock:
        data = _load()
        if data.get("case") == case_id:
            data["case"] = ""
            atomic_write(_path(), json.dumps(data, ensure_ascii=False))


def note(t=None):
    """A line for the ones who'd know where he is — Alfred and Barbara on their screens."""
    t = t or time.time()
    s = state(t)
    if s.get("route"):
        left = max(1, round((s["route"]["end"] - t) / 60))
        return f"Bruce: on his way to {s['where']}, {s['route'].get('by', 'travelling')}, about {left} min out"
    return f"Bruce: at {s['where']}" + (" — suited up" if s["suit"] and s["where"] != HOME else "")
