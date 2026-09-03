"""
Deterministic post-processing.

These enforce the things a system prompt asks for but cannot reliably deliver.
They are pure functions over text — no I/O, no model calls — which makes them
the cheapest part of the project to test.
"""
import difflib
import re

# Sentences a contact should never end on unless the operator signalled they
# are leaving. Anchored so they only match an actual farewell: an earlier
# version used bare prefixes like `take care\b.*`, which silently deleted
# legitimate lines such as "Take care of the deployment first."
SIGNOFF_PATTERNS = [
    r"sleep well\b.*",
    r"get some (rest|sleep)\b.*",
    r"rest up[.!]?$",
    r"rest easy[.!]?$",
    r"good ?night\b.*",
    r"take care[.!]?$",
    r"take care of yourself\b.*",
    r"catch up (on|with) (your )?sleep\b.*",
    r"turn in (early|for the night)\b.*",
    r"off to bed\b.*",
]

# Cues that the operator is leaving, which is the only thing that licenses a
# farewell. Matched on word boundaries: a bare substring test read "the night
# shift was brutal" and "I finished at midnight" as goodbyes, which quietly
# unlocked the sign-off the guards exist to suppress.
LEAVING_CUES = [
    r"\bbye\b",
    r"\bgood ?night\b",
    r"\bnight\b[\s!.]*$",        # "night" is a farewell only as a parting word
    r"\bgoing to (bed|sleep)\b",
    r"\bgonna sleep\b",
    r"\boff to bed\b",
    r"\bi'?m tired\b",
    r"\bheading off\b",
    r"\bsee you\b",
    r"\btalk later\b",
    r"\blogging off\b",
    r"\bturning in\b",
]

GREETING_PATTERNS = [
    r"good ?morning\b.*",
    r"good ?afternoon\b.*",
    r"good ?evening\b.*",
    r"morning again\b.*",
    r"morning\b[.,]?$",
    r"hello again\b.*",
    r"welcome back\b.*",
    r"back again\b.*",
]

# Replies shorter than this (in words) are exempt from the repetition guard.
# A terse persona is *meant* to reuse "Mm." and "Go on."; flagging those as
# loops fought the character rather than helping it.
REPETITION_MIN_WORDS = 4


def split_sentences(text):
    return re.split(r'(?<=[.!?])\s+', (text or "").strip())


def user_is_leaving(prompt):
    lowered = (prompt or "").lower()
    return any(re.search(cue, lowered) for cue in LEAVING_CUES)


def strip_signoffs(text):
    """Remove trailing sleep/goodbye sentences tacked on uninvited."""
    sentences = split_sentences(text)
    while sentences:
        last = sentences[-1].strip().lower()
        if any(re.match(pattern, last) for pattern in SIGNOFF_PATTERNS):
            sentences.pop()
        else:
            break
    cleaned = " ".join(sentences).strip()
    return cleaned if cleaned else "Mm."


def strip_regreeting(text):
    """Drop a leading re-greeting so nobody says 'Good morning' twice."""
    sentences = split_sentences(text)
    if sentences:
        first = sentences[0].strip().lower()
        if any(re.match(pattern, first) for pattern in GREETING_PATTERNS):
            sentences.pop(0)
    cleaned = " ".join(sentences).strip()
    return cleaned if cleaned else text  # never return empty


def cap_length(text, max_sentences):
    """
    Safety net against runaway word-salad, not a style tool. Only replies past
    the cap are trimmed, so the model still varies its own length.
    """
    sentences = split_sentences(text)
    if len(sentences) <= max_sentences:
        return text
    trimmed = " ".join(sentences[:max_sentences]).strip()
    return trimmed if trimmed else text


def too_similar(candidate, recent, threshold=0.75):
    """
    True if the candidate echoes a recent reply, reuses the same opening words,
    or ends on the same trailing phrase — the signatures of a loop.
    """
    cand = (candidate or "").strip().lower()
    words = cand.split()
    if len(words) < REPETITION_MIN_WORDS:
        return False

    opening = " ".join(words[:4])
    closing = " ".join(words[-5:])
    for previous in recent:
        prev = (previous or "").strip().lower()
        prev_words = prev.split()
        if difflib.SequenceMatcher(None, cand, prev).ratio() >= threshold:
            return True
        if opening and opening == " ".join(prev_words[:4]):
            return True
        if len(words) >= 5 and closing and closing == " ".join(prev_words[-5:]):
            return True
    return False


def apply(text, prompt, max_sentences, already_greeted):
    """
    The full guard stack. Order matters: sign-offs first (tail), then the
    greeting (head), then the runaway cap.
    """
    if not user_is_leaving(prompt):
        text = strip_signoffs(text)
    if already_greeted:
        text = strip_regreeting(text)
    return cap_length(text, max_sentences)
