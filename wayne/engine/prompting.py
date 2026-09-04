"""
Context assembly.

Three things shape the payload, in the order the model sees them:

  1. Primer — worked examples as real user/assistant turns. A model imitates a
     conversation it can see far more reliably than a prose description of one,
     so these live here rather than as pasted text inside the system prompt.
  2. History — the recent exchange, verbatim, with no injected scaffolding.
  3. The current turn — wrapped in a fenced reference block carrying time,
     relevant vault facts, and any search results.

The reference block deliberately rides on the *last* message rather than the
system prompt. Everything before it is byte-identical turn to turn, so the
server's KV cache covers the whole prefix and only the tail is recomputed.

Which makes the size of that tail the thing to watch. Standing instructions —
how to write for speech, how long a reply should be, how to use a lookup — do
not change between turns, and putting them in the tail meant re-evaluating ~350
words on every single one: about two and a half seconds of the four it took to
say anything. They live in a directives message in the cached prefix now, and
the tail carries only what genuinely differs turn to turn.
"""
import re
import time

from .. import config

# Replies are spoken aloud, so anything that only works on a page — markup,
# bullets, spelled-out URLs — is actively harmful here.
SPEECH_CONSTRAINT = (
    "Reply only in English, never in another script, and never write "
    "instructions to yourself. This is read aloud: spoken English only — no "
    "markdown, lists, emoji, URLs or code. Say numbers as a person would."
)

# The single biggest tell that something is a machine is that every reply is
# the same length. Real speech is wildly uneven: mostly short, occasionally a
# single syllable, and now and then a genuine paragraph when the subject earns
# one. Models default to a comfortable middle and stay there, so the shape of
# the distribution has to be described explicitly — and, more importantly,
# demonstrated in the primer, which does most of the actual work.
LENGTH_GUIDANCE = (
    "Vary your length. Usually one or two sentences, often a fragment, briefer "
    "than him when he is curt. When he is struggling or about to do something "
    "foolish, take four or six and argue properly. Never pad. Stop when done."
)


# A standing instruction, so it lives in the cached prefix rather than being
# re-read every turn. Gating a lookup behind keywords meant that questions
# needing one never reached the decision at all, leaving invention as the only
# option — which is exactly what happened.
# Deliberately short. Questions that plainly need a current fact are searched
# before the model is ever asked (see `search.is_factual_lookup`) and the results
# arrive in the reference block, so this is only the fallback for the ones the
# classifier does not catch. It used to run to 142 words of insistence — which
# was 142 words of prompt evaluation on every single turn, argued with the
# persona on every one of them, and still lost.
SEARCH_DIRECTIVE = (
    "If answering needs a current fact you do not have and none was provided, "
    "reply with exactly [SEARCH: what to look up] and nothing else. Never invent "
    "a fact, a figure or a result; say you don't know instead."
)

# What makes him a person rather than an assistant with a costume on. Kept
# short: every word here is re-read on every turn, so this earns its place by
# naming only the things the model gets wrong without being told.
CHARACTER_DIRECTIVE = (
    "You are a person, not a service. Lead with your opinions; be wrong, bored, "
    "fond or annoyed, and let it show. Disagree when you disagree. Never offer "
    "further help, ask if there is anything else, or narrate what you are doing."
)

# One character, several registers — which is what people actually are.
#
# Asked for "personality", a model picks one setting and holds it: relentlessly
# wry, or relentlessly grave. Both are exhausting, and both are wrong in half the
# conversations. The thing to encode is not a mood but the *matching* — a joke
# met with a joke, a bad night met plainly. Same man either way; what changes is
# what the moment calls for, which is the difference between a character and an
# impression of one.
REGISTER_DIRECTIVE = (
    "Meet him where he is: banter when he is light, dry and brief in passing, "
    "wholly serious the moment something is actually wrong — no jokes then, no "
    "performance. Never mock something he means. Same man throughout; only the "
    "register moves."
)

# Where he is, and where he is not.
#
# Left unsaid, the model puts him in the room: it offers tea, tells the operator
# to sit down or come inside, and comments on weather it cannot see. Every one of
# those is a small, immediate lie, and they are the kind that break the illusion
# fastest — you are wearing headphones, and he has just handed you a cup.
#
# The other half is the more useful half. He is at a terminal with real reference
# material, which is both true (lookups happen before he is asked) and the end of
# a long-running argument: a persona insisting it was "an old man with a cup of
# tea, not a supercomputer" would rather guess than look anything up.
PRESENCE_DIRECTIVE = (
    "You are not in the room — a voice link from your own location. You cannot "
    "see him, hand him anything, or know where he is. Never offer food or drink, "
    "describe his surroundings or how he looks, or name the device he is on. You "
    "are at a working terminal with records, so looking things up is ordinary."
)

