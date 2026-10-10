"""
What a contact is doing right now, and what they mean to do next.

Everything about how a contact behaves on their phone comes from this one
state, rather than from a rule per behaviour:

  * the dot beside their name — online, idle, busy, offline;
  * how soon they read a text, and whether they answer it properly or in a
    line from the middle of something;
  * when they text first, or ring — because they said they would, because
    the errand he sent them on is done, or because something came to mind;
  * what they're in the middle of when he calls.

Three things set it, in order of precedence:

  1. **What the conversation established.** He sends Dick to the docks; Lucius
     says he's going into a board meeting; Selina says she's going to bed.
     After each exchange a short model pass reads what was said and records
     what they're now doing and for how long (see `initiative.afterthought`).
     Nobody should be online at the docks.
  2. **Being in a conversation.** Someone who has just texted back is holding
     their phone, and stays online for a few minutes — so a back-and-forth
     runs at the speed of a back-and-forth, not one reply an hour.
  3. **Their routine.** Asleep at night, at work by day, on patrol on some
     nights but not others — declared in the profile, and decided per day
     with a seeded draw so it never flickers between refreshes. Outside the
     routine, free time drifts between online and idle in twenty-minute
     spells.

And intentions — "I'll call you when I'm out", "text me when it's done",
"report back" — are kept here with the time they fall due. The console's
pulse carries them out (see `Console.pulse`).
"""
import hashlib
import json
import math
import random
import re
import threading
import time
import uuid

from .. import paths
from ..memory.store import atomic_write, read_text
from . import places, travel

ONLINE, IDLE, BUSY, OFFLINE = "online", "idle", "busy", "offline"
STATUSES = (ONLINE, IDLE, BUSY, OFFLINE)

# How long someone stays online after they last sent or read something of his.
ENGAGED_FOR = 300
# Free time comes in spells this long: phone in hand, or put down.
_SPELL = 1200

_lock = threading.Lock()
_registry = {}


def of(contact):
    """The one Presence for a contact, shared by the console and their session."""
    with _lock:
        found = _registry.get(contact.id)
        if found is None or found.contact is not contact:
            found = _registry[contact.id] = Presence(contact)
        return found


def _draw(*parts):
    """A random number fixed by its inputs: the same day gives the same answer."""
    digest = hashlib.sha1("|".join(str(p) for p in parts).encode()).digest()
    return int.from_bytes(digest[:8], "big") / 2 ** 64


def _drift(seed, t, knot):
    """
    Noise that wanders rather than jumps: a value in [0, 1] at time t, eased
    between random values `knot` seconds apart — the same at the same moment,
    for everyone who asks.
    """
    k = t / knot
    i = math.floor(k)
    f = k - i
    f = f * f * (3 - 2 * f)
    a, b = _draw(seed, i), _draw(seed, i + 1)
    return a + (b - a) * f


def tide(t=None):
    """
    How much the whole city is on its phones right now, about 0.4–1.9: the
    day's own shape — the commute, lunch, the evening scroll, the dead small
    hours — on waves nobody could name, so now and then everyone happens to be
    online at once, and now and then nobody is.
    """
    t = t or time.time()
    local = time.localtime(t)
    hour = local.tm_hour + local.tm_min / 60
    shape = (0.5 if hour < 6 else 1.05 if hour < 9.5 else 0.85 if hour < 12 else 1.15 if hour < 13.75
             else 0.85 if hour < 17 else 1.0 if hour < 20 else 1.3 if hour < 23.5 else 0.95)
    wave = 0.7 + 0.6 * _drift("tide", t, 1500)
    surge = 1.0 + 0.45 * max(0.0, _drift("surge", t, 700) - 0.82) / 0.18     # the odd everyone-at-once
    return shape * wave * surge


def mood(contact, t=None):
    """
    How glued to their phone they are right now, about 0.3–2: their habit —
    Dick lives on his, Randy barely touches his — carried on slow waves of
    their own: a flowing hour, then an afternoon of one-word answers.
    """
    t = t or time.time()
    habit = float((contact.texting_pace or {}).get("phone", 0.3))
    knot = 1200 + 1800 * _draw(contact.id, "knot")          # how long their spells run: 20–50 minutes
    return (0.55 + habit * 1.6) * (0.4 + 1.2 * _drift(f"mood:{contact.id}", t, knot))


