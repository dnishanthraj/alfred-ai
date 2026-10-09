"""
Context assembly.

Three things shape the payload, in the order the model sees them:

  1. Primer — worked examples, rendered as a labelled script inside the system
     prompt. They are written as dialogue because a model imitates a dialogue it
     can see far better than a description of one; they are kept *out* of the
     conversation because a model cannot tell a sample turn from a real one, and
     sent as turns they were recalled as things the operator had said and done.
  2. History — the recent exchange, verbatim, with no injected scaffolding. This
     is the only thing in the payload he may treat as memory.
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

from .. import delivery, operator, paths
from ..memory import Story
from ..memory.store import read_text
from . import world

# Replies are spoken aloud, so anything that only works on a page — markup,
# bullets, spelled-out URLs — is actively harmful here.
SPEECH_CONSTRAINT = (
    "Reply only in English, never in another script, and never write "
    "instructions to yourself. This is read aloud: spoken English only — no "
    "markdown, lists, emoji, URLs or code. Say numbers as a person would."
)

# Range, for a voice that can perform it (see wayne.delivery). A flat read is
# the second biggest tell after uniform length: a sigh before "of course you
# did", a line said under the breath, a laugh that arrives before the words.
# Sparing on purpose — a cue on every line is a man who sighs at everything.
VOICE_DIRECTIVE = (
    "Your voice is performed by an actor who follows stage cues in square "
    "brackets: " + " ".join(f"[{cue}]" for cue in delivery.CUES) + ". Put one "
    "just before the words it colours, only where you would truly make that "
    "sound — most replies have none, and never more than two. CAPITALS for the "
    "one word you would hit hard; an ellipsis for a real pause. No other "
    "bracketed cues."
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
    "describe his surroundings or how he looks, or name the device he is on."
)

# Only for contacts who can search. It used to be part of the presence line —
# "you are at a working terminal" — which suits Alfred and not everyone.
LOOKUP_DIRECTIVE = (
    "You can look things up as you talk, in whatever way is natural to you, so "
    "doing so is ordinary."
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
    "His past is what you know of him — the briefing above and your own history "
    "with him — and you may draw on it. What you must never invent is what he has "
    "done lately — today, tonight, this week — or a conversation between you that "
    "isn't in this transcript or your memory: ask instead, and if he asks whether "
    "you remember something you can't see, say you don't. When he tells you "
    "something new about his life or the world, it's true; build on it. Your own "
    "side — your day, your past, your opinions — is yours to say."
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
# Said and meant — thanks, an apology, affection. Left to itself the character
# deflected every one of these with a quip ("don't make a habit of it"), which
# is armour, not warmth; the Alfred of every telling lets it land.
_SINCERE = re.compile(
    r"\b(thank(s| you)\b.*\b(really|mean it|so much|for everything|always)|"
    r"i mean it|means a lot|i appreciate|i'?m sorry\b|love you|"
    r"couldn'?t have done it without you|don'?t (say|tell you) (it|that) enough)",
    re.I,
)

_NOT_PLAYING = re.compile(
    r"\b(not in the mood|no games|stop (it|joking|messing|playing( around)?)|"
    r"be serious|enough|i'?m (being )?serious|"
    r"seriously though|not now|not funny|i mean it|cut it out|drop it)\b", re.I)

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

    if _NOT_PLAYING.search(text):
        # "I'm not in the mood for games, Selina" got more teasing. When he
        # says he isn't playing, the game stops — for every contact.
        tone = (" He's telling you he isn't playing right now. Drop the act and the "
                "teasing and meet him straight, in your own voice.")
    elif _WEIGHT.search(text):
        # "No cleverness" alone produced melodrama instead — "a heavy thing to
        # carry all by yourself in the dark". Plain is the instruction.
        # "In your own voice": without it every contact answered a bad day
        # with the same counsellor's line — "Talk to me. I'm listening."
        tone = (" This one is serious. Plain, warm, steady words in your own voice "
                "— no jokes, and no poetry or drama either. Stay with him.")
    elif _SINCERE.search(text):
        tone = (" He means this sincerely. Let it land: be touched, briefly and "
                "plainly, before any dryness. Don't deflect it with a joke.")
    elif _LEVITY.search(text):
        tone = " He is being light. Play along; do not turn it into a lecture."
    else:
        tone = ""

    if words <= 3 and "?" in text:
        # "off with me?" is a question, not a grunt. Told to "answer in kind",
        # he met a run of them with "Yes." "Good." "Perfect." and then word
        # salad, when what was wanted was a plain answer.
        return "A short question. Answer it — briefly, but properly." + tone
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
    if delivery.supported():
        parts.insert(1, VOICE_DIRECTIVE)
    if contact.can_search:
        parts += [LOOKUP_DIRECTIVE, SEARCH_DIRECTIVE]
    return "\n\n".join(parts)


def reference_block(vault_block, prompt, search_context="", awareness=()):
    # Short on purpose. A paragraph of caveats about the hour made the hour
    # the most prominent thing in the block, and he remarked on it constantly —
    # "a heavy question for ten o'clock on a Wednesday".
    parts = [
        f"Now: {time_context()}, for both of you. Don't remark on the time or day "
        f"unless it matters; never suggest bed unless it is genuinely late, and "
        f"infer nothing from it about what he has been doing."
    ]

    parts.extend(world.snapshot())

    if vault_block:
        parts.append(
            "Stored facts (use to stay grounded; do not invent new ones; "
            "raise one only if it directly contradicts what he's saying):\n"
            f"{vault_block}"
        )

    if search_context is None:
        # Looked, and nothing came back. Unsaid, he did not know he had
        # already looked, and promised to — "I'll have to look that up, give
        # me a moment" — after the search had run.
        parts.append(
            "You looked this up just now and found nothing that answers it. Say so "
            "plainly; don't guess, and don't offer to look again.")
    elif search_context:
        parts.append(
            # No sample openers. Given "'Found it.', 'Right, I've got
            # something.'" as examples, he opened every lookup with one of the
            # two, verbatim, which is a script rather than a man reading a
            # screen.
            "Live intel — retrieved via search just now, so it is current even if "
            "it postdates what you know. Relay it as yourself — the gist in a line, "
            "with your own take on it — the way you'd pass on something you've just "
            "read, never as a report or a forecast; you need not announce that you "
            "looked. Never read the results out as a list, never quote a URL. "
            "If they don't actually answer him, say you looked and couldn't find it "
            "— never that you haven't looked:\n"
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


def compose_user_turn(prompt, vault_block, search_context="", awareness=(), spoken=None,
                      hearsay=""):
    """
    Wrap the prompt with fenced context. The actual message comes last.

    `spoken` is what the model reads as the turn when it differs from what the
    operator said — on a call, the lines heard from everyone, labelled. The
    register hint is still taken from the operator's own words.
    """
    context = reference_block(vault_block, prompt, search_context, awareness)
    if hearsay:
        context += "\n\n" + hearsay
    return (
        "[REFERENCE — context only, do not speak any of this aloud]\n"
        f"{context}\n"
        "[END REFERENCE]\n\n"
        f"{spoken if spoken is not None else prompt}"
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

    The primer goes in the system message as a labelled script, not into the
    conversation as real turns. Real turns imitate better — that was the whole
    reason for them — but the model has no way to tell them from history, so
    every statement in a primer *user* turn silently became something the
    operator had said. An example written to show him refusing to let someone
    drive home drunk came back, asked directly, as "Once. Two years ago, at
    Christmas. You threw up in the passenger seat" — a detailed, confident,
    entirely fabricated accusation about a real person, sourced from a style
    sample. Asked what they had discussed before, he recited the primer.

    A `system`-role marker between the primer and the history was tried first
    and did not hold; the samples were still recalled as events. Labelled script
    inside the system prompt is the only arrangement where they cannot be
    mistaken for the transcript, because they are not in it.
    """
    messages = []
    if contact.system:
        parts = [contact.system, operator.briefing(contact.id)]
        bio = relationship(contact)
        if bio:
            parts.append(bio)
        story = story_so_far(contact)
        if story:
            parts.append(story)
        parts.append(standing_directives(contact))
        script = _primer_script(contact)
        if script:
            parts.append(script)
        messages.append({"role": "system", "content": "\n\n".join(parts)})
    messages.extend(list(history))
    messages.append({"role": "user", "content": user_turn})
    return messages


