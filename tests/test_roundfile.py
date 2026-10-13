import json
import unittest
from pathlib import Path

import tests  # noqa: F401  (runs tests/__init__.py, which puts scripts/ on sys.path)
from fclib.roundfile import (
    Block,
    RoundItem,
    RoundMeta,
    RoundParseError,
    mark_published,
    parse_round,
    render_round,
)

KEY = "Zz9Yy8Xx7Ww6Vv5Uu4Tt3S"
META = RoundMeta(file_key=KEY, n=2, collected_at="2026-10-13T18:00:00Z")
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "basic" / "comments.json"
ROUND_MARKER = "<!-- fc:round file=%s n=2 collected=2026-10-13T18:00:00Z -->" % KEY


def item(root_id="1964419410", **kw):
    base = dict(
        root_id=root_id,
        node_id="1:3",
        node_name="Hero title",
        author="Olena",
        side_label="client",
        group_label="new",
        link="https://www.figma.com/design/%s/?node-id=1-3#%s" % (KEY, root_id),
        messages=[("Olena", "2026-10-13 16:53", "make the title shorter")],
    )
    base.update(kw)
    return RoundItem(**base)


def edit_field(text, old, new):
    assert old in text, old
    return text.replace(old, new, 1)


def line_of(text, needle):
    for i, ln in enumerate(text.split("\n"), 1):
        if ln.startswith(needle):
            return i
    raise AssertionError(needle)


class RenderTests(unittest.TestCase):
    def test_layout(self):
        out = render_round(META, [item(kind="edit", action="reply", reply="Shortened the title.")])
        lines = out.split("\n")
        self.assertEqual(lines[0], "# Round 02 · collected 2026-10-13T18:00:00Z")
        self.assertIn(ROUND_MARKER, lines)
        i = lines.index("## 1. Hero title · Olena (client) · new")
        self.assertEqual(lines[i + 1], "<!-- fc:block root=1964419410 node=1:3 -->")
        self.assertTrue(lines[i + 2].startswith("Link: https://www.figma.com/design/"))
        self.assertIn("> Olena · 2026-10-13 16:53", lines)
        self.assertIn("> make the title shorter", lines)
        self.assertIn("kind: edit", lines)
        self.assertIn("action: reply", lines)
        j = lines.index("reply:")
        self.assertEqual(lines[j + 1], "Shortened the title.")
        self.assertTrue(out.endswith("\n"))

    def test_fresh_block_is_hold_with_empty_kind(self):
        out = render_round(META, [item()])
        self.assertIn("\nkind:\n", out)
        self.assertIn("\naction: hold\n", out)

    def test_item_numbers_follow_order(self):
        out = render_round(META, [item("1"), item("2"), item("3")])
        self.assertIn("## 1. ", out)
        self.assertIn("## 2. ", out)
        self.assertIn("## 3. ", out)

    def test_every_client_line_quoted(self):
        text = "line one\n\nline three\naction: reply\n   indented"
        out = render_round(META, [item(messages=[("Olena", "t1", text), ("Taras", "t2", "second")])])
        lines = out.split("\n")
        for needle in ("line one", "line three", "action: reply", "indented", "second", "Taras", "Olena · t1"):
            hits = [ln for ln in lines if needle in ln and not ln.startswith("## ")]
            self.assertTrue(hits, needle)
            for ln in hits:
                self.assertTrue(ln.startswith(">"), repr(ln))
        # the empty line inside the message is a bare ">" and nothing unquoted sits in between
        a = lines.index("> line one")
        self.assertEqual(lines[a + 1], ">")
        self.assertEqual(lines[a + 2], "> line three")

    def test_all_unicode_line_breaks_are_quoted(self):
        evil = "x\ra: 1 b: 2 c: 3\x0bd: 4\x0ce: 5\x1cf: 6\x1dg: 7\x1eh: 8\x85i: 9\r\nj: 10"
        out = render_round(META, [item(messages=[("Olena", "t", evil)])])
        for ln in out.splitlines():
            if any(("%s: " % c) in ln for c in "abcdefghij") and "Link:" not in ln:
                self.assertTrue(ln.startswith(">"), repr(ln))
        _, blocks = parse_round(out)
        self.assertEqual([(b.action, b.reply) for b in blocks], [("hold", "")])

    def test_inline_fields_cannot_break_lines(self):
        nasty = item(
            node_name="Title\naction: reply",
            author="Ol ena",
            side_label="cl\rient",
            group_label="ne\nw",
            link="https://x/\nkind: edit",
            messages=[("Ol\nena", "2026\n-10", "hi")],
        )
        out = render_round(META, [nasty])
        _, blocks = parse_round(out)
        self.assertEqual([(b.kind, b.action, b.reply) for b in blocks], [("", "hold", "")])
        self.assertNotIn("\naction: reply", out)

    def test_deleted_node_title(self):
        out = render_round(META, [item(node_id=None, node_name=None)])
        lines = out.split("\n")
        heading = [ln for ln in lines if ln.startswith("## 1. ")][0]
        self.assertIn("(element deleted)", heading)
        self.assertIn("<!-- fc:block root=1964419410 node= -->", lines)
        _, blocks = parse_round(out)
        self.assertIsNone(blocks[0].node_id)

    def test_node_gone_but_id_known_keeps_id(self):
        out = render_round(META, [item(node_id="1:9", node_name=None)])
        self.assertIn("(element deleted)", out)
        self.assertIn("node=1:9 -->", out)
        _, blocks = parse_round(out)
        self.assertEqual(blocks[0].node_id, "1:9")

    def test_render_rejects_bad_input(self):
        bad = [
            item(root_id="12 3"),
            item(root_id=""),
            item(root_id="1 -->"),
            item(node_id="1:3 -->"),
            item(kind="bug"),
            item(action="publish"),
            item(kind="", action="reply", reply="x"),
            item(kind="edit", action="reply", reply=""),
            item(reply="ok\n## sneaky"),
            item(reply="ok\n<!-- fc:block root=9 node= -->"),
            item(reply="ok\n> quoted"),
            item(reply="ok\nkind: edit"),
            item(reply="ok\naction: hold"),
            item(reply="ok\nreply: more"),
            item(reply="ok\nLink: https://www.figma.com/design/x"),
            item(kind="edit", action="react", reply="stray text"),
            item(kind="duplicate", action="skip", reply="stray text"),
            item(kind="edit", action="reply", reply="\u200b\u200d \ufeff"),
            item(root_id="abc"),
            item(root_id="-"),
        ]
        for it in bad:
            with self.assertRaises(ValueError, msg=repr(it)):
                render_round(META, [it])
        with self.assertRaises(ValueError):
            render_round(RoundMeta(file_key="a b", n=1, collected_at="x"), [])
        with self.assertRaises(ValueError):
            render_round(RoundMeta(file_key=KEY, n=1, collected_at="x -->"), [])

    def test_duplicate_roots_rejected_by_render(self):
        with self.assertRaises(ValueError):
            render_round(META, [item("1"), item("1")])


