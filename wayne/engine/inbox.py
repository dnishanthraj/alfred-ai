"""
Bruce's mail: his Wayne Enterprises inbox, and the cave's secure line.

What lands in it is what his life would send him, from what's actually
happened:

- the Batcomputer's night report at six — every case, who was on it and how it
  ended, the scanner's worst, the rogues caught, loose or on a run, who got
  hurt, the patrols — put together from the night itself, not written up;
- the morning's papers, as a briefing;
- Gordon, on the secure line, when there's something to say — a rogue in a
  cell, a bad night with names in it;
- Wayne Enterprises: Lucius, the board, PR, legal — the company's week;
- invitations — the galas, the box at the Knightsdome, Vicki Vale;
- what a night leaves behind: the insurer after a chase through Burnley, a
  thank-you to the Foundation from a family who don't know who to thank,
  Arkham's notice to its benefactors that someone's out;
- and spam, in its own folder.

Written by the model, a few at a time while it's loaded and idle (the night
report is assembled, never written). Kept in DATA_DIR/_inbox.json.
"""
import datetime
import hashlib
import json
import random
import threading
import time
import uuid

from .. import paths
from ..memory.store import atomic_write, read_text

FOLDERS = ("inbox", "secure", "spam", "archive")
KEEP = 400
_lock = threading.Lock()

# Who writes to him, and how they sound.
SENDERS = {
    "batcomputer": ("Batcomputer", "night-report@cave.local", "secure"),
    "gordon": ("James Gordon", "jg@secure.gcpd.local", "secure"),
    "oracle": ("Oracle", "o@clocktower.local", "secure"),
    "lucius": ("Lucius Fox", "lucius.fox@wayne-enterprises.com", "inbox"),
    "board": ("Wayne Enterprises Board Office", "board@wayne-enterprises.com", "inbox"),
    "pr": ("WE Communications", "press@wayne-enterprises.com", "inbox"),
    "legal": ("WE Legal", "legal@wayne-enterprises.com", "inbox"),
    "foundation": ("Wayne Foundation", "office@waynefoundation.org", "inbox"),
    "briefing": ("Morning Briefing", "briefing@wayne-enterprises.com", "inbox"),
}


def _path():
    return paths.DATA_DIR / "_inbox.json"


def _load():
    try:
        return json.loads(read_text(_path()) or "{}")
    except ValueError:
        return {}


def _save(data):
    data["mail"] = sorted(data.get("mail") or [], key=lambda m: m["at"])[-KEEP:]
    atomic_write(_path(), json.dumps(data, ensure_ascii=False))


def mail(folder=None):
    """His mail, newest first — one folder's, or all of it."""
    found = sorted(_load().get("mail") or [], key=lambda m: -m["at"])
    return [m for m in found if folder is None or m["folder"] == folder]


def unread():
    return sum(1 for m in _load().get("mail") or [] if not m.get("read") and m["folder"] in ("inbox", "secure"))


def deliver(sender, subject, body, folder=None, at=None, name=None, address=None, kind="", key=None):
    """
    A message arrives. `sender` is one of SENDERS or free text; `key` keeps the
    same thing from arriving twice (the night report for a date, a case's thanks).
    """
    known = SENDERS.get(sender)
    with _lock:
        data = _load()
        if key and key in (data.get("keys") or []):
            return None
        message = {"id": uuid.uuid4().hex[:12], "at": at or time.time(),
                   "from": name or (known[0] if known else sender), "address": address or (known[1] if known else ""),
                   "subject": subject.strip()[:160], "body": body.strip()[:6000],
                   "folder": folder or (known[2] if known else "inbox"), "read": False, "kind": kind}
        data.setdefault("mail", []).append(message)
        if key:
            data["keys"] = (data.get("keys") or [])[-600:] + [key]
        _save(data)
    return message


def update(message_id, **fields):
    """Read it, file it, bin it."""
    with _lock:
        data = _load()
        for m in data.get("mail") or []:
            if m["id"] == message_id:
                if fields.get("folder") in FOLDERS:
                    m["folder"] = fields["folder"]
                if "read" in fields:
                    m["read"] = bool(fields["read"])
                _save(data)
                return m
        if fields.get("delete"):
            return None
    return None


def delete(message_id):
    with _lock:
        data = _load()
        data["mail"] = [m for m in data.get("mail") or [] if m["id"] != message_id]
        _save(data)


def queue(event):
    """Something happened that'll bring mail: a case ended, someone broke out. Written when the model's free."""
    with _lock:
        data = _load()
        data["events"] = (data.get("events") or [])[-40:] + [{**event, "at": time.time()}]
        _save(data)


def _take_event():
    with _lock:
        data = _load()
        events = data.get("events") or []
        if not events:
            return None
        event = events.pop(0)
        data["events"] = events
        _save(data)
        return event


