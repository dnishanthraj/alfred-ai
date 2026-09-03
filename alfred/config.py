"""
Central configuration. Anything personal (names, API keys, voice IDs) is
read from the environment (.env) rather than hardcoded, so the source tree
stays shareable while each user's own identity lives in their local .env.
"""
import os

from dotenv import load_dotenv

from .paths import ENV_FILE

load_dotenv(ENV_FILE)

# --- Identity ---
USER_NAME = os.getenv("ALFRED_USER_NAME", "User")
ASSISTANT_NAME = os.getenv("ALFRED_ASSISTANT_NAME", "Alfred")
OLLAMA_MODEL = os.getenv("ALFRED_OLLAMA_MODEL", "alfred")

# --- Push-to-talk hotkey (pynput key name, e.g. 'Key.cmd_r', 'Key.shift_r') ---
PTT_KEY_STR = os.getenv("ALFRED_PTT_KEY", "Key.cmd_r")

# --- Whisper transcription hints (comma-separated proper nouns to bias STT) ---
WHISPER_HINT_PROMPT = os.getenv(
    "ALFRED_WHISPER_HINTS", f"{ASSISTANT_NAME}, {USER_NAME}"
)

# --- ElevenLabs TTS ---
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")
ALFRED_VOICE_ID = os.getenv("ALFRED_VOICE_ID")
ELEVENLABS_MODEL = os.getenv("ALFRED_TTS_MODEL", "eleven_turbo_v2_5")

# --- Conversation guards ---
# Safety net against runaway word-salad replies, not a style control. Raise it
# if legitimate longer answers are being clipped mid-thought.
MAX_REPLY_SENTENCES = int(os.getenv("ALFRED_MAX_REPLY_SENTENCES", "4"))

# --- Web console ---
WEB_HOST = os.getenv("ALFRED_WEB_HOST", "127.0.0.1")
WEB_PORT = int(os.getenv("ALFRED_WEB_PORT", "8420"))
# Global push-to-talk needs macOS Accessibility permission for whatever process
# launches the server. Off by default — the in-page hold-to-talk button always
# works and needs nothing but a mic permission.
GLOBAL_HOTKEY = os.getenv("ALFRED_GLOBAL_HOTKEY", "0").lower() in ("1", "true", "yes")


def missing_requirements():
    """Config problems worth surfacing at boot rather than failing per-turn."""
    problems = []
    if not ELEVENLABS_API_KEY:
        problems.append("ELEVENLABS_API_KEY is unset — replies will be silent.")
    if not ALFRED_VOICE_ID:
        problems.append("ALFRED_VOICE_ID is unset — replies will be silent.")
    return problems