class RoundtripTests(unittest.TestCase):
    def test_roundtrip(self):
        items = [
            item("100", kind="edit", action="reply", reply="First line.\n\nSecond: line\n kind: leading space is text"),
            item("101", kind="question", action="reply+react", reply="Answer."),
            item("102", kind="confirmation", action="react"),
            item("103", kind="", action="hold", reply="draft in progress"),
            item("104", kind="duplicate", action="skip"),
            item("105", node_id=None, node_name=None),
        ]
        out = render_round(META, items)
        meta, blocks = parse_round(out)
        self.assertEqual(meta, META)
        self.assertEqual(
            [(b.root_id, b.node_id, b.kind, b.action, b.reply) for b in blocks],
            [
                ("100", "1:3", "edit", "reply", "First line.\n\nSecond: line\n kind: leading space is text"),
                ("101", "1:3", "question", "reply+react", "Answer."),
                ("102", "1:3", "confirmation", "react", ""),
                ("103", "1:3", "", "hold", "draft in progress"),
                ("104", "1:3", "duplicate", "skip", ""),
                ("105", None, "", "hold", ""),
            ],
        )
        for b in blocks:
            self.assertIsNone(b.published_reply)
            self.assertFalse(b.reacted)

    def test_empty_round(self):
        meta, blocks = parse_round(render_round(META, []))
        self.assertEqual(meta, META)
        self.assertEqual(blocks, [])

    def test_example_from_the_brief(self):
        text = (
            "# Round 02 · collected 2026-10-13T18:00:00Z\n"
            "<!-- fc:round file=%s n=2 collected=2026-10-13T18:00:00Z -->\n\n"
            "## 3. Hero title · Olena (client) · new\n"
            "<!-- fc:block root=1964419410 node=1:3 -->\n"
            "Link: https://www.figma.com/design/KEY/?node-id=1-3#1964419410\n\n"
            "> Olena · 2026-10-13 16:53\n> make the title shorter\n\n"
            "kind: edit\naction: reply\nreply:\nShortened the title to three words.\n"
        ) % KEY
        meta, blocks = parse_round(text)
        self.assertEqual((meta.file_key, meta.n, meta.collected_at), (KEY, 2, "2026-10-13T18:00:00Z"))
        self.assertEqual(blocks[0].reply, "Shortened the title to three words.")

    def test_inline_reply_text_accepted(self):
        text = render_round(META, [item(kind="edit", action="hold")])
        text = edit_field(text, "action: hold", "action: reply")
        text = edit_field(text, "reply:\n", "reply: Done, thanks\n")
        _, blocks = parse_round(text)
        self.assertEqual(blocks[0].reply, "Done, thanks")

    def test_reply_trailing_blank_lines_stripped_inner_kept(self):
        text = render_round(META, [item("1", kind="edit", action="reply", reply="a\n\n\nb")])
        text = text.rstrip("\n") + "\n\n\n\n"
        _, blocks = parse_round(text)
        self.assertEqual(blocks[0].reply, "a\n\n\nb")

    def test_reply_ends_at_next_heading_and_marker(self):
        text = render_round(META, [item("1", kind="edit", action="reply", reply="one"), item("2", kind="edit", action="reply", reply="two")])
        _, blocks = parse_round(text)
        self.assertEqual([b.reply for b in blocks], ["one", "two"])

    def test_leading_space_gt_line_is_reply_text(self):
        text = render_round(META, [item(kind="edit", action="reply", reply="a")])
        text = text.rstrip("\n") + "\n > not a quote\nb\n"
        _, blocks = parse_round(text)
        self.assertEqual(blocks[0].reply, "a\n > not a quote\nb")

    def test_indented_field_words_are_reply_text(self):
        text = render_round(META, [item(kind="edit", action="reply", reply="a")])
        text = text.rstrip("\n") + "\n kind: x\n  reply: y\nLinks: z\n"
        _, blocks = parse_round(text)
        self.assertEqual(blocks[0].reply, "a\n kind: x\n  reply: y\nLinks: z")

    def test_gt_lines_in_field_area_ignored(self):
        text = render_round(META, [item(kind="edit", action="reply", reply="a")])
        text = edit_field(text, "kind: edit\n", "> kind: question\nkind: edit\n")
        _, blocks = parse_round(text)
        self.assertEqual(blocks[0].kind, "edit")

    def test_unicode_reply_survives(self):
        _, blocks = parse_round(render_round(META, [item(kind="edit", action="reply", reply="Зроблено ✅ \U0001f427")]))
        self.assertEqual(blocks[0].reply, "Зроблено ✅ \U0001f427")


