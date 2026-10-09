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
import time
from collections import deque

import ollama

from .. import config, delivery, events
from ..memory import History, Story, Vault
from . import culture, grapevine, groupchat, guards, initiative, presence, prompting, world
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
def operator_name():
    from .. import operator
    return operator.name()


def _link_marker(contact):
    return f"{contact.name}?"


# The user side of a text they sent first. Never shown; it keeps the history
# alternating, and tells the model on a later turn that nobody had asked.
REACH_MARKER = "(Nothing from him — you texted first.)"

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
# Case-sensitive, and opening a bracket or a line: "Research: still nothing"
# was read as a marker and spoken as "Re".
_SEARCH_MARKER = re.compile(r"(?:\[\s*|^\s*)SEARCH\s*:\s*([^\]\n]+?)\s*\]?\s*$", re.M)

# Something he asked them, privately, to go and say or do in a group chat.
_GROUP_TASK = re.compile(r"\[\s*group\s*:\s*([^|\]]+)\|([^\]]+)\]", re.I)

# A text they've chosen not to answer yet, or at all.
_REPLY_CHOICE = re.compile(r"\[\s*(later|no reply)\s*\]", re.I)
# A tapback on his text, with or instead of a reply.
_TEXT_REACT = re.compile(r"\[\s*react\s*:\s*([^\]]{1,8})\]", re.I)

# Someone on a call ringing someone else in: [add: Jason].
_CALL_ADD = re.compile(r"\[\s*add\s*:\s*([^\]]+)\]", re.I)

# Written at the end of a reply when they're the one closing the call.
_HANG_UP = re.compile(r"\[\s*(hang(s|ing)?\s+up|end(s|ing)?\s+(the\s+)?call|click)\b[^\]]*\]", re.I)

