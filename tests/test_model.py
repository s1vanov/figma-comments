import copy
import json
import os
import unittest

import tests  # noqa: F401

from fclib.model import (
    AuthorSummary,
    Group,
    Participant,
    build_threads,
    classify,
    orphan_replies,
    parse_ts,
    unknown_authors,
)
from fclib.participants import load_participants

_FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "basic")
OWNER = "team-1"

# Expected group of every thread of the `basic` fixture (fixtures/basic/README.md).
EXPECTED = {
    "4200109": Group.NEW,
    "4200115": Group.NEW,
    "4200116": Group.NEW,
    "4200111": Group.NEW,
    "4200112": Group.NEW,
    "4200103": Group.CLIENT_REPLIED,
    "4200101": Group.RESOLVED,
    "4200104": Group.READY_TO_CLOSE,
    "4200117": Group.NEW,
    "4200107": Group.WAITING_CLIENT,
    "4200113": Group.INTERNAL,
    "4200118": Group.UNKNOWN_AUTHOR,
}


def _load(name):
    with open(os.path.join(_FIX, name), encoding="utf-8") as f:
        return json.load(f)["comments"]


def _people():
    return load_participants(os.path.join(_FIX, "participants.json"))


def _msg(cid, user, ts, parent="", resolved=None, reactions=None, text="x"):
    return {
        "id": cid,
        "parent_id": parent,
        "user": {"id": user, "handle": user},
        "created_at": ts,
        "resolved_at": resolved,
        "message": text,
        "reactions": reactions or [],
    }


def _check(user, ts):
    return {
        "emoji": ":white_check_mark:",
        "created_at": ts,
        "user": {"id": user, "handle": user},
    }


PEOPLE = {
    "c1": Participant("c1", "C", "client"),
    "t1": Participant("t1", "T", "team"),
}


def _group(comments, owner="t1"):
    threads = build_threads(comments)
    assert len(threads) == 1
    return classify(threads[0], PEOPLE, owner)


class ParseTsTest(unittest.TestCase):
    def test_z_suffix_and_fraction(self):
        a = parse_ts("2026-10-13T10:00:00Z")
        b = parse_ts("2026-10-13T10:00:00.905Z")
        c = parse_ts("2026-10-13T10:00:00.9Z")
        self.assertLess(a, b)
        self.assertEqual(b.microsecond, 905000)
        self.assertEqual(c.microsecond, 900000)
        self.assertIsNotNone(a.tzinfo)

    def test_offset_and_naive(self):
        self.assertEqual(
            parse_ts("2026-10-13T12:00:00+02:00"), parse_ts("2026-10-13T10:00:00Z")
        )
        self.assertEqual(parse_ts("2026-10-13T10:00:00"), parse_ts("2026-10-13T10:00:00Z"))