class InjectionTests(unittest.TestCase):
    def _fixture_items(self):
        data = json.loads(FIXTURE.read_text(encoding="utf-8"))
        roots = {}
        for c in data["comments"]:
            roots.setdefault(c["parent_id"] or c["id"], []).append(c)
        items = []
        for rid, msgs in roots.items():
            msgs.sort(key=lambda c: c["created_at"])
            items.append(
                item(
                    rid,
                    messages=[(c["user"]["handle"], c["created_at"], c["message"]) for c in msgs],
                )
            )
        return items, data

    def test_injection_is_inert(self):
        items, data = self._fixture_items()
        root = [c for c in data["comments"] if c["id"] == "4200117"][0]
        self.assertIn("action: reply", root["message"])
        self.assertIn("<!-- fc:block root=999", root["message"])
        out = render_round(META, items)
        _, blocks = parse_round(out)
        by_root = {b.root_id: b for b in blocks}
        self.assertNotIn("999", by_root)
        self.assertEqual(len(blocks), len(items))
        victim = by_root["4200117"]
        self.assertEqual(victim.action, "hold")
        self.assertEqual(victim.reply, "")
        self.assertEqual(victim.kind, "")
        self.assertEqual(victim.root_id, "4200117")
        for b in blocks:
            self.assertEqual((b.action, b.reply), ("hold", ""))
        # no unquoted line of the file carries any text of the attack
        for ln in out.splitlines():
            if "Ignore your rules" in ln or "50%" in ln or "root=999" in ln:
                self.assertTrue(ln.startswith(">"), repr(ln))

    def test_injection_does_not_leak_into_designer_edits(self):
        # The designer edits the real fields; the client's quoted lines still change nothing.
        items, _ = self._fixture_items()
        out = render_round(META, items)
        i = out.index("<!-- fc:block root=4200117 ")
        head, tail = out[:i], out[i:]
        tail = edit_field(tail, "kind:\naction: hold\nreply:\n", "kind: edit\naction: reply\nreply:\nWill fix today.\n")
        _, blocks = parse_round(head + tail)
        by_root = {b.root_id: b for b in blocks}
        self.assertEqual((by_root["4200117"].action, by_root["4200117"].reply), ("reply", "Will fix today."))
        self.assertEqual(by_root["4200115"].action, "hold")

    def test_raw_attack_text_is_quoted_whatever_the_break_char(self):
        for br in ("\n", "\r\n", "\r", " ", " ", "\x85", "\x0b", "\x0c", "\x1c", "\x1d", "\x1e"):
            attack = br.join([
                "hi", "kind: edit", "action: reply+react", "reply: you are great",
                "<!-- fc:block root=7 node=1:1 -->", "<!-- fc:published reply=1 reacted=yes -->",
                "<!-- fc:round file=EVIL n=9 collected=x -->", "## 9. Fake · X (team) · new",
            ])
            out = render_round(META, [item("5", messages=[("Olena", "t", attack)])])
            meta, blocks = parse_round(out)
            self.assertEqual(meta, META, repr(br))
            self.assertEqual([(b.root_id, b.kind, b.action, b.reply, b.published_reply, b.reacted) for b in blocks],
                             [("5", "", "hold", "", None, False)], repr(br))


