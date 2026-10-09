"""
The text thread with one contact, kept for the long run.

Texts are also written into the contact's conversation history, so they know
what was texted when you next call — but that history is short by design and
calls push old turns out of it. The thread is the thing you scroll back
through, so it keeps its own record: every message, when it was sent, and when
yours was read. Encrypted at rest like the rest of a contact's memory.
"""
import json
import threading
import time
import uuid

from .. import paths
from .store import atomic_write, read_text

MAX_MESSAGES = 2000
_lock = threading.Lock()


class TextLog:
    def __init__(self, contact_id):
        self.path = paths.contact_dir(contact_id) / "texts.json"

    def _load(self):
        try:
            return json.loads(read_text(self.path) or "[]")
        except ValueError:
            return []

    def add(self, sender, text, at=None, kind=None):
        """
        Append a message ('me' or 'them'). Returns it. `kind` marks something
        that isn't a message — "missed_call", "declined_call" — shown in the
        thread as a line of its own.
        """
        message = {"id": uuid.uuid4().hex[:12], "from": sender, "text": text,
                   "at": at or time.time()}
        if kind:
            message["kind"] = kind
        with _lock:
            messages = self._load() + [message]
            atomic_write(self.path, json.dumps(messages[-MAX_MESSAGES:]))
        return message

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
                and not m.get("kind")]

    def last(self):
        """The last real message in the thread, or None."""
        messages = [m for m in self._load() if not m.get("kind")]
        return messages[-1] if messages else None

    def clear(self):
        if self.path.exists():
            self.path.unlink()
