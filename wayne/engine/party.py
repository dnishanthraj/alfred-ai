"""
A call with more than one contact on the line.

Three people talking is not three conversations. Everyone hears everything,
so each contact is handed what was said since they last spoke — every line
labelled with who said it — and only one of them answers at a time.

Who answers is decided here, in code, the way a room decides it:

  * name someone ("Lucius, what do you think?") and they answer;
  * address the room ("both of you", "you two") and each answers in turn;
  * otherwise whoever he was last talking to carries on.

After that, one other contact may come in — but only when the last reply
named them or asked them something. One follow-up a turn, never a ping-pong:
two models left to answer each other will happily talk until the power fails,
and the operator's call becomes something he is listening to rather than in.

Every event a contact produces is tagged with who produced it, so the console
speaks it in the right voice and labels the subtitle. Interrupting is the same
as on any call: talking over whoever is speaking closes the whole exchange.
"""
import random
import re

from .. import events
from .. import operator as wayne_operator

MAX_CONTACTS = 4
# Contact turns per operator turn: the people he addressed, plus one follow-up.
MAX_TURNS = 3

_THE_ROOM = re.compile(
    r"\b(both of you|you both|you two|the two of you|all of you|you all|everyone|"
    r"everybody|guys|you lot)\b", re.I)


def _opening(text):
    """The first few words of a line, broken off: "Actually, I was—"."""
    words = text.split()
    cut = min(len(words), random.choice((2, 3, 3, 4)))
    return " ".join(words[:cut]).rstrip(",.;:!?") + "—"


