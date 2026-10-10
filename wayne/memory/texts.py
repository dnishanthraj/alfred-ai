"""
The text thread with one contact, kept for the long run.

Texts are also written into the contact's conversation history, so they know
what was texted when you next call — but that history is short by design and
calls push old turns out of it. The thread is the thing you scroll back
through, so it keeps its own record: every message, when it was sent, and when
yours was read. Encrypted at rest like the rest of a contact's memory.
"""
import json
import re
import threading
import time
import uuid

from .. import paths
from .store import atomic_write, read_text

MAX_MESSAGES = 2000
_lock = threading.Lock()


def quote(messages, message_id):
    """The message an answer is to, as the answer keeps it: who, and the start of what they said."""
    if not message_id:
        return None
    found = next((m for m in messages if m.get("id") == message_id and not m.get("kind")), None)
    if found is None:
        return None
    text = found["text"]
    return {"id": found["id"], "from": found["from"], "text": text if len(text) <= 120 else text[:117] + "…"}


def quoted_by(messages, words, fallback=False):
    """
    Which of `messages` a reply means by a few of its words ("the car thing")
    — the best fit, the latest of equals; with no fit, the latest if
    `fallback`, else None.
    """
    if not messages:
        return None
    wanted = set(re.findall(r"\w+", (words or "").lower()))

    def fit(m):
        return len(wanted & set(re.findall(r"\w+", m["text"].lower()))) / len(wanted) if wanted else 0

    best = max(reversed(messages), key=fit)
    if fit(best) >= 0.5:
        return best
    return messages[-1] if fallback else None


class TextLog:
    def __init__(self, contact_id):
        self.path = paths.contact_dir(contact_id) / "texts.json"

    def _load(self):
        try:
            return json.loads(read_text(self.path) or "[]")
        except ValueError:
            return []

    def add(self, sender, text, at=None, kind=None, origin=None, reply_to=None):
        """
        Append a message ('me' or 'them'). Returns it. `kind` marks something
        that isn't a message — "missed_call", "declined_call" — shown in the
        thread as a line of its own. `reply_to` is the id of the message this
        one answers: it's kept with a short quote of it, as a phone shows.
        """
        message = {"id": uuid.uuid4().hex[:12], "from": sender, "text": text,
                   "at": at or time.time()}
        if kind:
            message["kind"] = kind
        if origin:
            message["origin"] = origin      # sent unprompted: "chase", "callback", ...
        with _lock:
            messages = self._load()
            quoted = quote(messages, reply_to)
            if quoted:
                message["reply_to"] = quoted
            messages = messages + [message]
            atomic_write(self.path, json.dumps(messages[-MAX_MESSAGES:]))
        return message

    def mark_seen(self, at=None):
        """He's opened the thread: their messages are read — and they can tell."""
        at = at or time.time()
        with _lock:
            messages = self._load()
            fresh = [m for m in messages if m["from"] == "them" and not m.get("seen_at") and not m.get("kind")]
            for m in fresh:
                m["seen_at"] = at
            if fresh:
                atomic_write(self.path, json.dumps(messages))
        return len(fresh)

    def mark_read(self, ids, at=None):
        at = at or time.time()
        with _lock:
            messages = self._load()
            for m in messages:
                if m["id"] in ids and not m.get("read_at"):
                    m["read_at"] = at
            atomic_write(self.path, json.dumps(messages))
        return at

    def page(self, before=None, limit=40):
        """The newest `limit` messages older than `before`, oldest first."""
        messages = self._load()
        if before:
            messages = [m for m in messages if m["at"] < before]
        return messages[-limit:]

    def unread(self):
        """His messages they haven't read yet — waiting, across a restart."""
        return [m for m in self._load() if m["from"] == "me" and not m.get("read_at")
                and not m.get("kind") and not m.get("ignored")]

    def ignore(self, ids):
        """Never opened, on purpose: left at Delivered for good."""
        with _lock:
            messages = self._load()
            for m in messages:
                if m["id"] in ids:
                    m["ignored"] = True
            atomic_write(self.path, json.dumps(messages))

    def react(self, message_id, who, emoji):
        """A tapback on a message ('me' or 'them'), or with no emoji, taken back. Returns it."""
        with _lock:
            messages = self._load()
            for m in messages:
                if m["id"] == message_id:
                    reactions = m.setdefault("reactions", {})
                    if emoji:
                        reactions[who] = emoji
                    else:
                        reactions.pop(who, None)
                    atomic_write(self.path, json.dumps(messages))
                    return m
        return None

    def last(self):
        """The last real message in the thread, or None."""
        messages = [m for m in self._load() if not m.get("kind")]
        return messages[-1] if messages else None

    def clear(self):
        if self.path.exists():
            self.path.unlink()
