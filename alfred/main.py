import os
import warnings

os.environ["PYTHONWARNINGS"] = "ignore"
os.environ["TORCH_CPP_LOG_LEVEL"] = "ERROR"
os.environ["TOKENIZERS_PARALLELISM"] = "false"
os.environ["TQDM_DISABLE"] = "1"
os.environ["HF_HUB_DISABLE_PROGRESS_BARS"] = "1"
warnings.filterwarnings("ignore")

import ollama
import sys
import select
import threading
import time
import random
import re
import difflib
from pynput import keyboard as pynput_keyboard

from . import memory
from .config import ASSISTANT_NAME, OLLAMA_MODEL, PTT_KEY_STR, USER_NAME
from .ear import listen_for_hold
from .search import format_search_results, google_search, needs_search
from .voice import get_voice_engine

voice_engine = get_voice_engine()

_PRE_SEARCH_PHRASES = [
    "One moment.",
    "Just a moment.",
    "Stand by.",
    "Let me check.",
    "Give me a moment.",
]

KEY_STATE = {'ptt': False, 'esc': False}
KEY_LOCK = threading.Lock()

def on_press(key):
    try:
        if str(key) == PTT_KEY_STR:
            with KEY_LOCK:
                KEY_STATE['ptt'] = True
        if key == pynput_keyboard.Key.esc:
            with KEY_LOCK:
                KEY_STATE['esc'] = True
    except AttributeError:
        pass

def on_release(key):
    try:
        if str(key) == PTT_KEY_STR:
            with KEY_LOCK:
                KEY_STATE['ptt'] = False
        if key == pynput_keyboard.Key.esc:
            with KEY_LOCK:
                KEY_STATE['esc'] = False
    except AttributeError:
        pass

listener = pynput_keyboard.Listener(on_press=on_press, on_release=on_release)
listener.daemon = True
listener.start()


class Colors:
    RESET         = "\033[0m"
    BOLD          = "\033[1m"
    ITALIC        = "\033[3m"
    POLAR_NIGHT_4 = "\033[38;2;76;86;106m"
    SNOW_STORM_1  = "\033[38;2;216;222;233m"
    FROST_2       = "\033[38;2;136;192;208m"
    FROST_4       = "\033[38;2;94;129;172m"
    AURORA_RED    = "\033[38;2;191;97;106m"
    AURORA_GREEN  = "\033[38;2;163;190;140m"
    AURORA_YELLOW = "\033[38;2;235;203;139m"


CONVERSATION_HISTORY = memory.load_history()


def clear_screen():
    os.system('cls' if os.name == 'nt' else 'clear')


def _time_context():
    """
    Returns an unambiguous time string using 24-hour clock.
    Example: "Saturday, 28 June 2026, 21:45 (evening)"
    Period label is factual only — the assistant must not infer activity from it.
    """
    now = time.localtime()
    hour = now.tm_hour
    if 5 <= hour < 12:
        period = "morning"
    elif 12 <= hour < 17:
        period = "afternoon"
    elif 17 <= hour < 21:
        period = "evening"
    else:
        period = "night"
    return time.strftime(f"%A, %d %B %Y, %H:%M ({period})", now)


def print_header():
    clear_screen()
    print(f"{Colors.FROST_4}{Colors.BOLD}╔═════════════════════════════════════════════════════════════════╗{Colors.RESET}")
    print(f"{Colors.FROST_4}{Colors.BOLD}║             WAYNETECH B.A.T. CONSOLE v5.2 [QWEN 2.5]            ║{Colors.RESET}")
    print(f"{Colors.FROST_4}{Colors.BOLD}╚═════════════════════════════════════════════════════════════════╝{Colors.RESET}")
    print(f"{Colors.POLAR_NIGHT_4}   System: {Colors.AURORA_GREEN}ONLINE{Colors.POLAR_NIGHT_4} | Memory: {Colors.AURORA_GREEN}HYBRID{Colors.POLAR_NIGHT_4} | Model: {Colors.FROST_2}{OLLAMA_MODEL.upper()}{Colors.RESET}\n")
    print(f"{Colors.POLAR_NIGHT_4}Controls:{Colors.RESET}")
    print(f"  [{Colors.AURORA_YELLOW}HOLD PTT KEY{Colors.RESET}]       :: Voice Command")
    print(f"  [{Colors.AURORA_YELLOW}TYPE & ENTER{Colors.RESET}]       :: Text Command")
    print(f"  [{Colors.AURORA_RED}PRESS ESC{Colors.RESET}]          :: Disconnect\n")
    print(f"{Colors.POLAR_NIGHT_4}───────────────────────────────────────────────────────────────────{Colors.RESET}\n")


