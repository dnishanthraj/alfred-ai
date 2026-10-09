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
_PATROL = re.compile(r"(?i)\b(patrol\w*|rooftops?|on comms|night shift|sweep|stakeout|swing\w*|"
                     r"on the streets|flying|prowl\w*|hunting)\b")
# How long they stay in one part of their beat before moving on.
BEAT_STEP = 15 * 60


@lru_cache(maxsize=1)
def gazetteer():
    return json.loads((Path(__file__).with_name("gotham.json")).read_text())


def _jitter(seed, spread):
    digest = hashlib.sha1(seed.encode()).digest()
    return (digest[0] / 255 - 0.5) * 2 * spread, (digest[1] / 255 - 0.5) * 2 * spread


def resolve(text):
    """
    A spot for a place as people write it — "Little Italy, Blüdhaven", "the
    Clocktower", "Wayne Tower R&D" — or None if it isn't somewhere on the map.
    """
    lowered = (text or "").lower()
    if not lowered.strip():
        return None
    data = gazetteer()
    # A city of its own first: Blüdhaven's waterfront is not Gotham's docks.
    for region in data.get("regions", []):
        if any(m in lowered for m in region["match"]):
            dx, dy = _jitter(lowered, region.get("spread", 4))
            return {"name": region["name"], "area": region["name"],
                    "x": round(region["x"] + dx, 1), "y": round(region["y"] + dy, 1)}
    best, length = None, 0
    for place in data["places"]:
        for name in [place["name"].lower(), *place.get("match", [])]:
            if re.search(rf"(?<!\w){re.escape(name)}(?!\w)", lowered) and len(name) > length:
                best, length = place, len(name)
    if best:
        return {"name": best["name"], "area": best["area"], "x": best["x"], "y": best["y"]}
    for island in data.get("islands", []):
        if re.search(rf"\b{island['name'].lower()}\b", lowered):
            dx, dy = _jitter(lowered, 3)
            return {"name": island["name"], "area": island["name"],
                    "x": round(island["x"] + island["w"] / 2 + dx, 1),
                    "y": round(island["y"] + island["h"] / 2 + dy, 1)}
    return None


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
    Where a patrol has them now: a stop on their beat within the area the plan
    named (all of it if the plan was vague), else the plan's own place.
    """
    beat = list(getattr(contact, "beat", ()) or ())
    there = resolve(planned) if planned and "/" not in planned else None
    if there and there["name"] not in ("Uptown", "Midtown", "Downtown"):
        beat = [b for b in beat if (resolve(b) or {}).get("area") == there["area"]] or []
        if not beat:
            return planned
    if not beat:
        return planned or None
    slot = int(t // BEAT_STEP)
    digest = hashlib.sha1(f"{contact.id}:{slot}".encode()).digest()
    return beat[digest[0] % len(beat)]
