import json
import os
import unittest

import tests  # noqa: F401  (puts scripts/ on sys.path)

from fclib.api import FigmaAPI  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "basic")
KEY = "Zz9Yy8Xx7Ww6Vv5Uu4Tt3S"


def load(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as fh:
        return json.load(fh)


class FakeClient:
    """Records calls and returns canned responses (no network)."""

    def __init__(self, responses=None):
        self.calls = []
        self._responses = responses

    def call(self, method, path, body=None, query=None):
        self.calls.append((method, path, body, query))
        if callable(self._responses):
            return self._responses(method, path, body, query)
        return self._responses if self._responses is not None else {}


class CommentsTest(unittest.TestCase):
    def test_get_with_as_md_returns_list(self):
        fake = FakeClient({"comments": [{"id": "c1"}, {"id": "c2"}]})
        result = FigmaAPI(fake).comments(KEY)
        self.assertEqual(result, [{"id": "c1"}, {"id": "c2"}])
        self.assertEqual(
            fake.calls, [("GET", "/v1/files/%s/comments" % KEY, None, {"as_md": "true"})]
        )

    def test_missing_comments_key_raises(self):
        for resp in ({}, {"meta": 1}):
            with self.assertRaisesRegex(ValueError, "unexpected response"):
                FigmaAPI(FakeClient(resp)).comments(KEY)

    def test_non_list_comments_raises(self):
        with self.assertRaisesRegex(ValueError, "unexpected response"):
            FigmaAPI(FakeClient({"comments": None})).comments(KEY)

    def test_real_shape_fixture(self):
        fake = FakeClient(load("shape-comments.json"))
        self.assertIsInstance(FigmaAPI(fake).comments(KEY), list)


class MeTest(unittest.TestCase):
    def test_returns_dict_as_is(self):
        payload = load("me.json")
        fake = FakeClient(payload)
        self.assertEqual(FigmaAPI(fake).me(), payload)
        self.assertEqual(fake.calls, [("GET", "/v1/me", None, None)])


class NodesTest(unittest.TestCase):
    def test_names_and_null_from_fixture(self):
        fake = FakeClient(load("shape-nodes.json"))
        result = FigmaAPI(fake).nodes(KEY, ["1:3", "999:999"])
        self.assertEqual(result, {"1:3": "Hero title", "999:999": None})
        self.assertEqual(
            fake.calls,
            [("GET", "/v1/files/%s/nodes" % KEY, None, {"ids": "1:3,999:999", "depth": 1})],
        )

    def test_id_missing_from_response_is_none(self):
        fake = FakeClient({"nodes": {}})
        self.assertEqual(FigmaAPI(fake).nodes(KEY, ["5:5"]), {"5:5": None})

    def test_malformed_response_raises(self):
        for resp in ("text", {}, {"nodes": []}, {"nodes": None}, [], None):
            with self.assertRaisesRegex(ValueError, "unexpected response"):
                FigmaAPI(FakeClient(resp)).nodes(KEY, ["1:3"])

    def test_malformed_second_batch_raises(self):
        ids = ["1:%d" % i for i in range(150)]

        def responder(method, path, body, query):
            if len(query["ids"].split(",")) == 100:
                return {"nodes": {}}
            return {}

        with self.assertRaisesRegex(ValueError, "unexpected response"):
            FigmaAPI(FakeClient(responder)).nodes(KEY, ids)

    def test_entry_without_name_is_none(self):
        fake = FakeClient(
            {
                "nodes": {
                    "1:1": {},
                    "1:2": {"document": {}},
                    "1:3": {"document": {"name": None}},
                    "1:4": None,
                }
            }
        )
        result = FigmaAPI(fake).nodes(KEY, ["1:1", "1:2", "1:3", "1:4", "1:5"])
        self.assertEqual(result, {k: None for k in ["1:1", "1:2", "1:3", "1:4", "1:5"]})

    def test_empty_ids_makes_no_call(self):
        fake = FakeClient({})
        self.assertEqual(FigmaAPI(fake).nodes(KEY, []), {})
        self.assertEqual(fake.calls, [])

    def test_250_ids_make_3_calls(self):
        ids = ["1:%d" % i for i in range(250)]

        def responder(method, path, body, query):
            return {
                "nodes": {i: {"document": {"name": "N" + i}} for i in query["ids"].split(",")}
            }

        fake = FakeClient(responder)
        result = FigmaAPI(fake).nodes(KEY, ids)
        self.assertEqual(len(fake.calls), 3)
        self.assertEqual([len(c[3]["ids"].split(",")) for c in fake.calls], [100, 100, 50])
        self.assertEqual(len(result), 250)
        self.assertEqual(result["1:249"], "N1:249")

    def test_exactly_100_ids_one_call(self):
        ids = ["1:%d" % i for i in range(100)]
        fake = FakeClient({"nodes": {}})
        FigmaAPI(fake).nodes(KEY, ids)
        self.assertEqual(len(fake.calls), 1)

    def test_duplicate_ids_are_deduplicated(self):
        fake = FakeClient({"nodes": {"1:3": {"document": {"name": "A"}}}})
        result = FigmaAPI(fake).nodes(KEY, ["1:3", "1:3"])
        self.assertEqual(result, {"1:3": "A"})
        self.assertEqual(fake.calls[0][3]["ids"], "1:3")


class WriteTest(unittest.TestCase):
    def test_reply_body_has_no_client_meta(self):
        fake = FakeClient({"id": "new1"})
        result = FigmaAPI(fake).reply(KEY, "root1", "Hello")
        self.assertEqual(result, {"id": "new1"})
        method, path, body, query = fake.calls[0]
        self.assertEqual((method, path, query), ("POST", "/v1/files/%s/comments" % KEY, None))
        self.assertEqual(body, {"message": "Hello", "comment_id": "root1"})
        self.assertNotIn("client_meta", body)

    def test_react(self):
        fake = FakeClient({})
        self.assertIsNone(FigmaAPI(fake).react(KEY, "c9"))
        self.assertEqual(
            fake.calls,
            [
                (
                    "POST",
                    "/v1/files/%s/comments/c9/reactions" % KEY,
                    {"emoji": ":white_check_mark:"},
                    None,
                )
            ],
        )

    def test_delete(self):
        fake = FakeClient({})
        self.assertIsNone(FigmaAPI(fake).delete(KEY, "c9"))
        self.assertEqual(
            fake.calls, [("DELETE", "/v1/files/%s/comments/c9" % KEY, None, None)]
        )


class QuotingTest(unittest.TestCase):
    def test_crafted_ids_cannot_change_path(self):
        fake = FakeClient({"nodes": {}})
        api = FigmaAPI(fake)
        api.react("k/../x", "c/../../me")
        api.delete("k?a=1", "c#frag")
        api.reply("k/y", "r", "m")
        api.nodes("k/n", ["1:1"])
        paths = [c[1] for c in fake.calls]
        self.assertEqual(
            paths,
            [
                "/v1/files/k%2F..%2Fx/comments/c%2F..%2F..%2Fme/reactions",
                "/v1/files/k%3Fa%3D1/comments/c%23frag",
                "/v1/files/k%2Fy/comments",
                "/v1/files/k%2Fn/nodes",
            ],
        )

    def test_comments_path_quoted(self):
        fake = FakeClient({"comments": []})
        FigmaAPI(fake).comments("k%2Fz")
        self.assertEqual(fake.calls[0][1], "/v1/files/k%252Fz/comments")


if __name__ == "__main__":
    unittest.main()
