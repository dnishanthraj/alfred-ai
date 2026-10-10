"""
Stage directions for the voice.

ElevenLabs v3 and v4 read bracketed cues — [sighs], [whispers], [laughs] — as
direction rather than text: measured on the voice in use, a sigh adds most of a
second of breath, a whisper drops loudness about 15%, a shout raises it about as
much, and none of them were spoken as words. That is the range a person has and
a flat text-to-speech read does not: the same line said tiredly, under the
breath, or through a laugh. Eleven v4 goes further and performs the sounds of a
scene too — a gate creaking, a phone buzzing — so a call can carry a bag
rustling at the checkout as well as the voice.

So the contact may write them, the way an actor's script carries them, and they
travel with a sentence only as far as the synthesiser. The transcript, the
history and every guard see clean text: a cue on screen is a stage direction
read aloud, and a cue in memory teaches the model to scatter more of them.

Only cues the voice can perform reach it — the delivery cues below, and on v4 a
short sound from where they are. Anything else that reads as a direction is
dropped, because an unknown cue is either ignored or — worse — spoken. Older
models read every cue aloud, so for them all of it is dropped.
"""
import re

from . import config

# Trimmed by listening to the transcripts: "snorts" and "curious" turned up on
# nearly every line once offered, and a cue on every line is a man who sighs at
# everything. These are offered on every call.
CUES = ("sighs", "exhales", "laughs", "whispers", "sarcastic", "shouting")

# Offered only when the moment has them (see voice_note): the yawn of someone he
# woke, the breath of someone running a rooftop. Performed whenever written.
SITUATIONAL = ("yawns", "tired", "groggy", "sleepy", "breathless", "panting", "out of breath", "clears throat",
               "chuckles", "hushed", "nervous", "worried", "pained", "gasps", "crying", "angry", "frustrated",
               "excited", "softly", "quietly", "muffled", "grunts")

# A sound from where they are, which v4 performs: a few words, something heard —
# never music, which is a soundtrack, not a scene.
_SOUNDISH = re.compile(
    r"rustl|clink|creak|door|footstep|traffic|siren|wind|rain|thunder|beep|buzz|ring|engine|horn|\bbags?\b|keys|"
    r"glass|cups?\b|crowd|chatter|bark|bird|waves?\b|water|click|knock|tap|clatter|thud|crash|gunshot|explosion|"
    r"whistl|static|crackl|rumbl|hum\b|humming|drip|splash|zip|paper|typing|keyboard|cars?\b|motor|bike|train|"
    r"helicopter|chopper|kettle|sizzl|traffic|announcement|trolley|cart|register|checkout|till|scanner|radio|"
    r"punch|scuffle|grapple|cable|gravel|leaves|dog|cat|bats?\b|alarm|phone|cutlery|pan\b|pot\b|fridge|tv\b|"
    r"television|cheer|applause|clap|laughter in|murmur|shuffl|slam|squeal|screech|brakes|tyres|tires",
    re.I)
_NOT_A_SCENE = re.compile(r"music|song|sings|singing|soundtrack|score|theme|jingle", re.I)
_SOUND_OF = re.compile(r"^(?:the\s+)?(?:sounds?|noises?)\s+of\s+(?:an?\s+|the\s+|some\s+)?", re.I)
_PAUSE = re.compile(r"^(?:a\s+)?(?:short\s+|long\s+|brief\s+)?(?:pause|pauses|beat|silence)$")

# Shaped like a cue — one to three plain words — rather than anything at all in
# brackets. A bracketed aside the model writes as prose ("[Rust, if you want
# rigour,] but it's worth it") is words he said, and stripping it as a cue
# silently cut the front off a reply.
_ANY_CUE = re.compile(r"\[\s*([A-Za-z]+(?:[ -][A-Za-z]+){0,2})\s*\]")
# A longer direction — "[sounds of a grocery bag rustling]" — is the model
# narrating the scene, not words anyone said: plain lower case after its first
# letter, no commas, and nobody speaking in it. Never on screen.
_BRACKETED = re.compile(r"\[\s*([^\[\]:]{1,90}?)\s*\]")
_ASTERISKED = re.compile(r"\*([A-Za-z][A-Za-z' -]{0,60}?)\*")
_PARENTHESISED = re.compile(r"\(\s*([A-Za-z][a-z' -]{0,60}?)\s*\)")
_NARRATION = re.compile(r"[A-Za-z][a-z' ,-]*")
_SPEECH = re.compile(r"\b(i|i'm|i'll|i've|i'd|me|my|mine|you|your|you're|yours|we|us|our|let's)\b", re.I)
_SPACES = re.compile(r"\s{2,}")
_STAGE_VERB = re.compile(r"^(sigh|laugh|chuckl|yawn|groan|grunt|scoff|snort|clear|cough|paus|exhal|inhal|whisper|"
                         r"mutter|smil|grin|gasp|sniff|swallow|gulp|shrug|nod)", re.I)


