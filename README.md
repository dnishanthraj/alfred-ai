# Alfred AI

**Status:** `v0.2.0` — early, actively developed. See [CHANGELOG.md](CHANGELOG.md).

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
   python run.py            # web console at http://127.0.0.1:8420
   python run.py --cli      # terminal console instead
   ```

   Or double-click [`scripts/launch.command`](scripts/launch.command), which starts
   Ollama if it isn't already running and opens the console in your browser.

## The console

`python run.py` serves a local web console — a WayneTech-styled terminal with a
radial spectrum ring driven by the actual TTS audio, live status readouts, and
the running transcript. Nothing is exposed beyond `127.0.0.1`.

Audio is played *in the browser* through the Web Audio API rather than through
`afplay`, which is what lets the visualizer read a real FFT of Alfred's voice
instead of animating a guess. Click **Establish Link** on first load — browsers
keep an `AudioContext` suspended until the page gets a user gesture.

- **Hold the mic button** (or hold **Space**) and speak, then release.
- **Type and press Enter** for text input.
- **Press Esc** to silence playback.

The terminal console (`--cli`) keeps the original push-to-talk behaviour, which
works with the window unfocused:

- **Hold the PTT key** (`Key.cmd_r` by default) and speak, then release.
- **Press Esc** once to interrupt mid-reply, or again (with nothing playing) to quit.

Both frontends share the same engine, so these work in either:

- **"remember that …" / "remember to …" / "note that …"** — saves a fact to long-term
  memory (`batcomputer_vault.txt`).
- **"clear memory" / "forget everything" / "protocol zero" / "wipe logs"** — wipes
  short-term conversation history.

## Project structure

```
alfred-ai/
├── run.py                   # entry point — web console, or --cli
├── alfred/
│   ├── core.py              # headless engine: context, generation, guards
│   ├── events.py            # the event vocabulary shared by every frontend
│   ├── server.py            # FastAPI + WebSocket backing the web console
│   ├── cli.py               # terminal frontend (ANSI + afplay)
│   ├── main.py              # back-compat shim re-exporting cli.main
│   ├── config.py            # env-driven settings (identity, keys, hotkey, web)
│   ├── paths.py             # project-root-anchored file paths
│   ├── ear.py               # audio capture + Whisper transcription
│   ├── voice.py             # ElevenLabs synthesis + local playback
│   ├── memory.py            # short-term history + long-term vault persistence
│   └── search.py            # web search trigger detection + DuckDuckGo lookup
├── web/                     # the console: no build step, no node toolchain
│   ├── index.html
│   ├── console.css
│   ├── app.js               # socket, transcript, mic capture, audio playback
│   └── visualizer.js        # radial spectrum ring
├── scripts/
│   └── launch.command       # macOS convenience launcher
├── Modelfile.example        # personality template — copy to `Modelfile` and customize
├── .env.example             # config template — copy to `.env` and fill in secrets
└── requirements.txt
```

`core.py` never prints and never plays audio — it yields the events in
`events.py`, and a frontend decides how to render them. That's what lets the
terminal and the browser share one conversation implementation, and it's the
seam a future phone client would plug into.

## Customization

Alfred's personality lives entirely in the Ollama `Modelfile`'s `SYSTEM` prompt — nothing
about tone or backstory is hardcoded in Python. To build a different assistant, replace
`Modelfile` with your own prompt and `ollama create` it under a new name (update
`ALFRED_OLLAMA_MODEL` in `.env` to match).

## Roadmap

Not yet built, roughly in priority order:

- **Test suite + CI** — the deterministic text-processing helpers in `core.py`
  and `search.py` are pure functions and cheap to cover; a GitHub Actions run
  on push would follow naturally.
- **Sentence-chunked TTS** — synthesis currently waits for the whole reply
  before the first word is spoken. Splitting on sentence boundaries and
  pipelining would cut the silence before Alfred starts talking.
- **Wake-word activation** as an alternative to holding the push-to-talk key.
- **Companion mobile app** — a thin SwiftUI client (iPhone, possibly Watch)
  talking to a small local API in front of Ollama, for use off the machine
  that hosts the model. This is a separate client rewrite, not a port of the
  Python app.

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
