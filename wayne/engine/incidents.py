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
from functools import lru_cache
from pathlib import Path

from .. import paths
from ..memory.store import atomic_write, read_text
from . import places

SLOT = 10 * 60          # new reports are drawn ten minutes at a time
LONGEST = 250           # minutes the worst report can stay open, at the worst hour in the busiest district
# How long GCPD takes to clear a report, in minutes, by how serious it is: a
# break-in is a car and a notebook; a hostage-taking needs backup and hours.
CLEARS = {1: (10, 30), 2: (20, 50), 3: (40, 100), 4: (70, 180)}

# Where each sort of trouble lives.
ROUGH = {"Crime Alley", "The Bowery", "The Narrows", "Burnley", "Waterloo Docks", "Port's Park", "Ironworks",
         "Amusement Mile", "Tricorner", "Robinsville", "Fort Joseph", "Halyard Square", "Central Business District"}
TURF = {"Crime Alley", "The Bowery", "The Narrows", "Burnley", "Waterloo Docks", "Port's Park", "Ironworks",
        "Chinatown", "Robinsville", "Tricorner", "Fort Joseph"}
DOWNTOWN = {"Diamond District", "Fashion District", "City Hall District", "Central Business District", "Old Gotham",
            "New Town", "Upper West Side", "Chinatown", "University District", "Coventry"}
NIGHTLIFE = {"Amusement Mile", "Fashion District", "Old Gotham", "Chinatown", "Coventry", "Burnley", "Halyard Square"}

# (what was reported, how serious 1–4, how often relative to the rest, where it
# belongs — its signature districts, five times likelier there, rare elsewhere).
# Most of Gotham's night is ordinary misery — a phone snatched off a moped, a
# kid stabbed over a postcode, a man who hits his wife — and under it the city's
# monsters, whose work is unmistakable.
KINDS = [
    ("Mugging", 1, 8, None), ("Break-in", 1, 8, None), ("Vandalism", 1, 5, None), ("Drug deal", 1, 7, None),
    ("Phone snatch", 1, 2, DOWNTOWN), ("Car break-in", 1, 5, None), ("Shoplifting", 1, 1.5, DOWNTOWN),
    ("Bar fight", 1, 2, NIGHTLIFE), ("Overdose", 1, 2, ROUGH),
    ("Assault", 2, 6, None), ("Gang activity", 2, 1.6, TURF), ("Smash-and-grab", 2, 4, None),
    ("Vehicle pursuit", 2, 3, None), ("Suspicious package", 2, 2, None), ("Missing person", 2, 3, None),
    ("Knifepoint robbery", 2, 4, None), ("Carjacking", 2, 3, None), ("Domestic violence", 2, 5, None),
    ("Hit-and-run", 2, 3, None), ("Unprovoked attack", 2, 3, None), ("Brawl", 2, 1, TURF),
    ("Protection racket", 2, 0.9, {"Chinatown", "The Bowery", "Robinsville", "Old Gotham"}),
    ("Armed robbery", 3, 5, None), ("Shots fired", 3, 5, None), ("Stabbing", 3, 1.3, ROUGH),
    ("Machete attack", 3, 0.5, TURF), ("Drive-by shooting", 3, 0.6, TURF), ("Turf war", 3, 0.45, TURF),
    ("Arson", 3, 3, None), ("Body found", 3, 3, None), ("Homicide", 3, 2.5, None),
    ("Kidnapping", 3, 1.5, None), ("Child abduction", 3, 0.6, None), ("Sexual assault", 3, 2, None),
    ("Riot", 3, 1, {"Crime Alley", "The Bowery", "The Narrows"}),
    ("Mass overdose", 3, 1, {"The Bowery", "Crime Alley", "The Narrows"}),
    ("Chemical spill", 3, 0.8, {"Upper East Side", "New Town"}),
    ("Riddle left at a crime scene", 3, 0.8, None),
    ("Cult gathering", 3, 0.6, {"Old Gotham", "The Narrows", "Tricorner"}),
    ("Freezing incident", 3, 0.5, {"Diamond District", "Upper East Side", "Fashion District"}),
    ("Plant overgrowth attack", 3, 0.5, {"Robinson Park", "Coventry", "University District"}),
    ("Mob hit", 3, 0.8, {"Kane Heights", "Old Gotham", "City Hall District", "Burnley"}),
    ("Mauling", 3, 0.3, {"Tricorner", "Waterloo Docks", "The Narrows", "Cherry Hills"}),
    ("Hostage situation", 4, 1, None), ("Explosion reported", 4, 1, None),
    ("Toxin exposure", 4, 0.6, {"The Narrows", "Crime Alley", "Amusement Mile"}),
    ("Laughing-gas attack", 4, 0.5, {"Amusement Mile", "Crime Alley", "Otisburg"}),
    ("Serial killing", 4, 0.3, None), ("Mass shooting", 4, 0.25, None), ("Officer down", 4, 0.4, ROUGH),
    ("Surgical abduction", 4, 0.2, {"The Narrows"}), ("Torture victim found", 4, 0.3, ROUGH),
    ("Assassination", 4, 0.25, {"Diamond District", "Financial District", "Old Gotham"}),
]
# Only ever theirs: with none of them out, these simply don't happen.
ROGUE_ONLY = {"Laughing-gas attack", "Freezing incident", "Plant overgrowth attack", "Riddle left at a crime scene",
              "Surgical abduction", "Toxin exposure", "Mauling", "Assassination"}