def print_prompt():
    print(f"\r\033[K{Colors.SNOW_STORM_1}{Colors.BOLD}[{USER_NAME.upper()}]:{Colors.RESET} ", end="", flush=True)


def _interrupt_check():
    return KEY_STATE['ptt'] or KEY_STATE['esc']


def _speak_async(text):
    audio_paths = voice_engine.batch_synthesize(text)
    threading.Thread(
        target=voice_engine.play_sequence,
        args=(audio_paths,),
        kwargs={'interrupt_check': _interrupt_check},
        daemon=True,
    ).start()


# Phrases the assistant should never end on unless the user signalled they're leaving/sleeping.
_SIGNOFF_PATTERNS = [
    r"sleep well\b.*",
    r"get some (rest|sleep)\b.*",
    r"rest up\b.*",
    r"rest easy\b.*",
    r"good ?night\b.*",
    r"take care\b.*",
    r"catch up (on|with) (your )?sleep\b.*",
    r"turn in\b.*",
    r"off to bed\b.*",
]

# Words from the user that DO license a sleep/goodbye sign-off.
_LEAVING_CUES = ["bye", "goodnight", "good night", "night", "sleeping", "going to bed",
                 "off to bed", "i'm tired", "im tired", "heading off", "see you", "talk later",
                 "going to sleep", "gonna sleep", "logging off"]

# Greeting openings the assistant should not repeat once it's already greeted this session.
_GREETING_PATTERNS = [
    r"good ?morning\b.*",
    r"good ?afternoon\b.*",
    r"good ?evening\b.*",
    r"morning again\b.*",
    r"morning\b[.,]?$",
    r"hello again\b.*",
    r"welcome back\b.*",
    r"back again\b.*",
]

# Set True once the assistant has greeted in this session (boot greeting counts).
_ALREADY_GREETED = {"value": False}


def _strip_regreeting(text):
    """If the assistant already greeted this session, drop a leading re-greeting
    sentence so it doesn't say 'Good morning' twice. Returns text with the opener removed."""
    sentences = re.split(r'(?<=[.!?])\s+', text.strip())
    if sentences:
        first = sentences[0].strip().lower()
        if any(re.match(pat, first) for pat in _GREETING_PATTERNS):
            sentences.pop(0)
    cleaned = " ".join(sentences).strip()
    return cleaned if cleaned else text  # never return empty


def _user_is_leaving(prompt):
    p = prompt.lower()
    return any(cue in p for cue in _LEAVING_CUES)


def _strip_signoffs(text):
    """Remove a trailing sleep/goodbye sentence the assistant tacked on uninvited.
    Splits into sentences and drops trailing ones that match a sign-off pattern."""
    # Split keeping the sentence-ending punctuation
    sentences = re.split(r'(?<=[.!?])\s+', text.strip())
    while sentences:
        last = sentences[-1].strip().lower()
        if any(re.match(pat, last) for pat in _SIGNOFF_PATTERNS):
            sentences.pop()
        else:
            break
    cleaned = " ".join(sentences).strip()
    # If we stripped everything, fall back to a neutral line rather than empty
    return cleaned if cleaned else "Mm."


def _cap_length(text, max_sentences=4):
    """The model occasionally rambles into word-salad. This is a safety net, not a
    style tool: only very long replies get trimmed, back to the first few
    sentences, so a runaway answer can't reach the ear as a monologue. Short and
    medium replies pass through untouched — the model still varies length itself."""
    sentences = re.split(r'(?<=[.!?])\s+', text.strip())
    # Only intervene on genuinely runaway replies.
    if len(sentences) <= max_sentences:
        return text
    trimmed = " ".join(sentences[:max_sentences]).strip()
    return trimmed if trimmed else text


