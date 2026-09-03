# Changelog

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning is informal pre-1.0 — breaking changes can land in a minor bump.

## [0.4.0] - 2026-09-04

### Fixed

- **Ambient voice mode never registered speech.** Five separate defects, found
  by driving the browser with a recorded phrase as a fake microphone:
  - the noise floor adapted at a 0.13s time constant against ~375 frames a
    second, so it climbed to meet each utterance within the onset window and
    the threshold permanently outran the voice;
  - the onset counter reset to zero on any quiet frame, and speech is full of
    micro-gaps, so a normal sentence never accumulated enough sustained energy;
  - `autoGainControl` ramped gain during pauses, lifting room noise above the
    threshold so a take opened and then never closed;
  - detection ran on raw ~3ms frame energy, far shorter than the gaps between
    words, cutting sentences in half;
  - a single threshold made the detector chatter, splitting one utterance into
    two half-transcribed fragments.
  Now: asymmetric noise-floor tracking, a smoothed envelope, an onset counter
  that decays rather than resets, AGC off, and two-threshold hysteresis.
- **Replies were delayed by up to 25 seconds.** Ollama evicts a model after five
  minutes idle; every resumed conversation paid a full reload. Added
  `ALFRED_MODEL_KEEP_ALIVE` (default `1h`) and a startup warm-up.
- **Ciphertext could be read back as if it were plaintext.** Fernet tokens are
  base64, so decoding one as text succeeds — undecryptable memory has to be
  recognised, not merely fail to parse.
- The test suite created `data/<id>/` directories as a side effect, because
  naming a contact's memory path also created it.

### Added

- **Encrypted memory at rest** — Fernet via `WAYNE_MEMORY_KEY`, generated with
  `python run.py --new-key`. Transparent to callers, and plaintext written
  before a key was set keeps working, so enabling it never looks like amnesia.
- **Memory knows when.** Vault facts are dated, and conversation history is
  timestamped so a contact can tell whether the last exchange was ten minutes
  or three weeks ago. Timestamps are stripped before the model sees the
  messages and turned into plain English for the greeting instead.
- **Graceful voice failure.** Synthesis errors degrade the voice link in-fiction
  and the contact continues in text, rather than surfacing an error.
- **Reasoning-model support** — a contact profile may set `"think": false`.
  qwen3-family models otherwise emit only reasoning tokens and appear to hang.
- **Send-now in ambient mode** — the mic button ends the current take rather
  than waiting out the silence hangover.
- `ConsoleMic.vad` exposes the detector's live state for tuning against a room.

### Changed

- Alfred's primer extended toward canonical register: British understatement,
  refusal, and warmth expressed dryly.
- Removed the stale `alfred/` package left behind by the 0.3.0 restructure.

## [0.3.0] - 2026-09-03

### Added

- **A directory of contacts.** The console is now a phone book rather than a
  single assistant. A contact is a JSON profile (`wayne/contacts/profiles/`)
  declaring model, voice, accent colour, sampling parameters, availability, and
  worked examples; each keeps its own memory under `data/<id>/`. Adding a
  character is a file, not a code change.
- **`primer` — worked examples as real conversation turns.** Style examples are
  injected as actual user/assistant messages at the head of the context instead
  of being described in prose inside the system prompt. A model imitates a
  conversation it can see far more reliably than a description of one.
- **Sentence-chunked, pipelined speech (roadmap "Phase 1.5").** Each sentence is
  synthesized the moment the model finishes writing it, several in flight at
  once, released strictly in order. Time to first audio drops from the length of
  the whole generation to roughly half a second.
- **Transcript revealed in time with the voice.** Words appear as they are
  spoken, spread across each clip's real duration and weighted by word length,
  rather than being printed before the first syllable.
- **Ambient microphone mode** alongside push-to-talk: an always-open channel with
  energy-based voice-activity detection, a pre-roll buffer so takes don't start
  mid-syllable, and barge-in — talking over a reply cuts it off.
- **Boot sequence and lock screen.** A power-on self test and a passcode prompt,
  which also supplies the user gesture browsers require before an AudioContext
  will start. Explicitly theatre, not security, and documented as such.
- **Search sources are cited** in the console under the answer.
- **"forget that …"** removes matching facts from a contact's vault.
- **Vault relevance retrieval** — under 40 facts the whole vault is sent; above
  that, entries are scored against the prompt and only the best are included.
- **Test suite** — 58 tests over the guards, memory, atomic writes, retrieval,
  sentence boundaries, contact loading, and search routing.
