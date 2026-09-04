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
from .search import format_search_results, google_search, is_factual_lookup

_PRE_SEARCH_PHRASES = [
    "One moment.",
    "Just a moment.",
    "Stand by.",
    "Let me check.",
    "Give me a moment.",
]

# Stored in place of the operator's turn when a call connects, so history stays
# a well-formed alternation. Never shown, and superseded on the next call.
#
# Deliberately plain rather than bracketed: a "[link established]" sitting in
# the context taught the model that bracketed directives were part of the
# conversation, and it duly emitted [SEARCH: link established] — then reported
# back on an IT company of that name. Placeholders should look like something
# a person would say.
def _link_marker(contact):
    return f"{contact.name}?"

# Used only when two generations running have handed the operator his own words
# back. Deliberately in character rather than neutral filler.
_DEFLECTIONS = [
    "I asked first.",
    "That's rather the point of asking.",
    "Don't be difficult.",
    "Out with it.",
    "I'd rather hear it from you.",
    "You know I'll only ask twice.",
    "Go on, then.",
    "Something on your mind, or are we just making noises?",
]

_WIPE_COMMANDS = ["clear memory", "forget everything", "protocol zero", "wipe logs"]
_MEMORIZE_PREFIXES = ("remember that", "remember to", "note that", "don't forget")
_FORGET_PREFIXES = ("forget that", "forget about")

# Sentence-final punctuation followed by whitespace, which is enough of a
# boundary for speech without waiting on a full parse.
_BOUNDARY = ".!?"

# How a contact asks for a lookup. Keyword matching decides only whether the
# question *could* be one; whether to actually go and look is the character's
# call, which is the difference between a search box and a person.
# Two forms, and the difference matters.
#
# While tokens are still arriving the marker must be *complete* — closed by a
# bracket or a newline. Anchoring on end-of-buffer instead matched the half of
# it that had arrived so far: "[SEARCH: Way" became a search for "Way", which
# came back with a cycling route called King Alfred's Way.
_SEARCH_MARKER_COMPLETE = re.compile(r"\[\s*SEARCH\s*:\s*([^\]\n]+?)\s*(?:\]|\n)", re.I)

# Once generation has finished the buffer cannot grow, so a marker missing its
# closing bracket — or its opening one — can be read safely.
_SEARCH_MARKER = re.compile(r"\[?\s*SEARCH\s*:\s*([^\]\n]+?)\s*\]?\s*$", re.I | re.M)
# Returned by the streaming pass instead of a reply, to say "he wants to look
# something up first". Nothing has been spoken at that point.
_SEARCH_REQUESTED = object()
# The marker is short; if this much text arrives without one, he isn't asking.
_MARKER_WATCH_CHARS = 90

# Offered on *every* turn, not only when a keyword suggested a lookup. Gating
# this behind phrases like "look up" meant that asking "what do we have on
# Waylon Jones?" never even reached the decision — so instead of looking, or
# admitting he had no idea, he invented an answer and stated it as fact.