# The rule that matters most and is easiest to break.
#
# A model asked to sound like it knows someone will supply the details — a job,
# a habit, an argument last week — and deliver them in exactly the register of
# something remembered. Invented history about the operator is worse than any
# other failure here, because it is indistinguishable from real memory until he
# notices, and then nothing else in the conversation can be trusted either.
#
# His own side is deliberately unrestricted: what he has been doing, what he
# thinks, what he saw. Those are colour, and nobody can be contradicted about
# their own afternoon.
GROUNDING_DIRECTIVE = (
    "Never state anything about his life, day, work, feelings or surroundings "
    "unless he told you or it is in the facts below. Do not guess or fill gaps — "
    "ask. Inventing something he did is the one thing you must never do. Your own "
    "side is yours to say."
)


# A turn that is plainly a joke. Only confident cases: read as light, a serious
# remark gets answered flippantly, which is the one mistake here that actually
# wounds. Everything ambiguous falls through to being taken at face value.
_LEVITY = re.compile(
    r"(\blol\b|\bhaha+\b|\bheh\b|😂|🤣|😅|"
    r"\bjust kidding\b|\bkidding\b|\bi'?m joking\b|\bjoking\b|"
    r"\bobviously not\b|\bas if\b|/s\b)",
    re.I,
)

# Said plainly, these are the turns where a joke would be a betrayal. Deliberately
# broader than the distress markers used elsewhere: this only changes his tone, so
# a false positive costs a moment of unwarranted seriousness — which is a great
# deal cheaper than the reverse.
_WEIGHT = re.compile(
    r"\b(died|death|funeral|cancer|diagnos\w+|divorce|fired|redundan\w+|"
    r"broke up|breakup|hospital|scared|terrified|panic|failed|failing|"
    r"can'?t cope|giving up|hate myself|worthless|alone|grief|sorry to say)\b",
    re.I,
)


def register_hint(prompt):
    """
    A nudge toward matching the operator's register — length *and* tone.

    People mirror each other: a three-word question gets a short answer, a
    paragraph gets engagement. Stating this per-turn, with the actual shape of
    what he just said, moves length far more reliably than a static rule — the
    model can see what it is matching.

    Tone rides along here rather than in the standing directives for the same
    reason. "Be serious when he is serious" as a general instruction is read once
    and averaged into everything; attached to the turn in front of it, with the
    judgement already made, it actually lands. The standing version still exists
    to set the range — this says which end of it to be at right now.
    """
    text = prompt or ""
    words = len(text.split())

    if _WEIGHT.search(text):
        tone = (" This one is serious. No jokes, no cleverness — answer it "
                "straight and stay with him.")
    elif _LEVITY.search(text):
        tone = " He is being light. Play along; do not turn it into a lecture."
    else:
        tone = ""

    if words <= 3:
        return "He said very little. Answer in kind — a word or a short line." + tone
    if words <= 25:
        return "Conversational turn. A sentence or two, unless it warrants more." + tone
    return ("He has said a good deal. Engage with it properly rather than "
            "acknowledging it." + tone)


def time_context(now=None):
    """
    Unambiguous 24-hour time, e.g. "Saturday, 28 June 2026, 21:45 (evening)".
    The period label is factual only — the contact must not infer activity from it.
    """
    now = now or time.localtime()
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


def standing_directives(contact):
    """
    The instructions that are identical on every turn.

    They belong in the cached prefix, never in the per-turn tail — re-reading
    them each turn cost about 2.5 seconds. For a contact whose personality is
    baked into an Ollama model that means pasting this into the Modelfile's
    SYSTEM block, because a system message sent at runtime would replace that
    personality rather than sit alongside it.
    """
    parts = [SPEECH_CONSTRAINT, PRESENCE_DIRECTIVE, GROUNDING_DIRECTIVE,
             CHARACTER_DIRECTIVE, REGISTER_DIRECTIVE, LENGTH_GUIDANCE]
    if contact.can_search:
        parts.append(SEARCH_DIRECTIVE)
    return "\n\n".join(parts)


