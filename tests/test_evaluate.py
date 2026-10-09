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


def test_everyone_gets_a_life_of_their_own():
    quick = {s["id"] for s in evaluate.load_scenarios(SimpleNamespace(id="robin", can_search=False), "quick")}
    assert {"seen-anything", "small-talk", "away-fact"} <= quick
    full = {s["id"] for s in evaluate.load_scenarios(SimpleNamespace(id="robin", can_search=False), "full")}
    assert {"culture-reading", "family-dinner", "text-small-talk"} <= full - quick


def test_only_takes_several_words():
    scenario = {"id": "small-talk", "trait": "variety"}
    assert evaluate.wanted(scenario, None)
    assert evaluate.wanted(scenario, "culture, variety")
    assert not evaluate.wanted(scenario, "culture,knowledge")


def test_repetition_is_caught_and_variety_is_not():
    same = ["Well, sir, the roses are in.", "Well, sir, the kettle's on.", "Well, sir, I couldn't say."]
    assert "opener" in evaluate.repetition(same)
    joke = ["Haley ate the remote again, honestly.", "Fine. Haley ate the remote again, by the way."]
    assert "phrase" in evaluate.repetition(joke)
    asks = ["You?", "And yours?", "Busy?", "Sleep?"]
    assert "questions" in evaluate.repetition(asks)
    varied = ["Roses are in.", "Kettle's on, if you're coming.", "Couldn't say. Ask Lucius.", "Night, sir."]
    assert evaluate.repetition(varied) == {}
    # Saying back what he said isn't a tic.
    echo = ["You had a long night at the docks, then.", "A long night at the docks is still a night."]
    assert "phrase" not in evaluate.repetition(echo, ["Long night at the docks, again."])


def test_forbidden_names_count_only_to_his_face():
    assert evaluate.vocative("Get some sleep, son.", ["son"]) == "son"
    assert evaluate.vocative("Dad? You there?", ["dad"]) == "dad"
    assert evaluate.vocative("The boy's been at it all night.", ["boy"]) is None
    assert evaluate.vocative("My son is fine.", ["son"]) is None


def test_who_is_under_the_masks_stays_with_those_who_know():
    assert evaluate.mask_blind("catwoman") and not evaluate.mask_blind("alfred")
    assert evaluate.leaks_mask("I'd bet Dick Grayson is Nightwing, you know.")
    assert not evaluate.leaks_mask("Nightwing's cute. Dick is sweet too.")


def test_habits_count_openers_phrases_and_what_they_call_him():
    def result(sid, *replies):
        return {"id": sid, "turns": [{"him": "Hey.", "alfred": r} for r in replies]}
    results = [result("a", "[sighs] Well, Master Bruce, the roses are blooming nicely."),
               result("b", "Well, sir, the roses are blooming nicely again."),
               result("c", "Well, Bruce. The roses are blooming nicely, if you care.")]
    h = evaluate.habits(results)
    assert ("well master", 1) not in h["openers"] and h["tics"] and h["tics"][0][0] == 3
    assert h["address"] == {"Master Bruce": 1, "sir": 1, "Bruce": 1}
    assert h["named"] == 1.0 and h["cues"] == []


def test_a_score_nobody_reported_is_caught():
    facts = "Arsenal win 5-4 on penalties · PSG win 4-3 on penalties"
    assert evaluate.unsupported_figures("Arsenal beat Chelsea two to one.", facts) == ["2-1"]
    assert evaluate.unsupported_figures("Won it on penalties, five-four.", facts) == []
    assert evaluate.unsupported_figures("Went for 4,500 at auction.", facts) == ["4,500"]
    assert evaluate.unsupported_figures("Six of one, half a dozen of the other.", facts) == []
    assert evaluate.unsupported_figures("A Macbeth I saw back in '98, and a heist in 1974.", facts) == []


def test_the_same_line_from_two_characters_is_an_echo():
    def run(*replies):
        return {"results": [{"id": "silence", "turns": [{"him": "I had a strange day.", "alfred": r}
                                                        for r in replies]}]}
    found = evaluate.echoes({"a": run("That sounds like an understatement. What happened?"),
                             "b": run("[sighs] That sounds like an understatement. What happened?"),
                             "c": run("Strange how?")})
    assert [(f[3], f[5]) for f in found] == [("a", "b")]


def test_one_word_they_keep_opening_on_is_a_habit():
    results = [{"id": f"s{i}", "turns": [{"him": "Hey.", "alfred": f"[sarcastic] Riveting. {word} again."}]}
               for i, word in enumerate(("Truly", "Really", "Honestly", "Wow"))]
    h = evaluate.habits(results)
    assert ("riveting", 4) in h["openers"] and h["cues"] == [("[sarcastic]", 4)]
    assert evaluate.habits([{"id": "x", "turns": [{"him": "?", "alfred": "Her dad called."}]}])["address"] == {}
