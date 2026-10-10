"""
The Batwing — Bruce's jet, and only Bruce flies it. In the hangar under the
Manor till he wants it; called, it flies itself to him and waits overhead; he
flies it — faster than anything else in Gotham's sky — and where he's going,
he drops out of it onto a roof by the place and he's there. It hangs overhead
a while in case he wants it again, then takes itself home.

There's room for one more: he can give one of the family a lift. They stay
where they are, he comes down for them, and drops them where they need to be —
they rappel down and get on with it — and he flies on, or stays with the jet.

Where it is, is worked out from what it's been asked to do and the clock, so
the map, the family and the case board agree. Kept in DATA_DIR/_jet.json.
"""
import json
import math
import threading
import time

from .. import paths
from ..memory.store import atomic_write, read_text
from . import places

SPEED = 45.0            # map units a minute: ~400 km/h low over the roofs — faster than any airliner on approach
PICKUP = 1.0            # minutes to come down, get a line to someone, and climb out
DROP = 0.5              # and to drop someone onto a roof
SEATS = 2               # him, and one more
HOME = "Wayne Manor"    # the hangar's under it
IDLE = 12 * 60          # how long it hangs where he got out before it takes itself home (seconds)
AREA = 35.0             # how far off it can be and still be worth calling for a trip, ~5 km
WAITS = 15 * 60         # called to him, how long it waits overhead for his next move (seconds)
PILOT = "bruce"

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


def home():
    spot = places.resolve(HOME)
    return (spot["x"] + 0.6, spot["y"] - 0.4) if spot else (0.0, 30.0)


def _flight(a, b, start, kind, riders=()):
    minutes = max(0.3, math.dist(a, b) / SPEED)
    return {"pts": [[round(a[0], 2), round(a[1], 2)], [round(b[0], 2), round(b[1], 2)]], "start": start,
            "end": start + minutes * 60, "kind": kind, "riders": list(riders)}


def _legs(t):
    return [leg for leg in _load().get("legs") or [] if leg["end"] > t - 3 * 3600]


def where(t=None):
    """(x, y, heading in degrees, the leg it's on or None) at t."""
    t = t or time.time()
    legs = _legs(t)
    current = next((leg for leg in legs if leg["start"] <= t < leg["end"]), None)
    if current:
        (ax, ay), (bx, by) = current["pts"]
        f = (t - current["start"]) / max(1.0, current["end"] - current["start"])
        return ax + (bx - ax) * f, ay + (by - ay) * f, math.degrees(math.atan2(bx - ax, ay - by)) % 360, current
    waiting = _load().get("waiting") or {}
    if waiting.get("until", 0) > t:
        x, y = waiting["at"]
        return x, y, 0.0, None              # overhead where he called it, till he moves
    done = [leg for leg in legs if leg["end"] <= t]
    if done:
        x, y = done[-1]["pts"][-1]
        return x, y, 0.0, None              # wherever it last stopped: home, or hanging where he got out
    hx, hy = home()
    return hx, hy, 0.0, None


def waiting_for(t=None):
    """Whether it's waiting overhead for him, called: 'bruce', or ''."""
    t = t or time.time()
    waiting = _load().get("waiting") or {}
    return waiting.get("for", "") if waiting.get("until", 0) > t else ""


def busy(t=None):
    """Mid-flight for him — on its way to him, or him at the controls — at t."""
    t = t or time.time()
    return any(leg["kind"] in ("flying", "coming") and leg["end"] > t and leg["start"] <= t + 60
               for leg in _load().get("legs") or [])


def offer(a, b, t=None, who=PILOT):
    """
    Him, from a to b, in the jet: {"pickup": when it's with him, "lift": when he's up, "arrive"} — or None
    when it isn't his to take (it's only ever his), it's mid-flight, or it's too far off to be worth calling.
    """
    t = t or time.time()
    if who != PILOT or busy(t):
        return None
    x, y, _h, _leg = where(t)
    off = math.dist((x, y), a)
    if off > AREA and waiting_for(t) != PILOT:
        return None
    pickup = t + off / SPEED * 60
    lift = pickup + (PICKUP * 60 if off > 0.3 else 15)
    return {"pickup": pickup, "lift": lift, "arrive": lift + (math.dist(a, b) / SPEED + DROP) * 60}


def _home_after(at, t):
    return _flight(at, home(), t + IDLE, "home")


def book(rider, a, b, t=None):
    """
    He flies it from a to b: it flies itself to him first if it isn't there, he
    climbs in, and at b he drops out onto a roof — there. Returns his trip
    ({"pts", "start", "end", "by", "pickup"}), or None if it isn't his to take.
    """
    t = t or time.time()
    with _lock:
        deal = offer(a, b, t, who=rider)
        if deal is None:
            return None
        x, y, _h, _leg = where(t)
        data = _load()
        data.pop("waiting", None)
        kept = [leg for leg in data.get("legs") or [] if leg["end"] <= t][-6:]
        legs = [_flight((x, y), a, t, "coming")] if math.dist((x, y), a) > 0.3 else []
        fly = _flight(a, b, deal["lift"], "flying", [PILOT])
        fly["end"] = deal["arrive"]
        data["legs"] = kept + legs + [fly, _home_after(b, deal["arrive"])]
        _save(data)
    return {"pts": fly["pts"], "start": deal["lift"], "end": deal["arrive"], "by": "on the Batwing",
            "pickup": deal["pickup"]}


