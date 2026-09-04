# Changelog

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning is informal pre-1.0 — breaking changes can land in a minor bump.

## [0.9.0] - 2026-09-04

### Fixed

- **Alfred had stopped being Alfred.** He introduced himself as "an artificial
  intelligence assistant". The previous release moved standing instructions
  into a system message for the latency win — and a system message sent at
  runtime *replaces* an Ollama model's baked SYSTEM prompt rather than adding
  to it, so the entire character was being deleted on every turn. The original
  code carried a comment warning about exactly this. The directives now live in
  the Modelfile itself; contacts that declare `system` in their profile get
  them merged, which is safe because there is no baked prompt to overwrite.
  **The latency win is kept: they are still in the cached prefix.**
- **Check-ins piled up as "Still here. Still here."** Asides were concatenated
  onto the previous turn, so a second one stacked onto the first — and once
  that was in the context he produced more of it. An aside is now its own
  field, and a newer one replaces the older, which is what actually happened.
- **The model leaked Chinese** into replies. It is now told to answer only in
  English, and never to write instructions to itself.
- `"boy"` added to Alfred's forbidden forms of address.

### Added

- **A real macOS app.** `scripts/make_app.command` builds "WayneTech
  Console.app" — its own Dock icon, a chromeless window with no tabs or address
  bar, and its own browser profile so it doesn't disturb your session. It
  starts Ollama and the server if they aren't running, and stops the server
  when you quit. Generated rather than committed, and rebuilt if the project
  moves.
- **Transcription confidence.** Whisper does not fail by going quiet, it fails
  by producing a confident sentence nobody said — which then steers the whole
  conversation. Its own signals (log-probability, repetition, silence
  likelihood) now reach the contact, who is told to ask rather than assume when
  the audio was poor. Whisper's stutter loop — "the film was made in the early
  90s, and the film was made in the early 90s" — is folded back to one clause.

### Changed

- **Ambient mode is disabled in the interface**, pending work on the detector:
  it triggers on room noise and mis-hears often enough to derail a
  conversation. Push-to-talk is unambiguous. The code path is intact; only the
  control is withheld.

## [0.8.1] - 2026-09-04

### Fixed

- **Replies had slowed to four seconds.** The reference block rides on the last
  message so it is recomputed every turn — and it had grown to 452 words, most
  of which never changed: how to write for speech, how long a reply should be,
  how to use a lookup. Standing instructions now live in a directives message
  inside the cached prefix, leaving 56 words in the tail. **4.0s to first
  sentence became 1.1s; 4.6s to first audio became 1.4s.**
- **Interrupting him still showed the rest of the sentence.** Words are revealed
  on timers spread across the clip, so stopping the audio left them firing: he
  fell silent while what he *would* have said carried on appearing. The line now
  stops where his voice did, marked with a dash, and the remainder is never
  rendered — it was never heard. The cut line survives the question that
  interrupted it, since that is precisely what you want to see.

## [0.8.0] - 2026-09-04

Everything here came out of reading one real transcript. It contained a
contact who invented facts, promised to look things up and didn't, dismissed
an explicit "it's urgent", and answered "You tell me." with "You tell me."

### Fixed

- **He invented answers instead of looking them up.** Asked "what do we have on
  Waylon Jones?" he replied that they were a tech firm keeping their heads
  down — entirely made up, stated as fact. Search had been gated behind
  keywords like "look up", so that question never even reached the decision.
  The gate is gone: a lookup is offered on every turn and he chooses, with an
  explicit instruction never to state a guess as knowledge.
- **He promised to search and then didn't.** "I'll see what I can find" would
  be spoken and nothing would follow. The marker is now watched for across the
  whole reply, so saying he will look and then looking is one turn.
- **He ignored stated urgency**, answering "it's urgent, I need it now" with a
  remark about keeping things in order. Urgency is detected and he is told to
  do the thing rather than counsel about pace.
- **He handed the operator's own words back.** "You tell me." → "You tell me."
  A parrot check now runs before anything is spoken — including on replies too
  short to contain a sentence boundary, which never reached the check at all
  and are exactly the ones most likely to be echoes. The retry is checked too:
  asked not to parrot, a model will happily parrot again.
- **He missed distress**, answering "I'm feeling pretty low today" with a
  remark about the morning. Now detected, and everything else stops.
