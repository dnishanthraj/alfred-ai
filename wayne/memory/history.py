"""
Short-term conversation memory, scoped to one contact.

Every message carries the time it was said. The model never sees that field —
it is stripped before the payload is built — but it is what lets a contact know
whether the last exchange was ten minutes or three weeks ago, which is most of
the difference between "Evening again" and "It's been a while."
"""
import json
import threading
import time

from .. import delivery, paths
from ..config import setting
from .store import atomic_write, read_text

# Full exchanges kept on disk as short-term memory.
MAX_HISTORY_PAIRS = 30
MAX_HISTORY_MESSAGES = MAX_HISTORY_PAIRS * 2

# How much of that history is actually sent to the model, in words.
#
# The disk cap above is generous because storage is cheap. This one is not,
# because prompt evaluation is the entire latency budget and it is paid again
# on every turn: Ollama here re-reads the whole prompt each time — measured at
# ~770 tokens/second, with `prompt_eval_count` reporting the full count on a
# verbatim repeat — so nothing about the prefix is free.
#
# Which is why the previous "the prefix cache covers it" reasoning was wrong,
# and wrong in the worst direction: history is the one part of the prompt that
# grows, so latency climbed steadily through a conversation and never came back
# down. Measured on the same machine, the same turn cost 0.83s with no history
# and 3.29s with 593 words of it.
#
# That turned out to be the model, not Ollama. qwen3.5 is a hybrid architecture
# whose recurrent layers cannot resume from a cached prefix, so it re-reads
# everything; conventional transformers (qwen3, gemma, gpt-oss) reuse it and
# read a repeated 2,400-token prompt in a fifth of a second rather than 1.6s.
# On those the budget costs little except on the turns the window moves.
#
# A word budget rather than a turn count, because turns are wildly uneven — one
# long answer costs as much as ten short ones, and a cap on pairs lets that
# through. Oldest whole exchanges are dropped first.
HISTORY_WORD_BUDGET = int(setting("HISTORY_WORDS", "260"))

