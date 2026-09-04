"""Contact profiles, availability, and search routing."""
import json
import time

import pytest

from wayne import config
from wayne.contacts import Availability, Directory, directory
from wayne.engine import prompting
from wayne.engine.search import needs_search


class TestDirectory:
    def test_ships_with_alfred(self):
        alfred = directory().get("alfred")
        assert alfred is not None
        assert alfred.full_name == "Alfred Pennyworth"

    def test_primer_parses_into_alternating_turns(self):
        # Still built as message dicts — that is what the script is rendered
        # from — but they are no longer *sent* as turns. See TestPrimerIsolation.
        messages = directory().get("alfred").primer_messages()
        assert messages
        assert len(messages) % 2 == 0
        assert [m["role"] for m in messages[:2]] == ["user", "assistant"]

    def test_unknown_contact_is_none(self):
        assert directory().get("bane") is None

    def test_a_new_profile_needs_no_python(self, tmp_path):
        # The whole point of the phone book: adding Lucius is a JSON file.
        (tmp_path / "lucius.json").write_text(json.dumps({
            "id": "lucius", "name": "Lucius", "full_name": "Lucius Fox",
            "model": "qwen2.5:14b", "role": "Applied Sciences",
            "system": "You are Lucius Fox.",
        }))
        book = Directory(profile_dir=tmp_path)
        assert book.ids() == ["lucius"]
        assert book.get("lucius").system == "You are Lucius Fox."

    def test_every_contact_gets_a_context_window(self, tmp_path):
        # Ollama defaults to 4096 tokens. A persona, a primer and a few turns of
        # history clear that, and once the prompt outgrows the window the KV
        # cache is discarded and the entire prompt is re-read every turn — 5.3
        # seconds to the first word instead of 1.8. So it is not left to the
        # profile to remember: the default is applied to every contact.
        (tmp_path / "lucius.json").write_text(json.dumps({
            "id": "lucius", "name": "Lucius", "model": "qwen2.5:14b",
        }))
        assert Directory(profile_dir=tmp_path).get("lucius") \
            .options["num_ctx"] == config.CONTEXT_WINDOW

    def test_a_profile_may_override_the_context_window(self, tmp_path):
        (tmp_path / "lucius.json").write_text(json.dumps({
            "id": "lucius", "name": "Lucius", "model": "qwen2.5:14b",
            "options": {"num_ctx": 16384, "temperature": 0.4},
        }))
        options = Directory(profile_dir=tmp_path).get("lucius").options
        assert options["num_ctx"] == 16384
        assert options["temperature"] == 0.4

    def test_a_malformed_profile_names_itself(self, tmp_path):
        (tmp_path / "broken.json").write_text('{"name": "no id here"}')
        with pytest.raises(ValueError, match="broken.json"):
            Directory(profile_dir=tmp_path)


class TestAvailability:
    def test_always_is_always(self):
        assert Availability(kind="always").is_available()

    def test_office_hours_are_respected(self):
        hours = Availability(kind="hours", days=(0,), start_hour=9, end_hour=17)
        monday_noon = time.struct_time((2026, 9, 7, 12, 0, 0, 0, 250, -1))
        monday_night = time.struct_time((2026, 9, 7, 23, 0, 0, 0, 250, -1))
        sunday_noon = time.struct_time((2026, 9, 6, 12, 0, 0, 6, 249, -1))
        assert hours.is_available(monday_noon)
        assert not hours.is_available(monday_night)
        assert not hours.is_available(sunday_noon)


class TestSearchRouting:
    @pytest.mark.parametrize("prompt", [
        "look up the weather in Oslo",
        "search for the F1 results",
        "what's the price of a Switch 2",
    ])
    def test_explicit_lookups_route_to_search(self, prompt):
        assert needs_search(prompt)

    @pytest.mark.parametrize("prompt", [
        "what do you think of my plan",
        "should I take the job",
        "how do you feel about that",
    ])
    def test_opinions_never_route_to_search(self, prompt):
        assert not needs_search(prompt)

    def test_plain_conversation_does_not_search(self):
        assert not needs_search("I had a rough day")

    def test_opinion_marker_beats_a_search_verb(self):
        # "look up" is present, but he's asking for a view, not a lookup.
        assert not needs_search("what do you think, should I look up her number?")


class TestPrimerIsolation:
    """
    The primer must never reach the model as conversation.

    It is written as dialogue because a model imitates a dialogue it can see far
    better than a description of one — and that is exactly why it cannot be sent
    as real turns: the model has no way to tell a sample from a memory. Sent as
    turns, an example showing him refuse to let someone drive home drunk came
    back, asked directly, as "Once. Two years ago, at Christmas. You threw up in
    the passenger seat" — a detailed and entirely fabricated accusation about a
    real person. Asked what they had discussed before, he recited the primer.

    A system-role marker placed between the primer and the history was tried and
    did not hold. The samples belong in the system prompt, labelled, where they
    cannot be mistaken for the transcript.
    """

    def _payload(self):
        contact = directory().get("alfred")
        history = [{"role": "user", "content": "genuine turn"},
                   {"role": "assistant", "content": "genuine reply"}]
        return contact, prompting.build_payload(contact, history, "what now")

    def test_primer_is_not_sent_as_conversation_turns(self):
        contact, payload = self._payload()
        conversation = [m for m in payload if m["role"] != "system"]
        primer_texts = {m["content"] for m in contact.primer_messages()}
        for message in conversation:
            assert message["content"] not in primer_texts

    def test_the_conversation_is_only_history_and_the_current_turn(self):
        _, payload = self._payload()
        conversation = [m for m in payload if m["role"] != "system"]
        assert [m["content"] for m in conversation] == [
            "genuine turn", "genuine reply", "what now"]

    def test_primer_still_reaches_the_model_in_the_system_prompt(self):
        contact, payload = self._payload()
        system = "\n".join(m["content"] for m in payload if m["role"] == "system")
        for message in contact.primer_messages():
            assert message["content"] in system

    def test_the_samples_are_labelled_as_not_having_happened(self):
        _, payload = self._payload()
        system = "\n".join(m["content"] for m in payload if m["role"] == "system")
        assert "None of this happened" in system
        assert "none of the above occurred" in system

    def test_a_contact_with_no_primer_gets_no_script(self, tmp_path):
        (tmp_path / "lucius.json").write_text(json.dumps({
            "id": "lucius", "name": "Lucius", "model": "qwen2.5:14b",
            "system": "You are Lucius Fox.",
        }))
        contact = Directory(profile_dir=tmp_path).get("lucius")
        payload = prompting.build_payload(contact, [], "hello")
        system = "\n".join(m["content"] for m in payload if m["role"] == "system")
        assert "HOW YOU SPEAK" not in system
