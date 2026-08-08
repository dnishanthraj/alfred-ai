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
