"""
Event vocabulary shared by the engine and every frontend.

The engine never prints and never plays audio — it yields these. A terminal
frontend renders them as ANSI; the web console renders them as DOM updates and
Web Audio. Adding a third frontend means consuming these same events, not
forking the conversation logic.
"""

# --- Engine states (the contact's current mode) ---
IDLE = "idle"
LISTENING = "listening"
TRANSCRIBING = "transcribing"
SEARCHING = "searching"
THINKING = "thinking"
SPEAKING = "speaking"


def state(value):
    """The engine changed mode. Frontends drive status readouts from this."""
    return {"type": "state", "value": value}


def message(role, text):
    """A complete turn to display. role is 'user' or 'assistant'."""
    return {"type": "message", "role": role, "text": text}


def reply_start():
    """Generation has begun."""
    return {"type": "reply_start"}


def sentence(index, text):
    """
    One complete, guarded sentence of the reply, ready to be synthesized.

    Emitted as soon as the sentence is finished rather than at the end of
    generation, so speech for the opening line starts while the rest is still
    being written.
    """
    return {"type": "sentence", "index": index, "text": text}


def reply_end(text, interim=False):
    """
    The reply is complete. `text` is the final spoken text.

    `interim` marks a holding line ("One moment.") that will be followed by the
    real answer in the same turn — frontends use it to avoid treating the turn
    as finished.
    """
    return {"type": "reply_end", "text": text, "interim": interim}


def speak(audio_id, text, index=0):
    """Audio for one sentence is ready at /api/audio/<audio_id>."""
    return {"type": "speak", "audio_id": audio_id, "text": text, "index": index}


def turn_complete():
    """
    Generation *and* synthesis are both finished for this turn.

    `reply_end` fires when the model stops writing, which is before the last
    sentences have been synthesized — a frontend that treats it as "nothing
    more is coming" would render text that is about to be spoken. This is the
    signal that the turn is genuinely done.
    """
    return {"type": "turn_complete"}


def sources(items):
    """Search results backing the answer, so the console can cite them."""
    return {"type": "sources", "items": items}


def notice(text, level="info"):
    """Out-of-band system message. level: info | warn | error."""
    return {"type": "notice", "text": text, "level": level}


def transcript(text):
    """What speech-to-text heard, before it is treated as a prompt."""
    return {"type": "transcript", "text": text}


def contact_changed(contact_id):
    """The console switched to a different correspondent."""
    return {"type": "contact", "id": contact_id}