# When the window has to move, how far down to trim it, as a share of the
# budget.
#
# Trimming to exactly the budget moved the start of the transcript on almost
# every turn — one exchange in, one exchange out — and a prefix cache is only
# as long as the part that has not changed. With the opening message different
# each time, everything after the system prompt was re-read on every turn even
# on a model that can cache. Cutting deeper when a cut is due holds the opening
# still for the next several turns, so those turns only pay for what is new.
TRIM_TO = 0.6


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
    # Timestamp of the message the model's window currently opens on. Held
    # across turns so the window only moves when it must (see `TRIM_TO`).
    _window_start = None
    # Turns write from the model's worker thread while the console's loop
    # records texts and asides: one lock round every change and save, or two
    # appends interleave and the turns stop alternating. (One for all: the
    # writes are tiny.)
    _lock = threading.RLock()

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

    def for_model(self, word_budget=None):
        """
        The messages as the model expects them: role and content only.
        Timestamps are ours, not the model's, and sending unknown keys to
        Ollama is asking for trouble.

        Trimmed to a word budget from the most recent end, dropping whole
        exchanges so the transcript never starts on an answer to a question the
        model cannot see. This is the single biggest lever on how quickly he
        answers — see `HISTORY_WORD_BUDGET`.
        """
        budget = HISTORY_WORD_BUDGET if word_budget is None else word_budget
        messages = [
            {
                "role": m["role"],
                "content": ("(by text) " if m.get("via") == "text" and m["role"] == "user" else "")
                           + ((m["content"] + " " + m["aside"]).strip()
                              if m.get("aside") else m["content"]),
            }
            for m in self.messages
        ]
        if budget is None or budget <= 0 or not messages:
            return messages

        # Keep the window where it was if everything since still fits, so the
        # prefix the model has already read stays byte-identical.
        start = self._held_start(messages, budget)
        if start is None:
            # Cut deeper than the budget, so the next few turns fit behind it.
            # A first window too: filled to the brim, it would have to move on
            # the very first reply, which is the one that is judged.
            start = self._trim_start(messages, int(budget * TRIM_TO))
            if start >= len(messages):
                # One exchange longer than the whole budget trims to nothing at
                # all. Losing the last thing said is far worse than going over
                # budget, so the most recent exchange is kept regardless.
                start = next((i for i in range(len(messages) - 1, -1, -1)
                              if messages[i]["role"] == "user"), len(messages) - 1)
        self._window_start = self.messages[start].get("at") if start < len(messages) else None
        return messages[start:]

    def _held_start(self, messages, budget):
        """The current window's opening index, if the window can stay put."""
        if self._window_start is None:
            return None
        for index, message in enumerate(self.messages):
            if message.get("at") == self._window_start:
                if message["role"] != "user":
                    return None
                used = sum(len(m["content"].split()) for m in messages[index:])
                return index if used <= budget else None
        return None

    @staticmethod
    def _trim_start(messages, budget):
        """Earliest index whose tail fits the budget, opening on a user turn."""
        start, used = len(messages), 0
        for index in range(len(messages) - 1, -1, -1):
            cost = len(messages[index]["content"].split())
            if used + cost > budget and start < len(messages):
                break
            start = index
            used += cost
        # Never open on an assistant turn: an answer with its question removed
        # reads as something he volunteered, and he will follow that example.
        while start < len(messages) and messages[start]["role"] == "assistant":
            start += 1
        return start

    def _is_marker(self, message, marker):
        """
        The placeholder a call's greeting answers. Flagged when written; older
        histories without the flag fall back to matching the text — which also
        matched him actually saying "Alfred?", and pruned that exchange.
        """
        if "marker" in message:
            return bool(message["marker"])
        flagged = any("marker" in m for m in self.messages)
        return not flagged and message["role"] == "user" and message["content"] == marker

    def last_greeting(self, marker):
        """The greeting from the most recent connection, or '' if there is none."""
        for index in range(len(self.messages) - 2, -1, -1):
            if (self.messages[index]["role"] == "user"
                    and self._is_marker(self.messages[index], marker)
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
        with self._lock:
            kept = []
            index = 0
            while index < len(self.messages):
                message = self.messages[index]
                is_pair = (message["role"] == "user"
                           and self._is_marker(message, marker)
                           and index + 1 < len(self.messages)
                           and self.messages[index + 1]["role"] == "assistant")
                if is_pair:
                    index += 2      # drop the placeholder and the greeting with it
                    continue
                kept.append(message)
                index += 1
            self.messages = kept

    def append(self, role, content):
        with self._lock:
            # What they did while they spoke stays with what they said — "[takes
            # a deep breath] Look, B." — so they know they just sighed. One cue a
            # reply at most (see session), so memory doesn't fill with sighs. What
            # the operator said is kept verbatim.
            if role == "assistant":
                content = delivery.remembered(content)
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
        with self._lock:
            if not text:
                return
            if self.messages and self.messages[-1]["role"] == "assistant":
                # Kept in its own field, not concatenated. Appending to the content
                # meant a second check-in stacked onto the first — "Still here.
                # Still here." — and once that was in the context he produced more
                # of it. One turn has at most one trailing aside; a newer one
                # replaces the older, because that is what actually happened.
                self.messages[-1]["aside"] = delivery.remembered(text)
                self.messages[-1]["at"] = time.time()
            else:
                self.append("assistant", text)
            self.save()

    def record_exchange(self, prompt, reply, via=None):
        """
        Store only the raw exchange — never the injected reference context.
        `via="text"` marks a text conversation, so a later call knows what was
        texted rather than said, and the message thread can show it.
        """
        with self._lock:
            self.append("user", prompt)
            self.append("assistant", reply)
            if via:
                self.messages[-1]["via"] = self.messages[-2]["via"] = via
            self.save()

    def amend_last_reply(self, text):
        """What they said turned out to belong somewhere else: the record says so."""
        with self._lock:
            if self.messages and self.messages[-1]["role"] == "assistant":
                self.messages[-1]["content"] = text
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
        with self._lock:
            del self.messages[:-MAX_HISTORY_MESSAGES]
            atomic_write(self.path, json.dumps(self.messages, indent=2))

    def clear(self):
        with self._lock:
            if self.path.exists():
                self.path.unlink()
            self.messages = []
