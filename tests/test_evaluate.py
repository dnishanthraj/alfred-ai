"""The evaluation's own arithmetic — the parts that decide what counts as a result."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace

_spec = importlib.util.spec_from_file_location(
    "evaluate", Path(__file__).resolve().parent.parent / "scripts" / "evaluate.py")
evaluate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(evaluate)


def test_a_lopsided_split_is_a_result_and_an_even_one_is_not():
    assert evaluate.sign_test(9, 1) < 0.05
    assert evaluate.sign_test(5, 4) > 0.5
    assert evaluate.sign_test(0, 0) == 1.0


def test_shared_phrases_between_characters_are_found():
    calls = {"a": ["Only three? You're getting soft in your old age."] + [""] * 11,
             "b": ["Only three? You're getting soft, handsome."] + [""] * 11,
             "c": ["Patrol."] + [""] * 11}
    matrix, shared = evaluate.likeness(calls)
    assert matrix["a|b"] > matrix["a|c"]
    assert shared and {shared[0][2], shared[0][4]} == {"a", "b"}


def test_expected_length_follows_the_spread():
    terse = SimpleNamespace(speech_length={"word": 0.6, "line": 0.4})
    measured = SimpleNamespace(speech_length={"word": 0.1, "line": 0.5, "few": 0.4})
    assert evaluate.expected_words(terse) < evaluate.expected_words(measured)


def test_scenarios_are_fitted_to_who_can_search():
    searcher = SimpleNamespace(id="alfred", can_search=True)
    field = SimpleNamespace(id="redhood", can_search=False)

    def ids(contact):
        return {s["id"] for s in evaluate.load_scenarios(contact, "full")}
    assert "lookup-person" in ids(searcher) and "lookup-person" not in ids(field)
    assert "no-screen" in ids(field) and "no-screen" not in ids(searcher)
