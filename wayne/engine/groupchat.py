"""
How a group chat behaves: who knows what, who answers, and when it goes quiet.

The log is the record (wayne.memory.groups); this module decides what to do
with it. Three things matter:

  * **What each member knows.** A short block of each group they're in, as far
    as they've read it, rides along with everything they say anywhere — on a
    call, in a text, in the group itself — so the chat is one shared memory,
    never nine private copies that drift.
  * **Who answers.** Anyone named answers. Otherwise each member who reads a
    new message may join in, at odds set by how chatty they are and how much
    life the thread still has.
  * **Going quiet.** Every message a member sends without Bruce in it uses up
    some of the thread's energy, and his next message restores it. Two models
    left to answer each other will happily talk until the power fails; this is
    what makes a group chat drift off the way real ones do.
"""
import random
import re
import time

from .. import operator
from ..memory import groups as store

# Energy a thread starts with when Bruce posts, and what each autonomous
# message keeps of it. Three or four rounds between them, then quiet.
FULL = 1.0
DECAY = 0.55
# Below this, nobody starts anything new in the thread.
QUIET = 0.15
# How recent a group's messages must be to come up in what a member knows.
RECENT = 3 * 86400


def names(directory):
    def name(sender):
        if sender == "me":
            return operator.name()
        contact = directory.get(sender)
        return contact.name if contact else sender
    return name


# A pause in a thread long enough to show: "(2 hours later)" — so a reply
# that took all afternoon reads as one, and "where were you?" can follow.
GAP = 20 * 60


def later(seconds):
    minutes = seconds / 60
    if minutes < 60:
        return f"{int(minutes)} minutes"
    hours = minutes / 60
    if hours < 24:
        return "an hour" if hours < 1.7 else f"{int(hours)} hours"
    return "a day" if hours < 48 else f"{int(hours / 24)} days"


def transcript(messages, name):
    lines, prev = [], None
    for m in messages:
        if prev is not None and m["at"] - prev > GAP:
            lines.append(f"({later(m['at'] - prev)} later)")
        prev = m["at"]
        if m.get("kind") == "system":
            lines.append(f"({m.get('said') or m['text']})")
            continue
        line = f"{name(m['from'])}: {m['text']}"
        reactions = m.get("reactions") or {}
        if reactions:
            line += "  [" + ", ".join(f"{e} from {name(who)}" for who, e in reactions.items()) + "]"
        lines.append(line)
    return "\n".join(lines)


def relations(contact, group, directory):
    """
    What each other person in the chat is to this contact, in their own
    character's words — the sentences of their profile about that person.
    In the middle of a long background those got lost: Dick, in a chat with
    Randy, asked him "what did you do?" as if he didn't know the whole story.
    """
    import re as _re
    text = " ".join(contact.system) if isinstance(contact.system, (list, tuple)) else str(contact.system)
    sentences = _re.split(r"(?<=[.!?])\s+", text)
    lines = []
    for member in group.members:
        other = directory.get(member)
        if other is None or other.id == contact.id:
            continue
        about = [s for s in sentences if _re.match(rf"^\W*{_re.escape(other.name)}\b", s)
                 or f" {other.name}:" in f" {s}"]
        if about:
            said = " ".join(about[:2])
            said = _re.sub(rf"^\W*{_re.escape(other.name)}\s*:\s*", "", said)
            lines.append(f"{other.name}: {said}"[:400])
    return lines


def secrets_note(group, contact_id):
    """
    Who in this group doesn't know about the masks — so nobody gives anyone
    away. Selina in a chat with the family is told nothing, and the family are
    told she's there.
    """
    secrets = operator.profile().get("secrets") or {}
    known = set(secrets.get("known_by") or [])
    if not known or contact_id not in known:
        return ""
    outsiders = [m for m in group.members if m not in known]
    if not outsiders:
        return ""
    who = ", ".join(outsiders)
    return (f"Not everyone in this chat knows about the masks ({who} doesn't). In front of {who}: "
            "no patrols, cases, villains, suits, gear, the cave, or who anyone really is — only what "
            "a family and its friends would say. Real names only.")


