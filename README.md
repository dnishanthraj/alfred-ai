# Alfred AI

**Status:** `v0.5.0` — early, actively developed. See [CHANGELOG.md](CHANGELOG.md).

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
- **Optional live web search** — explicit lookups are folded into context.
  DuckDuckGo by default (no key needed); set `BRAVE_API_KEY` for a real search
  API with cleaner results and no rate-limiting.
- **Deterministic conversation guards** — anti-repetition, sign-off suppression, and
  length capping run in code rather than relying on the model to police itself.

## Requirements

- macOS (uses `afplay` for playback and macOS Accessibility permissions for the global
  hotkey listener — this project is not cross-platform as written)
- Python 3.11 or 3.12 (not 3.13+ — several dependencies are wheel-only)
- [Ollama](https://ollama.com) installed and running, with the `qwen2.5:14b` base model
  pulled (`ollama pull qwen2.5:14b`)
- An [ElevenLabs](https://elevenlabs.io) account and API key
- A working microphone

## Setup

1. **Clone and install dependencies**

   ```bash
   git clone https://github.com/<your-username>/alfred-ai.git
   cd alfred-ai
   python3.11 -m venv venv       # 3.11 or 3.12 — see the note below
   source venv/bin/activate
   pip install -r requirements.txt
   ```

   > **Use Python 3.11 or 3.12, not 3.13+.** Several dependencies
   > (`tokenizers`, `ctranslate2`, `mlx-whisper`) ship wheels only for those
   > versions; on a newer interpreter pip falls back to building from source
   > and the Rust build fails. A bare `python3` may well point at something
   > newer, so name the version explicitly.

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
of contacts on the left, and the link itself in the middle: a radial spectrum
ring driven by the real FFT of whoever is speaking. Nothing is exposed beyond
`127.0.0.1`.

On load you get a power-on self test and a passcode prompt. That screen is
theatre, but it is also load-bearing: browsers keep an `AudioContext` suspended
until the page receives a user gesture, so the console genuinely cannot come up
without one. **The passcode is not security** — it is checked in the page, the
server gates nothing on it, and it sits in plain text in `.env`. Don't put
anything behind it that needs protecting.

**Nobody is on the line until you call them.** Press **Call** next to a contact
and the instrument materialises; press **End** and it dissolves. There is no
transcript — only the last thing said to you stays on screen, alongside a quiet
echo of what the console heard you say. A conversation held out loud does not
need a log of itself, and a scrollback is the strongest possible reminder that
you are typing at software.

### Talking

Two microphone modes, switchable in the composer:

- **Push** — hold the mic button or hold **Space**, speak, release. Reliable in
  a noisy room.
- **Ambient** — the channel stays open. A voice-activity detector decides when
  an utterance starts and ends, and **speaking over a reply cuts it off**, the
  way interrupting a person does. The mic button becomes *send now*, so you
  never have to sit through the pause.

Also: type and press Enter, or press **Esc** to silence playback.

### Feedback

A voice assistant that listens while it speaks can hear itself: the reply
leaves the speakers, the microphone picks it up, and it comes back as though
you had said it — after which it answers itself, forever. Three things stop it:
the browser's echo cancellation, a much higher detection threshold (plus a
cooldown) while a reply is playing, and, as the last line of defence, a check
that compares every *spoken* transcript against what was just said aloud and
silently discards a match. Typed input is never subject to that check, so
quoting a reply back deliberately still works.

### Why it feels like a conversation

Two things, both of which are about timing rather than the model:

- **Sentences are synthesized as they are written.** The engine emits each
  sentence the moment it is complete and several go to ElevenLabs at once, so
  the first line is already playing while the rest is still being generated —
  roughly half a second to first audio instead of waiting out the whole reply.
- **The transcript is revealed in time with the speech.** Words appear as they
  are spoken, spread across each clip's real duration. Printing the reply the
  instant the model finishes reads as a chat log with a voice bolted on.

### Memory

Each contact keeps two kinds of memory under `data/<id>/`: the recent
conversation, and a vault of facts you explicitly asked them to remember.

Both record *when*. Vault facts are dated, because "I moved to London" means
something different learned last week than learned two years ago, and the
conversation is timestamped so a contact knows whether it has been ten minutes
or three weeks — the difference between "Evening again" and "It's been a while."
Timestamps are never sent to the model as data; they are turned into plain
English first.

- **"remember that …" / "note that …"** — stores a fact in that contact's vault.
- **"forget that …"** — removes matching facts.
- **"clear memory" / "protocol zero"** — wipes that contact's history and vault.

Memory is encrypted at rest when `WAYNE_MEMORY_KEY` is set:

```bash
python run.py --new-key      # prints a key to paste into .env
```

Existing plaintext memory keeps working and is re-encrypted as it is next
written. This is real encryption, unlike the lock screen — but the key lives in
`.env` beside the data, so it protects against casual reading, backups and sync
clients, not against someone who already has your `.env`. **Lose the key and
the memory is unreadable.**

### When the voice fails

If ElevenLabs is unreachable, out of quota, or unconfigured, the console does
not show a stack trace. The voice link degrades and the contact carries on in
text. The reply still reaches the screen — the page renders any sentence that
never got audio.

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

`forbidden_address` is enforced in code rather than left to the prompt: a
smaller, faster model will ignore "never call him lad" often enough to matter,
and one slip undoes a great deal of careful prompting.

**`primer` is the important field.** Those exchanges are injected as real
user/assistant turns at the head of every context rather than described in
prose inside the system prompt. A model imitates a conversation it can see far
more reliably than a description of one, and it is the single cheapest way to
make a character sound like themselves.

## Latency

Warm, on an M-series Mac: **~0.5s to first token, ~34 tok/s, about 0.3s from
the first sentence being written to audio playing.** Measured on the same
machine, `qwen2.5:14b` gave 0.33s / 23 tok/s and `qwen2.5:32b` was unusable at
0.3 tok/s — it swaps.

Two things matter more than the model:

- **`ALFRED_MODEL_KEEP_ALIVE`** (default `1h`). Ollama evicts a model after five
  minutes idle by default, and reloading a 14B costs around 25 seconds — which
  is the entire difference between "instant" and "did it crash?" for a
  conversation resumed after a coffee. The server also warms the default
  contact's model at startup, while you are still reading the boot screen.
- **Sentence pipelining.** Speech starts on sentence one rather than after the
  whole reply.

If you use a **reasoning model** (the qwen3 family, deepseek-r1, gpt-oss), set
`"think": false` in that contact's profile. Left on, they spend their whole
budget on reasoning tokens, emit no speakable content, and appear to hang.

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

- **Tool use / function calling** — calendar, reminders, home control, and the
  ability for a contact to *show* you something rather than describe it. Needs
  a real tool-call loop rather than the current single-shot generation.
- **Wake-word activation** — ambient mode listens to everything; a wake word
  would keep the pipeline closed until addressed.
- **Embedded results** in the transcript, once tool use lands.
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
- **Memory encryption is opt-in and key-adjacent.** Without `WAYNE_MEMORY_KEY`,
  memory under `data/<contact>/` is plain text. With it, the key still lives in
  `.env` on the same disk — good against casual reading and backups, not
  against someone who has that file.
- **Ambient mode depends on your room.** It leans on the browser's echo cancellation
  to avoid hearing the reply through your speakers; on open speakers in a live room
  it can still retrigger. Headphones make it reliable.

## License

[MIT](LICENSE)