- `pyproject.toml` with ruff and pytest configuration.

### Changed

- **Restructured into `wayne/`** with `engine/`, `memory/`, `audio/`,
  `contacts/`, and `frontends/` packages, replacing the flat `alfred/` module.
- **Blue palette and a new abstract mark.** The bat emblem is gone from the
  console and the favicon; the identity is now a concentric-aperture glyph that
  matches the spectrum ring.
- Existing `batcomputer_history.json` / `batcomputer_vault.txt` are migrated
  into `data/alfred/` on first run and the originals renamed, not deleted.
- Replies now carry an explicit spoken-output constraint (no markdown, lists,
  URLs, or emoji), since everything generated is read aloud.

### Fixed

- **"night" matched as a bare substring** in the leaving-cue check, so "the
  night shift was brutal" read as a goodbye and unlocked the farewell sign-off
  the guards exist to suppress. Cues are now matched on word boundaries.
- **A reply could render twice.** A deferred transcript flush scheduled by one
  turn could fire during the next, closing a turn that was still being spoken
  into and leaving the audio to reveal it again in a fresh bubble.
- **The turn could settle before its audio finished**, because `reply_end` fires
  when the model stops writing, not when the last sentence has been synthesized.
  Added a `turn_complete` event for the real end of a turn.
- Guards that could not previously run mid-stream now do: re-greeting is applied
  to the first sentence before it is spoken, farewells are buffered until known
  to be trailing, and the repetition check runs on sentence one, before any
  audio has been committed.

## [0.2.0] - 2026-09-03

### Added

- **Web console** — a local WayneTech-styled GUI at `http://127.0.0.1:8420`,
  served by FastAPI with no build step or node toolchain. Radial spectrum
  visualizer driven by a real FFT of the TTS audio, live status readouts,
  hold-to-talk (button or Space), streamed replies, and transcript replay
  across reloads. `python run.py` now starts it; `--cli` keeps the terminal.
- **Headless engine** (`alfred/core.py`) that yields events (`alfred/events.py`)
  instead of printing. The terminal and the browser are now two renderers over
  one conversation implementation.
- Streaming generation — tokens appear as the model produces them.
- Config validation at boot, and `ALFRED_WEB_HOST` / `ALFRED_WEB_PORT` /
  `ALFRED_MAX_REPLY_SENTENCES` / `ALFRED_TTS_MODEL` settings.

### Fixed

- **Push-to-talk could hang the app.** A tap fast enough to release before the
  recorder's own key listener attached meant the release event never arrived
  and `listener.join()` blocked forever. Recording now polls a predicate the
  caller owns, removing the second listener entirely.
- **Sign-off stripping deleted legitimate text.** `take care\b.*` matched
  "Take care of the deployment first." and silently dropped it. Farewell
  patterns are now anchored.
- **The anti-repetition guard fought the persona.** Deliberately terse replies
  ("Mm.", "Go on.") were flagged as loops and regenerated at temperature 0.95,
  pushing the model away from the style the Modelfile asks for. Replies under
  four words are now exempt.
- **History writes were not atomic.** A crash mid-write truncated the file, and
  since unparseable history is treated as "no history", that silently wiped the
  conversation memory. Writes now go through a temp file and `os.replace`.
- **Synthesis blocked the input loop.** `_speak_async` ran the ElevenLabs round
  trip on the calling thread before spawning its playback thread.
- Importing `alfred.main` no longer loads a Whisper model, starts a keyboard
  listener, or prints — the STT backend resolves lazily on first use.
- The memory vault is de-duplicated and capped; it is injected into every
  prompt and previously grew without bound.
- Whisper's hallucinated punctuation from silence (`"."`) is no longer
  submitted as a prompt.
- `launch.command` no longer puppeteers Ghostty through System Events
  keystrokes; it starts the server directly.
- Dropped the dead `{"think": False}` generation option — `think` is a
  top-level chat parameter, not an option key, so it was silently ignored.

### Changed

- `ollama` pinned to `0.6.2`; `0.1.0` forced an `httpx` old enough to conflict
  with `ddgs` and made the requirements unresolvable.

## [0.1.0] - 2026-08-08

### Added

- Initial public release: push-to-talk voice input (Whisper), local LLM
  reasoning (Ollama), ElevenLabs speech output, short/long-term memory,
  optional web search, deterministic conversation guards.
- Refactored into an `alfred/` package with env-driven configuration
  (`alfred/config.py`) so personal identity/API keys stay out of source.
- `Modelfile.example` personality template, `.env.example` config template.
