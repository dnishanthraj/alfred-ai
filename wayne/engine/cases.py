"""
Cases: reports off the scanner that someone in the family has taken on.

A report becomes a case when he puts someone on it — from the map, or by just
telling them ("Tim, take the robbery in the Diamond District") — or when
someone on patrol nearby takes it themselves. From then on it's theirs: they
head there (the map follows), it's part of what they know when he talks to
them, and they may well tell him about it — that they've got it, how it went.

A case closes the way a real one does: they say it's handled, in a call or a
text (the pass after every conversation listens for that), or it runs its
course and they close it themselves. Either way it gets a line of how it
ended, in their words, and it stays on the board for a while after.

Only the people who'd work a scene take cases: Dick, Tim, Barbara, Cass,
Jason, Randy — not Alfred, not Lucius, not Selina.
"""
import json
import re
import threading
import time

from .. import paths
from ..memory.store import atomic_write, read_text

FIELD = ("nightwing", "robin", "batgirl", "orphan", "redhood", "batwing")
# How long after closing a case stays on the board (and in their mind).
KEEP_CLOSED = 6 * 3600

_lock = threading.Lock()


def _path():
    return paths.DATA_DIR / "_cases.json"


def everything():
    try:
        return json.loads(read_text(_path()) or "[]")
    except ValueError:
        return []


def _save(cases):
    now = time.time()
    kept = [c for c in cases if c["status"] != "closed" or now - c.get("closed_at", now) < 3 * 86400]
    atomic_write(_path(), json.dumps(kept[-200:], ensure_ascii=False))


def known(case):
    """
    Whether he'd know about a case: he gave it, or whoever took it lets him see
    where they are. Jason working something on his own is Jason's business.
    """
    if case.get("by") == "him":
        return True
    from ..contacts import directory
    contact = directory().get(case.get("assignee"))
    return contact is None or getattr(contact, "shares_location", True)


def board(now=None):
    """Open cases he knows of, and the ones closed in the last few hours, newest first."""
    now = now or time.time()
    return sorted((c for c in everything() if known(c) and (c["status"] != "closed"
                   or now - c.get("closed_at", 0) < KEEP_CLOSED)), key=lambda c: -c["opened_at"])


def active(contact_id):
    """The case they're on right now, or None."""
    return next((c for c in everything() if c["assignee"] == contact_id and c["status"] != "closed"), None)


def recent(contact_id, now=None):
    """The last case they closed, if it was tonight."""
    now = now or time.time()
    closed = [c for c in everything() if c["assignee"] == contact_id and c["status"] == "closed"
              and now - c.get("closed_at", 0) < KEEP_CLOSED]
    return max(closed, key=lambda c: c["closed_at"]) if closed else None


def for_report(report_id):
    return next((c for c in everything() if c["id"] == report_id), None)


def assign(report, contact_id, by="him", travel=0):
    """
    Put someone on a report. Replaces whoever had it. Returns the case.
    `travel` is how many minutes it takes them to get there from wherever they are.
    """
    now = time.time()
    with _lock:
        cases = everything()
        case = next((c for c in cases if c["id"] == report["id"]), None)
        if case is None:
            case = {"id": report["id"], "kind": report["kind"], "severity": report["severity"],
                    "place": report["place"], "area": report["area"], "x": report["x"], "y": report["y"],
                    "dispatch": report.get("dispatch", ""), "suspect": report.get("suspect", ""),
                    "opened_at": now, "log": []}
            cases.append(case)
        case.update({"assignee": contact_id, "by": by, "status": "assigned", "updated_at": now,
                     "travel": round(travel, 1),
                     "due": now + (travel + 35 + 12 * report["severity"]) * 60})
        case["log"].append({"at": now, "text": f"assigned to {contact_id} ({'by him' if by == 'him' else 'took it'})"})
        _save(cases)
    return case


def advance(now=None, arrived=None):
    """
    On scene once they've had time to get there (`arrived(case)` is told).
    Returns cases that have run their course.
    """
    now = now or time.time()
    due, landed = [], []
    with _lock:
        cases = everything()
        changed = False
        for c in cases:
            if c["status"] == "assigned" and now - c["updated_at"] > c.get("travel", 9) * 60:
                c["status"], c["updated_at"] = "on scene", now
                c["log"].append({"at": now, "text": "on scene"})
                landed.append(c)
                changed = True
            if c["status"] in ("assigned", "on scene") and now > c.get("due", now + 1):
                due.append(c)
        if changed:
            _save(cases)
    for c in landed:
        if arrived:
            arrived(c)
    return due