def engagement(contact, t=None):
    """The two together — the city's pull and their own — kept to something a person could be."""
    return max(0.3, min(2.2, tide(t) * mood(contact, t)))


# Two plans a short walk apart put people in the same place (in map units).
TOGETHER_WITHIN = 6.0


def together(presence, t=None):
    """
    (leader, group) — who this person is actually with at t, as Presences,
    and whose place the group is in.

    A plan or a conversation can say "with Cass"; it only holds if it holds
    for Cass too: she said the same, or she's free then, or her own plan
    already has her there. What she said about where she is outranks anyone's
    plan; someone asleep or out of reach is in nobody's plans but their own;
    and Jason and Randy, who keep their lives to themselves, are with someone
    only when they say so too. Everyone in a group is in one place — so the
    map, the card under their name and what each of them is told all agree.
    """
    from ..contacts import directory
    t = t or time.time()
    book = directory()
    everyone = {presence.contact.id: presence}
    for contact in book:
        if contact.id not in everyone:
            everyone[contact.id] = _registry.get(contact.id) or of(contact)
    order = {cid: i for i, cid in enumerate(everyone)}
    seen = {}

    def situation(cid):
        if cid not in seen:
            seen[cid] = everyone[cid]._situation(t)
        return seen[cid]

    def near(a, b):
        here = places.resolve(everyone[a]._home_is_home(situation(a)[0].get("where") or ""))
        there = places.resolve(everyone[b]._home_is_home(situation(b)[0].get("where") or ""))
        return bool(here and there) and math.dist((here["x"], here["y"]), (there["x"], there["y"])) <= TOGETHER_WITHIN

    def holds(a, b):
        """a says they're with b: is b with a?"""
        if b not in everyone or b == a:
            return False
        block, company, firm = situation(b)
        if a in company:
            return True             # they both say so
        if firm or block.get("status") == OFFLINE:
            return False            # b said where they are, or is asleep or out of reach
        if not getattr(everyone[b].contact, "shares_status", True):
            return False            # in nobody's plans but their own, unless they say so too
        return not block or near(a, b)      # free then, or already there by their own plan

    links = {cid: set() for cid in everyone}
    for a in everyone:
        for b in situation(a)[1]:
            if holds(a, b):
                links[a].add(b)
                links[b].add(a)
    group, todo = {presence.contact.id}, [presence.contact.id]
    while todo:
        for other in links[todo.pop()] - group:
            group.add(other)
            todo.append(other)
    # The place is the one whoever arranged it had in mind: what someone said
    # before any plan, someone who named the others before someone named.
    leader = min(group, key=lambda cid: (not situation(cid)[2],
                                         not (set(situation(cid)[1]) & group), order[cid]))
    return everyone[leader], [everyone[cid] for cid in sorted(group, key=order.get)]


def sharing(company):
    """Those of them who let him see where they are: "with Jason" would give Jason away."""
    from ..contacts import directory
    book = directory()
    return [c for c in company if book.get(c) is None or getattr(book.get(c), "shares_location", True)]


def describe_until(until, now=None):
    """'for about an hour', 'until about 7' — how long, said the way a person would."""
    if not until:
        return ""
    left = until - (now or time.time())
    if left <= 0:
        return ""
    if left < 50 * 60:
        return f"for another {max(5, int(round(left / 300.0)) * 5)} minutes or so"
    if left < 100 * 60:
        return "for about another hour"
    return f"until about {time.strftime('%-I%p', time.localtime(until)).lower()}"