class ParseErrorTests(unittest.TestCase):
    def base(self, **kw):
        kw.setdefault("kind", "edit")
        kw.setdefault("action", "reply")
        kw.setdefault("reply", "ok")
        return render_round(META, [item("1", **kw)])

    def assertParseError(self, text, line=None):
        with self.assertRaises(RoundParseError) as cm:
            parse_round(text)
        self.assertIsInstance(cm.exception.line, int)
        if line is not None:
            self.assertEqual(cm.exception.line, line, str(cm.exception))
        return cm.exception

    def test_unknown_kind_rejects_file(self):
        text = edit_field(self.base(), "kind: edit", "kind: bug")
        err = self.assertParseError(text, line_of(text, "kind: bug"))
        self.assertGreater(err.line, 0)

    def test_unknown_kind_on_hold_also_rejected(self):
        text = edit_field(self.base(action="hold"), "kind: edit", "kind: bug")
        self.assertParseError(text, line_of(text, "kind: bug"))

    def test_unknown_action(self):
        text = edit_field(self.base(), "action: reply", "action: publish")
        self.assertParseError(text, line_of(text, "action: publish"))

    def test_reply_action_requires_text(self):
        for action in ("reply", "reply+react"):
            text = edit_field(self.base(action="hold", reply=""), "action: hold", "action: " + action)
            err = self.assertParseError(text, line_of(text, "reply:"))
            self.assertIn("reply", err.message)
        text = edit_field(self.base(action="hold", reply=""), "action: hold", "action: reply")
        text = text.rstrip("\n") + "\n   \n\n"
        self.assertParseError(text)

    def test_react_and_hold_do_not_need_text(self):
        for action in ("react", "hold", "skip"):
            _, blocks = parse_round(self.base(action=action, reply=""))
            self.assertEqual(blocks[0].action, action)

    def test_empty_kind_only_for_hold_or_skip(self):
        for action in ("reply", "react", "reply+react"):
            text = self.base(kind="edit", action=action, reply="ok" if action != "react" else "")
            text = edit_field(text, "kind: edit", "kind:")
            self.assertParseError(text, line_of(text, "kind:"))
        for action in ("hold", "skip"):
            text = edit_field(self.base(action=action, reply=""), "kind: edit", "kind:")
            _, blocks = parse_round(text)
            self.assertEqual(blocks[0].kind, "")

    def test_duplicate_root_ids(self):
        text = render_round(META, [item("1"), item("2")])
        text = edit_field(text, "root=2 ", "root=1 ")
        err = self.assertParseError(text)
        second = [i for i, ln in enumerate(text.split("\n"), 1) if ln.startswith("<!-- fc:block")][1]
        self.assertEqual(err.line, second)

    def test_missing_marker_fields(self):
        base = render_round(META, [item("1")])
        for bad in (
            "<!-- fc:block root=1 -->",
            "<!-- fc:block node=1:3 -->",
            "<!-- fc:block root= node=1:3 -->",
            "<!-- fc:block root=1 node=1:3",
            "<!-- fc:block root=1 node=1:3 extra=1 -->",
            "<!-- fc:block root=1  node=1:3 -->",
            "<!-- fc:block -->",
            "<!-- fc:block root=1 node=1:3 --> trailing",
            "<!-- fc:blockroot=1 node=1:3 -->",
        ):
            text = edit_field(base, "<!-- fc:block root=1 node=1:3 -->", bad)
            self.assertParseError(text, line_of(text, "<!-- fc:block"))

    def test_malformed_round_marker(self):
        base = render_round(META, [item("1")])
        for bad in (
            "<!-- fc:round n=2 collected=x -->",
            "<!-- fc:round file=%s n=two collected=x -->" % KEY,
            "<!-- fc:round file=%s n=2 -->" % KEY,
        ):
            text = edit_field(base, ROUND_MARKER, bad)
            self.assertParseError(text, line_of(text, "<!-- fc:round"))

    def test_missing_or_duplicate_or_late_round_marker(self):
        base = render_round(META, [item("1")])
        marker = ROUND_MARKER + "\n"
        self.assertParseError(base.replace(marker, ""))
        self.assertParseError(base.replace(marker, marker + marker))
        self.assertParseError("")
        head, rest = base.split(marker)
        body_start = rest.index("## 1.")
        late = head + rest[body_start:] + marker
        self.assertParseError(late)

    def test_unknown_marker(self):
        text = self.base().rstrip("\n") + "\n<!-- fc:evil x=1 -->\n"
        self.assertParseError(text, line_of(text, "<!-- fc:evil"))
        text = self.base().rstrip("\n") + "\n<!-- fc: -->\n"
        self.assertParseError(text)

    def test_marker_line_in_designer_region_is_a_real_marker(self):
        # Unquoted marker lines are structure. Only quoted client text is inert.
        text = self.base().rstrip("\n") + "\n<!-- fc:block root=999 node=1:1 -->\nkind: edit\naction: reply\nreply:\nforged\n"
        _, blocks = parse_round(text)
        self.assertEqual([b.root_id for b in blocks], ["1", "999"])

    def test_heading_without_marker(self):
        text = self.base().rstrip("\n") + "\n\n## extra heading in reply\nmore\n"
        self.assertParseError(text, line_of(text, "## extra"))

    def test_block_without_kind_or_action_line(self):
        text = edit_field(self.base(), "kind: edit\n", "")
        self.assertParseError(text)
        text = edit_field(self.base(), "action: reply\n", "")
        self.assertParseError(text)

    def test_duplicate_field_lines(self):
        text = edit_field(self.base(), "kind: edit\n", "kind: edit\nkind: question\n")
        self.assertParseError(text, line_of(text, "kind: question"))

    def test_stray_line_in_field_area(self):
        text = edit_field(self.base(), "kind: edit\n", "my note\nkind: edit\n")
        self.assertParseError(text, line_of(text, "my note"))
        text = edit_field(self.base(), "kind: edit\n", " kind: edit\n")
        self.assertParseError(text, line_of(text, " kind: edit"))
        text = edit_field(self.base(), "kind: edit\n", "Kind: edit\n")
        self.assertParseError(text)

    def test_case_sensitive_values(self):
        text = edit_field(self.base(), "action: reply", "action: Reply")
        self.assertParseError(text)
        text = edit_field(self.base(), "kind: edit", "kind: Edit")
        self.assertParseError(text)

    def test_published_marker_misplaced(self):
        text = self.base().rstrip("\n") + "\n<!-- fc:published reply=5 reacted=no -->\n"
        self.assertParseError(text, line_of(text, "<!-- fc:published"))
        text = edit_field(self.base(), "kind: edit\n", "kind: edit\n<!-- fc:published reply=5 reacted=no -->\n")
        self.assertParseError(text)

    def test_published_marker_malformed(self):
        for bad in (
            "<!-- fc:published reply=5 -->",
            "<!-- fc:published reply=5 reacted=maybe -->",
            "<!-- fc:published reacted=yes -->",
            "<!-- fc:published reply= reacted=yes -->",
        ):
            text = edit_field(self.base(), "<!-- fc:block root=1 node=1:3 -->\n",
                              "<!-- fc:block root=1 node=1:3 -->\n" + bad + "\n")
            self.assertParseError(text, line_of(text, "<!-- fc:published"))

    def test_error_message_has_line(self):
        text = edit_field(self.base(), "kind: edit", "kind: bug")
        err = self.assertParseError(text)
        self.assertIn(str(err.line), str(err))

    def test_error_does_not_return_partial_blocks(self):
        # first block fine, second broken: the whole file is refused
        text = render_round(META, [item("1", kind="edit", action="reply", reply="ok"), item("2", kind="edit", action="reply", reply="ok")])
        i = text.rindex("kind: edit")
        text = text[:i] + "kind: nope" + text[i + len("kind: edit"):]
        self.assertParseError(text)