class Call:
    def __init__(self, operator=None):
        self.operator = operator or wayne_operator.full_name()
        self.members = []          # ContactSession, in the order they joined
        self.last_speaker = None
        # Everything said on the call, labelled — so whoever joins late is
        # handed what they missed instead of arriving blind.
        self.transcript = []
        # Everyone rung for this call, picked up or not: a missed ring can
        # still dial back in while it's going.
        self.invited = set()
        # Whether the last unprompted line (a greeting, a reaction) got an
        # answer — so nobody greets a newcomer twice.
        self.aside_answered = False

    # --- membership ------------------------------------------------------

    def join(self, session):
        if session in self.members or len(self.members) >= MAX_CONTACTS:
            return False
        if not self.transcript and self.members:
            # Until now this was a one-to-one call, which kept no transcript of
            # its own; the first contact's recent conversation is what was said.
            first = self.members[0]
            for message in first.history.messages[-6:]:
                who = self.operator if message["role"] == "user" else first.contact.full_name
                self.transcript.append(f"{who}: {message['content']}")
        self.members.append(session)
        session.mark_call_start()
        # The last few lines before they joined, marked as such: enough to
        # pick up the thread, not a recording of the evening.
        session.heard = [f"(before you joined) {line}" for line in self.transcript[-6:]]
        self._attach()
        return True

    def leave(self, session):
        if session not in self.members:
            return
        self.members.remove(session)
        session.keep_heard()
        session.call, session.heard = None, []
        if self.last_speaker is session:
            self.last_speaker = None
        self._attach()

    def _attach(self):
        # The call is only switched on with company: one contact alone is an
        # ordinary call, and behaves exactly as one.
        group = len(self.members) > 1
        for member in self.members:
            member.call = self if group else None
            if not group:
                member.keep_heard()
                member.heard = []

    @property
    def is_group(self):
        return len(self.members) > 1

    # --- what each contact is told ---------------------------------------

    def others(self, session):
        return [m for m in self.members if m is not session]

    def note_for(self, session, follow_up=False):
        names = ", ".join([self.operator] + [m.contact.full_name for m in self.others(session)])
        note = (f"This is a call with more than one person on the line: you, {names}. "
                "Lines from others are labelled with their names. Speak only for "
                "yourself — never write a line for anyone else — and keep it to what "
                "you'd say on a call with company: brief, in turn, and to whoever you're "
                "talking to. Anything earlier in your memory is from previous calls; "
                "this call is about what's being said now. If you have to go, say a quick "
                "goodbye and end with [hang up] — you'll drop off and the call goes on. If he "
                "asks you to get someone on the line, or someone truly needs to be here, end "
                "with [add: their first name] and you'll ring them in.")
        secret = self._secrets(session)
        if secret:
            note += " " + secret
        if follow_up:
            note += (" He didn't ask you directly; you're coming in because of what was "
                     "just said to you or about you. Answer that, briefly.")
        return note

    def _secrets(self, session):
        """
        Who on the line doesn't know what's under the masks, for those who do —
        Selina on a call with Dick and Tim hears Dick and Tim, never Nightwing.
        """
        secrets = wayne_operator.profile().get("secrets") or {}
        known = set(secrets.get("known_by") or [])
        if session.contact.id not in known:
            return ""
        outsiders = [m.contact.name for m in self.others(session) if m.contact.id not in known]
        if not outsiders:
            return ""
        who = " and ".join(outsiders)
        return (f"{who} is on this call and doesn't know about the masks. In front of {who}: no "
                "patrols, cases, villains, suits, gear, the cave, or who anyone really is — nothing "
                "a family and its friends wouldn't say on an ordinary call. Real names only. If "
                "someone else slips, cover it lightly, as you would.")

    def own_words(self, session, sentence):
        """A sentence with any speaker label for someone else removed, or ''."""
        for other in self.others(session):
            for name in {other.contact.name, other.contact.full_name}:
                if re.match(rf"^\W*{re.escape(name)}\s*:", sentence, re.I):
                    return ""
        for name in {session.contact.name, session.contact.full_name}:
            sentence = re.sub(rf"^\W*{re.escape(name)}\s*:\s*", "", sentence, flags=re.I)
        return sentence.strip()

    # --- turns -------------------------------------------------------------

    def addressed(self, text):
        """
        Who a line is for: the room, or the one person it's said to — whoever
        is named first. "Lucius, Alfred says the suit's too heavy" is for
        Lucius; Alfred is being talked about, not to, and comes in only if the
        reply brings him in.
        """
        if _THE_ROOM.search(text or ""):
            return list(self.members)
        first = None
        for member in self.members:
            for name in {member.contact.name, member.contact.full_name}:
                found = re.search(rf"\b{re.escape(name)}\b", text or "", re.I)
                if found and (first is None or found.start() < first[0]):
                    first = (found.start(), member)
        return [first[1]] if first else []

    def turn(self, prompt, interrupted=False, confidence=1.0):
        """One operator line, answered by whoever it was for. A generator of events."""
        prompt = (prompt or "").strip()
        if not prompt:
            return
        yield events.message("user", prompt)

        speakers = self.addressed(prompt) or [self.last_speaker or self.members[0]]
        operator_line = f"{self.operator}: {prompt}"
        self.transcript.append(operator_line)
        # Everyone not answering first hears the line now; the first speaker
        # gets it as their own prompt.
        for member in self.members:
            if member is not speakers[0]:
                member.heard.append(operator_line)

        spoken = set()
        queue = [(member, i > 0) for i, member in enumerate(speakers)]
        turns = 0
        # "Everyone, quick status" is everyone: four on the line is four turns.
        most = max(MAX_TURNS, len(speakers))
        while queue and turns < most:
            member, follow_up = queue.pop(0)
            if member not in self.members or member in spoken:
                continue
            reply = yield from self._speak(member, prompt, interrupted, confidence, follow_up)
            turns += 1
            spoken.add(member)
            if not reply:
                continue
            self.last_speaker = member
            line = f"{member.contact.full_name}: {reply}"
            self.transcript.append(line)
            for other in self.others(member):
                other.heard.append(line)
            # Someone named or asked in that reply may come in — once.
            if not queue and turns < most:
                for other in self.addressed(reply):
                    if other is not member and other not in spoken:
                        queue.append((other, True))
                        break

    def text_turn(self, member, body):
        """
        He texted one person on the call. Only they saw it; they answer out
        loud, and everyone hears the answer — not the text.
        """
        reply = yield from self._speak(member, body, False, 1.0, False, via="text_on_call")
        if reply:
            self.last_speaker = member
            line = f"{member.contact.full_name}: {reply}"
            self.transcript.append(line)
            for other in self.others(member):
                other.heard.append(line)

    def _speak(self, member, prompt, interrupted, confidence, follow_up, via=None):
        """Run one contact's turn, tagging every event with who said it."""
        reply = ""
        for event in member.ask(prompt, interrupted=interrupted, confidence=confidence,
                                follow_up=follow_up, via=via):
            event = {**event, "speaker": member.contact.id}
            if event["type"] == "reply_end" and not event.get("interim"):
                reply = event["text"]
            yield event
        return reply

    def greet(self, newcomer, note="", willing=False):
        """
        Someone has just been added. They greet the call already in progress,
        and everyone else hears it.
        """
        instruction = (
            "[REFERENCE — context only]\n" + self.note_for(newcomer) + "\n[END REFERENCE]\n\n"
            f"You've just been patched into a call that's already going, with "
            f"{', '.join([self.operator] + [m.contact.full_name for m in self.others(newcomer)])}. "
            "Greet them your own way, the way you'd join a call in progress — one "
            "line, and not an apology for being late."
            + ("" if willing else " If you'd genuinely rather not be on this call at all — and "
               "only then — say so in your own words and end with [hang up].")
            + (f" ({note})" if note else ""))
        others = " and ".join(m.contact.full_name for m in self.others(newcomer))
        line = yield from self._speak_aside(newcomer, instruction,
                                            f"You're on with {others} now.", greeting=True)
        if line:
            self._heard_by_all(newcomer, line)
            if not newcomer._hanging_up:
                yield from self.answered(newcomer, line)
        return line

    def farewell(self, member):
        """A contact being let go says a quick goodbye before leaving the call."""
        instruction = (
            "[REFERENCE — context only]\n" + self.note_for(member) + "\n[END REFERENCE]\n\n"
            "He's letting you drop off the call. Say a quick goodbye — one line.")
        line = yield from self._speak_aside(member, instruction, "You can drop off now.",
                                            farewell=True)
        if line:
            self._heard_by_all(member, line)

    def react(self, member, what_happened, chase=False):
        """
        Someone else on the call reacts to a change — a newcomer, someone who
        didn't pick up, someone leaving — in a line, or not at all.
        """
        instruction = (
            "[REFERENCE — context only]\n" + self.note_for(member) + "\n[END REFERENCE]\n\n"
            f"{what_happened} React the way you would, in a line — or if you wouldn't say "
            "anything, reply with exactly SKIP."
            + (" If you'd chase them yourself — text them, ring them — say so and end with "
               "[nudge]." if chase else ""))
        line = yield from self._speak_aside(member, instruction)
        if line:
            self._heard_by_all(member, line)
            yield from self.answered(member, line)
        return line

    def lull(self, member, quiet_for=1):
        """
        Nobody's said anything for a moment. On a call with company somebody
        usually fills it — ribbing someone, picking a thread back up, asking
        the operator something — and sometimes the quiet just sits.
        """
        line = yield from self._speak_aside(member, self._lull_instruction(member, quiet_for))
        if line:
            self._heard_by_all(member, line)
            yield from self.answered(member, line)
        return line

    def _lull_instruction(self, member, quiet_for):
        others = ", ".join(m.contact.full_name for m in self.others(member))
        return (
            "[REFERENCE — context only]\n" + self.note_for(member) + "\n[END REFERENCE]\n\n"
            f"There's a pause on the call{' again' if quiet_for > 1 else ''} — nobody has said "
            f"anything for a few seconds. You're on with {self.operator} and {others}. If you'd "
            "fill it, say what you'd actually say: to one of them by name, about what's been said "
            "or what's going on with you, or a question for him. A line or two. If you'd let the "
            "quiet sit, reply with exactly SKIP."
            + (" He's said nothing for a while now; you might ask if he's still there." if quiet_for >= 3
               else ""))

    def collide(self, first, second, quiet_for=1):
        """
        Two of them start into the same pause. A few words each, both stop;
        one of them sorts it out — "sorry, go on" or "no, me first", in their
        own way — and whoever ends up with the floor says their piece. Rare,
        as it is on a real call. Falls back to an ordinary lull if either of
        them wouldn't have said anything anyway.
        """
        a = self._own(first, first.draft(self._lull_instruction(first, quiet_for)))
        b = self._own(second, second.draft(self._lull_instruction(second, quiet_for)))
        if not (a and b):
            speaker, line = (first, a) if a else (second, b)
            if not line:
                return ""
            yield from self._tagged(speaker, speaker.utter(line))
            self._heard_by_all(speaker, line)
            yield from self.answered(speaker, line)
            return line
        start_a, start_b = _opening(a), _opening(b)
        yield from self._tagged(first, first.utter(start_a, cut_off=True))
        yield from self._tagged(second, second.utter(start_b, cut_off=True))
        self._heard_by_all(first, start_a)
        self._heard_by_all(second, start_b)
        instruction = (
            "[REFERENCE — context only]\n" + self.note_for(second) + "\n[END REFERENCE]\n\n"
            f"You and {first.contact.name} both started talking at the same moment — they got as "
            f"far as \"{start_a}\", you as far as \"{start_b}\", and you both stopped. Sort it out "
            "the way you would, in a few words: let them go first and end with [yield], or go "
            f"ahead and say what you were going to say (it was: \"{b}\").")
        sorted_out = yield from self._speak_aside(second, instruction)
        if sorted_out:
            self._heard_by_all(second, sorted_out)
        if not getattr(second, "_yielding", False):
            return sorted_out
        second._yielding = False
        yield from self._tagged(first, first.utter(a))
        self._heard_by_all(first, a)
        yield from self.answered(first, a)
        return a

    def _own(self, member, text):
        from . import guards  # noqa: PLC0415 — guards imports party indirectly
        return " ".join(x for x in (self.own_words(member, y) for y in guards.split_sentences(text or "")) if x)

    def _tagged(self, member, generator):
        for event in generator:
            yield {**event, "speaker": member.contact.id}

    def answered(self, speaker, line):
        """
        Someone named in a line nobody prompted — a greeting, a reaction, a
        remark in a lull — answers it, once. "Dick, what did I miss?" from
        someone just patched in otherwise hung in the air until he spoke.
        Only a name counts here: "hey, guys" is a greeting, not a question.
        """
        self.aside_answered = False
        target = self._named(line, speaker)
        if target is None:
            return
        reply = yield from self._speak(target, line, False, 1.0, True)
        if reply:
            self.last_speaker = target
            self.aside_answered = True
            self._heard_by_all(target, reply)

    def _named(self, text, speaker):
        """Whoever else on the call is named first in it, or None."""
        first = None
        for member in self.others(speaker):
            for n in {member.contact.name, member.contact.full_name}:
                found = re.search(rf"\b{re.escape(n)}\b", text or "", re.I)
                if found and (first is None or found.start() < first[0]):
                    first = (found.start(), member)
        return first[1] if first else None

    def _heard_by_all(self, member, line):
        self.transcript.append(f"{member.contact.full_name}: {line}")
        for other in self.others(member):
            other.heard.append(f"{member.contact.full_name}: {line}")

    def _speak_aside(self, member, instruction, prompted_by=None, greeting=False, farewell=False):
        text = ""
        for event in member.say(instruction, prompted_by, greeting=greeting, farewell=farewell):
            event = {**event, "speaker": member.contact.id}
            if event["type"] == "reply_end":
                text = event["text"]
            yield event
        return text