def block(contact_id, directory, now=None):
    """
    What this contact knows of their group chats: each one they're in, as far as
    they've read it. Background, like hearsay — theirs to bring up or not.
    """
    now = now or time.time()
    name = names(directory)
    parts = []
    for group in store.groups_with(contact_id):
        seen = [m for m in group.seen_by(contact_id, limit=8) if now - m["at"] < RECENT]
        if not seen:
            continue
        members = ", ".join([operator.name()] + [name(m) for m in group.members if m != contact_id])
        parts.append(f"Group chat \"{group.name}\" (you, {members}) — the latest you've read:\n"
                     + transcript(seen, name))
    if not parts:
        return ""
    return ("Your group chats — background you know, not something to recite; raise it only if "
            "it's natural:\n" + "\n\n".join(parts))


def addressed(text, contact):
    """Named in it — 'Dick?', '@tim', 'Barbara, ...', '@everyone' — so they answer."""
    if pings_everyone(text):
        return True
    for name in {contact.name, contact.full_name, contact.id}:
        if re.search(rf"(?<!\w)@?{re.escape(name)}\b", text or "", re.I):
            return True
    return False


def tagged(text, contact):
    """'@Tim' — a tag, which pings, rather than just his name in passing."""
    return pings_everyone(text) or any(re.search(rf"(?<!\w)@{re.escape(n)}\b", text or "", re.I)
               for n in {contact.name, contact.full_name.split()[0], contact.id})


def handles(group, directory, sender=None):
    """Who can be tagged in this chat, and as what: {id: "Tim"}, him as "me"."""
    found = {"me": operator.name()}
    for cid in group.members:
        contact = directory.get(cid)
        if contact is not None and cid != sender:
            found[cid] = contact.name
    return found


# "@everyone" pings the whole chat; these are what people type for it.
EVERYONE = {"everyone", "all", "everybody", "here", "channel", "team", "guys"}


def fix_tags(text, group, directory, sender):
    """
    A tag is someone in the chat, written the way it shows — "@Barbara",
    "@Bruce", "@everyone". The model half-writes them ("@b", "@Barb",
    "@timmy") or tags someone who isn't here: a half-tag that can only mean one
    of them becomes theirs, a stray letter or two that could mean anyone goes,
    and anyone not in the chat is just a name.
    """
    names = handles(group, directory, sender)
    known = {}
    for cid, name in names.items():
        contact = directory.get(cid)
        known[cid] = {name.lower(), cid} | ({contact.full_name.split()[0].lower()} if contact else set())
    known["me"] |= {operator.full_name().split()[0].lower()}

    def who(word):
        word = word.lower()
        exact = [cid for cid, said in known.items() if word in said]
        if exact:
            return exact[0]
        if len(word) < 3:
            return None
        close = {cid for cid, said in known.items()
                 if any(s.startswith(word) or (len(s) >= 3 and word.startswith(s)) for s in said)}
        return close.pop() if len(close) == 1 else None

    def fix(match):
        word, tail = match.group(1), match.group(2)
        if word.lower() in EVERYONE:
            return "@everyone" + tail
        cid = who(word)
        if cid:
            return "@" + names[cid] + tail
        return "" if len(word) < 3 else word + tail
    text = re.sub(r"(?<![\w@])@([A-Za-z][A-Za-z-]*)([,:]?)", fix, text)
    return re.sub(r"(?m)^[ \t]+|[ \t]+$", "", re.sub(r"[ \t]{2,}", " ", text)).strip()


def pings_everyone(text):
    return bool(re.search(r"(?<![\w@])@everyone\b", text or "", re.I))


