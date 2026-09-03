"""
The phone book.

A contact is everything that makes one correspondent themselves: which model
answers, which voice speaks, how terse they are, what they are willing to look
up, when they are reachable, and the worked examples that teach the model their
register. None of it is hardcoded in the engine — adding Lucius Fox means
dropping a JSON profile next to Alfred's, not touching Python.
"""
import json
import os
import time
from dataclasses import dataclass, field

from ..paths import PROFILE_DIR


@dataclass(frozen=True)
class Availability:
    """
    When a contact picks up. Alfred is always reachable; someone with a day job
    is not, and a console that admits it is more convincing than one where
    everybody answers instantly at 4am.
    """
    kind: str = "always"           # "always" | "hours"
    days: tuple = (0, 1, 2, 3, 4)  # Monday = 0
    start_hour: int = 9
    end_hour: int = 18
    away_message: str = "Not available right now."

    def is_available(self, now=None):
        if self.kind == "always":
            return True
        now = now or time.localtime()
        return now.tm_wday in self.days and self.start_hour <= now.tm_hour < self.end_hour


@dataclass(frozen=True)
class Contact:
    id: str
    name: str
    full_name: str
    role: str
    tagline: str
    model: str
    voice_id: str
    accent: str
    max_reply_sentences: int
    can_search: bool
    options: dict
    boot_prompts: dict
    # Optional. Contacts with their own built Ollama model (an `ollama create`
    # from a Modelfile) carry their personality in the model itself and leave
    # this empty. Contacts that share a base model declare it here instead, so
    # adding one is a JSON file rather than a model build.
    system: str = ""
    # Reasoning models (the qwen3 family, gpt-oss, deepseek-r1) emit a separate
    # `thinking` stream and produce no spoken content until it finishes. Left
    # on, a contact appears to hang for ten or twenty seconds before the first
    # word. Profiles using such a model should set this false. It is only sent
    # when explicitly declared, because passing it to a model that has no
    # thinking mode is an error.
    think: bool | None = None
    primer: tuple = ()
    availability: Availability = field(default_factory=Availability)

    @property
    def has_voice(self):
        return bool(self.voice_id)

    def primer_messages(self):
        """
        Worked examples as real user/assistant turns at the head of the context.

        A model imitates a conversation it can see far more reliably than a
        prose description of one, so these are injected as actual messages
        rather than pasted into the system prompt as text.
        """
        messages = []
        for exchange in self.primer:
            messages.append({"role": "user", "content": exchange["user"]})
            messages.append({"role": "assistant", "content": exchange["assistant"]})
        return messages


def _load_profile(path):
    with open(path) as f:
        raw = json.load(f)

    availability = Availability(**raw.get("availability", {})) if raw.get("availability") \
        else Availability()

    # Voice IDs are secrets, so profiles name an environment variable rather
    # than carrying the value.
    voice_id = os.getenv(raw.get("voice_env", ""), "") if raw.get("voice_env") else ""

    return Contact(
        id=raw["id"],
        name=raw["name"],
        full_name=raw.get("full_name", raw["name"]),
        role=raw.get("role", ""),
        tagline=raw.get("tagline", ""),
        model=os.getenv(raw.get("model_env", ""), "") or raw["model"],
        voice_id=voice_id,
        accent=raw.get("accent", "#4FA8E0"),
        max_reply_sentences=int(raw.get("max_reply_sentences", 4)),
        can_search=bool(raw.get("can_search", True)),
        options=raw.get("options", {}),
        boot_prompts=raw.get("boot_prompts", {}),
        system=raw.get("system", ""),
        think=raw.get("think"),
        primer=tuple(raw.get("primer", [])),
        availability=availability,
    )


class Directory:
    """All known contacts, loaded once from the profile directory."""

    def __init__(self, profile_dir=None):
        self.profile_dir = profile_dir or PROFILE_DIR
        self._contacts = {}
        self.reload()

    def reload(self):
        self._contacts = {}
        for path in sorted(self.profile_dir.glob("*.json")):
            try:
                contact = _load_profile(path)
            except Exception as exc:
                raise ValueError(f"Could not load contact profile {path.name}: {exc}") from exc
            self._contacts[contact.id] = contact

    def __iter__(self):
        return iter(self._contacts.values())

    def __len__(self):
        return len(self._contacts)

    def get(self, contact_id):
        return self._contacts.get(contact_id)

    def ids(self):
        return list(self._contacts)


_directory = None


def directory():
    global _directory
    if _directory is None:
        _directory = Directory()
    return _directory
