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

    def test_a_reply_that_is_only_a_greeting_empties(self):
        # The caller substitutes something neutral. Returning the greeting
        # unchanged — the old behaviour — meant he simply greeted twice.
        assert guards.strip_regreeting("Good evening.") == ""

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
    def test_backchannels_are_exempt(self):
        # The persona is built on these; flagging them as loops fought the
        # Modelfile and forced a high-temperature regeneration. Exemption is by
        # being a backchannel, not by being short — "Morning." is short too.
        for terse in ("Mm.", "Go on.", "Quite.", "Right."):
            assert not guards.too_similar(terse, [terse])

    def test_a_short_reply_that_is_not_a_backchannel_is_not_exempt(self):
        assert guards.too_similar("Hm yourself.", ["Hm yourself."])

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


class TestEchoDetection:
    """
    The last line of defence against acoustic feedback: a reply leaves the
    speakers, the microphone hears it, and it arrives back as though the
    operator had said it. Left unchecked the contact answers itself forever.
    """

    SPOKEN = [
        "Procrastination won't pay the bills. Get on with it.",
        "I've scraped better men than you off the road.",
    ]

    @pytest.mark.parametrize("heard", [
        "Procrastination won't pay the bills. Get on with it.",       # clean echo
        "procrastination wont pay the bills get on with it",          # lossy STT
        "Procrastination won't pay the bills",                        # fragment
    ])
    def test_catches_its_own_voice(self, heard):
        assert guards.echoes(heard, self.SPOKEN)

    @pytest.mark.parametrize("heard", [
        "I'll get on with it tomorrow, I promise",
        "What did you mean about the road?",
        "Tell me about the bills again",
    ])
    def test_lets_genuine_replies_through(self, heard):
        assert not guards.echoes(heard, self.SPOKEN)

    def test_short_utterances_are_never_treated_as_echo(self):
        # Dropping "go on" because it resembled a reply is worse than the echo.
        assert not guards.echoes("go on", ["Go on, then."])
        assert not guards.echoes("yes", self.SPOKEN)

    def test_nothing_spoken_means_nothing_to_echo(self):
        assert not guards.echoes("anything at all here", [])


class TestForbiddenAddress:
    """
    Forms of address the character would never use, enforced in code because a
    smaller model ignores the instruction often enough to matter.
    """

    TERMS = ["lad", "laddie", "my boy", "son", "young man"]

    @pytest.mark.parametrize("text,expected", [
        ("It's past midnight, lad.", "It's past midnight."),
        ("Now listen, my boy, this is important.", "Now listen, this is important."),
        ("Lad, you are being absurd.", "You are being absurd."),
        ("Go on then lad.", "Go on then."),
        ("Enough, young man!", "Enough!"),
    ])
    def test_removes_the_vocative(self, text, expected):
        assert guards.strip_forbidden_address(text, self.TERMS) == expected

    @pytest.mark.parametrize("text", [
        "Your son called earlier.",
        "The lads at the pub said otherwise.",
        "Get on with it.",
    ])
    def test_leaves_ordinary_usage_alone(self, text):
        # Only clearly vocative uses go; the same word elsewhere survives.
        assert guards.strip_forbidden_address(text, self.TERMS) == text

    def test_no_terms_configured_is_a_no_op(self):
        assert guards.strip_forbidden_address("Right then, lad.", ()) == "Right then, lad."

    def test_runs_as_part_of_the_stack(self):
        result = guards.apply(
            "Steady on, lad. Sleep well.",
            prompt="how are you", max_sentences=4, already_greeted=False,
            forbidden_address=self.TERMS,
        )
        assert result == "Steady on."


class TestRepeatCounting:
    """
    Noticing that something has been said before — the basis for "you've told
    me this three times now", which is what separates listening from parsing.
    """

    SAID = [
        "I really should call her back",
        "work has been fine I suppose",
        "I really ought to call her back",
    ]

    def test_counts_paraphrases_not_just_copies(self):
        assert guards.count_repeats("I really should call her back", self.SAID) >= 2

    def test_returns_zero_for_something_new(self):
        assert guards.count_repeats("The car failed its MOT this morning", self.SAID) == 0

    def test_ignores_short_filler(self):
        # "yes" and "go on" recur constantly and mean nothing by recurring.
        assert guards.count_repeats("yes", ["yes", "yes", "yes"]) == 0
        assert guards.count_repeats("go on", ["go on", "go on"]) == 0

    def test_nothing_said_before_counts_as_nothing(self):
        assert guards.count_repeats("Anything at all here", []) == 0