class BuildThreadsTest(unittest.TestCase):
    def test_fixture_has_twelve_threads(self):
        threads = build_threads(_load("comments.json"))
        self.assertEqual(len(threads), 12)
        self.assertEqual({t.root_id for t in threads}, set(EXPECTED))
        self.assertEqual(orphan_replies(_load("comments.json")), [])

    def test_messages_sorted_by_created_at_root_first(self):
        threads = {t.root_id: t for t in build_threads(_load("comments.json"))}
        t = threads["4200103"]
        self.assertEqual([m["id"] for m in t.messages], ["4200103", "4200105", "4200108"])
        self.assertEqual([m["id"] for m in t.replies], ["4200105", "4200108"])
        self.assertEqual(t.root["id"], "4200103")

    def test_replies_sorted_even_if_input_unordered(self):
        root = _msg("r", "c1", "2026-10-13T10:00:00Z")
        r2 = _msg("2", "t1", "2026-10-13T12:00:00Z", parent="r")
        r1 = _msg("1", "c1", "2026-10-13T11:00:00Z", parent="r")
        (t,) = build_threads([r2, r1, root])
        self.assertEqual([m["id"] for m in t.messages], ["r", "1", "2"])

    def test_sort_compares_parsed_datetimes_not_strings(self):
        # "…T10:00:00+02:00" is 08:00Z: earlier than 09:00Z although it sorts later as text.
        root = _msg("r", "c1", "2026-10-13T07:30:00Z")
        early = _msg("e", "t1", "2026-10-13T10:00:00+02:00", parent="r")
        late = _msg("l", "t1", "2026-10-13T09:00:00Z", parent="r")
        (t,) = build_threads([root, late, early])
        self.assertEqual([m["id"] for m in t.replies], ["e", "l"])

    def test_does_not_mutate_input(self):
        comments = _load("comments.json")
        before = copy.deepcopy(comments)
        build_threads(comments)
        orphan_replies(comments)
        self.assertEqual(comments, before)

    def test_orphan_replies_excluded_and_reported(self):
        root = _msg("r", "c1", "2026-10-13T10:00:00Z")
        ok = _msg("a", "t1", "2026-10-13T11:00:00Z", parent="r")
        orphan = _msg("o", "t1", "2026-10-13T11:00:00Z", parent="missing")
        threads = build_threads([orphan, ok, root])
        self.assertEqual(len(threads), 1)
        self.assertEqual([m["id"] for m in threads[0].messages], ["r", "a"])
        self.assertEqual([m["id"] for m in orphan_replies([orphan, ok, root])], ["o"])

    def test_reply_to_a_reply_is_an_orphan(self):
        root = _msg("r", "c1", "2026-10-13T10:00:00Z")
        reply = _msg("a", "t1", "2026-10-13T11:00:00Z", parent="r")
        nested = _msg("n", "c1", "2026-10-13T12:00:00Z", parent="a")
        self.assertEqual([m["id"] for m in orphan_replies([root, reply, nested])], ["n"])
        (t,) = build_threads([root, reply, nested])
        self.assertEqual([m["id"] for m in t.messages], ["r", "a"])

    def test_numeric_ids_are_tolerated(self):
        root = _msg("1", "c1", "2026-10-13T10:00:00Z")
        reply = _msg("2", "t1", "2026-10-13T11:00:00Z", parent="1")
        reply["parent_id"] = 1
        (t,) = build_threads([root, reply])
        self.assertEqual(len(t.replies), 1)


class ClassifyFixtureTest(unittest.TestCase):
    def test_every_thread_matches_the_readme_table(self):
        people = _people()
        for t in build_threads(_load("comments.json")):
            with self.subTest(root=t.root_id):
                self.assertEqual(classify(t, people, OWNER), EXPECTED[t.root_id])

    def test_after_crash_variant(self):
        expected = dict(EXPECTED)
        # Our replies at 10:41 come last in these four threads.
        for rid in ("4200103", "4200109", "4200115", "4200116"):
            expected[rid] = Group.WAITING_CLIENT
        people = _people()
        threads = build_threads(_load("comments-after-crash.json"))
        self.assertEqual(len(threads), 12)
        for t in threads:
            with self.subTest(root=t.root_id):
                self.assertEqual(classify(t, people, OWNER), expected[t.root_id])