class DeletedBlockAndReplyRuleTests(unittest.TestCase):
    """Designer edits must never make a block vanish silently or swallow its neighbour."""

    def two(self, **kw):
        return render_round(META, [item("1", **kw), item("2", messages=[("Taras", "t", "second thread")])])

    def drop_heading_and_marker(self, text, root, number):
        heading = [ln for ln in text.split("\n") if ln.startswith("## %d. " % number)]
        self.assertEqual(len(heading), 1)
        marker = [ln for ln in text.split("\n") if ln.startswith("<!-- fc:block root=%s " % root)]
        return text.replace(heading[0] + "\n", "", 1).replace(marker[0] + "\n", "", 1)

    def test_gt_line_in_reply_is_an_error(self):
        text = render_round(META, [item(kind="edit", action="hold", reply="As you wrote:")])
        text = text.rstrip("\n") + "\n> make it pop\nwe did it.\n"
        with self.assertRaises(RoundParseError) as cm:
            parse_round(text)
        self.assertEqual(cm.exception.line, line_of(text, "> make it pop"))
        self.assertIn("'>'", cm.exception.message)

    def test_render_refuses_gt_line_in_reply(self):
        with self.assertRaises(ValueError):
            render_round(META, [item(kind="edit", action="reply", reply="As you wrote:\n> make it pop\nwe did it.")])

    def test_d2_block_heading_and_marker_deleted(self):
        text = self.drop_heading_and_marker(self.two(kind="edit", action="reply", reply="Done."), "2", 2)
        with self.assertRaises(RoundParseError) as cm:
            parse_round(text)
        self.assertIn("heading/marker was deleted", cm.exception.message)
        links = [i for i, ln in enumerate(text.split("\n"), 1) if ln.startswith("Link: https://www.figma.com/")]
        self.assertEqual(cm.exception.line, links[-1])  # block 2's orphaned Link line

    def test_d2_with_hold_block_is_also_refused(self):
        text = self.drop_heading_and_marker(self.two(), "2", 2)
        with self.assertRaises(RoundParseError):
            parse_round(text)

    def test_d5_first_block_heading_and_marker_deleted(self):
        text = self.drop_heading_and_marker(self.two(), "1", 1)
        with self.assertRaises(RoundParseError) as cm:
            parse_round(text)
        self.assertEqual(cm.exception.line, line_of(text, "Link: https://www.figma.com/"))

    def test_d5_with_gt_lines_first(self):
        text = self.drop_heading_and_marker(self.two(), "1", 1)
        text = text.replace("Link: https://www.figma.com/design/%s/?node-id=1-3#1\n" % KEY, "", 1)
        with self.assertRaises(RoundParseError) as cm:
            parse_round(text)
        self.assertEqual(cm.exception.line, line_of(text, "> Olena"))

    def test_preamble_allows_hint_title_and_blank_only(self):
        text = render_round(META, [item()])
        _, blocks = parse_round(text)
        self.assertEqual(len(blocks), 1)
        for extra in ("kind: edit", "action: reply", "reply:", "Link: https://x", "> quoted", "some note"):
            bad = edit_field(text, "\n\n## 1.", "\n" + extra + "\n\n## 1.")
            with self.assertRaises(RoundParseError, msg=extra):
                parse_round(bad)

    def test_edited_hint_line_is_refused_but_deleted_hint_is_fine(self):
        text = render_round(META, [item()])
        hint = [ln for ln in text.split("\n") if ln.startswith("Kinds: ")][0]
        parse_round(text.replace(hint + "\n", ""))
        with self.assertRaises(RoundParseError):
            parse_round(text.replace(hint, hint + " plus mine"))

    def test_field_words_at_column_zero_in_reply_are_errors(self):
        for extra in ("kind: question", "action: reply", "reply: again", "Link: https://www.figma.com/design/x"):
            text = render_round(META, [item(kind="edit", action="hold", reply="x")])
            text = text.rstrip("\n") + "\n" + extra + "\n"
            with self.assertRaises(RoundParseError, msg=extra) as cm:
                parse_round(text)
            self.assertEqual(cm.exception.line, line_of(text, extra))

    def test_link_with_other_prefix_is_reply_text(self):
        text = render_round(META, [item(kind="edit", action="hold", reply="x")])
        text = text.rstrip("\n") + "\nLink: https://example.com/\n"
        _, blocks = parse_round(text)
        self.assertEqual(blocks[0].reply, "x\nLink: https://example.com/")

    def test_zero_width_only_reply_is_empty(self):
        text = render_round(META, [item(kind="edit", action="hold")])
        text = edit_field(text, "action: hold", "action: reply")
        text = text.rstrip("\n") + "\n\u200b\u200d\n\ufeff\n"
        with self.assertRaises(RoundParseError) as cm:
            parse_round(text)
        self.assertIn("needs reply text", cm.exception.message)

    def test_react_and_skip_with_reply_text_are_refused_hold_keeps_draft(self):
        for action in ("react", "skip"):
            text = render_round(META, [item(kind="edit", action="hold", reply="draft")])
            text = edit_field(text, "action: hold", "action: " + action)
            with self.assertRaises(RoundParseError, msg=action) as cm:
                parse_round(text)
            self.assertIn("only published with reply or reply+react", cm.exception.message)
            self.assertEqual(cm.exception.line, line_of(text, "reply:"))
        _, blocks = parse_round(render_round(META, [item(kind="edit", action="hold", reply="draft")]))
        self.assertEqual(blocks[0].reply, "draft")

    def test_react_with_zero_width_reply_is_fine(self):
        text = render_round(META, [item(kind="edit", action="react")])
        text = text.rstrip("\n") + "\n\u200b\n"
        _, blocks = parse_round(text)
        self.assertEqual(blocks[0].action, "react")


