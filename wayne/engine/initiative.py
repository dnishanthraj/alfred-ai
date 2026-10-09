"""
What contacts do of their own accord.

Two halves:

**Afterthought** — after a text exchange or a call, one short model pass reads
what was said and answers two questions: what are they doing now (and for how
long), and did anybody say they'd be in touch? "Go check the docks" makes Dick
busy for an hour and leaves him meaning to report back; "call me when you're
out" leaves Lucius meaning to ring. Read by the model rather than matched by
keyword, because the ways of saying either are endless and a missed promise is
worse than none.

**Impulse** — the rest of life: a thought about something they talked about,
something from their own day, something they heard from someone else, a
question he left hanging. Rare, and governed by a budget (see `Console.pulse`)
so the console feels lived-in rather than noisy.

Plus the finish on a text — the lowercase, the dropped full stop, three
messages instead of one — which is the part of texting style a model will
describe perfectly and then not do.
"""
import json
import random
import re
import time

import ollama

from .. import operator
from . import culture, grapevine, places, presence, world


def afterthought(session, exchanges, by="text"):
    """
    Read an exchange; set what they're doing and what they mean to do. Runs in
    the background — it costs a second of the model and nobody is waiting.
    """
    contact = session.contact
    exchanges = [m for m in exchanges[-8:] if m.get("content") and not m.get("marker")]

    def line(m):
        if m["role"] == "user" and m["content"].startswith("(Nothing from him"):
            return f"({contact.name} texted first)"
        return f"{operator.name() if m['role'] == 'user' else contact.name}: {m['content']}"
    # Context, then the exchange that's new. Asked about all of it, the pass
    # re-found a promise already kept and scheduled it again.
    earlier = [line(m) for m in exchanges[:-2]]
    latest = [line(m) for m in exchanges[-2:]]
    if not latest:
        return None
    lines = ((["Earlier, for context only:"] + earlier + ["", "The newest exchange:"])
             if earlier else []) + latest
    now = time.strftime("%A %H:%M")
    from . import cases
    case = cases.active(contact.id)
    instruction = (
        f"It's {now}. Here is the latest of a conversation {'by text' if by == 'text' else 'on a call'} "
        f"between {operator.full_name()} and {contact.full_name}:\n\n" + "\n".join(lines) + "\n\n"
        f"Answer two questions about {contact.name}, from the newest exchange only — never guess, "
        f"and never report something from the earlier lines that has already happened.\n"
        f"1. Is {contact.name} now going off to do something, or in the middle of something, that "
        f"keeps them away from their phone — an errand {operator.name()} gave them, a meeting, "
        f"patrol, sleep? Or did they say they're now free, done, or back?\n"
        f"2. Did {contact.name} agree or offer to get back in touch — call {operator.name()}, text "
        f"him, report back? If {operator.name()} asked and {contact.name} refused, brushed it off or "
        f"only said 'maybe' or 'if it matters', the answer is no.\n"
        f"3. Across the whole conversation: does {contact.name} come away worried about "
        f"{operator.name()} — something he said, how he sounded — and not reassured by the end? "
        f"Only if it's clear; ordinary concern that was settled is no.\n"
        + (f"4. {contact.name} is working a case ({case['kind'].lower()} at {case['place']}). From the newest "
           f"exchange: did they say it's dealt with — caught, stopped, over? If so, how, in a few words.\n"
           if case else "") + "\n"
        "Reply with JSON only, in this shape:\n"
        '{"doing": "a few words, e.g. checking the docks" or null, '
        '"where": "the place it puts them, as it would show on a map (e.g. Gotham Docks)" or null, '
        '"status": "busy" or "offline" or null, "minutes": how long it will take or null, '
        '"free": true if they said they are now free/back/done, '
        '"contact": {"by": "text" or "call", "in_minutes": number or null if it is "when done", '
        '"about": "the subject — e.g. what they found at the docks; never a time like when done"} '
        'or null, "worried": "what about him worries them, in a few words" or null'
        + (', "case_closed": "how it ended, in a few words" or null' if case else '') + '}')
    try:
        reply = ollama.chat(model=contact.model, think=False, format="json",
                            options={**contact.options, "temperature": 0, "num_predict": 160},
                            messages=[{"role": "user", "content": instruction}])["message"]["content"]
        found = json.loads(reply)
    except Exception:
        return None
    apply(session, found)
    return found


