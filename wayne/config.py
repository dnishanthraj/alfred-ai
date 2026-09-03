"""
Console-wide configuration.

Anything personal (names, API keys, voice IDs) is read from the environment so
the source tree stays shareable. Per-*contact* settings — model, voice, reply
style — live in that contact's profile, not here.
"""
import os

from dotenv import load_dotenv

from .paths import ENV_FILE

load_dotenv(ENV_FILE)

# --- Operator identity ---
USER_NAME = os.getenv("ALFRED_USER_NAME") or os.getenv("WAYNE_USER_NAME") or "Operator"

# --- Speech-to-text ---
WHISPER_HINT_PROMPT = os.getenv("ALFRED_WHISPER_HINTS", USER_NAME)

# --- ElevenLabs ---
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")
ELEVENLABS_MODEL = os.getenv("ALFRED_TTS_MODEL", "eleven_turbo_v2_5")

# --- Push-to-talk (terminal frontend only; the console has its own controls) ---
PTT_KEY_STR = os.getenv("ALFRED_PTT_KEY", "Key.cmd_r")

# --- Web console ---
WEB_HOST = os.getenv("ALFRED_WEB_HOST", "127.0.0.1")
WEB_PORT = int(os.getenv("ALFRED_WEB_PORT", "8420"))

# The lock screen is deliberately theatre, not security: it is enforced in the
# page, the server does not check it, and anyone with shell access can read it
# straight out of this file. It exists because a console should feel like one.
# Do not put anything behind it that actually needs protecting.
CONSOLE_PASSCODE = os.getenv("WAYNE_PASSCODE", "zorro")

DEFAULT_CONTACT = os.getenv("WAYNE_DEFAULT_CONTACT", "alfred")


def missing_requirements():
    """Config problems worth surfacing at boot rather than failing per-turn."""
    problems = []
    if not ELEVENLABS_API_KEY:
        problems.append("ELEVENLABS_API_KEY is unset — replies will be silent.")
    if USER_NAME == "Operator":
        problems.append(
            "ALFRED_USER_NAME is unset — set it in .env so the console knows who you are."
        )
    return problems
