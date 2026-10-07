# Alfred AI

**Status:** `v0.9.5` — early, actively developed. See [CHANGELOG.md](CHANGELOG.md).

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
- **Search that runs before he can invent an answer** — a question that plainly
  needs a current fact is looked up on the way in and he is handed what was
  found, rather than being trusted to ask for it — a character asked to look
  something up will happily explain why he cannot, and then guess. Opinions,
  feelings, and anything about either of you never go to the web. He can still disbelieve the results, or
  tell you to do your own homework. DuckDuckGo by default (no key needed); set
  `BRAVE_API_KEY` for a real search API.
- **He knows he is on a link, not in the room** — no offering you tea, no telling
  you to sit down, no remarking on how you look. He has his own location, his own
  evening and a terminal to look things up on; what he cannot do is see you.
- **He will not invent your life** — anything about your day, your work or your
  plans has to have been said by you or be in the vault, or he asks instead of
  guessing. The worked examples that teach his voice are kept out of the
  conversation entirely and labelled as fiction, because a model cannot tell a
  sample turn from a real one and will otherwise recall them as your history.
  His own side is unrestricted: what *he* has been doing is his to make up, and
  nobody can be contradicted about their own afternoon.
- **One character, several registers** — he banters when you are light, is dry
  and brief in passing, and goes wholly serious the moment something is actually
  wrong. The tone is decided per turn from what you just said and attached to it,
  rather than set once and averaged into everything.
- **He knows what is going on around you** — a live weather reading for where
  you are, the headlines, and today's calendar, each fetched in the background
  and handed to him with the time it was read. Opt-in, one line of `.env` each.
  Anything else in the world is looked up when a question needs it.
- **He knows what time it is, for both of you** — no suggesting bed at three in
  the afternoon, and no greeting you for the wrong half of the day.
- **Deterministic conversation guards** — anti-repetition, sign-off suppression,
  physical-presence stripping, and length capping run in code rather than relying
  on the model to police itself.

## Requirements

- macOS (uses `afplay` for playback and macOS Accessibility permissions for the global
  hotkey listener — this project is not cross-platform as written)
