"""Short-term conversation memory, scoped to one contact."""
import json

from .. import paths
from .store import atomic_write

# Full exchanges (user + assistant pairs) kept as short-term memory. 16 pairs
# is a solid recent-conversation window without bloating context.
MAX_HISTORY_PAIRS = 16
MAX_HISTORY_MESSAGES = MAX_HISTORY_PAIRS * 2


class History:
    def __init__(self, contact_id):
        self.contact_id = contact_id
        self.path = paths.history_file(contact_id)
        self.messages = self._load()

    def _load(self):
        if not self.path.exists():
            return []
        try:
            with open(self.path) as f:
                return json.load(f)[-MAX_HISTORY_MESSAGES:]
        except (OSError, json.JSONDecodeError, ValueError):
            # Corrupt or unreadable history shouldn't crash the boot.
            return []

    def __bool__(self):
        return bool(self.messages)

    def __iter__(self):
        return iter(self.messages)

    def __getitem__(self, item):
        return self.messages[item]

    def append(self, role, content):
        self.messages.append({"role": role, "content": content})

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

    def save(self):
        del self.messages[:-MAX_HISTORY_MESSAGES]
        atomic_write(self.path, json.dumps(self.messages, indent=2))

    def clear(self):
        if self.path.exists():
            self.path.unlink()
        self.messages = []
