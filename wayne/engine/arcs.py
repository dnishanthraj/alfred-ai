"""
The rogues' runs: what a case's ending does to whoever was behind it.

Let one of them get away and they don't go quiet — they get bolder. Each
rogue carries heat: a failure (got away, the hostage lost, worse) raises it,
and they come back within hours, worse than before — the Joker who walked away
from a gas attack takes hostages next; Bane, slipped, brings more men to a
riot. Bring them in and the run's over. Heat cools a little each day.

Kept in DATA_DIR/_arcs.json. What comes back is on the scanner like any call
(see incidents.at), the papers follow the run (see gazette), and the family
know it — "that's the third time he's slipped us this week".
"""
import hashlib
import json
import random
import threading
import time

from .. import paths
from ..memory.store import atomic_write, read_text

COOLS = 0.15            # heat lost a day
_lock = threading.Lock()
# How a failure raises it, and a success lowers it.
RAISE = {"got away": 0.25, "too late": 0.2, "cold": 0.2, "worse": 0.35, "lost": 0.4, "killed": 0.1}
LOWER = 0.35


def _path():
    return paths.DATA_DIR / "_arcs.json"


def _load():
    try:
        return json.loads(read_text(_path()) or "{}")
    except ValueError:
        return {}


def _save(data):
    atomic_write(_path(), json.dumps(data, ensure_ascii=False))


def heat(rogue, t=None):
    """How hot a rogue's run is right now, 0–1, cooled by the days since it last moved."""
    t = t or time.time()
    run = (_load().get("runs") or {}).get(rogue) or {}
    days = max(0.0, (t - run.get("at", t)) / 86400)
    return max(0.0, min(1.0, run.get("heat", 0.0) - COOLS * days))


def streak(rogue):
    """How many times running they've got away."""
    return ((_load().get("runs") or {}).get(rogue) or {}).get("streak", 0)


def record(case, result, t=None):
    """
    A case with one of them behind it is over: their run moves on. Got away —
    hotter, and they'll be back worse within hours; caught — it's over.
    Returns what's coming back, if anything.
    """
    rogue = case.get("suspect") or ""
    if not rogue:
        return None
    t = t or time.time()
    how = result.get("how", "")
    with _lock:
        data = _load()
        runs = data.setdefault("runs", {})
        run = runs.get(rogue) or {"heat": 0.0, "streak": 0}
        now_heat = heat(rogue, t)
        coming = None
        if result.get("caught"):
            run = {"heat": 0.0, "streak": 0, "at": t, "last": f"caught at {case['place']}"}
        elif result.get("ok"):
            run = {"heat": max(0.0, now_heat - LOWER), "streak": 0, "at": t, "last": f"stopped at {case['place']}"}
        else:
            run = {"heat": min(1.0, now_heat + RAISE.get(how, 0.2)), "streak": run.get("streak", 0) + 1, "at": t,
                   "last": f"got away from the {case['kind'].lower()} at {case['place']}"}
            coming = _comeback(rogue, case, run, t)
            if coming:
                data.setdefault("coming", []).append(coming)
        runs[rogue] = run
        data["coming"] = [c for c in data.get("coming") or [] if c["ends"] > t - 86400][-30:]
        _save(data)
    return coming


def _comeback(rogue, case, run, t):
    """What they do next, a few hours on, somewhere of theirs: worse than the last, with more of them."""
    from . import incidents, places
    entry = next((g for g in incidents.rogues() if g["name"] == rogue), None)
    if entry is None:
        return None
    rnd = random.Random(f"{rogue}:{case['id']}")
    worse = [k for k in entry.get("kinds", []) if incidents.SEVERITY.get(k, 2) >= case.get("severity", 2)]
    kind = rnd.choice(worse or entry.get("kinds") or [case["kind"]])
    haunts = [places.resolve(h) for h in entry.get("haunts", [])]
    haunts = [h for h in haunts if h] or [places.resolve(case["area"]) or {"name": case["place"], "area": case["area"],
                                                                          "x": case["x"], "y": case["y"]}]
    where = rnd.choice(haunts)
    # Back within hours — after dark if it can be.
    at = t + rnd.uniform(1.0, 5.0) * 3600
    hour = time.localtime(at).tm_hour
    if 6 <= hour < 19:
        at += (20 - hour) * 3600
    severity = min(4, max(incidents.SEVERITY.get(kind, 3), case.get("severity", 2) + 1))
    lo, hi = incidents.ARMY.get(rogue, (2, 6))
    crew = int((lo + (hi - lo) * rnd.random()) * (1 + 0.6 * run["heat"]))
    rid = "arc-" + hashlib.sha1(f"{rogue}:{case['id']}".encode()).hexdigest()[:10]
    return {"id": rid, "kind": kind, "severity": severity, "place": where["name"], "area": where.get("area", ""),
            "x": round(where["x"] + rnd.uniform(-0.4, 0.4), 2), "y": round(where["y"] + rnd.uniform(-0.4, 0.4), 2),
            "at": int(at), "ends": int(at + rnd.uniform(60, 150) * 60), "suspect": rogue, "crew": crew,
            "after": f"the {case['kind'].lower()} at {case['place']}"}


def coming(t=None):
    """The comebacks scheduled, as reports — the ones open at t, for the scanner."""
    t = t or time.time()
    return [dict(c) for c in _load().get("coming") or [] if c["at"] <= t < c["ends"]]


def lines(t=None):
    """The rogues on a run, in a line each — for the papers and for whoever's asked."""
    t = t or time.time()
    out = []
    for rogue, run in (_load().get("runs") or {}).items():
        h = heat(rogue, t)
        if h < 0.2:
            continue
        times = run.get("streak", 0)
        out.append(f"{rogue} on a run — {'got away ' + str(times) + ' times running' if times > 1 else run.get('last', '')}"
                   + (", bolder each time" if h > 0.5 else ""))
    return out


def note(rogue, t=None):
    """For someone on a case with one of them: what they know of the run."""
    times, h = streak(rogue), heat(rogue, t)
    if times < 1 or h < 0.15:
        return ""
    return (f"{rogue} has slipped the family {times} time{'s' if times != 1 else ''} running lately — "
            + ("bolder every time; this one matters." if h > 0.5 else "and knows it."))
