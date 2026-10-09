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


def setting(name, default=""):
    """
    A console-wide setting: WAYNE_<name>, or the ALFRED_<name> it used to be.

    The console began as one character and its settings were named after him;
    it is a directory of contacts now, so the console's own settings say so.
    The old names still work, so an existing .env does not break. Settings that
    really are one character's (ALFRED_VOICE_ID) keep that character's name.
    """
    return os.getenv(f"WAYNE_{name}") or os.getenv(f"ALFRED_{name}") or default

# --- Operator identity ---
USER_NAME = setting("USER_NAME", "Operator")

# How long Ollama holds the model in memory after a reply. The default of five
# minutes means a conversation resumed after a coffee pays a full model load —
# around 25 seconds for a 14B — before the first word. Holding it resident
# trades RAM for the difference between "instant" and "did it crash?".
#
# Shortened from an hour once the default model became a 15 GB 26B: on a 24 GB
# machine, holding it all evening put the rest of the Mac into swap and kept it
# hot. The console now loads the model as a call rings and releases it after
# hang-up (see HANG_UP_RELEASE); this is only the backstop for anything else.
MODEL_KEEP_ALIVE = setting("MODEL_KEEP_ALIVE", "10m")

# How long after hanging up the model is released, unless another call comes
# in first. Long enough to ring straight back without a reload.
HANG_UP_RELEASE = int(setting("HANG_UP_RELEASE", "300"))

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
CONTEXT_WINDOW = int(setting("CONTEXT_WINDOW", "8192"))

# Where the operator is, as a place name ("London"). Optional: when set, the
# contact is handed a live weather reading for it (see wayne/engine/world.py)
# instead of being left to imagine one. Personal, so it lives in .env.
LOCATION = setting("LOCATION", "").strip()
# What to call that place when speaking of it — "Gotham", in a roleplay whose
# real weather comes from somewhere considerably less dramatic.
LOCATION_NAME = setting("LOCATION_NAME", "").strip()

# An RSS feed of headlines he has glanced at (e.g. a national news front page).
# Optional; titles only, refreshed every half hour in the background.
NEWS_FEED = setting("NEWS_FEED", "").strip()

# Read today's and tomorrow's events from macOS Calendar. Off unless set to 1;
# the first read asks for Calendar permission.
CALENDAR = setting("CALENDAR", "").strip().lower() in ("1", "true", "yes")

# --- Speech-to-text ---
WHISPER_HINT_PROMPT = setting("WHISPER_HINTS", USER_NAME)

# Speech-to-text model. `small.en` is the default on measurement, not habit: on
# an M-series Mac it runs in ~0.2s, and a model four times its size was three
# times slower without being more accurate on the same audio. Change it if your
# room or accent says otherwise.
WHISPER_MODEL = setting("WHISPER_MODEL", "mlx-community/whisper-small.en-mlx")

# How many sentences may be synthesised at once. ElevenLabs caps concurrent
# requests by plan, and every sentence going out together drew "429 Too many
# concurrent requests" — the sentences over the cap were simply never spoken.
TTS_CONCURRENCY = max(1, int(setting("TTS_CONCURRENCY", "2")))

# The console's own voice, for logging in and out and placing and ending calls.
# Optional; without it those moments stay silent.
BATCOMPUTER_VOICE_ID = os.getenv("BATCOMPUTER_VOICE_ID", "").strip()

# How the voice should say words it would otherwise get wrong — names, mostly.
# "Word:Spelling;Other:Spelling", applied to what is spoken and never to what is
# shown. Personal, so it lives in .env.
PRONUNCIATIONS = dict(
    pair.split(":", 1) for pair in setting("PRONOUNCE", "").split(";") if ":" in pair)

# --- ElevenLabs ---
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")
# eleven_v4_turbo is the v4 voice built for conversation: first audio in about
# 0.2s on the streaming endpoint. eleven_v4 is the more expressive flagship at
# 0.6–0.9s to first audio and about a second per sentence; eleven_turbo_v2_5 is
# the previous generation, a touch faster still.
ELEVENLABS_MODEL = setting("TTS_MODEL", "eleven_v4_turbo")

# --- Push-to-talk (terminal frontend only; the console has its own controls) ---
PTT_KEY_STR = setting("PTT_KEY", "Key.cmd_r")

# --- Web console ---
WEB_HOST = setting("WEB_HOST", "127.0.0.1")
WEB_PORT = int(setting("WEB_PORT", "8420"))

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
    if not MEMORY_KEY:
        problems.append(
            "Memory vault is unencrypted on disk. Run `python run.py --new-key` to secure it."
        )
    return problems
