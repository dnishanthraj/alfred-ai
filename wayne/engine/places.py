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


_edits_cache = {"mtime": None, "edits": {}}


def _edits_path():
    from .. import paths
    return paths.DATA_DIR / "_codex_places.json"


def edits():
    """His own descriptions of places, written in the Codex: {place: text}. Read again when the file changes."""
    path = _edits_path()
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {}
    if mtime != _edits_cache["mtime"]:
        try:
            _edits_cache["edits"] = json.loads(path.read_text() or "{}")
        except ValueError:
            _edits_cache["edits"] = {}
        _edits_cache["mtime"] = mtime
    return _edits_cache["edits"]


def edit(name, text):
    """Rewrite what Gotham knows of a place (empty text puts back what the map had)."""
    from ..memory.store import atomic_write
    current = dict(edits())
    if (text or "").strip():
        current[name] = text.strip()[:1200]
    else:
        current.pop(name, None)
    atomic_write(_edits_path(), json.dumps(current, ensure_ascii=False))
    _edits_cache["mtime"] = None


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
            or _match([p for p in data["places"] if p["area"] in regions], lowered, by_name_only=True)
            or _match(venues(), lowered, by_name_only=True))
    return _spot(best) if best else None


def knows(contact, place):
    """
    Whether they'd know a place: everyone knows the districts and the landmarks;
    the bars, cafés and shops they know round where they live, work, patrol and
    keep their habits, and elsewhere only some — the way anyone knows their own
    city. The same places, every time.
    """
    if contact is None or place.get("kind") != "venue":
        return True
    theirs = {getattr(contact, "home", "") or ""} | {h.get("where", "") for h in getattr(contact, "week", ()) or ()}
    theirs |= {b.get("where", "") for b in getattr(contact, "routine", ()) or ()} | set(getattr(contact, "beat", ()) or ())
    areas = {found["area"] for found in (resolve(w) for w in theirs if w) if found}
    if place.get("area") in areas:
        return True
    import hashlib
    return int(hashlib.sha1(f"{contact.id}:{place['name']}".encode()).hexdigest()[:8], 16) / 0xFFFFFFFF < 0.6


def note(text, most=2, contact=None):
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
    for venue in venues():
        name = venue["name"].lower()
        if len(name) >= 5 and re.search(rf"(?<!\w){re.escape(name)}(?!\w)", lowered):
            found.append((len(name), {**venue, "kind": "venue"}))
    found = [p for _, p in sorted(found, key=lambda f: -f[0])]
    # Not both a district and something inside it that only matched because of it.
    lines = []
    for place in found[:most]:
        written = edits().get(place["name"])
        if not knows(contact, place):
            lines.append(f"{place['name']}: you don't know it — never been, never heard of it, as far as you "
                         "remember. Say so, or guess, as you would.")
            continue
        if place["kind"] == "district":
            here = [q["name"] for q in data["places"] if q["area"] == place["area"] and q["kind"] != "district"][:9]
            sketch = written or data.get("areas", {}).get(place["name"], "")
            lines.append(f"{place['name']}: {sketch}" + (f" In it: {', '.join(here)}." if here else ""))
        elif written or place.get("bio"):
            lines.append(f"{place['name']} ({place['area']}): {written or place['bio']}")
    if not lines:
        return ""
    return ("What you know of the places he mentioned, as anyone who knows Gotham would — use it only if it "
            "fits, in your own words:\n" + "\n".join(lines))


def near(x, y):
    """
    Where a point is, as someone passing would put it — "Burnside, by Burnside
    College" or just "Burnside" — or "" out on the water or past the city.
    """
    import math
    data = gazetteer()
    spots = [p for p in data["places"] if p.get("kind") != "district"]
    best = min(spots, key=lambda p: (p["x"] - x) ** 2 + (p["y"] - y) ** 2, default=None)
    if best is None or math.dist((best["x"], best["y"]), (x, y)) > 6:
        return ""
    area = best["area"]
    if math.dist((best["x"], best["y"]), (x, y)) < 1.2 and best["name"] != area:
        return f"{area}, by {best['name']}"
    return area


@lru_cache(maxsize=1)
def venues():
    """The city's bars, clubs, diners, cafés, gyms, cinemas, hotels and shops, as the map has them — each with its line."""
    path = Path(__file__).with_name("venues.json")
    found = json.loads(path.read_text()).get("venues", []) if path.exists() else []
    notes_path = Path(__file__).with_name("venue_notes.json")
    notes = json.loads(notes_path.read_text()).get("notes", {}) if notes_path.exists() else {}
    return [{**v, "bio": notes.get(v["name"], "")} for v in found]


def around(x, y, but="", most=3, within=1.6):
    """The landmarks nearest a point, for someone standing there: what they could see, walk to, mention."""
    import math
    data = gazetteer()
    near = sorted((math.dist((p["x"], p["y"]), (x, y)), p["name"]) for p in data["places"]
                  if p.get("kind") in ("landmark", "spot") and p["name"] != but)
    places = [name for d, name in near if d <= within][:most]
    spots = sorted((math.dist((v["x"], v["y"]), (x, y)), v) for v in venues())
    places += [f"{v['name']} ({_KIND.get(v['kind'], v['kind'])})" for d, v in spots if d <= within * 0.8][:2]
    return places


_KIND = {"club": "a club", "bar": "a bar", "diner": "a diner", "church": "a church", "fire": "a firehouse",
         "school": "a school", "cafe": "a café", "gym": "a gym", "cinema": "a cinema", "hotel": "a hotel", "shop": "a shop"}
# Talk of going somewhere for something: a drink, a coffee, a bite.
_OUTING = re.compile(r"(?i)\b(drinks?|a pint|beers?|bar|pub|coffee|caf[eé]|brunch|breakfast|lunch|dinner|bite|eat|"
                     r"food|diner|burger|club|dancing|movie|film|cinema|gym|workout|hotel|shopping)\b")


def outing(text, x, y, most=4):
    """
    Talk of going out for something, near (x, y): a few real places to suggest
    — "The Rusty Anchor (a bar, Waterloo Docks)" — or "".
    """
    import math
    if not _OUTING.search(text or ""):
        return ""
    wanted = {"drink": ("bar", "club"), "coffee": ("cafe",), "eat": ("diner", "cafe"), "club": ("club",),
              "film": ("cinema",), "gym": ("gym",), "hotel": ("hotel",), "shop": ("shop",)}
    lowered = text.lower()
    kinds = set()
    for word, which in (("drink", "drink"), ("pint", "drink"), ("beer", "drink"), ("bar", "drink"), ("pub", "drink"),
                        ("coffee", "coffee"), ("caf", "coffee"), ("brunch", "eat"), ("breakfast", "eat"),
                        ("lunch", "eat"), ("dinner", "eat"), ("bite", "eat"), ("eat", "eat"), ("food", "eat"),
                        ("diner", "eat"), ("burger", "eat"), ("club", "club"), ("dancing", "club"), ("movie", "film"),
                        ("film", "film"), ("cinema", "film"), ("gym", "gym"), ("workout", "gym"), ("hotel", "hotel"),
                        ("shopping", "shop")):
        if word in lowered:
            kinds.update(wanted[which])
    spots = sorted((math.dist((v["x"], v["y"]), (x, y)), v) for v in venues() if v["kind"] in kinds)
    picks = [f"{v['name']} ({_KIND.get(v['kind'], v['kind'])}, {v['area']})" for _, v in spots[:most]]
    return ("If you'd suggest somewhere, these are real places near you: " + "; ".join(picks) + "."
            if picks else "")


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