def supported(model=None):
    """True when the voice model performs cues rather than reading them out."""
    model = model or config.ELEVENLABS_MODEL or ""
    return model.startswith(("eleven_v3", "eleven_v4"))


def sounds_supported(model=None):
    """True when the voice performs the sounds of a scene as well as a delivery (v4)."""
    model = model or config.ELEVENLABS_MODEL or ""
    return model.startswith("eleven_v4")


def _tidy(text):
    return _SPACES.sub(" ", text).strip()


def _direction(inside):
    """The bracket's contents as a direction (lower case, one space), or None if it's words someone said."""
    words = " ".join(inside.split())
    if not words:
        return ""
    if _ANY_CUE.fullmatch(f"[{words}]"):
        return words.lower()
    if _NARRATION.fullmatch(words) and not _SPEECH.search(words) and len(words.split()) <= 12:
        return words.lower()
    return None


def _performable(cue, model=None, strict=False):
    """
    What the voice gets for a direction — itself, the sound alone, or None for
    nothing. `strict`: only the known cues and sounds, never a free-form
    direction — for *asterisks* and (parentheses), which are as often emphasis
    or an aside as an action.
    """
    words = cue.replace(",", " ").split()
    if words and words[0] in ("he", "she", "they"):
        words = words[1:]                       # "[he sighs heavily]" is a sigh
    for n in (len(words), 2, 1):
        if " ".join(words[:n]) in CUES or " ".join(words[:n]) in SITUATIONAL:
            return " ".join(words[:n])
    if sounds_supported(model) and not _NOT_A_SCENE.search(cue) and not _PAUSE.match(cue):
        sound = _SOUND_OF.sub("", cue)
        if _SOUNDISH.search(sound) and len(sound.split()) <= 6:
            return sound
        # v4 takes a direction in its own words — "[takes a deep breath]", "[through
        # gritted teeth]" — and performs it; any short one, as written.
        if not strict and 1 <= len(words) <= 8:
            return " ".join(words)
    return None


def remembered(text):
    """
    A line as it's kept in their memory: the words, with what they did while
    saying them — "[takes a deep breath] Look, B." — so Dick knows he just
    sighed. Only the directions the voice performs; markers and junk go.
    """
    def keep(match):
        cue = _direction(match.group(1))
        if cue is None:
            return match.group(0)
        performed = _performable(cue, "eleven_v4")
        return f"[{performed}]" if performed else " "
    return _tidy(_BRACKETED.sub(keep, _marked(text or "")))


def _marked(text):
    """*sighs* and (laughs) written as the cues they are; emphasis asterisks just go."""
    def star(match):
        inside = match.group(1)
        cue = " ".join(inside.lower().split())
        if _performable(cue, "eleven_v4", strict=True) or _STAGE_VERB.match(cue):
            return f"[{cue}]"
        return inside
    text = _ASTERISKED.sub(star, text)

    def paren(match):
        cue = " ".join(match.group(1).lower().split())
        if not _SPEECH.search(cue) and (_performable(cue, "eleven_v4", strict=True) or _STAGE_VERB.match(cue)
                                         or _PAUSE.match(cue)):
            return f"[{cue}]"
        return match.group(0)
    return _PARENTHESISED.sub(paren, text)


def clean(text):
    """The words alone, for the screen, memory and the guards."""
    def drop(match):
        return " " if _direction(match.group(1)) is not None else match.group(0)
    return _tidy(_BRACKETED.sub(drop, _marked(text or "")))


# Said in full, the way they're said aloud — the screen keeps "Mr." as written.
_SAID_IN_FULL = [(re.compile(rf"\b{short}\.(?=\s)"), said) for short, said in (
    ("Mr", "Mister"), ("Mrs", "Missus"), ("Dr", "Doctor"), ("Prof", "Professor"), ("Det", "Detective"),
    ("Lt", "Lieutenant"), ("Sgt", "Sergeant"), ("Capt", "Captain"), ("Commr", "Commissioner"))]


def _pronounce(text):
    for short, said in _SAID_IN_FULL:
        text = short.sub(said, text)
    for word, spelling in config.PRONUNCIATIONS.items():
        text = re.sub(rf"\b{re.escape(word.strip())}\b", spelling.strip(), text)
    return text


def cued(text, model=None):
    """True when the voice would perform a cue somewhere in this text."""
    return "[" in voiced(text, model)


def voiced(text, model=None):
    """What the synthesiser gets: the words, plus any cue it can perform."""
    text = _marked(_pronounce(text or ""))
    if not supported(model):
        return clean(text)

    kept = []

    def keep(match):
        cue = _direction(match.group(1))
        if cue is None:
            # Words he said, bracketed: said as words — v4 would take the brackets as direction.
            return match.group(1)
        # One cue per piece of text: "[laughs] [sarcastic] Delusions..." asked
        # the voice for two deliveries of the same words at once.
        performed = _performable(cue, model)
        if performed and not kept:
            kept.append(performed)
            return f"[{performed}]"
        return " "

    return _tidy(_BRACKETED.sub(keep, text))
