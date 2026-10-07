"""Stage cues: performed by the voice, never shown or remembered."""
from wayne import delivery, events
from wayne.memory import History


def test_cues_reach_the_voice_but_not_the_screen(monkeypatch):
    monkeypatch.setattr(delivery.config, "ELEVENLABS_MODEL", "eleven_v4_turbo")
    event = events.sentence(0, "[sighs] Of course you did.")
    assert event["text"] == "Of course you did."
    assert event["voice"] == "[sighs] Of course you did."


def test_unknown_cues_are_dropped_everywhere(monkeypatch):
    monkeypatch.setattr(delivery.config, "ELEVENLABS_MODEL", "eleven_v4")
    assert delivery.voiced("[dramatic music] Right.") == "Right."
    assert delivery.voiced("[Whispers] Quiet.") == "[whispers] Quiet."


def test_older_voices_get_no_cues_at_all(monkeypatch):
    # They read every cue out loud.
    monkeypatch.setattr(delivery.config, "ELEVENLABS_MODEL", "eleven_turbo_v2_5")
    assert delivery.voiced("[sighs] Of course you did.") == "Of course you did."


def test_memory_keeps_his_words_without_cues_and_yours_verbatim():
    h = History.__new__(History)
    h.contact_id = "test"
    h.messages = []
    h.append("user", "what does [x] mean")
    h.append("assistant", "[laughs] Nothing good.")
    assert [m["content"] for m in h.messages] == ["what does [x] mean", "Nothing good."]


def test_the_finished_reply_is_shown_clean():
    assert events.reply_end("[sighs] Fine. Have it your way.")["text"] == "Fine. Have it your way."


def test_word_timings_skip_cues():
    from wayne.audio.tts import word_starts
    text = "[sighs] Of course."
    starts = [i * 0.1 for i in range(len(text))]
    assert word_starts(list(text), starts) == [["Of", 800], ["course.", 1100]]