def _too_similar(candidate, recent_assistant_msgs, threshold=0.75):
    """True if candidate closely resembles a recent assistant reply (whole-string
    similarity) or reuses the same opening few words — the signature of a loop."""
    cand = candidate.strip().lower()
    cand_words = cand.split()
    cand_open = " ".join(cand_words[:4])
    cand_close = " ".join(cand_words[-5:])  # trailing phrase fingerprint
    for prev in recent_assistant_msgs:
        prev_l = prev.strip().lower()
        prev_words = prev_l.split()
        ratio = difflib.SequenceMatcher(None, cand, prev_l).ratio()
        if ratio >= threshold:
            return True
        if cand_open and cand_open == " ".join(prev_words[:4]):
            return True
        # Shared trailing phrase (5+ words) is the signature of a sign-off loop
        if len(cand_words) >= 5 and cand_close and cand_close == " ".join(prev_words[-5:]):
            return True
    return False


def _build_context_block():
    """
    Build per-turn dynamic context (time, memory vault).
    Folded into the USER message — never sent as role=system, which would
    override the Modelfile's SYSTEM prompt and wipe the assistant's personality.
    """
    parts = []
    parts.append(
        f"Current time: {_time_context()}\n"
        f"(Factual context only. Do not infer what {USER_NAME} has been doing, "
        f"is about to do, or should do based on this.)"
    )
    vault_data = memory.load_core_memory()
    if vault_data:
        parts.append(
            "Stored facts (use to stay grounded; do not invent new ones; "
            "raise one only if it directly contradicts what he's saying):\n"
            f"{vault_data}"
        )
    return "\n\n".join(parts)


def _compose_user_turn(prompt, search_context=""):
    """Wrap prompt with fenced context. Actual message comes last."""
    context = _build_context_block()
    if search_context:
        context += (
            f"\n\nLive intel — retrieved via search. Open with a brief natural "
            f"acknowledgment ('Found it.', 'Right, I've got something.') then answer "
            f"naturally. Never read results as a list:\n{search_context}"
        )
    if not context:
        return prompt
    return (
        "[REFERENCE — context only, do not speak any of this aloud]\n"
        f"{context}\n"
        "[END REFERENCE]\n\n"
        f"{prompt}"
    )