# --- the night report: assembled, not written --------------------------------------

def night_report(day=None, now=None):
    """
    The Batcomputer's report on last night — six in the evening to six this
    morning — from the record itself: cases, the scanner, rogues, the hurt, the
    patrols. (subject, body).
    """
    from ..contacts import directory
    from . import arcs, cases, codex, incidents, patrols
    now = now or time.time()
    day = day or datetime.date.fromtimestamp(now)
    end = time.mktime((day.year, day.month, day.day, 6, 0, 0, 0, 0, -1))
    start = end - 12 * 3600
    book = directory()
    names = {c.id: c.name for c in book} | {"bruce": "you"}
    worked = [c for c in cases.everything() if start <= c.get("opened_at", 0) < end]
    lines = ["# Night report", f"**{datetime.date.fromtimestamp(start).strftime('%A %-d %B')} into "
             f"{day.strftime('%A %-d %B %Y')}**, 18:00–06:00", ""]
    lines.append(f"## Cases ({len(worked)})")
    if not worked:
        lines.append("None worked.")
    for c in sorted(worked, key=lambda c: c.get("opened_at", 0)):
        who = ", ".join(names.get(m, m) for m in cases.team(c)) or "—"
        how = (c.get("result") or {}).get("how") or ("still open" if c["status"] != "closed" else "closed")
        when = time.strftime("%H:%M", time.localtime(c.get("opened_at", start)))
        lines.append(f"- **{when} · {c['kind']}** — {c['place']} ({c['area']})  ")
        lines.append(f"  Team: {who}. Outcome: **{how}**." + (f" Behind it: {c['suspect']}." if c.get("suspect") else "")
                     + (f"  \n  > {c['outcome']}" if c.get("outcome") else ""))
    hurt = [(m, i) for c in worked for m, i in ((c.get("result") or {}).get("hurt") or {}).items()]
    lines += ["", "## Injuries"] + ([f"- {names.get(m, m).capitalize()}: {i}." for m, i in hurt] or ["None reported."])
    seen = {}
    for hours in range(0, 12):
        for r in incidents.at(start + hours * 3600 + 1800):
            seen[r["id"]] = r
    worst = sorted(seen.values(), key=lambda r: (-r["severity"], -(r.get("toll") or {}).get("dead", 0)))[:6]
    lines += ["", "## The scanner", f"{len(seen)} calls; the worst:"]
    for r in worst:
        toll = incidents.toll_text(r.get("toll") or {})
        lines.append(f"- {time.strftime('%H:%M', time.localtime(r['at']))} · **{r['kind']}** — {r['place']} ({r['area']})"
                     + (f", {toll}" if toll else "") + (f" — {r['suspect']}" if r.get("suspect") else ""))
    rogue_lines = []
    for rogue in incidents.rogues():
        where = codex.where(rogue, end)
        if where.get("how") and start <= (where.get("since") or 0) < end:
            rogue_lines.append(f"- **{rogue['name']}**: {where['how']}.")
    rogue_lines += [f"- {line}." for line in arcs.lines(end)]
    lines += ["", "## Rogues"] + (rogue_lines or ["No change."])
    covered = {}
    for watch_t in range(int(start + 3 * 3600), int(end), int(patrols.WATCH)):
        for cid, on in patrols.roster(watch_t).items():
            covered.setdefault(names.get(cid, cid), []).append(on["sector"].split(",")[0])
    lines += ["", "## Patrols"] + ([f"- **{who}**: {', '.join(dict.fromkeys(areas))}." for who, areas in covered.items()]
                                   or ["Nobody out."])
    good = sum(1 for c in worked if (c.get("result") or {}).get("ok"))
    subject = (f"Night report — {len(worked)} case{'s' if len(worked) != 1 else ''}, {good} closed well"
               + (f", {len(hurt)} hurt" if hurt else ""))
    return subject, "\n".join(lines)


def write_night_report(now=None):
    """Six in the morning: last night's report, once."""
    now = now or time.time()
    day = datetime.date.fromtimestamp(now)
    if time.localtime(now).tm_hour < 6:
        return None
    key = f"night:{day.isoformat()}"
    if key in (_load().get("keys") or []):
        return None
    subject, body = night_report(day, now)
    at = time.mktime((day.year, day.month, day.day, 6, 0, 0, 0, 0, -1))
    return deliver("batcomputer", subject, body, at=at, kind="report", key=key)


