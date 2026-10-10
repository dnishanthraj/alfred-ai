"""Group calls and chats behaving like people: asides answered, pauses filled, talk-overs, chases."""
import asyncio
import time
from types import SimpleNamespace

import pytest

from wayne import events, paths
from wayne.engine import groupchat, party, presence
from wayne.engine.party import Call
from wayne.memory import groups as store


class Member:
    """A contact on a call: scripted replies, asides and drafts, recording what it was asked."""

    def __init__(self, cid, name, replies=(), asides=(), drafts=()):
        self.contact = SimpleNamespace(id=cid, name=name, full_name=name)
        self.history = SimpleNamespace(messages=[], record_aside=lambda text: None)
        self.replies, self.asides, self.drafts = list(replies), list(asides), list(drafts)
        self.call, self.heard, self.asked, self.instructions = None, [], [], []
        self._hanging_up, self._yielding = False, False

    def mark_call_start(self):
        pass

    def call_messages(self):
        return list(self.history.messages)

    def keep_heard(self):
        pass

    def ask(self, prompt, interrupted=False, confidence=1.0, follow_up=False, via=None):
        self.asked.append((prompt, follow_up))
        self.heard = []
        reply = self.replies.pop(0) if self.replies else "Mm."
        yield events.sentence(0, reply)
        yield events.reply_end(reply)

    def say(self, instruction, prompted_by=None, greeting=False, farewell=False):
        self.instructions.append(instruction)
        line = self.asides.pop(0) if self.asides else ""
        self._yielding = "[yield]" in line
        line = line.replace("[yield]", "").strip()
        if line:
            yield events.sentence(0, line)
        yield events.reply_end(line)

    def draft(self, instruction):
        return self.drafts.pop(0) if self.drafts else ""

    def utter(self, text, cut_off=False):
        yield events.sentence(0, text)
        yield events.reply_end(text)


def _call(*members):
    call = Call(operator="Him")
    for m in members:
        call.join(m)
    return call


def _said(out):
    return [(e["speaker"], e["text"]) for e in out if e["type"] == "reply_end" and e.get("text")]


@pytest.fixture(autouse=True)
def no_jumping_in(monkeypatch):
    monkeypatch.setattr(party, "CHIME", 0)


def test_someone_jumps_in_unasked_and_whoever_they_name_answers(monkeypatch):
    monkeypatch.setattr(party, "CHIME", 1.0)
    monkeypatch.setattr(party.random, "random", lambda: 0.0)
    dick = Member("nightwing", "Dick", replies=["Docks it is."])
    tim = Member("robin", "Tim", asides=["Dick, you said that last week."], replies=["And I was right."])
    call = _call(dick, tim)
    said = _said(list(call.turn("Dick, where tonight?")))
    assert said[0] == ("nightwing", "Docks it is.")
    assert ("robin", "Dick, you said that last week.") in said
    assert any("What's been said on the call" in i for i in tim.instructions)   # it heard him


def test_while_a_line_rings_someone_may_say_so():
    dick = Member("nightwing", "Dick", asides=["Ugh. Really? Him?"])
    call = _call(dick, Member("robin", "Tim"))
    assert _said(list(call.ringing(dick, "Jason"))) == [("nightwing", "Ugh. Really? Him?")]


def test_a_call_holds_four_besides_him():
    members = [Member(f"c{i}", f"N{i}") for i in range(5)]
    call = _call(*members)
    assert len(call.members) == 4 == party.MAX_CONTACTS


def test_everyone_means_all_four_get_a_turn():
    members = [Member(f"c{i}", f"N{i}") for i in range(4)]
    call = _call(*members)
    list(call.turn("Everyone — quick status."))
    assert all(m.asked for m in members)


