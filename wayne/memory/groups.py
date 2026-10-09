"""
Group chats: one log per group, the single record of what was said in it.

Nobody's own memory holds a copy. What a contact knows of a group is what they
have read of it — a read pointer per member — so Dick on a call can tell Bruce
what Barbara and Tim have been on about, but only as far as he's actually got
through the thread, and two contacts can never remember the same chat
differently. Encrypted at rest like the rest of memory.
"""
import json
import threading
import time
import uuid

from .. import paths
from .store import atomic_write, read_text

MAX_MESSAGES = 3000
_lock = threading.RLock()


def _root():
    return paths.DATA_DIR / "_groups"


class Group:
    """One group chat: who's in it, what was said, and how far each member has read."""

    def __init__(self, group_id):
        self.id = group_id
        self.dir = _root() / group_id
        self.meta_path = self.dir / "group.json"
        self.log_path = self.dir / "messages.json"

    # --- storage -------------------------------------------------------------

    def meta(self):
        try:
            return json.loads(read_text(self.meta_path) or "{}")
        except ValueError:
            return {}

    def _save_meta(self, meta):
        atomic_write(self.meta_path, json.dumps(meta, indent=1))

    def messages(self):
        try:
            return json.loads(read_text(self.log_path) or "[]")
        except ValueError:
            return []

    # --- reading and writing ------------------------------------------------------

    @property
    def name(self):
        return self.meta().get("name", "Group")

    @property
    def members(self):
        """Contact ids in the group — Bruce is always in it, and isn't listed."""
        return list(self.meta().get("members", []))

    def add(self, sender, text, at=None, origin=None):
        """Post a message from 'me' (Bruce) or a contact id. Returns it."""
        message = {"id": uuid.uuid4().hex[:12], "from": sender, "text": text, "at": at or time.time()}
        if origin:
            message["origin"] = origin
        with _lock:
            messages = self.messages() + [message]
            atomic_write(self.log_path, json.dumps(messages[-MAX_MESSAGES:]))
            if sender != "me":
                self._mark(sender, message["at"])      # you've read what you wrote
        return message

    def _mark(self, member, at):
        meta = self.meta()
        reads = meta.setdefault("reads", {})
        if at > reads.get(member, 0):
            reads[member] = at
            self._save_meta(meta)

    def mark_read(self, member, at=None):
        """They've read up to now (or `at`). Returns the time."""
        at = at or time.time()
        with _lock:
            self._mark(member, at)
        return at

    def read_upto(self, member):
        return self.meta().get("reads", {}).get(member, 0)

    def unread_for(self, member):
        """Messages they haven't read, not counting their own."""
        upto = self.read_upto(member)
        # System lines count — "Bruce removed Tim" is something to react to.
        return [m for m in self.messages() if m["at"] > upto and m["from"] != member]

    def seen_by(self, member, limit=12):
        """The tail of the thread as far as they've read it — what they can know."""
        upto = self.read_upto(member)
        return [m for m in self.messages() if m["at"] <= upto][-limit:]

    def page(self, before=None, limit=40):
        messages = self.messages()
        if before:
            messages = [m for m in messages if m["at"] < before]
        return messages[-limit:]

    def system(self, text, at=None, said=None):
        """
        A line about the group, not in it: 'Randy left', 'Dick added Barbara'.
        `said` is how the characters read it when it differs from what Bruce
        sees — "You added Barbara" to him is "Bruce added Barbara" to them.
        """
        message = {"id": uuid.uuid4().hex[:12], "from": "system", "kind": "system", "text": text,
                   "at": at or time.time()}
        if said:
            message["said"] = said
        with _lock:
            messages = self.messages() + [message]
            atomic_write(self.log_path, json.dumps(messages[-MAX_MESSAGES:]))
        return message

    def react(self, message_id, member, emoji):
        """A tapback on a message — or, with no emoji, taking one back. Returns the message."""
        with _lock:
            messages = self.messages()
            for message in messages:
                if message.get("id") == message_id:
                    reactions = message.setdefault("reactions", {})
                    if emoji:
                        reactions[member] = emoji
                    else:
                        reactions.pop(member, None)
                    atomic_write(self.log_path, json.dumps(messages[-MAX_MESSAGES:]))
                    return message
        return None

    def add_member(self, contact_id, at=None):
        """In they come — having read nothing yet, so the recent thread is theirs to read."""
        with _lock:
            meta = self.meta()
            if contact_id in meta.get("members", []):
                return False
            meta.setdefault("members", []).append(contact_id)
            # They can scroll up, as anyone added to a group can.
            meta.setdefault("reads", {})[contact_id] = 0
            self._save_meta(meta)
        return True

    def remove_member(self, contact_id, by_bruce=False):
        with _lock:
            meta = self.meta()
            if contact_id not in meta.get("members", []):
                return False
            meta["members"].remove(contact_id)
            if by_bruce:
                # Kept out: nobody else gets to add them straight back.
                meta.setdefault("removed", {})[contact_id] = time.time()
            self._save_meta(meta)
        return True

    def rename(self, name):
        with _lock:
            meta = self.meta()
            meta["name"] = (name or meta.get("name", "Group")).strip()[:60]
            self._save_meta(meta)

    def readers_of(self, message):
        """Which members have read a given message."""
        reads = self.meta().get("reads", {})
        return [m for m in self.members if reads.get(m, 0) >= message["at"] and m != message["from"]]

    def summary(self):
        meta, messages = self.meta(), self.messages()
        return {"id": self.id, "name": meta.get("name", "Group"), "members": meta.get("members", []),
                "created_at": meta.get("created_at"), "reads": meta.get("reads", {}),
                "last": messages[-1] if messages else None}

    def delete(self):
        with _lock:
            for path in (self.log_path, self.meta_path):
                if path.exists():
                    path.unlink()
            if self.dir.exists():
                try:
                    self.dir.rmdir()
                except OSError:
                    pass


def create(name, members):
    """A new group of Bruce and these contacts. Returns it."""
    group = Group(uuid.uuid4().hex[:10])
    with _lock:
        group._save_meta({"name": (name or "Group").strip()[:60], "members": list(dict.fromkeys(members)),
                          "created_at": time.time(), "reads": {}})
    return group


def all_groups():
    root = _root()
    if not root.exists():
        return []
    groups = [Group(p.name) for p in root.iterdir() if (p / "group.json").exists()]
    return sorted(groups, key=lambda g: (g.summary()["last"] or {}).get("at", g.meta().get("created_at", 0)),
                  reverse=True)


def of(group_id):
    group = Group(group_id)
    return group if group.meta_path.exists() else None


def groups_with(contact_id):
    return [g for g in all_groups() if contact_id in g.members]


def clear_all():
    for group in all_groups():
        group.delete()