def write_briefing(now=None):
    """The morning papers, as a briefing — once the day's are out."""
    from . import gazette
    now = now or time.time()
    day = datetime.date.fromtimestamp(now)
    if time.localtime(now).tm_hour < 7 or not gazette.written(day):
        return None
    items = gazette.today(day)
    if not items:
        return None
    body = "\n\n".join(f"### {i['headline']}\n*{i['outlet']}{' — ' + i['by'] if i['by'] and i['by'] != i['outlet'] else ''}*"
                       f"\n\n{i['dek']}" for i in items)
    # It came at seven, whenever he's reading it.
    seven = time.mktime((day.year, day.month, day.day, 7, 0, 0, 0, 0, -1))
    return deliver("briefing", f"Your morning briefing — {day.strftime('%A %-d %B')}",
                   "Good morning. Gotham today, as the papers and the channels have it.\n\n---\n\n" + body,
                   kind="briefing", key=f"briefing:{day.isoformat()}", at=min(now, seven) if now >= seven else now)


# --- written by the model ------------------------------------------------------------

def _draw(*parts):
    return int(hashlib.sha1(":".join(map(str, parts)).encode()).hexdigest()[:8], 16) / 0xFFFFFFFF


def _ask(model, options, instruction, purpose):
    from . import model as llm
    try:
        reply = llm.ask(model, [{"role": "user", "content": instruction}],
                        {**options, "temperature": 0.85, "num_predict": 700}, fmt="json", purpose=purpose)
        found = json.loads(reply)
    except Exception:
        return None
    return found if isinstance(found, dict) else None


_MAIL_SHAPE = ('Return JSON only: {"from_name": "...", "from_address": "...", "subject": "...", '
               '"body": "the email itself, paragraphs separated by blank lines, signed off as they would — light '
               'markdown where a real email would have it: **bold** for a date or a figure, a short - list, a > quote; '
               'headings only in a newsletter or a formal notice"}')


def _context(now):
    from . import gazette
    day = datetime.date.fromtimestamp(now)
    papers = "; ".join(f"“{i['headline']}”" for i in (gazette.today(day) or [])[:4])
    return (f"It's {day.strftime('%A %-d %B %Y')}, {time.strftime('%H:%M', time.localtime(now))}, in Gotham City. "
            f"Bruce Wayne is CEO-in-name of Wayne Enterprises and its public face (the city thinks him a "
            f"charming, unserious billionaire). " + (f"This morning's papers: {papers}. " if papers else ""))


def write_one(model, options, now=None):
    """
    One piece of mail, if any's due: an event's first (a capture, a bad night, a
    breakout, a chase's damage, a rescue), then the company's day, an
    invitation, the spam. Blocking; run while the model's idle.
    """
    now = now or time.time()
    event = _take_event()
    if event:
        return _from_event(model, options, event, now)
    hour = time.localtime(now).tm_hour
    day = datetime.date.fromtimestamp(now).isoformat()
    sent = [m for m in mail() if datetime.date.fromtimestamp(m["at"]).isoformat() == day]
    company = [m for m in sent if m.get("kind") == "company"]
    weekend = datetime.date.fromtimestamp(now).weekday() >= 5
    if 8 <= hour < 19 and len(company) < (1 if weekend else 3) and _draw("company", day, len(company)) < (0.15 if weekend else 0.5):
        return _company(model, options, now)
    invites = [m for m in mail() if m.get("kind") == "invitation" and now - m["at"] < 2 * 86400]
    if 9 <= hour < 21 and not invites and _draw("invite", day) < 0.6:
        return _invitation(model, options, now)
    spam = [m for m in sent if m.get("kind") == "spam"]
    if len(spam) < 4 and _draw("spam", day, len(spam), hour) < 0.35:
        return _spam(model, options, now)
    return None


def _company(model, options, now):
    which = random.choice(["lucius", "lucius", "board", "pr", "legal", "foundation"])
    name, address, folder = SENDERS[which]
    who = {"lucius": "Lucius Fox, Wayne Enterprises' president and CEO — dry, exact, quietly amused by Bruce, and "
                     "the one who keeps the company running (R&D, Applied Sciences, acquisitions, the books)",
           "board": "the board office's secretary — formal, calendar-driven: meetings, votes, minutes, a director's grumbling",
           "pr": "WE Communications — the PR team managing Bruce Wayne's image, the press, interview requests (Vicki Vale), "
                 "the Globe's latest about him",
           "legal": "WE Legal — careful, clause-heavy: contracts, a lawsuit, a settlement, signatures needed",
           "foundation": "the Wayne Foundation's director — the charity: the Narrows children's centre, grants, the "
                         "Thomas Wayne Memorial Clinic, a gala to plan"}[which]
    found = _ask(model, options, _context(now) + f"Write one email to Bruce Wayne from {who}, about something real in "
                 "the company's or the city's week — specific names, numbers, a decision he's needed for — not about "
                 "the vigilantes. It reads like real corporate mail. " + _MAIL_SHAPE, "a company email")
    if not found or not found.get("subject"):
        return None
    return deliver(which, found["subject"], found.get("body", ""), folder=folder, kind="company",
                   name=name, address=address)