def test_someone_named_in_a_reaction_answers_it():
    dick = Member("nightwing", "Dick", asides=["Barbara, where were you?"])
    babs = Member("batgirl", "Barbara", replies=["Library. Don't start."])
    call = _call(dick, babs)
    out = list(call.react(dick, "Barbara has just dialled back into the call."))
    assert _said(out) == [("nightwing", "Barbara, where were you?"), ("batgirl", "Library. Don't start.")]
    assert babs.asked[0][1] is True              # answering him, not the operator


def test_a_greeting_to_the_room_isnt_a_question_to_anyone():
    dick, tim = Member("nightwing", "Dick"), Member("robin", "Tim", asides=["Hey, guys."])
    call = _call(dick, tim)
    list(call.greet(tim))
    assert not dick.asked


def test_a_pause_is_filled_or_left_alone():
    dick = Member("nightwing", "Dick", asides=["Tim, you're quiet."])
    tim = Member("robin", "Tim", replies=["Thinking."])
    call = _call(dick, tim)
    assert _said(list(call.lull(dick))) == [("nightwing", "Tim, you're quiet."), ("robin", "Thinking.")]
    quiet = Member("redhood", "Jason")
    call2 = _call(quiet, Member("orphan", "Cass"))
    assert _said(list(call2.lull(quiet))) == []


def test_two_starting_at_once_sort_out_who_goes(monkeypatch):
    monkeypatch.setattr(party.random, "choice", lambda seq: 2)
    dick = Member("nightwing", "Dick", drafts=["So I was thinking about the docks."])
    tim = Member("robin", "Tim", drafts=["Actually the manifests changed."],
                 asides=["Sorry — go ahead. [yield]"])
    call = _call(dick, tim)
    said = _said(list(call.collide(dick, tim)))
    assert said[0] == ("nightwing", "So I—")
    assert said[1] == ("robin", "Actually the—")
    assert said[2] == ("robin", "Sorry — go ahead.")
    assert said[3] == ("nightwing", "So I was thinking about the docks.")


def test_whoever_doesnt_yield_just_goes_on(monkeypatch):
    monkeypatch.setattr(party.random, "choice", lambda seq: 2)
    dick = Member("nightwing", "Dick", drafts=["So I was thinking."])
    tim = Member("robin", "Tim", drafts=["The manifests changed."],
                 asides=["No, me first — the manifests changed."])
    call = _call(dick, tim)
    said = _said(list(call.collide(dick, tim)))
    assert said[-1] == ("robin", "No, me first — the manifests changed.")
    assert len(said) == 3


def test_those_in_the_know_are_warned_about_an_outsider_on_the_line():
    dick, selina = Member("nightwing", "Dick"), Member("catwoman", "Selina")
    call = _call(dick, selina)
    assert "Selina" in call.note_for(dick) and "masks" in call.note_for(dick)
    assert "masks" not in call.note_for(selina)


# --- group chats -----------------------------------------------------------------


@pytest.fixture
def private_data(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "DATA_DIR", tmp_path)
    monkeypatch.setattr(paths, "contact_dir", lambda cid: tmp_path / cid)
    presence._registry.clear()
    yield
    presence._registry.clear()


def test_a_long_gap_and_reactions_show_in_what_they_read(private_data):
    group = store.create("Night shift", ["nightwing", "robin"])
    t = time.time() - 4 * 3600
    first = group.add("me", "Anyone free?", at=t)
    group.add("robin", "me", at=t + 3 * 3600)
    group.react(first["id"], "nightwing", "👍")
    text = groupchat.transcript(group.messages(), lambda s: {"me": "Bruce", "robin": "Tim",
                                                              "nightwing": "Dick"}[s])
    assert "(3 hours later)" in text
    assert "👍 from Dick" in text
    group.react(first["id"], "nightwing", None)
    assert not group.messages()[0]["reactions"]


def test_a_tag_is_a_ping_and_a_name_is_just_a_name():
    tim = SimpleNamespace(id="robin", name="Tim", full_name="Tim Drake")
    assert groupchat.tagged("@tim you up?", tim)
    assert not groupchat.tagged("tim you up?", tim)
    assert groupchat.addressed("tim you up?", tim)