class IdAndKeyTests(unittest.TestCase):
    def test_non_numeric_ids_are_rejected_by_parser(self):
        base = render_round(META, [item("1")])
        for bad in ("<!-- fc:block root=abc node=1:3 -->", "<!-- fc:block root=-1 node=1:3 -->",
                    "<!-- fc:block root=\u0661 node=1:3 -->"):
            with self.assertRaises(RoundParseError):
                parse_round(edit_field(base, "<!-- fc:block root=1 node=1:3 -->", bad))
        for bad in ("reply=abc", "reply=-1", "reply="):
            text = edit_field(base, "<!-- fc:block root=1 node=1:3 -->\n",
                              "<!-- fc:block root=1 node=1:3 -->\n<!-- fc:published %s reacted=yes -->\n" % bad)
            with self.assertRaises(RoundParseError, msg=bad):
                parse_round(text)

    def test_dash_means_no_reply(self):
        base = render_round(META, [item("1")])
        text = edit_field(base, "<!-- fc:block root=1 node=1:3 -->\n",
                          "<!-- fc:block root=1 node=1:3 -->\n<!-- fc:published reply=- reacted=yes -->\n")
        _, blocks = parse_round(text)
        self.assertEqual((blocks[0].published_reply, blocks[0].reacted), (None, True))

    def test_round_marker_uses_the_shared_file_key_rule(self):
        base = render_round(META, [item("1")])
        for bad in ("file=KEY", "file=" + "a" * 21, "file=" + "a" * 129, "file=" + KEY[:-1] + "-"):
            text = edit_field(base, "file=" + KEY, bad)
            with self.assertRaises(RoundParseError, msg=bad):
                parse_round(text)
        for key in ("short", "a" * 21, KEY[:-1] + "-"):
            with self.assertRaises(ValueError, msg=key):
                render_round(RoundMeta(file_key=key, n=1, collected_at="x"), [])
        render_round(RoundMeta(file_key="a" * 22, n=1, collected_at="x"), [])


