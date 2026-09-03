"""
Long-term memory: facts the operator explicitly asked a contact to remember.

On retrieval: the vault is injected into every prompt, so its size is a running
tax on the context window. Below `ALWAYS_INJECT_BELOW` entries that tax is
trivial and everything goes in — selecting a subset would only risk dropping
the one fact that mattered. Above it, entries are scored against the current
prompt and only the best are sent.

The scorer is deliberately lexical (term overlap, recency, a small bonus for
proper nouns). Embeddings would retrieve better on paraphrase — "my job" vs
"where I work" — but they add a model, an index to keep in sync, and a startup
cost, and at a few hundred short facts the lexical version is close enough that
the difference is hard to notice. `score_entries` is the seam to swap if that
stops being true.
"""
import re

from .. import paths
from .store import atomic_write

MAX_VAULT_ENTRIES = 200
# Under this many facts, send the whole vault — selection can only lose.
ALWAYS_INJECT_BELOW = 40
# When selecting, how many facts to send.
RETRIEVE_TOP_K = 25

_TRIGGERS = [
    "remember that", "remember to", "note that", "don't forget that", "don't forget",
]

_STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "is", "are", "was", "were", "be", "been",
    "i", "im", "i'm", "my", "me", "you", "your", "he", "she", "it", "we", "they",
    "to", "of", "in", "on", "at", "for", "with", "that", "this", "have", "has",
    "do", "does", "did", "what", "who", "when", "where", "how", "why", "about",
}


def _terms(text):
    return {
        word for word in re.findall(r"[a-z0-9']+", (text or "").lower())
        if word not in _STOPWORDS and len(word) > 2
    }


class Vault:
    def __init__(self, contact_id):
        self.contact_id = contact_id
        self.path = paths.vault_file(contact_id)

    def entries(self):
        """The vault as individual facts, without bullet markers."""
        if not self.path.exists():
            return []
        with open(self.path) as f:
            return [line.lstrip("- ").strip() for line in f if line.strip()]

    def score_entries(self, prompt, entries):
        """
        Rank facts by relevance to the current prompt. Recency is a weak
        tiebreaker so that, absent any lexical signal, the newest facts win.
        """
        prompt_terms = _terms(prompt)
        total = len(entries)
        scored = []
        for index, entry in enumerate(entries):
            entry_terms = _terms(entry)
            overlap = len(prompt_terms & entry_terms)
            # Capitalised words mid-sentence are usually names, places, or
            # projects — the things worth surfacing when they come up.
            proper = sum(1 for w in entry.split()[1:] if w[:1].isupper())
            recency = index / total if total else 0
            scored.append((overlap * 3 + proper * 0.5 + recency, index, entry))
        scored.sort(key=lambda item: (-item[0], -item[1]))
        return scored

    def relevant(self, prompt, limit=RETRIEVE_TOP_K):
        """The facts worth spending context on for this particular prompt."""
        entries = self.entries()
        if len(entries) < ALWAYS_INJECT_BELOW:
            return entries

        picked = self.score_entries(prompt, entries)[:limit]
        # Restore original order so the model reads them as a coherent dossier
        # rather than a relevance-ranked list.
        picked.sort(key=lambda item: item[1])
        return [entry for _, _, entry in picked]

    def as_block(self, prompt=""):
        facts = self.relevant(prompt) if prompt else self.entries()
        return "\n".join(f"- {fact}" for fact in facts)

    def memorize(self, text):
        """
        Strip the trigger phrase and append the fact. Duplicates are ignored —
        repeating "remember that I live in London" three times shouldn't spend
        three lines of every future prompt on it.
        """
        clean = text
        lowered = text.lower()
        for trigger in _TRIGGERS:
            if lowered.startswith(trigger):
                clean = text[len(trigger):].strip()
                break

        if not clean:
            return text

        clean = clean[0].upper() + clean[1:]
        existing = self.entries()
        if clean.lower() in (e.lower() for e in existing):
            return clean

        entries = (existing + [clean])[-MAX_VAULT_ENTRIES:]
        atomic_write(self.path, "".join(f"- {e}\n" for e in entries))
        return clean

    def forget(self, needle):
        """Drop facts matching a phrase. Returns what was removed."""
        entries = self.entries()
        needle_l = needle.lower().strip()
        keep = [e for e in entries if needle_l not in e.lower()]
        removed = [e for e in entries if needle_l in e.lower()]
        if removed:
            atomic_write(self.path, "".join(f"- {e}\n" for e in keep))
        return removed

    def clear(self):
        if self.path.exists():
            self.path.unlink()
