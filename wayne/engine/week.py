"""
Their week: the habits that make a Tuesday theirs — Dick on the trapeze on
Mondays, Wednesdays and Fridays, Lucius on the golf course on Saturday mornings,
Tim at the pictures with Bernard on Wednesday nights, Cass and Steph at the
beach every third Sunday, and every other Sunday dinner at the Manor for
whoever comes.

Loose, as a week is: a habit happens most weeks, not every one, and the
every-few-weeks ones come round on their own count. The day planner builds on
it (see initiative.day_plan); a day with no plan of its own falls back to it.
The same day always reads the same, so everyone agrees on whose Sunday it is.
"""
import datetime
import hashlib
import time

DAYS = ("Mondays", "Tuesdays", "Wednesdays", "Thursdays", "Fridays", "Saturdays", "Sundays")


def _draw(*parts):
    return int(hashlib.sha1(":".join(map(str, parts)).encode()).hexdigest()[:8], 16) / 0xFFFFFFFF


def _hours(text):
    hh, _, mm = str(text).partition(":")
    return int(hh) + int(mm or 0) / 60


def _this_week(contact, habit, day):
    """Whether an every-few-weeks habit falls this week: its own count, or a shared one (`phase`)."""
    every = max(1, int(habit.get("every", 1)))
    if every == 1:
        return True
    iso = day.isocalendar()
    week = iso[0] * 53 + iso[1]
    phase = habit.get("phase")
    if phase is None:
        phase = int(_draw(contact.id, habit.get("doing", ""), "phase") * every)
    return week % every == int(phase) % every


def on(contact, t=None):
    """The habits that are on for them on the day of t, earliest first — chance and count included."""
    t = t or time.time()
    day = datetime.date.fromtimestamp(t)
    out = []
    for habit in getattr(contact, "week", ()) or ():
        if day.weekday() not in habit.get("days", range(7)):
            continue
        if not _this_week(contact, habit, day):
            continue
        if _draw(contact.id, habit.get("doing", ""), day.isoformat()) >= habit.get("chance", 0.85):
            continue        # not this week: something came up, or they couldn't face it
        out.append(habit)
    return sorted(out, key=lambda h: _hours(h.get("from", "0")))


def block(contact, t=None):
    """The habit they're at right now as a block of their day (as a routine block reads), or None."""
    t = t or time.time()
    local = time.localtime(t)
    hour = local.tm_hour + local.tm_min / 60
    for habit in on(contact, t):
        start, end = _hours(habit.get("from", "0")), _hours(habit.get("to", "0"))
        if end <= start:
            end += 24
        if start <= hour < end:
            return {"from": start, "to": end, "doing": habit["doing"], "status": habit.get("status", "busy"),
                    "where": habit.get("where", ""), "with": [], "habit": True}
    return None


def today(contact, t=None):
    """'at 18:00, trapeze and floor work at the old gymnastics gym (Iron Works Gym); …' — or ''."""
    return "; ".join(f"{h['from']}–{h['to']} {h['doing']} ({h['where']})" for h in on(contact, t))


def describe(contact):
    """Their week in a line: 'Mondays, Wednesdays and Fridays: trapeze…; every third Sunday: the beach…'."""
    parts = []
    for habit in getattr(contact, "week", ()) or ():
        days = habit.get("days", ())
        when = ("every day" if len(days) == 7 else
                " and ".join(filter(None, [", ".join(DAYS[d] for d in days[:-1]), DAYS[days[-1]]])) if days else "")
        every = int(habit.get("every", 1))
        if every > 1:
            when = {2: "every other ", 3: "every third ", 4: "every fourth "}.get(every, f"every {every}th ") + \
                   (DAYS[days[0]][:-1] if len(days) == 1 else when)
        parts.append(f"{when}: {habit['doing']} ({habit.get('where', '')}, {habit.get('from', '')})")
    return "; ".join(parts)


def circle(contact):
    """'Wally West — the Flash; your best friend…; Amy Rohrbach — …' — or ''."""
    return "; ".join(f"{c['name']} — {c['who']}" for c in getattr(contact, "circle", ()) or ())
