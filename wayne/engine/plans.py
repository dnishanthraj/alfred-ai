"""
Plans he makes with them — "dinner at Ralli's, Friday at eight, everyone?" —
kept, answered, and kept to.

Whoever's asked answers their own way, from what their day holds then: Dick's
in, Jason's got a thing, Cass is a maybe. The answer rides on what they'd say
anyway, as an [rsvp: yes|no|maybe] at its end. On the day, the ones who said
yes set off in time to get there — by road, as long as the roads take — and
are there together: on the map, on each other's hover cards, and in what they
say. A maybe is a coin they toss on the day.
"""
import datetime
import hashlib
import json
import re
import threading
import time
import uuid

from .. import paths
from ..memory.store import atomic_write, read_text
from . import places

_lock = threading.Lock()

# A plan, roughly: a when and a let's. Only a gate — what the plan is, the model reads.
_WHEN = re.compile(
    r"\b(tonight|today|tomorrow|this (?:evening|afternoon|weekend|week)|next (?:week|weekend|\w+day)|"
    r"(?:mon|tues|wednes|thurs|fri|satur|sun)day|weekend|noon|midnight|lunchtime|"
    r"\d{1,2}(?::\d{2})?\s*(?:am|pm)|at \d{1,2}(?::\d{2})?\b|o'?clock)\b", re.I)
_LETS = re.compile(
    r"\b(let'?s|we'?re (?:going|getting|doing|having|heading)|we are going|come (?:to|with|over|along)|"
    r"join (?:us|me)|meet (?:me|us|up)|dinner|lunch|brunch|breakfast|drinks?|coffee|movie|film|game|match|"
    r"party|bbq|barbecue|picnic|bowling|karaoke|everyone|everybody|anyone|who'?s in|you in|are you free|"
    r"free (?:on|for|tonight|tomorrow)|reservation|booked|table for)\b", re.I)
RSVP = re.compile(r"\[\s*rsvp\s*:\s*(yes|no|maybe)\s*\]", re.I)
ANSWERS = ("yes", "no", "maybe")


def _path():
    return paths.DATA_DIR / "_plans.json"


def all_plans():
    try:
        return json.loads(read_text(_path()) or "{}").get("plans", [])
    except ValueError:
        return []


def _save(rows):
    # The last few weeks' worth: a plan long past is only a memory.
    keep = [p for p in rows if p.get("until", 0) > time.time() - 14 * 86400][-60:]
    atomic_write(_path(), json.dumps({"plans": keep}, ensure_ascii=False))


def maybe_proposal(text):
    """Whether this could be him making a plan — a when and a let's. The model decides if it is."""
    return bool(text) and bool(_WHEN.search(text)) and bool(_LETS.search(text))


def read(text, invited, model, options, now=None):
    """
    The plan in what he said, as the model reads it — what, where, when, who —
    or None if he wasn't making one. `invited` is who could be asked: the chat's
    members, or the one he's talking to, as (id, first name).
    """
    from . import model as llm
    now = now or time.time()
    today = datetime.datetime.fromtimestamp(now)
    names = ", ".join(name for _, name in invited)
    instruction = (
        f"Today is {today.strftime('%A %-d %B %Y, %H:%M')}. Bruce wrote, to {names}:\n\"{text}\"\n\n"
        "Is he making a plan to meet up — a definite time to be somewhere together? If so, read it: what it "
        "is (a few words, as you'd say it: \"dinner at Ralli's\"), where (the place's name, or \"\" if he "
        "didn't say), the date (YYYY-MM-DD — work out \"Friday\" or \"tomorrow\" from today) and time (HH:MM, "
        "24-hour; a sensible one if he only said \"dinner\" or \"tonight\"), how many hours it'll last, and who "
        f"he's asking — [\"everyone\"], or first names from: {names}. A question about a plan already made, a "
        "joke, or a maybe-someday isn't one. Return JSON only: "
        '{"plan": true, "what": "...", "where": "...", "date": "YYYY-MM-DD", "time": "HH:MM", "hours": 2, '
        '"who": ["everyone"]} or {"plan": false}')
    try:
        reply = llm.ask(model, [{"role": "user", "content": instruction}],
                        {**options, "temperature": 0.1, "num_predict": 140}, fmt="json", purpose="reading a plan")
        got = json.loads(reply)
    except Exception:
        return None
    if not got.get("plan"):
        return None
    try:
        at = datetime.datetime.strptime(f"{got['date']} {got['time']}", "%Y-%m-%d %H:%M").timestamp()
    except (KeyError, ValueError, TypeError):
        return None
    if not now - 1800 < at < now + 21 * 86400:
        return None         # in the past, or so far off it's a someday
    hours = got.get("hours")
    hours = min(8.0, max(0.5, float(hours))) if isinstance(hours, (int, float)) else 2.0
    who = [str(w).strip().lower() for w in got.get("who") or [] if str(w).strip()]
    asked = [cid for cid, name in invited if not who or "everyone" in who or name.lower() in who]
    where = str(got.get("where") or "").strip()
    found = places.resolve(where) if where else None
    return {"what": str(got.get("what") or "a plan").strip()[:80], "where": found["name"] if found else where,
            "at": at, "until": at + hours * 3600, "who": asked or [cid for cid, _ in invited]}


_read = {}


def read_once(text, invited, model, options, now=None):
    """`read`, once per thing he said: on a group call, everyone hears the same plan."""
    key = (text, tuple(sorted(invited)))
    if key not in _read:
        if len(_read) > 40:
            _read.clear()
        _read[key] = read(text, invited, model, options, now)
    return _read[key]


