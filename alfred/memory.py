import json
import os
import tempfile

from .paths import CORE_MEMORY_FILE, HISTORY_FILE

# Number of full exchanges (user + assistant pairs) to keep as short-term memory.
# Stored as message count = pairs * 2. 16 pairs = 32 messages ~ a solid
# recent-conversation window without bloating context or slowing the model.
MAX_HISTORY_PAIRS = 16
MAX_HISTORY_MESSAGES = MAX_HISTORY_PAIRS * 2

# The vault is injected into *every* prompt, so it cannot grow without bound —
# past a few hundred lines it starts crowding out the conversation window.
MAX_VAULT_ENTRIES = 200

_MEMORIZE_TRIGGERS = [
    "remember that", "remember to", "note that", "don't forget that", "don't forget",
]


def _atomic_write(path, text):
    """
    Write via a temp file in the same directory, then rename. A crash partway
    through a plain open(...,'w') truncates the file, and since load_history()
    treats unparseable JSON as "no history", that silently wipes the user's
    conversation memory.
    """
    directory = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-", suffix=".swap")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_history():
    if HISTORY_FILE.exists():
        try:
            with open(HISTORY_FILE, 'r') as f:
                history = json.load(f)
            return history[-MAX_HISTORY_MESSAGES:]
        except (json.JSONDecodeError, IOError):
            # Corrupt or unreadable history shouldn't crash the boot.
            return []
    return []


def save_history(history):
    pruned = history[-MAX_HISTORY_MESSAGES:]
    _atomic_write(HISTORY_FILE, json.dumps(pruned, indent=2))


def clear_history():
    """Wipes the short-term conversation memory."""
    if HISTORY_FILE.exists():
        HISTORY_FILE.unlink()
    return []


def load_core_memory():
    if CORE_MEMORY_FILE.exists():
        with open(CORE_MEMORY_FILE, 'r') as f:
            return f.read().strip()
    return ""


def vault_entries():
    """The vault as a list of individual facts, without their bullet markers."""
    raw = load_core_memory()
    if not raw:
        return []
    return [line.lstrip("- ").strip() for line in raw.splitlines() if line.strip()]


def clear_core_memory():
    if CORE_MEMORY_FILE.exists():
        CORE_MEMORY_FILE.unlink()


def memorize(text):
    """
    Strip the trigger phrase and append the remaining fact to the vault.
    Duplicates are ignored — repeating "remember that I live in London" three
    times shouldn't spend three lines of every future prompt on it.
    """
    clean_text = text
    lowered = text.lower()
    for trigger in _MEMORIZE_TRIGGERS:
        if lowered.startswith(trigger):
            clean_text = text[len(trigger):].strip()
            break

    if not clean_text:
        return text

    clean_text = clean_text[0].upper() + clean_text[1:]

    existing = vault_entries()
    if clean_text.lower() in (e.lower() for e in existing):
        return clean_text

    entries = existing + [clean_text]
    if len(entries) > MAX_VAULT_ENTRIES:
        entries = entries[-MAX_VAULT_ENTRIES:]

    _atomic_write(CORE_MEMORY_FILE, "".join(f"- {e}\n" for e in entries))
    return clean_text
