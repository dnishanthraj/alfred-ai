"""
Patrol: who's out tonight, where each of them covers, and how they move.

The family's nights are a roster, not six people wandering: whoever's out on
patrol in a watch (two hours) covers a sector — a district — the worst of the
city likelier, their own turf likelier still, never two on one unless they're
out as a pair (Dick and Tim, Barbara and Cass), and somewhere new the next
watch. Someone who worked a case lately goes back over ground that has trouble
like it on the scanner again — the same crew, the same rogue — because that's
how a lead gets followed.

Within a sector they move the way a patrol does: a run across the roofs to a
vantage point, a few minutes watching from it, on to the next. Something near
them comes over the scanner and they go (that's the cases' business — see
Console._case_tick).

All of it drawn from the night and the clock, so the map, a call and the
scanner agree on where everyone is at any moment.
"""
import hashlib
import math
import random
import threading
import time

from . import places, travel

WATCH = 2 * 3600        # a watch: sectors change hands every two hours
STOP = 6 * 60           # a run to a vantage point and the watching from it
PACE = 1.4              # map units a minute on patrol: moving, but looking as they go
FIELD = ("nightwing", "robin", "batgirl", "orphan", "redhood", "batwing")
# Who goes out together, on the nights they're both out — and how often.
PAIRS = {("nightwing", "robin"): 0.45, ("batgirl", "orphan"): 0.4, ("robin", "orphan"): 0.3,
         ("nightwing", "batgirl"): 0.3, ("redhood", "batwing"): 0.1, ("nightwing", "redhood"): 0.06}
# Dick's city is Blüdhaven; some nights he's in Gotham instead.
GOTHAM_NIGHTS = 0.2
# Nobody patrols the asylum's island, or the estate.
_NOT_A_BEAT = {"Arkham Island", "Bristol"}

_lock = threading.Lock()
_rosters = {}           # watch -> (made at, roster)
_points = {}            # sector -> vantage points


def _draw(*parts):
    return int(hashlib.sha1(":".join(map(str, parts)).encode()).hexdigest()[:8], 16) / 0xFFFFFFFF


def _local(t):
    return t + time.localtime(t).tm_gmtoff


