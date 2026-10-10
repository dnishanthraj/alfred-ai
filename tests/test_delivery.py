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


def test_memory_keeps_what_they_did_and_yours_verbatim():
    # So Dick knows he just sighed: the action stays with the line; junk doesn't.
    h = History.__new__(History)
    h.contact_id = "test"
    h.messages = []
    h.append("user", "what does [x] mean")
    h.append("assistant", "[laughs] Nothing good. [dramatic music]")
    assert [m["content"] for m in h.messages] == ["what does [x] mean", "[laughs] Nothing good."]


def test_v4_performs_a_direction_in_its_own_words(monkeypatch):
    monkeypatch.setattr(delivery.config, "ELEVENLABS_MODEL", "eleven_v4_turbo")
    line = "[takes a deep breath] Look, B."
    assert delivery.clean(line) == "Look, B."
    assert delivery.voiced(line) == "[takes a deep breath] Look, B."
    assert delivery.remembered(line) == line
    monkeypatch.setattr(delivery.config, "ELEVENLABS_MODEL", "eleven_v3")
    assert delivery.voiced(line) == "Look, B."


def test_the_finished_reply_is_shown_clean():
    assert events.reply_end("[sighs] Fine. Have it your way.")["text"] == "Fine. Have it your way."


def test_word_timings_skip_cues():
    from wayne.audio.tts import word_starts
    text = "[sighs] Of course."
    starts = [i * 0.1 for i in range(len(text))]
    assert word_starts(list(text), starts) == [["Of", 800], ["course.", 1100]]


def test_only_one_cue_per_piece_of_text(monkeypatch):
    monkeypatch.setattr(delivery.config, "ELEVENLABS_MODEL", "eleven_v4_turbo")
    assert delivery.voiced("[laughs] [sarcastic] Delusions.") == "[laughs] Delusions."


def test_pronunciations_change_the_voice_not_the_screen(monkeypatch):
    from wayne import events
    monkeypatch.setattr(delivery.config, "PRONUNCIATIONS", {"Fox": "Focks"})
    event = events.sentence(0, "Mr. Fox will see you.")
    assert event["text"] == "Mr. Fox will see you."
    assert event["voice"] == "Mister Focks will see you."      # titles said in full, as they're spoken


def test_a_scene_described_in_brackets_is_never_a_subtitle(monkeypatch):
    # "[sounds of grocery bag rustling]" came up on screen in the middle of a call.
    monkeypatch.setattr(delivery.config, "ELEVENLABS_MODEL", "eleven_v4_turbo")
    line = "[sounds of grocery bag rustling] Yeah, I'm at the store."
    assert delivery.clean(line) == "Yeah, I'm at the store."
    assert events.sentence(0, line)["text"] == "Yeah, I'm at the store."
    # v4 performs the sound itself, the way it would come down the line.
    assert delivery.voiced(line) == "[grocery bag rustling] Yeah, I'm at the store."


def test_only_v4_gets_the_sounds_and_never_music(monkeypatch):
    monkeypatch.setattr(delivery.config, "ELEVENLABS_MODEL", "eleven_v3")
    assert delivery.voiced("[car door slams] Right.") == "Right."
    monkeypatch.setattr(delivery.config, "ELEVENLABS_MODEL", "eleven_v4")
    assert delivery.voiced("[car door slams] Right.") == "[car door slams] Right."
    assert delivery.voiced("[soft piano music] Right.") == "Right."


def test_asterisks_and_parentheses_are_cues_or_just_words(monkeypatch):
    monkeypatch.setattr(delivery.config, "ELEVENLABS_MODEL", "eleven_v4_turbo")
    assert delivery.clean("*sighs* Fine.") == "Fine."
    assert delivery.voiced("*sighs* Fine.") == "[sighs] Fine."
    assert delivery.clean("(laughs) Not likely.") == "Not likely."
    assert delivery.clean("I *really* mean it.") == "I really mean it."
    assert delivery.clean("He's in Blüdhaven (again).") == "He's in Blüdhaven (again)."


def test_words_in_brackets_stay_words(monkeypatch):
    monkeypatch.setattr(delivery.config, "ELEVENLABS_MODEL", "eleven_v4_turbo")
    line = "[Rust, if you want rigour,] but it's worth it."
    assert delivery.clean(line) == line
    # Said as words: v4 would otherwise take the brackets for direction and drop them.
    assert delivery.voiced(line) == "Rust, if you want rigour, but it's worth it."


def test_the_moment_s_own_cues_are_performed(monkeypatch):
    monkeypatch.setattr(delivery.config, "ELEVENLABS_MODEL", "eleven_v4_turbo")
    assert delivery.voiced("[yawns] What time is it?") == "[yawns] What time is it?"
    assert delivery.voiced("[he sighs heavily] Fine.") == "[sighs] Fine."
    assert delivery.cued("[yawns] What?") and not delivery.cued("Mr. Fox, what?")
