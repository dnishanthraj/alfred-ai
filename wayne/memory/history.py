"""
Short-term conversation memory, scoped to one contact.

Every message carries the time it was said. The model never sees that field —
it is stripped before the payload is built — but it is what lets a contact know
whether the last exchange was ten minutes or three weeks ago, which is most of
the difference between "Evening again" and "It's been a while."
"""
import json
import time

from .. import paths
from .store import atomic_write, read_text

# Full exchanges kept as short-term memory. Raised from 16 once the system
# prompt came down from 1440 words to ~490: the context freed up there is far
# better spent on what was actually said than on restating rules. At roughly
# 25 words a turn this is a few thousand tokens, which the prefix cache covers.
MAX_HISTORY_PAIRS = 30
MAX_HISTORY_MESSAGES = MAX_HISTORY_PAIRS * 2


def describe_gap(seconds):
    """A human interval, the way someone would actually say it."""
    if seconds is None:
        return ""
    minutes = seconds / 60
    if minutes < 2:
        return "moments ago"
    if minutes < 60:
        return f"{int(minutes)} minutes ago"
    hours = minutes / 60
    if hours < 24:
        return "about an hour ago" if hours < 1.7 else f"{int(hours)} hours ago"
    days = hours / 24
    if days < 2:
        return "yesterday"
    if days < 14:
        return f"{int(days)} days ago"
    if days < 60:
        return f"{int(days / 7)} weeks ago"
    return f"{int(days / 30)} months ago"


class History:
    def __init__(self, contact_id):
        self.contact_id = contact_id
        self.path = paths.history_file(contact_id)
        self.messages = self._load()

    def _load(self):
        if not self.path.exists():
            return []
        try:
            return json.loads(read_text(self.path))[-MAX_HISTORY_MESSAGES:]
        except (OSError, json.JSONDecodeError, ValueError):
            # Corrupt or unreadable history shouldn't crash the boot.
            return []

    def __bool__(self):
        return bool(self.messages)

    def __len__(self):
        return len(self.messages)

    def for_model(self):
        """
        The messages as the model expects them: role and content only.
        Timestamps are ours, not the model's, and sending unknown keys to
        Ollama is asking for trouble.
        """
        return [
            {
                "role": m["role"],
                "content": (m["content"] + " " + m["aside"]).strip()
                           if m.get("aside") else m["content"],
            }
            for m in self.messages
        ]

    def last_greeting(self, marker):
        """The greeting from the most recent connection, or '' if there is none."""
        for index in range(len(self.messages) - 2, -1, -1):
            if (self.messages[index]["role"] == "user"
                    and self.messages[index]["content"] == marker
                    and self.messages[index + 1]["role"] == "assistant"):
                return self.messages[index + 1]["content"]
        return ""

    def drop_prior_greetings(self, marker):
        """
        Remove earlier connection greetings, keeping the conversation itself.

        Every call stores a placeholder user turn and the greeting that answered
        it. Across a few calls that leaves a stack of "Good morning" in the
        context, and a model reading four greetings writes a fifth — which is
        exactly how a character starts sounding like it has no memory of
        speaking to you.
        """
        kept = []
        index = 0
        while index < len(self.messages):
            message = self.messages[index]
            is_pair = (message["role"] == "user"
                       and message["content"] == marker
                       and index + 1 < len(self.messages)
                       and self.messages[index + 1]["role"] == "assistant")
            if is_pair:
                index += 2      # drop the placeholder and the greeting with it
                continue
            kept.append(message)
            index += 1
        self.messages = kept

    def append(self, role, content):
        self.messages.append({"role": role, "content": content, "at": time.time()})

    def record_aside(self, text):
        """
        Record something the contact said unprompted — a check-in, a sign-off,
        picking the thread back up after a pause.

        It is appended to his previous turn rather than added as a new one.
        Everything he says has to be remembered, or he asks "still with me?"
        and then has no idea he asked; but a bare assistant message with no
        user turn before it breaks the alternation the model relies on. Said
        one after the other with nothing in between, they *were* one turn.
        """
        if not text:
            return
        if self.messages and self.messages[-1]["role"] == "assistant":
            # Kept in its own field, not concatenated. Appending to the content
            # meant a second check-in stacked onto the first — "Still here.
            # Still here." — and once that was in the context he produced more
            # of it. One turn has at most one trailing aside; a newer one
            # replaces the older, because that is what actually happened.
            self.messages[-1]["aside"] = text.strip()
            self.messages[-1]["at"] = time.time()
        else:
            self.append("assistant", text)
        self.save()

    def record_exchange(self, prompt, reply):
        """Store only the raw exchange — never the injected reference context."""
        self.append("user", prompt)
        self.append("assistant", reply)
        self.save()

    def recent_assistant(self, turns=6):
        return [m["content"] for m in self.messages[-turns:] if m["role"] == "assistant"]

    def recent_user(self, turns=6):
        return [m["content"] for m in self.messages[-turns:] if m["role"] == "user"]

    def last_assistant(self):
        if self.messages and self.messages[-1]["role"] == "assistant":
            return self.messages[-1]["content"]
        return ""

    def seconds_since_last(self):
        """How long since anything was said, or None for a fresh history."""
        for message in reversed(self.messages):
            stamp = message.get("at")
            if stamp:
                return max(0.0, time.time() - stamp)
        return None

    def time_since_last(self):
        return describe_gap(self.seconds_since_last())

    def save(self):
        del self.messages[:-MAX_HISTORY_MESSAGES]
        atomic_write(self.path, json.dumps(self.messages, indent=2))

    def clear(self):
        if self.path.exists():
            self.path.unlink()
        self.messages = []