- Python 3.11 or 3.12 (not 3.13+ — several dependencies are wheel-only)
- [Ollama](https://ollama.com) installed and running, with `gemma4:26b-a4b-it-qat`
  pulled (15 GB; needs a 24 GB Mac) — or `gemma4:e4b` (6.6 GB) on a smaller one
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

2. **Tell him who you are**

   Alfred's character is committed in
   [`wayne/contacts/profiles/alfred.json`](wayne/contacts/profiles/alfred.json).
   What he knows about *you* — name, work, where you live — goes in `Modelfile`,
   which is gitignored so your details never leave your machine:

   ```bash
   cp Modelfile.example Modelfile
   ```

   Fill in its `SYSTEM` block, then pull the model:

   ```bash
   ollama pull gemma4:26b-a4b-it-qat     # or gemma4:e4b on a 16 GB Mac
   ```

   There is no `ollama create`: the profile's character and your `Modelfile`
   are read at startup and sent to the base model together, so an edit to
   either takes effect on the next launch.

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
   | `ALFRED_OLLAMA_MODEL` | No | Ollama model tag for Alfred (default: `gemma4:26b-a4b-it-qat`, from step 2) |
   | `WAYNE_PASSCODE` | No | Lock-screen passcode (default: `zorro`). Theatre, not security |
   | `ALFRED_WEB_PORT` | No | Console port (default: `8420`) |
   | `ALFRED_PTT_KEY` | No | [pynput](https://pynput.readthedocs.io) key for `--cli` push-to-talk (default: `Key.cmd_r`) |
   | `ALFRED_WHISPER_HINTS` | No | Comma-separated proper nouns to bias speech recognition |
   | `ALFRED_CONTEXT_WINDOW` | No | Model context in tokens (default: `8192`). See [Latency](#latency) |
   | `ALFRED_HISTORY_WORDS` | No | Conversation words sent to the model (default: `260`). The main latency dial |
   | `ALFRED_TTS_MODEL` | No | ElevenLabs model (default: `eleven_v4_turbo`; `eleven_v4` is more expressive and ~0.5s slower to start) |
   | `ALFRED_LOCATION` | No | Your town or city, for a live weather feed |
   | `ALFRED_NEWS_FEED` | No | An RSS feed URL for headlines he has glanced at |
   | `ALFRED_CALENDAR` | No | `1` to let him see today's and tomorrow's events in macOS Calendar |

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

   Or build a proper macOS app once:

   ```bash
   ./scripts/make_app.command
   ```

   That produces **WayneTech Console.app** — its own Dock icon, a window with no
   tabs or address bar, its own browser profile. It starts Ollama and the server
   if they aren't running, and stops the server when you quit. Open it once,
   then right-click the Dock icon → Options → Keep in Dock.

   > **You do not rebuild it when you change the code.** The bundle is a
   > launcher that points at this directory — it contains no copy of the
   > project — so Python changes take effect when you quit and reopen it, and
   > front-end changes on a reload (asset URLs are stamped with a version that
   > follows the files, so the browser cannot serve you a stale `app.js`).
   > Editing your `Modelfile` likewise just needs a restart. Re-run the script
   > only if you **move the project**, since the path is baked into the launcher.

   [`scripts/launch.command`](scripts/launch.command) still works if you'd
   rather have a browser tab.

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

**Nobody is on the line until you call them.** Press **Call** and it rings —
amber, pulsing, with a tone — until they pick up, at which point the instrument
materialises and turns blue. Press **End** and it flashes red and dissolves.
The ring is not only dressing: it covers the seconds the model spends loading,
so the wait reads as a call connecting rather than software thinking about it.

Click a contact's **name** to open their personnel file — who they are to you,
in your own words, editable and saved per contact. Drop an image at
`web/portraits/<id>.png` (`.jpg` and `.webp` work too) for a portrait; otherwise a
silhouette stands in. No cropping needed — the slot frames to head-and-shoulders
itself, so a full square render with air around the subject lands correctly. What
you put there is gitignored, since a portrait is usually either personal or
someone else's copyright. There is no
transcript — only the last thing said to you stays on screen, alongside a quiet
echo of what the console heard you say. A conversation held out loud does not
need a log of itself, and a scrollback is the strongest possible reminder that
you are typing at software.

### Talking

Two microphone modes, switchable in the composer:

- **Push** — hold the mic button, **Space**, or **Right Command**, speak,
  release. Reliable in a noisy room.
Ambient (always-listening) mode is built but **disabled in the interface**: the
detector triggers on room noise and mis-hears often enough to derail a
conversation. Push-to-talk is unambiguous, so it is the only mode until that is
fixed.

Also: type and press Enter, or press **Esc** to silence playback. Interrupting
works — a new message stops him mid-sentence, and the line on screen stops
where his voice did.

### Presence

He breaks a silence himself after half a minute or so — briefly, generated in
character so it differs every time, and never written to memory. The second
check comes sooner than the first, and after that he closes the call rather
than sitting on a dead line. Asking him for a minute buys you one; if *he* asks
for a moment he takes it and comes back on his own.

He also notices things a transcript would not: that you have said something
three times now, that you have talked over him again, that you said it was
urgent, or that something is actually wrong. Interrupting works — a new message
stops the reply in flight rather than queueing behind it.

A stack of guards runs on every reply before it is spoken, because the failures
that break the illusion are specific and recurring: handing your own words back
to you, greeting you twice, repeating himself, inventing a fact rather than
looking it up, or promising to look and then not looking.

### Hearing you

Speech-to-text runs on `whisper-small.en`, chosen by measurement: ~0.2s on an
M-series Mac, where a model four times the size was three times slower and no
more accurate. The accuracy is instead in the hints — the console tells the
decoder who is on the line and what was just said, which took word accuracy on
hard audio from 83% to 100%. If a name is still coming out wrong, add it to
`ALFRED_WHISPER_HINTS`.

Whisper does not fail by going quiet — it fails by producing a confident
sentence nobody said, which then steers the conversation somewhere it was never
going. Its own uncertainty signals are passed to the contact, who asks rather
than assumes when the audio was poor.

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

To wipe it by hand, delete the files under `data/<contact>/` — for Alfred:

```bash
rm data/alfred/history.json    # the conversation; safe to delete any time
rm data/alfred/vault.txt       # long-term facts he was told to remember
rm data/alfred/bio.txt         # your edits to his dossier card
rm -rf data/                   # everything, for every contact
```

Only `history.json` is the running conversation — deleting it starts him fresh
without losing what he has been told to remember. The whole directory is
gitignored and is recreated on the next launch, so there is nothing to restore.
Do it with the server stopped, or it will write the in-memory copy back out.

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

A contact declares its personality one of three ways: `system` inline (as above),
`system_file` pointing at a Modelfile whose `SYSTEM` block is read at startup (what
Alfred does, so the prompt can stay gitignored), or a pre-built Ollama model carrying
it internally. Only the third needs `ollama create`, and it is the one to avoid unless
you have reason to: a derived build can silently return empty replies.

`max_reply_sentences` is a runaway ceiling, not a style control — length is
steered by the prompt and, far more effectively, by the range demonstrated in
`primer`. A primer whose replies are all the same length teaches exactly that.

`forbidden_address` is enforced in code rather than left to the prompt: a
smaller, faster model will ignore "never call him lad" often enough to matter,
and one slip undoes a great deal of careful prompting.

**`primer` is the important field.** Those exchanges are injected as real
user/assistant turns at the head of every context rather than described in
prose inside the system prompt. A model imitates a conversation it can see far
more reliably than a description of one, and it is the single cheapest way to
make a character sound like themselves.

## Latency

Measured on an M4 Pro (24 GB) with `scripts/bench_model.py`, which runs the real
engine — persona, primer, guards, search — over a scripted conversation without
touching memory. The number is time to the first sentence, which is when speech
synthesis starts:

| Model | First sentence (median) | Verdict |
|---|---|---|
| `gemma4:26b-a4b-it-qat` | **0.84s** | Default. Best character by a distance; 4B active parameters, so fast |
| `gemma4:e4b` | 0.66s | Fallback for smaller Macs; flatter, a little harsh |
| `gemma4:12b` (MLX) | 1.61s | Good voice, uneven latency |
| `qwen3.5:9b` | 2.02s | Re-reads the whole prompt every turn (below); invents the most |
| `gemma4:12b` | 2.62s | 13 tok/s on the llama.cpp path |
| `gpt-oss:20b` | 0.95s | Fast, but leaks fragments ("Done.Got it.") and isn't a character |

Run it yourself after changing anything: `venv/bin/python scripts/bench_model.py <model>`.

**The architecture of the model matters more than its size.** `qwen3.5` is a
hybrid with recurrent layers, which cannot resume from a cached prompt — so its
~2,400-token prefix was read from scratch on every turn, 1.6s before a word. A
conventional transformer reuses the cached prefix and reads the same prompt in a
fifth of a second, which is why a 26B mixture-of-experts answers faster than a 9B
hybrid. The prompt is laid out for that cache: everything stable comes first,
and only the per-turn reference block (time, feeds, search results) changes.

The rest of the pipeline:

- **Speech** — ElevenLabs over its streaming endpoint, one sentence at a time,
  several in flight. Per sentence: `eleven_v4_turbo` ~0.55s, `eleven_v4` ~1.0–1.4s,
  `eleven_turbo_v2_5` ~0.35s. The connection is pooled, so the TLS handshake is
  paid once rather than per sentence. The page prefetches and decodes each clip
  as it is queued, so sentences play back to back.
- **The ring** — a call's greeting is generated and voiced while the line rings,
  so he speaks the moment he picks up.
- **Interruptions stop the model.** Talking over him closes the stream, which
  stops Ollama; the next turn no longer waits behind a reply nobody will hear.
  Replies that reach the sentence cap stop being read the same way.
- **The history window moves in steps.** It is trimmed with headroom, so its
  opening — the start of what the cache can reuse — holds still for several
  turns rather than shifting every turn.
- **Ambient feeds never block.** Weather, headlines and calendar refresh on
  background threads; a turn reads whatever is cached.
- **`ALFRED_HISTORY_WORDS`** (default `260`), **`ALFRED_CONTEXT_WINDOW`**
  (default `8192`) and **`ALFRED_MODEL_KEEP_ALIVE`** (default `1h`) are the dials.
  The server warms the model with the real prompt prefix at startup, so the
  first reply is not the one that pays for loading.

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
