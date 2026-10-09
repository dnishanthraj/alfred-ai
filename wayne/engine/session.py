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
import concurrent.futures
import random
import re
from collections import deque

import ollama

from .. import config, delivery, events
from ..memory import History, Story, Vault
from . import grapevine, guards, prompting, world
from .search import format_search_results, google_search, is_factual_lookup

# Searches run on a worker so the holding line can be written meanwhile.
_SEARCHES = concurrent.futures.ThreadPoolExecutor(max_workers=2)

# Only when a generated holding line fails its checks (see `_holding_line`).
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

# The floor handed to him without a question mark.
_INVITATION = re.compile(
    r"\b(go on|do tell|tell me|out with it|i'?m (all ears|listening)|let'?s hear it|"
    r"spit it out|what happened|talk to me|i'?m here)\b", re.I)

# A promise to go and look. After a search has already run this turn it is
# untrue — "I'll have to look it up for you", said over the results — and the
# console reads "give me a moment" as him stepping away, so it also arms a
# timer for him to come back to a search that already happened. Instructed not
# to, he still did it; so it is caught here.
_PROMISE_TO_LOOK = re.compile(
    r"\b(i'?ll|let me|i will|i shall|i'?ll have to|i need to|give me a (moment|second|minute))"
    r"\b[^.?!]*\b(look|check|find|search|dig|moment|second|minute)\b", re.I)

# Corners of his own life for an unprompted remark to come from. Chosen in code,
# so that a silence on Tuesday is not the boiler again; the words are still his.
# Used when a profile does not list its own (see `Contact.own_life`).
_OWN_CORNERS = [
    "something you're doing right now", "something you've been reading or watching",
    "something you heard today", "a memory of your own",
    "something you've been mulling over about him, from this conversation only",
    "the weather outside your own window",
]

# What he does with a silence, by kind (see `ContactSession.check_in`).
_SILENCE_MOVES = {
    "prod": ("You left the floor to him and he hasn't taken it. Nudge him back to "
             "it — refer to what you asked or what he was about to tell you, lightly "
             "and in different words. Not 'still there?'"),
    "own": ("Say something from your own end, unprompted — a thought you've just "
            "had, what you're doing, something you're reading or listening to, an "
            "opinion you've been chewing on. Small and natural, the way someone on "
            "an open line mentions what's in front of them. One or two sentences."),
    "thread": ("Go back to something from earlier in this conversation that deserves "
               "another word — something he said, or left unfinished. Only what is "
               "actually in the transcript; invent nothing about him."),
    "world": ("Mention the item from your terminal above, in your own words and with "
              "your view on it — something you've just noticed. Only what it says."),
    "check": ("It has been a good while. Ask, lightly and in your own way, whether "
              "he's still there — without assuming he has gone."),
}

_WIPE_COMMANDS = ["clear memory", "forget everything", "protocol zero", "wipe logs"]
_MEMORIZE_PREFIXES = ("remember that", "remember to", "note that", "don't forget")
_FORGET_PREFIXES = ("forget that", "forget about")

# "Remember that night we got caught in the rain?" begins exactly like "remember
# that I'm allergic to nuts", and was filed in the vault as a fact — answered
# with "Noted. Stored to the vault." A question, or a reach for a shared moment,
# is conversation, not an instruction.
_REMINISCING = re.compile(
    r"^(remember|don'?t forget) (that|when|the) "
    r"(time|night|day|evening|morning|weekend|summer|winter|year|trip)\b")


# A reach for a shared memory: "you remember…", "remember when…", "that time we…".
_SHARED_PAST = re.compile(
    r"\b(you remember|(do|don'?t|didn'?t) you remember|you don'?t remember|"
    r"remember (when|that|the|our|my)|do you recall|that time (we|you|i)|"
    r"the (night|day|time|weekend|trip) (we|you|i))\b", re.I)


# Words that appear in a reach for the past without being what it is about.
_COMMON = {"remember", "recall", "that", "when", "time", "night", "weekend", "with",
           "what", "were", "right", "there", "they", "then", "this", "your", "have",
           "trip", "back", "those", "about", "after", "before"}


