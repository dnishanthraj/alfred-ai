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
import re
import time
from dataclasses import dataclass, field

from .. import config
from ..paths import PROFILE_DIR, ROOT_DIR


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
        if self.start_hour <= self.end_hour:
            return now.tm_wday in self.days and self.start_hour <= now.tm_hour < self.end_hour
        # Hours that run past midnight ("20" to "4") belong to the day they began.
        if now.tm_hour >= self.start_hour:
            return now.tm_wday in self.days
        return now.tm_hour < self.end_hour and (now.tm_wday - 1) % 7 in self.days


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
    # thinking mode is an error. gpt-oss cannot switch it off at all and takes
    # a level instead — "low", "medium" or "high" — so a string passes through.
    think: bool | str | None = None
    # Forms of address this character would never use. Enforced in code
    # because a smaller model will ignore the instruction often enough to
    # matter, and one "lad" undoes a great deal of careful prompting.
    forbidden_address: tuple = ()
    # The relationship, written from the operator's side. Editable in the
    # console; this is only what it says before anyone edits it.
    bio: str = ""
    primer: tuple = ()
    availability: Availability = field(default_factory=Availability)
    # Corners of their own life for an unprompted remark to come from when a
    # silence wants breaking — Alfred's garden is not Selina's rooftops.
    own_life: tuple = ()
    # Last-resort lines when two attempts in a row echoed the operator.
    deflections: tuple = ()
    # One sentence telling the evaluation judge who this is meant to be.
    judge: str = ""
    # How the personnel-file portrait is cropped: {"size": "180%", "position":
    # "50% 18%"}. Supplied images vary — a square render, a tall full-length
    # shot — and one crop does not suit them all.
    portrait: dict = field(default_factory=dict)
    # How they write a text message — "full sentences, proper punctuation",
    # "lowercase, no full stops". A text from Selina should not read like one
    # from Alfred.
    texting: str = ""
    # Heading the directory lists them under ("Family", "Gotham").
    group: str = "Contacts"
    # How quickly they read and answer a text, in seconds: {"read": [min, max],
    # "busy": chance of being tied up, "busy_for": [min, max], "wpm": typing}.
    texting_pace: dict = field(default_factory=dict)

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


# A Modelfile's SYSTEM block, so an existing personality file can be used
# directly rather than duplicated into the profile.
_MODELFILE_SYSTEM = re.compile(r'SYSTEM\s+"""(.*?)"""', re.S)


def _read_system_file(name):
    """
    Load a character's system prompt from a file outside the repository.

    It exists because the prompt contains real details about the operator and
    must stay gitignored, while the profile that references it is committed.
    Pointing at a Modelfile works too: its SYSTEM block is extracted, so the
    same file can be used with `ollama create` or read directly.
    """
    # An unset `system_file` must not resolve to `ROOT_DIR / ""`, which is the
    # project directory itself: it exists, so the guard below passes, and the
    # read then fails with "Is a directory" — taking down every profile that
    # simply declares neither `system` nor `system_file`.
    if not name:
        return ""
    path = ROOT_DIR / name
    if not path.exists() or not path.is_file():
        return ""
    text = path.read_text()
    match = _MODELFILE_SYSTEM.search(text)
    return (match.group(1) if match else text).strip()


def _system_prompt(raw):
    """
    The character, then the private file — both optional, either alone fine.

    The character is who they are and belongs in the committed profile, where
    it can be read, reviewed and improved. The file holds what is personal to
    the operator — names, work, where they live — and stays gitignored. They
    used to be one or the other, which put the whole character in a private
    file nobody else could see. `system` may be a list of paragraphs, because a
    character written as one JSON string is unreadable.
    """
    character = raw.get("system") or ""
    if isinstance(character, list):
        character = "\n\n".join(character)
    # One private file or several: the facts every contact shares, then what is
    # private to this one (what they call him, say) — both gitignored.
    files = raw.get("system_file") or []
    files = [files] if isinstance(files, str) else files
    private = [text for text in (_read_system_file(name) for name in files) if text]
    return "\n\n".join(part for part in (character.strip(), *private) if part)


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
        options={"num_ctx": config.CONTEXT_WINDOW, **raw.get("options", {})},
        boot_prompts=raw.get("boot_prompts", {}),
        system=_system_prompt(raw),
        think=raw.get("think"),
        forbidden_address=tuple(raw.get("forbidden_address", [])),
        bio=raw.get("bio", ""),
        primer=tuple(raw.get("primer", [])),
        availability=availability,
        own_life=tuple(raw.get("own_life", [])),
        deflections=tuple(raw.get("deflections", [])),
        judge=raw.get("judge", ""),
        portrait=raw.get("portrait", {}),
        texting=raw.get("texting", ""),
        group=raw.get("group", "Contacts"),
        texting_pace=raw.get("texting_pace", {}),
    )


class Directory:
    """All known contacts, loaded once from the profile directory."""

    def __init__(self, profile_dir=None):
        self.profile_dir = profile_dir or PROFILE_DIR
        self._contacts = {}
        self.reload()

    def reload(self):
        self._contacts = {}
        loaded = []
        for path in sorted(self.profile_dir.glob("*.json")):
            try:
                loaded.append((json.loads(path.read_text()).get("order", 100), _load_profile(path)))
            except Exception as exc:
                raise ValueError(f"Could not load contact profile {path.name}: {exc}") from exc
        # The directory's order is the console's: Alfred first, not alphabetical.
        for _, contact in sorted(loaded, key=lambda item: item[0]):
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