def test_someone_chases_a_question_she_hasnt_read(private_data, monkeypatch):
    import wayne.frontends.groupchats as gcm
    import wayne.frontends.web as web

    def contact(cid, name):
        return SimpleNamespace(id=cid, name=name, full_name=name, routine=(), shares_status=True,
                               texting_pace={"phone": 1.0}, texting_style={}, initiative={},
                               texting="")
    book = {c.id: c for c in (contact("nightwing", "Dick"), contact("batgirl", "Barbara"))}

    class Directory:
        def get(self, cid):
            return book.get(cid)

        def __iter__(self):
            return iter(book.values())

    console = web.Console.__new__(web.Console)

    console._adding, console._dropped_adds, console._closing = set(), set(), set()
    console.directory, console.clients, console._tasks = Directory(), {object()}, set()
    console._init_groups()
    chased = []

    async def post_as(group, who, unread=(), must=False, opening=None, task=None, chase=None):
        chased.append((who.id, chase))
    console._post_as = post_as
    monkeypatch.setattr(gcm, "CHASE_ODDS", 1.0)
    group = store.create("Night shift", ["nightwing", "batgirl"])
    asked = group.add("me", "@Barbara can you pull Tuesday's cameras?", at=time.time() - 15 * 60)
    group.add("me", "Anyone free at midnight?", at=time.time() - 14 * 60)
    group.mark_read("nightwing")
    presence.of(book["nightwing"]).touch()

    async def run():
        await console._group_chase(time.time())
        await asyncio.sleep(0.05)
    asyncio.run(run())
    assert chased and chased[0][0] == "nightwing" and chased[0][1]["name"] == "Barbara"
    assert asked["id"] in group.meta()["chased"]
    chased.clear()
    asyncio.run(run())
    assert not chased                       # once per question


def test_tags_on_whoever_is_already_talking_are_just_names(private_data):
    book = {"robin": SimpleNamespace(id="robin", name="Tim", full_name="Tim Drake"),
            "nightwing": SimpleNamespace(id="nightwing", name="Dick", full_name="Dick Grayson")}

    class Directory:
        def get(self, cid):
            return book.get(cid)
    group = store.create("Night shift", ["robin", "nightwing", "batgirl"])
    group.add("robin", "clean usually means professional")
    text = groupchat.needless_tags("@Tim it's surgical\nand @Dick should see this", group,
                                   Directory(), "batgirl")
    assert text == "it's surgical\nand @Dick should see this"


def test_one_of_them_asking_is_likely_but_not_owed():
    tim = SimpleNamespace(id="robin", name="Tim", full_name="Tim", initiative={}, texting_pace={})
    group = SimpleNamespace(members=["robin"])
    from_bruce = [{"from": "me", "text": "Tim?", "kind": None}]
    from_babs = [{"from": "batgirl", "text": "@Tim look at this", "kind": None}]
    assert groupchat.reply_odds(tim, group, from_bruce, energy=0.1) == 1.0
    assert groupchat.reply_odds(tim, group, from_babs, energy=0.1) < 0.5


# --- where they are --------------------------------------------------------------


def test_where_they_are_comes_from_their_plan_or_home(private_data, monkeypatch):
    from wayne.engine import initiative
    tim = SimpleNamespace(id="robin", name="Tim", full_name="Tim Drake", routine=(), shares_status=True,
                          shares_location=True, home="Wayne Manor", texting_pace={}, model="m",
                          options={}, system="")
    dick = SimpleNamespace(id="nightwing", name="Dick", full_name="Dick Grayson")
    reply = ('{"plan": [{"from": "00:00", "to": "23:59", "doing": "patrol", "status": "online", '
             '"where": "Crime Alley rooftops", "with": ["Dick"]}]}')
    from wayne.engine import model as llm
    monkeypatch.setattr(llm.ollama, "chat", lambda **kw: {"message": {"content": reply}})
    plan = initiative.day_plan(tim, people=[tim, dick])
    assert plan[0]["where"] == "Crime Alley rooftops" and plan[0]["with"] == ["nightwing"]
    whereabouts = presence.of(tim)
    assert whereabouts.whereabouts() == ("Wayne Manor", [])
    whereabouts.set_plan(plan)
    _free("nightwing")
    shown = whereabouts.public()
    assert shown["where"] == "Crime Alley rooftops" and shown["with"] == ["nightwing"]