def apply(session, found):
    """Record what the afterthought found."""
    if not isinstance(found, dict):
        return
    state = presence.of(session.contact)
    if found.get("free"):
        state.clear_activity()     # done with the last thing — maybe on to the next
    doing = found.get("doing")
    if isinstance(doing, str) and doing.strip() and doing.strip().lower() not in ("null", "none"):
        status = found.get("status") if found.get("status") in (presence.BUSY, presence.OFFLINE) \
            else presence.BUSY
        minutes = _number(found.get("minutes"), 60)
        where = found.get("where") if isinstance(found.get("where"), str) else ""
        state.set_activity(doing, status, minutes,
                           where="" if where.strip().lower() in ("null", "none") else where)
    reach = found.get("contact")
    if isinstance(reach, dict) and isinstance(reach.get("about"), str) and reach["about"].strip():
        minutes = _number(reach.get("in_minutes"), None)
        activity = state.get("activity")
        if activity and activity.get("until", 0) <= time.time():
            activity = None         # long over
        if minutes is None:
            # "When it's done": when the thing they're doing is over.
            minutes = ((activity["until"] - time.time()) / 60 + random.uniform(2, 12)) if activity \
                else random.uniform(10, 40)
        due = time.time() + max(1.0, minutes) * 60 * random.uniform(0.85, 1.2)
        state.intend(reach.get("by", "text"), reach["about"], due, origin="promise")
    closed = found.get("case_closed")
    if isinstance(closed, str) and closed.strip() and closed.strip().lower() not in ("null", "none", "no"):
        from . import cases
        case = cases.active(session.contact.id)
        if case:
            cases.close(case["id"], closed.strip())
            state.clear_activity()
    worry = found.get("worried")
    if isinstance(worry, str) and worry.strip() and worry.strip().lower() not in ("null", "none", "no"):
        # It stayed with them. Whether they say so later is theirs — Dick will,
        # Jason might, a terse "you good?" an hour on; Randy probably won't.
        leaning = session.contact.initiative or {}
        odds = leaning.get("checks_in", 0.15 + leaning.get("per_day", 0.4) * 0.35)
        if random.random() < odds and not any(i.get("origin") in ("promise", "worry") for i in state.intents()):
            due = time.time() + random.uniform(25, 180) * 60
            state.intend("text", worry.strip()[:120], due, origin="worry")


def _number(value, default):
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return default


# --- status lines ---------------------------------------------------------------

def status_line(contact, state):
    """
    The status line they'd set on their phone right now, in their own voice —
    a few words or one emoji. Written by them because a status is something
    a person writes: Dick's is not Lucius's.
    """
    if not contact.shares_status:
        situation = ("You never say where you are or what you're doing — the line gives "
                     "nothing away, it's just you.")
    elif state.get("doing"):
        situation = f"Right now you're {state['doing']}."
    else:
        situation = "Nothing in particular going on — whatever's on your mind, or nothing at all."
        lately = culture.pick(contact)
        if lately:
            # Free time is when a status is about the things you're into.
            situation += f" (Something you've seen lately, if it's on your mind: {lately}.)"
    now = time.strftime("%A %-I%p", time.localtime()).replace("AM", "am").replace("PM", "pm")
    instruction = (
        f"It's {now}. Write the status line you'd set on your phone right now, the one people see "
        f"under your name. {situation} The way you text ({contact.texting}). A few words at most, "
        "or a single emoji — whatever you'd actually put. Reply with only the line itself.")
    try:
        reply = ollama.chat(model=contact.model, think=False,
                            options={**contact.options, "temperature": 0.9, "num_predict": 24},
                            messages=[{"role": "system", "content": contact.system},
                                      {"role": "user", "content": instruction}])["message"]["content"]
    except Exception:
        return ""
    line = reply.strip().splitlines()[0].strip().strip('"').strip() if reply.strip() else ""
    return line if 0 < len(line) <= 60 else ""


# --- the day ahead ----------------------------------------------------------------

_STATUSES = ("online", "idle", "busy", "offline")


