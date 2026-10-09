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
import re

from .. import events
from .. import operator as wayne_operator

MAX_CONTACTS = 3
# Contact turns per operator turn: the people he addressed, plus one follow-up.
MAX_TURNS = 3

_THE_ROOM = re.compile(
    r"\b(both of you|you both|you two|the two of you|all of you|you all|everyone|"
    r"everybody|guys|you lot)\b", re.I)


class Call:
    def __init__(self, operator=None):
        self.operator = operator or wayne_operator.full_name()
        self.members = []          # ContactSession, in the order they joined
        self.last_speaker = None
        # Everything said on the call, labelled — so whoever joins late is
        # handed what they missed instead of arriving blind.
        self.transcript = []

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
        # The last few lines before they joined, marked as such: enough to
        # pick up the thread, not a recording of the evening.
        session.heard = [f"(before you joined) {line}" for line in self.transcript[-6:]]
        self._attach()
        return True

    def leave(self, session):
        if session not in self.members:
            return
        self.members.remove(session)
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
                "this call is about what's being said now.")
        if follow_up:
            note += (" He didn't ask you directly; you're coming in because of what was "
                     "just said to you or about you. Answer that, briefly.")
        return note

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
        while queue and turns < MAX_TURNS:
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
            if not queue and turns < MAX_TURNS:
                for other in self.addressed(reply):
                    if other is not member and other not in spoken:
                        queue.append((other, True))
                        break

    def _speak(self, member, prompt, interrupted, confidence, follow_up):
        """Run one contact's turn, tagging every event with who said it."""
        reply = ""
        for event in member.ask(prompt, interrupted=interrupted, confidence=confidence,
                                follow_up=follow_up):
            event = {**event, "speaker": member.contact.id}
            if event["type"] == "reply_end" and not event.get("interim"):
                reply = event["text"]
            yield event
        return reply

    def greet(self, newcomer):
        """
        Someone has just been added. They greet the call already in progress,
        and everyone else hears it.
        """
        instruction = (
            "[REFERENCE — context only]\n" + self.note_for(newcomer) + "\n[END REFERENCE]\n\n"
            f"You've just been patched into a call that's already going, with "
            f"{', '.join([self.operator] + [m.contact.full_name for m in self.others(newcomer)])}. "
            "Greet them your own way, the way you'd join a call in progress — one "
            "line, and not an apology for being late.")
        others = " and ".join(m.contact.full_name for m in self.others(newcomer))
        line = yield from self._speak_aside(newcomer, instruction,
                                            f"You're on with {others} now.")
        if line:
            self.transcript.append(f"{newcomer.contact.full_name}: {line}")
            for other in self.others(newcomer):
                other.heard.append(f"{newcomer.contact.full_name}: {line}")

    def farewell(self, member):
        """A contact being let go says a quick goodbye before leaving the call."""
        instruction = (
            "[REFERENCE — context only]\n" + self.note_for(member) + "\n[END REFERENCE]\n\n"
            "He's letting you drop off the call. Say a quick goodbye — one line.")
        line = yield from self._speak_aside(member, instruction, "You can drop off now.")
        if line:
            self.transcript.append(f"{member.contact.full_name}: {line}")
            for other in self.others(member):
                other.heard.append(f"{member.contact.full_name}: {line}")

    def _speak_aside(self, member, instruction, prompted_by=None):
        text = ""
        for event in member.say(instruction, prompted_by):
            event = {**event, "speaker": member.contact.id}
            if event["type"] == "reply_end":
                text = event["text"]
            yield event
        return text