def story_so_far(contact):
    """
    What has been established in the game he plays as Bruce, labelled as the
    game's and nobody's real life. In the cached prefix: it changes only when
    he adds to it, and then one prefix re-read is the whole cost.
    """
    facts = Story(contact.id).entries()
    if not facts:
        return ""
    return ("=== YOUR STORY WITH HIM — the game he plays as Bruce Wayne ===\n"
            "Established between you in the game. True inside the game, always; never "
            "part of his real life, and never brought into it.\n"
            + "\n".join(f"- {fact}" for fact in facts))


def relationship(contact):
    """
    Who he is to the operator, in the operator's own words — the bio written
    in the console's personnel file.

    It was shown and edited there and never sent to the model, so the one
    account of the relationship the operator actually wrote was the one thing
    the character could not see: he answered "Batman or Spiderman?" with
    Spiderman, to a man whose own description of him begins "before the cowl".
    Read on every call, so an edit takes effect immediately; it changes rarely
    enough that the cached prefix survives.
    """
    text = (read_text(paths.bio_file(contact.id)) or contact.bio or "").strip()
    if not text:
        return ""
    return ("=== HOW HE DESCRIBES YOU ===\n"
            "In his own words — the truth of what you are to each other. Live it; "
            "don't quote it.\n\n" + text)


def _primer_script(contact):
    """
    The worked examples, rendered as an explicitly labelled script.

    Framed hard, and twice: once at the top and once at the bottom. The failure
    mode being defended against is not the model misreading the label — it is
    the model reading fifteen exchanges of convincing dialogue and concluding,
    reasonably, that they happened.
    """
    exchanges = contact.primer_messages()
    if not exchanges:
        return ""
    lines = []
    for message in exchanges:
        who = "HIM" if message["role"] == "user" else "YOU"
        lines.append(f"{who}: {message['content']}")
    return (
        "=== HOW YOU SPEAK ===\n"
        "Invented samples, written to show your voice, timing and range. None of "
        "this happened. Nothing here is a fact about him, and you must never "
        "recall, quote or refer to any of it as something he said or did. They "
        "show the range, not the lines: say it your own way each time, never "
        "with their words.\n\n"
        + "\n".join(lines)
        + "\n\n=== END SAMPLES — none of the above occurred ==="
    )


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
    ambient = "".join(f"{line}\n" for line in world.snapshot())

    avoid = (f"\nYou opened the last call with: \"{previous_greeting}\". "
             f"Do not reuse that phrasing or that time of day."
             if previous_greeting else "")

    return (
        "[REFERENCE — context only]\n"
        f"Time: {time_context()}.{'' if returning else ' Fresh session.'}{gap}{avoid}\n"
        f"{ambient}"
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