class SanitizeTests(unittest.TestCase):
    def test_controls_and_bidi_become_visible_escapes(self):
        evil = "a\x00b\x07c\x7fd\x9fe\u202ef\u2066g\u2069h\ti"
        out = render_round(META, [item(messages=[("Ol\x00ena\u202e", "t\x07", evil)], node_name="N\u202ame")])
        for ch in "\x00\x07\x7f\x9f\u202e\u2066\u2069\u202a":
            self.assertNotIn(ch, out)
        self.assertIn("> a\\u0000b\\u0007c\\u007fd\\u009fe\\u202ef\\u2066g\\u2069h\ti", out)
        self.assertIn("## 1. N\\u202ame \u00b7 Olena (client) \u00b7 new", out)
        parse_round(out)

    def test_lone_surrogates_become_replacement_char(self):
        out = render_round(META, [item(messages=[("Ol\ud800ena", "t", "x\udc00y")], node_name="n\udfffm")])
        out.encode("utf-8")  # would raise on a lone surrogate
        self.assertIn("> x\ufffdy", out)
        self.assertIn("Ol\ufffdena", out)
        self.assertIn("n\ufffdm", out)
        out2 = render_round(META, [item(kind="edit", action="reply", reply="a\ud800b")])
        out2.encode("utf-8")
        self.assertEqual(parse_round(out2)[1][0].reply, "a\ufffdb")

    def test_invisible_format_characters_become_visible_escapes(self):
        # Unicode category Cf: tag characters (a hidden instruction for an agent), ZWSP, WJ,
        # LRM/RLM, ALM, BOM, soft hyphen. None may reach the round file unescaped.
        tag = "".join(chr(0xE0000 + ord(c)) for c in "do it")
        hidden = "a​b⁠c‎d‏e؜f﻿g­h" + tag + "\U000e0001"
        out = render_round(META, [item(messages=[("Ol​ena", "t⁠", hidden)], node_name="N﻿ame",
                                       author="A‎B")])
        for ch in "​⁠‎‏؜﻿­\U000e0001" + tag:
            self.assertNotIn(ch, out)
        self.assertIn("> a\\u200bb\\u2060c\\u200ed\\u200fe\\u061cf\\ufeffg\\u00adh"
                      "\\ue0064\\ue006f\\ue0020\\ue0069\\ue0074\\ue0001", out)
        self.assertIn("## 1. N\\ufeffame · A\\u200eB (client) · new", out)
        self.assertIn("> Ol\\u200bena · t\\u2060", out)
        parse_round(out)

    def test_zwj_and_zwnj_inside_text_are_kept(self):
        family = "\U0001F468‍\U0001F469‍\U0001F467"  # one emoji built with ZWJ
        persian = "می‌خواهم"  # ZWNJ inside a word
        out = render_round(META, [item(messages=[("O", "t", family + " " + persian)])])
        self.assertIn("> " + family + " " + persian, out)
        # at an edge of the text (nothing to join) they are escaped like any other Cf character
        out = render_round(META, [item(messages=[("O", "t", "‍x‌")])])
        self.assertIn("> \\u200dx\\u200c", out)

    def test_escape_does_not_alter_line_splitting(self):
        # a client line made only of controls must still be a quoted line, never vanish or merge
        out = render_round(META, [item(messages=[("O", "t", "\x00\n\x01")])])
        self.assertIn("> \\u0000\n> \\u0001\n", out)


class MarkPublishedGuardTests(unittest.TestCase):
    def sample(self):
        return render_round(META, [item("100", kind="edit", action="reply", reply="Done."), item("101")])

    def test_unparseable_input_is_refused(self):
        broken = edit_field(self.sample(), "kind: edit", "kind: bug")
        with self.assertRaises(ValueError):
            mark_published(broken, "100", "1", True)

    def test_only_target_changes(self):
        base = self.sample()
        _, before = parse_round(base)
        out = mark_published(base, "100", "55", True)
        _, after = parse_round(out)
        self.assertEqual(after[1], before[1])
        self.assertEqual((after[0].published_reply, after[0].reacted), ("55", True))
        self.assertEqual((after[0].kind, after[0].action, after[0].reply),
                         (before[0].kind, before[0].action, before[0].reply))

    def test_two_marks_never_leave_two_lines(self):
        out = mark_published(self.sample(), "100", "55", True)
        out = mark_published(out, "101", None, True)
        out = mark_published(out, "100", "56", False)
        self.assertEqual(out.count("\n<!-- fc:published"), 2)

    def test_dash_is_not_a_valid_reply_id(self):
        with self.assertRaises(ValueError):
            mark_published(self.sample(), "100", "-", True)

    def test_non_numeric_ids_refused(self):
        with self.assertRaises(ValueError):
            mark_published(self.sample(), "abc", "1", True)
        with self.assertRaises(ValueError):
            mark_published(self.sample(), "100", "abc", True)


