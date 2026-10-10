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
import json
import random
import threading
import time

from .. import paths
from ..memory.store import atomic_write, read_text
from . import places

SLOT = 10 * 60          # new reports are drawn ten minutes at a time
LASTS = (25, 95)        # minutes a report stays open

# (what was reported, how serious 1–4, how often relative to the rest, where it
# belongs — its signature districts, five times likelier there, rare elsewhere)
KINDS = [
    ("Mugging", 1, 9, None), ("Break-in", 1, 9, None), ("Vandalism", 1, 6, None), ("Drug deal", 1, 8, None),
    ("Assault", 2, 7, None), ("Gang activity", 2, 7, None), ("Smash-and-grab", 2, 5, None),
    ("Vehicle pursuit", 2, 4, None), ("Suspicious package", 2, 3, None), ("Missing person", 2, 3, None),
    ("Armed robbery", 3, 5, None), ("Shots fired", 3, 5, None), ("Arson", 3, 3, None),
    ("Body found", 3, 3, None), ("Kidnapping", 3, 2, None),
    ("Riot", 3, 1, {"Crime Alley", "The Bowery", "The Narrows"}),
    ("Mass overdose", 3, 1, {"The Bowery", "Crime Alley", "The Narrows"}),
    ("Chemical spill", 3, 0.8, {"Upper East Side", "New Town"}),
    ("Riddle left at a crime scene", 3, 0.8, None),
    ("Cult gathering", 3, 0.6, {"Old Gotham", "The Narrows", "Tricorner"}),
    ("Freezing incident", 3, 0.5, {"Diamond District", "Upper East Side", "Fashion District"}),
    ("Plant overgrowth attack", 3, 0.5, {"Robinson Park", "Coventry", "University District"}),
    ("Hostage situation", 4, 1, None), ("Explosion reported", 4, 1, None),
    ("Toxin exposure", 4, 0.6, {"The Narrows", "Crime Alley", "Amusement Mile"}),
    ("Laughing-gas attack", 4, 0.5, {"Amusement Mile", "Crime Alley", "Otisburg"}),
    ("Serial killing", 4, 0.3, None),
]


# Places that don't get scanner calls of their own: nobody calls the police
# from inside Blackgate, or on Wayne Manor, or on the police.
QUIET_PLACES = {"prison", "asylum", "statue", "lighthouse", "observatory", "garden", "water", "cemetery",
                "manor", "police", "clock"}
# A hospital is where the night's calls end up, not where they start.
_CALM = {"hospital": 0.3}

_spot_cache = None


def _crime_by_area():
    data = places.gazetteer()
    levels = {d["name"]: d.get("crime", 0.1) for d in data.get("districts", [])}
    levels.update(data.get("district_crime_extra", {}))
    return levels


def _spots():
    """
    Places a report can come from, each with its weight: how rough its part
    of the city is (squared and more: Crime Alley far outweighs the Upper East
    Side), shared among the places there — a district with nine landmarks
    doesn't get nine times the trouble of one with a single name on the map,
    only three (a bigger district, a bit more).
    """
    global _spot_cache
    if _spot_cache is not None:
        return _spot_cache
    levels = _crime_by_area()
    found = [p for p in places.gazetteer()["places"] if p.get("icon") not in QUIET_PLACES]
    per_area = {}
    for p in found:
        per_area[p["area"]] = per_area.get(p["area"], 0) + 1
    spots = []
    for p in found:
        level = levels.get(p["name"]) or levels.get(p["area"]) or 0.1
        weight = level ** 2.2 / per_area[p["area"]] ** 0.5 * _CALM.get(p.get("icon"), 1.0)
        spots.append((p, level, weight))
    _spot_cache = spots
    return spots


def _rate(t):
    hour = time.localtime(t).tm_hour
    night = hour >= 20 or hour < 5
    return 1.7 if night else (1.0 if 17 <= hour < 20 else 0.55)


