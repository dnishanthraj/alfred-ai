"""
One conversation with one contact.

`ask()` is a generator of events (see `wayne.events`) so a terminal, a web
console, or a phone client can all drive the same conversation without
duplicating the guard logic.

Sentences are emitted the moment they are complete rather than at the end of
generation, so synthesis for the opening line starts while the model is still
writing the rest. That is most of the difference between a reply that lands a
beat after you stop talking and one that lands four seconds later.

Streaming speech constrains which guards can run, and each is handled in the
only way that actually works mid-stream:

  * re-greeting  — decidable on sentence one, applied before it is emitted.
  * sign-offs    — only strip when *trailing*, which isn't knowable yet, so a
                   sentence that looks like a farewell is buffered and either
                   flushed (more content followed) or dropped (it was last).
  * length cap   — decidable by count.
  * repetition   — needs the whole reply, which is too late once audio is out.
                   The opening-words check runs on sentence one, before
                   anything is spoken, and catches the loop signature that
                   matters; the whole-text check is skipped when streaming.
"""
import random
import re

import ollama

from .. import config, events
from ..memory import History, Vault
from . import guards, prompting
from .search import format_search_results, google_search, needs_search

_PRE_SEARCH_PHRASES = [
    "One moment.",
    "Just a moment.",
    "Stand by.",
    "Let me check.",
    "Give me a moment.",
]

_WIPE_COMMANDS = ["clear memory", "forget everything", "protocol zero", "wipe logs"]
_MEMORIZE_PREFIXES = ("remember that", "remember to", "note that", "don't forget")
_FORGET_PREFIXES = ("forget that", "forget about")

# Sentence-final punctuation followed by whitespace, which is enough of a
# boundary for speech without waiting on a full parse.
_BOUNDARY = ".!?"