def _free(contact_id):
    """Someone with a plan for today that has nothing on right now."""
    from wayne.contacts import directory
    other = presence.of(directory().get(contact_id))
    later = (time.localtime().tm_hour + 12) % 24
    errand = [{"from": later, "to": later + 0.5, "doing": "errands", "status": "busy", "where": "Bristol", "with": []}]
    other.set_plan(errand, t=time.time() - 86400)   # last night planned too: no routine left over from it
    other.set_plan(errand)
    return other


def _all_day(doing, where, status="busy", company=()):
    return [{"from": 0.0, "to": 23.99, "doing": doing, "status": status, "where": where,
             "with": list(company), "drift": 0}]


def test_together_only_when_it_holds_for_both(private_data):
    """
    Barbara's plan saying "with Cass" put nobody with anybody: Cass was on the
    other side of the city, and the map, the card and what Barbara said all
    disagreed. Together now holds for both, or for neither.
    """
    from wayne.contacts import directory
    tim = SimpleNamespace(id="robin", name="Tim", full_name="Tim Drake", routine=(), shares_status=True,
                          shares_location=True, home="Wayne Manor", texting_pace={})
    mine = presence.of(tim)
    mine.set_plan(_all_day("dinner", "The Bowery", company=["nightwing"]))
    dick = presence.of(directory().get("nightwing"))

    dick.set_plan(_all_day("asleep", "Home, Blüdhaven", status="offline"))
    assert mine.whereabouts() == ("The Bowery", []) and dick.whereabouts()[1] == []
    assert "with" not in mine.note()

    dick.set_plan(_all_day("errands", "Blüdhaven"))         # the other side of the river
    assert mine.whereabouts()[1] == [] and dick.whereabouts()[1] == []

    dick.set_plan(_all_day("a drink", "Crime Alley"))       # round the corner: the same evening
    assert mine.whereabouts() == ("The Bowery", ["nightwing"])
    assert dick.whereabouts() == ("The Bowery", ["robin"])
    assert "You're with Tim" in dick.note() and "You're with Dick" in mine.note()

    # What Dick said on a call outranks Tim's plan: he's at the docks, alone...
    dick.set_activity("checking the docks", "busy", 60, where="Gotham Docks")
    assert mine.whereabouts() == ("The Bowery", []) and dick.whereabouts() == ("Gotham Docks", [])
    # ...or with Tim, who's there with him, whatever Tim's plan said.
    dick.set_activity("checking the docks", "busy", 60, where="Gotham Docks", company=["robin"])
    assert mine.whereabouts() == ("Gotham Docks", ["nightwing"])
    assert mine.public()["spot"]["name"] == dick.public()["spot"]["name"]


def test_nobody_sees_where_someone_who_doesnt_share_is(private_data):
    jason = SimpleNamespace(id="redhood", name="Jason", full_name="Jason Todd", routine=(),
                            shares_status=False, shares_location=False, hidden_as="unknown",
                            home="", texting_pace={})
    assert "where" not in presence.of(jason).public()