def at(t=None):
    """The reports open at time t, newest first."""
    t = t or time.time()
    spots = _spots()
    weights = [weight for _, _, weight in spots]
    written = dispatches()          # read once, not once per report
    open_now = []
    for slot in range(int(t // SLOT) - LASTS[1] * 60 // SLOT, int(t // SLOT) + 1):
        seed = int(hashlib.sha1(f"gotham-scanner:{slot}".encode()).hexdigest()[:12], 16)
        r = random.Random(seed)
        start = slot * SLOT
        count = sum(1 for _ in range(4) if r.random() < _rate(start) / 4)
        for i in range(count):
            place, level, _ = r.choices(spots, weights=weights)[0]
            kind, severity, _, home = r.choices(KINDS, weights=[
                w * (level if s >= 3 else 1) * ((5 if place["area"] in home else 0.3) if home else 1)
                for _, s, w, home in KINDS])[0]
            began = start + r.uniform(0, SLOT)
            ends = began + r.uniform(*LASTS) * 60 * (1 + 0.3 * (severity - 1))
            if not began <= t < ends:
                continue
            report = {
                "id": f"{slot}-{i}", "kind": kind, "severity": severity,
                "place": place["name"], "area": place["area"],
                "x": round(place["x"] + r.uniform(-0.5, 0.5), 2), "y": round(place["y"] + r.uniform(-0.5, 0.5), 2),
                "at": int(began), "ends": int(ends), "status": _status((t - began) / (ends - began)),
            }
            text = written.get(report["id"])
            if text:
                report["dispatch"] = text
            open_now.append(report)
    return sorted(open_now, key=lambda i: -i["at"])


def _status(progress):
    """Where a report has got to: called in, answered, held, over."""
    if progress < 0.12:
        return "reported"
    if progress < 0.62:
        return "units responding"
    if progress < 0.9:
        return "contained"
    return "resolved"


def get(report_id, t=None):
    return next((r for r in at(t) if r["id"] == report_id), None)


# --- the dispatches, written by the model ----------------------------------------------

_lock = threading.Lock()


def _dispatch_path():
    return paths.DATA_DIR / "_dispatch.json"


def dispatches():
    try:
        return json.loads(read_text(_dispatch_path()) or "{}")
    except ValueError:
        return {}


def unwritten(t=None):
    """Open reports with no dispatch yet, the worst first."""
    written = dispatches()
    return sorted((r for r in at(t) if r["id"] not in written and r["status"] != "resolved"),
                  key=lambda r: (-r["severity"], -r["at"]))


def write_dispatch(report, model, options):
    """
    The call as dispatch put it out: a line or two of radio, specific — what
    callers saw, how many, what state they're in. Gotham is what it is: the
    worst of these are said plainly, however grim. Blocking; run when idle.
    """
    import ollama
    hour = time.strftime("%H:%M", time.localtime(report["at"]))
    force = ("Blüdhaven PD dispatch, across the bay from Gotham" if report["area"] == "Blüdhaven"
             else "GCPD dispatch in Gotham City")
    instruction = (
        f"You are {force}. At {hour} a call comes in: {report['kind'].lower()}, "
        f"{report['place']} ({report['area']}), severity {report['severity']} of 4. Write the dispatch as it "
        "goes out over the radio — one or two terse sentences, in dispatch voice, with the specifics a "
        "caller would give: what was seen or heard, how many, descriptions, injuries, what's still going "
        "on. This is Gotham — when it's bad, say it plainly, however grim or strange. Never mention "
        "Batman or any vigilante. Reply with only the dispatch.")
    try:
        reply = ollama.chat(model=model, think=False, options={**options, "temperature": 0.95, "num_predict": 110},
                            messages=[{"role": "user", "content": instruction}])["message"]["content"]
    except Exception:
        return ""
    text = " ".join(reply.strip().strip('"').split())[:320]
    if text:
        with _lock:
            data = dispatches()
            data[report["id"]] = text
            # Only the recent ones: yesterday's calls are no use to anyone.
            data = dict(list(data.items())[-300:])
            atomic_write(_dispatch_path(), json.dumps(data, ensure_ascii=False))
    return text


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
