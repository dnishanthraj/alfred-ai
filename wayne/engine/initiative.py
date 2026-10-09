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
from . import grapevine, presence, world


def afterthought(session, exchanges, by="text"):
    """
    Read an exchange; set what they're doing and what they mean to do. Runs in
    the background — it costs a second of the model and nobody is waiting.
    """
    contact = session.contact
    lines = [f"{operator.name() if m['role'] == 'user' else contact.name}: {m['content']}"
             for m in exchanges[-8:] if m.get("content")]
    if not lines:
        return None
    now = time.strftime("%A %H:%M")
    instruction = (
        f"It's {now}. Here is the latest of a conversation {'by text' if by == 'text' else 'on a call'} "
        f"between {operator.full_name()} and {contact.full_name}:\n\n" + "\n".join(lines) + "\n\n"
        f"Answer two questions about {contact.name}, from what was actually said — never guess.\n"
        f"1. Is {contact.name} now going off to do something, or in the middle of something, that "
        f"keeps them away from their phone — an errand {operator.name()} gave them, a meeting, "
        f"patrol, sleep? Or did they say they're now free, done, or back?\n"
        f"2. Did {contact.name} agree or offer to get back in touch — call {operator.name()}, text "
        f"him, report back? If {operator.name()} asked and {contact.name} refused, brushed it off or "
        f"only said 'maybe' or 'if it matters', the answer is no.\n\n"
        "Reply with JSON only, in this shape:\n"
        '{"doing": "a few words, e.g. checking the docks" or null, '
        '"status": "busy" or "offline" or null, "minutes": how long it will take or null, '
        '"free": true if they said they are now free/back/done, '
        '"contact": {"by": "text" or "call", "in_minutes": number or null if it is "when done", '
        '"about": "the subject — e.g. what they found at the docks; never a time like when done"} '
        'or null}')
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
        state.set_activity(doing, status, minutes)
    reach = found.get("contact")
    if isinstance(reach, dict) and isinstance(reach.get("about"), str) and reach["about"].strip():
        minutes = _number(reach.get("in_minutes"), None)
        activity = state.get("activity")
        if minutes is None:
            # "When it's done": when the thing they're doing is over.
            minutes = ((activity["until"] - time.time()) / 60 + random.uniform(2, 12)) if activity \
                else random.uniform(10, 40)
        due = time.time() + max(1.0, minutes) * 60 * random.uniform(0.85, 1.2)
        state.intend(reach.get("by", "text"), reach["about"], due, origin="promise")


def _number(value, default):
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return default


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
        latest = heard[-1]
        options += [("hearsay", f"what {latest['from']} told you: {latest['text']}")] * 2
    if len(session.history.recent_user(turns=12)) >= 2:
        options += [("thread", "something from your last conversation with him that's stayed with "
                               "you — a follow-up, a thought you had after, a question")] * 2
    if contact.own_life:
        options.append(("own", f"something from your own day: {random.choice(contact.own_life)}"))
    items = [item.strip() for line in world.snapshot()
             for item in line.split(": ", 1)[-1].split(" | ") if item.strip()]
    if items:
        options.append(("world", f"something you just saw: {random.choice(items)}"))
    if not options:
        return None
    return random.choice(options)[1]


# --- the finish on a text --------------------------------------------------------

_SENTENCES = re.compile(r"(?<=[.!?…])\s+")
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
        text = re.sub(r"\b(?![A-Z]{2,}\b)[A-Za-z][\w'’]*",
                      lambda m: m.group(0).lower(), text)
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
        return [styled(contact, " ".join(lines), rng)] if lines else []
    if len(lines) > 1:
        # The model put them on separate lines: separate messages, as written.
        return [styled(contact, line, rng) for line in lines[:5]]
    parts = [p.strip() for p in _SENTENCES.split(text) if p and p.strip()]
    if len(parts) < 2 or rng.random() >= burst:
        return [styled(contact, text, rng)]
    out, current = [], parts[0]
    for i, part in enumerate(parts[1:]):
        # The first break always, once it's decided they'd send several.
        if i == 0 or rng.random() < 0.6:
            out.append(current)
            current = part
        else:
            current += " " + part
    out.append(current)
    return [styled(contact, p, rng) for p in out[:4] if p]
