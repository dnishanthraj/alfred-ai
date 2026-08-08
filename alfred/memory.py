import json

from .paths import CORE_MEMORY_FILE, HISTORY_FILE

# Number of full exchanges (user + assistant pairs) to keep as short-term memory.
# Stored as message count = pairs * 2. 16 pairs = 32 messages ~ a solid
# recent-conversation window without bloating context or slowing the model.
MAX_HISTORY_PAIRS = 16
MAX_HISTORY_MESSAGES = MAX_HISTORY_PAIRS * 2


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
    with open(HISTORY_FILE, 'w') as f:
        json.dump(pruned, f, indent=2)


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


def memorize(text):
    triggers = ["remember that", "remember to", "note that", "don't forget that", "don't forget"]
    clean_text = text
    for t in triggers:
        if text.lower().startswith(t):
            clean_text = text[len(t):].strip()
            break

    if clean_text:
        clean_text = clean_text[0].upper() + clean_text[1:]
        entry = f"- {clean_text}"
        with open(CORE_MEMORY_FILE, 'a') as f:
            f.write(entry + "\n")
        return clean_text
    return text