- **Search queries were truncated mid-stream.** The marker was matched against
  the end of a *partial* buffer, so "[SEARCH: Way" searched for "Way" and came
  back with a cycling route called King Alfred's Way. A marker must be complete
  while tokens are still arriving.
- **The history placeholder taught the model to emit directives.** A literal
  "[link established]" sitting in the context produced "[SEARCH: link
  established]", and a report on an IT company of that name. Placeholders now
  look like something a person would say.
- **Assistant voice.** "I don't have real-time updates, check the news" is not
  a person talking. He now either knows, finds out, or says he has no idea.

## [0.7.1] - 2026-09-04

### Fixed

- **Six consecutive "Morning."s**, from a real conversation. Three guards each
  declined to catch it for a different reason, and all three are fixed:
  - `strip_regreeting` returned the greeting unchanged whenever stripping it
    would empty the reply — which is exactly the reply most in need of
    stripping. It now returns empty and the caller substitutes.
  - `too_similar` exempted anything under four words, to protect "Mm." and
    "Go on.". That also exempted "Morning.". Exemption is now by being a
    *backchannel* — a phrase that carries meaning by recurring — not by length.
  - `count_repeats` ignored anything under three words, so a repeated one-word
    greeting was invisible. Same backchannel rule.
- **The loop check compared a sentence against whole replies.** It runs on the
  first sentence of a reply but measured it against entire previous ones, which
  scores too low to ever fire — so "You've said morning nine times now… ten…
  eleven…" could run indefinitely. Openings are now compared with openings.
- **Repetition was remarked on every single turn**, announcing a running total,
  which is as mechanical as the repetition it complains about. He says it once
  and then deals with whatever is behind it. Observed afterwards: a remark,
  then "You've said that. Care for some tea now?", then "Tea time?", then "Tea?".
- **A check-in was not remembered.** He would ask "still with me?" and have no
  idea he had asked. Self-initiated lines are now appended to his previous turn,
  which keeps the alternation the model needs while preserving what was said.

### Changed

- History window raised from 16 exchanges to 30. The context freed by trimming
  the system prompt is far better spent on what was actually said.

## [0.7.0] - 2026-09-04

### Fixed

- **Interrupting him did nothing.** A new message queued behind the turn in
  flight, so he finished what he was saying and then answered something you had
  moved past. Turns now carry an epoch: anything newer supersedes them, and a
  superseded turn stops forwarding events and stops synthesising immediately.
- **A silence check-in appended itself to his previous line** instead of
  replacing it, because a new reply only cleared the display when the operator
  had spoken first.

### Added

- **He notices what you repeat.** Paraphrase counts, so "I should call her" and
  "I ought to call her" are the same admission twice. On the third he says so:
  *"You've said that three times now. What's stopping you?"*
- **He notices being talked over**, and after the third time is entitled to
  remark on it.
- **Escalating check-ins, then he hangs up.** The second silence is chased
  sooner than the first, and after that he closes the call himself rather than
  sitting on a dead line — *"I'll keep the kettle on."*
- **"Give me a moment" is real.** If he says he needs one he takes it, nothing
  is expected of you meanwhile, and he comes back on his own: *"Right, where
  were we?"*
- **Per-request speech-to-text hints** built from who is on the line and what
  was last said. Whisper mangles proper nouns it has no reason to expect;
  feeding it the likely ones took word accuracy on hard audio from **83% to
  100%** at no latency cost. `ALFRED_WHISPER_MODEL` makes the model
  configurable — though on measurement a model four times larger was three
  times slower and no more accurate, so the default stands.

## [0.6.1] - 2026-09-04

### Fixed

- **"No active link" sat on top of a ringing call.** The overlay and the
  visualizer were keyed to a `connecting` state that had been renamed to
  `ringing`, so neither reacted to a call being placed, and both reappeared
  mid-hang-up. Every phase other than "off" now clears the empty state.
- **The directory claimed a connection while the line was still ringing** — it
  read `connectedId`, which is set the moment Call is pressed. Ringing is now
  its own state: amber, pulsing, labelled Connecting, with the button offering
  Cancel rather than End.
- **He repeated the same greeting on every call.** Each connection stored a
  placeholder turn and its greeting, so a few calls left a stack of "Good
  morning" in the context and the model wrote another. Superseded greetings are
  now pruned — and because pruning also removes the evidence that he greeted at
  all, the previous greeting is carried into the next boot prompt as something
  to avoid rather than to copy.