# How a case ends with someone behind bars, in the words people write it.
_CAUGHT = re.compile(r"(?i)\b(custody|arrest\w*|cuffed|caught|locked (him|her|them) up|took (him|her|them) down|"
                     r"handed (him|her|them) (over|to)|gcpd ha(s|ve) (him|her|them)|in a cell|bagged|apprehended|"
                     r"back in arkham|back to arkham|off to blackgate)\b")


def close(case_id, outcome):
    """
    It's over: how it ended, in a line. If one of the rogues was behind it and
    the ending has them caught, they're in GCPD custody now (see codex.capture).
    """
    now = time.time()
    with _lock:
        cases = everything()
        case = next((c for c in cases if c["id"] == case_id), None)
        if case is None or case["status"] == "closed":
            return None
        case.update({"status": "closed", "closed_at": now, "updated_at": now, "outcome": outcome.strip()[:240]})
        case["log"].append({"at": now, "text": "closed"})
        _save(cases)
    if case.get("suspect") and _CAUGHT.search(outcome or ""):
        from . import codex
        codex.capture(case["suspect"], f"caught on the {case['kind'].lower()} at {case['place']}")
    return case


def mark(case_id, **fields):
    """Note something on a case — that they called for help, say."""
    with _lock:
        cases = everything()
        case = next((c for c in cases if c["id"] == case_id), None)
        if case is None:
            return None
        case.update(fields)
        _save(cases)
    return case


def drop(case_id):
    with _lock:
        cases = [c for c in everything() if c["id"] != case_id]
        _save(cases)


# The kinds where being on scene means a fight; the rest are searched, asked about, pieced together.
_VIOLENT = ("robbery", "assault", "hostage", "gang", "shots", "shooting", "riot", "smash", "kidnap", "stabbing",
            "fight", "carjack", "home invasion", "attack", "explosion", "arson")


def _roll(case, what):
    import hashlib
    return int(hashlib.sha1(f"{case['id']}:{what}".encode()).hexdigest()[:8], 16) / 0xFFFFFFFF


def goes_wrong(case):
    """Whether this one goes sideways — more of them than dispatch said, someone hurt: the serious ones, sometimes."""
    return case["severity"] >= 3 and _roll(case, "trouble") < 0.18 + 0.1 * (case["severity"] - 3)


def phase(case, now=None):
    """
    Where they've got to with it, from the clock: (key, how it is) — on the
    way; just there, sizing it up; in the thick of it; wrapping up. Worked out,
    not stored, so a call and the map agree on it at every moment.
    """
    now = now or time.time()
    if case["status"] == "closed":
        return "closed", "it's over"
    if case["status"] == "assigned":
        left = max(1, round((case["updated_at"] + case.get("travel", 9) * 60 - now) / 60))
        return "en route", f"on your way there, about {left} minute{'s' if left != 1 else ''} out"
    span = max(60.0, case.get("due", now) - case["updated_at"])
    f = (now - case["updated_at"]) / span
    violent = any(word in case["kind"].lower() for word in _VIOLENT)
    if f < 0.18:
        return "arriving", "just got there — taking it in, quiet, before anyone knows you're there"
    if f < 0.68:
        if goes_wrong(case):
            return "gone wrong", ("it's gone bad — more of them than dispatch said, or someone's hurt; you're in it "
                                  "and could use help")
        return ("in it", "in the thick of it — fighting" if violent else
                "in the thick of it — searching, asking, piecing it together")
    return "wrapping up", "it's settling — cuffs on or the scene secured, GCPD on the way, catching your breath"


def brief(contact_id, now=None):
    """What they're working, or just finished, as a line for the model."""
    now = now or time.time()
    case = active(contact_id)
    if case:
        minutes = int((now - case["opened_at"]) / 60)
        who = "He put you on it" if case["by"] == "him" else "You took it yourself"
        _, how = phase(case, now)
        return (f"You're working a case: {case['kind'].lower()} at {case['place']} ({case['area']})"
                + (f" — dispatch said: \"{case['dispatch']}\"" if case.get("dispatch") else "")
                + f". {who} {minutes} minutes ago. Right now you're {how}. It's yours to talk about, "
                "your way — what you've found, what you think, how it's going.")
    done = recent(contact_id, now)
    if done:
        return (f"Earlier tonight you closed a case: {done['kind'].lower()} at {done['place']} — "
                f"{done.get('outcome') or 'handled'}.")
    return ""


def board_note(names, now=None):
    """The case board, for whoever watches it (Alfred, Barbara)."""
    lines = []
    for c in board(now)[:6]:
        who = names.get(c["assignee"], c["assignee"])
        state = f"closed — {c.get('outcome', '')}" if c["status"] == "closed" else c["status"]
        lines.append(f"{c['kind'].lower()} at {c['place']}: {who}, {state}")
    return ("The case board: " + "; ".join(lines) + ".") if lines else ""
