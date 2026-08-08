# Alfred AI

A local, voice-driven personal assistant for macOS — push-to-talk speech in, a locally-run
LLM (via [Ollama](https://ollama.com)) for thinking, and natural-sounding [ElevenLabs](https://elevenlabs.io)
speech out. The bundled `Modelfile.example` gives it a dry, Alfred Pennyworth-style
personality, but the prompt is fully swappable — write your own persona and it works
the same.

Conversation happens over one continuous terminal session: hold a hotkey to talk, or type.
Alfred remembers the running conversation and a small set of long-term facts you tell him
to remember, both stored locally on disk.

## Features

- **Push-to-talk voice input** — hold a configurable hotkey, speak, release. Transcribed
  locally with [Whisper](https://github.com/openai/whisper) (Apple Neural Engine via
  `mlx-whisper` on Apple Silicon, CPU via `faster-whisper` elsewhere).
- **Local LLM reasoning** — runs entirely through [Ollama](https://ollama.com); no chat
  transcript ever leaves your machine except the final reply text sent to ElevenLabs for
  speech synthesis.
- **Natural TTS** — replies are spoken aloud through the ElevenLabs API.
- **Short- and long-term memory** — recent turns persist across restarts
  (`batcomputer_history.json`), and explicit "remember that…" facts persist indefinitely
  (`batcomputer_vault.txt`). Both are local files, gitignored, never committed.
- **Optional live web search** — explicit search phrases ("look up…", "what's the weather…")
  trigger a DuckDuckGo lookup that gets folded into context before the model replies.
- **Deterministic conversation guards** — anti-repetition, sign-off suppression, and
  reply-length capping run in code rather than relying on the model to self-police.

## Requirements

- macOS (uses `afplay` for playback and macOS Accessibility permissions for the global
  hotkey listener — this project is not cross-platform as written)
- Python 3.11+
- [Ollama](https://ollama.com) installed and running, with the `qwen2.5:14b` base model
  pulled (`ollama pull qwen2.5:14b`)
- An [ElevenLabs](https://elevenlabs.io) account and API key
- A working microphone

## Setup

1. **Clone and install dependencies**

   ```bash
   git clone https://github.com/<your-username>/alfred-ai.git
   cd alfred-ai
   python3 -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   ```

2. **Create your personality file**

   The system prompt (`Modelfile`) is gitignored because it's meant to hold *your* real
   name and details — it's never committed.

   ```bash
   cp Modelfile.example Modelfile
   ```

   Edit `Modelfile` and fill in the `BASELINE DOSSIER` section with real, current facts
   about yourself. Then build the Ollama model:

   ```bash
   ollama create alfred -f Modelfile
   ```

3. **Configure secrets and identity**

   ```bash
   cp .env.example .env
   ```

   Edit `.env`:

   | Variable | Required | Description |
   |---|---|---|
   | `ELEVENLABS_API_KEY` | Yes | Your ElevenLabs API key |
   | `ALFRED_VOICE_ID` | Yes | Voice ID from your ElevenLabs voice library |
   | `ALFRED_USER_NAME` | No | Your name, shown in the console and used as a Whisper transcription hint (default: `User`) |
   | `ALFRED_ASSISTANT_NAME` | No | Assistant's display name (default: `Alfred`) |
   | `ALFRED_OLLAMA_MODEL` | No | Ollama model tag to call (default: `alfred`, matching step 2) |
   | `ALFRED_PTT_KEY` | No | [pynput](https://pynput.readthedocs.io) key name for push-to-talk (default: `Key.cmd_r`) |
   | `ALFRED_WHISPER_HINTS` | No | Comma-separated proper nouns to bias speech recognition |

4. **Grant macOS permissions**

   The first run will prompt for **Microphone** and **Accessibility** (or Input Monitoring)
   permissions for your terminal app — required for audio capture and the global
   push-to-talk hotkey listener. Grant both in System Settings → Privacy & Security.

5. **Run**

   ```bash
   python run.py
   ```

   Or double-click [`scripts/launch.command`](scripts/launch.command) (macOS only; written
   for the [Ghostty](https://ghostty.org) terminal — edit the `tell application` line if
   you use a different one).

## Usage

- **Hold the PTT key** (`Key.cmd_r` by default) and speak, then release — your speech is
  transcribed and sent.
- **Type and press Enter** for text input instead of voice.
- **Press Esc** once to interrupt Alfred mid-reply, or again (with nothing playing) to quit.
- **"remember that …" / "remember to …" / "note that …"** — saves a fact to long-term
  memory (`batcomputer_vault.txt`).
- **"clear memory" / "forget everything" / "protocol zero" / "wipe logs"** — wipes
  short-term conversation history.

## Project structure

```
alfred-ai/
├── run.py                 # entry point — `python run.py`
├── alfred/
│   ├── main.py             # conversation loop, input handling, response post-processing
│   ├── config.py           # env-driven settings (identity, keys, hotkey)
│   ├── paths.py             # project-root-anchored file paths
│   ├── ear.py               # push-to-talk recording + Whisper transcription
│   ├── voice.py             # ElevenLabs synthesis + playback
│   ├── memory.py            # short-term history + long-term vault persistence
│   └── search.py            # web search trigger detection + DuckDuckGo lookup
├── scripts/
│   └── launch.command       # macOS convenience launcher
├── Modelfile.example        # personality template — copy to `Modelfile` and customize
├── .env.example              # config template — copy to `.env` and fill in secrets
└── requirements.txt
```

## Customization

Alfred's personality lives entirely in the Ollama `Modelfile`'s `SYSTEM` prompt — nothing
about tone or backstory is hardcoded in Python. To build a different assistant, replace
`Modelfile` with your own prompt and `ollama create` it under a new name (update
`ALFRED_OLLAMA_MODEL` in `.env` to match).

## Notes & limitations

- **macOS only** as written — `afplay` for audio playback and `pynput`'s macOS
  Accessibility hook for the global hotkey aren't portable as-is.
- **Local-first, not local-only** — the LLM and speech-to-text run entirely on-device;
  only the final text reply is sent to ElevenLabs for speech synthesis.
- **No encryption** — the memory files (`batcomputer_history.json`,
  `batcomputer_vault.txt`) are plain text on disk. Don't store anything in the "remember
  that…" vault you wouldn't want readable by anyone with access to the machine.

## License

[MIT](LICENSE)