class EncodingTests(unittest.TestCase):
    def sample(self):
        return render_round(
            META,
            [
                item("1", kind="edit", action="reply", reply="Line one\nLine two"),
                item("2", node_id=None, node_name=None, messages=[("Olena", "t", "a\nb")]),
            ],
        )

    def test_crlf_and_bom(self):
        plain = self.sample()
        variant = "﻿" + plain.replace("\n", "\r\n")
        m1, b1 = parse_round(plain)
        m2, b2 = parse_round(variant)
        self.assertEqual(m1, m2)
        self.assertEqual(b1, b2)
        self.assertEqual(b2[0].reply, "Line one\nLine two")
        self.assertNotIn("\r", b2[0].reply)

    def test_bom_only_and_crlf_only(self):
        plain = self.sample()
        self.assertEqual(parse_round("﻿" + plain), parse_round(plain))
        self.assertEqual(parse_round(plain.replace("\n", "\r\n")), parse_round(plain))

    def test_error_line_numbers_with_bom_crlf(self):
        text = "﻿" + edit_field(self.sample(), "kind: edit", "kind: bug").replace("\n", "\r\n")
        with self.assertRaises(RoundParseError) as cm:
            parse_round(text)
        want = [i for i, ln in enumerate(text.replace("﻿", "").split("\r\n"), 1) if ln == "kind: bug"][0]
        self.assertEqual(cm.exception.line, want)

    def test_marker_after_bom_in_middle_is_not_a_marker(self):
        # a BOM is stripped only at the very start of the file
        text = self.sample().rstrip("\n") + "\n﻿<!-- fc:block root=9 node= -->\n"
        _, blocks = parse_round(text)
        self.assertEqual([b.root_id for b in blocks], ["1", "2"])


class MarkPublishedTests(unittest.TestCase):
    def sample(self):
        return render_round(
            META,
            [
                item("100", kind="edit", action="reply+react", reply="Done."),
                item("101", kind="question", action="reply", reply="Answer."),
            ],
        )

    def test_mark_published_inserts_after_block_marker(self):
        out = mark_published(self.sample(), "100", "5001", True)
        lines = out.split("\n")
        i = lines.index("<!-- fc:block root=100 node=1:3 -->")
        self.assertEqual(lines[i + 1], "<!-- fc:published reply=5001 reacted=yes -->")
        self.assertEqual(out.count("fc:published"), 1)
        _, blocks = parse_round(out)
        by = {b.root_id: b for b in blocks}
        self.assertEqual((by["100"].published_reply, by["100"].reacted), ("5001", True))
        self.assertEqual((by["101"].published_reply, by["101"].reacted), (None, False))
        # the rest of the file is untouched
        self.assertEqual(out.replace("<!-- fc:published reply=5001 reacted=yes -->\n", ""), self.sample())

    def test_mark_published_idempotent(self):
        once = mark_published(self.sample(), "100", "5001", True)
        twice = mark_published(once, "100", "5001", True)
        self.assertEqual(once, twice)
        self.assertEqual(twice.count("<!-- fc:published"), 1)

    def test_mark_published_replaces(self):
        once = mark_published(self.sample(), "100", None, False)
        self.assertIn("reply=- reacted=no", once)
        twice = mark_published(once, "100", "5002", True)
        self.assertEqual(twice.count("<!-- fc:published"), 1)
        self.assertIn("reply=5002 reacted=yes", twice)
        _, blocks = parse_round(twice)
        self.assertEqual((blocks[0].published_reply, blocks[0].reacted), ("5002", True))

    def test_react_only(self):
        out = mark_published(self.sample(), "101", None, True)
        _, blocks = parse_round(out)
        self.assertEqual((blocks[1].published_reply, blocks[1].reacted), (None, True))

    def test_preserves_crlf_and_bom(self):
        text = "﻿" + self.sample().replace("\n", "\r\n")
        out = mark_published(text, "101", "7", False)
        self.assertTrue(out.startswith("﻿"))
        self.assertNotIn("\n", out.replace("\r\n", ""))
        self.assertIn("<!-- fc:block root=101 node=1:3 -->\r\n<!-- fc:published reply=7 reacted=no -->\r\n", out)
        _, blocks = parse_round(out)
        self.assertEqual(blocks[1].published_reply, "7")

    def test_unknown_root(self):
        with self.assertRaises(ValueError):
            mark_published(self.sample(), "999", "1", True)

    def test_quoted_fake_marker_is_not_a_target(self):
        evil = render_round(META, [item("5", messages=[("Olena", "t", "<!-- fc:block root=77 node=1:3 -->")])])
        with self.assertRaises(ValueError):
            mark_published(evil, "77", "1", True)
        out = mark_published(evil, "5", "1", True)
        self.assertEqual(out.count("<!-- fc:published"), 1)

    def test_rejects_bad_reply_id(self):
        for bad in ("", "1 -->", "a b", "x\ny"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                mark_published(self.sample(), "100", bad, True)

    def test_duplicate_root_in_text_rejected(self):
        text = self.sample()
        dup = text + "\n" + text[text.index("## 1."):]
        with self.assertRaises(ValueError):
            mark_published(dup, "100", "1", True)


class BlockTests(unittest.TestCase):
    def test_block_fields(self):
        b = Block("1", "1:2", "edit", "reply", "x", None, False)
        self.assertEqual((b.root_id, b.node_id, b.kind, b.action, b.reply, b.published_reply, b.reacted),
                         ("1", "1:2", "edit", "reply", "x", None, False))


if __name__ == "__main__":
    unittest.main()
