"""Vault storage and retrieval, encryption at rest, and elapsed-time memory."""
import json
import re

import pytest

from wayne.engine.session import ContactSession
from wayne.memory import History, Vault
from wayne.memory.history import describe_gap
from wayne.memory.store import atomic_write, generate_key


@pytest.fixture
def vault(tmp_path, monkeypatch):
    v = Vault("test")
    monkeypatch.setattr(v, "path", tmp_path / "vault.txt")
    return v


class TestVaultWrites:
    def test_strips_the_trigger_phrase(self, vault):
        assert vault.memorize("remember that I live in London") == "I live in London"
        assert [Vault._fact_only(e) for e in vault.entries()] == ["I live in London"]

    def test_ignores_duplicates(self, vault):
        vault.memorize("remember that I live in London")
        vault.memorize("remember that I live in London")
        vault.memorize("Remember that I LIVE in London")
        assert len(vault.entries()) == 1

    def test_forget_removes_matching_facts(self, vault):
        vault.memorize("remember that I work at Acme")
        vault.memorize("remember that my sister is called Priya")
        removed = vault.forget("Acme")
        assert removed == ["I work at Acme"]
        assert [Vault._fact_only(e) for e in vault.entries()] == ["My sister is called Priya"]

    def test_forget_reports_nothing_when_no_match(self, vault):
        vault.memorize("remember that I work at Acme")
        assert vault.forget("Wayne Enterprises") == []
        assert len(vault.entries()) == 1


class TestVaultRetrieval:
    def test_small_vaults_are_sent_whole(self, vault):
        for i in range(10):
            vault.memorize(f"remember that fact number {i} is true")
        # Selection can only lose when the whole thing is nearly free to send.
        assert len(vault.relevant("anything at all")) == 10

    def test_large_vaults_are_filtered_by_relevance(self, vault):
        for i in range(60):
            vault.memorize(f"remember that unrelated trivia {i}")
        vault.memorize("remember that my dissertation is on distributed consensus")

        picked = vault.relevant("how is the dissertation going")
        assert len(picked) < 61
        assert any("dissertation" in entry for entry in picked)

    def test_retrieval_preserves_document_order(self, vault):
        for i in range(60):
            vault.memorize(f"remember that item {i} exists")
        picked = vault.relevant("item 3 exists")
        indexes = [vault.entries().index(p) for p in picked]
        assert indexes == sorted(indexes)


class TestHistory:
    def test_round_trips_an_exchange(self, tmp_path, monkeypatch):
        h = History("test")
        monkeypatch.setattr(h, "path", tmp_path / "history.json")
        h.messages = []
        h.record_exchange("what's my name", "Wayne.")
        assert json.loads((tmp_path / "history.json").read_text())[-1]["content"] == "Wayne."

    def _conversation(self, pairs, words_each=10):
        h = History.__new__(History)
        h.contact_id = "test"
        h.messages = []
        for i in range(pairs):
            h.messages.append({"role": "user", "content": f"u{i} " + "word " * words_each})
            h.messages.append({"role": "assistant", "content": f"a{i} " + "word " * words_each})
        return h

    def test_history_sent_to_the_model_is_capped(self):
        # Prompt evaluation is the whole latency budget and it is paid again
        # every turn, so an uncapped history means a conversation that gets
        # steadily slower the longer it runs.
        h = self._conversation(pairs=30)
        sent = h.for_model(word_budget=60)
        assert len(sent) < len(h.messages)
        assert sum(len(m["content"].split()) for m in sent) <= 60

    def test_the_cap_keeps_the_most_recent_turns(self):
        h = self._conversation(pairs=30)
        assert h.for_model(word_budget=60)[-1]["content"].startswith("a29")

    def test_the_cap_never_opens_on_an_answer(self):
        # An assistant turn whose question was trimmed away reads as something
        # he volunteered, and he follows that example.
        h = self._conversation(pairs=30)
        for budget in (12, 40, 100, 250):
            sent = h.for_model(word_budget=budget)
            assert not sent or sent[0]["role"] == "user", budget

    def test_a_budget_of_zero_disables_trimming(self):
        h = self._conversation(pairs=30)
        assert len(h.for_model(word_budget=0)) == 60

    def test_one_enormous_turn_is_still_returned(self):
        # Better an over-budget prompt than a turn with no context at all.
        h = self._conversation(pairs=1, words_each=500)
        assert len(h.for_model(word_budget=60)) >= 1

    def test_corrupt_history_is_treated_as_empty_not_fatal(self, tmp_path, monkeypatch):
        path = tmp_path / "history.json"
        path.write_text("{ this is not json")
        h = History.__new__(History)
        h.contact_id = "test"
        h.path = path
        assert h._load() == []


class TestAtomicWrite:
    def test_leaves_no_temp_files_behind(self, tmp_path):
        target = tmp_path / "data.json"
        atomic_write(target, '{"ok": true}')
        assert target.read_text() == '{"ok": true}'
        assert list(tmp_path.glob(".tmp-*")) == []

    def test_original_survives_a_failed_write(self, tmp_path, monkeypatch):
        target = tmp_path / "data.json"
        atomic_write(target, "original")

        def boom(*_args, **_kwargs):
            raise OSError("disk full")

        monkeypatch.setattr("os.replace", boom)
        with pytest.raises(OSError):
            atomic_write(target, "replacement")

        assert target.read_text() == "original"
        assert list(tmp_path.glob(".tmp-*")) == []


