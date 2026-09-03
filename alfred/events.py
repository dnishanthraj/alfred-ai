"""
Event vocabulary shared by the engine and every frontend.

The engine never prints and never plays audio — it yields these. A terminal
frontend renders them as ANSI; the web console renders them as DOM updates and
Web Audio. Adding a third frontend (phone, watch) means consuming these same
events, not forking the conversation logic.
"""

# --- Engine states (mutually exclusive, always the assistant's current mode) ---
IDLE = "idle"
LISTENING = "listening"
TRANSCRIBING = "transcribing"
SEARCHING = "searching"
THINKING = "thinking"
SPEAKING = "speaking"


def state(value):
    """The engine changed mode. Frontends use this to drive status readouts."""
    return {"type": "state", "value": value}


def message(role, text):
    """A complete turn to display. role is 'user' or 'assistant'."""
    return {"type": "message", "role": role, "text": text}


def token(text):
    """One streamed fragment of the in-progress assistant reply."""
    return {"type": "token", "text": text}


def reply_start():
    """The assistant has begun generating; open an empty bubble."""
    return {"type": "reply_start"}


def reply_end(text):
    """
    Generation finished and the deterministic guards have run. `text` is the
    final, guarded reply and supersedes anything delivered via token events —
    the guards trim the tail, so the streamed text can differ from what is
    actually spoken and stored.
    """
    return {"type": "reply_end", "text": text}


def speak(audio_id, text):
    """Synthesized audio is ready to be fetched from /api/audio/<audio_id>."""
    return {"type": "speak", "audio_id": audio_id, "text": text}


def notice(text, level="info"):
    """Out-of-band system message. level: info | warn | error."""
    return {"type": "notice", "text": text, "level": level}


def transcript(text):
    """What the speech-to-text backend heard, before it is treated as a prompt."""
    return {"type": "transcript", "text": text}