# Who runs the street where — Gotham's crews, so a turf war has two names in it.
GANGS = {
    "Chinatown": ["the Ghost Dragons"],
    "Crime Alley": ["the False Face Society", "the Park Row Kings"],
    "The Bowery": ["the False Face Society", "the Bowery Kings"],
    "The Narrows": ["the Street Demonz", "the Narrows Boys"],
    "Burnley": ["the Odessa Mob", "the Burnley Bloods"],
    "Waterloo Docks": ["the Odessa Mob", "Penguin's dock crews"],
    "Ironworks": ["the False Face Society", "the Ironworks crew"],
    "Robinsville": ["Penguin's crews", "the Robinsville Royals"],
    "Tricorner": ["the Tricorner Saints", "the Street Demonz"],
    "Amusement Mile": ["the Jokerz"],
    "Port's Park": ["the Port Side Kings"], "Fort Joseph": ["the Fort Joe Boys"],
    "Central Business District": ["the Blüdhaven Kings"], "Halyard Square": ["the Blüdhaven Kings"],
}
_GANG_KINDS = {"Gang activity", "Brawl", "Turf war", "Drive-by shooting", "Machete attack", "Protection racket",
               "Shots fired", "Stabbing"}

# The toll when it's over — (dead, hurt) ranges — skewed low: most stabbings
# are one kid in an ambulance, but not all of them.
TOLL = {
    "Mugging": ((0, 0), (0, 1)), "Phone snatch": ((0, 0), (0, 1)), "Knifepoint robbery": ((0, 0), (0, 1)),
    "Assault": ((0, 0), (1, 1)), "Unprovoked attack": ((0, 0), (1, 2)), "Bar fight": ((0, 0), (1, 3)),
    "Brawl": ((0, 0), (2, 5)), "Gang activity": ((0, 0), (0, 2)), "Stabbing": ((0, 1), (1, 2)),
    "Machete attack": ((0, 1), (1, 3)), "Drive-by shooting": ((0, 2), (1, 4)), "Turf war": ((0, 2), (2, 6)),
    "Shots fired": ((0, 1), (0, 2)), "Armed robbery": ((0, 1), (0, 2)), "Carjacking": ((0, 0), (0, 1)),
    "Hit-and-run": ((0, 1), (1, 1)), "Domestic violence": ((0, 1), (1, 1)), "Sexual assault": ((0, 0), (1, 1)),
    "Homicide": ((1, 1), (0, 0)), "Body found": ((1, 1), (0, 0)), "Serial killing": ((1, 3), (0, 0)),
    "Mass shooting": ((2, 7), (4, 14)), "Officer down": ((0, 1), (1, 2)), "Hostage situation": ((0, 2), (0, 3)),
    "Explosion reported": ((0, 6), (2, 18)), "Arson": ((0, 2), (0, 4)), "Riot": ((0, 2), (5, 25)),
    "Mass overdose": ((0, 4), (4, 15)), "Overdose": ((0, 1), (1, 1)), "Toxin exposure": ((0, 3), (4, 20)),
    "Laughing-gas attack": ((1, 9), (5, 30)), "Freezing incident": ((0, 2), (1, 5)),
    "Plant overgrowth attack": ((0, 1), (1, 6)), "Chemical spill": ((0, 1), (2, 10)), "Kidnapping": ((0, 0), (0, 1)),
    "Surgical abduction": ((0, 1), (1, 2)), "Mob hit": ((1, 2), (0, 1)), "Assassination": ((1, 1), (0, 2)),
    "Mauling": ((0, 1), (1, 2)), "Torture victim found": ((0, 1), (1, 1)), "Cult gathering": ((0, 1), (0, 2)),
    "Vehicle pursuit": ((0, 1), (0, 3)), "Smash-and-grab": ((0, 0), (0, 1)), "Breakout": ((0, 2), (1, 6)),
}


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
    return 1.7 if night else (1.3 if 17 <= hour < 20 else 0.95)