def test_on_patrol_they_are_somewhere_on_their_beat_not_at_home(private_data, monkeypatch):
    from wayne.engine import incidents, places
    monkeypatch.setattr(incidents, "near", lambda areas, t=None: None)    # a quiet night
    tim = SimpleNamespace(id="robin", name="Tim", full_name="Tim Drake", shares_status=True,
                          shares_location=True, home="Wayne Manor", texting_pace={},
                          beat=("Diamond District", "Gotham Docks"),
                          routine=({"from": 0, "to": 24, "doing": "on patrol", "status": "online", "drift": 0},))
    whereabouts = presence.of(tim)
    where, _ = whereabouts.whereabouts()
    assert where in tim.beat
    spots = {whereabouts.whereabouts(t)[0] for t in range(0, 6 * 3600, places.BEAT_STEP)}
    assert len(spots) == 2                      # they move along it
    assert whereabouts.public()["spot"]["name"] in tim.beat


def test_places_people_write_land_on_the_map():
    from wayne.engine import places
    assert places.resolve("Little Italy, Blüdhaven")["name"] == "Blüdhaven"
    assert places.resolve("the clock tower")["name"] == "The Clocktower"
    assert places.resolve("Wayne Enterprises R&D Labs")["name"] == "Wayne Tower"
    assert places.resolve("somewhere vague") is None


def test_what_they_follow_reaches_a_conversation_about_it(private_data, monkeypatch):
    from wayne.engine import culture
    dick = SimpleNamespace(id="nightwing", name="Dick", interests={"follows": ["films"]}, model="m",
                           options={})
    monkeypatch.setattr(culture, "google_search",
                        lambda q, n: [{"title": "Box office", "snippet": "Verity opened at number one."}])
    from wayne.engine import model as llm
    monkeypatch.setattr(llm.ollama, "chat", lambda **kw: {
        "message": {"content": '{"items": ["Verity opened at number one this weekend"]}'}})
    assert culture.stale(dick)
    assert culture.refresh(dick) == ["Verity opened at number one this weekend"]
    assert not culture.stale(dick)
    assert "Verity" in culture.note(dick, "seen any good films?")
    assert culture.note(dick, "where are you?") == ""


def test_a_tapback_in_a_dm_lands_on_the_message_and_can_be_taken_back(private_data):
    from wayne.memory.texts import TextLog
    log = TextLog("nightwing")
    mine = log.add("me", "home in 10")
    assert log.react(mine["id"], "them", "👍")["reactions"] == {"them": "👍"}
    log.react(mine["id"], "them", None)
    assert log.page()[-1]["reactions"] == {}


def test_the_map_traces_where_a_patrol_has_been(private_data, monkeypatch):
    from wayne.engine import incidents, places
    monkeypatch.setattr(incidents, "near", lambda areas, t=None: None)
    tim = SimpleNamespace(id="robin", name="Tim", full_name="Tim Drake", shares_status=True,
                          shares_location=True, home="Wayne Manor", texting_pace={},
                          beat=("Diamond District", "Gotham Docks", "Old Gotham"),
                          routine=({"from": 0, "to": 24, "doing": "on patrol", "status": "online", "drift": 0},))
    trail = presence.of(tim).trail(hours=3)
    assert len(trail) >= 2
    assert all(p["name"] in tim.beat for p in trail)
    assert all(a["where"] != b["where"] for a, b in zip(trail, trail[1:], strict=False))
    assert places.resolve("Waterloo Docks, Blüdhaven")["area"] == "Blüdhaven"
    assert places.resolve("the docks")["name"] == "Gotham Docks"     # Gotham's, without Blüdhaven named


def test_a_patrol_goes_where_the_trouble_is_on_its_beat(private_data, monkeypatch):
    from wayne.engine import incidents, places
    tim = SimpleNamespace(id="robin", beat=("Diamond District", "Old Gotham"))
    monkeypatch.setattr(incidents, "near", lambda areas, t=None: {"place": "GCPD Central", "area": "Diamond District"})
    spots = {places.patrol_spot(tim, "", t) for t in range(0, 6 * 3600, places.BEAT_STEP)}
    assert "GCPD Central" in spots