class ContactSession:
    """A live conversation with one contact. Construct, `boot()`, then `ask()`."""

    def __init__(self, contact):
        self.contact = contact
        self.history = History(contact.id)
        self.vault = Vault(contact.id)
        self.already_greeted = False
        # How many times he has talked over a reply this session.
        self.interruptions = 0
        # Phrases he has already been told he is repeating. Saying it twice is
        # observant; saying it every turn is a counter with a voice.
        self.remarked_on = set()
        self._last_deflection = None

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

        # Compared like with like. The loop check runs on the first sentence,
        # so it has to be measured against the first sentence of previous
        # replies — matching one sentence against whole multi-sentence replies
        # scores too low to ever fire, which let "You've said morning nine
        # times now… ten… eleven…" run indefinitely.
        recent = [guards.split_sentences(reply)[0]
                  for reply in self.history.recent_assistant(turns=12) if reply.strip()]

        buffer = ""          # tokens not yet forming a complete sentence
        held = []            # complete sentences that look like farewells
        spoken = []          # sentences actually emitted
        index = 0
        checked_opening = False
        can_search = self.contact.can_search
        self._pending_query = None

        def finalize(sentence):
            """Guard one sentence. Returns (emit, sentence) or (False, None)."""
            nonlocal index
            # Last line of defence: a marker that slipped past detection must
            # never be read aloud.
            sentence = _SEARCH_MARKER.sub("", sentence).strip()
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

            # Watched for the whole reply, not just its opening. He may write
            # the marker straight away, or say "I'll see what I can find" and
            # then ask — and the second is the more natural of the two, so it
            # has to work. Anything already spoken becomes the holding line.
            if can_search:
                match = _SEARCH_MARKER_COMPLETE.search(buffer)
                if match:
                    self._pending_query = match.group(1).strip()
                    self._spoke_before_search = bool(spoken)
                    return _SEARCH_REQUESTED
                # An opening bracket may be the start of one; wait for the rest
                # rather than speaking half a marker.
                if "[" in buffer and "]" not in buffer and len(buffer) < _MARKER_WATCH_CHARS:
                    continue

            while True:
                cut = self._sentence_end(buffer)
                if cut is None:
                    break
                sentence, buffer = buffer[:cut].strip(), buffer[cut:].lstrip()

                # Before anything is spoken, one chance to catch a loop — his
                # own, or the operator's words handed straight back.
                if not checked_opening:
                    checked_opening = True
                    if (guards.too_similar(sentence, recent)
                            or guards.parrots(sentence, prompt)):
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

        # Generation has finished, so an unclosed marker can now be read.
        if can_search:
            match = _SEARCH_MARKER.search(buffer)
            if match:
                self._pending_query = match.group(1).strip()
                self._spoke_before_search = bool(spoken)
                return _SEARCH_REQUESTED

        # Whatever is left in the buffer is the final sentence, unpunctuated.
        #
        # A short reply — "Hmm." — never reaches the sentence loop at all: the
        # boundary detector needs punctuation *followed by whitespace*, and
        # there is none at the end of a stream. So the loop and parrot checks
        # have to run here too, or the shortest replies, which are exactly the
        # ones most likely to be echoes, skip them entirely.
        tail = buffer.strip()
        if tail and not checked_opening and not spoken:
            checked_opening = True
            probe = _SEARCH_MARKER.sub("", tail).strip()
            if probe and (guards.too_similar(probe, recent)
                          or guards.parrots(probe, prompt)):
                return (yield from self._regenerate(payload, prompt, recent))

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
        """
        One retry with a stronger nudge, before any audio.

        The retry is checked too. Asked not to parrot, a model will happily
        parrot again — and an unchecked retry meant "You tell me." was answered
        with "You tell me." even after the guard had caught it. If the second
        attempt fails the same way, anything is better than the echo.
        """
        retry_payload = list(payload)
        retry_payload.append({
            "role": "user",
            "content": "[That was either a repeat of your own last line or an echo of "
                       "his. Say something genuinely different — new words, new angle. "
                       "Do not hand his own words back to him.]",
        })
        try:
            text = self._chat_once(retry_payload, temperature=0.95)
        except Exception:
            text = ""
        if not text or guards.parrots(text, prompt) or guards.too_similar(text, recent):
            text = self._deflection()
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
        # Read before pruning: this is the greeting about to be removed.
        marker = _link_marker(self.contact)
        previous = self.history.last_greeting(marker)
        payload = prompting.build_payload(
            self.contact, self.history.for_model(),
            prompting.boot_prompt(self.contact, returning,
                                  self.history.time_since_last(), previous),
        )

        greeting = "Online. I'm here when you're ready."
        try:
            generated = self._chat_once(payload)
            if generated:
                # Only the address guard, never the full stack: that one strips
                # greetings and sign-offs, which is precisely what this is.
                # Without it the opening line was the one utterance in the whole
                # session nothing checked — and it came back "welcome back, dear
                # boy", spoken *and* written into history as an example to
                # follow.
                greeting = guards.strip_forbidden_address(
                    generated, self.contact.forbidden_address) or generated
        except Exception as exc:
            yield events.notice(f"Cold start failed — using fallback. ({exc})", "warn")

        # Only the most recent connection belongs in the context.
        self.history.drop_prior_greetings(marker)

        # The 'user' side is a neutral placeholder, never shown on screen.
        self.history.append("user", marker)
        self.history.append("assistant", greeting)
        self.history.save()
        self.already_greeted = True

        for i, sentence in enumerate(guards.split_sentences(greeting)):
            if sentence.strip():
                yield events.sentence(i, sentence.strip())
        yield events.reply_end(greeting)
        yield events.state(events.IDLE)

    def check_in(self):
        """
        A turn generated by silence rather than by something being said.

        Deliberately not a canned "are you still there?" — that is the same
        every time, and the second time you hear it the illusion is finished.
        It goes through the model like any other turn, with the length of the
        silence as context, so what comes back varies and sometimes isn't a
        question at all.

        Not written to history: an unanswered check-in shouldn't become part
        of what he remembers, or the next reply is answering a ghost.
        """
        yield events.state(events.THINKING)
        yield events.reply_start()

        gap = self.history.time_since_last() or "a little while"
        instruction = (
            "[REFERENCE — context only]\n"
            f"He has said nothing for {gap}. The link is still open.\n"
            f"{prompting.SPEECH_CONSTRAINT}\n"
            "[END REFERENCE]\n\n"
            "Break the silence yourself, briefly — a few words at most. You might "
            "check he is still there, or say nothing of consequence at all, the way "
            "someone in the same room does. Do not ask what he needs, do not offer "
            "help, and do not start a new subject."
        )
        yield from self._speak_aside(instruction, temperature=0.9, cap=2)

    def _awareness(self, prompt, interrupted, confidence=1.0):
        """
        What a person on the other end would have registered about this turn,
        beyond its words. Kept short: a list of observations, not a briefing.
        """
        notes = []

        if confidence < 0.6:
            # Speech-to-text does not fail by going quiet — it fails by
            # producing a confident sentence nobody said, and answering that
            # sends the conversation somewhere it was never going. Better to
            # ask than to take it at face value.
            notes.append(
                "The audio was poor and this transcription is unreliable. If it does "
                "not follow from what you were talking about, do not take it at face "
                "value and do not change the subject to suit it — say you did not "
                "catch that and ask him to say it again."
            )
        elif confidence < 0.8:
            notes.append(
                "The audio was imperfect; this may not be exactly what he said. If it "
                "reads oddly, check what he meant rather than assuming."
            )

        repeats = guards.count_repeats(prompt, self.history.recent_user(turns=60))
        if repeats:
            # Only remark once. Announcing a running total every turn — "five
            # times now", "six times now" — is as mechanical as the repetition
            # it is complaining about. A person says it, and if it carries on
            # they stop counting and deal with whatever is behind it.
            key = re.sub(r"[^\w\s]", "", prompt.lower()).strip()
            if key in self.remarked_on:
                notes.append(
                    "He is still repeating himself. Do not mention it again — you have "
                    "already said so. Respond to whatever is actually behind it, or let "
                    "it lie."
                )
            elif repeats == 1:
                notes.append("He has said this to you before, earlier in the conversation.")
            else:
                self.remarked_on.add(key)
                notes.append(
                    f"He has now said this {repeats + 1} times. Say so once — plainly, "
                    "and without pretending you hadn't noticed the earlier ones."
                )

        if guards.in_distress(prompt):
            notes.append(
                "He has said something is genuinely wrong. Stop everything else and "
                "answer that — directly, without brightness, without changing the "
                "subject, and without looking anything up. Ask him what is going on, "
                "or say the one true thing you would say to him in the room."
            )

        if guards.is_urgent(prompt):
            notes.append(
                "He has said this is urgent. Treat it as urgent: do the thing he "
                "asked for in this reply — look it up if that is what it takes — "
                "rather than counselling him about pace or asking what else is on. "
                "If you genuinely cannot, say why in one line."
            )

        if interrupted:
            self.interruptions += 1
            if self.interruptions >= 3:
                notes.append(
                    f"He cut you off mid-sentence again — {self.interruptions} times now. "
                    "You are entitled to remark on it."
                )
            else:
                notes.append("He cut you off mid-sentence. Let it go and answer what he asked.")
        return notes

    def sign_off(self):
        """
        Close the call himself. Nobody stays on a dead line indefinitely, and a
        contact who would is a program with the receiver off the hook.
        """
        yield events.state(events.THINKING)
        yield events.reply_start()
        instruction = (
            "[REFERENCE — context only]\n"
            f"He has not answered for some time and you have already checked twice.\n"
            f"{prompting.SPEECH_CONSTRAINT}\n"
            "[END REFERENCE]\n\n"
            "Close the call yourself, in one short line. Not wounded, not fussing — "
            "he has clearly stepped away. Make it plain you will be here when he "
            "comes back."
        )
        yield from self._speak_aside(instruction, temperature=0.9, cap=2)

    def resume(self):
        """
        Come back after asking for a moment. Whatever he was doing is his own
        business; what matters is that he returns of his own accord rather than
        waiting to be prompted, which is the whole point of having said it.
        """
        yield events.state(events.THINKING)
        yield events.reply_start()
        instruction = (
            "[REFERENCE — context only]\n"
            "You asked him for a moment a short while ago, and you have taken it.\n"
            f"{prompting.SPEECH_CONSTRAINT}\n"
            "[END REFERENCE]\n\n"
            "Pick the thread back up in a line or two: you are back, and you answer "
            "or continue whatever you had paused for. Do not apologise at length "
            "and do not explain yourself unless he asks."
        )
        yield from self._speak_aside(instruction, temperature=0.85, cap=3)

    def _speak_aside(self, instruction, temperature, cap):
        """
        A turn the contact initiates rather than answers.

        It *is* remembered — appended to his previous turn, since nothing was
        said in between. A contact who asks "still with me?" and then cannot
        recall asking is not someone you are having a conversation with.
        """
        payload = prompting.build_payload(self.contact, self.history.for_model(), instruction)
        try:
            text = self._chat_once(payload, temperature=temperature)
        except Exception:
            yield events.state(events.IDLE)
            return
        text = guards.apply(text, "", cap, self.already_greeted,
                            self.contact.forbidden_address)
        for i, sentence in enumerate(guards.split_sentences(text)):
            if sentence.strip():
                yield events.sentence(i, sentence.strip())
        self.history.record_aside(text)
        yield events.reply_end(text)
        yield events.state(events.IDLE)

    def ask(self, prompt, interrupted=False, confidence=1.0):
        """Run one full turn."""
        prompt = (prompt or "").strip()
        if not prompt:
            return

        yield events.message("user", prompt)

        handled = yield from self._handle_command(prompt)
        if handled:
            return

        awareness = self._awareness(prompt, interrupted, confidence)
        vault_block = self.vault.as_block(prompt)

        # A plainly factual question is looked up before he is asked anything,
        # so he answers from what was found rather than from what he can
        # imagine. He is still free to be unimpressed by the result.
        search_context = ""
        if self.contact.can_search and is_factual_lookup(prompt):
            yield events.state(events.SEARCHING)
            holding = random.choice(_PRE_SEARCH_PHRASES)
            yield events.sentence(0, holding)
            yield events.reply_end(holding, interim=True)
            search_context = yield from self._run_search(prompt)

        user_turn = prompting.compose_user_turn(
            prompt, vault_block, search_context, awareness)
        payload = prompting.build_payload(self.contact, self.history.for_model(), user_turn)

        yield events.state(events.THINKING)
        yield events.reply_start()

        try:
            reply = yield from self._emit_sentences(payload, prompt)
        except Exception as exc:
            yield events.notice(f"Connection severed: {exc}", "error")
            yield events.state(events.IDLE)
            return

        # He asked to go and look. Nothing has been spoken yet — the marker is
        # caught before the first sentence is released — so the search happens
        # and he answers properly, rather than promising and moving on.
        if reply == _SEARCH_REQUESTED:
            query = self._pending_query or prompt
            yield events.state(events.SEARCHING)
            if not getattr(self, "_spoke_before_search", False):
                # He went straight to looking without saying anything, so give
                # him a line rather than leaving dead air over the search.
                holding = random.choice(_PRE_SEARCH_PHRASES)
                yield events.sentence(0, holding)
                yield events.reply_end(holding, interim=True)
            search_context = yield from self._run_search(query)

            user_turn = prompting.compose_user_turn(
                prompt, vault_block, search_context, awareness)
            payload = prompting.build_payload(
                self.contact, self.history.for_model(), user_turn)
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

    def _consider_search(self, prompt):
        """
        One short pass in which the contact decides what to do with a request
        that looks like a lookup. Returns a query if he chose to go and look,
        or None if he has already said his piece — in which case that reply is
        emitted here and the turn is over.
        """
        vault_block = self.vault.as_block(prompt)
        user_turn = prompting.compose_user_turn(prompt, vault_block)
        payload = prompting.build_payload(self.contact, self.history.for_model(), user_turn)

        yield events.state(events.THINKING)
        try:
            text = self._chat_once(payload)
        except Exception as exc:
            yield events.notice(f"Connection severed: {exc}", "error")
            yield events.state(events.IDLE)
            return None

        match = _SEARCH_MARKER.search(text)
        if match:
            query = match.group(1).strip()
            # A marker plus commentary means the commentary was never meant to
            # be heard; the lookup is the whole of the intent.
            return query or prompt

        reply = guards.apply(text, prompt, self.contact.max_reply_sentences,
                             self.already_greeted, self.contact.forbidden_address)
        yield events.reply_start()
        for i, sentence in enumerate(guards.split_sentences(reply)):
            if sentence.strip():
                yield events.sentence(i, sentence.strip())
        self.history.record_exchange(prompt, reply)
        yield events.reply_end(reply)
        yield events.state(events.IDLE)
        return None

    def _deflection(self):
        """
        A last resort when two attempts have both come back an echo. Varied,
        and never the one used last, so the fallback cannot itself become the
        repetition it exists to prevent.
        """
        options = [o for o in _DEFLECTIONS if o != self._last_deflection]
        choice = random.choice(options or _DEFLECTIONS)
        self._last_deflection = choice
        return choice

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