class TestGreetingLoop:
    """
    Six consecutive "Morning."s reached a real conversation. Three guards each
    declined to catch it, for three different reasons.
    """

    def test_a_reply_that_is_only_a_greeting_is_stripped_to_nothing(self):
        # This returned the greeting unchanged, on the reasoning that a guard
        # should never empty a reply — which defeated the guard precisely when
        # it was needed.
        assert guards.strip_regreeting("Morning.") == ""
        assert guards.strip_regreeting("Good morning, sir.") == ""

    def test_a_greeting_with_content_keeps_the_content(self):
        assert guards.strip_regreeting("Good morning. Tea?") == "Tea?"

    def test_the_stack_substitutes_rather_than_greeting_again(self):
        result = guards.apply("Morning.", prompt="yeah", max_sentences=4,
                              already_greeted=True)
        assert result and "morning" not in result.lower()

    def test_repeated_greetings_count_as_repetition(self):
        # Exempting everything under four words also exempted "Morning.",
        # so a greeting could repeat indefinitely unnoticed.
        assert guards.too_similar("Morning.", ["Morning."])

    @pytest.mark.parametrize("phrase", ["Mm.", "Go on.", "Quite.", "Indeed."])
    def test_backchannels_stay_exempt(self, phrase):
        # These are meant to recur; that is the character.
        assert not guards.too_similar(phrase, [phrase])

    def test_a_repeated_greeting_from_the_operator_is_noticed(self):
        assert guards.count_repeats("Morning", ["Morning"] * 4) == 4

    def test_filler_from_the_operator_is_not(self):
        assert guards.count_repeats("yes", ["yes"] * 4) == 0


class TestOpeningComparison:
    """
    The loop check runs on the first sentence of a reply, so it has to be
    measured against the first sentence of earlier ones. Comparing a single
    sentence against whole multi-sentence replies scores too low to ever fire.
    """

    LONG = ("You've said morning nine times now. "
            "Is there something you're trying to tell me?")
    NEXT = "You've said morning ten times now."

    def test_a_sentence_against_a_whole_reply_misses(self):
        assert not guards.too_similar(self.NEXT, [self.LONG])

    def test_a_sentence_against_the_matching_opening_catches_it(self):
        opening = guards.split_sentences(self.LONG)[0]
        assert guards.too_similar(self.NEXT, [opening])


class TestParroting:
    """
    Handing the operator's own words back. "You tell me." answered with "You
    tell me." reads as a machine instantly, whatever the persona says.
    """

    @pytest.mark.parametrize("reply,prompt", [
        ("You tell me.", "You tell me."),
        ("you tell me", "You tell me?"),
        ("Nothing much.", "Nothing much"),
    ])
    def test_catches_an_echo(self, reply, prompt):
        assert guards.parrots(reply, prompt)

    @pytest.mark.parametrize("reply,prompt", [
        ("Then say something worth hearing.", "You tell me."),
        ("And what would you have me say?", "You tell me."),
        ("Out with it.", "Hmm."),
    ])
    def test_leaves_real_replies_alone(self, reply, prompt):
        assert not guards.parrots(reply, prompt)


class TestUrgency:
    @pytest.mark.parametrize("prompt", [
        "It's urgent, Alfred. I need it now.",
        "I need this right now",
        "hurry",
        "Find it. Now.",
    ])
    def test_detects_urgency(self, prompt):
        assert guards.is_urgent(prompt)

    @pytest.mark.parametrize("prompt", [
        "How was your day?",
        "I'll get to it eventually",
        "Nothing much happening",
    ])
    def test_ordinary_conversation_is_not_urgent(self, prompt):
        assert not guards.is_urgent(prompt)


class TestBackchannelEchoes:
    """
    The backchannel exemption protects him from being scolded for his own
    recurring "Mm." It should not excuse him for handing back the operator's.
    """

    @pytest.mark.parametrize("phrase", ["Hmm.", "So?", "Right.", "Mm."])
    def test_echoing_the_operator_is_still_parroting(self, phrase):
        assert guards.parrots(phrase, phrase)

    @pytest.mark.parametrize("phrase", ["Mm.", "Go on.", "Quite."])
    def test_but_his_own_recurrence_is_still_fine(self, phrase):
        assert not guards.too_similar(phrase, [phrase])