# Asking them to do something somewhere: "say something in it", "tell them", "post".
_ASKS = re.compile(r"(?i)\b(say|tell|post|ask|text|write|put|send|message|drop|reply|answer|chime|"
                   r"respond|announce|share|let (them|everyone|the others|the group) know)\b")
# A chat, said as a chat: "the group", "the GC", "the chat with you and Randy", "in it".
_A_CHAT = re.compile(r"(?i)\b(group ?chat|the group|gc|the chat|chat with|group with|in (it|there)|that chat)\b")
# Talking to a room, not to one person.
_TO_A_ROOM = re.compile(r"(?i)\b(you guys|guys|y'?all|you all|you two|you lot|everyone|everybody|folks|team)\b")


def meant_group(text, contact, directory):
    """
    The group chat a DM from him is about, when he's asking them to do
    something in one: by its name, by who's in it ("the chat with you and
    Randy"), or — if they're in just one with him — by "the group". None
    when he isn't asking for anything in a chat.
    """
    groups = store.groups_with(contact.id)
    if not groups or not _ASKS.search(text or ""):
        return None
    lowered = text.lower()
    named = [g for g in groups if g.name and g.name.lower().strip(" .!") in lowered]
    if len(named) == 1:
        return named[0]
    if not _A_CHAT.search(text):
        return None
    others = {cid for g in groups for cid in g.members if cid != contact.id and directory.get(cid)
              and re.search(rf"(?<!\w){re.escape(directory.get(cid).name)}\b", text, re.I)}
    fits = [g for g in groups if others and others <= set(g.members)]
    if fits:
        return min(fits, key=lambda g: len(g.members))      # the smallest chat that has them all
    return groups[0] if len(groups) == 1 else None


def speaks_to_group(text, group, directory, sender):
    """Written to the room — "you guys", or someone else in it by name — not to him alone."""
    if _TO_A_ROOM.search(text or ""):
        return True
    return any(re.search(rf"(?<!\w)@?{re.escape(directory.get(cid).name)}\b", text or "", re.I)
               for cid in group.members if cid != sender and directory.get(cid))


def needless_tags(text, group, directory, sender):
    """
    An @ on someone already in the back-and-forth is just their name: people
    tag to pull someone in, not on every reply to them.
    """
    recent = {m["from"] for m in group.messages()[-4:] if m.get("kind") != "system"} - {sender}
    for cid in recent:
        contact = directory.get(cid)
        if cid == "me":
            said = {operator.name()}
        elif contact is None:
            continue
        else:
            said = {contact.name, contact.full_name.split()[0], contact.id}
        for n in said:
            text = re.sub(rf"(?im)^@{re.escape(n)}\b[,:]?\s*", "", text)
            text = re.sub(rf"(?i)(?<!\w)@({re.escape(n)})\b", r"\1", text)
    return text


def liveliness(group, directory):
    """
    How many of them are around right now, as a multiplier: a chat comes alive
    when several are online at once — patrol hours, mostly — and goes quiet when
    they're asleep or busy. The waves come from their days, not a timer.
    """
    from . import presence
    members = [directory.get(m) for m in group.members if directory.get(m)]
    if not members:
        return 1.0
    online = sum(presence.of(c).now()["status"] == presence.ONLINE for c in members)
    return 0.6 + 0.9 * online / len(members)