class Presence:
    def __init__(self, contact):
        self.contact = contact
        self.path = paths.contact_dir(contact.id) / "presence.json"
        # The afterthought writes from a worker thread while the console's
        # loop reads and updates: one lock round every change and save, or a
        # kept promise came back from a stale copy and was kept twice.
        self._lock = threading.RLock()
        self._state = self._load()
        # When the page last looked: a move it saw happen is travelled; one that
        # happened while nobody watched (overnight, the console off) just is.
        self._looked = None

    # --- storage -----------------------------------------------------------

    def _load(self):
        try:
            state = json.loads(read_text(self.path) or "{}")
        except ValueError:
            state = {}
        state.setdefault("activity", None)
        state.setdefault("last_active", 0)
        state.setdefault("intents", [])
        return state

    def save(self):
        with self._lock:
            atomic_write(self.path, json.dumps(self._state, indent=1))

    # --- the routine -------------------------------------------------------

    def _routine(self, t):
        """
        The routine block covering time t, if there is one today. A loose
        schedule, not a timetable: each block's edges drift by up to an hour or
        so, differently every day, and some days it doesn't happen at all.
        """
        local = time.localtime(t)
        hour = local.tm_hour + local.tm_min / 60
        plans = self._state.get("plans") or {}
        for began in (t, t - 86400):            # a night may have begun yesterday
            key = time.strftime("%Y-%m-%d", time.localtime(began))
            for block in plans.get(key) or []:
                start, end = block["from"], block["to"]
                if end <= start:
                    end += 24
                at = hour + (24 if began < t - 1 else 0)
                if start <= at < end:
                    return block
        for index, block in enumerate(self.contact.routine):
            for began in (t, t - 86400):        # a night may have begun yesterday
                day = time.localtime(began)
                key = time.strftime("%Y-%m-%d", day)
                if plans.get(key):
                    continue    # they planned that day themselves; the routine is only a fallback
                if "days" in block and day.tm_wday not in block["days"]:
                    continue
                if _draw(self.contact.id, index, key) >= block.get("chance", 1.0):
                    continue     # not that day
                drift = block.get("drift", 1.25)
                start = block.get("from", 0) + (_draw(self.contact.id, index, key, "start") - 0.5) * 2 * drift
                end = block.get("to", 0) + (_draw(self.contact.id, index, key, "end") - 0.5) * 2 * drift
                if end <= start:
                    end += 24                   # runs past midnight
                at = hour + (24 if began < t - 1 else 0)
                if start <= at < end:
                    return block
        return None

    def has_plan(self, t=None):
        key = time.strftime("%Y-%m-%d", time.localtime(t or time.time()))
        return bool((self._state.get("plans") or {}).get(key))

    def plan_today(self, t=None):
        key = time.strftime("%Y-%m-%d", time.localtime(t or time.time()))
        return list((self._state.get("plans") or {}).get(key) or [])

    def set_plan(self, blocks, t=None):
        """Their own plan for the day — kept for today and yesterday's late night."""
        key = time.strftime("%Y-%m-%d", time.localtime(t or time.time()))
        with self._lock:
            plans = dict(self._state.get("plans") or {})
            plans[key] = blocks
            self._state["plans"] = dict(sorted(plans.items())[-2:])
            self.save()

    def _whim(self, t):
        """
        Something unplanned, now and then, in free time: walking the dog, out
        with a friend, at the bookshop. Decided per two-hour spell, so it holds
        while it lasts and doesn't flicker.
        """
        whims = self.contact.texting_pace.get("whims") or getattr(self.contact, "whims", ())
        if not whims or self.has_plan(t):
            return None     # a plan of their own already has the day's spontaneity in it
        spell = int(t // 7200)
        if _draw(self.contact.id, "whim", spell) >= self.contact.texting_pace.get("whim_rate", 0.18):
            return None
        whim = whims[int(_draw(self.contact.id, "which", spell) * len(whims))]
        start, end = whim.get("hours", (8, 22))
        hour = time.localtime(t).tm_hour
        if not (start <= hour < end if start < end else hour >= start or hour < end):
            return None     # nobody's on the golf course at three in the morning
        lasts = whim.get("minutes", 60) * 60
        begins = spell * 7200 + _draw(self.contact.id, "when", spell) * max(0, 7200 - lasts)
        return whim if begins <= t < begins + lasts else None

    def _free_time(self, t):
        """
        Phone in hand or put down, in spells — the same spell reads the same.
        Gotham's people live at night: in the evening and small hours their
        phones are out far more than in the day (`phone_night`).
        """
        pace = self.contact.texting_pace
        hour = time.localtime(t).tm_hour
        night = hour >= 20 or hour < 4
        share = pace.get("phone_night", pace.get("phone", 0.3)) if night else pace.get("phone", 0.3)
        # On the city's waves and their own: some evenings everyone's on, some
        # afternoons nobody is.
        share = min(0.95, share * engagement(self.contact, t))
        return ONLINE if _draw(self.contact.id, int(t // _SPELL)) < share else IDLE

    # --- now ---------------------------------------------------------------

    def now(self, t=None):
        """
        {"status", "doing", "until", "source", "last_active"} for time t.
        `doing` is a few words ("on patrol", "in a board meeting") or "".
        """
        t = t or time.time()
        last = self._state["last_active"]
        activity = self._state["activity"]
        if activity and activity.get("until", 0) > t:
            return {"status": activity["status"], "doing": activity["doing"],
                    "until": activity["until"], "source": "conversation", "last_active": last,
                    "terminal": activity.get("terminal", False)}
        if 0 <= t - last < ENGAGED_FOR:
            return {"status": ONLINE, "doing": "", "until": last + ENGAGED_FOR,
                    "source": "engaged", "last_active": last}
        block = self._routine(t) or self._whim(t)
        if block:
            return {"status": block.get("status", BUSY), "doing": block.get("doing", ""),
                    "until": 0, "source": "routine", "last_active": last,
                    "terminal": block.get("terminal", False)}
        return {"status": self._free_time(t), "doing": "", "until": 0,
                "source": "free", "last_active": last}

    def line_key(self, t=None):
        """
        What their status line is about: the thing they're doing, or for
        free time — and for anyone who doesn't share — the day. A line is
        written once per key, so it changes when their situation does.
        """
        state = self.now(t)
        t = t or time.time()
        day = time.strftime("%Y-%m-%d", time.localtime(t))
        if not self.contact.shares_status:
            return f"day:{day}"             # nothing to give away; a new mood a day
        if not state["doing"]:
            # Free time: a fresh line every few hours — the spell length drawn
            # per contact and day, so they don't all change on the hour together.
            span = 2 + int(_draw(self.contact.id, "span", day) * 4)     # 2–5 hours
            return f"free:{day}:{time.localtime(t).tm_hour // span}"
        return f"{state['source']}:{state['doing']}"

    def line(self, t=None):
        """
        The status line they wrote for now — or, until they've written one,
        the last they set. A status stays up until someone changes it.
        """
        lines = self._state.get("lines") or {}
        return lines.get(self.line_key(t)) or (list(lines.values())[-1] if lines else "")

    def has_current_line(self, t=None):
        return self.line_key(t) in (self._state.get("lines") or {})

    def set_line(self, key, text):
        with self._lock:
            lines = dict(self._state.get("lines") or {})
            lines[key] = text.strip()[:80]
            # Only the recent ones: yesterday's lines are no use to anyone.
            self._state["lines"] = dict(list(lines.items())[-12:])
            self.save()

    def whereabouts(self, t=None):
        """
        (place, [contact ids with them]) for right now — always from what
        they're actually doing: somewhere a conversation sent them, the place in
        their plan, a spot on their beat while they patrol (moving every
        quarter hour or so), or home when nothing puts them elsewhere. Anyone
        they're really with (see `together`) is in the same place, and has
        them in their company too.
        """
        t = t or time.time()
        leader, group = together(self, t)
        place = (self if leader is self else leader)._own_place(t)
        return place, [p.contact.id for p in group if p is not self]

    def _situation(self, t):
        """
        What decides where they are at t, on their own: (block, company, firm).
        Firm is what a conversation established — they said where they are —
        and it outranks any plan, theirs or anyone else's.
        """
        activity = self._state.get("activity")
        if activity and activity.get("since", 0) <= t < activity.get("until", 0):
            return activity, list(activity.get("with") or []), True
        block = self._routine(t) or self._whim(t) or {}
        return block, list(block.get("with") or []), False

    def _own_place(self, t):
        """Where they'd be at t by themselves, before anyone they're with comes into it."""
        block, _, firm = self._situation(t)
        if firm:
            return self._activity_place(block, t)
        return self._planned_place(block, t)

    def _activity_place(self, activity, t):
        """
        Where what they said they're doing puts them: the patrol they named,
        walked where they said; the place they named; a place in what they're
        doing ("a board meeting at Wayne Tower"); home, gone to sleep; and
        otherwise wherever they were when they said it.
        """
        doing, where = activity.get("doing") or "", activity.get("where") or ""
        if places.is_patrol(doing):
            spot = places.patrol_spot(self.contact, where, t)
            if spot:
                return spot
        if where:
            return self._home_is_home(where)
        named = places.resolve(doing)
        if named:
            return named["name"]
        if activity.get("status") == OFFLINE:
            return getattr(self.contact, "home", "") or ""
        since = activity.get("since", t)
        return self._planned_place(self._routine(since) or self._whim(since) or {}, since)

    def _planned_place(self, block, t):
        """Where their plan or routine has them at t."""
        if places.is_patrol(block.get("doing")):
            # A patrol moves: along their beat, inside wherever the plan put
            # them — "the rooftops" or "Blüdhaven" is a beat to walk, and Tim
            # checking Blüdhaven's borders stays in Blüdhaven, off his own.
            spot = places.patrol_spot(self.contact, block.get("where") or "", t)
            if spot:
                return spot
        if block.get("where"):
            return self._home_is_home(block["where"])
        return getattr(self.contact, "home", "") or ""

    def _home_is_home(self, where):
        """
        'Home, Bristol' in Alfred's plan is the Manor: home is where they live.
        But 'Home, Blüdhaven' in Tim's is someone else's place over there — Dick's
        — so it stays where it says.
        """
        home = getattr(self.contact, "home", "") or ""
        found = re.match(r"(?i)\s*(?:at\s+)?home\b[\s,:-]*(.*)", where or "")
        if not home or not found:
            return where
        there, mine = places.resolve(found.group(1)) if found.group(1) else None, places.resolve(home)
        if there and mine and there["area"] != mine["area"]:
            return found.group(1)
        return home

    def trail(self, hours=2.0, step=300, t=None):
        """
        Where they've been over the last few hours, oldest first: one point per
        place, with when they got there — the path the map draws behind them.
        """
        t = t or time.time()
        points = []
        for at in range(int(t - hours * 3600), int(t) + 1, step):
            where, _ = self.whereabouts(at)
            spot = places.resolve(where)
            if spot and (not points or points[-1]["where"] != where):
                points.append({"at": at, "where": where, **spot})
        return points

    def public(self, t=None):
        """
        What the page is told. Someone who doesn't share their status — Jason,
        Selina — shows as unknown: Bruce wouldn't know, so the console doesn't
        either. Their status line is still theirs to set.
        """
        state = self.now(t)
        if not self.contact.shares_status:
            shown = getattr(self.contact, "hidden_as", "unknown") or "unknown"
            out = {"status": shown, "doing": "", "last_active": None, "line": self.line(t)}
            seen = self._state.get("seen")
            if seen and (t or time.time()) - seen.get("at", 0) < 48 * 3600:
                out["last_seen"] = seen          # the last he knows of where they were, and how
            return out
        shown = {"status": state["status"], "doing": state["doing"],
                 "last_active": state["last_active"] or None, "line": self.line(t)}
        if getattr(self.contact, "shares_location", True):
            shown["where"], company = self.whereabouts(t)
            shown["with"] = sharing(company)
            shown["spot"] = places.resolve(shown["where"])
            trip = self.trip(shown["where"], shown["spot"], t)
            if trip:
                shown["route"] = {"pts": trip["pts"], "start": trip["start"], "end": trip["end"], "from": trip.get("from", "")}
        return shown

    # --- getting about -----------------------------------------------------

    def trip(self, where, spot, t=None):
        """
        Their journey, while they're making one: when where they are changes,
        they go there — from wherever they were, mid-journey or not — along
        the roads, taking as long as the roads take (see wayne.engine.travel).
        None once they've arrived, or if the move happened while no one looked.
        """
        if not spot:
            return None
        t = t or time.time()
        with self._lock:
            looked, self._looked = self._looked, t
            trip = self._state.get("trip")
            here = (spot["x"], spot["y"])
            if trip and trip.get("to") == where:
                return trip if t < trip["end"] else None
            moving = trip and trip.get("pts") and t < trip["end"]
            origin = travel.position(trip, t) if trip and trip.get("pts") else None
            seen = looked is not None and t - looked < 20 * 60
            if origin is None or not seen or math.dist(origin, here) < 0.8:
                self._state["trip"] = {"to": where, "pts": [list(here)], "start": t, "end": t}
                self.save()
                return None
            pts, minutes = travel.route(origin, here, None if moving else trip.get("to"), spot["name"],
                                        patrol=places.is_patrol(self.now(t).get("doing")))
            trip = {"to": where, "from": "" if moving else trip.get("to", ""), "pts": pts, "start": t,
                    "end": t + minutes * 60}
            self._state["trip"] = trip
            self._state["trips"] = (self._state.get("trips") or [])[-7:] + [trip]
            self.save()
            return trip

    def trips(self, since):
        """The journeys they've made since `since`, oldest first — the roads behind them on the map."""
        return [tr for tr in (self._state.get("trips") or []) if tr.get("end", 0) >= since and len(tr.get("pts") or []) > 1]

    def seen_at(self, where, how, t=None):
        """
        Someone who keeps their whereabouts to themselves was seen, or said,
        where they are: the last he knows, and how he knows it.
        """
        spot = places.resolve(where)
        if not spot:
            return
        with self._lock:
            self._state["seen"] = {"where": spot["name"], "x": spot["x"], "y": spot["y"], "at": t or time.time(),
                                   "how": how}
            self.save()

    def note(self, t=None):
        """
        One line for the model: what they're doing and where — the place the
        map shows him, so "where are you?" gets the truth, not "at home" — and
        anyone with them. A call to someone on patrol is answered from a
        rooftop, one at 4am by someone he woke. Talking to him changes their
        status, not their day: mid-call, Alfred is still in the garden.
        """
        from ..contacts import directory
        t = t or time.time()
        state = self.now(t)
        block, _, firm = self._situation(t)
        doing = (block.get("doing") or "").strip()
        where, company = self.whereabouts(t)
        book = directory()
        names = [book.get(c).name for c in company if book.get(c)]
        # The line they set, which he can see — "didn't you read my status?"
        status = self.line(t)
        said = f' Your status line, which he can see, says "{status}".' if status else ""
        place = self._spoken_place(where)
        if doing:
            how_long = describe_until(block.get("until", 0), t) if firm else ""
            line = (f"Right now you're {doing}"
                    + (f", at {place}" if place and place.split(" (")[0].lower() not in doing.lower() else "")
                    + (f", {how_long}" if how_long else "") + ".")
        else:
            line = f"Right now you're at {place}." if place else ""
        if names:
            line += f" You're with {' and '.join(names)}."
        trip = self._state.get("trip")
        if trip and len(trip.get("pts") or []) > 1 and t < trip.get("end", 0):
            left = max(1, round((trip["end"] - t) / 60))
            line += f" You're on your way there now, about {left} minute{'s' if left != 1 else ''} out."
        if state["status"] == OFFLINE:
            line += " Your phone wasn't in your hand; he's reached you anyway."
        elif state["status"] == BUSY and doing:
            line += " You're in the middle of it."
        return (line + said).strip()

    def _spoken_place(self, where):
        """'Home, Blüdhaven' as they'd think of it — home; anywhere else, by its name."""
        if not where:
            return ""
        home = getattr(self.contact, "home", "") or ""
        if where == home or re.match(r"(?i)\s*home\b", where):
            rest = where.split(",", 1)[1].strip() if "," in where else (where if where != "Home" else "")
            return f"home ({rest})" if rest and rest.lower() != "home" else "home"
        return where

    # --- changes -----------------------------------------------------------

    def touch(self, t=None):
        """They've just been on their phone with him."""
        with self._lock:
            self._state["last_active"] = t or time.time()
            self.save()

    def set_activity(self, doing, status, minutes, t=None, where="", company=()):
        with self._lock:
            t = t or time.time()
            status = status if status in STATUSES else BUSY
            minutes = max(5, min(int(minutes or 60), 14 * 60))
            self._state["activity"] = {"doing": doing.strip()[:80], "status": status,
                                       "since": t, "until": t + minutes * 60,
                                       "where": (where or "").strip()[:60],
                                       "with": [c for c in company if c != self.contact.id]}
            self.save()
        if where and not getattr(self.contact, "shares_status", True):
            self.seen_at(where, "they told you", t)

    def clear_activity(self):
        with self._lock:
            if self._state["activity"]:
                self._state["activity"] = None
                self.save()

        # --- intentions ----------------------------------------------------------

    def intend(self, action, about, due, origin="promise"):
        """Something they mean to do: text him, or call him, at `due`."""
        with self._lock:
            intent = {"id": uuid.uuid4().hex[:10], "action": "call" if action == "call" else "text",
                      "about": about.strip()[:240], "due": due, "origin": origin}
            # One plan per purpose: a newer promise replaces an older one of the same kind.
            self._state["intents"] = [i for i in self._state["intents"]
                                      if i["origin"] != origin][-4:] + [intent]
            self.save()
            return intent

    def due(self, t=None):
        t = t or time.time()
        return [i for i in self._state["intents"] if i["due"] <= t]

    def postpone(self, intent_id, seconds, count=True):
        """Later. `count` is False when nothing was tried — they were asleep."""
        with self._lock:
            for intent in self._state["intents"]:
                if intent["id"] == intent_id:
                    intent["due"] = time.time() + seconds
                    if count:
                        intent["tries"] = intent.get("tries", 0) + 1
            self.save()

    def done(self, intent_id):
        with self._lock:
            self._state["intents"] = [i for i in self._state["intents"] if i["id"] != intent_id]
            self.save()

    def drop(self, origin):
        """Forget every intention of one kind — a callback, once they've talked."""
        with self._lock:
            kept = [i for i in self._state["intents"] if i["origin"] != origin]
            if len(kept) != len(self._state["intents"]):
                self._state["intents"] = kept
                self.save()

    def intents(self):
        return list(self._state["intents"])

    # --- bookkeeping the pulse needs ---------------------------------------

    def get(self, key, default=None):
        return self._state.get(key, default)

    def put(self, key, value):
        with self._lock:
            self._state[key] = value
            self.save()

    def clear(self):
        self._state = {"activity": None, "last_active": 0, "intents": []}
        if self.path.exists():
            self.path.unlink()


# How likely they are to answer, by what they're doing — unless the profile
# says otherwise. Someone free nearly always picks up; someone in a meeting
# usually doesn't; someone asleep mostly sleeps through it.
_ANSWERS = {ONLINE: 0.97, IDLE: 0.9, BUSY: 0.45, OFFLINE: 0.25}


_DO_NOT_DISTURB = re.compile(r"(?i)\b(do not disturb|dnd|don'?t call|no calls|not now|leave me alone|"
                             r"asleep|sleeping|off the grid|offline)\b|💤|🔕|😴|🛌")


def answers(contact, state, again=False, rng=random):
    """
    Whether they pick up: None if they do, else "declined" (they saw it and
    rejected it) or "no_answer" (it rang out). `again` is a second call hard
    on the heels of the first, which reads as urgent — people answer those.
    """
    odds = {**_ANSWERS, **(contact.initiative or {}).get("answers", {})}
    chance = odds.get(state["status"], 0.9)
    # They said so on their status — "do not disturb", 💤 — and mean it: the
    # first call mostly goes unanswered. A second one soon after gets through,
    # as phones let repeated calls through a do-not-disturb.
    if not again and _DO_NOT_DISTURB.search(of(contact).line() or ""):
        chance *= 0.35
    # Each call hard on the heels of the last cuts the chance of ignoring it —
    # by how much is theirs (`redial`): Alfred picks up the second time almost
    # surely; Randy, who doesn't answer, mostly still doesn't.
    tries = int(again)
    if tries:
        chance = 1 - (1 - chance) / ((contact.initiative or {}).get("redial", 3.0) ** tries)
    if rng.random() < chance:
        return None
    if state["status"] == OFFLINE:
        return "no_answer"          # the phone was nowhere near them
    if state["status"] == BUSY:
        return "declined" if rng.random() < 0.8 else "no_answer"
    return "declined" if rng.random() < 0.5 else "no_answer"


def read_delay(contact, state, rng=random):
    """
    Seconds before they look at a text, from what they're doing — or None
    while their phone is out of reach (they'll see it when that changes).
    """
    pace = contact.texting_pace or {}
    status = state["status"]
    # Glued to it, a text's read in seconds; in a dry spell it waits.
    pull = engagement(contact) ** 0.8
    if status == ONLINE:
        if rng.random() < 0.1:
            return rng.uniform(30, 180) / pull      # saw it, finished what they were doing first
        return rng.uniform(*pace.get("online_read", [2, 9])) / pull
    if status == IDLE:
        return rng.uniform(*pace.get("idle_read", [60, 600])) / pull
    if status == BUSY:
        if rng.random() < min(0.9, pace.get("glance", 0.3) * pull):
            return rng.uniform(20, 150)     # a glance between things
        return rng.uniform(*pace.get("busy_read", [900, 3600])) / pull
    return None