def _invitation(model, options, now):
    found = _ask(model, options, _context(now) + "Write one invitation to Bruce Wayne — a gala, a charity auction, a "
                 "box at the Knightsdome, the mayor's fundraiser, a gallery opening on Museum Mile, a dinner Vicki Vale "
                 "insists on, an old Gotham family's anniversary. From whoever would send it, with the date, the place "
                 "(somewhere real in Gotham), the dress, an RSVP. " + _MAIL_SHAPE, "an invitation")
    if not found or not found.get("subject"):
        return None
    return deliver(found.get("from_name") or "Invitation", found["subject"], found.get("body", ""), folder="inbox",
                   kind="invitation", name=found.get("from_name"), address=found.get("from_address"))


def _spam(model, options, now):
    found = _ask(model, options, _context(now) + "Write one piece of spam that lands in Bruce Wayne's junk folder — "
                 "Gotham-flavoured: a crypto scheme, a 'Wayne heir' scam, cheap Iceberg Lounge VIP passes, a "
                 "too-good yacht charter, an Arkham-surplus auction, a phishing 'WE IT security notice'. Badly "
                 "written in the way spam is. " + _MAIL_SHAPE, "spam")
    if not found or not found.get("subject"):
        return None
    return deliver(found.get("from_name") or "Unknown", found["subject"], found.get("body", ""), folder="spam",
                   kind="spam", name=found.get("from_name"), address=found.get("from_address"))


def _from_event(model, options, event, now):
    """What a night leaves in his mail."""
    kind = event.get("kind")
    if kind == "case":
        how, rogue = event.get("how", ""), event.get("suspect", "")
        place, what = event.get("place", ""), event.get("what", "")
        if event.get("caught") or how in ("lost", "worse", "killed") or event.get("severity", 0) >= 4:
            # Gordon, on the secure line: a rogue in a cell, or a night with names in it.
            found = _ask(model, options, _context(now) + "Write one email from Commissioner James Gordon to Batman, "
                         "on their secure line — weary, decent, few words, signed 'Jim' or 'G'. About last night: "
                         f"the {what} at {place}" + (f", {rogue} behind it" if rogue else "") + f" — it ended: {how}"
                         + (f"; {event['caught']} is in GCPD custody" if event.get("caught") else "")
                         + ". What's next for GCPD, what he needs, what he can't say in public. " + _MAIL_SHAPE,
                         "Gordon's email")
            if found and found.get("subject"):
                return deliver("gordon", found["subject"], found.get("body", ""), kind="gordon",
                               key=f"gordon:{event.get('id')}")
        if how in ("saved", "caught", "contained") and event.get("severity", 0) >= 3 and _draw("thanks", event.get("id")) < 0.4:
            found = _ask(model, options, _context(now) + "Write one email to the Wayne Foundation (forwarded to Bruce) "
                         f"from someone who was there at the {what} at {place} last night — saved by masked people they "
                         "can't name and don't know how to thank; they're writing to the biggest charity in the city "
                         "because they don't know who else to tell. Human, unpolished. " + _MAIL_SHAPE, "a thank-you")
            if found and found.get("subject"):
                return deliver("foundation", "Fwd: " + found["subject"], found.get("body", ""), kind="thanks",
                               key=f"thanks:{event.get('id')}")
        if event.get("chase") and _draw("claim", event.get("id")) < 0.5:
            found = _ask(model, options, _context(now) + "Write one email from Wayne Enterprises' insurers to Bruce "
                         f"Wayne about a claim: property WE owns or insures near {place} was damaged last night in a "
                         "police chase (a car through a storefront, a security barrier, a fleet van). Dry, formal, a "
                         "claim number. " + _MAIL_SHAPE, "an insurance email")
            if found and found.get("subject"):
                return deliver("Gotham Mutual Insurance", found["subject"], found.get("body", ""), kind="insurance",
                               name=found.get("from_name") or "Gotham Mutual Insurance",
                               address=found.get("from_address") or "claims@gothammutual.com", key=f"claim:{event.get('id')}")
        return None
    if kind == "breakout":
        found = _ask(model, options, _context(now) + "Write one email from Arkham Asylum's administration to its "
                     f"benefactors (the Wayne Foundation funds a wing) about an escape: {event.get('rogue')} "
                     f"{event.get('how', 'is out')}. Careful, legalistic, reassuring, 'the matter is in hand'. "
                     + _MAIL_SHAPE, "an Arkham notice")
        if found and found.get("subject"):
            return deliver("Arkham Asylum Administration", found["subject"], found.get("body", ""), kind="arkham",
                           name=found.get("from_name") or "Arkham Asylum Administration",
                           address=found.get("from_address") or "administration@arkham-asylum.org",
                           key=f"arkham:{event.get('rogue')}:{int(event.get('at', 0) // 86400)}")
    return None
