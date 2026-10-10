"""
The Batwing — Bruce's jet. There's one of it: in the hangar under the Manor
most of the night, out now and then on a few low passes over the city, and —
when one of the family needs to be across Gotham faster than the roofs or the
roads will take them — flown to them on autopilot from the cave, down low
enough to grapple up to, and across the city to drop them on a roof by the
scene. Two of them at a time; one job at a time. It isn't coming for someone
on the far side of the city when it's ten minutes out: they take the bike.

Where it is, is worked out from what it's been asked to do and the clock, so
the map, the family and the case board agree. Kept in DATA_DIR/_jet.json.
"""
import hashlib
import json
import math
import threading
import time

from .. import paths
from ..memory.store import atomic_write, read_text
from . import places

SPEED = 45.0            # map units a minute: ~400 km/h low over the roofs — faster than any airliner on approach
PICKUP = 1.0            # minutes to come down, get a line to them, and climb out
DROP = 0.5              # and to put them on a roof
SEATS = 2
HOME = "Wayne Manor"    # the hangar's under it
IDLE = 12 * 60          # how long it waits where it dropped them before it goes home (seconds)
AREA = 35.0             # how far off it'll come for someone from — "in the area", ~5 km
# Its passes over the city at night, when nobody's asked for it.
PASS_SLOT = 2700
OVERFLY = ["Diamond District", "Old Gotham", "The Narrows", "Crime Alley", "Burnside", "Otisburg", "Amusement Mile",
           "Upper East Side", "Tricorner", "Robinson Park", "Chinatown", "Coventry", "New Town"]

_lock = threading.Lock()


def _path():
    return paths.DATA_DIR / "_jet.json"


def _load():
    try:
        return json.loads(read_text(_path()) or "{}")
    except ValueError:
        return {}


def _save(data):
    atomic_write(_path(), json.dumps(data, ensure_ascii=False))


def _draw(*parts):
    return int(hashlib.sha1(":".join(map(str, parts)).encode()).hexdigest()[:8], 16) / 0xFFFFFFFF


def home():
    spot = places.resolve(HOME)
    return (spot["x"] + 0.6, spot["y"] - 0.4) if spot else (0.0, 30.0)


def _night(t):
    hour = time.localtime(t).tm_hour
    return hour >= 21 or hour < 4


def _flight(a, b, start, kind, riders=()):
    minutes = max(0.4, math.dist(a, b) / SPEED)
    return {"pts": [[round(a[0], 2), round(a[1], 2)], [round(b[0], 2), round(b[1], 2)]], "start": start,
            "end": start + minutes * 60, "kind": kind, "riders": list(riders)}