def day_plan(contact, when=None, others="", people=()):
    """
    Their day, sketched by them: sleep, work, whether they're going out tonight,
    and whatever they've got on — in their own life, decided by the model, not a
    table. Loose blocks of time with a status each; gaps are free time. Returns
    [] if it can't be had. Written once a day, only while the model is already
    loaded (see Console._write_day_plans).
    """
    when = when or time.time()
    day = time.strftime("%A %-d %B", time.localtime(when))
    instruction = (
        f"It's {day}. Sketch your day today and tonight as loose blocks of time, the way your "
        "life actually runs — sleep, work, patrol if you'd go out tonight, and two or three "
        "things that are yours today: errands, people, plans, a whim. Times are approximate and "
        "needn't fill the day. Each block's status: online (phone in hand, on comms), idle "
        "(around, phone down), busy (occupied — a glance at most), offline (asleep or "
        "unreachable). Where: the actual place, as it would show on a map to someone else "
        "(\"Home, Blüdhaven\", not \"my flat\") — in Gotham, by its district or landmark where "
        f"you can ({', '.join(places.names())}). With: anyone from your circle you'd be with "
        "(first names), usually nobody."
        + (f" Things you've seen lately in what you follow — if any of it would shape your day "
           f"(a film to catch, a match to watch, a release to queue for), it can: "
           f"{' | '.join(culture.seen(contact)[:6])}." if culture.seen(contact) else "")
        + (f" What others in your circle have planned so far today — if one of them is meeting "
           f"you, keep it at their time and place, unless you'd really not:\n{others}\n"
           if others else " ")
        + "Return JSON only: "
        '{"plan": [{"from": "HH:MM", "to": "HH:MM", "doing": "under eight words, as you\'d say it", '
        '"status": "online|idle|busy|offline", "where": "place", "with": []}]}')
    try:
        reply = ollama.chat(model=contact.model, think=False, format="json",
                            options={**contact.options, "temperature": 0.9, "num_predict": 700},
                            messages=[{"role": "system", "content": contact.system},
                                      {"role": "user", "content": instruction}])["message"]["content"]
    except Exception:
        return []
    try:
        blocks = json.loads(reply).get("plan") or []
    except ValueError:
        # Cut off mid-plan: keep every block that did arrive whole.
        blocks = []
        for found in re.finditer(r"\{[^{}]*\}", reply):
            try:
                blocks.append(json.loads(found.group(0)))
            except ValueError:
                pass
    plan = []
    for b in blocks[:12]:
        try:
            start = _hours(b["from"])
            end = _hours(b["to"])
        except (KeyError, ValueError, TypeError):
            continue
        status = b.get("status") if b.get("status") in _STATUSES else "busy"
        doing = str(b.get("doing") or "").strip()[:70]
        where = _on_the_map(str(b.get("where") or ""))
        names = b.get("with") if isinstance(b.get("with"), list) else []
        company = [p.id for p in people if p.id != contact.id
                   and any(str(n).strip().lower() in (p.name.lower(), p.id) for n in names)]
        if doing and start != end:
            plan.append({"from": start, "to": end, "doing": doing, "status": status, "drift": 0.4,
                         "where": where, "with": company})
    return plan


def _on_the_map(place):
    """'My study, Bristol' → 'Home, Bristol': where they are, as he'd see it."""
    place = place.strip()[:60]
    place = re.sub(r"(?i)^my (apartment|flat|place|house|home|room|bedroom|study|kitchen|loft|couch|bed)\b",
                   "Home", place)
    place = re.sub(r"(?i)^my\s+", "", place)
    return place[:1].upper() + place[1:]


def _hours(clock):
    """'21:30' -> 21.5"""
    hours, _, minutes = str(clock).strip().partition(":")
    value = int(hours) + int(minutes or 0) / 60
    if not 0 <= value <= 24:
        raise ValueError(clock)
    return value


# --- tics ------------------------------------------------------------------------

def untic(contact, text, recent):
    """
    A habit is a habit because it's occasional. Shown 'lol' in the examples, a
    model put it in every message, and Dick read like a tic with a face. A tic
    used in any of their last few texts is taken out of this one.
    """
    for tic in (contact.texting_style or {}).get("tics", []):
        pattern = re.compile(rf"(?<!\w){re.escape(tic)}(?!\w)", re.I)
        if not pattern.search(text) or not any(pattern.search(r) for r in recent):
            continue
        trimmed = pattern.sub("", text)
        trimmed = re.sub(r"\s+([,.!?])", r"\1", trimmed)
        trimmed = re.sub(r"^[\s,.;:!]+|[\s,;:]+$", "", trimmed)
        trimmed = re.sub(r"\s{2,}", " ", trimmed).strip()
        if len(trimmed) >= 2:
            text = trimmed
    return text


# --- impulses ----------------------------------------------------------------