class ContactSession:
    """A live conversation with one contact. Construct, `boot()`, then `ask()`."""

    def __init__(self, contact):
        self.contact = contact
        self.history = History(contact.id)
        self.vault = Vault(contact.id)
        self.already_greeted = False

    # --- model calls ------------------------------------------------------

    def _options(self, **overrides):
        options = dict(self.contact.options)
        options.update(overrides)
        return options

    def _extra(self):
        """Top-level chat arguments that only apply to some models."""
        extra = {"keep_alive": config.MODEL_KEEP_ALIVE}
        if self.contact.think is not None:
            extra["think"] = self.contact.think
        return extra

    def _chat_once(self, payload, **overrides):
        response = ollama.chat(
            model=self.contact.model, messages=payload,
            stream=False, options=self._options(**overrides), **self._extra(),
        )
        return response["message"]["content"].strip()

    def _stream(self, payload, **overrides):
        for part in ollama.chat(
            model=self.contact.model, messages=payload,
            stream=True, options=self._options(**overrides), **self._extra(),
        ):
            piece = part.get("message", {}).get("content", "")
            if piece:
                yield piece

    # --- sentence assembly ------------------------------------------------

    def _emit_sentences(self, payload, prompt):
        """
        Stream a reply, yielding guarded sentence events as they complete and
        returning the full spoken text.
        """
        max_sentences = self.contact.max_reply_sentences
        leaving = guards.user_is_leaving(prompt)
        recent = self.history.recent_assistant()

        buffer = ""          # tokens not yet forming a complete sentence
        held = []            # complete sentences that look like farewells
        spoken = []          # sentences actually emitted
        index = 0
        checked_opening = False

        def finalize(sentence):
            """Guard one sentence. Returns (emit, sentence) or (False, None)."""
            nonlocal index
            text = guards.strip_forbidden_address(
                sentence.strip(), self.contact.forbidden_address)
            if not text:
                return False, None
            if index == 0 and not spoken and self.already_greeted:
                text = guards.strip_regreeting(text).strip()
                if not text:
                    return False, None
            return True, text

        for piece in self._stream(payload):
            buffer += piece

            while True:
                cut = self._sentence_end(buffer)
                if cut is None:
                    break
                sentence, buffer = buffer[:cut].strip(), buffer[cut:].lstrip()

                # Before anything is spoken, one chance to catch a loop.
                if not checked_opening:
                    checked_opening = True
                    if guards.too_similar(sentence, recent):
                        return (yield from self._regenerate(payload, prompt, recent))

                emit, text = finalize(sentence)
                if not emit:
                    continue

                if not leaving and self._is_signoff(text):
                    held.append(text)      # might be trailing; decide later
                    continue

                # Real content arrived, so anything held wasn't a farewell.
                for pending in held:
                    if len(spoken) >= max_sentences:
                        break
                    spoken.append(pending)
                    yield events.sentence(index, pending)
                    index += 1
                held = []

                if len(spoken) >= max_sentences:
                    continue
                spoken.append(text)
                yield events.sentence(index, text)
                index += 1

        # Whatever is left in the buffer is the final sentence, unpunctuated.
        tail = buffer.strip()
        if tail and not (not leaving and self._is_signoff(tail)):
            emit, text = finalize(tail)
            if emit and len(spoken) < max_sentences:
                spoken.append(text)
                yield events.sentence(index, text)

        if not spoken:
            fallback = "Mm."
            yield events.sentence(0, fallback)
            spoken = [fallback]

        return " ".join(spoken)

    @staticmethod
    def _sentence_end(text):
        """
        Index just past the first sentence boundary, or None. Requires trailing
        whitespace so a decimal or an abbreviation mid-number isn't a boundary.
        """
        for i, char in enumerate(text):
            if char in _BOUNDARY and i + 1 < len(text) and text[i + 1].isspace():
                return i + 1
        return None

    @staticmethod
    def _is_signoff(sentence):
        lowered = sentence.strip().lower()
        return any(re.match(p, lowered) for p in guards.SIGNOFF_PATTERNS)

    def _regenerate(self, payload, prompt, recent):
        """One non-streaming retry with a stronger nudge, before any audio."""
        retry_payload = list(payload)
        retry_payload.append({
            "role": "user",
            "content": "[You just repeated yourself. Say something completely "
                       "different — new words, new angle. Do not reuse your last phrasing.]",
        })
        try:
            text = self._chat_once(retry_payload, temperature=0.95)
        except Exception:
            text = ""
        if not text:
            text = "Mm."
        text = guards.apply(text, prompt, self.contact.max_reply_sentences,
                            self.already_greeted, self.contact.forbidden_address)
        for i, sentence in enumerate(guards.split_sentences(text)):
            if sentence.strip():
                yield events.sentence(i, sentence.strip())
        return text

    # --- turns ------------------------------------------------------------

    def boot(self):
        """
        Generate the opening line. Stored as a proper user/assistant pair: a
        history starting with an orphaned assistant message leaves the model
        unsure who spoke last, and it hallucinates on the next exchange.
        """
        yield events.state(events.THINKING)

        returning = bool(self.history)
        payload = prompting.build_payload(
            self.contact, self.history.for_model(),
            prompting.boot_prompt(self.contact, returning, self.history.time_since_last()),
        )

        greeting = "Online. I'm here when you're ready."
        try:
            generated = self._chat_once(payload)
            if generated:
                greeting = generated
        except Exception as exc:
            yield events.notice(f"Cold start failed — using fallback. ({exc})", "warn")

        # The 'user' side is a neutral placeholder, never shown on screen.
        self.history.append("user", "[link established]")
        self.history.append("assistant", greeting)
        self.history.save()
        self.already_greeted = True

        for i, sentence in enumerate(guards.split_sentences(greeting)):
            if sentence.strip():
                yield events.sentence(i, sentence.strip())
        yield events.reply_end(greeting)
        yield events.state(events.IDLE)

    def ask(self, prompt):
        """Run one full turn."""
        prompt = (prompt or "").strip()
        if not prompt:
            return

        yield events.message("user", prompt)

        handled = yield from self._handle_command(prompt)
        if handled:
            return

        search_context = ""
        if self.contact.can_search and needs_search(prompt, self.history.last_assistant()):
            yield events.state(events.SEARCHING)
            holding = random.choice(_PRE_SEARCH_PHRASES)
            yield events.sentence(0, holding)
            yield events.reply_end(holding, interim=True)
            search_context = yield from self._run_search(prompt)

        vault_block = self.vault.as_block(prompt)
        user_turn = prompting.compose_user_turn(prompt, vault_block, search_context)
        payload = prompting.build_payload(self.contact, self.history.for_model(), user_turn)

        yield events.state(events.THINKING)
        yield events.reply_start()

        try:
            reply = yield from self._emit_sentences(payload, prompt)
        except Exception as exc:
            yield events.notice(f"Connection severed: {exc}", "error")
            yield events.state(events.IDLE)
            return

        self.history.record_exchange(prompt, reply)
        yield events.reply_end(reply)
        yield events.state(events.IDLE)

    def _run_search(self, prompt):
        # A terse follow-up ("and the price?") is meaningless as a standalone
        # query — graft the last couple of user turns onto it.
        query = prompt
        if len(prompt.split()) <= 4:
            recent = self.history.recent_user()
            if recent:
                query = " ".join(recent[-2:]) + " " + prompt
        try:
            results = google_search(query, num_results=5)
        except Exception as exc:
            yield events.notice(f"Search failed: {exc}", "warn")
            return ""
        if not results:
            return ""
        yield events.sources([
            {"title": r.get("title", ""), "url": r.get("url", "")} for r in results
        ])
        return format_search_results(results)

    def _handle_command(self, prompt):
        """Memory commands, handled in code rather than left to the model."""
        lowered = prompt.lower()

        if lowered in _WIPE_COMMANDS:
            self.history.clear()
            self.vault.clear()
            reply = "Memory purged. We start fresh."
            yield events.sentence(0, reply)
            yield events.reply_end(reply)
            yield events.state(events.IDLE)
            return True

        if lowered.startswith(_FORGET_PREFIXES):
            needle = ""
            for prefix in _FORGET_PREFIXES:
                if lowered.startswith(prefix):
                    needle = prompt[len(prefix):].strip(" .")
                    break
            removed = self.vault.forget(needle) if needle else []
            reply = ("Forgotten." if removed
                     else "Nothing in the vault matches that.")
            self.history.record_exchange(prompt, reply)
            yield events.sentence(0, reply)
            yield events.reply_end(reply)
            yield events.state(events.IDLE)
            return True

        if lowered.startswith(_MEMORIZE_PREFIXES):
            self.vault.memorize(prompt)
            reply = "Noted. Stored to the vault."
            self.history.record_exchange(prompt, reply)
            yield events.sentence(0, reply)
            yield events.reply_end(reply)
            yield events.state(events.IDLE)
            return True

        return False