### Changed

- **Calls ring for a varying moment before they are answered.** The model now
  replies in a fraction of a second, which reads as a machine waiting for input
  rather than a person crossing a room.
- The silence check-in comes sooner — around half a minute rather than a minute
  and a half, which is closer to how long a real pause runs before someone asks.

## [0.6.0] - 2026-09-04

### Fixed

- **Every reply took 3 seconds longer than it needed to.** The Modelfile's
  SYSTEM prompt had grown to 1440 words and was evaluated on every turn:
  0.27s to first token on the base model against 3.27s with it. Roughly 900 of
  those words were worked examples now carried far more effectively by
  `primer`, and several rules had since become code guards. Trimmed to ~490
  words — **3.27s to 0.13s**.

### Added

- **Natural dialogue length.** `max_reply_sentences` was a style control that
  truncated anything longer, so a monologue was impossible by construction.
  It is now a runaway ceiling (9), and length is steered by the prompt: an
  explicit description of the distribution, a per-turn hint that mirrors the
  operator's own register, and a primer rewritten to demonstrate the full range
  from one word to a paragraph. Observed replies now run 1, 2, 3, 9, 36 and 50
  words to different prompts.
- **Search is a decision, not a trigger.** A keyword no longer forces a lookup.
  When a request *might* be one, the contact answers first and may ask what is
  actually wanted, refuse on principle, or emit a marker asking to go and look —
  only the last costs a search. "Look it up for me" now gets "look what up,
  precisely?"; "the weather in London tomorrow" gets a search; "trace who owns
  this number" gets refused.
- **Call tones and a ringing state.** Synthesised with oscillators rather than
  shipped as files, so the ring stops dead on the beat it is answered. Ringing
  is amber and pulses; connecting turns it blue; hanging up flashes red and
  dissolves the instrument. The ring also covers model load, so the wait reads
  as a call connecting rather than software thinking.
- **He notices silence.** After a minute or so with nothing said, he breaks it
  himself — generated in character, so it varies, and never stored in history.
  He gives up after two. Asking for a minute buys you one.
- **Right Command as push-to-talk**, alongside Space. A held modifier can
  swallow its own keyup, so losing window focus now closes the microphone.
- **Personnel file per contact** — who they are to you, in your own words,
  editable in the console and stored per contact. Ships with a default written
  from the operator's side, and a portrait slot: drop an image at
  `web/portraits/<id>.png`, otherwise a silhouette stands in.

### Changed

- Alfred's Modelfile now distinguishes surveillance from ordinary lookups. It
  previously said he "cannot look things up", which contradicted the search
  capability and made him refuse the weather.

## [0.5.0] - 2026-09-04

### Fixed

- **Runaway conversation loops.** With the microphone open, a reply left the
  speakers, was picked up, transcribed as the operator, and answered — which
  produced another reply, indefinitely. Now defended three ways: a far higher
  detection threshold plus a cooldown while audio is playing, a longer and
  louder commitment required to barge in, and a semantic check that compares
  every *spoken* transcript against what was just said aloud and discards a
  match. Typed input is exempt, so quoting a reply back on purpose still works.
- **Hanging up mid-reply left the rest of the turn arriving** — sentences kept
  queueing and audio kept playing after the link was closed.
- A new question was displayed against the previous answer for as long as the
  reply took, which read as a non-sequitur.
- Stale rules from the previous layout were duplicated in the stylesheet and
  overrode the new ones.

### Changed

- **The console is a link, not a chat window.** Nothing connects until you
  press Call; the instrument materialises on connect and dissolves on End.
  There is no transcript — only the last thing said to you, with a quiet echo
  of what the console heard from you. Model name, speech backend and
  synthesiser are gone from the interface and from the session payload
  entirely; status text no longer describes machinery.
- **Alfred now runs on `qwen3.5:9b` with reasoning disabled** (`alfred-q35`),
  benchmarked on the same machine at ~34 tok/s against 23 for `qwen2.5:14b`,
  with sharper in-character replies.
- Search is pluggable: DuckDuckGo by default, Brave when `BRAVE_API_KEY` is set.

### Added

- **`forbidden_address`** per contact — forms of address the character would
  never use, stripped deterministically. A smaller model ignores the
  instruction often enough that one "lad" undoes the prompting around it.
- Ambient mode's mic button now ends the current take instead of waiting out
  the silence hangover.

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