def ask_alfred(prompt):
    global CONVERSATION_HISTORY

    if not prompt.strip():
        return

    # Emergency memory wipe
    if prompt.lower() in ["clear memory", "forget everything", "protocol zero", "wipe logs"]:
        CONVERSATION_HISTORY = memory.clear_history()
        response_text = "Memory purged. We start fresh."
        print(f"\r\033[K{Colors.FROST_2}{Colors.BOLD}[{ASSISTANT_NAME.upper()}]:{Colors.RESET} {response_text}\n")
        _speak_async(response_text)
        return True

    # Long-term memory save
    if prompt.lower().startswith(("remember that", "remember to", "note that", "don't forget")):
        memory.memorize(prompt)
        response_text = "Noted. Stored to the vault."
        print(f"\r\033[K{Colors.FROST_2}{Colors.BOLD}[{ASSISTANT_NAME.upper()}]:{Colors.RESET} {response_text}\n")
        _speak_async(response_text)
        CONVERSATION_HISTORY.append({'role': 'user', 'content': prompt})
        CONVERSATION_HISTORY.append({'role': 'assistant', 'content': response_text})
        memory.save_history(CONVERSATION_HISTORY)
        return True

    # Optional web search
    search_context = ""
    last_alfred_msg = (
        CONVERSATION_HISTORY[-1]['content']
        if CONVERSATION_HISTORY and CONVERSATION_HISTORY[-1]['role'] == 'assistant'
        else ""
    )
    if needs_search(prompt, last_alfred_msg=last_alfred_msg):
        pre_phrase = random.choice(_PRE_SEARCH_PHRASES)
        print(f"\r\033[K{Colors.FROST_2}{Colors.BOLD}[{ASSISTANT_NAME.upper()}]:{Colors.RESET} {pre_phrase}")
        print(f"{Colors.ITALIC}{Colors.AURORA_YELLOW}[Scanning Global Grid...]{Colors.RESET}", end="", flush=True)
        _speak_async(pre_phrase)

        search_query = prompt
        if len(prompt.split()) <= 4:
            recent_user = [m['content'] for m in CONVERSATION_HISTORY[-6:] if m['role'] == 'user']
            if recent_user:
                search_query = ' '.join(recent_user[-2:]) + ' ' + prompt

        try:
            results = google_search(search_query, num_results=5)
            if results:
                search_context = format_search_results(results)
        except Exception as e:
            print(f"\r\033[K{Colors.AURORA_RED}[Search Failed]{Colors.RESET}: {e}")
        print("\r\033[K", end="", flush=True)

    user_turn = _compose_user_turn(prompt, search_context)
    messages_payload = list(CONVERSATION_HISTORY)
    messages_payload.append({'role': 'user', 'content': user_turn})

    try:
        print(f"\r\033[K{Colors.ITALIC}{Colors.POLAR_NIGHT_4}[Processing...]{Colors.RESET}", end="", flush=True)
        gen_opts = {"repeat_last_n": 256, "think": False}
        response_obj = ollama.chat(model=OLLAMA_MODEL, messages=messages_payload,
                                   stream=False, options=gen_opts)
        full_text = response_obj['message']['content'].strip()

        # --- Deterministic guards the prompt can't reliably enforce ---
        # (a) Strip uninvited sleep/goodbye sign-offs unless he's leaving.
        if not _user_is_leaving(prompt):
            full_text = _strip_signoffs(full_text)

        # (a2) Strip a duplicate greeting if the assistant already greeted this session.
        if _ALREADY_GREETED["value"]:
            full_text = _strip_regreeting(full_text)

        # (a3) Safety net against runaway word-salad replies.
        full_text = _cap_length(full_text, max_sentences=4)

        # (b) Anti-repetition: if this closely echoes a recent reply, regenerate
        #     once with a stronger nudge. Breaks loops the model falls into.
        recent_alfred = [m['content'] for m in CONVERSATION_HISTORY[-6:]
                         if m['role'] == 'assistant']
        if _too_similar(full_text, recent_alfred):
            retry_payload = list(messages_payload)
            retry_payload.append({
                'role': 'user',
                'content': "[You just repeated yourself. Say something completely "
                           "different — new words, new angle. Do not reuse your last phrasing.]"
            })
            try:
                retry_obj = ollama.chat(model=OLLAMA_MODEL, messages=retry_payload,
                                        stream=False,
                                        options={"repeat_last_n": 256, "temperature": 0.95, "think": False})
                retry_text = retry_obj['message']['content'].strip()
                if not _user_is_leaving(prompt):
                    retry_text = _strip_signoffs(retry_text)
                if retry_text and not _too_similar(retry_text, recent_alfred):
                    full_text = retry_text
            except Exception:
                pass

        # Save only raw exchange — not injected context.
        CONVERSATION_HISTORY.append({'role': 'user', 'content': prompt})
        CONVERSATION_HISTORY.append({'role': 'assistant', 'content': full_text})
        memory.save_history(CONVERSATION_HISTORY)
    except Exception as e:
        print(f"\n{Colors.AURORA_RED}[ERROR] Connection severed: {e}{Colors.RESET}")
        return

    print(f"\r\033[K{Colors.FROST_2}{Colors.BOLD}[{ASSISTANT_NAME.upper()}]:{Colors.RESET} {full_text}\n")
    _speak_async(full_text)
    return True


