"""
Stage directions for the voice.

ElevenLabs v3 and v4 read bracketed cues — [sighs], [whispers], [laughs] — as
direction rather than text: measured on the voice in use, a sigh adds most of a
second of breath, a whisper drops loudness about 15%, a shout raises it about as
much, and none of them were spoken as words. That is the range a person has and
a flat text-to-speech read does not: the same line said tiredly, under the
breath, or through a laugh.

So the contact may write them, the way an actor's script carries them, and they
travel with a sentence only as far as the synthesiser. The transcript, the
history and every guard see clean text: a cue on screen is a stage direction
read aloud, and a cue in memory teaches the model to scatter more of them.

Only cues on this list reach the voice. Anything else in brackets is dropped,
because an unknown cue is either ignored or — worse — spoken. Older models read
every cue aloud, so for them all of it is dropped.
"""
import re

from . import config

# Trimmed by listening to the transcripts: "snorts" and "curious" turned up on
# nearly every line once offered, and a cue on every line is a man who sighs at
# everything.
CUES = ("sighs", "exhales", "laughs", "whispers", "sarcastic", "shouting")

# Shaped like a cue — one to three plain words — rather than anything at all in
# brackets. A bracketed aside the model writes as prose ("[Rust, if you want
# rigour,] but it's worth it") is words he said, and stripping it as a cue
# silently cut the front off a reply.
_ANY_CUE = re.compile(r"\[\s*([A-Za-z]+(?:[ -][A-Za-z]+){0,2})\s*\]")
_SPACES = re.compile(r"\s{2,}")


def supported(model=None):
    """True when the voice model performs cues rather than reading them out."""
    model = model or config.ELEVENLABS_MODEL or ""
    return model.startswith(("eleven_v3", "eleven_v4"))


def _tidy(text):
    return _SPACES.sub(" ", text).strip()


def clean(text):
    """The words alone, for the screen, memory and the guards."""
    return _tidy(_ANY_CUE.sub(" ", text or ""))


def voiced(text, model=None):
    """What the synthesiser gets: the words, plus any cue it can perform."""
    if not supported(model):
        return clean(text)

    def keep(match):
        return f"[{match.group(1).lower()}]" if match.group(1).lower() in CUES else " "

    return _tidy(_ANY_CUE.sub(keep, text or ""))