def test_the_scanner_never_calls_from_inside_blackgate():
    import time as _time

    from wayne.engine import incidents
    reports = [r for t in range(0, 48 * 3600, 3 * 3600) for r in incidents.at(_time.time() - t)]
    assert reports
    assert not any(r["place"] in ("Blackgate Penitentiary", "Arkham Asylum", "Statue of Justice") for r in reports)


# --- cases ------------------------------------------------------------------------------


REPORT = {"id": "r1", "kind": "Armed robbery", "severity": 3, "place": "GCPD Central", "area": "Diamond District",
          "x": 54.4, "y": 102.4, "dispatch": "Two masked men, shots fired."}


def test_a_case_is_assigned_reaches_the_scene_and_closes_with_its_ending(private_data, monkeypatch):
    from wayne.engine import cases
    case = cases.assign(REPORT, "robin", by="him")
    assert cases.active("robin")["id"] == "r1" and case["status"] == "assigned"
    assert "armed robbery at GCPD Central" in cases.brief("robin") and "He put you on it" in cases.brief("robin")
    real = time.time
    monkeypatch.setattr(cases.time, "time", lambda: real() + 10 * 60)
    cases.advance()
    assert cases.active("robin")["status"] == "on scene"
    monkeypatch.setattr(cases.time, "time", lambda: real() + 3 * 3600)
    assert [c["id"] for c in cases.advance()] == ["r1"]          # run its course
    cases.close("r1", "Both in custody; one needed a hospital.")
    assert cases.active("robin") is None
    assert "Both in custody" in cases.brief("robin")             # still on their mind tonight
    assert "Tim" in cases.board_note({"robin": "Tim"})


def test_saying_its_handled_closes_the_case(private_data):
    from wayne.engine import cases, initiative
    cases.assign(REPORT, "robin", by="self")
    tim = SimpleNamespace(id="robin", name="Tim", initiative={}, texting_pace={}, routine=(),
                          shares_status=True, home="")
    initiative.apply(SimpleNamespace(contact=tim), {"case_closed": "caught them at the docks"})
    assert cases.active("robin") is None
    assert cases.recent("robin")["outcome"] == "caught them at the docks"


def test_only_the_field_takes_cases():
    from wayne.engine import cases
    assert "alfred" not in cases.FIELD and "lucius" not in cases.FIELD and "catwoman" not in cases.FIELD
    assert {"nightwing", "robin", "batgirl", "orphan"} <= set(cases.FIELD)


def test_taking_a_report_is_heard_in_what_they_say():
    from wayne.engine import session
    assert session._TAKE.search("On it. [take: Diamond District]").group(1) == "Diamond District"


# --- from the persona evaluation -------------------------------------------------------


def test_grief_is_not_a_cue_for_the_box_office():
    from wayne.engine import culture
    dick = SimpleNamespace(interests={"follows": ["new films and the box office"], "pastimes": ["trapeze"]})
    assert not culture.about_culture("rough night. lost someone. won't talk about it", dick)
    assert culture.about_culture("seen anything good lately?", dick)
    assert culture.about_culture("how was trapeze practice", dick)     # their own interests count
    assert culture.topic_for(SimpleNamespace(interests={"follows": ["England Test cricket"]}),
                             "who won the cricket this week?") == "England Test cricket"


def test_a_question_about_someone_in_his_life_is_never_a_web_search(private_data):
    from wayne.contacts import directory
    from wayne.engine.session import ContactSession
    session = ContactSession.__new__(ContactSession)
    session.contact = directory().get("catwoman")
    assert session._about_people("What was Randy like when he was little?")
    assert session._about_people("Who's going to be the problem?")
    assert not session._about_people("Who won the heavyweight fight last night?")


def test_sir_mid_sentence_is_lower_case():
    from wayne.engine import guards
    assert guards.tidy_address("Goodnight, Sir.") == "Goodnight, sir."
    assert guards.tidy_address("Sir, the car is ready.") == "Sir, the car is ready."


