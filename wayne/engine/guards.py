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


def echoes(text, recently_spoken, threshold=0.62):
    """
    True if a transcript looks like the contact's own voice coming back.

    Echo cancellation is imperfect: a reply leaves the speakers, the microphone
    hears it, speech-to-text renders it as the operator, and the contact
    answers what it just said — a loop that sustains itself indefinitely once
    started. Thresholds alone cannot close this, because on a bad take the echo
    genuinely is louder than the room.

    So the last line of defence is semantic rather than acoustic: whatever the
    contact just said is compared against what supposedly just arrived. The
    threshold is deliberately loose, since transcription of speaker output is
    lossy — it will be a mangled version of the reply, not a copy of it.
    """
    candidate = (text or "").strip().lower()
    if len(candidate.split()) < 3:
        # Too short to attribute confidently, and dropping "yes" or "go on"
        # because it resembled a reply would be worse than the occasional echo.
        return False

    for spoken in recently_spoken:
        previous = (spoken or "").strip().lower()
        if not previous:
            continue
        if difflib.SequenceMatcher(None, candidate, previous).ratio() >= threshold:
            return True
        # A short echo of a long reply scores low overall but is near-identical
        # to the fragment it came from, so check containment both ways.
        shorter, longer = sorted((candidate, previous), key=len)
        if len(shorter) >= 18 and shorter in longer:
            return True
    return False


def count_repeats(prompt, previous_prompts, threshold=0.78):
    """
    How many times the operator has already said essentially this.

    Not for suppressing anything — for *noticing*. A person you have told the
    same thing to three times says so, and a contact who cannot tell is
    obviously not listening. Paraphrase counts: "I should call her" and "I
    ought to call her" are the same admission twice.
    """
    candidate = (prompt or "").strip().lower()
    if len(candidate.split()) < 3:
        # "yes", "go on", "ok" recur constantly and mean nothing by recurring.
        return 0
    return sum(
        1 for previous in previous_prompts
        if difflib.SequenceMatcher(None, candidate, (previous or "").strip().lower())
        .ratio() >= threshold
    )


def strip_forbidden_address(text, terms):
    """
    Remove forms of address a character would never use.

    A system prompt can say "never call him lad" and a large model will mostly
    obey; a smaller, faster one will not, and one slip is enough to break the
    illusion the whole console exists to sustain. Only clearly vocative uses
    are removed — the term adjacent to a comma or ending a sentence — so
    ordinary occurrences of the same word survive.
    """
    if not terms:
        return text
    for term in terms:
        word = re.escape(term)
        # ", lad." / ", lad?"  ->  "."
        text = re.sub(rf",\s*{word}\b(?=\s*[.!?,]|$)", "", text, flags=re.I)
        # "Lad, ..." at the start of a sentence — the word that follows has to
        # be re-capitalised, or removing the vocative leaves a lowercase start.
        text = re.sub(
            rf"(^|(?<=[.!?]\s)){word}\s*,\s*(\w)",
            lambda m: m.group(1) + m.group(2).upper(),
            text, flags=re.I,
        )
        # " ... lad." with no comma, still clearly a vocative at the end
        text = re.sub(rf"\s+{word}\b(?=\s*[.!?]|$)", "", text, flags=re.I)
    return re.sub(r"\s{2,}", " ", text).strip()


def apply(text, prompt, max_sentences, already_greeted, forbidden_address=()):
    """
    The full guard stack. Order matters: sign-offs first (tail), then the
    greeting (head), then the runaway cap.
    """
    if forbidden_address:
        text = strip_forbidden_address(text, forbidden_address)
    if not user_is_leaving(prompt):
        text = strip_signoffs(text)
    if already_greeted:
        text = strip_regreeting(text)
    return cap_length(text, max_sentences)
