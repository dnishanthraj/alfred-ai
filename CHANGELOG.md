# Changelog

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning is informal pre-1.0 — breaking changes can land in a minor bump.

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