def impulse(session):
    """
    Something to text him about, unprompted — or None. Picked here, not left
    to the model: told "text him something", every contact asks how he is.
    """
    contact = session.contact
    options = []
    heard = grapevine.heard(contact.id)
    if heard:
        latest = heard[0]          # newest first
        options += [("hearsay", f"what {latest['from']} told you: {latest['text']}")] * 2
    if len(session.history.recent_user(turns=12)) >= 2:
        options += [("thread", "something from your last conversation with him that's stayed with "
                               "you — a follow-up, a thought you had after, a question")] * 2
    if contact.own_life:
        options.append(("own", f"something from your own day: {random.choice(contact.own_life)}"))
    pastimes = (contact.interests or {}).get("pastimes") or []
    if pastimes:
        options.append(("hobby", f"something to do with what you do for fun: {random.choice(pastimes)}"))
    lately = culture.pick(contact)
    if lately:
        # What they've seen in the things they follow: a take, an argument
        # starter, an "are you watching this" — theirs to make of it.
        options.append(("culture", f"something you just saw about what you follow — {lately} — "
                                   "with your own take on it, the way you'd text a friend about it"))
    items = [item.strip() for line in world.snapshot(contact)
             for item in line.split(": ", 1)[-1].split(" | ") if item.strip()]
    if items:
        options.append(("world", f"something you just saw: {random.choice(items)}"))
    if not options:
        return None
    return random.choice(options)[1]


# --- the finish on a text --------------------------------------------------------

# How long this reply runs, drawn per text from the person's own spread — so
# Jason is mostly a word or two and now and then a paragraph, and Alfred is
# composed every time. Said to the model as a tendency, not a rule.
_LENGTHS = {
    "word": "a word or two — dry, minimal",
    "line": "one short line",
    "few": "two or three short lines",
    "long": "longer than usual — you've got something to say",
}


_SERIOUS = re.compile(r"(?i)\b(lost|died|dead|hurt|hospital|sorry|can'?t do this|scared|alone|"
                      r"rough|bad night|worst|funeral|help)\b")


def length_hint(contact, prompt="", rng=random):
    """
    How long this text runs, from their own spread — moved by what he sent:
    "rough night. lost someone" drew a bare "I'm sorry." from three of them
    when the dice said a word or two. Something serious gets at least a line.
    """
    weights = (contact.texting_style or {}).get("length")
    if not weights:
        return ""
    kinds = [k for k in _LENGTHS if weights.get(k)]
    pick = rng.choices(kinds, weights=[weights[k] for k in kinds])[0]
    order = list(_LENGTHS)
    if _SERIOUS.search(prompt or "") and order.index(pick) < order.index("few"):
        pick = "few" if "few" in weights else pick
    elif "?" in (prompt or "") and pick == "word":
        pick = "line"
    return f"Length this time: {_LENGTHS[pick]}, unless it truly needs otherwise."


# Keys that sit next to each other, for the slips a thumb actually makes.
_NEAR = dict(zip("qwertyuiopasdfghjklzxcvbnm", [
    "wa", "qes", "wrd", "etf", "ryg", "tuh", "yij", "uok", "ipl", "ol", "qsz", "awdx", "sefc",
    "drgv", "fthb", "gyjn", "hukm", "jilm", "kop", "asx", "zsdc", "xdfv", "cfgb", "vghn",
    "bhjm", "njk"], strict=True))


def typo(word, rng=random):
    """One realistic slip: a neighbouring key, a dropped, doubled or swapped letter."""
    if len(word) < 4:
        return word
    i = rng.randrange(1, len(word) - 1)
    kind = rng.choice(("near", "drop", "double", "swap"))
    if kind == "near" and word[i].lower() in _NEAR:
        return word[:i] + rng.choice(_NEAR[word[i].lower()]) + word[i + 1:]
    if kind == "drop":
        return word[:i] + word[i + 1:]
    if kind == "double":
        return word[:i] + word[i] + word[i:]
    return word[:i - 1] + word[i] + word[i - 1] + word[i + 1:]


# Autocorrect's favourite betrayals: a real word, the wrong one.
_AUTOCORRECT = {"docks": "ducks", "cave": "cafe", "fucking": "ducking", "fuck": "duck",
                "shit": "shot", "were": "we're", "well": "we'll", "hell": "he'll", "sure": "sire",
                "roof": "riff", "patrol": "petrol", "tonight": "tonights", "omw": "own"}


