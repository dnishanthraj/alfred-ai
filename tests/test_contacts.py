"""Contact profiles, availability, and search routing."""
import json
import time

import pytest

from wayne.contacts import Availability, Directory, directory
from wayne.engine.search import needs_search


class TestDirectory:
    def test_ships_with_alfred(self):
        alfred = directory().get("alfred")
        assert alfred is not None
        assert alfred.full_name == "Alfred Pennyworth"

    def test_primer_becomes_real_message_turns(self):
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