def at(t=None):
    """The reports open at time t, newest first."""
    t = t or time.time()
    spots = _spots()
    weights = [weight for _, _, weight in spots]
    written = dispatches()          # read once, not once per report
    out_now = _loose_kinds()
    weighted = [(k, sv, w if k not in ROGUE_ONLY or k in out_now else 0.0, home) for k, sv, w, home in KINDS]
    open_now = []
    for slot in range(int(t // SLOT) - LONGEST * 60 // SLOT, int(t // SLOT) + 1):
        seed = int(hashlib.sha1(f"gotham-scanner-2:{slot}".encode()).hexdigest()[:12], 16)
        r = random.Random(seed)
        start = slot * SLOT
        count = sum(1 for _ in range(4) if r.random() < _rate(start) / 4)
        for i in range(count):
            place, level, _ = r.choices(spots, weights=weights)[0]
            # The worst of it comes after dark.
            dark = time.localtime(start).tm_hour >= 21 or time.localtime(start).tm_hour < 4
            kind, severity, _, home = r.choices(weighted, weights=[
                w * (level if s >= 3 else 1) * ((5 if place["area"] in home else 0.3) if home else 1)
                * (1.8 if dark and s >= 3 else 1)
                for _, s, w, home in weighted])[0]
            began = start + r.uniform(0, SLOT)
            # Cleared faster by day, with more cars out; slower where the district
            # is already stretched; the serious ones go to backup first.
            hour = time.localtime(began).tm_hour
            shift = 0.7 if 7 <= hour < 19 else 1.15
            stretched = 1 + 0.4 * max(0.0, level - 0.5)
            ends = began + r.uniform(*CLEARS[severity]) * 60 * shift * stretched
            backup = severity >= 4 or (severity == 3 and r.random() < 0.6)
            if not began <= t < ends:
                continue
            report = {
                "id": f"s2-{slot}-{i}", "kind": kind, "severity": severity,
                "place": place["name"], "area": place["area"],
                "x": round(place["x"] + r.uniform(-0.5, 0.5), 2), "y": round(place["y"] + r.uniform(-0.5, 0.5), 2),
                "at": int(began), "ends": int(ends), "status": _status((t - began) / (ends - began), backup),
            }
            text = written.get(report["id"])
            if text:
                report["dispatch"] = text
            suspect = _suspect(kind, place["area"], r)
            if suspect:
                report["suspect"] = suspect
            report["toll"], gang = _toll(report)
            if gang:
                report["gang"] = gang
            open_now.append(report)
    open_now.extend(_breakouts(t))
    return sorted(open_now, key=lambda i: -i["at"])


BREAKOUT_HOURS = 3


def _breakouts(t):
    """
    Someone just got out of Arkham or Blackgate: rare, and the worst thing on the
    scanner while it lasts — a few hours of guards hurt, sirens on the causeway,
    and a manhunt anyone can join.
    """
    from . import codex
    out = []
    for rogue in _rogues():
        state = codex.where(rogue, t)
        how = state.get("how") or ""
        since = state.get("since") or 0
        if state.get("status") != "at large" or not how.startswith("broke out of") or not 0 <= t - since < BREAKOUT_HOURS * 3600:
            continue
        held = places.resolve(how.replace("broke out of", "").strip())
        if not held:
            continue
        report = {"id": f"esc-{rogue['name'].lower().replace(' ', '-')}-{int(since)}", "kind": "Breakout", "severity": 4,
                  "place": held["name"], "area": held["area"], "x": held["x"], "y": held["y"], "at": int(since),
                  "ends": int(since + BREAKOUT_HOURS * 3600), "suspect": rogue["name"],
                  "status": _status((t - since) / (BREAKOUT_HOURS * 3600), backup=True)}
        report["toll"], _ = _toll(report)
        out.append(report)
    return out


def rogues():
    """Gotham's rogues and where they are tonight (see rogues.json)."""
    return _rogues()


@lru_cache(maxsize=1)
def _rogues():
    return json.loads((Path(__file__).with_name("rogues.json")).read_text())["rogues"]


def _suspect(kind, area, r):
    """Whose work it looks like: one of theirs, done where they work, by someone who's out."""
    from . import codex
    loose = [g for g in _rogues() if kind in g["kinds"] and codex.rogue_status(g["name"]) == "at large"]
    if not loose:
        return ""
    local = [g for g in loose if area in g["haunts"]]
    pool = local or loose
    # Laughing gas is the Joker's; a robbery is almost always just a robbery.
    signature = next((w for k, _s, w, _h in KINDS if k == kind), 9) < 1
    odds = (0.85 if local else 0.6) if signature else (0.07 if local else 0.015)
    if r.random() > odds:
        return ""
    return r.choice(pool)["name"]


def _loose_kinds():
    """Every crime someone who's out does — what the monsters loose tonight could be behind."""
    from . import codex
    return {k for g in _rogues() for k in g["kinds"] if codex.rogue_status(g["name"]) == "at large"}


def _toll(report):
    """
    The dead and the hurt so far, and the crew behind it if it's a gang's — drawn
    from the report's own seed, so it's the same every time it's asked. Callers
    report the hurt; the first units confirm the dead; one of the hurt may not
    make it, and that's known by the time it's contained.
    """
    r = random.Random(f"toll:{report['id']}")
    (d0, d1), (h0, h1) = TOLL.get(report["kind"], ((0, 0), (0, 1)))
    if report.get("suspect"):
        rogue = next((g for g in _rogues() if g["name"] == report["suspect"]), None)
        if rogue and rogue.get("threat", 0) >= 4:        # the worst of them: worse
            d1, h1 = d1 + max(1, d1 // 2), h1 + max(1, h1 // 2)
    dead = d0 + int((d1 - d0 + 1) * r.random() ** 1.8)
    hurt = h0 + int((h1 - h0 + 1) * r.random() ** 1.6)
    dies_later = hurt > 0 and r.random() < 0.15
    status = report["status"]
    known_dead = dead if status != "reported" or report["kind"] in ("Homicide", "Body found", "Serial killing") else 0
    if dies_later and status in ("contained", "resolved"):
        known_dead, hurt = known_dead + 1, hurt - 1
    gang = ""
    if report["kind"] in _GANG_KINDS and not report.get("suspect"):
        crews = GANGS.get(report["area"], [])
        if crews and (report["kind"] != "Shots fired" or r.random() < 0.5) and (report["kind"] != "Stabbing" or r.random() < 0.4):
            gang = r.choice(crews)
            if report["kind"] == "Turf war" and len(crews) > 1:
                gang = " and ".join(r.sample(crews, 2))
    return {"dead": known_dead, "hurt": hurt}, gang


def toll_text(toll):
    """'2 dead, 3 hurt' — or '' when nobody is."""
    parts = ([f"{toll['dead']} dead"] if toll.get("dead") else []) + ([f"{toll['hurt']} hurt"] if toll.get("hurt") else [])
    return ", ".join(parts)


def _status(progress, backup=False):
    """
    Where a report has got to: called in, a car on its way, backup called for
    when it's more than one car can handle, contained, cleared.
    """
    if progress < 0.12:
        return "reported"
    if progress < (0.38 if backup else 0.62):
        return "units responding"
    if backup and progress < 0.66:
        return "backup requested"
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
    from . import model as llm
    hour = time.strftime("%H:%M", time.localtime(report["at"]))
    force = ("Blüdhaven PD dispatch, across the bay from Gotham" if report["area"] == "Blüdhaven"
             else "GCPD dispatch in Gotham City")
    toll = toll_text(report.get("toll") or {})
    instruction = (
        f"You are {force}. At {hour} a call comes in: {report['kind'].lower()}, "
        f"{report['place']} ({report['area']}), severity {report['severity']} of 4." + _setting(report)
        + (f" Callers report {toll}." if toll else "")
        + (f" It looks like {report['gang']}." if report.get("gang") else "")
        + " Write the dispatch as it goes out over the radio — one or two terse sentences, in dispatch voice, "
        "with the specifics a caller would give: what was seen or heard, weapons, how many, descriptions, "
        "injuries, what's still going on. A caller's own words can go in, swearing and all. This is Gotham — "
        "when it's bad, say it plainly, however grim or strange. Never mention Batman or any vigilante. "
        "Reply with only the dispatch.")
    try:
        reply = llm.ask(model, [{"role": "user", "content": instruction}],
                        {**options, "temperature": 0.95, "num_predict": 110}, purpose="a dispatch")
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


def _log_path():
    return paths.DATA_DIR / "_reportlog.json"


def logs():
    try:
        return json.loads(read_text(_log_path()) or "{}")
    except ValueError:
        return {}


# What a log should say for how bad it is and how far it's got — no further.
SCALE = {1: "minor: a car or two, nobody badly hurt unless the call says so",
         2: "serious: a few units", 3: "violent or dangerous: several units, people hurt",
         4: "the worst kind: a major response, lives at stake"}
STAGE = {"reported": "just called in: the call and the dispatch, nobody on scene yet",
         "units responding": "units on the way or just arriving: nothing's contained yet",
         "backup requested": "the first units on scene have called for backup: it isn't under control",
         "contained": "contained: the scene is secured but still being worked",
         "resolved": "cleared: how it ended — arrests, the hurt taken away, the scene released"}


def _stored(report_id):
    """A report's log as kept: {"entries": [...], "status": the status it was written up to}."""
    kept = logs().get(report_id)
    if isinstance(kept, list):          # the first logs were the entries alone
        kept = {"entries": kept, "status": ""}
    return kept or {"entries": [], "status": ""}


# How a witness or an officer would put each of them — never a name they don't know.
SEEN_AS = {"bruce": "the Bat himself — big, dark, there and then not", "nightwing": "Nightwing — the acrobat in black and blue",
           "robin": "Robin — a kid in red and black, fast, with gadgets", "batgirl": "Batgirl",
           "orphan": "a girl in black with no face, who fights like nothing anyone's seen",
           "redhood": "the Red Hood — the red helmet, the guns", "batwing": "the flying one in the armoured suit — Batwing"}


def _setting(report):
    """What the place is, so what happens there is of a piece with it — the boardwalk, the Iceberg Lounge's floor."""
    found = places.resolve(report.get("place") or "")
    bio = (found or {}).get("bio") or ""
    return f" Where it is: {report['place']} — {bio[:220]}" if bio else ""


def _case_view(report, now):
    """Who from the family is on it, as the street saw them — and how far it's got: '' if nobody."""
    from . import cases
    case = cases.for_report(report["id"])
    if not case:
        return "", ""
    members = case.get("members") or {}
    seen = []
    for cid in cases.team(case):
        m = members.get(cid) or {}
        arrived = m.get("joined", case["opened_at"]) + m.get("travel", 9) * 60
        if arrived <= now and cid in SEEN_AS:
            seen.append(f"{SEEN_AS[cid]} (from {time.strftime('%H:%M', time.localtime(arrived))})")
    result = case.get("result") or {}
    ended = case.get("outcome", "") if case["status"] == "closed" else ""
    key = f"{'+'.join(cases.team(case))}:{len(seen)}:{result.get('how', '')}"
    view = ""
    if seen:
        view = (" Witnesses and the first officers saw the vigilantes on scene: " + "; ".join(seen)
                + " — the log can say so, the way a witness or a wary officer would, never with anyone's real name.")
    if ended:
        view += f" How it ended: {ended} ({result.get('how', '')})."
    return view, key


def write_log(report, model, options, now=None):
    """
    The incident log behind a report, as GCPD keeps it: the call, the
    dispatch, what the first officers found, witnesses, backup if it came to
    that, and where it stands — timestamped. Written when someone first opens
    it, and added to when it's opened again further on: a log grows as the
    case does. Gotham's worst is logged as fact, plainly. Blocking.
    """
    from . import model as llm
    kept = _stored(report["id"])
    if report.get("suspect"):
        from . import codex
        codex.encounter(report["suspect"], f"{report['kind'].lower()} at {report['place']}")
    family, family_key = _case_view(report, now or time.time())
    if kept["entries"] and kept["status"] == report["status"] and kept.get("family", "") == family_key:
        return kept["entries"]
    began = time.strftime("%H:%M", time.localtime(report["at"]))
    clock = time.strftime("%H:%M", time.localtime(now or time.time()))
    force = "Blüdhaven PD" if report["area"] == "Blüdhaven" else "GCPD"
    suspect = report.get("suspect")
    rogue = next((g for g in _rogues() if g["name"] == suspect), None)
    toll = toll_text(report.get("toll") or {})
    facts = (f"{report['kind'].lower()} at {report['place']} ({report['area']}), first call at {began}, "
             f"severity {report['severity']} of 4 ({SCALE[report['severity']]}).{_setting(report)} It's {clock} now and the report "
             f"is {STAGE.get(report['status'], report['status'])} — the log goes that far and no further."
             + (f" Casualties as of now: {toll} — keep to exactly that." if toll else " Nobody hurt as of now.")
             + (f" The dispatch went out as: \"{report['dispatch']}\"" if report.get("dispatch") else "")
             + (f" It's {report['gang']}: the colours, the tags, the street talk say so." if report.get("gang") else "")
             + (f" Evidence points to {suspect} ({rogue['mo']}) — the log can say so, as officers would; "
                "it's as bad as they are." if rogue else "") + family)
    rules = (" Plain police language — specific, terse, human: weapons, wounds, ages, what the street saw. Quote "
             "callers, witnesses and suspects in their own words — scared, drunk, furious, swearing the way real "
             "people do. This is Gotham: murder, mutilation, torture and sexual assault happen here and are logged "
             "as facts, said plainly — never with sexual detail, and nothing sexual involving a child, ever. "
             + ("Mention the vigilantes only as witnesses saw them, above. " if family else
                "Never mention Batman or any vigilante. ") + "Return JSON only: "
             '{"log": [{"time": "HH:MM", "who": "Caller | Dispatch | a unit like 2-Adam-14 | Det. Name", "text": "..."}]}')
    if kept["entries"]:
        so_far = "\n".join(f"{e['time']} {e['who']}: {e['text']}" for e in kept["entries"])
        instruction = (f"The {force} incident log for report {report['id']} — {facts} The log so far:\n{so_far}\n"
                       f"Write the one to three entries that come next, between {kept['entries'][-1]['time']} and "
                       f"{clock}, bringing it to where it stands now." + rules)
    else:
        instruction = (f"Write the {force} incident log for report {report['id']}: {facts} Four to seven timestamped "
                       f"entries from {began} to {clock}, no later: the caller's own words (quoted), dispatch, the "
                       "first units on scene and what they found, a witness or two, backup if it came to that, "
                       "evidence, and the outcome if it's contained or cleared." + rules)
    try:
        reply = llm.ask(model, [{"role": "user", "content": instruction}],
                        {**options, "temperature": 0.9, "num_predict": 600}, fmt="json", purpose="an incident log")
        fresh = json.loads(reply).get("log") or []
    except Exception:
        return kept["entries"]
    fresh = [{"time": str(e.get("time", ""))[:5], "who": str(e.get("who", ""))[:40], "text": str(e.get("text", ""))[:400]}
             for e in fresh if isinstance(e, dict) and e.get("text")][:8 if not kept["entries"] else 3]
    entries = kept["entries"] + fresh
    if fresh:
        with _lock:
            data = logs()
            data[report["id"]] = {"entries": entries, "status": report["status"], "family": family_key}
            data = dict(list(data.items())[-200:])
            atomic_write(_log_path(), json.dumps(data, ensure_ascii=False))
            # Logged before the dispatch was written: the log's own dispatch is it, so the two agree.
            said = next((e["text"] for e in fresh if e["who"].lower() == "dispatch"), "")
            if said and not report.get("dispatch") and report["id"] not in dispatches():
                written = dispatches()
                written[report["id"]] = said[:320]
                atomic_write(_dispatch_path(), json.dumps(dict(list(written.items())[-300:]), ensure_ascii=False))
    return entries


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
    def line(r):
        toll = toll_text(r.get("toll") or {})
        return f"{r['kind'].lower()} — {r['place']} ({r['status']}{', ' + toll if toll else ''})"
    return "The police scanner right now: " + "; ".join(line(r) for r in reports) + "."