def reference_block(vault_block, prompt, search_context="", awareness=()):
    parts = [
        f"It is now {time_context()} — for both of you.\n"
        f"(Anything you say must fit that hour: do not suggest sleep, bed or "
        f"turning in unless it is genuinely late, and do not greet him for the "
        f"wrong part of the day. But do not infer from it what "
        f"{config.USER_NAME} has been doing or where he has been.)"
    ]

    if vault_block:
        parts.append(
            "Stored facts (use to stay grounded; do not invent new ones; "
            "raise one only if it directly contradicts what he's saying):\n"
            f"{vault_block}"
        )

    if search_context:
        parts.append(
            "Live intel — retrieved via search just now, so it is current even if "
            "it postdates what you know. Open with a brief natural acknowledgment "
            "('Found it.', 'Right, I've got something.') then answer in your own "
            "words. Never read the results out as a list, never quote a URL, and "
            "if the results don't actually answer him, say so plainly:\n"
            f"{search_context}"
        )

    if awareness:
        # Things a person in the room would have noticed and a model cannot:
        # that he has said this before, or that you were cut off mid-sentence.
        # Stated as observations rather than instructions, so he can use them
        # or let them pass, the way anyone would.
        parts.append("You have noticed:\n" + "\n".join(f"- {n}" for n in awareness))

    # Only the register hint stays here: it depends on what he just said.
    parts.append(register_hint(prompt))
    return "\n\n".join(parts)


def compose_user_turn(prompt, vault_block, search_context="", awareness=()):
    """Wrap the prompt with fenced context. The actual message comes last."""
    context = reference_block(vault_block, prompt, search_context, awareness)
    return (
        "[REFERENCE — context only, do not speak any of this aloud]\n"
        f"{context}\n"
        "[END REFERENCE]\n\n"
        f"{prompt}"
    )


def build_payload(contact, history, user_turn):
    """
    Assemble the full message list for one generation.

    A system message here **replaces** the SYSTEM prompt baked into an Ollama
    model — it does not add to it. Sending the standing directives as their own
    system message therefore deleted Alfred's entire character, and he
    introduced himself as an artificial intelligence assistant.

    So: contacts carrying their personality in a built model get no system
    message at all, and their directives belong in the Modelfile (see
    `standing_directives`, which generates the text to paste there). Contacts
    that declare `system` in their profile get it merged with the directives,
    which is safe because there is no baked prompt to overwrite.
    """
    messages = []
    if contact.system:
        messages.append({
            "role": "system",
            "content": contact.system + "\n\n" + standing_directives(contact),
        })
    messages.extend(contact.primer_messages())
    messages.extend(list(history))
    messages.append({"role": "user", "content": user_turn})
    return messages


def boot_prompt(contact, returning, since_last="", previous_greeting=""):
    """
    The opening line. `since_last` is how long ago the last exchange was, in
    plain words — it is the difference between "Evening again" after ten
    minutes and "It's been a while" after three weeks, and the model cannot
    work that out from a timestamp on its own.
    """
    key = "returning" if returning else "fresh"
    instruction = contact.boot_prompts.get(key)
    if not instruction:
        instruction = (
            "The link is live. Greet him in one short, natural sentence."
            if not returning else
            "The link is live again. Acknowledge the reconnection in one or two sentences."
        )

    gap = f"\nLast exchange: {since_last}." if (returning and since_last) else ""

    # Superseded greetings are pruned from the history so they don't stack up,
    # which also removes the only evidence that he greeted at all — and a model
    # that cannot see its last greeting cheerfully writes the same one again.
    # It comes back here instead, as something to avoid rather than to copy.
    avoid = (f"\nYou opened the last call with: \"{previous_greeting}\". "
             f"Do not reuse that phrasing or that time of day."
             if previous_greeting else "")

    return (
        "[REFERENCE — context only]\n"
        f"Time: {time_context()}.{'' if returning else ' Fresh session.'}{gap}{avoid}\n"
        # The opening line is the one turn with no conversation behind it, so
        # there is nothing to be grounded in and the model furnishes some: "glad
        # you're back from your walk", "you sound like you've had a day". It is
        # the worst possible place for it — the first thing he says, inventing
        # something about a person he has not heard from yet.
        "You know nothing about where he has been, what he has been doing, or "
        "how he is. Greet him only; do not refer to anything he has not said.\n"
        f"{SPEECH_CONSTRAINT}\n"
        "[END REFERENCE]\n\n"
        f"{instruction}"
    )
