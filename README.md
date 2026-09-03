# Alfred AI

**Status:** `v0.3.0` — early, actively developed. See [CHANGELOG.md](CHANGELOG.md).

A local, voice-driven console for macOS — speech in, a locally-run LLM (via
[Ollama](https://ollama.com)) for thinking, and natural-sounding
[ElevenLabs](https://elevenlabs.io) speech out, wrapped in a WayneTech-styled
web interface. Alfred Pennyworth ships as the first contact; the console is
built as a directory, so adding another character is a JSON profile.

Everything but speech synthesis runs on your machine, and each contact keeps
their own memory on disk.

## Features

- **A console, not a chat window** — contact directory, live status readouts, and a
  radial spectrum ring driven by the real FFT of the voice currently speaking.
- **Two microphone modes** — push-to-talk, or an ambient always-open channel with
  voice-activity detection and barge-in (talk over a reply and it stops).
- **Speech that starts before the reply is finished** — sentences are synthesized as
  the model writes them, several in flight at once, so audio begins in about half a
  second rather than after the whole answer.
- **Transcript in time with the voice** — words appear as they are spoken, not dumped
  on screen before the first syllable.
- **A directory of characters** — each contact has their own model, voice, sampling
  parameters, availability, worked examples, and memory. Adding one is a JSON file.
- **Local LLM reasoning** — entirely through [Ollama](https://ollama.com); no chat
  transcript leaves your machine except the reply text sent to ElevenLabs.
- **Speech-to-text on device** — [Whisper](https://github.com/openai/whisper) via
  `mlx-whisper` on Apple Silicon, `faster-whisper` on CPU elsewhere.
- **Short- and long-term memory** — recent turns plus an explicit "remember that…"
  vault, per contact, with relevance retrieval once the vault grows.
- **Optional live web search** — explicit lookups hit DuckDuckGo and are folded into
  context, with the sources cited in the console.
- **Deterministic conversation guards** — anti-repetition, sign-off suppression, and
  length capping run in code rather than relying on the model to police itself.

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
   | `ALFRED_USER_NAME` | Yes | Your name — shown in the console and used as a Whisper hint |
   | `ALFRED_OLLAMA_MODEL` | No | Ollama model tag for Alfred (default: `alfred`, matching step 2) |
   | `WAYNE_PASSCODE` | No | Lock-screen passcode (default: `zorro`). Theatre, not security |
   | `ALFRED_WEB_PORT` | No | Console port (default: `8420`) |
   | `ALFRED_PTT_KEY` | No | [pynput](https://pynput.readthedocs.io) key for `--cli` push-to-talk (default: `Key.cmd_r`) |
   | `ALFRED_WHISPER_HINTS` | No | Comma-separated proper nouns to bias speech recognition |

   Display name, role, voice, and sampling parameters are per-contact and live in
   [`wayne/contacts/profiles/alfred.json`](wayne/contacts/profiles/alfred.json).

4. **Grant macOS permissions**

   The web console needs only a **Microphone** permission, granted in the browser.
   The terminal frontend (`--cli`) additionally needs **Accessibility** (or Input
   Monitoring) for your terminal app, for the global push-to-talk hotkey. Grant it
   in System Settings → Privacy & Security.

5. **Run**

   ```bash
   python run.py            # web console at http://127.0.0.1:8420
   python run.py --cli      # terminal console instead
   ```

   Or double-click [`scripts/launch.command`](scripts/launch.command), which starts
   Ollama if it isn't already running and opens the console in your browser.

## The console

`python run.py` serves a local console at `http://127.0.0.1:8420` — a directory
of contacts on the left, the conversation in the middle, and a radial spectrum
ring on the right driven by the real FFT of whoever is speaking. Nothing is
exposed beyond `127.0.0.1`.

On load you get a power-on self test and a passcode prompt. That screen is
theatre, but it is also load-bearing: browsers keep an `AudioContext` suspended
until the page receives a user gesture, so the console genuinely cannot come up
without one. **The passcode is not security** — it is checked in the page, the
server gates nothing on it, and it sits in plain text in `.env`. Don't put
anything behind it that needs protecting.

### Talking

Two microphone modes, switchable in the composer:

- **Push** — hold the mic button or hold **Space**, speak, release. Reliable in
  a noisy room.
- **Ambient** — the channel stays open. A voice-activity detector decides when
  an utterance starts and ends, and **speaking over a reply cuts it off**, the
  way interrupting a person does.

Also: type and press Enter, or press **Esc** to silence playback.

### Why it feels like a conversation

Two things, both of which are about timing rather than the model:

- **Sentences are synthesized as they are written.** The engine emits each
  sentence the moment it is complete and several go to ElevenLabs at once, so
  the first line is already playing while the rest is still being generated —
  roughly half a second to first audio instead of waiting out the whole reply.
- **The transcript is revealed in time with the speech.** Words appear as they
  are spoken, spread across each clip's real duration. Printing the reply the
  instant the model finishes reads as a chat log with a voice bolted on.

### Memory commands

These work in either frontend:

- **"remember that …" / "note that …"** — stores a fact in that contact's vault.
- **"forget that …"** — removes matching facts.
- **"clear memory" / "protocol zero"** — wipes that contact's history and vault.

### Terminal

`python run.py --cli` keeps the original push-to-talk behaviour, which works
with the window unfocused (it needs macOS Accessibility permission for the
global hotkey; the web console needs only a microphone permission).

## Contacts

The console is a phone book, not a single assistant. A contact is a JSON
profile in [`wayne/contacts/profiles/`](wayne/contacts/profiles/) declaring who
answers, in what voice, with what sampling parameters, and when they are
reachable. Each has its own memory under `data/<id>/`.

Adding one is a file, not a code change:

```json
{
  "id": "lucius",
  "name": "Lucius",
  "full_name": "Lucius Fox",
  "role": "Applied Sciences",
  "accent": "#5FC9A8",
  "model": "qwen2.5:14b",
  "voice_env": "LUCIUS_VOICE_ID",
  "system": "You are Lucius Fox — dry, brilliant, and unimpressed by theatrics.",
  "availability": { "kind": "hours", "days": [0,1,2,3,4], "start_hour": 9, "end_hour": 18 },
  "primer": [{ "user": "Can you build it?", "assistant": "I can. Whether you should is your problem." }]
}
```

A contact either carries its personality in its own built Ollama model (Alfred
does — see `Modelfile`) or declares a `system` prompt and shares a base model.
The second needs no `ollama create`.

**`primer` is the important field.** Those exchanges are injected as real
user/assistant turns at the head of every context rather than described in
prose inside the system prompt. A model imitates a conversation it can see far
more reliably than a description of one, and it is the single cheapest way to
make a character sound like themselves.

## Project structure

```
alfred-ai/
├── run.py                        # entry point — web console, --cli, --list
├── pyproject.toml
├── wayne/
│   ├── config.py                 # console-wide settings from .env
│   ├── paths.py                  # project-root-anchored locations
│   ├── events.py                 # event vocabulary shared by every frontend
│   ├── contacts/
│   │   ├── profile.py            # Contact, Availability, the directory
│   │   └── profiles/*.json       # one file per character
│   ├── engine/
│   │   ├── session.py            # one conversation: streaming, sentences, turns
│   │   ├── guards.py             # deterministic post-processing (pure functions)
│   │   ├── prompting.py          # context assembly, primer, speech constraints
│   │   └── search.py             # search routing + DuckDuckGo lookup
│   ├── memory/
│   │   ├── history.py            # short-term conversation, per contact
│   │   ├── vault.py              # long-term facts + relevance retrieval
│   │   └── store.py              # atomic writes
│   ├── audio/
│   │   ├── stt.py                # capture + Whisper transcription
│   │   └── tts.py                # ElevenLabs synthesis + local playback
│   └── frontends/
│       ├── cli.py                # terminal (ANSI + afplay)
│       └── web.py                # FastAPI + WebSocket
├── web/                          # the console: no build step, no node toolchain
│   ├── index.html
│   ├── css/console.css
│   └── js/{app,audio,mic,boot,visualizer}.js
├── tests/
├── data/<contact>/               # per-contact memory (gitignored)
└── scripts/launch.command
```

The engine never prints and never plays audio — it yields the events in
`events.py`, and a frontend decides how to render them. That is what lets the
terminal and the browser share one conversation implementation, and it is the
seam a phone client would plug into: it would consume the same `/ws` stream and
`/api/audio` endpoints the web console already uses.

## Development

```bash
pip install -r requirements.txt
pytest              # the guards, memory, retrieval, and contact loading
```

## Roadmap

- **Wake-word activation** — ambient mode is always listening; a wake word
  would let it stay closed until addressed.
- **Tool use** — calendar, reminders, home control. Needs a real tool-call loop
  rather than the current single-shot generation.
- **Encrypted memory at rest** — see the limitation below.
- **Companion mobile app** — a thin SwiftUI client against the existing local
  API. A separate client, not a port of the Python app.

## Notes & limitations

- **The lock screen is not access control.** It is checked in the page, the server
  gates nothing on it, and the passcode is plain text in `.env`. It exists because a
  console should feel like one.
- **macOS only** as written — `afplay` and `pynput`'s Accessibility hook aren't
  portable. The web console is closer to portable than the terminal one, since it
  plays audio in the browser.
- **Local-first, not local-only** — the LLM and speech-to-text run on-device; the
  reply text is sent to ElevenLabs for synthesis.
- **No encryption at rest** — memory under `data/<contact>/` is plain text. Don't put
  anything in the vault you wouldn't want readable by anyone with access to the machine.
- **Ambient mode depends on your room.** It leans on the browser's echo cancellation
  to avoid hearing the reply through your speakers; on open speakers in a live room
  it can still retrigger. Headphones make it reliable.

## License

[MIT](LICENSE)
