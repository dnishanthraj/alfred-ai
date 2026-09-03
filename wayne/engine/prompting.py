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


def reference_block(vault_block, prompt, search_context=""):
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

    parts.append(SPEECH_CONSTRAINT)
    return "\n\n".join(parts)


def compose_user_turn(prompt, vault_block, search_context=""):
    """Wrap the prompt with fenced context. The actual message comes last."""
    context = reference_block(vault_block, prompt, search_context)
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


def boot_prompt(contact, returning, since_last=""):
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
    return (
        "[REFERENCE — context only]\n"
        f"Time: {time_context()}.{'' if returning else ' Fresh session.'}{gap}\n"
        f"{SPEECH_CONSTRAINT}\n"
        "[END REFERENCE]\n\n"
        f"{instruction}"
    )