class TestSentenceBoundaries:
    @pytest.mark.parametrize("text,expected", [
        ("Mm. Go on.", 3),                       # index just past "Mm."
        ("No boundary yet", None),
        ("Ends here.", None),                    # needs trailing whitespace
        ("It cost 3.5 million. Really.", 20),    # decimal is not a boundary
    ])
    def test_finds_the_first_boundary(self, text, expected):
        assert ContactSession._sentence_end(text) == expected


class TestEncryptionAtRest:
    """
    Encryption must be transparent to every caller and must never be the reason
    someone loses their memory — including across turning it on.
    """

    def _with_key(self, monkeypatch, key):
        import wayne.memory.store as store
        monkeypatch.setattr("wayne.config.MEMORY_KEY", key)
        monkeypatch.setattr(store, "_fernet", None)
        monkeypatch.setattr(store, "_fernet_failed", False)
        return store

    def test_round_trips(self, tmp_path, monkeypatch):
        store = self._with_key(monkeypatch, generate_key())
        path = tmp_path / "vault.txt"
        store.atomic_write(path, "- A fact worth keeping")
        assert store.read_text(path) == "- A fact worth keeping"

    def test_ciphertext_is_not_readable_on_disk(self, tmp_path, monkeypatch):
        store = self._with_key(monkeypatch, generate_key())
        path = tmp_path / "vault.txt"
        store.atomic_write(path, "- I live at Wayne Manor")
        assert b"Wayne Manor" not in path.read_bytes()

    def test_plaintext_written_before_a_key_still_reads(self, tmp_path, monkeypatch):
        # Turning encryption on must not look like amnesia.
        path = tmp_path / "vault.txt"
        path.write_bytes(b"- Written before encryption")
        store = self._with_key(monkeypatch, generate_key())
        assert store.read_text(path) == "- Written before encryption"

    def test_unreadable_ciphertext_yields_empty_not_a_crash(self, tmp_path, monkeypatch):
        store = self._with_key(monkeypatch, generate_key())
        path = tmp_path / "vault.txt"
        store.atomic_write(path, "- Secret")
        self._with_key(monkeypatch, generate_key())  # different key
        assert store.read_text(path) == ""

    def test_a_malformed_key_falls_back_rather_than_failing(self, tmp_path, monkeypatch):
        store = self._with_key(monkeypatch, "not-a-valid-fernet-key")
        path = tmp_path / "vault.txt"
        store.atomic_write(path, "- Still stored")
        assert store.read_text(path) == "- Still stored"


class TestVaultDating:
    def test_facts_are_dated(self, vault):
        vault.memorize("remember that I moved to Gotham")
        assert re.match(r"^\[\d{4}-\d{2}-\d{2}\] I moved to Gotham$", vault.entries()[0])

    def test_dedupe_compares_the_fact_not_the_date(self, vault):
        vault.memorize("remember that I moved to Gotham")
        vault.memorize("remember that I moved to Gotham")
        assert len(vault.entries()) == 1

    def test_forget_matches_on_the_fact(self, vault):
        vault.memorize("remember that I moved to Gotham")
        assert vault.forget("Gotham") == ["I moved to Gotham"]
        assert vault.entries() == []


class TestElapsedTime:
    @pytest.mark.parametrize("seconds,expected", [
        (30, "moments ago"), (600, "10 minutes ago"), (3600 * 5, "5 hours ago"),
        (86400 * 1.2, "yesterday"), (86400 * 5, "5 days ago"), (86400 * 30, "4 weeks ago"),
    ])
    def test_describes_a_gap_the_way_a_person_would(self, seconds, expected):
        assert describe_gap(seconds) == expected

    def test_no_history_has_no_gap(self):
        assert describe_gap(None) == ""

    def test_timestamps_are_hidden_from_the_model(self, tmp_path, monkeypatch):
        h = History("test")
        monkeypatch.setattr(h, "path", tmp_path / "history.json")
        h.messages = []
        h.record_exchange("hello", "Mm.")
        assert all(set(m) == {"role", "content"} for m in h.for_model())
        assert h.seconds_since_last() < 5


class TestGreetingPileUp:
    """
    Each call stores a placeholder turn and the greeting answering it. Left in
    place, a handful of calls fill the context with greetings and the model
    writes another — which is how a character starts sounding like it has no
    memory of ever speaking to you.
    """

    MARKER = "[link established]"

    def _history(self, tmp_path, monkeypatch):
        h = History("test")
        monkeypatch.setattr(h, "path", tmp_path / "history.json")
        h.messages = []
        return h

    def test_drops_earlier_greetings(self, tmp_path, monkeypatch):
        h = self._history(tmp_path, monkeypatch)
        for greeting in ("Morning.", "Morning again.", "Good morning."):
            h.append("user", self.MARKER)
            h.append("assistant", greeting)
            h.append("user", "how are you")
            h.append("assistant", "Fine.")

        h.drop_prior_greetings(self.MARKER)
        contents = [m["content"] for m in h.messages]
        assert self.MARKER not in contents
        assert not any("orning" in c for c in contents)
        # The actual conversation is untouched.
        assert contents == ["how are you", "Fine."] * 3

    def test_leaves_a_history_with_no_greetings_alone(self, tmp_path, monkeypatch):
        h = self._history(tmp_path, monkeypatch)
        h.append("user", "hello")
        h.append("assistant", "Mm.")
        before = list(h.messages)
        h.drop_prior_greetings(self.MARKER)
        assert h.messages == before

    def test_does_not_strip_a_marker_the_operator_actually_typed(self, tmp_path, monkeypatch):
        # Only a marker immediately followed by an assistant turn is a greeting
        # pair; the same words at the end of a log are just a message.
        h = self._history(tmp_path, monkeypatch)
        h.append("user", self.MARKER)
        h.drop_prior_greetings(self.MARKER)
        assert len(h.messages) == 1