class ClassifyRulesTest(unittest.TestCase):
    def test_resolved_beats_everything(self):
        comments = [
            _msg("r", "stranger", "2026-10-13T10:00:00Z", resolved="2026-10-13T11:00:00Z")
        ]
        self.assertEqual(_group(comments), Group.RESOLVED)

    def test_unknown_author_beats_internal_and_new(self):
        comments = [
            _msg("r", "c1", "2026-10-13T10:00:00Z"),
            _msg("a", "stranger", "2026-10-13T11:00:00Z", parent="r"),
        ]
        self.assertEqual(_group(comments), Group.UNKNOWN_AUTHOR)

    def test_owner_not_in_people_is_team(self):
        comments = [
            _msg("r", "c1", "2026-10-13T10:00:00Z"),
            _msg("a", "me", "2026-10-13T11:00:00Z", parent="r"),
        ]
        self.assertEqual(_group(comments, owner="me"), Group.WAITING_CLIENT)

    def test_owner_only_thread_is_internal(self):
        comments = [_msg("r", "me", "2026-10-13T10:00:00Z")]
        self.assertEqual(_group(comments, owner="me"), Group.INTERNAL)

    def test_owner_counts_as_team_even_if_listed_as_client(self):
        comments = [_msg("r", "c1", "2026-10-13T10:00:00Z")]
        self.assertEqual(_group(comments, owner="c1"), Group.INTERNAL)

    def test_no_client_message_is_internal(self):
        comments = [
            _msg("r", "t1", "2026-10-13T10:00:00Z"),
            _msg("a", "t1", "2026-10-13T11:00:00Z", parent="r"),
        ]
        self.assertEqual(_group(comments), Group.INTERNAL)

    def test_client_only_is_new(self):
        comments = [
            _msg("r", "c1", "2026-10-13T10:00:00Z"),
            _msg("a", "c1", "2026-10-13T11:00:00Z", parent="r"),
        ]
        self.assertEqual(_group(comments), Group.NEW)

    def test_team_started_client_replied_is_client_replied_not_internal(self):
        comments = [
            _msg("r", "t1", "2026-10-13T10:00:00Z"),
            _msg("a", "c1", "2026-10-13T11:00:00Z", parent="r"),
        ]
        self.assertEqual(_group(comments), Group.CLIENT_REPLIED)

    def test_last_message_team_is_waiting_client(self):
        comments = [
            _msg("r", "c1", "2026-10-13T10:00:00Z"),
            _msg("a", "t1", "2026-10-13T11:00:00Z", parent="r"),
        ]
        self.assertEqual(_group(comments), Group.WAITING_CLIENT)

    def test_client_after_team_is_client_replied(self):
        comments = [
            _msg("r", "c1", "2026-10-13T10:00:00Z"),
            _msg("a", "t1", "2026-10-13T11:00:00Z", parent="r"),
            _msg("b", "c1", "2026-10-13T12:00:00Z", parent="r"),
        ]
        self.assertEqual(_group(comments), Group.CLIENT_REPLIED)

    def test_client_message_after_check_mark_reopens_thread(self):
        comments = [
            _msg(
                "r", "c1", "2026-10-13T10:00:00Z",
                reactions=[_check("t1", "2026-10-13T11:00:00Z")],
            ),
            _msg("a", "c1", "2026-10-13T12:00:00Z", parent="r"),
        ]
        self.assertEqual(_group(comments), Group.NEW)