def add(plan, made_in=""):
    """Keep a plan he's made; the same plan said twice (the same time and place) is one plan."""
    with _lock:
        rows = all_plans()
        for p in rows:
            if abs(p["at"] - plan["at"]) < 1800 and p.get("where", "") == plan.get("where", ""):
                p["who"] = sorted(set(p["who"]) | set(plan["who"]))
                _save(rows)
                return p
        plan = {**plan, "id": uuid.uuid4().hex[:10], "made_in": made_in, "made": time.time(), "answers": {}}
        rows.append(plan)
        _save(rows)
        return plan


def answer(plan_id, contact_id, said, why=""):
    """Their yes, no or maybe — the last one they gave."""
    if said not in ANSWERS:
        return
    with _lock:
        rows = all_plans()
        for p in rows:
            if p["id"] == plan_id:
                p.setdefault("answers", {})[contact_id] = {"said": said, "why": why[:160], "at": time.time()}
                _save(rows)
                return


def pending(contact_id, now=None):
    """Plans they've been asked to and haven't answered, soonest first."""
    now = now or time.time()
    return sorted((p for p in all_plans() if contact_id in p["who"] and contact_id not in p.get("answers", {})
                   and p["until"] > now), key=lambda p: p["at"])


def kept(contact_id, now=None, ahead=7 * 86400):
    """Plans they've said yes or maybe to, still to come (or under way), soonest first."""
    now = now or time.time()
    return sorted((p for p in all_plans() if p.get("answers", {}).get(contact_id, {}).get("said") in ("yes", "maybe")
                   and p["until"] > now and p["at"] < now + ahead), key=lambda p: p["at"])


def when(at, now=None):
    """'tonight at 8', 'tomorrow at 7:30', 'Friday at 8' — as people say it."""
    now = now or time.time()
    then, today = datetime.datetime.fromtimestamp(at), datetime.datetime.fromtimestamp(now).date()
    clock = then.strftime("%-I:%M").replace(":00", "") + ("am" if then.hour < 12 else "pm")
    gap = (then.date() - today).days
    day = ("tonight" if then.hour >= 17 else "today") if gap == 0 else "tomorrow" if gap == 1 else \
        then.strftime("%A") if gap < 7 else then.strftime("%A %-d %B")
    return f"{day} at {clock}"


def _place(plan):
    """' at Ralli's Family Restaurant', unless the plan already says where ('dinner at Ralli's')."""
    where = plan.get("where") or ""
    first = where.split()[0].lower() if where else ""
    return f" at {where}" if where and first not in plan["what"].lower() else ""


def _name(cid, directory):
    found = directory.get(cid) if directory else None
    return found.name if found else cid


def note(contact, directory=None, now=None, schedule=None):
    """
    For their prompt: plans they've been asked to and not answered — with what
    their day holds then, and how to answer — and the ones they've said they'll
    be at, with who else is and isn't coming. '' when there are none.
    """
    now = now or time.time()
    lines = []
    for p in pending(contact.id, now)[:2]:
        then = schedule(p["at"]) if schedule else ""
        others = [f"{_name(c, directory)} {a['said']}" for c, a in p.get("answers", {}).items() if c != contact.id]
        lines.append(
            f"He's asked you to come to {p['what']}{_place(p)}, {when(p['at'], now)}."
            + (f" Around then you'd be {then}." if then else "")
            + (f" So far: {', '.join(others)}." if others else "")
            + " You haven't said yet. Say whether you're in, your way — yes, no (and why, as you'd put it), "
            "or maybe — and end what you write with [rsvp: yes], [rsvp: no] or [rsvp: maybe].")
    for p in kept(contact.id, now)[:2]:
        mine = p["answers"][contact.id]["said"]
        coming = [_name(c, directory) for c, a in p.get("answers", {}).items() if a["said"] == "yes" and c != contact.id]
        out = [_name(c, directory) for c, a in p.get("answers", {}).items() if a["said"] == "no"]
        lines.append(
            f"You've said you'll {'come' if mine == 'yes' else 'maybe come'} to {p['what']}{_place(p)}, "
            f"{when(p['at'], now)}" + (f" — with {', '.join(coming)}" if coming else "")
            + (f"; {', '.join(out)} can't make it" if out else "") + ".")
    return " ".join(lines)


def _coin(plan_id, contact_id):
    return int(hashlib.sha1(f"{plan_id}:{contact_id}".encode()).hexdigest()[:8], 16) / 0xFFFFFFFF


def going(plan, contact_id):
    """Whether they're actually going: a yes is a yes; a maybe, a coin they toss."""
    said = plan.get("answers", {}).get(contact_id, {}).get("said")
    return said == "yes" or (said == "maybe" and _coin(plan["id"], contact_id) < 0.5)


def block(contact, t=None, lead_minutes=None):
    """
    The plan they're keeping at t, as a block of their day: on the way there
    first — setting off early enough to arrive on time — then there, with
    whoever else is going. None if nothing's on.
    """
    t = t or time.time()
    for p in all_plans():
        if contact.id not in p["who"] or not going(p, contact.id):
            continue
        lead = (lead_minutes(p) if lead_minutes else 30) * 60
        if p["at"] - lead <= t < p["until"]:
            company = [c for c in p["who"] if c != contact.id and going(p, c)]
            start = time.localtime(p["at"] - lead)
            return {"from": start.tm_hour + start.tm_min / 60, "to": 0, "doing": p["what"],
                    "status": "idle", "where": p["where"], "with": company, "plan": p["id"]}
    return None
