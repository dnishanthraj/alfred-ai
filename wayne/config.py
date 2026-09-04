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

# How long Ollama holds the model in memory after a reply. The default of five
# minutes means a conversation resumed after a coffee pays a full model load —
# around 25 seconds for a 14B — before the first word. Holding it resident
# trades RAM for the difference between "instant" and "did it crash?".
MODEL_KEEP_ALIVE = os.getenv("ALFRED_MODEL_KEEP_ALIVE", "1h")

# Ollama's default context is 4096 tokens. A persona, a primer and a few turns
# of history clear that easily, and once the prompt outgrows the window Ollama
# shifts context — which throws away the KV cache and re-reads the *entire*
# prompt on every single turn. The symptom is latency that climbs with the
# conversation and never comes back down: measured here at 5.3 seconds to the
# first word, with a repeat of the identical prompt no faster than the first.
#
# Sized so the whole stable prefix fits with room for the conversation to grow,
# which is what makes it cacheable. The same request then answers in 1.8s.
# Raise it for longer histories at the cost of memory.
CONTEXT_WINDOW = int(os.getenv("ALFRED_CONTEXT_WINDOW", "8192"))

# --- Speech-to-text ---
WHISPER_HINT_PROMPT = os.getenv("ALFRED_WHISPER_HINTS", USER_NAME)

# Speech-to-text model. `small.en` is the default on measurement, not habit: on
# an M-series Mac it runs in ~0.2s, and a model four times its size was three
# times slower without being more accurate on the same audio. Change it if your
# room or accent says otherwise.
WHISPER_MODEL = os.getenv("ALFRED_WHISPER_MODEL", "mlx-community/whisper-small.en-mlx")

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

# Encrypts conversation history and the memory vault at rest. Generate one with
# `python run.py --new-key`. Unlike the lock screen, this is real encryption —
# but the key sits in .env beside the data, so it defends against casual
# reading, backups and sync clients, not against someone who has your .env.
MEMORY_KEY = os.getenv("WAYNE_MEMORY_KEY", "")


def missing_requirements():
    """
    Config problems worth surfacing at boot rather than failing per-turn.

    Phrased for the console rather than for a log: a missing voice key is a
    degraded link, not a stack trace. The console is meant to read like a place.
    """
    problems = []
    if not ELEVENLABS_API_KEY:
        problems.append("Voice link unavailable — text only. (ELEVENLABS_API_KEY is unset.)")
    if USER_NAME == "Operator":
        problems.append(
            "Operator unidentified. Set ALFRED_USER_NAME in .env so the console knows who you are."
        )
    if not MEMORY_KEY:
        problems.append(
            "Memory vault is unencrypted on disk. Run `python run.py --new-key` to secure it."
        )
    return problems
