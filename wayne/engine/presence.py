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
import random
import threading
import time
import uuid

from .. import paths
from ..memory.store import atomic_write, read_text

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
            if (self._state.get("plans") or {}).get(time.strftime("%Y-%m-%d", local)):
                break       # they planned today themselves; the routine is only a fallback
            for began in (t, t - 86400):        # a night may have begun yesterday
                day = time.localtime(began)
                key = time.strftime("%Y-%m-%d", day)
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

    def public(self, t=None):
        """
        What the page is told. Someone who doesn't share their status — Jason,
        Selina — shows as unknown: Bruce wouldn't know, so the console doesn't
        either. Their status line is still theirs to set.
        """
        state = self.now(t)
        if not self.contact.shares_status:
            shown = getattr(self.contact, "hidden_as", "unknown") or "unknown"
            return {"status": shown, "doing": "", "last_active": None, "line": self.line(t)}
        return {"status": state["status"], "doing": state["doing"],
                "last_active": state["last_active"] or None, "line": self.line(t)}

    def note(self, t=None):
        """
        One line for the model, when what they're doing matters — so a call
        to someone on patrol is answered from a rooftop, and one at 4am by
        someone he woke. Free time and being mid-conversation need no note.
        """
        state = self.now(t)
        if state["source"] not in ("conversation", "routine") or not state["doing"]:
            return ""
        how_long = describe_until(state["until"], t)
        line = f"Right now you're {state['doing']}" + (f", {how_long}" if how_long else "") + "."
        if state["status"] == OFFLINE:
            line += " Your phone wasn't in your hand; he's reached you anyway."
        elif state["status"] == BUSY:
            line += " You're in the middle of it."
        return line

    # --- changes -----------------------------------------------------------

    def touch(self, t=None):
        """They've just been on their phone with him."""
        with self._lock:
            self._state["last_active"] = t or time.time()
            self.save()

    def set_activity(self, doing, status, minutes, t=None):
        with self._lock:
            t = t or time.time()
            status = status if status in STATUSES else BUSY
            minutes = max(5, min(int(minutes or 60), 14 * 60))
            self._state["activity"] = {"doing": doing.strip()[:80], "status": status,
                                       "since": t, "until": t + minutes * 60}
            self.save()

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


def answers(contact, state, again=False, rng=random):
    """
    Whether they pick up: None if they do, else "declined" (they saw it and
    rejected it) or "no_answer" (it rang out). `again` is a second call hard
    on the heels of the first, which reads as urgent — people answer those.
    """
    odds = {**_ANSWERS, **(contact.initiative or {}).get("answers", {})}
    chance = odds.get(state["status"], 0.9)
    # Each call hard on the heels of the last cuts the chance of ignoring it to
    # a third: twice reads as urgent, three times is hard to sleep through.
    tries = int(again)
    if tries:
        chance = 1 - (1 - chance) / (3 ** tries)
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
    if status == ONLINE:
        return rng.uniform(*pace.get("online_read", [2, 9]))
    if status == IDLE:
        return rng.uniform(*pace.get("idle_read", [60, 600]))
    if status == BUSY:
        if rng.random() < pace.get("glance", 0.3):
            return rng.uniform(20, 150)     # a glance between things
        return rng.uniform(*pace.get("busy_read", [900, 3600]))
    return None