def test_tags_are_people_in_the_chat_written_as_they_show():
    """Dick wrote "@b" — nobody. A tag is one of them, him, or everyone; anything else isn't a tag."""
    book = {c.id: c for c in [SimpleNamespace(id="nightwing", name="Dick", full_name="Dick Grayson"),
                              SimpleNamespace(id="batgirl", name="Barbara", full_name="Barbara Gordon"),
                              SimpleNamespace(id="robin", name="Tim", full_name="Tim Drake"),
                              SimpleNamespace(id="orphan", name="Cass", full_name="Cassandra Cain")]}
    group = SimpleNamespace(members=["nightwing", "batgirl", "robin"])
    fix = lambda text: groupchat.fix_tags(text, group, book, "nightwing")    # noqa: E731
    assert fix("@b please tell me you didn't spend an hour on the name") == \
        "please tell me you didn't spend an hour on the name"               # could be Bruce or Barbara
    assert fix("@bruce, look") == "@Bruce, look"
    assert fix("@Barb pull the feed") == "@Barbara pull the feed"
    assert fix("@timmy's fault") == "@Tim's fault"
    assert fix("@Cass is out") == "Cass is out"                             # not in this chat
    assert fix("@all heads up") == "@everyone heads up"
    assert fix("mail me at a@b.com") == "mail me at a@b.com"


def test_everyone_pings_the_whole_chat():
    tim = SimpleNamespace(id="robin", name="Tim", full_name="Tim Drake")
    assert groupchat.tagged("@everyone briefing at ten", tim)
    assert groupchat.addressed("@everyone briefing at ten", tim)
    assert not groupchat.tagged("everyone's tired", tim)


def test_a_reaction_mostly_ends_it_but_a_question_mark_asks():
    from wayne.engine import initiative
    dick = SimpleNamespace(initiative={"per_day": 1.2}, texting_pace={})
    randy = SimpleNamespace(initiative={"per_day": 0.2}, texting_pace={"on_read": 0.6})
    assert initiative.tapback_odds(dick, "❤️") < 0.5
    assert initiative.tapback_odds(dick, "❓") > initiative.tapback_odds(dick, "❤️")
    assert initiative.tapback_odds(randy, "❓") < initiative.tapback_odds(dick, "❓")


def test_asked_in_a_dm_to_post_in_the_group_it_goes_in_the_group(monkeypatch):
    """Dick wrote the group's message — "you guys…", "and randy, quit ghosting us" — in his DM with Bruce."""
    book = {c.id: c for c in [SimpleNamespace(id="nightwing", name="Dick", full_name="Dick Grayson"),
                              SimpleNamespace(id="batwing", name="Randy", full_name="Randy")]}
    chat = SimpleNamespace(name="Bat Chat", members=["nightwing", "batwing"])
    monkeypatch.setattr(store, "groups_with", lambda cid: [chat])
    dick = book["nightwing"]
    assert groupchat.meant_group("THe chat with you and Randy. Say something in it.", dick, book) is chat
    assert groupchat.meant_group("tell the group I'm running late", dick, book) is chat
    assert groupchat.meant_group("how was patrol?", dick, book) is None
    assert groupchat.meant_group("Randy said something funny earlier", dick, book) is None
    wrong_thread = "you guys are literally just sitting there\nand randy, quit ghosting us!"
    assert groupchat.speaks_to_group(wrong_thread, chat, book, "nightwing")
    assert not groupchat.speaks_to_group("on it", chat, book, "nightwing")


def test_last_nights_routine_outlives_todays_plan_being_written(private_data):
    """At 00:30 Dick was on patrol; the moment his plan for the day was written, he was home and free."""
    night = SimpleNamespace(id="nightwing", name="Dick", full_name="Dick Grayson", shares_status=True,
                            shares_location=True, home="Home, Blüdhaven", texting_pace={},
                            routine=({"from": 21, "to": 3, "doing": "on patrol", "status": "online", "drift": 0},))
    state = presence.of(night)
    half_past = time.mktime(time.strptime("2026-10-10 00:30", "%Y-%m-%d %H:%M"))
    assert state.now(half_past)["doing"] == "on patrol"
    state.set_plan([{"from": 9.0, "to": 12.0, "doing": "brunch", "status": "idle", "where": "Blüdhaven"}], t=half_past)
    assert state.now(half_past)["doing"] == "on patrol"