def reply_odds(contact, group, unread, energy, lively=1.0):
    """
    The chance they say something after reading these messages. Named: always.
    A question to the room: likely, for the chatty. Otherwise their own
    chattiness, scaled by how much life the thread has left.
    """
    said = [m for m in unread if m.get("kind") != "system"]
    if any(addressed(m["text"], contact) for m in said if m["from"] == "me"):
        return 1.0
    if any(addressed(m["text"], contact) for m in said):
        # One of them asking: likely, but not owed. Owed, two of them tagging
        # each other went on for twenty messages, each "it's not just X, it's Y".
        return max(0.0, min(0.95, (0.3 + 0.65 * energy) * lively))
    if all(m.get("kind") == "system" for m in unread):
        # Someone joined or was shown the door: some react, most don't.
        return max(0.0, min(0.7, 0.35 * energy * lively))
    chatty = min(1.0, 0.25 + (contact.initiative or {}).get("per_day", 0.5) * 0.4)
    from_bruce = any(m["from"] == "me" for m in unread)
    question = any(m["text"].rstrip().endswith("?") for m in unread)
    odds = chatty * (1.2 if question else 1.0) * (1.25 if from_bruce else 1.0)
    # Someone who leaves texts on read leaves group chats on read too: Randy,
    # ghosting the family, reads it and says nothing far more often than not.
    odds *= 1 - (contact.texting_pace or {}).get("on_read", 0)
    return max(0.0, min(0.95, odds * energy * lively))


_ASKS_TO_ADD = r"(?i)\b(add|bring|get|loop|invite|pull)\b[^.?!]*\b{name}\b"


def may_add(group, adder, newcomer, now=None):
    """
    Whether a member's [add] stands. Yes if Bruce asked for that person in the
    last few messages. Otherwise only if nobody he removed is being put back, and
    members haven't added anyone in the last half hour — Dick, given the power,
    added Tim, Cass and Jason in three minutes after being told to keep it small.
    """
    import re as _re
    now = now or time.time()
    recent = [m for m in group.messages()[-8:] if m["from"] == "me"]
    if any(_re.search(_ASKS_TO_ADD.format(name=_re.escape(newcomer.name)), m["text"]) for m in recent):
        return True
    meta = group.meta()
    if now - (meta.get("removed") or {}).get(newcomer.id, 0) < 86400:
        return False
    if now - meta.get("member_added_at", 0) < 1800:
        return False
    keep_small = any(_re.search(r"(?i)\b(just|only|between) (you|us)\b|\bkeep it (small|between)|"
                                r"\bno(body| one) else\b|\bremove\b", m["text"]) for m in recent)
    return not keep_small


_ASKS_TO_REMOVE = r"(?i)\b(remove|kick|boot|drop|take)\b[^.?!]*\b({name}|them all|everyone)\b"


def may_remove(group, remover, member, now=None):
    """
    Whether a member's [remove] stands: because Bruce asked — "Dick, take Tim
    out", "remove them all" — or to undo an add of their own a moment ago.
    Never on a whim, and never Bruce's own members out from under him.
    """
    import re as _re
    now = now or time.time()
    recent = [m for m in group.messages()[-8:] if m["from"] == "me"]
    if any(_re.search(_ASKS_TO_REMOVE.format(name=_re.escape(member.name)), m["text"]) for m in recent):
        return True
    added = [m for m in group.messages()[-12:] if m.get("kind") == "system"
             and m["text"] == f"{remover.name} added {member.name}" and now - m["at"] < 900]
    return bool(added)


def note_added(group, now=None):
    meta = group.meta()
    meta["member_added_at"] = now or time.time()
    group._save_meta(meta)


def energy(group):
    return float(group.meta().get("energy", FULL))


def spend(group, sender):
    """After a message: Bruce restores the thread; anyone else uses some of it up."""
    meta = group.meta()
    meta["energy"] = FULL if sender == "me" else max(0.0, float(meta.get("energy", FULL)) * DECAY)
    group._save_meta(meta)
    return meta["energy"]


def wants_to_start(contact, tick_seconds, rng=random):
    """
    Someone opening a fresh conversation in a quiet group, this tick — about a
    third as often as they'd text Bruce unprompted, spread over a waking day.
    """
    per_day = (contact.initiative or {}).get("per_day", 0.4) * 0.3
    return rng.random() < per_day * tick_seconds / (16 * 3600)


def refresh(group):
    """A fresh conversation starts with its energy back."""
    meta = group.meta()
    meta["energy"] = FULL
    group._save_meta(meta)
