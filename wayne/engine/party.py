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
import time

from .. import events
from .. import operator as wayne_operator

MAX_CONTACTS = 4
# Contact turns per operator turn: the people he addressed, plus one follow-up.
MAX_TURNS = 3
# How often, after the people he spoke to have answered, someone else jumps in.
CHIME = 0.5

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
        self._last_operator_line = ""     # his latest line — who he named in it gets answered first
        # When someone on the line last put something to him: the call waits a
        # moment for his answer before carrying on without him.
        self.asked_him_at = 0.0

    # --- membership ------------------------------------------------------

    def join(self, session):
        if session in self.members or len(self.members) >= MAX_CONTACTS:
            return False
        if not self.transcript and self.members:
            # Until now this was a one-to-one call, which kept no transcript of
            # its own: what's been said on *this call* is what was said — not
            # the first contact's history, which held their private texts.
            first = self.members[0]
            for message in first.call_messages()[-6:]:
                who = self.operator if message["role"] == "user" else first.contact.full_name
                self.transcript.append(f"{who}: {message['content']}")
        self.members.append(session)
        session.mark_call_start()
        # The last few lines before they joined, marked as such: enough to
        # pick up the thread, not a recording of the evening — and none at all
        # for someone outside the secret, who'd have heard nothing of it anyway.
        from .groupchat import outside
        before = [] if outside(session.contact.id) else self.transcript[-6:]
        session.heard = [f"(before you joined) {line}" for line in before]
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
        room = self.in_the_room(session)
        if room:
            note += (f" {' and '.join(room)} {'is' if len(room) == 1 else 'are'} right there with you — the same room, "
                     "not just the same line: you hear each other directly, see what each other sees, and whatever "
                     "happens where you are happens to you both.")
        if follow_up:
            note += (" He didn't ask you directly; you're coming in because of what was "
                     "just said to you or about you. Answer that, briefly.")
        elif session in self.addressed(self._last_operator_line):
            # Asked "Dick, where are you exactly?", Dick answered Tim's joke about cereal.
            note += (" He's just spoken to you by name: answer what he said to you first — anything "
                     "for the others after, if at all.")
        return note

    def in_the_room(self, session):
        """Who else on the call is actually with them — the same place, not just the same line."""
        from . import presence
        _, group = presence.together(presence.of(session.contact))
        with_them = {p.contact.id for p in group}
        return [m.contact.name for m in self.others(session) if m.contact.id in with_them]

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
        # Their own line, labelled the way the transcript labels everyone else's:
        # "You: It's not cereal, Tim" was read out, label and all.
        sentence = re.sub(r"^\W*(?:you|me|myself)\s*:\s*", "", sentence, flags=re.I)
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

    def turn(self, prompt, interrupted=False, confidence=1.0, talked_over=""):
        """
        One operator line, answered by whoever it was for. A generator of
        events. Talking over someone, he's answering them — unless he names
        someone else — whoever the model had got round to writing next.
        """
        prompt = (prompt or "").strip()
        if not prompt:
            return
        yield events.message("user", prompt)

        over = next((m for m in self.members if m.contact.id == talked_over), None) if interrupted else None
        speakers = self.addressed(prompt) or [over or self.last_speaker or self.members[0]]
        self._last_operator_line = prompt
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
            self._notice_asks(reply)
            # Someone named or asked in that reply may come in — once.
            if not queue and turns < most:
                for other in self.addressed(reply):
                    if other is not member and other not in spoken:
                        queue.append((other, True))
                        break
        # Then the room: nobody asked them, but they heard it. One may jump in
        # — agree, argue, rib someone — and whoever they name answers back.
        # Likelier the more of them there are, less likely each time.
        chance = CHIME * min(1.0, 0.55 + 0.25 * (len(self.members) - 1))
        extra = 0
        while extra < 2 and len(self.members) > 1 and random.random() < chance:
            others = [m for m in self.members if m is not self.last_speaker]
            weights = [(0.3 + (getattr(m.contact, "initiative", None) or {}).get("per_day", 0.5) * 0.5)
                       * (1.6 if m not in spoken else 1.0) for m in others]
            member = random.choices(others, weights=weights)[0]
            line = yield from self.chime(member)
            if not line:
                break
            spoken.add(member)
            extra += 1
            chance *= 0.5

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

    def chime(self, member):
        """Jumping in on what's just been said, unasked — or staying out of it."""
        instruction = (
            "[REFERENCE — context only]\n" + self.note_for(member) + "\n[END REFERENCE]\n\n"
            "Nobody asked you anything, but you're on this call and you heard that. If you'd "
            "jump in — agree, argue, rib someone, add what you know, ask someone something — "
            "say it in a line, to whoever it's for. If you'd stay out of it, reply with exactly SKIP.")
        line = yield from self._speak_aside(member, instruction)
        if line:
            self.last_speaker = member
            self._heard_by_all(member, line)
            yield from self.answered(member, line)
        return line

    def ringing(self, member, name):
        """He's ringing someone into the call; while it rings, someone may say so."""
        instruction = (
            "[REFERENCE — context only]\n" + self.note_for(member) + "\n[END REFERENCE]\n\n"
            f"He's ringing {name} into this call right now — it's still ringing. If you'd say "
            f"something about that — about {name}, or about him bringing them in — say it in a "
            "few words, your way. If you wouldn't, reply with exactly SKIP.")
        line = yield from self._speak_aside(member, instruction)
        if line:
            self._heard_by_all(member, line)
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
            f"The call's going — you're on with {self.operator} and {others}, and the last line has "
            "just landed. If you'd come in now, do it the way you would with these people: answer or "
            "rib whoever spoke, disagree, pick up a thread, ask one of them something by name, or turn "
            "to him. Keep it to what one person says before someone else talks — often a few words. "
            "If you'd leave it, or it's run its course, reply with exactly SKIP."
            + (" He hasn't said anything for a while; someone might check he's still there — or you "
               "carry on without him." if quiet_for >= 3 else ""))

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
        reply = yield from self._speak(target, line, False, 1.0, True, via="from_contact")
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
        self._notice_asks(line)

    def event(self, what):
        """
        Something that happened on the call rather than was said — who didn't
        pick up, who joined, who left — in everyone's ears, and in what anyone
        joining later is handed: whoever picks up a group ring knows who
        declined it, instead of asking after them.
        """
        line = f"({what})"
        self.transcript.append(line)
        for member in self.members:
            member.heard.append(line)

    def _notice_asks(self, line):
        """A line that puts something to him by name — the call holds a moment for his answer."""
        first = self.operator.split()[0]
        if "?" in (line or "") and re.search(rf"\b({re.escape(first)}|{re.escape(self.operator)})\b", line, re.I):
            self.asked_him_at = time.time()

    def waiting_on_him(self, within=7.0):
        """Whether someone's just asked him something and the call should give him a moment."""
        return time.time() - self.asked_him_at < within

    def _speak_aside(self, member, instruction, prompted_by=None, greeting=False, farewell=False):
        # What's been said since they last spoke — or a reaction, a remark into a
        # pause, a jump-in, answers something it never heard.
        from .session import capped
        heard = member.heard[-8:]
        heard_all = "\n".join(capped(member.heard))
        if heard:
            instruction = ("What's been said on the call since you last spoke:\n" + "\n".join(heard)
                           + "\n\n" + instruction)
        member.heard = []
        # What they say is remembered against what they'd heard, as one exchange.
        prompted_by = "\n".join(p for p in (heard_all, prompted_by) if p) or None
        text = ""
        for event in member.say(instruction, prompted_by, greeting=greeting, farewell=farewell):
            event = {**event, "speaker": member.contact.id}
            if event["type"] == "reply_end":
                text = event["text"]
            yield event
        if not text and heard_all and hasattr(member.history, "record_exchange"):
            member.history.record_exchange(heard_all, "Mm.")     # heard, said nothing
        return text
