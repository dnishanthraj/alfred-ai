"""
The headless conversation engine.

Everything that decides *what Alfred says* lives here; nothing that decides
*how it is shown or heard* does. `ask()` is a generator of events (see
`events.py`) so a terminal, a web console, or a phone client can all drive the
same conversation without duplicating the guard logic.

State is held on the instance rather than in module globals, so the engine can
be constructed, torn down, and tested without leaking between sessions.
"""
import difflib
import random
import re
import time

import ollama

from . import events, memory
from .config import (
    MAX_REPLY_SENTENCES,
    OLLAMA_MODEL,
    USER_NAME,
)
from .search import format_search_results, google_search, needs_search

_PRE_SEARCH_PHRASES = [
    "One moment.",
    "Just a moment.",
    "Stand by.",
    "Let me check.",
    "Give me a moment.",
]

# Sentences the assistant should never end on unless the user signalled they're
# leaving. Anchored so they only match an actual farewell — an earlier version
# used bare prefixes like `take care\b.*`, which silently deleted legitimate
# lines such as "Take care of the deployment first."
_SIGNOFF_PATTERNS = [
    r"sleep well\b.*",
    r"get some (rest|sleep)\b.*",
    r"rest up[.!]?$",
    r"rest easy[.!]?$",
    r"good ?night\b.*",
    r"take care[.!]?$",
    r"take care of yourself\b.*",
    r"catch up (on|with) (your )?sleep\b.*",
    r"turn in (early|for the night)\b.*",
    r"off to bed\b.*",
]

# Words from the user that DO license a sleep/goodbye sign-off.
_LEAVING_CUES = [
    "bye", "goodnight", "good night", "night", "sleeping", "going to bed",
    "off to bed", "i'm tired", "im tired", "heading off", "see you", "talk later",
    "going to sleep", "gonna sleep", "logging off",
]

# Greeting openings the assistant should not repeat once it has greeted.
_GREETING_PATTERNS = [
    r"good ?morning\b.*",
    r"good ?afternoon\b.*",
    r"good ?evening\b.*",
    r"morning again\b.*",
    r"morning\b[.,]?$",
    r"hello again\b.*",
    r"welcome back\b.*",
    r"back again\b.*",
]

_WIPE_COMMANDS = ["clear memory", "forget everything", "protocol zero", "wipe logs"]
_MEMORIZE_PREFIXES = ("remember that", "remember to", "note that", "don't forget")

# Replies shorter than this (in words) are exempt from the anti-repetition
# guard. The persona is deliberately terse — "Mm.", "Go on." are *meant* to
# recur, and flagging them as loops fought the Modelfile rather than helping it.
_REPETITION_MIN_WORDS = 4


def _split_sentences(text):
    return re.split(r'(?<=[.!?])\s+', text.strip())


def time_context():
    """
    Unambiguous 24-hour time string, e.g. "Saturday, 28 June 2026, 21:45 (evening)".
    The period label is factual only — the assistant must not infer activity from it.
    """
    now = time.localtime()
    hour = now.tm_hour
    if 5 <= hour < 12:
        period = "morning"
    elif 12 <= hour < 17:
        period = "afternoon"
    elif 17 <= hour < 21:
        period = "evening"
    else:
        period = "night"
    return time.strftime(f"%A, %d %B %Y, %H:%M ({period})", now)


def strip_regreeting(text):
    """Drop a leading re-greeting so the assistant doesn't say 'Good morning' twice."""
    sentences = _split_sentences(text)
    if sentences:
        first = sentences[0].strip().lower()
        if any(re.match(pat, first) for pat in _GREETING_PATTERNS):
            sentences.pop(0)
    cleaned = " ".join(sentences).strip()
    return cleaned if cleaned else text  # never return empty


def user_is_leaving(prompt):
    p = prompt.lower()
    return any(cue in p for cue in _LEAVING_CUES)


def strip_signoffs(text):
    """Remove trailing sleep/goodbye sentences the assistant tacked on uninvited."""
    sentences = _split_sentences(text)
    while sentences:
        last = sentences[-1].strip().lower()
        if any(re.match(pat, last) for pat in _SIGNOFF_PATTERNS):
            sentences.pop()
        else:
            break
    cleaned = " ".join(sentences).strip()
    return cleaned if cleaned else "Mm."


def cap_length(text, max_sentences=None):
    """
    Safety net against runaway word-salad, not a style tool. Only replies past
    the cap are trimmed; short and medium replies pass through untouched so the
    model still varies its own length.
    """
    if max_sentences is None:
        max_sentences = MAX_REPLY_SENTENCES
    sentences = _split_sentences(text)
    if len(sentences) <= max_sentences:
        return text
    trimmed = " ".join(sentences[:max_sentences]).strip()
    return trimmed if trimmed else text


