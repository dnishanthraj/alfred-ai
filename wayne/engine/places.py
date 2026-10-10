"""
Where things are in Gotham — so "patrolling the Diamond District" is a dot on
a map, and someone on patrol is somewhere on their beat, not at home.

The map (gotham.json) is canon's: three islands, the rivers between them, the
mainland across the Gotham River, the Manor out in Gotham County, Blüdhaven up
the coast. A place named in someone's plan is matched to it; anywhere it can't
place stays a name without a dot.
"""
import hashlib
import json
import re
from functools import lru_cache
from pathlib import Path

# What a patrol looks like in someone's day, whoever wrote it.
# Out on patrol, said any of the ways they say it — but not "swing by Alfred's",
# "flying to Metropolis", "house hunting", "a rooftop bar", "sweep the kitchen"
# or "on comms", every one of which put someone on their beat.
_PATROL = re.compile(r"(?i)\b(patrol\w*|stake ?outs?|staking (it )?out|prowl\w*|on the streets|"
                     r"(out )?on the rooftops|(swinging|flying|running) (the )?(rooftops|city|beat|over|across)|"
                     r"on the beat|night rounds)\b")
# How long they stay in one part of their beat before moving on.
BEAT_STEP = 15 * 60


@lru_cache(maxsize=1)
def gazetteer():
    return json.loads((Path(__file__).with_name("gotham.json")).read_text())


def _jitter(seed, spread):
    digest = hashlib.sha1(seed.encode()).digest()
    return (digest[0] / 255 - 0.5) * 2 * spread, (digest[1] / 255 - 0.5) * 2 * spread


def _match(places, lowered, by_name_only=False):
    best, length = None, 0
    for place in places:
        names = [place["name"].lower()] + ([] if by_name_only else place.get("match", []))
        for name in names:
            if re.search(rf"(?<!\w){re.escape(name.strip())}(?!\w)", lowered) and len(name) > length:
                best, length = place, len(name)
    return best


def _spot(place):
    return {"name": place["name"], "area": place["area"], "x": place["x"], "y": place["y"]}


def resolve(text):
    """
    A spot for a place as people write it — "Little Italy, Blüdhaven", "the
    Clocktower", "Wayne Tower R&D" — or None if it isn't somewhere on the map.
    The same words always land on the same spot, so it's worked out once (a
    trail asked it a hundred times a request); each caller gets its own copy.
    """
    spot = _resolve((text or "").lower())
    return dict(spot) if spot else None


@lru_cache(maxsize=4096)
def _resolve(lowered):
    if not lowered.strip():
        return None
    data = gazetteer()
    regions = {r["name"] for r in data.get("regions", [])}
    # A city of its own first: Blüdhaven's waterfront is not Gotham's docks.
    for region in data.get("regions", []):
        if any(m in lowered for m in region["match"]):
            inside = _match([p for p in data["places"] if p["area"] == region["name"]], lowered)
            if inside:
                return _spot(inside)
            dx, dy = _jitter(lowered, region.get("spread", 4))
            return {"name": region["name"], "area": region["name"],
                    "x": round(region["x"] + dx, 1), "y": round(region["y"] + dy, 1)}
    best = (_match([p for p in data["places"] if p["area"] not in regions], lowered)
            or _match([p for p in data["places"] if p["area"] in regions], lowered, by_name_only=True))
    return _spot(best) if best else None


def note(text, most=2):
    """
    What anyone from Gotham knows of the places he's just named — a district's
    character and what's in it, a landmark's story — from the map itself, so
    "what's in Burnside?" is answered with the Burnside on the map, not a guess.
    "" when he named none. Only proper names and specific ways of saying them
    count: "the docks" in passing doesn't bring the docks' history with it.
    """
    lowered = (text or "").lower()
    if len(lowered) < 4:
        return ""
    data = gazetteer()
    found = []
    for place in data["places"]:
        terms = [place["name"].lower()] + [m for m in place.get("match", []) if len(m) >= 7]
        hit = max((len(t) for t in terms if re.search(rf"(?<!\w){re.escape(t)}(?!\w)", lowered)), default=0)
        if hit:
            found.append((hit, place))
    found = [p for _, p in sorted(found, key=lambda f: -f[0])]
    # Not both a district and something inside it that only matched because of it.
    lines = []
    for place in found[:most]:
        if place["kind"] == "district":
            here = [q["name"] for q in data["places"] if q["area"] == place["area"] and q["kind"] != "district"][:9]
            sketch = data.get("areas", {}).get(place["name"], "")
            lines.append(f"{place['name']}: {sketch}" + (f" In it: {', '.join(here)}." if here else ""))
        elif place.get("bio"):
            lines.append(f"{place['name']} ({place['area']}): {place['bio']}")
    if not lines:
        return ""
    return ("What you know of the places he mentioned, as anyone who knows Gotham would — use it only if it "
            "fits, in your own words:\n" + "\n".join(lines))


def names():
    """Every place on the map, by name — for anyone saying where they'll be."""
    data = gazetteer()
    return [p["name"] for p in data["places"]] + [r["name"] for r in data.get("regions", [])]


def is_patrol(doing):
    return bool(_PATROL.search(doing or ""))


def on_beat(contact, t):
    """Where on their beat they are right now: moving on every quarter hour or so."""
    beat = list(getattr(contact, "beat", ()) or ())
    if not beat:
        return None
    slot = int(t // BEAT_STEP)
    digest = hashlib.sha1(f"{contact.id}:{slot}".encode()).digest()
    return beat[digest[0] % len(beat)]


def patrol_spot(contact, planned, t):
    """
    Where a patrol has them now: at an open report on their beat, if there is
    one and they'd answer it; otherwise a stop on their beat within the area
    the plan named (all of it if the plan was vague), else the plan's own place.
    """
    from . import incidents
    beat = list(getattr(contact, "beat", ()) or ())
    there = resolve(planned) if planned and "/" not in planned else None
    if there:
        beat = [b for b in beat if (resolve(b) or {}).get("area") == there["area"]] or []
        if not beat:
            return planned
    if not beat:
        return planned or None
    areas = {(resolve(b) or {}).get("area") for b in beat}
    report = incidents.near(areas, t)
    slot = int(t // BEAT_STEP)
    digest = hashlib.sha1(f"{contact.id}:{slot}".encode()).digest()
    if report and digest[1] % 3:
        # Two times in three, they go where the trouble is.
        return report["place"]
    return beat[digest[0] % len(beat)]