def watch_of(t):
    """Which watch t falls in: they turn over on the even hours, local time."""
    return int(_local(t) // WATCH)


def night_of(t):
    """The night t belongs to — the small hours count as the night before."""
    return time.strftime("%Y-%m-%d", time.localtime(t - 6 * 3600))


# --- the sectors ---------------------------------------------------------------

_SECTORS = None


def sectors():
    """[{"name", "x", "y", "crime", "city"}] — every district someone could be covering."""
    global _SECTORS
    if _SECTORS is not None:
        return _SECTORS
    out = []
    for d in places.gazetteer().get("districts", []):
        if d["name"] in _NOT_A_BEAT:
            continue
        x, y = d["at"]
        city = "bludhaven" if x > 100 and y < 10 else "gotham"
        # Said so it lands where it is: "Waterloo Docks" alone is Gotham's docks.
        name = f"{d['name']}, Blüdhaven" if city == "bludhaven" else d["name"]
        out.append({"name": name, "x": x, "y": y, "crime": d.get("crime", 0.3), "city": city, "district": d["name"]})
    _SECTORS = out
    return out


def sector_named(text):
    """The sector a place or a line of plan names — 'on patrol in Crime Alley', 'the Bowery' — or None."""
    if not text:
        return None
    found = places.resolve(text)
    if not found:
        return None
    for s in sectors():
        if found["name"] in (s["name"], s["district"]) or (found.get("area") == s["district"]):
            return s
    return None


def _city_named(text):
    found = places.resolve(text or "")
    if not found:
        return None
    return "bludhaven" if found["name"] == "Blüdhaven" or found.get("area") == "Blüdhaven" else None


def _home_city(contact, t):
    """Where they patrol by default: their beat's city — Dick's Blüdhaven, except the nights he's in Gotham."""
    beat = [places.resolve(b) for b in (getattr(contact, "beat", ()) or ())]
    bludhaven = sum(1 for b in beat if b and b.get("area") == "Blüdhaven")
    if beat and bludhaven > len(beat) / 2:
        return "gotham" if _draw(contact.id, "gotham-night", night_of(t)) < GOTHAM_NIGHTS else "bludhaven"
    return "gotham"


def is_sector(name):
    return any(s["name"] == name for s in sectors())


# --- the roster ------------------------------------------------------------------

def _on_patrol(contact, t):
    from . import presence
    whereabouts = presence._registry.get(contact.id) or presence.of(contact)
    block, _company, _firm = whereabouts._situation(t)
    return places.is_patrol(block.get("doing")), block


def _related_areas(contact_id, t):
    """
    Districts with trouble on the scanner like a case they worked lately — the
    same crew, the same rogue: where they'd go back to look.
    """
    from . import cases, incidents
    worked = [c for c in cases.everything() if contact_id in cases.team(c) and t - c.get("opened_at", 0) < 48 * 3600]
    if not worked:
        return {}
    suspects = {c.get("suspect") for c in worked if c.get("suspect")}
    crews = {c.get("gang") for c in worked if c.get("gang")}
    out = {}
    for r in incidents.at(t):
        why = (r.get("suspect") in suspects and r.get("suspect")) or (r.get("gang") in crews and r.get("gang"))
        if why:
            out[r["area"]] = why
    return out


def _weights(contact, city, t, taken, last, beat_areas, related):
    hour = time.localtime(t).tm_hour
    early = hour >= 19 or hour < 1
    out = []
    for s in sectors():
        if s["city"] != city or s["name"] in taken:
            continue
        w = (0.2 + s["crime"]) ** 1.6
        if s["district"] in beat_areas:
            w *= 3.0
        if early and s["district"] in _NIGHTLIFE:
            w *= 1.5
        if not early and s["district"] in _ROUGH:
            w *= 1.4
        if s["district"] in related:
            w *= 4.0
        if s["name"] == last:
            w *= 0.12           # somewhere new each watch
        out.append((s, w))
    return out


def _sets():
    from . import incidents
    return incidents.NIGHTLIFE, incidents.ROUGH


_NIGHTLIFE, _ROUGH = set(), set()


def roster(t, depth=1):
    """
    Who's out on patrol in the watch t falls in, and where: {contact id: {"sector",
    "with": [partner], "lead": why they went back there, or ""}}. Earlier starters
    pick first, so someone coming out at midnight doesn't move anyone already out.
    """
    global _NIGHTLIFE, _ROUGH
    if not _NIGHTLIFE:
        _NIGHTLIFE, _ROUGH = _sets()
    w = watch_of(t)
    with _lock:
        made = _rosters.get((w, depth))
        if made and time.monotonic() - made[0] < 60:
            return made[1]
    from ..contacts import directory
    book = directory()
    start = w * WATCH - time.localtime(t).tm_gmtoff
    samples = [start + k * 25 * 60 for k in range(5)]
    out_now = []
    for cid in FIELD:
        contact = book.get(cid)
        if contact is None:
            continue
        on = [(s, blk) for s in samples for patrolling, blk in [_on_patrol(contact, s)] if patrolling]
        if on:
            out_now.append((on[0][0], contact, on[0][1]))
    out_now.sort(key=lambda o: (o[0], _draw(o[1].id, "order", night_of(t))))
    previous = roster(t - WATCH, depth - 1) if depth > 0 else {}
    taken, result, partner = set(), {}, {}
    # Tonight's pairs, among those out this watch.
    ids = [c.id for _, c, _ in out_now]
    cities = {c.id: _home_city(c, t) for _, c, _ in out_now}
    for (a, b), odds in PAIRS.items():
        if a not in ids or b not in ids or a in partner or b in partner:
            continue
        # Tim over in Blüdhaven with Dick happens; just not most nights.
        if _draw(a, b, night_of(t)) < odds * (1.0 if cities[a] == cities[b] else 0.3):
            partner[a], partner[b] = b, a
    # Whoever's plan names their ground — Jason's Crime Alley — has it; the roster works round them.
    fixed_for = {c.id: sector_named(blk.get("where") or "") or sector_named(blk.get("doing") or "")
                 for _, c, blk in out_now}
    taken |= {s["name"] for s in fixed_for.values() if s}
    out_now.sort(key=lambda o: not fixed_for.get(o[1].id))
    for _began, contact, block in out_now:
        cid = contact.id
        if cid in result:
            continue
        fixed = fixed_for.get(cid)
        city = (_city_named(block.get("where") or "") or _city_named(block.get("doing") or "")
                or _home_city(contact, t))
        related = _related_areas(cid, t)
        lead = ""
        if fixed:
            choice = fixed
        else:
            beat_areas = {(places.resolve(b) or {}).get("area") for b in (getattr(contact, "beat", ()) or ())}
            beat_areas |= {(places.resolve(b) or {}).get("name") for b in (getattr(contact, "beat", ()) or ())}
            options = _weights(contact, city, t, taken, (previous.get(cid) or {}).get("sector"), beat_areas, related)
            if not options:
                options = _weights(contact, city, t, set(), None, beat_areas, related)
            if not options:
                continue
            r = random.Random(f"{cid}:{w}:{night_of(t)}")
            choice = r.choices([s for s, _ in options], weights=[x for _, x in options])[0]
            lead = related.get(choice["district"], "")
        taken.add(choice["name"])
        mate = partner.get(cid)
        result[cid] = {"sector": choice["name"], "with": [mate] if mate else [], "lead": lead}
        if mate and mate not in result:
            result[mate] = {"sector": choice["name"], "with": [cid], "lead": lead}
    with _lock:
        _rosters[(w, depth)] = (time.monotonic(), result)
        if len(_rosters) > 64:
            for key in sorted(_rosters, key=lambda k: _rosters[k][0])[:16]:
                _rosters.pop(key, None)
    return result


def sector(contact, planned, t):
    """
    The sector they're covering at t: the one a plan or what they said names,
    or tonight's roster's — or, out on their own, the likeliest that's free.
    """
    named = sector_named(planned)
    if named:
        return named["name"]
    on = roster(t).get(contact.id)
    if on:
        return on["sector"]
    city = _city_named(planned) or _home_city(contact, t)
    taken = {v["sector"] for v in roster(t).values()}
    beat_areas = {(places.resolve(b) or {}).get("area") for b in (getattr(contact, "beat", ()) or ())}
    options = _weights(contact, city, t, taken, None, beat_areas, {}) or _weights(contact, city, t, set(), None,
                                                                                 beat_areas, {})
    if not options:
        return planned or None
    r = random.Random(f"{contact.id}:{watch_of(t)}:{night_of(t)}:solo")
    return r.choices([s for s, _ in options], weights=[x for _, x in options])[0]["name"]


def partner(contact_id, t):
    """Who they're out with this watch, if they're patrolling as a pair: [id] or []."""
    return list((roster(t).get(contact_id) or {}).get("with") or [])


def lead(contact_id, t):
    """Why they're working the ground they are, if it's a lead — 'the False Face Society' — or ''."""
    return (roster(t).get(contact_id) or {}).get("lead", "")


# --- moving through a sector -----------------------------------------------------

def _vantage(sector_name):
    """Points to watch from in a sector — street corners and roofs over them, never out on the water."""
    if sector_name in _points:
        return _points[sector_name]
    s = next((s for s in sectors() if s["name"] == sector_name), None)
    if s is None:
        found = places.resolve(sector_name)
        if not found:
            return []
        s = {"name": sector_name, "x": found["x"], "y": found["y"]}
    graph = travel._load()
    others = [o for o in sectors() if o["name"] != s["name"]]
    mine = []
    for radius in (4.5, 6.5, 9.0):
        mine = [p for p in graph["nodes"] if math.dist(p, (s["x"], s["y"])) <= radius
                and all(math.dist(p, (s["x"], s["y"])) <= math.dist(p, (o["x"], o["y"])) for o in others)]
        if len(mine) >= 8:
            break
    rnd = random.Random(f"vantage:{sector_name}")
    rnd.shuffle(mine)
    picked = []
    for p in mine:
        if all(math.dist(p, q) > 0.9 for q in picked):
            picked.append((round(p[0], 2), round(p[1], 2)))
        if len(picked) >= 28:
            break
    if not picked:
        picked = [(s["x"], s["y"])]
    _points[sector_name] = picked
    return picked


def _waypoints(seed_id, sector_name, w):
    """The vantage points of a watch in a sector, in the order they're walked: each a short run from the last."""
    key = (seed_id, sector_name, w)
    if key in _walks:
        return _walks[key]
    pts = _vantage(sector_name)
    graph = travel._load()
    rnd = random.Random(f"walk:{seed_id}:{sector_name}:{w}")
    order = [rnd.choice(pts)]
    for _ in range(WATCH // STOP + 2):
        here = order[-1]
        near = sorted((p for p in pts if p != here and p not in order[-3:]
                       and travel._widest_water(graph, here, p) <= travel.GLIDE), key=lambda p: math.dist(here, p))[:5]
        order.append(rnd.choice(near) if near else here)
    if len(_walks) > 256:
        _walks.clear()
    _walks[key] = order
    return order


_walks = {}


def _stop(t):
    return int(_local(t) // STOP)


def where(contact_id, sector_name, t, arrived=None, seed_id=None, offset=(0.0, 0.0)):
    """
    Where in their sector they are at t: {"x", "y", "near", "leg"} — `leg` the run
    they're on ({"pts", "start", "end"}) or None while they watch from somewhere.
    `arrived` is when they got to the sector; `seed_id` whose walk it is (a pair
    walks one, a step apart).
    """
    seed_id = seed_id or contact_id
    w = watch_of(t)
    order = _waypoints(seed_id, sector_name, w)
    first = w * (WATCH // STOP)
    k = _stop(t)
    i = max(0, min(len(order) - 1, k - first))
    stop_start = t - (_local(t) - k * STOP)
    centre = places.resolve(sector_name)
    centre = (centre["x"], centre["y"]) if centre else order[0]
    if arrived and arrived > stop_start:
        here, leg = centre, None             # just got here: taking it in from where they landed
    else:
        came_from_centre = arrived and arrived > stop_start - STOP
        prev = centre if (i == 0 or came_from_centre) else order[i - 1]
        dest = order[i]
        length = math.dist(prev, dest)
        run = min(STOP * 0.7, max(30.0, length / PACE * 60)) if length > 0.05 else 0
        if run and t < stop_start + run:
            pts = travel._rooftops(prev, dest)
            pts = [[round(x + offset[0], 2), round(y + offset[1], 2)] for x, y in pts]
            # A line across each street, a run along each roof: the pace of it, not a glide.
            leg = {"pts": pts, "start": stop_start, "end": stop_start + run, "times": travel.roof_run(pts)[0]}
            here = travel.position(leg, t)
            return {"x": round(here[0], 2), "y": round(here[1], 2), "near": near(*here), "leg": leg}
        here, leg = dest, None
    x, y = here[0] + offset[0], here[1] + offset[1]
    return {"x": round(x, 2), "y": round(y, 2), "near": near(x, y), "leg": leg}


_HOMES = None


def near(x, y, within=1.6):
    """The landmark or venue a vantage point looks down on — 'Sacred Martyr Church' — or ''; never anyone's home."""
    global _HOMES
    if _HOMES is None:
        from ..contacts import directory
        _HOMES = {(getattr(c, "home", "") or "").lower() for c in directory()}
    best = None
    for p in places.gazetteer()["places"]:
        if p.get("kind") not in ("landmark", "spot") or p["name"].lower() in _HOMES or "apartment" in p["name"].lower():
            continue
        d = math.dist((p["x"], p["y"]), (x, y))
        if d <= within and (best is None or d < best[0]):
            best = (d, p["name"])
    for v in places.venues():
        d = math.dist((v["x"], v["y"]), (x, y))
        if d <= within * 0.6 and (best is None or d < best[0]):
            best = (d, v["name"])
    return best[1] if best else ""


