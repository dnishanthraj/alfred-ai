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


def transcript(messages, name):
    return "\n".join(f"({m.get('said') or m['text']})" if m.get("kind") == "system"
                     else f"{name(m['from'])}: {m['text']}" for m in messages)


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
    return (f"Not everyone in this chat knows what's under the masks ({', '.join(outsiders)} "
            "doesn't). Say nothing that would give anyone away.")


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
    """Named in it — 'Dick?', '@tim', 'Barbara, ...' — so they answer."""
    for name in {contact.name, contact.full_name, contact.id}:
        if re.search(rf"(?<!\w)@?{re.escape(name)}\b", text or "", re.I):
            return True
    return False


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
    if any(addressed(m["text"], contact) for m in unread):
        return 1.0
    chatty = min(1.0, 0.25 + (contact.initiative or {}).get("per_day", 0.5) * 0.4)
    from_bruce = any(m["from"] == "me" for m in unread)
    question = any(m["text"].rstrip().endswith("?") for m in unread)
    odds = chatty * (1.2 if question else 1.0) * (1.25 if from_bruce else 1.0)
    # Someone who leaves texts on read leaves group chats on read too: Randy,
    # ghosting the family, reads it and says nothing far more often than not.
    odds *= 1 - (contact.texting_pace or {}).get("on_read", 0)
    return max(0.0, min(0.95, odds * energy * lively))


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
