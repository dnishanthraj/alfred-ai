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
"""
import time

from .. import config

# Replies are spoken aloud, so anything that only works on a page — markup,
# bullets, spelled-out URLs — is actively harmful here.
SPEECH_CONSTRAINT = (
    "This reply will be read aloud by a speech synthesiser. Write it as spoken "
    "English: no markdown, no bullet points, no numbered lists, no emoji, no "
    "URLs, no code. Say numbers and dates the way a person would say them."
)

# The single biggest tell that something is a machine is that every reply is
# the same length. Real speech is wildly uneven: mostly short, occasionally a
# single syllable, and now and then a genuine paragraph when the subject earns
# one. Models default to a comfortable middle and stay there, so the shape of
# the distribution has to be described explicitly — and, more importantly,
# demonstrated in the primer, which does most of the actual work.
LENGTH_GUIDANCE = (
    "Length is not fixed. Match the moment:\n"
    "- Most turns are one or two sentences. This is the default.\n"
    "- Often a fragment is right: \"Mm.\" \"Go on.\" \"Doubtful.\" \"And?\" "
    "A single word is a complete reply when nothing more is needed.\n"
    "- When he is evasive, brief, or testing you, be briefer than he is.\n"
    "- When he is genuinely struggling, has asked you something that deserves "
    "an answer, or is about to do something foolish, take the room you need — "
    "four, six sentences, a proper argument. Do not ration yourself then.\n"
    "Never pad. Never summarise what you just said. Never close with an offer "
    "of further help. Stop the moment you are finished, even mid-thought."
)


def register_hint(prompt):
    """
    A nudge toward matching the operator's register.

    People mirror each other: a three-word question gets a short answer, a
    paragraph gets engagement. Stating this per-turn, with the actual shape of
    what he just said, moves length far more reliably than a static rule — the
    model can see what it is matching.
    """
    words = len((prompt or "").split())
    if words <= 3:
        return "He said very little. Answer in kind — a word or a short line."
    if words <= 25:
        return "Conversational turn. A sentence or two, unless it warrants more."
    return "He has said a good deal. Engage with it properly rather than acknowledging it."


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


def reference_block(vault_block, prompt, search_context="", awareness=()):
    parts = [
        f"Current time: {time_context()}\n"
        f"(Factual context only. Do not infer what {config.USER_NAME} has been doing, "
        f"is about to do, or should do based on this.)"
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

    parts.append(SPEECH_CONSTRAINT)
    parts.append(LENGTH_GUIDANCE)
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
    """Assemble the full message list for one generation."""
    messages = []
    if contact.system:
        messages.append({"role": "system", "content": contact.system})
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
        f"{SPEECH_CONSTRAINT}\n"
        "[END REFERENCE]\n\n"
        f"{instruction}"
    )
