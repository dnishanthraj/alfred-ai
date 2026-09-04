"""
The lookup decision.

Whether a turn goes to the web is decided here rather than by the model. That
is a deliberate inversion: asked to look something up, this character would
rather tell you he is an old man with a cup of tea, and then guess. Guessing is
the one failure mode worth engineering against, so the lookup happens on the way
in and he is handed what was found.

Which makes the classifier the whole safety margin, in both directions. Miss a
lookup and he invents a fact; fire one spuriously and he googles the person he
is talking to.
"""
import pytest

from wayne.engine.search import is_factual_lookup


class TestFactualLookup:

    @pytest.mark.parametrize("prompt", [
        "Who is Waylon Jones?",
        "What's the weather in London today?",
        "Who won the last race?",
        "Look it up for me.",
        "What is the price of a return to Edinburgh?",
        "Tell me about the Wayne Foundation.",
        "What happened to the seven forty from Paddington?",
    ])
    def test_facts_go_to_the_web(self, prompt):
        assert is_factual_lookup(prompt) is True

    @pytest.mark.parametrize("prompt", [
        "I'm feeling pretty low, honestly.",
        "Mm.",
        "Nothing much.",
        "Come off it.",
    ])
    def test_conversation_does_not(self, prompt):
        assert is_factual_lookup(prompt) is False

    @pytest.mark.parametrize("prompt", [
        "What do you think of Arsenal?",
        "What do you make of my job?",
        "Should I call her back?",
        "What would you do in my position?",
        "Your take on the merger?",
    ])
    def test_opinions_are_his_job_not_the_web_s(self, prompt):
        # These match the factual patterns almost perfectly — "what do you make
        # of X" is `what` + `do` — and are exactly what he is for.
        assert is_factual_lookup(prompt) is False

    @pytest.mark.parametrize("prompt", [
        "Who are you?",
        "Who am I?",
        "What are you, exactly?",
        "What is my schedule?",
        "How are you?",
        "Where are we?",
    ])
    def test_questions_about_the_two_people_on_the_line_never_search(self, prompt):
        # "Who are you?" is `who` + `are`, so it matched the factual patterns and
        # sent a web search for a butler while he stood there being one. The
        # answer to any of these is in the persona or the vault, never online.
        assert is_factual_lookup(prompt) is False

    def test_empty_input_is_not_a_lookup(self):
        assert is_factual_lookup("") is False
        assert is_factual_lookup(None) is False