def system_boot_sequence():
    """
    Generate the assistant's opening line and store it in history as a proper
    user/assistant pair. This is critical: if the history starts with an
    orphaned assistant message (no preceding user turn), the model gets
    confused about who spoke last and hallucinates on the next exchange.

    The boot prompt is stored as the 'user' turn so the conversation chain
    is always: user → assistant → user → assistant...
    """
    global CONVERSATION_HISTORY

    print(f"\033[3m{Colors.POLAR_NIGHT_4}[Initializing Systems...]{Colors.RESET}", end="", flush=True)

    current_time = _time_context()
    has_history = bool(CONVERSATION_HISTORY)

    if not has_history:
        greeting_prompt = (
            f"[REFERENCE — context only]\n"
            f"Time: {current_time}. Fresh session.\n"
            f"[END REFERENCE]\n\n"
            f"The link just came live. Greet him in one sentence — natural, warm, "
            f"brief. No mention of sleep, rest, code, work, or technology."
        )
    else:
        greeting_prompt = (
            f"[REFERENCE — context only]\n"
            f"Time: {current_time}.\n"
            f"[END REFERENCE]\n\n"
            f"The link is live again. One or two sentences. Acknowledge the reconnection "
            f"naturally. Reference the last session only if something is genuinely worth "
            f"noting. No mention of sleep, rest, or bed."
        )

    fallback = "Online. I'm here when you're ready."
    generated_greeting = [fallback]
    warmup_success = [False]

    def _generate():
        try:
            # Build payload from existing history + boot prompt as user turn
            messages_payload = list(CONVERSATION_HISTORY)
            messages_payload.append({'role': 'user', 'content': greeting_prompt})
            response_obj = ollama.chat(model=OLLAMA_MODEL, messages=messages_payload, stream=False,
                                       options={"think": False})
            result_text = response_obj['message']['content'].strip()
            if result_text:
                generated_greeting[0] = result_text
            warmup_success[0] = True
        except Exception:
            pass

    t = threading.Thread(target=_generate)
    t.start()
    t.join(timeout=30)

    if not warmup_success[0]:
        print(f"\n{Colors.AURORA_RED}[WARNING] Cold start timed out. Using fallback.{Colors.RESET}")

    greeting_text = generated_greeting[0]

    # Store as a proper user/assistant pair so history is always well-formed.
    # The 'user' side is a neutral placeholder — it's never shown on screen.
    # Without this, the next real message arrives with an orphaned assistant
    # turn at the top of history, which confuses the model.
    CONVERSATION_HISTORY.append({'role': 'user', 'content': '[link established]'})
    CONVERSATION_HISTORY.append({'role': 'assistant', 'content': greeting_text})
    memory.save_history(CONVERSATION_HISTORY)

    # The assistant has now greeted — any later greeting in a reply is a duplicate.
    _ALREADY_GREETED["value"] = True

    print("\r\033[K", end="")
    print(f"{Colors.FROST_2}{Colors.BOLD}[{ASSISTANT_NAME.upper()}]:{Colors.RESET} {greeting_text}\n")
    _speak_async(greeting_text)


def main():
    print_header()
    system_boot_sequence()
    print_prompt()

    while True:
        try:
            if KEY_STATE['ptt']:
                if voice_engine.is_playing:
                    voice_engine.stop_playback()

                print(f"\r\033[K{Colors.AURORA_RED}[LISTENING...]{Colors.RESET}", end="", flush=True)

                text = listen_for_hold(hotkey_char=PTT_KEY_STR)

                if text:
                    print(f"\r\033[K{Colors.SNOW_STORM_1}{Colors.BOLD}[{USER_NAME.upper()}]:{Colors.RESET} {text}")
                    ask_alfred(text)

                print_prompt()
                continue

            readable, _, _ = select.select([sys.stdin], [], [], 0.0)
            if readable:
                if voice_engine.is_playing:
                    voice_engine.stop_playback()

                user_input = sys.stdin.readline().strip()
                if user_input.lower() in ['exit', 'quit']:
                    voice_engine.stop_playback()
                    print(f"\n{Colors.FROST_2}[{ASSISTANT_NAME.upper()}]: Signing off.{Colors.RESET}")
                    break

                if user_input:
                    sys.stdout.write("\033[F")
                    print(f"\r\033[K{Colors.SNOW_STORM_1}{Colors.BOLD}[{USER_NAME.upper()}]:{Colors.RESET} {user_input}")
                    ask_alfred(user_input)

                print_prompt()
                continue

            if KEY_STATE['esc']:
                if voice_engine.is_playing:
                    voice_engine.stop_playback()
                    print(f"\r\033[K{Colors.AURORA_YELLOW}[Silence Command Accepted]{Colors.RESET}\n")
                    print_prompt()
                    while KEY_STATE['esc']:
                        time.sleep(0.1)
                else:
                    print(f"\n\n{Colors.FROST_2}[{ASSISTANT_NAME.upper()}]: Protocol Zero. Shutting down.{Colors.RESET}")
                    break

            time.sleep(0.02)

        except KeyboardInterrupt:
            sys.exit()


if __name__ == "__main__":
    main()