def slip(contact, text, rng=random):
    """
    Maybe a slip in a message — a fat-fingered typo, or autocorrect swapping in
    the wrong real word — and maybe the correction after it, the way they'd
    write one: '*docks' (Tim), 'docks*' (Dick), or not at all (Jason lets it
    stand). Returns the message as sent, and the correction or None.
    """
    style = contact.texting_style or {}
    if rng.random() >= style.get("typos", 0):
        return text, None
    swappable = [m for m in re.finditer(r"\b[a-z']+\b", text) if m.group(0) in _AUTOCORRECT]
    if swappable and rng.random() < style.get("autocorrect", 0):
        target = rng.choice(swappable)
        wrong, autocorrected = _AUTOCORRECT[target.group(0)], True
    else:
        words = [m for m in re.finditer(r"\b[a-z]{4,}\b", text)]
        if not words:
            return text, None
        target = rng.choice(words)
        wrong, autocorrected = typo(target.group(0), rng), False
    if wrong == target.group(0):
        return text, None
    sent = text[:target.start()] + wrong + text[target.end():]
    if rng.random() >= style.get("corrects", 0):
        return sent, None
    word = target.group(0)
    form = style.get("correct_style", "prefix")
    if form == "mixed":
        form = rng.choice(("prefix", "suffix"))
    correction = f"*{word}" if form == "prefix" else f"{word}*"
    if autocorrected and rng.random() < 0.35:
        correction += rng.choice((" autocorrect", " stupid autocorrect", " ugh autocorrect"))
    return sent, correction


# Sentence ends — but not after a title ("Mr. Freeze" is one name, not two
# texts) or a lone initial.
_SENTENCES = re.compile(r"(?<!\bMr\.)(?<!\bMs\.)(?<!\bDr\.)(?<!\bSt\.)(?<!\bMrs\.)(?<!\b[A-Z]\.)"
                        r"(?<=[.!?…])\s+")
_URL = re.compile(r"\S+://\S+|\bwww\.\S+")
_EMOJI = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F\u200D]+")


def styled(contact, text, rng=random):
    """Their texting habits, applied: emoji, case, the last full stop."""
    style = contact.texting_style or {}
    text = text.strip()
    if _EMOJI.search(text) and rng.random() >= style.get("emoji", 1.0):
        # Shown one thumbs-up in a sample, a model gives every message one.
        text = re.sub(r"\s{2,}", " ", _EMOJI.sub("", text)).strip() or text
    if rng.random() < style.get("lower", 0):
        # Lowercase texters write 'i' too; acronyms they'd keep (GCPD) survive.
        # Links keep their case — they're case-sensitive.
        links = _URL.findall(text)
        text = re.sub(r"\b(?![A-Z]{2,}\b)[A-Za-z][\w'’]*",
                      lambda m: m.group(0).lower(), text)
        for link in links:
            text = re.sub(re.escape(link.lower()), lambda _m, link=link: link, text, count=1)
    if style.get("period") is False:
        # Only the last one: a full stop mid-message still separates two thoughts.
        text = re.sub(r"(?<![.])\.$", "", text)
    return text.strip()


def bubbles(contact, text, rng=random):
    """
    One reply as the separate messages they'd actually send. Dick sends three
    in a row; Alfred sends one, composed.
    """
    burst = (contact.texting_style or {}).get("burst", 0)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not burst:
        return _with_slips(contact, [styled(contact, " ".join(lines), rng)], rng) if lines else []
    if len(lines) > 1:
        # The model put them on separate lines: separate messages, as written.
        # Never more than five — but what's past the fifth joins it, not the bin.
        lines = lines[:4] + [" ".join(lines[4:])] if len(lines) > 5 else lines
        return _with_slips(contact, [styled(contact, line, rng) for line in lines], rng)
    parts = [p.strip() for p in _SENTENCES.split(text) if p and p.strip()]
    if len(parts) < 2 or rng.random() >= burst:
        return _with_slips(contact, [styled(contact, text, rng)], rng)
    out, current = [], parts[0]
    for i, part in enumerate(parts[1:]):
        # The first break always, once it's decided they'd send several.
        if i == 0 or rng.random() < 0.6:
            out.append(current)
            current = part
        else:
            current += " " + part
    out.append(current)
    out = out[:3] + [" ".join(out[3:])] if len(out) > 4 else out
    return _with_slips(contact, [styled(contact, p, rng) for p in out if p], rng)


def _with_slips(contact, messages, rng):
    """At most one typo a reply — and its correction, sent straight after."""
    for i, message in enumerate(messages):
        sent, correction = slip(contact, message, rng)
        if sent != message:
            return messages[:i] + [sent] + ([correction] if correction else []) + messages[i + 1:]
    return messages