def lift(passenger, bruce_at, them_at, drop_at, t=None):
    """
    He gives one of them a lift: it flies itself to him if it isn't with him, he
    flies to where they're waiting, comes down for them, and drops them at
    drop_at — and he's there with the jet overhead. ({his trip}, {their trip}),
    or None if it's mid-flight or too far off to call.
    """
    t = t or time.time()
    with _lock:
        if busy(t):
            return None
        x, y, _h, _leg = where(t)
        if math.dist((x, y), bruce_at) > AREA and waiting_for(t) != PILOT:
            return None
        data = _load()
        data.pop("waiting", None)
        kept = [leg for leg in data.get("legs") or [] if leg["end"] <= t][-6:]
        legs, at = [], t
        if math.dist((x, y), bruce_at) > 0.3:
            legs.append(_flight((x, y), bruce_at, at, "coming"))
            at = legs[-1]["end"] + PICKUP * 60
        boarded = at
        to_them = _flight(bruce_at, them_at, at, "flying", [PILOT])
        aboard = to_them["end"] + PICKUP * 60
        across = _flight(them_at, drop_at, aboard, "flying", [PILOT, passenger])
        across["end"] += DROP * 60
        data["legs"] = kept + legs + [to_them, across, _home_after(drop_at, across["end"])]
        _save(data)
    first = math.dist(*to_them["pts"]) / SPEED + PICKUP
    total = first + math.dist(*across["pts"]) / SPEED + DROP
    his = {"pts": to_them["pts"] + [across["pts"][-1]], "start": boarded, "end": across["end"], "by": "on the Batwing",
           "pickup": boarded, "times": [0.0, round(first / total, 4), 1.0]}      # the stop at their roof, kept in time
    theirs = {"pts": across["pts"], "start": aboard, "end": across["end"], "by": "on the Batwing, with him",
              "pickup": aboard - PICKUP * 60}
    return his, theirs


def summon(who, a, t=None):
    """
    He calls it to him: it flies itself there and waits overhead for his next
    move. Only his to call; None mid-flight. Returns the state it's in.
    """
    t = t or time.time()
    if who != PILOT:
        return None
    with _lock:
        if busy(t):
            return None
        x, y, _h, _leg = where(t)
        data = _load()
        kept = [leg for leg in data.get("legs") or [] if leg["end"] <= t][-6:]
        come = _flight((x, y), a, t, "coming")
        data["legs"] = kept + [come, _flight(a, home(), come["end"] + WAITS, "home")]
        data["waiting"] = {"for": who, "at": [round(a[0], 2), round(a[1], 2)], "until": come["end"] + WAITS}
        _save(data)
    return state(t)


def send_home(t=None):
    """Done with it: it takes itself back to the hangar now."""
    t = t or time.time()
    with _lock:
        if busy(t):
            return None
        x, y, _h, _leg = where(t)
        data = _load()
        data.pop("waiting", None)
        kept = [leg for leg in data.get("legs") or [] if leg["end"] <= t][-6:]
        data["legs"] = kept + ([_flight((x, y), home(), t, "home")] if math.dist((x, y), home()) > 0.3 else [])
        _save(data)
    return state(t)


def state(t=None):
    """What the map shows: where it is, which way it's headed, the legs ahead, who's aboard, and in words."""
    t = t or time.time()
    x, y, heading, leg = where(t)
    legs = [lg for lg in _legs(t) if lg["end"] > t][:4]
    riders = leg["riders"] if leg else []
    hx, hy = home()
    parked = leg is None and math.dist((x, y), (hx, hy)) < 0.2
    nxt = next((lg for lg in legs if lg["start"] > t), None)
    leaving = next((lg for lg in legs if lg["kind"] == "home" and lg["start"] > t), None)
    if leg is None and nxt and nxt["kind"] == "flying" and nxt["start"] - t <= PICKUP * 60 + 5:
        status, riders, parked = "coming down for a pickup", nxt["riders"], False
    elif leg is None and waiting_for(t):
        status = "waiting overhead for you"
    elif leg is None and parked:
        status = "in the hangar"
    elif leg is None:
        status = "hanging where you got out" + (
            f" — home in {max(1, round((leaving['start'] - t) / 60))} min" if leaving else "")
    else:
        status = {"coming": "flying itself to you", "flying": "you at the controls",
                  "home": "taking itself home"}[leg["kind"]]
    return {"x": round(x, 2), "y": round(y, 2), "r": round(heading), "legs": legs, "riders": riders,
            "status": status, "parked": parked, "waiting_for": waiting_for(t)}