def test_what_they_said_theyre_doing_puts_them_somewhere_sensible(private_data):
    lucius = SimpleNamespace(id="lucius", name="Lucius", full_name="Lucius Fox", routine=(), shares_status=True,
                             shares_location=True, home="Home, Upper East Side", texting_pace={})
    state = presence.of(lucius)
    state.set_activity("in a board meeting at Wayne Tower", "busy", 60)
    assert state.whereabouts()[0] == "Wayne Tower"
    state.set_activity("going to sleep", "offline", 480)
    assert state.whereabouts()[0] == "Home, Upper East Side"


def test_nobody_is_on_the_golf_course_at_three_in_the_morning(private_data):
    lucius = SimpleNamespace(id="lucius", name="Lucius", full_name="Lucius Fox", routine=(), shares_status=True,
                             shares_location=True, home="Home, Upper East Side",
                             texting_pace={"whim_rate": 1.0, "whims": [{"doing": "on the golf course", "minutes": 120}]})
    state = presence.of(lucius)
    small_hours = time.mktime(time.strptime("2026-10-10 03:10", "%Y-%m-%d %H:%M"))
    assert state._whim(small_hours) is None


def test_company_never_gives_away_someone_who_keeps_their_whereabouts_private(private_data):
    from wayne.contacts import directory
    dick = presence.of(directory().get("nightwing"))
    dick.set_activity("grabbing food in Crime Alley", "busy", 60, where="Crime Alley", company=["redhood"])
    jason = presence.of(directory().get("redhood"))
    jason.set_activity("grabbing food in Crime Alley", "busy", 60, where="Crime Alley", company=["nightwing"])
    assert dick.whereabouts()[1] == ["redhood"]         # they know who they're with
    assert dick.public()["with"] == []                  # but Bruce's console doesn't say


def test_assigning_a_case_sends_them_on_their_way_by_road(private_data, monkeypatch):
    import wayne.frontends.web as web
    from wayne.engine import cases, incidents
    tim = SimpleNamespace(id="robin", name="Tim", full_name="Tim Drake", initiative={"takes_orders": 1.0}, texting_pace={},
                          routine=(), shares_status=True, home="Houseboat, Gotham Marina", beat=())
    jason = SimpleNamespace(id="redhood", name="Jason", full_name="Jason Todd", initiative={"takes_orders": 1.0},
                            texting_pace={}, routine=(), shares_status=False, home="Jason's safehouse", beat=())
    console = web.Console.__new__(web.Console)
    console._adding, console._dropped_adds, console._closing, console._tasks = set(), set(), set(), set()
    console.directory = SimpleNamespace(get={"robin": tim, "redhood": jason}.get)
    events = []

    async def broadcast(e):
        events.append(e)
    console.broadcast = broadcast
    console._presence_changed = lambda c: asyncio.sleep(0)
    monkeypatch.setattr(incidents, "get", lambda rid: dict(REPORT, id=rid))
    monkeypatch.setattr(cases, "FIELD", ("robin", "redhood"))
    case = asyncio.run(console.assign_case("r1", "robin", tell=False))
    assert case and case["status"] == "assigned"
    doing = presence.of(tim).now()
    assert "on the way to the armed robbery at GCPD Central" in doing["doing"]
    assert presence.of(tim).whereabouts()[0] == "GCPD Central"           # heading there, so the map shows the trip
    asyncio.run(console.assign_case("r2", "redhood", tell=False))
    assert presence.of(jason).get("seen")["how"] == "on a case"           # he can't see Jason — but he knows this
