"""
The deterministic post-processing, which is where the character actually gets
enforced. Every case here is a regression: each one is a way the guards were
observed to misbehave against the persona they exist to protect.
"""
import pytest

from wayne.engine import guards


class TestSignoffs:
    @pytest.mark.parametrize("text", [
        "Fine. Sleep well.",
        "Understood. Good night.",
        "As you wish. Get some rest.",
        "Right. Take care.",
    ])
    def test_strips_trailing_farewell(self, text):
        assert not any(
            word in guards.strip_signoffs(text).lower()
            for word in ("sleep", "good night", "rest", "take care")
        )

    @pytest.mark.parametrize("text", [
        "Take care of the deployment first.",
        "You should rest up your argument before presenting it.",
        "Turn in your notice tomorrow.",
    ])
    def test_keeps_legitimate_use_of_farewell_words(self, text):
        # These matched the old unanchored patterns and were silently deleted.
        assert guards.strip_signoffs(text).startswith(text.split()[0])

    def test_never_returns_empty(self):
        assert guards.strip_signoffs("Good night.") == "Mm."


class TestLeavingCues:
    @pytest.mark.parametrize("prompt", [
        "right, I'm off to bed",
        "night",
        "goodnight",
        "see you tomorrow",
        "going to sleep now",
    ])
    def test_detects_a_departure(self, prompt):
        assert guards.user_is_leaving(prompt)

    @pytest.mark.parametrize("prompt", [
        "the night shift was brutal today",
        "I finished at midnight and I'm still up",
        "tonight went badly",
        "the goodbyes were awkward",
    ])
    def test_ignores_the_word_used_in_passing(self, prompt):
        # A bare substring test read all of these as farewells, which unlocked
        # the very sign-off the guards exist to suppress.
        assert not guards.user_is_leaving(prompt)


class TestRegreeting:
    def test_strips_duplicate_greeting(self):
        assert guards.strip_regreeting("Good evening. What's on your mind?") \
            == "What's on your mind?"

    def test_keeps_sole_greeting_rather_than_emptying(self):
        assert guards.strip_regreeting("Good evening.") == "Good evening."

    def test_leaves_non_greeting_untouched(self):
        text = "Evening traffic was dreadful, apparently."
        assert guards.strip_regreeting(text) == text


class TestLengthCap:
    def test_passes_short_replies_through(self):
        text = "Mm. Go on."
        assert guards.cap_length(text, 4) == text

    def test_trims_runaway_replies(self):
        text = " ".join(f"Sentence {i}." for i in range(10))
        assert len(guards.split_sentences(guards.cap_length(text, 4))) == 4


class TestRepetition:
    def test_terse_replies_are_exempt(self):
        # The persona is built on these; flagging them as loops fought the
        # Modelfile and forced a high-temperature regeneration.
        for terse in ("Mm.", "Go on.", "Hm yourself."):
            assert not guards.too_similar(terse, [terse])

    def test_catches_a_verbatim_repeat(self):
        line = "That is a poor plan and you know it perfectly well"
        assert guards.too_similar(line, [line])

    def test_catches_a_shared_opening(self):
        # The first four words are the loop signature the guard looks for.
        assert guards.too_similar(
            "You should really consider the alternative here",
            ["You should really consider something else entirely"],
        )

    def test_distinct_replies_pass(self):
        assert not guards.too_similar(
            "Pick three and send them tonight",
            ["I've scraped better men than you off the road"],
        )


class TestStack:
    def test_order_head_then_tail(self):
        result = guards.apply(
            "Good evening. You've done enough. Sleep well.",
            prompt="how are you", max_sentences=4, already_greeted=True,
        )
        assert result == "You've done enough."

    def test_farewell_allowed_when_user_is_leaving(self):
        result = guards.apply(
            "Goodnight to you too.",
            prompt="night, I'm off to bed", max_sentences=4, already_greeted=False,
        )
        assert "night" in result.lower()