# Offered once a call has had some substance — never on the greeting, which
# would end calls before they started.
CLOSING_DIRECTIVE = (
    "If the call has run its course — what was needed is said, and one of you has "
    "somewhere to be — you may close it yourself: say goodbye your way and end with "
    "[hang up]. Never mid-topic, and never just because he paused.")
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
        self.call_started_at = time.time()
        # Texts that landed during a call but were overtaken before they could
        # be answered: they ride along with the next turn.
        self.unseen_texts = []
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
        max_sentences = getattr(self, "_turn_cap", None) or self.contact.max_reply_sentences
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
        can_search = self._can_look() and not getattr(self, "_no_search", False)
        self._pending_query = None
        self._cue_spent = False
        self._hanging_up = False
        self._call_add = []

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
            # "Just 'okay'?" — people don't audit each other's word counts on a
            # call. Told not to, every contact still did it to a short reply.
            if guards.remarks_on_brevity(delivery.clean(text), prompt):
                dropped_presence += 1
                return False, None
            if index == 0 and not spoken and self.already_greeted:
                text = guards.strip_regreeting(text).strip()
                if not text:
                    return False, None
            return True, text

        stream = self._stream(payload)
        for piece in stream:
            buffer += piece
            if _HANG_UP.search(buffer):
                # They're closing the call: their goodbye is meant, not trailing.
                self._hanging_up = leaving = True
                buffer = _HANG_UP.sub("", buffer)
            adding = _CALL_ADD.search(buffer)
            if adding:
                self._call_add.append(adding.group(1).strip())
                buffer = _CALL_ADD.sub("", buffer)
            task = _GROUP_TASK.search(buffer)
            if task:
                self._group_task = (task.group(1).strip(), task.group(2).strip())
                buffer = _GROUP_TASK.sub("", buffer)
            elif "[group" in buffer.lower() and "]" not in buffer[buffer.lower().rfind("[group"):]:
                continue        # half a marker so far; wait for the rest
            tapback = _TEXT_REACT.search(buffer)
            if tapback:
                self._text_react = tapback.group(1).strip()
                buffer = _TEXT_REACT.sub("", buffer)
            elif "[react" in buffer.lower() and "]" not in buffer[buffer.lower().rfind("[react"):]:
                continue        # half a tapback so far
            choice = _REPLY_CHOICE.search(buffer)
            if choice:
                self._reply_choice = "none" if "no" in choice.group(1).lower() else "later"
                buffer = _REPLY_CHOICE.sub("", buffer)

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
                        stream.close()      # or the retry queues behind the rest of it
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

        if _HANG_UP.search(tail):
            self._hanging_up = leaving = True
            tail = _HANG_UP.sub("", tail).strip()
        tail_is_content = bool(tail) and not self._is_signoff(tail)
        if self._hanging_up or tail_is_content:
            # Real content after them means they weren't a farewell after all.
            for pending in held:
                if len(spoken) < max_sentences:
                    spoken.append(pending)
                    yield events.sentence(index, pending)
                    index += 1
            held = []
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
                if char == "…" or text[max(0, i - 2):i + 1] == "...":
                    # An ellipsis is a pause, not an ending, when the thought
                    # carries on in lowercase — "You sound... tired." Cut there,
                    # a one-line reply stopped at "You sound...". Wait to see.
                    rest = text[i + 1:].lstrip()
                    if not rest:
                        return None
                    if rest[0].islower():
                        continue
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
        if _HANG_UP.search(text):
            self._hanging_up = True
            text = _HANG_UP.sub("", text).strip()
        text = self._plain(text)
        if not text or guards.parrots(text, prompt) or guards.too_similar(text, recent):
            text = self._deflection()
        text = guards.apply(text, prompt,
                            getattr(self, "_turn_cap", None) or self.contact.max_reply_sentences,
                            self.already_greeted, self.contact.forbidden_address,
                            farewell=getattr(self, "_hanging_up", False))
        if self.call:
            text = " ".join(x for x in (self.call.own_words(self, y)
                                        for y in guards.split_sentences(text)) if x)
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

    def boot(self, incoming=None, rung=0, also_ringing=()):
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
        whereabouts = presence.of(self.contact).note()
        if incoming:
            # They rang him. Whoever calls has a reason, and opens with it.
            instruction += (f"\n(This time YOU rang HIM, and he's just picked up. You called "
                            f"about: {incoming}. Open the way you do when you're the one "
                            "calling — a word of hello at most, then why.)")
        elif whereabouts:
            instruction += f"\n({whereabouts} He's calling you.)"
        if also_ringing:
            instruction += (f"\n(He's ringing several of you at once — {', '.join(also_ringing)} too. "
                            "You're the first on the line; the others may join.)")
        if rung and not incoming:
            instruction += (f"\n(He's rung you {rung + 1} times in the last few minutes before you "
                            "picked up. React to that the way you would.)")
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
        self.mark_call_start()
        self._silences, self._last_silence_move = 0, None

        # The 'user' side is a neutral placeholder, never shown on screen.
        self.history.append("user", marker)
        self.history.messages[-1]["marker"] = True
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
        if self.contact.silence:
            move += " " + self.contact.silence      # how this person meets a silence
        if kind == "world":
            # One item, picked here. Given the whole feed he raised the top
            # headline every time.
            items = [item.strip() for line in world.snapshot(self.contact)
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
        if world.snapshot(self.contact):
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
        yield from self._speak_aside(instruction, temperature=0.9, cap=2, farewell=True)

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

    def _plain(self, text):
        """Markers that only mean something to the engine, never said or sent."""
        text = _SEARCH_MARKER_COMPLETE.sub("", text or "")
        text = _SEARCH_MARKER.sub("", text)
        return _HANG_UP.sub("", text).strip()

    def draft(self, instruction):
        """
        What they'd say to this, written but not said — for two people opening
        their mouths into the same pause, where only the first words come out.
        """
        payload = prompting.build_payload(self.contact, self.history.for_model(), instruction)
        try:
            text = self._chat_once(payload, temperature=0.85, num_predict=80)
        except Exception:
            return ""
        if re.match(r"\W*skip\b", text or "", re.I):
            return ""
        text = re.sub(r"\[[^\]]*\]", "", text or "")
        return self._plain(text).strip()

    def utter(self, text, cut_off=False):
        """
        Say exactly this — a line already drafted — as an event stream. A
        line cut off by someone else isn't remembered as said; one finished is.
        """
        yield events.reply_start()
        for i, sentence in enumerate(guards.split_sentences(text)):
            if sentence.strip():
                yield events.sentence(i, sentence.strip())
        if text and not cut_off:
            self.history.record_aside(text)
        yield events.reply_end(text)

    def say(self, instruction, prompted_by=None, greeting=False, farewell=False):
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
        if re.match(r"\W*skip\b", text or "", re.I):
            text = ""       # nothing they'd say
        # Chasing someone who didn't pick up: "I'll text her."
        self._nudging = bool(re.search(r"\[\s*nudge\s*\]", text or "", re.I))
        text = re.sub(r"\[\s*nudge\s*\]", "", text or "", flags=re.I)
        # Talked over: letting the other one go first.
        self._yielding = bool(re.search(r"\[\s*(yield|you go)\s*\]", text or "", re.I))
        text = re.sub(r"\[\s*(yield|you go)\s*\]", "", text or "", flags=re.I)
        # Joining only to say no — "I told you not to call me" — and gone.
        self._hanging_up = bool(_HANG_UP.search(text or ""))
        self._call_add = [m.strip() for m in _CALL_ADD.findall(text or "")]
        text = _CALL_ADD.sub("", text or "")
        text = self._plain(text)
        # Joining a call is a greeting and leaving one a goodbye: the guards
        # that strip those from ordinary turns emptied both to "Mm.".
        text = guards.apply(text, "", 2, not greeting, self.contact.forbidden_address,
                            farewell=farewell) if text else ""
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

    def _background(self):
        """What they've heard secondhand, and what's been said in their group chats."""
        from ..contacts import directory
        parts = [grapevine.block(self.contact.id), groupchat.block(self.contact.id, directory())]
        return "\n\n".join(p for p in parts if p)

    def group_post(self, group, unread, must=False, opening=None, task=None, chase=None):
        """
        Their next message in a group chat, or "" if they'd rather not say
        anything. `unread` is what they've just read; `opening` is something on
        their mind when they start a conversation themselves.
        Not written into their own history: the group's log is the record, and
        it reaches them through _background() wherever they speak next.
        """
        from ..contacts import directory
        book = directory()
        name = groupchat.names(book)
        members = ", ".join([operator_name()] + [name(m) for m in group.members if m != self.contact.id])
        context = [f"Time: {prompting.time_context()}.",
                   f"Group chat \"{group.name}\" — you, {members}."]
        known = groupchat.relations(self.contact, group, book)
        if known:
            context.append("Who's here, and what they are to you — keep it in mind:\n" + "\n".join(known))
        note = groupchat.secrets_note(group, self.contact.id)
        if note:
            context.append(note)
        earlier = [m for m in group.seen_by(self.contact.id, limit=14) if m not in unread][-8:]
        if earlier:
            context.append("Earlier in the chat:\n" + groupchat.transcript(earlier, name))
        if unread:
            context.append("Just now (you've only now read these):\n" + groupchat.transcript(unread, name))
        # Where they are and how late they are to it: a reply three hours on
        # reads as one, and they can say why — or not.
        state = presence.of(self.contact).now()
        waited = time.time() - min(m["at"] for m in unread) if unread else 0
        if waited > groupchat.GAP:
            context.append(f"These came in over the last {groupchat.later(waited)}; you're only "
                           "seeing them now" + (f" — you've been {state['doing']}." if state["doing"] else "."))
        elif state["doing"]:
            context.append(f"Right now you're {state['doing']}.")
        lately = culture.note(self.contact, " ".join(m["text"] for m in unread[-4:]) if unread else (opening or ""))
        if lately:
            context.append(lately)
        style = f" ({self.contact.texting})" if self.contact.texting else ""
        if task:
            ask = (f"Bruce asked you privately to do this in the group: {task}. Do it now, in your own "
                   "words, as you would — don't mention that he asked unless you'd naturally say so.")
        elif chase:
            ask = (f"{operator_name()} asked {chase['name']} something in here {chase['ago']} and they "
                   "haven't even read it yet."
                   + (f" As far as you know they're {chase['doing']}." if chase.get("doing") else "")
                   + f" Say something about it if you would — ping them with @{chase['name']}, cover "
                   "for them, or tell him where they probably are (only what you'd actually know). "
                   "If you wouldn't bother, reply with exactly SKIP.")
        elif opening:
            quiet = (time.time() - group.messages()[-1]["at"] > 3600) if group.messages() else True
            ask = (("Nobody's said anything for a while. " if quiet else "")
                   + f"Start something in the group — {opening}. One or two short texts.")
        else:
            ask = ("Read what's actually going on — what Bruce means, and what you know of everyone "
                   "here — then write your next message to the group, the way you text in a group: "
                   "reply to whoever you're answering, react, or keep it to a word. You don't have "
                   "to address Bruce.")
            ask += (" You were asked directly: answer." if must else
                    " If you wouldn't actually say anything here, reply with exactly SKIP.")
            others = [c for c in book if c.id not in group.members and c.id != self.contact.id]
            ask += (" If you'd genuinely walk out of this chat now — you've had enough, it isn't your "
                    "place, it's over for you — end with [leave]. If he asks you to remove someone, end with [remove: their first name]. If he "
                    "asked you to add someone — and only then, or if someone is truly needed and he "
                    "hasn't said to keep it small — end with [add: their first name]"
                    + (f" (could be {', '.join(c.name for c in others)})" if others else "") + ".")
        ask += (" Most messages tag nobody; tag someone with @Name only to pull in someone who isn't "
                "already talking — never the person you're replying to. If you'd just react to "
                "the latest message instead of writing anything — the way you actually do, if you "
                "do — reply with only [react: emoji], usually one of ❤️ 👍 👎 😂 ‼️ ❓; you can also put "
                "[react: emoji] with a text.")
        instruction = ("[REFERENCE — context only]\n" + "\n".join(context) + "\n[END REFERENCE]\n\n"
                       + ask + f" As texts{style}; never write a line for anyone else, no stage cues.")
        payload = prompting.build_payload(self.contact, self.history.for_model(), instruction,
                                          texting=True)
        try:
            text = self._chat_once(payload, temperature=0.9, num_predict=110)
        except Exception:
            return ""
        # What they do to the group, not what they say in it.
        self._group_actions = {
            "leave": bool(re.search(r"\[\s*leave\s*\]", text, re.I)),
            "add": [m.strip() for m in re.findall(r"\[\s*add\s*:\s*([^\]]+)\]", text, re.I)],
            "remove": [m.strip() for m in re.findall(r"\[\s*remove\s*:\s*([^\]]+)\]", text, re.I)],
            "react": next(iter(re.findall(r"\[\s*react\s*:\s*([^\]]{1,8})\]", text, re.I)), "").strip()}
        text = re.sub(r"\[\s*(leave|(add|remove|react)\s*:[^\]]*)\]", "", text, flags=re.I)
        text = re.sub(r"\s*\[[^\]]{0,14}\]", "", text)     # a marker half-written: "[]", "[react]"
        text = self._plain(text)
        if re.match(r"\W*skip\b", text, re.I) and not must:
            return ""
        if not text.strip(" .") and self._group_actions["react"]:
            return ""           # a tapback, and nothing to say
        # A line written for someone else ("Tim: lol") is theirs to write, not this one's.
        lines = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            label = re.match(r"^\W*(\w[\w .'-]{0,20}):\s*(.*)$", line)
            if label:
                speaker = label.group(1).strip().lower()
                if speaker in {self.contact.name.lower(), self.contact.full_name.lower()}:
                    line = label.group(2)
                elif any(speaker == book.get(m).name.lower() for m in group.members
                         if book.get(m)) or speaker == operator_name().lower():
                    continue
            lines.append(line)
        text = "\n".join(guards.cap_length(line, 3) for line in lines[:4])
        text = guards.strip_forbidden_address(delivery.clean(text), self.contact.forbidden_address)
        return text.strip()

    def reach_out(self, about, why="impulse"):
        """
        A text they send first. `about` is what's on their mind — picked by
        the caller (see wayne.engine.initiative), because a model told only
        "text him" asks how he is, every time. Remembered like any text.
        """
        gap = self.history.time_since_last()
        context = [f"Time: {prompting.time_context()}."]
        if gap:
            context.append(f"You last spoke {gap}.")
        whereabouts = presence.of(self.contact).note()
        if whereabouts:
            context.append(whereabouts)
        background = self._background()
        if background:
            context.append(background)
        style = f" ({self.contact.texting})" if self.contact.texting else ""
        if why == "chase":
            ask = ("He's left your last text on read. What you do about it is yours: chase it the way "
                   "you would, come at it from a new angle, or let it go and text him about something "
                   "else entirely. If you'd honestly just leave it, reply with exactly SKIP.")
        elif why in ("declined", "missed"):
            ask = (f"You just rang him and he {'declined the call' if why == 'declined' else 'did not pick up'}. "
                   f"You were calling about: {about}. Text him instead, the way you would.")
        elif why == "worry":
            ask = (f"Since you last spoke, something's stayed with you: {about}. Check in on him — "
                   "the way you would, which might be a word, a joke, or something that never says "
                   "'worried'. If you'd honestly let it go, reply with exactly SKIP.")
        elif why == "second_thought":
            ask = ("You've just texted him. A moment later one more thing occurs to you — send it "
                   "as a short follow-up text. If nothing would, reply with exactly SKIP.")
        elif why == "busy_now":
            ask = (f"He's ringing you right now and you're not picking up — {about}. Text him "
                   "a line, the way you would when you can't talk. Say why only if you'd bother.")
        elif why == "callback":
            ask = (f"He rang you earlier and you didn't pick up — {about}. You're free now. "
                   "Get back to him by text. Say why you couldn't answer if you want to, or don't; "
                   "if it was nothing in particular, it was nothing in particular.")
        elif why == "promise":
            ask = (f"You said you'd get back to him about {about} — whatever you were doing for "
                   "it is done now. Text him. "
                   "Whatever happened on your end is yours to tell — it's your life; keep it "
                   "plausible and in keeping with what you both said.")
        else:
            ask = (f"Nothing from him; you're texting him first because of {about}. Say it the "
                   "way you'd actually text it — no 'hey, how are you' preamble unless "
                   "that's truly you.")
        instruction = (
            "[REFERENCE — context only]\n" + "\n".join(context) + "\n[END REFERENCE]\n\n"
            + ask + f" Write it as a text{style} — a line or two, no stage cues.")
        payload = prompting.build_payload(self.contact, self.history.for_model(), instruction,
                                          texting=True)
        try:
            text = self._chat_once(payload, temperature=0.9, num_predict=120)
        except Exception:
            return ""
        text = guards.strip_forbidden_address(text, self.contact.forbidden_address)
        text = delivery.clean(guards.strip_presence(self._plain(text)) or self._plain(text)).strip().strip('"')
        # A text, not a letter: told "a line or two", a model writes a paragraph.
        lines = [guards.cap_length(line, 3) for line in text.splitlines() if line.strip()][:4]
        text = "\n".join(lines)
        if why in ("second_thought", "chase", "worry") and re.match(r"\W*skip\b", text, re.I):
            return ""
        if text:
            self.history.record_exchange(REACH_MARKER, text, via="text")
        return text

    def _speak_aside(self, instruction, temperature, cap, farewell=False):
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
        text = guards.apply(self._plain(text), "", cap, self.already_greeted,
                            self.contact.forbidden_address, farewell=farewell)
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
        self._said = (prompt or "").strip()
        self._via = via
        try:
            yield from self._ask(prompt, interrupted, confidence, follow_up, via)
        except GeneratorExit:
            # What was heard, as it was put to them (with whatever else was
            # heard on the call) — and only if the turn wasn't already
            # recorded, or closing the generator after the fact recorded it twice.
            if self._spoken:
                self.history.record_exchange(self._said, " ".join(self._spoken),
                                             via="text" if self._via == "text" else None)
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
        said = self._heard_turn(prompt, follow_up, via) if self.call else prompt
        if via == "text_on_call" and not self.call:
            said = f"(texted you during the call) {prompt}"
        if self.unseen_texts and via != "text":
            said = "\n".join(f"(texted you during the call) {t}" for t in self.unseen_texts) \
                + "\n" + said
            self.unseen_texts = []
        self._said = said
        if not self.call:
            yield events.message("user", prompt)

        if not follow_up:
            handled = yield from self._handle_command(prompt)
            if handled:
                return

        awareness = self._awareness(prompt, interrupted, confidence)
        texting = via == "text"
        self._reply_choice, self._deferred, self._group_task = None, None, None
        self._text_react = None
        length, self._turn_cap = None, None
        if via is None:
            # A call: this turn's length, from their own spread (see
            # prompting.spoken_length) — and held to it. With company too: a
            # group call isn't the place for the monologue a one-to-one might
            # carry, and without it Tim answered "who's running point?" in five.
            length, self._turn_cap = prompting.spoken_length(self.contact, prompt)
            length = length or None
        if texting and getattr(self, "pestered", 0) >= 3:
            awareness.append(f"He's sent you {self.pestered} texts in a row in the last few minutes. "
                             "React to that the way you would.")
            self.pestered = 0
        if texting:
            state = presence.of(self.contact).now()
            if state["status"] == presence.BUSY and state["doing"]:
                awareness.append(
                    f"You're {state['doing']} — reading this between things. Reply briefly, "
                    "or tell him you'll get back to him.")
            elif state["doing"] and state["source"] in ("routine", "conversation"):
                # Patrol is when they're most reachable — and least chatty.
                awareness.append(f"You're {state['doing']}; texting between things, so keep it short.")
            # Theirs to decide, whatever their status: answer now, sit on it, or not at all.
            pace = self.contact.texting_pace or {}
            habit = ("You do this a lot." if pace.get("on_read", 0) + pace.get("ghost", 0) >= 0.4 else
                     "You do it sometimes." if pace.get("on_read", 0) + pace.get("ghost", 0) >= 0.15 else
                     "You almost never do this.")
            from ..memory import groups as group_store
            mine = [g.name for g in group_store.groups_with(self.contact.id)]
            if mine:
                awareness.append(
                    "Your group chats with him: " + ", ".join(f"“{n}”" for n in mine) + ". If he asks you "
                    "to say or do something in one, don't write it here — answer him here (asking which "
                    "chat if it isn't clear), and end with [group: exact chat name | what you'll do there]; "
                    "you'll then go and do it in the chat yourself.")
            tapped = getattr(self, "tapbacks_seen", [])
            if tapped:
                awareness.append("Since you last texted: " + "; ".join(tapped) + ".")
                self.tapbacks_seen = []
            awareness.append(
                "If you'd react to his text instead of writing back, or as well — the way you do, "
                "if you do — include [react: emoji], usually one of ❤️ 👍 👎 😂 ‼️ ❓.")
            awareness.append(
                "You've read it. If you'd genuinely leave him on read for a while right now — mood, "
                "pride, busy, making a point — begin with [later] and write what you'd eventually send; "
                "if you wouldn't answer at all, reply exactly [no reply]. " + habit)
        if via == "text_on_call":
            awareness.append(
                "He's just texted you this while you're on the call together — it's on "
                "your phone. Acknowledge it on the call, briefly, and use it.")
        elif via == "text":
            # A text is answered as a text: short, written, in their own style.
            awareness.append(
                "He's texting you, not calling. Reply as a text message, "
                "written the way you text" + (f" ({self.contact.texting})" if self.contact.texting
                                              else "") + ", no stage cues. "
                "Several short texts go on separate lines.")
            length = initiative.length_hint(self.contact, prompt) or None
        if self.call:
            awareness.append(self.call.note_for(self, follow_up))
        elif via is None and self._said_this_call() >= 2:
            awareness.append(CLOSING_DIRECTIVE)
        vault_block = self.vault.as_block(prompt)

        # A plainly factual question is looked up before he is asked anything,
        # so he answers from what was found rather than from what he can
        # imagine. He is still free to be unimpressed by the result.
        search_context = ""
        # Unless an ambient feed already has it: that is the same answer
        # without a second and a half of searching and a page of snippets.
        covered = world.covered(prompt, (self.contact.name, self.contact.full_name), self.contact)
        # Weather with nowhere attached and nowhere known: ask, don't search.
        placeless = (_WEATHERISH.search(prompt) and not config.LOCATION
                     and not re.search(r"\b(in|at|for) [A-Z]", prompt))
        if self.contact.can_search and not self._can_look() and is_factual_lookup(prompt):
            state = presence.of(self.contact).now()
            awareness.append(
                f"You're away from any screen right now ({state['doing'] or 'out'}) — you can't "
                "look anything up. Answer from what you know, or say you'll check when you can.")
        lately = culture.note(self.contact, prompt)
        if lately:
            awareness.append(lately)
        tracker = self._tracker(prompt)
        if tracker:
            awareness.append(tracker)
        known = ""
        if (self._can_look() and is_factual_lookup(prompt) and not covered
                and not placeless and not follow_up):
            yield events.state(events.SEARCHING)
            search_context = yield from self._run_search(
                prompt, hold_for=None if via == "text" or not self.contact.search_aloud else prompt)
        elif is_factual_lookup(prompt) and not covered and not placeless and not follow_up:
            # Nobody at a screen still knows things. The answer is found quietly
            # and handed over as what they might know — theirs to use if someone
            # like them would, never as a lookup: Dick knows the score, Jason
            # doesn't know the charts, and nobody says "let me check".
            found = yield from self._run_search(prompt, hold_for=None, silent=True)
            if found:
                known = ("What's true here, if you happen to know it — you'd have heard, read or "
                         "seen it the way someone like you would. Use only what you'd plausibly "
                         "know, said as you'd say it; never as though you'd just looked it up. If "
                         "you honestly wouldn't know, say so your way:\n" + found)

        hearsay = "\n\n".join(part for part in (self._background(), known) if part)
        user_turn = prompting.compose_user_turn(
            prompt, vault_block, search_context, awareness, spoken=said,
            hearsay=hearsay, contact=self.contact, length=length)
        payload = prompting.build_payload(self.contact, self.history.for_model(), user_turn,
                                          texting=texting)

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
                query, hold_for=None if spoke or not self.contact.search_aloud else prompt)

            user_turn = prompting.compose_user_turn(
                prompt, vault_block, search_context, awareness, spoken=said, contact=self.contact,
                length=length)
            payload = prompting.build_payload(
                self.contact, self.history.for_model(), user_turn, texting=texting)
            yield events.state(events.THINKING)
            yield events.reply_start()
            try:
                # One search a turn. Asked again — likeliest when the first
                # found nothing — he answers from what he has instead.
                self._no_search = True
                reply = yield from self._emit_sentences(payload, prompt)
            except Exception as exc:
                yield events.notice(f"Connection severed: {exc}", "error")
                yield events.state(events.IDLE)
                return
            finally:
                self._no_search = False
            if reply == _SEARCH_REQUESTED:
                reply = ""

        if not isinstance(reply, str) or not reply:
            reply = reply if isinstance(reply, str) and reply else "Mm."
        if texting and self._reply_choice == "none":
            # Read, and left. Remembered as exactly that.
            self.history.record_exchange(said, "(You read it and didn't answer.)", via="text")
            self._spoken = []
            yield events.reply_end("")
            yield events.state(events.IDLE)
            return
        if texting and self._reply_choice == "later":
            # Held back, not remembered until it's actually sent (Console decides).
            self._deferred = (said, reply)
            self._spoken = []
            yield events.reply_end(reply)
            yield events.state(events.IDLE)
            return
        self.history.record_exchange(said, reply, via="text" if via == "text" else None)
        self._spoken = []
        self.heard = []
        yield events.reply_end(reply)
        closing = guards.user_is_leaving(prompt) or getattr(self, "_hanging_up", False)
        if closing and not self.call and via is None:
            yield events.call_ending()
        yield events.state(events.IDLE)

    _WHEREABOUTS = re.compile(r"(?i)\b(where(?:'s| is| are|abouts)?|location|heard from|seen|up to|"
                              r"status|on patrol|out tonight|doing|check on|tracker|going on|happening|"
                              r"scanner|police|reports?|crime|trouble|quiet tonight)\b")

    def _tracker(self, prompt):
        """
        Where the family are, for the ones who see the tracker as he does —
        Alfred in the cave, Barbara at her screens — when he's asking after
        someone. Those who don't share their location aren't on it.
        """
        if not getattr(self.contact, "sees_whereabouts", False) or not self._WHEREABOUTS.search(prompt or ""):
            return ""
        from ..contacts import directory
        book = directory()
        lines, dark = [], []
        for other in book:
            if other.id == self.contact.id:
                continue
            if not other.shares_location:
                dark.append(other.name)
                continue
            state, (where, company) = presence.of(other).now(), presence.of(other).whereabouts()
            with_ = [book.get(c).name for c in company if book.get(c)]
            lines.append(f"{other.name}: {state['status']}" + (f", {state['doing']}" if state["doing"] else "")
                         + (f" — {where}" if where else "") + (f", with {' and '.join(with_)}" if with_ else ""))
        from . import incidents
        scanner = incidents.scanner_note()
        return ("The family tracker, as you see it on your screens. Answer only what he asked — "
                "whoever he asked about, the way you would — not a roll call: " + "; ".join(lines)
                + (f". Not on it: {', '.join(dark)}." if dark else ".")
                + (f" {scanner}" if scanner else ""))

    def _can_look(self):
        """
        Whether they could look something up right now: someone who searches
        at all, at a screen. Alfred almost always is; Barbara at the clock
        tower is; nobody on a rooftop, asleep, or out on an errand is.
        """
        if not self.contact.can_search:
            return False
        state = presence.of(self.contact).now()
        return state["source"] in ("free", "engaged") or bool(state.get("terminal"))

    def mark_call_start(self):
        """
        This call begins now. A time, not a list index: the history is trimmed
        from the front as it grows, and an index into it went stale after
        thirty exchanges — every call after that looked empty, so nothing
        travelled on the grapevine and no promise made on a call was kept.
        """
        self.call_started_at = time.time()

    def call_messages(self):
        """What has been said since this call began, greeting placeholder excluded."""
        return [m for m in self.history.messages
                if m.get("at", 0) >= self.call_started_at - 0.001 and not m.get("marker")]

    def call_index(self):
        """Where this call starts in the history, as an index — for older callers."""
        for i, m in enumerate(self.history.messages):
            if m.get("at", 0) >= self.call_started_at - 0.001:
                return i
        return len(self.history.messages)

    def keep_heard(self):
        """
        Off a group call with lines heard and not yet answered: remembered as
        heard, or "what did you make of what Lucius said?" draws a blank.
        """
        if self.heard:
            self.history.record_exchange("\n".join(self.heard), "Mm.")
            self.heard = []

    def _said_this_call(self):
        """How many times he has spoken since this call began."""
        return sum(1 for m in self.call_messages() if m["role"] == "user" and not m.get("via"))

    def _heard_turn(self, prompt, follow_up, via=None):
        """
        What this contact hears as one turn on a call: everything said since
        they last spoke, each line labelled with who said it, then — unless
        they are answering someone else — the operator's own line.
        """
        lines = list(self.heard)
        if not follow_up:
            label = " (texted you, privately)" if via == "text_on_call" else ""
            lines.append(f"{self.call.operator}{label}: {prompt}")
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
        user_turn = prompting.compose_user_turn(prompt, vault_block, contact=self.contact)
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

    def _run_search(self, prompt, hold_for=None, silent=False):
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
            results = pending.result(timeout=8 if silent else 15)
        except Exception as exc:
            if not silent:
                yield events.notice(f"Search failed: {exc}", "warn")
            return None    # looked, and came back empty-handed
        if not results:
            return None
        if silent:
            return format_search_results(results)     # nobody saw a search happen
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

        needle = next((prompt[len(p):].strip(" .!?") for p in _FORGET_PREFIXES
                       if lowered.startswith(p)), None)
        # "Forget about it." is an idiom, not an instruction: it names nothing,
        # and as a search for "it" it once struck most of the notes.
        if needle is not None and needle.lower() not in ("", "it", "that", "this", "them", "all that"):
            removed = self.vault.forget(needle)
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