def too_similar(candidate, recent_assistant_msgs, threshold=0.75):
    """
    True if the candidate closely resembles a recent reply, reuses the same
    opening words, or ends on the same trailing phrase — the signatures of a
    loop. Very short replies are exempt: terseness is the persona, not a bug.
    """
    cand = candidate.strip().lower()
    cand_words = cand.split()
    if len(cand_words) < _REPETITION_MIN_WORDS:
        return False

    cand_open = " ".join(cand_words[:4])
    cand_close = " ".join(cand_words[-5:])
    for prev in recent_assistant_msgs:
        prev_l = prev.strip().lower()
        prev_words = prev_l.split()
        if difflib.SequenceMatcher(None, cand, prev_l).ratio() >= threshold:
            return True
        if cand_open and cand_open == " ".join(prev_words[:4]):
            return True
        if len(cand_words) >= 5 and cand_close and cand_close == " ".join(prev_words[-5:]):
            return True
    return False


class AlfredCore:
    """
    One conversation. Construct it, `boot()` it, then feed it prompts via
    `ask()`. Both are generators — iterate them to drive a frontend.
    """

    def __init__(self, model=None):
        self.model = model or OLLAMA_MODEL
        self.history = memory.load_history()
        self.already_greeted = False

    # --- context assembly -------------------------------------------------

    def _context_block(self):
        """
        Per-turn dynamic context (time, memory vault), folded into the USER
        message. Never sent as role=system: that would override the Modelfile's
        SYSTEM prompt and wipe the assistant's personality.
        """
        parts = [
            f"Current time: {time_context()}\n"
            f"(Factual context only. Do not infer what {USER_NAME} has been doing, "
            f"is about to do, or should do based on this.)"
        ]
        vault = memory.load_core_memory()
        if vault:
            parts.append(
                "Stored facts (use to stay grounded; do not invent new ones; "
                "raise one only if it directly contradicts what he's saying):\n"
                f"{vault}"
            )
        return "\n\n".join(parts)

    def _compose_user_turn(self, prompt, search_context=""):
        """Wrap the prompt with fenced context. The actual message comes last."""
        context = self._context_block()
        if search_context:
            context += (
                "\n\nLive intel — retrieved via search. Open with a brief natural "
                "acknowledgment ('Found it.', 'Right, I've got something.') then answer "
                f"naturally. Never read results as a list:\n{search_context}"
            )
        if not context:
            return prompt
        return (
            "[REFERENCE — context only, do not speak any of this aloud]\n"
            f"{context}\n"
            "[END REFERENCE]\n\n"
            f"{prompt}"
        )

    # --- generation -------------------------------------------------------

    def _chat_once(self, messages_payload, **extra_options):
        """One blocking call to the model. Returns the reply text."""
        options = {"repeat_last_n": 256}
        options.update(extra_options)
        response = ollama.chat(
            model=self.model, messages=messages_payload,
            stream=False, options=options,
        )
        return response['message']['content'].strip()

    def _chat_stream(self, messages_payload, **extra_options):
        """
        Streaming call. Yields token events as they arrive and returns the
        accumulated text, so callers use `text = yield from self._chat_stream(...)`.
        """
        options = {"repeat_last_n": 256}
        options.update(extra_options)
        chunks = []
        for part in ollama.chat(
            model=self.model, messages=messages_payload,
            stream=True, options=options,
        ):
            piece = part.get('message', {}).get('content', '')
            if piece:
                chunks.append(piece)
                yield events.token(piece)
        return "".join(chunks).strip()

    def _apply_guards(self, text, prompt):
        """
        The deterministic post-processing the prompt can't reliably enforce.
        Order matters: sign-offs first (tail), then greeting (head), then the
        runaway cap.
        """
        if not user_is_leaving(prompt):
            text = strip_signoffs(text)
        if self.already_greeted:
            text = strip_regreeting(text)
        return cap_length(text)

    def _remember_turn(self, prompt, reply):
        """Store only the raw exchange — never the injected reference context."""
        self.history.append({'role': 'user', 'content': prompt})
        self.history.append({'role': 'assistant', 'content': reply})
        memory.save_history(self.history)

    # --- public API -------------------------------------------------------

    def boot(self):
        """
        Generate the opening line. Stored as a proper user/assistant pair: a
        history that starts with an orphaned assistant message leaves the model
        unsure who spoke last, and it hallucinates on the next exchange.
        """
        yield events.state(events.THINKING)

        now = time_context()
        if not self.history:
            greeting_prompt = (
                f"[REFERENCE — context only]\n"
                f"Time: {now}. Fresh session.\n"
                f"[END REFERENCE]\n\n"
                f"The link just came live. Greet him in one sentence — natural, warm, "
                f"brief. No mention of sleep, rest, code, work, or technology."
            )
        else:
            greeting_prompt = (
                f"[REFERENCE — context only]\n"
                f"Time: {now}.\n"
                f"[END REFERENCE]\n\n"
                f"The link is live again. One or two sentences. Acknowledge the reconnection "
                f"naturally. Reference the last session only if something is genuinely worth "
                f"noting. No mention of sleep, rest, or bed."
            )

        payload = list(self.history)
        payload.append({'role': 'user', 'content': greeting_prompt})

        greeting = "Online. I'm here when you're ready."
        try:
            generated = self._chat_once(payload)
            if generated:
                greeting = generated
        except Exception as exc:
            yield events.notice(f"Cold start failed — using fallback. ({exc})", "warn")

        # The 'user' side is a neutral placeholder, never shown on screen.
        self.history.append({'role': 'user', 'content': '[link established]'})
        self.history.append({'role': 'assistant', 'content': greeting})
        memory.save_history(self.history)
        self.already_greeted = True

        yield events.message("assistant", greeting)
        yield events.reply_end(greeting)
        yield events.state(events.IDLE)

    def ask(self, prompt):
        """Run one full turn. Yields events; the caller renders and speaks them."""
        prompt = (prompt or "").strip()
        if not prompt:
            return

        yield events.message("user", prompt)

        lowered = prompt.lower()

        if lowered in _WIPE_COMMANDS:
            self.history = memory.clear_history()
            reply = "Memory purged. We start fresh."
            yield events.message("assistant", reply)
            yield events.reply_end(reply)
            yield events.state(events.IDLE)
            return

        if lowered.startswith(_MEMORIZE_PREFIXES):
            memory.memorize(prompt)
            reply = "Noted. Stored to the vault."
            self._remember_turn(prompt, reply)
            yield events.message("assistant", reply)
            yield events.reply_end(reply)
            yield events.state(events.IDLE)
            return

        search_context = ""
        last_reply = (
            self.history[-1]['content']
            if self.history and self.history[-1]['role'] == 'assistant'
            else ""
        )
        if needs_search(prompt, last_alfred_msg=last_reply):
            yield events.state(events.SEARCHING)
            holding_line = random.choice(_PRE_SEARCH_PHRASES)
            yield events.message("assistant", holding_line)
            yield events.reply_end(holding_line)

            # A terse follow-up ("and the price?") is meaningless as a standalone
            # query — graft the last couple of user turns onto it for context.
            query = prompt
            if len(prompt.split()) <= 4:
                recent = [m['content'] for m in self.history[-6:] if m['role'] == 'user']
                if recent:
                    query = ' '.join(recent[-2:]) + ' ' + prompt
            try:
                results = google_search(query, num_results=5)
                if results:
                    search_context = format_search_results(results)
            except Exception as exc:
                yield events.notice(f"Search failed: {exc}", "warn")

        payload = list(self.history)
        payload.append({'role': 'user', 'content': self._compose_user_turn(prompt, search_context)})

        yield events.state(events.THINKING)
        yield events.reply_start()

        try:
            raw = yield from self._chat_stream(payload)
        except Exception as exc:
            yield events.notice(f"Connection severed: {exc}", "error")
            yield events.state(events.IDLE)
            return

        reply = self._apply_guards(raw, prompt)

        # If this echoes a recent reply, regenerate once with a stronger nudge.
        recent_replies = [m['content'] for m in self.history[-6:] if m['role'] == 'assistant']
        if too_similar(reply, recent_replies):
            retry_payload = list(payload)
            retry_payload.append({
                'role': 'user',
                'content': "[You just repeated yourself. Say something completely "
                           "different — new words, new angle. Do not reuse your last phrasing.]",
            })
            try:
                retry_text = self._chat_once(retry_payload, temperature=0.95)
                retry_text = self._apply_guards(retry_text, prompt)
                if retry_text and not too_similar(retry_text, recent_replies):
                    reply = retry_text
            except Exception:
                pass  # keep the original reply rather than failing the turn

        self._remember_turn(prompt, reply)
        yield events.reply_end(reply)
        yield events.state(events.IDLE)