class ReadyToCloseTest(unittest.TestCase):
    def test_check_on_root_after_last_client_message(self):
        comments = [
            _msg(
                "r", "c1", "2026-10-13T10:00:00Z",
                reactions=[_check("t1", "2026-10-13T10:30:00Z")],
            )
        ]
        self.assertEqual(_group(comments), Group.READY_TO_CLOSE)

    def test_check_on_a_reply_counts(self):
        comments = [
            _msg("r", "c1", "2026-10-13T10:00:00Z"),
            _msg(
                "a", "t1", "2026-10-13T11:00:00Z", parent="r",
                reactions=[_check("t1", "2026-10-13T11:05:00Z")],
            ),
        ]
        self.assertEqual(_group(comments), Group.READY_TO_CLOSE)

    def test_check_before_last_client_message_does_not_count(self):
        comments = [
            _msg(
                "r", "c1", "2026-10-13T10:00:00Z",
                reactions=[_check("t1", "2026-10-13T10:30:00Z")],
            ),
            _msg("a", "c1", "2026-10-13T11:00:00Z", parent="r"),
        ]
        self.assertEqual(_group(comments), Group.NEW)

    def test_check_at_same_instant_does_not_count(self):
        comments = [
            _msg(
                "r", "c1", "2026-10-13T10:00:00Z",
                reactions=[_check("t1", "2026-10-13T10:00:00.000Z")],
            )
        ]
        self.assertEqual(_group(comments), Group.NEW)

    def test_comparison_uses_instants_not_text(self):
        # 10:30+02:00 is 08:30Z, i.e. before the client's 09:00Z message.
        comments = [
            _msg(
                "r", "c1", "2026-10-13T09:00:00Z",
                reactions=[_check("t1", "2026-10-13T10:30:00+02:00")],
            )
        ]
        self.assertEqual(_group(comments), Group.NEW)

    def test_client_check_mark_does_not_count(self):
        comments = [
            _msg(
                "r", "c1", "2026-10-13T10:00:00Z",
                reactions=[_check("c1", "2026-10-13T10:30:00Z")],
            )
        ]
        self.assertEqual(_group(comments), Group.NEW)

    def test_unknown_reactor_does_not_count(self):
        comments = [
            _msg(
                "r", "c1", "2026-10-13T10:00:00Z",
                reactions=[_check("stranger", "2026-10-13T10:30:00Z")],
            )
        ]
        self.assertEqual(_group(comments), Group.NEW)

    def test_owner_not_in_people_counts_as_team(self):
        comments = [
            _msg(
                "r", "c1", "2026-10-13T10:00:00Z",
                reactions=[_check("me", "2026-10-13T10:30:00Z")],
            )
        ]
        self.assertEqual(_group(comments, owner="me"), Group.READY_TO_CLOSE)

    def test_other_emoji_does_not_count(self):
        thumbs = {
            "emoji": ":+1:",
            "created_at": "2026-10-13T10:30:00Z",
            "user": {"id": "t1", "handle": "T"},
        }
        comments = [_msg("r", "c1", "2026-10-13T10:00:00Z", reactions=[thumbs])]
        self.assertEqual(_group(comments), Group.NEW)

    def test_ready_to_close_beats_waiting_client(self):
        comments = [
            _msg("r", "c1", "2026-10-13T10:00:00Z"),
            _msg(
                "a", "t1", "2026-10-13T11:00:00Z", parent="r",
                reactions=[_check("t1", "2026-10-13T11:01:00Z")],
            ),
        ]
        self.assertEqual(_group(comments), Group.READY_TO_CLOSE)

    def test_reaction_without_user_or_timestamp_is_ignored(self):
        comments = [
            _msg(
                "r", "c1", "2026-10-13T10:00:00Z",
                reactions=[{"emoji": ":white_check_mark:"}],
            )
        ]
        self.assertEqual(_group(comments), Group.NEW)


class UnknownAuthorsTest(unittest.TestCase):
    def test_fixture(self):
        threads = build_threads(_load("comments.json"))
        out = unknown_authors(threads, _people(), OWNER)
        self.assertEqual(len(out), 1)
        self.assertIsInstance(out[0], AuthorSummary)
        self.assertEqual((out[0].user_id, out[0].handle, out[0].count), ("unknown-1", "Dev", 1))
        self.assertTrue(out[0].sample.startswith("У проді відступ кнопки 12"))

    def test_order_count_and_sample(self):
        long_text = "line one\nline two\r\nline three " + "z" * 100
        comments = [
            _msg("r1", "c1", "2026-10-13T10:00:00Z"),
            _msg("s1", "ghost2", "2026-10-13T10:10:00Z", parent="r1", text="second ghost"),
            _msg("s0", "ghost1", "2026-10-13T10:05:00Z", parent="r1", text=long_text),
            _msg("r2", "ghost1", "2026-10-13T12:00:00Z", text="later message"),
            _msg("o", "me", "2026-10-13T12:30:00Z", text="owner is not unknown"),
        ]
        out = unknown_authors(build_threads(comments), PEOPLE, "me")
        # ghost1 appears (10:05) before ghost2 (10:10) in the first thread.
        self.assertEqual([a.user_id for a in out], ["ghost1", "ghost2"])
        self.assertEqual(out[0].count, 2)
        self.assertEqual(out[1].count, 1)
        self.assertEqual(out[0].sample, ("line one line two line three " + "z" * 100)[:80])
        self.assertEqual(len(out[0].sample), 80)
        self.assertNotIn("\n", out[0].sample)
        self.assertEqual(out[1].sample, "second ghost")

    def test_none_when_everyone_known(self):
        comments = [_msg("r", "c1", "2026-10-13T10:00:00Z")]
        self.assertEqual(unknown_authors(build_threads(comments), PEOPLE, "t1"), [])


if __name__ == "__main__":
    unittest.main()
