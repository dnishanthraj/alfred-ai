"""
What the police scanner is saying: incidents reported around Gotham, right now.

Not a feed and not scripted: a steady trickle drawn from each district's own
trouble — Crime Alley and the Narrows far more than the Upper East Side — and
from the hour, three times busier at night. Each lasts its while: reported,
units responding, then gone. The same moment always gives the same reports,
so the map, the patrols and the people on comms all agree on what's happening.

Anyone on patrol whose beat covers a report may well be there; Alfred and
Barbara, who watch the scanner, know what's out there.
"""
import hashlib
import random
import time

from . import places

SLOT = 10 * 60          # new reports are drawn ten minutes at a time
LASTS = (25, 95)        # minutes a report stays open

# (what was reported, how serious 1–4, how often relative to the rest)
KINDS = [
    ("Mugging", 1, 9), ("Break-in", 1, 9), ("Vandalism", 1, 6), ("Drug deal", 1, 8),
    ("Assault", 2, 7), ("Gang activity", 2, 7), ("Smash-and-grab", 2, 5), ("Vehicle pursuit", 2, 4),
    ("Suspicious package", 2, 3), ("Armed robbery", 3, 5), ("Shots fired", 3, 5), ("Arson", 3, 3),
    ("Hostage situation", 4, 1), ("Explosion reported", 4, 1),
]


# Places that don't get scanner calls of their own.
QUIET_PLACES = {"prison", "asylum", "statue", "lighthouse", "observatory", "garden", "water", "cemetery"}


def _crime_by_area():
    data = places.gazetteer()
    levels = {d["name"]: d.get("crime", 0.4) for d in data.get("districts", [])}
    levels.update(data.get("district_crime_extra", {}))
    return levels


def _spots():
    """Places a report can come from, with how rough their part of the city is."""
    levels = _crime_by_area()
    spots = []
    for p in places.gazetteer()["places"]:
        if p.get("icon") in QUIET_PLACES:
            continue        # nobody calls the police from inside Blackgate
        area = "Blüdhaven" if p["area"] == "Blüdhaven" else p["area"]
        level = levels.get(p["name"]) or levels.get(area)
        if level is None:
            level = max([v for k, v in levels.items() if k in (p["area"],)] or [0.4])
        spots.append((p, level))
    return spots


def _rate(t):
    hour = time.localtime(t).tm_hour
    night = hour >= 20 or hour < 5
    return 1.7 if night else (1.0 if 17 <= hour < 20 else 0.55)


def at(t=None):
    """The reports open at time t, newest first."""
    t = t or time.time()
    spots = _spots()
    weights = [level ** 2.2 for _, level in spots]
    open_now = []
    for slot in range(int(t // SLOT) - LASTS[1] * 60 // SLOT, int(t // SLOT) + 1):
        seed = int(hashlib.sha1(f"gotham-scanner:{slot}".encode()).hexdigest()[:12], 16)
        r = random.Random(seed)
        start = slot * SLOT
        count = sum(1 for _ in range(4) if r.random() < _rate(start) / 4)
        for i in range(count):
            place, level = r.choices(spots, weights=weights)[0]
            kind, severity, _ = r.choices(KINDS, weights=[w * (level if s >= 3 else 1)
                                                          for _, s, w in KINDS])[0]
            began = start + r.uniform(0, SLOT)
            ends = began + r.uniform(*LASTS) * 60 * (1 + 0.3 * (severity - 1))
            if not began <= t < ends:
                continue
            open_now.append({
                "id": f"{slot}-{i}", "kind": kind, "severity": severity,
                "place": place["name"], "area": place["area"],
                "x": round(place["x"] + r.uniform(-0.5, 0.5), 2), "y": round(place["y"] + r.uniform(-0.5, 0.5), 2),
                "at": int(began),
                "status": "reported" if t - began < 8 * 60 else "units responding",
            })
    return sorted(open_now, key=lambda i: -i["at"])


def near(areas, t=None):
    """An open report in any of these areas — the one a patrol there would be at."""
    areas = set(areas)
    for report in at(t):
        if report["area"] in areas and report["severity"] >= 2:
            return report
    return None


def scanner_note(t=None):
    """One line for whoever watches the scanner: what's open, most serious first."""
    reports = sorted(at(t), key=lambda i: (-i["severity"], -i["at"]))[:5]
    if not reports:
        return ""
    return "The police scanner right now: " + "; ".join(
        f"{r['kind'].lower()} — {r['place']} ({r['status']})" for r in reports) + "."