# Building the story of the game he plays as Bruce (see wayne.memory.Story).
_STORY_REMEMBER = re.compile(
    r"^\s*(?:(?:for|in) (?:our|the) (?:story|game)[,:]?\s*(?:remember|note)?(?:\s+that)?|"
    r"remember (?:for|in) (?:our|the) (?:story|game)(?:\s+that)?|story note:?)\s*[,:]?\s*(.+)$", re.I)
_STORY_FORGET = re.compile(
    r"^\s*(?:forget (?:from|in) (?:our|the) (?:story|game)(?:\s+that)?|"
    r"(?:for|in) (?:our|the) (?:story|game)[,:]?\s*forget(?:\s+that)?)\s*[,:]?\s*(.+)$", re.I)
_STORY_RESET = re.compile(r"^\s*(reset|clear|wipe|start over) (our|the) (story|game)\b", re.I)
# Gotham's words — only to keep real-life rules from contradicting the game.
_GAME_WORDS = re.compile(
    r"\b(bruce|wayne|batman|bats|gotham|cave|cowl|batmobile|batwing|robin|nightwing|"
    r"red hood|oracle|jason|dick|tim drake|damian|barbara|cassandra|stephanie|selina|"
    r"catwoman|gordon|joker|penguin|riddler|arkham|our son|our daughter)\b", re.I)

_BARE_OPINION = re.compile(
    r"^\s*(and )?(so )?(what do you think|what d'?you reckon|thoughts|your (view|take)|"
    r"and you|what about you)\s*\??\s*$", re.I)
_DANGER = re.compile(
    r"\b(drink|drunk|pints?|beers?|wine|had a few|tipsy|over the limit)\b.*\b(driv\w*|behind the wheel)\b|"
    r"\b(driv\w*|behind the wheel)\b.*\b(drink|drunk|pints?|beers?|wine|had a few|tipsy)\b", re.I)
_WEATHERISH = re.compile(r"\b(weather|forecast|rain(ing)?|umbrella)\b", re.I)
# Short follow-ups that point back at the previous turn ("and the price?").
_REFERS_BACK = re.compile(r"^\s*(and|what about|how about)\b|\b(it|that|this|he|she|they|them|"
                          r"his|her|their|there|then)\b", re.I)


def _echoes_back(prompt, last):
    """All but at most one of his words came from your last reply."""
    words, said = _plain_words(prompt), set(_plain_words(last))
    return bool(words) and sum(w in said for w in words) >= max(1, len(words) - 1)


def _plain_words(text):
    return re.findall(r"[a-z']+", delivery.clean(text or "").lower())


