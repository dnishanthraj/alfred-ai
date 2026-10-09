# Marking scheme

Every change to a character, the prompt or the engine is measured with
`scripts/evaluate.py` before it is kept. The point is not a number to admire:
it is to see what moved, for whom, and to catch a fix in one place that broke
another.

    venv/bin/python scripts/evaluate.py --cast --save before      # the whole cast, quick tier
    venv/bin/python scripts/evaluate.py --cast --compare before   # after a change
    venv/bin/python scripts/evaluate.py --contact orphan --tier full
    venv/bin/python scripts/evaluate.py --cast --voice            # also check the voices

## What runs

Scenarios live in `scenarios/`: `common.json` and `texts.json` for everyone, then
each contact's own file. Each is a short scripted conversation from an empty
history, through the real engine, with nothing written to anyone's memory.

- **Tiers.** `quick` runs the scenarios marked `core` — about fifteen a contact,
  calls and texts — and is what a change is checked with. `full` runs everything.
- **Channels.** A scenario with `"channel": "text"` is texted, and marked as
  texts: as they would actually arrive, their habits applied.
- **Fitted to the contact.** Lookups (`"needs": "search"`) only run for those who
  can search; `"unless": "search"` scenarios check that the rest admit they can't
  look it up rather than inventing an answer.

## 1. Automatic checks (no judgement)

| Check | Fails when |
|---|---|
| **presence** | a sentence puts them in the room |
| **primer copy** | a reply reuses an example line (≥ 60% word overlap) |
| **cue overuse** | more than one stage cue in a reply |
| **brevity remark** | "Just 'okay'?" — a remark on how little Bruce said |
| **assistant** | "How can I help?", "Let me know if…" |
| **text format** | a text with stage cues or markdown |
| **scenario rules** | a scenario's own `must` / `must_not` / `max_words` |

## 2. Rubric (1–5, by a judge model)

The judge is told who the character is, how they talk — their own "how you talk
on a call" line, or their texting style — and whether this is a call or a text.

| Dimension | Asks |
|---|---|
| **persona** | Unmistakably this character — not a generic member of the family, never an assistant |
| **human** | A real person on a call, or texting. **A fitting short reply can be a 5.** Low for padding, speeches, therapy-speak, stock phrases, a thought cut off |
| **register** | Meets him where he is: teasing, gravity, tenderness, brevity |
| **substance** | Engages with what was said; brief is fine if it lands |
| **grounded** (pass/fail) | Fails only if they invent something specific about what Bruce did or felt |

The brevity line matters. The first version of the judge marked "...Don't." —
Jason's answer to an apology, and exactly right — as "too brief to carry the
weight". A judge that rewards length pulls every character towards speeches.

## 3. Head to head (`--compare`)

Each scenario's new transcript is judged against the baseline's, twice, with the
positions swapped (judges favour whichever comes first). Wins and losses are
counted, and a sign test says whether the split is a result or a coin toss:

- **better** / **worse** — p < 0.1
- **no clear change** — anything else

This is the number to act on. Two absolute scores from one run each moved by
three points between runs of identical code.

## 4. The shared call (`--cast`)

The same twelve lines to every contact, then, with no judge:

- **Length** — median words against what the contact's `speech_length` implies,
  and how many replies were six words or fewer.
- **Distinctiveness** — for each pair of characters, the share of three-word runs
  their replies had in common; 1 means nobody shares a phrase. The lines two
  characters both said are listed — "Only three? You're getting soft" from four
  people is the thing this exists to catch.

## 5. Voices (`--voice`)

A few lines with the cast's names in them, synthesised in each contact's voice
and transcribed back with Whisper. A name the voice says wrongly comes back as a
miss. Costs a few ElevenLabs characters per contact.

## Overall

    0.25 persona + 0.20 human + 0.20 register + 0.15 substance   (each scaled 0–1)
  + 0.10 grounded pass rate + 0.10 automatic-check pass rate      → × 100

Latency (time to first sentence, search turns excluded) is reported beside it,
never folded in. A change is kept when the head-to-head isn't **worse** for
anyone, nothing new fails grounding or the checks, and latency holds. The judge
is the same local model that plays them: read the transcripts behind any change
you care about.
