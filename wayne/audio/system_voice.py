"""
The console's own voice — the Batcomputer — for the moments that belong to
the machine rather than to anyone on the line: unlocking, locking, placing a
call, ending one.

These lines are written in advance on purpose. Everywhere a character speaks,
what they say is generated, because a person never says the same thing twice;
a system is the one voice that should sound like a system. A few variants per
moment keep it from sounding like a recording, and each is synthesised once
and kept on disk, so it plays the instant it is asked for.

Optional: set BATCOMPUTER_VOICE_ID. Without it the console is silent at these
moments, as it always was.
"""
import hashlib
import random
import threading
import time

from .. import config, paths

LINES = {
    "unlock": [
        "Identity confirmed. Good {period}.",
        "Voice print accepted. Systems online.",
        "Access granted. Good {period}.",
        "Authentication complete. Welcome back.",
    ],
    "lock": [
        "Console secured.",
        "Session locked.",
        "Locking down. Goodbye.",
    ],
    "call": [
        "Establishing secure line to {name}.",
        "Opening encrypted channel to {name}.",
        "Routing call to {name}.",
    ],
    "end": [
        "Line closed.",
        "Channel terminated.",
        "Connection ended.",
    ],
    "add": [
        "Adding {name} to the call.",
        "Patching {name} in.",
        "Bringing {name} onto the line.",
    ],
    "drop": [
        "{name} has left the call.",
        "{name} is off the line.",
    ],
    "incoming": [
        "Incoming call. {name}.",
        "{name} is calling.",
        "Secure line request from {name}.",
    ],
    "unavailable": [
        "{name} is not available.",
        "No answer from {name}'s line.",
    ],
}

_lock = threading.Lock()


def available():
    return bool(config.BATCOMPUTER_VOICE_ID and config.ELEVENLABS_API_KEY)


def _period(hour=None):
    hour = time.localtime().tm_hour if hour is None else hour
    return "morning" if 5 <= hour < 12 else "afternoon" if 12 <= hour < 18 else "evening"


def line(event, name=""):
    """A line for this moment, chosen at random, with the blanks filled."""
    options = LINES.get(event)
    if not options:
        return ""
    return random.choice(options).format(name=name, period=_period())


def _cache_path(text):
    key = hashlib.sha1(f"{config.BATCOMPUTER_VOICE_ID}|{config.ELEVENLABS_MODEL}|{text}"
                       .encode()).hexdigest()[:20]
    return paths.DATA_DIR / "_system_voice" / f"{key}.mp3"


def audio(text, voice_engine):
    """mp3 bytes for a line, from disk if it has been said before."""
    path = _cache_path(text)
    if path.exists():
        return path.read_bytes()
    clip = voice_engine.synthesize(text, config.BATCOMPUTER_VOICE_ID)
    with _lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(clip)
    return clip


def prime(voice_engine, names):
    """Synthesise every line ahead of time, so none waits on the network."""
    if not available():
        return
    texts = set()
    for options in LINES.values():
        for option in options:
            for name in (names if "{name}" in option else [""]):
                for period in (("morning", "afternoon", "evening") if "{period}" in option else ("",)):
                    texts.add(option.format(name=name, period=period))
    for text in sorted(texts):
        try:
            audio(text, voice_engine)
        except Exception:
            return   # no voice, no quota, no network: stay silent rather than retry