def _is_instruction(lowered):
    return not lowered.rstrip().endswith("?") and not _REMINISCING.match(lowered)

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
        self.story = Story(contact.id)
        self.already_greeted = False
        # How many times he has talked over a reply this session.
        self.interruptions = 0
        # Phrases he has already been told he is repeating. Saying it twice is
        # observant; saying it every turn is a counter with a voice.
        self.remarked_on = set()
        self._last_deflection = None
        # Check-ins and sign-offs he has already made. `record_aside` keeps only
        # the newest against his last turn — deliberately, so they don't stack —
        # which means by the third check-in the model can see only the second and
        # cheerfully writes a third in the same shape. "Still drawing breath?"
        # followed by "Still breathing yet?" is a machine with two phrasings.
        self._recent_asides = deque(maxlen=6)
        self._looked = False   # a search has run this turn
        self._last_call = None  # how the previous call ended (see call_ended)
        self.call_start = len(self.history.messages)
        # On a call with others (see wayne.engine.party): the call, and what
        # this contact has heard said since they last spoke.
        self.call = None
        self.heard = []
        # Silences broken since he last spoke, and the last way one was broken.
        self._silences = 0
        self._last_silence_move = None

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

    def warm(self):
        """Load the model and read the stable prefix, generating one token."""
        payload = prompting.build_payload(self.contact, self.history.for_model(), ".")
        ollama.chat(model=self.contact.model, messages=payload, stream=False,
                    options=self._options(num_predict=1), **self._extra())

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

        # Compared like with like: sentence against sentence. Matching one
        # sentence against whole multi-sentence replies scores too low to ever
        # fire, which let "You've said morning nine times now… ten… eleven…"
        # run indefinitely.
        #
        # Every sentence of each recent reply, not just its opening. Comparing
        # against openings alone meant a line could only be caught if he had
        # once used it to *start* a reply — so an observation made in passing
        # came back almost verbatim two turns later, in full, and nothing
        # noticed. What matters is whether he has said this before, not where in
        # the reply he happened to say it.
        recent = [
            sentence
            for reply in self.history.recent_assistant(turns=12) if reply.strip()
            for sentence in guards.split_sentences(reply) if sentence.strip()
        ]

        buffer = ""          # tokens not yet forming a complete sentence
        held = []            # complete sentences that look like farewells
        spoken = self._spoken = []   # sentences actually emitted
        capped = False       # the cap was reached; nothing further is wanted
        dropped_presence = 0  # sentences discarded for putting him in the room
        index = 0
        checked_opening = False
        can_search = self.contact.can_search
        self._pending_query = None
        self._cue_spent = False

        def finalize(sentence):
            """Guard one sentence. Returns (emit, sentence) or (False, None)."""
            nonlocal index, dropped_presence
            # Last line of defence: a marker that slipped past detection must
            # never be read aloud.
            sentence = _SEARCH_MARKER.sub("", sentence).strip()
            text = guards.strip_forbidden_address(
                sentence.strip(), self.contact.forbidden_address)
            # On a call, a line written for somebody else ("Lucius: Indeed.")
            # is dropped, and his own name as a speaker label is stripped.
            if self.call:
                text = self.call.own_words(self, text)
                if not text:
                    return False, None
            # A cue with no words after it — "Of course. [sighs]" — has
            # nothing to say on screen and nothing to attach to in the voice.
            if not delivery.clean(text):
                return False, None
            # He is on a voice link, not in the room. Checked here as well as in
            # `guards.apply`, because this is the streaming path — which is the
            # path nearly every reply actually takes, and it was applying only
            # the address and greeting guards. Dropping the sentence beats
            # regenerating: the staging almost always arrives inside an
            # otherwise good answer, one clause of three.
            # One cue a reply. Offered a voice that can sigh, he sighed at the
            # start of nearly every sentence.
            if delivery.voiced(text) != delivery.clean(text):
                if self._cue_spent:
                    text = delivery.clean(text)
                self._cue_spent = True
            if self._looked and _PROMISE_TO_LOOK.search(delivery.clean(text)):
                dropped_presence += 1
                return False, None
            if guards.presumes_presence(delivery.clean(text)):
                dropped_presence += 1
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
                    capped = True
                    break
                spoken.append(text)
                yield events.sentence(index, text)
                index += 1
            if capped:
                # Stop reading, which closes the stream and stops the model.
                # Reading on to the end of a reply that will be cut anyway cost
                # the full generation time, during which the next turn waited.
                break

        if capped:
            return " ".join(spoken)

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

        if not spoken and dropped_presence:
            # He said something, and all of it was staging. Falling through to
            # the neutral acknowledgement below would answer a real question
            # with "Mm." — which is how a guard turns into the bug it was
            # written to prevent. One more attempt is cheaper than that.
            return (yield from self._regenerate(payload, prompt, recent))

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

    def call_ended(self, how):
        """
        How the last call ended, for the next one to know: "switched" when he
        rang off mid-call to ring someone else. A person notices being cut off
        for another line; the next greeting can say so, once.
        """
        self._last_call = how

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
        instruction = prompting.boot_prompt(self.contact, returning,
                                            self.history.time_since_last(), previous)
        if self._last_call == "switched":
            instruction += ("\n(Last time, he cut your call short to ring someone else. "
                            "You may remark on it, once and lightly — or not.)")
        self._last_call = None
        payload = prompting.build_payload(self.contact, self.history.for_model(), instruction)

        greeting = "Online. I'm here when you're ready."
        try:
            # Two attempts. The opening line is generated with no conversation
            # behind it, which is exactly when the model furnishes some — "pull
            # up a chair before that look on your face worries me" was the
            # greeting, in full, from a man on the other end of a phone line.
            # If the guards empty it, the whole line was staging and there is
            # nothing to salvage; asking again costs less than opening the call
            # on the one utterance that gets remembered as an example to follow.
            for attempt in range(2):
                generated = self._chat_once(
                    payload, temperature=0.85 + 0.15 * attempt)
                if not generated:
                    continue
                # The address guard, then presence — but never the full stack,
                # which strips greetings and sign-offs, and this is a greeting.
                cleaned = guards.strip_forbidden_address(
                    generated, self.contact.forbidden_address)
                cleaned = guards.strip_presence(cleaned).strip()
                if cleaned:
                    greeting = cleaned
                    break
        except Exception as exc:
            yield events.notice(f"Cold start failed — using fallback. ({exc})", "warn")

        # Only the most recent connection belongs in the context.
        self.history.drop_prior_greetings(marker)
        # Where this call begins in the history, for the grapevine at hang-up.
        self.call_start = len(self.history.messages)

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

        What a person does with a silence depends on what came before it, so
        this does not always do the same thing:

          * He asked something and got nothing — a question left hanging is
            the one silence people do not leave alone. Prod once, differently.
          * The first lull on an open line — nobody is obliged to talk, and
            "are you still there?" every time is a machine polling. He starts
            something instead: a thought from his own end, a return to
            something said earlier, or something from the feeds worth a remark.
          * A long second silence — now it is reasonable to ask.

        The client decides *when* (see IDLE_WINDOWS in app.js); this decides
        what. The shape is picked here, in code, not left to the model: told
        "say something", it asks if you are there, every time.
        """
        yield events.state(events.THINKING)
        yield events.reply_start()

        self._silences += 1
        kind = self._silence_kind()
        gap = self.history.time_since_last() or "a little while"
        context = [f"He has said nothing for {gap}. The link is still open.",
                   # He has no way of knowing whether the operator stepped out,
                   # is thinking, or simply did not hear. Left to itself the
                   # model decides — "you've gone quiet on me" — and states a
                   # conclusion it cannot have reached.
                   "You cannot see him and have no idea whether he is busy, "
                   "thinking or away. Do not conclude which."]
        move = _SILENCE_MOVES[kind]
        if kind == "world":
            # One item, picked here. Given the whole feed he raised the top
            # headline every time.
            items = [item.strip() for line in world.snapshot()
                     for item in line.split(": ", 1)[-1].split(" | ") if item.strip()]
            context.append("From your terminal: " + random.choice(items))
        elif kind == "own":
            move += f" Let it come from {random.choice(self.contact.own_life or _OWN_CORNERS)}."
        context.append(prompting.SPEECH_CONSTRAINT)
        instruction = (
            "[REFERENCE — context only]\n" + "\n".join(context) + "\n[END REFERENCE]\n\n"
            + move + " Don't ask what he needs and don't offer help."
        )
        yield from self._speak_aside(instruction, temperature=0.9, cap=3)

    def _silence_kind(self):
        """Which way to break this silence. See `check_in`."""
        last = self.history.last_assistant()
        # "Do go on... I'm all ears" leaves the floor to him as surely as a
        # question does, and was met with a change of subject.
        asked = last.rstrip().endswith("?") or bool(_INVITATION.search(last))
        if self._silences > 1:
            return "check"
        if asked:
            return "prod"
        moves = ["own", "own"]
        if len(self.history.recent_user(turns=12)) >= 2:
            moves.append("thread")
        if world.snapshot():
            moves.append("world")
        # Never the same move twice running, across calls of the same session.
        choices = [m for m in moves if m != self._last_silence_move] or moves
        self._last_silence_move = random.choice(choices)
        return self._last_silence_move

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

        if _SHARED_PAST.search(prompt) and self._in_the_game(prompt):
            # "You don't remember our son, Randy?" — said as Bruce — got "I
            # don't have a son", because the rule against inventing his real
            # past fired inside the game too. In the game his lead is the truth.
            notes.append(
                "He's reaching for something in the game you play. It's true in your "
                "story — go with it and build on it; don't deny it or ask if it happened.")
        elif _SHARED_PAST.search(prompt) and not self._on_record(prompt):
            # Asked "you remember that weekend in Cornwall?", he described the
            # leaking roof in Polperro — a whole shared past, invented on the
            # spot, which is the one thing that makes everything else he says
            # untrustworthy. The standing rule was not enough; said about this
            # turn specifically, it is.
            notes.append(
                "He is referring to something you supposedly shared. It is not in this "
                "conversation or in the stored facts, so you do not remember it. Say so "
                "plainly and ask him to tell you — do not supply a single detail."
            )

        if guards.user_is_leaving(prompt):
            # "Goodnight my friend" got a bare "Goodnight." and nothing else,
            # however the character was described. Said on the turn, it holds.
            notes.append("He's saying goodbye, and the call ends after your reply. "
                         "Send him off warmly, with a little of you in it — a line or "
                         "two, not a bare goodbye.")

        if _DANGER.search(prompt):
            # Half the time "I'm fine to drive, it was only three pints" got a
            # quip and no refusal. This is the one moment wit is wrong.
            notes.append("He is about to do something that could kill him. Refuse — "
                         "at once, plainly, in command. No jokes. Then the fear "
                         "underneath it, in a few words.")

        last = self.history.last_assistant()
        if _BARE_OPINION.match(prompt) and last:
            # "What do you think?" after the headlines got "About what,
            # exactly?" — the most recent subject is the obvious referent.
            notes.append("He's asking your view on what you were just talking about. "
                         "Give it.")
        elif (prompt.rstrip().endswith("?") and len(prompt.split()) <= 4 and last
              and _echoes_back(prompt, last)):
            # "off with me?", "good?" — his own words handed back as a question.
            notes.append("He's turned your own words back on you as a question — "
                         "teasing, or genuinely asking. Answer plainly and warmly; "
                         "if he's playing, play back. Never cryptic.")
        clipped = [r for r in self.history.recent_assistant(turns=4) if r.strip()][-2:]
        if len(clipped) == 2 and all(len(_plain_words(r)) <= 2 for r in clipped):
            notes.append("Your last few replies have been a word or two. Open up a "
                         "little — say something real.")

        if (_WEATHERISH.search(prompt) and not config.LOCATION
                and not re.search(r"\b(in|at|for) [A-Z]", prompt)):
            # Telling him "you don't know where he is" made him ask a man whose
            # town is in his notes. He often does know; when he doesn't, asking
            # is right.
            notes.append("He asked about the weather without saying where. If you "
                         "know where he is, look it up for there with [SEARCH: ...]; "
                         "if you don't, ask him where — briefly.")

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

    def _in_the_game(self, prompt):
        """
        Whether this turn is part of the game: Gotham's words in it or in what
        he said just before, or something already in the story. Used only to
        stop the real-life grounding rules contradicting the game — the
        character decides for itself which world it's in.
        """
        recent = " ".join(self.history.recent_user(turns=6)[-3:] + [prompt])
        return bool(_GAME_WORDS.search(recent)) or self.story.mentions(prompt)

    def _on_record(self, prompt):
        """
        Whether what he is reaching for is anywhere he could know it from: a
        stored fact, or something said earlier in this conversation.
        """
        if self.vault.mentions(prompt):
            return True
        terms = {w for w in re.findall(r"[a-z']{4,}", prompt.lower())} - _COMMON
        said = " ".join(m["content"] for m in self.history.messages).lower()
        return any(re.search(rf"\b{re.escape(t)}\b", said) for t in terms)

    def sign_off(self):
        """
        Close the call himself. Nobody stays on a dead line indefinitely, and a
        contact who would is a program with the receiver off the hook.
        """
        yield events.state(events.THINKING)
        yield events.reply_start()
        instruction = (
            "[REFERENCE — context only]\n"
            f"He has not answered for some time, and you have tried to draw him out.\n"
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

    def say(self, instruction, prompted_by=None):
        """
        One line the contact says because of something on the call — joining
        it, leaving it — rather than in answer to anything. Remembered, like
        any aside, against their last turn.
        """
        yield events.reply_start()
        payload = prompting.build_payload(self.contact, self.history.for_model(), instruction)
        try:
            text = self._chat_once(payload, temperature=0.85, num_predict=80)
        except Exception:
            text = ""
        text = guards.apply(text, "", 2, True, self.contact.forbidden_address) if text else ""
        if self.call:
            text = " ".join(s for s in (self.call.own_words(self, x)
                                        for x in guards.split_sentences(text)) if s)
        for i, sentence in enumerate(guards.split_sentences(text)):
            if sentence.strip():
                yield events.sentence(i, sentence.strip())
        if text:
            # Joining or leaving a call is its own exchange, saved as one —
            # attached as an aside, it overwrote whatever aside the last turn
            # of an older call already carried.
            if prompted_by:
                self.history.record_exchange(prompted_by, text)
            else:
                self.history.record_aside(text)
        yield events.reply_end(text)

    def _speak_aside(self, instruction, temperature, cap):
        """
        A turn the contact initiates rather than answers.

        It *is* remembered — appended to his previous turn, since nothing was
        said in between. A contact who asks "still with me?" and then cannot
        recall asking is not someone you are having a conversation with.
        """
        if self._recent_asides:
            instruction += ("\n\nYou have already said, unanswered: "
                            + "; ".join(f'"{a}"' for a in self._recent_asides)
                            + ". Say something different in shape as well as words.")
        payload = prompting.build_payload(self.contact, self.history.for_model(), instruction)
        try:
            text = self._chat_once(payload, temperature=temperature)
            if guards.too_similar(text, list(self._recent_asides)):
                # One retry, hotter. Breaking a silence with the same line you
                # broke the last one with is worse than not breaking it at all.
                text = self._chat_once(payload, temperature=min(temperature + 0.2, 1.1))
        except Exception:
            yield events.state(events.IDLE)
            return
        text = guards.apply(text, "", cap, self.already_greeted,
                            self.contact.forbidden_address)
        self._recent_asides.append(text)
        for i, sentence in enumerate(guards.split_sentences(text)):
            if sentence.strip():
                yield events.sentence(i, sentence.strip())
        self.history.record_aside(text)
        yield events.reply_end(text)
        yield events.state(events.IDLE)

    def ask(self, prompt, interrupted=False, confidence=1.0, follow_up=False, via=None):
        """
        Run one full turn.

        Closed early when he is talked over (see `Console.drive`). What he had
        said by then is remembered — it was heard, or begun — and the rest of
        the reply, which was never generated, is not.
        """
        self._spoken = []
        try:
            yield from self._ask(prompt, interrupted, confidence, follow_up, via)
        except GeneratorExit:
            if self._spoken:
                self.history.record_exchange((prompt or "").strip(), " ".join(self._spoken))
            raise

    def _ask(self, prompt, interrupted, confidence, follow_up=False, via=None):
        self._silences = 0
        self._looked = False
        prompt = (prompt or "").strip()
        if not prompt:
            return

        # On a call with others the model reads the conversation as it was
        # heard — who said what since this contact last spoke — and the
        # operator's line is announced once by the call, not by each contact.
        said = self._heard_turn(prompt, follow_up) if self.call else prompt
        if not self.call:
            yield events.message("user", prompt)

        if not follow_up:
            handled = yield from self._handle_command(prompt)
            if handled:
                return

        awareness = self._awareness(prompt, interrupted, confidence)
        if via == "text":
            # A text is answered as a text: short, written, in their own style.
            awareness.append(
                "He's texting you, not calling. Reply as a text message — a line or two, "
                "written the way you text" + (f" ({self.contact.texting})" if self.contact.texting
                                              else "") + ", no stage cues.")
        if self.call:
            awareness.append(self.call.note_for(self, follow_up))
        vault_block = self.vault.as_block(prompt)

        # A plainly factual question is looked up before he is asked anything,
        # so he answers from what was found rather than from what he can
        # imagine. He is still free to be unimpressed by the result.
        search_context = ""
        # Unless an ambient feed already has it: that is the same answer
        # without a second and a half of searching and a page of snippets.
        covered = world.covered(prompt, (self.contact.name, self.contact.full_name))
        # Weather with nowhere attached and nowhere known: ask, don't search.
        placeless = (_WEATHERISH.search(prompt) and not config.LOCATION
                     and not re.search(r"\b(in|at|for) [A-Z]", prompt))
        if (self.contact.can_search and is_factual_lookup(prompt) and not covered
                and not placeless and not follow_up):
            yield events.state(events.SEARCHING)
            search_context = yield from self._run_search(
                prompt, hold_for=None if via == "text" else prompt)

        user_turn = prompting.compose_user_turn(
            prompt, vault_block, search_context, awareness, spoken=said,
            hearsay=grapevine.block(self.contact.id))
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
            # If he went straight to looking without saying anything, give him
            # a line rather than leaving dead air over the search.
            spoke = getattr(self, "_spoke_before_search", False)
            search_context = yield from self._run_search(
                query, hold_for=None if spoke else prompt)

            user_turn = prompting.compose_user_turn(
                prompt, vault_block, search_context, awareness, spoken=said)
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

        self.history.record_exchange(said, reply, via=via)
        self.heard = []
        yield events.reply_end(reply)
        if guards.user_is_leaving(prompt) and not self.call and via != "text":
            yield events.call_ending()
        yield events.state(events.IDLE)

    def _heard_turn(self, prompt, follow_up):
        """
        What this contact hears as one turn on a call: everything said since
        they last spoke, each line labelled with who said it, then — unless
        they are answering someone else — the operator's own line.
        """
        lines = list(self.heard)
        if not follow_up:
            lines.append(f"{self.call.operator}: {prompt}")
        self.heard = []
        return "\n".join(lines)

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
        pool = self.contact.deflections or _DEFLECTIONS
        options = [o for o in pool if o != self._last_deflection]
        choice = random.choice(options or pool)
        self._last_deflection = choice
        return choice

    def _run_search(self, prompt, hold_for=None):
        """
        Look something up, saying a holding line meanwhile when `hold_for` is
        the question being looked up for.

        The search is started first and the line written while it runs: the
        search is network, the line is the model, so they overlap and the line
        costs nothing the search was not already costing.
        """
        # A terse follow-up ("and the price?") is meaningless as a standalone
        # query — graft the last couple of user turns onto it.
        # Only when it actually refers back: grafted onto every short question,
        # "Weather tomorrow?" went out as "You know who she is right? Weather
        # tomorrow?" and came back with nothing useful.
        query = prompt
        if len(prompt.split()) <= 4 and _REFERS_BACK.search(prompt):
            recent = self.history.recent_user()
            if recent:
                query = " ".join(recent[-2:]) + " " + prompt
        elif _WEATHERISH.search(prompt) and config.LOCATION and not re.search(
                r"\b(in|at|for) [A-Z]", prompt):
            query = f"{prompt} {config.LOCATION}"
        self._looked = True
        pending = _SEARCHES.submit(google_search, query, 4)
        if hold_for:
            holding = self._holding_line(hold_for)
            yield events.sentence(0, holding)
            yield events.reply_end(holding, interim=True)
        try:
            results = pending.result()
        except Exception as exc:
            yield events.notice(f"Search failed: {exc}", "warn")
            return None    # looked, and came back empty-handed
        if not results:
            return None
        yield events.sources([
            {"title": r.get("title", ""), "url": r.get("url", "")} for r in results
        ])
        return format_search_results(results)

    def _holding_line(self, prompt):
        """
        What he says while he looks — his own words, fitted to the question.

        A fixed pool ("One moment.", "Stand by.") was the most scripted thing he
        said: the same five lines, whatever was asked. Generated, it can be "Hold
        on, I'll see if it's going to pour" or "Waylon Jones… give me a second".
        It must not answer — the answer is what is being fetched — so anything
        that looks like content (a number, more than a short line) is discarded
        for a stock line rather than risk a guess said aloud.
        """
        instruction = (
            "[REFERENCE — context only]\n"
            "You are looking this up on your terminal right now; the results are "
            "not back yet.\n"
            f"{prompting.SPEECH_CONSTRAINT}\n"
            "[END REFERENCE]\n\n"
            f"He asked: \"{prompt}\"\n"
            "Say a few words to let him know you're looking — your own way, naming "
            "what you're looking for. One short sentence. Do not answer it, guess, "
            "or state any fact."
        )
        payload = prompting.build_payload(self.contact, self.history.for_model(), instruction)
        try:
            line = self._chat_once(payload, temperature=0.9, num_predict=24)
        except Exception:
            line = ""
        line = guards.strip_forbidden_address(
            guards.split_sentences(delivery.clean(line))[0], self.contact.forbidden_address)
        if (not line or len(line.split()) > 12 or re.search(r"\d", line)
                or guards.presumes_presence(line) or "SEARCH" in line.upper()):
            line = random.choice(_PRE_SEARCH_PHRASES)
        return line

    def _handle_command(self, prompt):
        """
        Memory commands. The action is done in code — the vault is not left
        to the model — but the reply is his: "Noted. Stored to the vault."
        every single time was a filing system talking, not a man writing
        something down.
        """
        lowered = prompt.lower()

        story = _STORY_REMEMBER.match(prompt)
        if story:
            fact = self.story.memorize(story.group(1))
            yield from self._acknowledge(
                prompt, f"In the game you play with him, this is now part of your story: "
                        f"\"{fact}\". It's true in the game from here on.")
            return True
        story = _STORY_FORGET.match(prompt)
        if story:
            removed = self.story.forget(story.group(1).strip(" ."))
            yield from self._acknowledge(
                prompt, "He asked to take this out of your story: \"" + story.group(1) + "\". "
                + ("It's gone from the story." if removed else "It wasn't in the story to begin with."))
            return True
        if _STORY_RESET.match(prompt):
            self.story.clear()
            yield from self._acknowledge(
                prompt, "He asked to start your story over. Everything established in the "
                        "game so far is wiped; you begin fresh.")
            return True

        if lowered in _WIPE_COMMANDS:
            self.history.clear()
            self.vault.clear()
            self.story.clear()
            grapevine.clear(self.contact.id)
            yield from self._acknowledge(
                prompt, "You have just wiped everything you remembered of him and "
                        "your conversations, as he asked. You start fresh.", record=False)
            return True

        if lowered.startswith(_FORGET_PREFIXES):
            needle = ""
            for prefix in _FORGET_PREFIXES:
                if lowered.startswith(prefix):
                    needle = prompt[len(prefix):].strip(" .")
                    break
            removed = self.vault.forget(needle) if needle else []
            yield from self._acknowledge(
                prompt, f"He asked you to forget: \"{needle}\". " + (
                    "You have struck it from your notes." if removed else
                    "You had nothing written down that matches it."))
            return True

        if lowered.startswith(_MEMORIZE_PREFIXES) and _is_instruction(lowered):
            self.vault.memorize(prompt)
            yield from self._acknowledge(
                prompt, "You have just written this down so you will remember it: "
                        f"\"{prompt}\".")
            return True

        return False

    def _acknowledge(self, prompt, what_happened, record=True):
        """A short reply, in character, to something just done on his behalf."""
        instruction = (
            "[REFERENCE — context only]\n"
            f"{what_happened}\n"
            f"{prompting.SPEECH_CONSTRAINT}\n"
            "[END REFERENCE]\n\n"
            f"He said: \"{prompt}\"\n"
            "Acknowledge it in your own words — a line, perhaps with a remark of "
            "your own. Don't repeat it back word for word."
        )
        payload = prompting.build_payload(self.contact, self.history.for_model(), instruction)
        try:
            reply = self._chat_once(payload, temperature=0.8, num_predict=60)
        except Exception:
            reply = ""
        reply = guards.apply(reply, prompt, 2, self.already_greeted,
                             self.contact.forbidden_address) if reply else ""
        if not reply or reply == "Mm.":
            reply = "Done."
        if record:
            self.history.record_exchange(prompt, reply)
        for i, sentence in enumerate(guards.split_sentences(reply)):
            if sentence.strip():
                yield events.sentence(i, sentence.strip())
        yield events.reply_end(reply)
        yield events.state(events.IDLE)