def _pass(t):
    """Its pass over the city in the slot t falls in, when it has one and nobody's asked for it: [legs] or []."""
    if not _night(t):
        return []
    k = int(t // PASS_SLOT)
    if _draw("jet-pass", k) > 0.5:
        return []
    begins = k * PASS_SLOT + _draw("jet-when", k) * 900
    stops = [places.resolve(n) for n in OVERFLY]
    stops = [(s["x"], s["y"]) for s in stops if s]
    route = [home()]
    for i in range(3 + int(_draw("jet-many", k) * 2)):
        pick = stops[int(_draw("jet-stop", k, i) * len(stops))]
        if pick not in route:
            route.append(pick)
    route.append(home())
    legs, at = [], begins
    for a, b in zip(route, route[1:], strict=False):
        leg = _flight(a, b, at, "pass")
        legs.append(leg)
        at = leg["end"]
    return legs


def _legs(t):
    """Every leg it's flying around t — what it was asked to do, else tonight's pass."""
    data = _load()
    booked = [leg for leg in data.get("legs") or [] if leg["end"] > t - 3 * 3600]
    if booked and (booked[-1]["end"] > t or booked[-1]["kind"] != "home" and booked[-1]["end"] + IDLE > t):
        return booked
    return booked + [leg for leg in _pass(t) if not booked or leg["start"] > booked[-1]["end"] + IDLE]


def where(t=None):
    """(x, y, heading in degrees, the leg it's on or None) at t."""
    t = t or time.time()
    legs = _legs(t)
    current = next((leg for leg in legs if leg["start"] <= t < leg["end"]), None)
    if current:
        (ax, ay), (bx, by) = current["pts"]
        f = (t - current["start"]) / max(1.0, current["end"] - current["start"])
        heading = math.degrees(math.atan2(bx - ax, ay - by)) % 360
        return ax + (bx - ax) * f, ay + (by - ay) * f, heading, current
    waiting = _load().get("waiting") or {}
    if waiting.get("until", 0) > t:
        x, y = waiting["at"]
        return x, y, 0.0, None              # overhead where it was called, till they move
    done = [leg for leg in legs if leg["end"] <= t]
    if done and done[-1]["kind"] != "home" and t - done[-1]["end"] < IDLE:
        x, y = done[-1]["pts"][-1]
        return x, y, 0.0, None              # where it put them down, waiting a while
    hx, hy = home()
    return hx, hy, 0.0, None


def busy(t=None):
    """Whether it's on a job — coming for someone, carrying them — at t."""
    t = t or time.time()
    return bool(waiting_for(t)) or any(leg["kind"] in ("pickup", "carry", "summoned") and leg["end"] > t
                                       and leg["start"] <= t + 60 for leg in _load().get("legs") or [])


def offer(a, b, t=None, who=""):
    """
    What it would take for the jet to come for someone at a and drop them at b:
    {"pickup": when it's with them, "lift": when they're up, "arrive": when they're down}
    — or None when it's on another job or too far off to come.
    """
    t = t or time.time()
    mine = waiting_for(t) == who if who else False
    if busy(t) and not mine:
        return None
    x, y, _h, _leg = where(t)
    if math.dist((x, y), a) > AREA and not mine:
        return None
    out = math.dist((x, y), a) / SPEED
    pickup = t + out * 60
    lift = pickup + PICKUP * 60
    arrive = lift + (math.dist(a, b) / SPEED + DROP) * 60
    return {"pickup": pickup, "lift": lift, "arrive": arrive}


def book(rider, a, b, t=None):
    """
    It comes for them: the legs flown from wherever it is now, to them, across
    to b — and home again a while later. Returns the trip they're on
    ({"pts", "start", "end", "by"}), or None if it can't.
    """
    t = t or time.time()
    with _lock:
        deal = offer(a, b, t, who=rider)
        if deal is None:
            return None
        x, y, _h, _leg = where(t)
        data = _load()
        data.pop("waiting", None)
        kept = [leg for leg in data.get("legs") or [] if leg["end"] <= t and leg["kind"] != "pass"][-6:]
        to_them = _flight((x, y), a, t, "pickup", [rider])
        carry = _flight(a, b, deal["lift"], "carry", [rider])
        carry["end"] = deal["arrive"]
        back = _flight(b, home(), deal["arrive"] + IDLE, "home")
        data["legs"] = kept + [to_them, carry, back]
        _save(data)
    return {"pts": carry["pts"], "start": deal["lift"], "end": deal["arrive"], "by": "on the Batwing",
            "pickup": deal["pickup"]}


WAITS = 15 * 60         # how long it waits for whoever called it before it gives up and goes home (seconds)


def summon(who, a, t=None):
    """
    Called to someone — him, usually: it flies to them and waits overhead for
    their next move, theirs alone while it waits. None if it's on someone
    else's job. Returns the state it's in.
    """
    t = t or time.time()
    with _lock:
        waiting = waiting_for(t)
        if busy(t) and waiting != who:
            return None
        x, y, _h, _leg = where(t)
        data = _load()
        kept = [leg for leg in data.get("legs") or [] if leg["end"] <= t and leg["kind"] != "pass"][-6:]
        come = _flight((x, y), a, t, "summoned", [who])
        back = _flight(a, home(), come["end"] + WAITS, "home")
        data["legs"] = kept + [come, back]
        data["waiting"] = {"for": who, "at": [round(a[0], 2), round(a[1], 2)], "until": come["end"] + WAITS}
        _save(data)
    return state(t)


def waiting_for(t=None):
    """Who it's come for and is waiting on, if anyone: their id, or ''."""
    t = t or time.time()
    waiting = _load().get("waiting") or {}
    return waiting.get("for", "") if waiting.get("until", 0) > t else ""


def share(rider, a, b, t=None):
    """A second passenger going the same way, from the same roof: aboard the same flight, if there's a seat."""
    t = t or time.time()
    with _lock:
        data = _load()
        for leg in data.get("legs") or []:
            if (leg["kind"] == "carry" and leg["start"] > t and len(leg["riders"]) < SEATS
                    and math.dist(leg["pts"][0], a) < 0.8 and math.dist(leg["pts"][-1], b) < 1.5):
                leg["riders"].append(rider)
                for other in data["legs"]:
                    if other["kind"] == "pickup" and other["end"] == leg["start"] - PICKUP * 60:
                        other["riders"] = list(leg["riders"])
                _save(data)
                return {"pts": leg["pts"], "start": leg["start"], "end": leg["end"], "by": "on the Batwing",
                        "pickup": leg["start"] - PICKUP * 60}
    return None


def state(t=None):
    """What the map shows: where it is, which way it's headed, the legs ahead, who's aboard, and in words."""
    t = t or time.time()
    x, y, heading, leg = where(t)
    legs = [lg for lg in _legs(t) if lg["end"] > t][:3]
    riders = leg["riders"] if leg and leg["kind"] == "carry" else []
    hx, hy = home()
    parked = leg is None and math.dist((x, y), (hx, hy)) < 0.2
    lifting = next((lg for lg in legs if lg["kind"] == "carry" and lg["start"] > t >= lg["start"] - PICKUP * 60 - 5), None)
    if leg is None and lifting:
        # Hanging over the roof while they get a line to it and climb aboard.
        x, y = lifting["pts"][0]
        parked, riders, status = False, lifting["riders"], "picking up"
    elif leg is None:
        status = "in the hangar" if parked else "waiting where it set down"
    else:
        status = {"pickup": "coming down for a pickup", "carry": "carrying", "home": "heading home",
                  "pass": "on a pass over the city", "summoned": "on its way to whoever called it"}[leg["kind"]]
    if leg is None and waiting_for(t):
        status = "waiting overhead"
    return {"x": round(x, 2), "y": round(y, 2), "r": round(heading), "legs": legs, "riders": riders,
            "status": status, "parked": parked, "waiting_for": waiting_for(t)}
