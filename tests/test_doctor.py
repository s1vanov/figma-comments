"""doctor, token set|status|clear and the token-expiry warning (no network, no real Keychain)."""
import contextlib
import getpass
import io
import os
import sys
import unittest
import warnings
from datetime import date
from unittest import mock

import tests  # noqa: F401
from tests.helpers import KEY
from tests.test_cli import CliBase

from fclib import cli
from fclib.doctor import Check, expiry_notice, run_doctor
from fclib.http import FigmaHTTPError, TransportError
from fclib.token_store import TokenInfo, TokenMissing

SECRET = "figd_SECRET-TOKEN-VALUE-0123456789"
TODAY = date(2026, 10, 13)
SCOPES = ["file_comments:read", "file_comments:write", "file_content:read", "current_user:read"]


def info(days=40, source="keychain"):
    expires = None if days is None else date.fromordinal(TODAY.toordinal() + days)
    return TokenInfo(SECRET, expires, source)


class FakeAPI:
    def __init__(self, me=None, comments=None, meta=None):
        self._me = me if me is not None else {"id": "u1", "handle": "Designer"}
        self._comments = comments if comments is not None else [{"id": "1"}]
        self._meta = meta if meta is not None else {"linkAccess": "inherit"}
        self.meta_calls = []

    @staticmethod
    def _give(value):
        if isinstance(value, Exception):
            raise value
        return value

    def me(self):
        return self._give(self._me)

    def comments(self, file_key):
        return self._give(self._comments)

    def file_meta(self, file_key, node_id=None):
        self.meta_calls.append((file_key, node_id))
        return self._give(self._meta)


def doctor(api=None, ref=None, token=None, replay=False):
    api = api or FakeAPI()

    def loader():
        if isinstance(token, Exception):
            raise token
        return token if token is not None else info()

    return run_doctor(ref, TODAY, lambda: api, token_loader=loader, replay=replay), api


def by_name(checks):
    return {c.name: c for c in checks}


def ref(node=None):
    return cli.parse_file_ref(KEY if node is None else "https://www.figma.com/design/%s/N?node-id=%s" % (KEY, node))


class TokenChecks(unittest.TestCase):
    def test_missing_token_names_the_set_command_and_stops(self):
        called = []
        loader = mock.Mock(side_effect=TokenMissing(
            "No Figma token found. Store one with: python3 scripts/figma_comments.py token set "
            "--expires YYYY-MM-DD\n(or set ...)"))
        checks = run_doctor(None, TODAY, lambda: called.append(1) or FakeAPI(), token_loader=loader)
        self.assertEqual(len(checks), 1)
        self.assertEqual(checks[0].name, "token")
        self.assertFalse(checks[0].ok)
        self.assertIn("token set", checks[0].action)
        self.assertIn("own terminal", checks[0].action)
        self.assertEqual(called, [])

    def test_found_token_shows_the_source_never_the_value(self):
        checks, _ = doctor()
        token = by_name(checks)["token"]
        self.assertTrue(token.ok)
        self.assertIn("keychain", token.detail)
        for c in checks:
            self.assertNotIn(SECRET, repr(c))

    def test_expiry_in_10_days_is_a_warning_with_the_number(self):
        checks, _ = doctor(token=info(10))
        expiry = by_name(checks)["expiry"]
        self.assertTrue(expiry.ok)
        self.assertTrue(expiry.warning)
        self.assertIn("10", expiry.action)
        self.assertIn("token set", expiry.action)

    def test_expiry_in_14_days_still_warns_15_does_not(self):
        self.assertTrue(by_name(doctor(token=info(14))[0])["expiry"].warning)
        c = by_name(doctor(token=info(15))[0])["expiry"]
        self.assertTrue(c.ok)
        self.assertFalse(c.warning)
        self.assertIn("15", c.detail)

    def test_expired_token_fails(self):
        c = by_name(doctor(token=info(-3))[0])["expiry"]
        self.assertFalse(c.ok)
        self.assertIn("expired", c.detail + c.action)
        self.assertIn("token set", c.action)

    def test_expires_today_is_not_yet_expired(self):
        c = by_name(doctor(token=info(0))[0])["expiry"]
        self.assertTrue(c.ok)
        self.assertTrue(c.warning)

    def test_unknown_expiry_is_a_warning(self):
        c = by_name(doctor(token=info(None))[0])["expiry"]
        self.assertTrue(c.ok)
        self.assertTrue(c.warning)
        self.assertIn("unknown", c.detail + c.action)

    def test_unknown_expiry_from_env_points_to_the_variable(self):
        c = by_name(doctor(token=info(None, "env"))[0])["expiry"]
        self.assertIn("FIGMA_TOKEN_EXPIRES", c.action)


class ApiChecks(unittest.TestCase):
    def test_me_403_lists_the_four_scopes(self):
        checks, _ = doctor(FakeAPI(me=FigmaHTTPError(403, "Invalid token")))
        me = by_name(checks)["api"]
        self.assertFalse(me.ok)
        self.assertIn("create a token with scopes " + ", ".join(SCOPES), me.action)
        self.assertNotIn(SECRET, repr(checks))

    def test_me_network_failure_is_reported(self):
        me = by_name(doctor(FakeAPI(me=TransportError("network unreachable")))[0])["api"]
        self.assertFalse(me.ok)
        self.assertIn("network", me.action)

    def test_me_other_http_error(self):
        me = by_name(doctor(FakeAPI(me=FigmaHTTPError(500, "boom")))[0])["api"]
        self.assertFalse(me.ok)
        self.assertIn("500", me.action)

    def test_without_a_ref_no_file_checks_run(self):
        checks, api = doctor()
        self.assertEqual([c.name for c in checks], ["token", "expiry", "api"])
        self.assertEqual(api.meta_calls, [])

    def test_with_a_ref_comments_and_link_access_are_checked(self):
        checks, _ = doctor(ref=ref())
        self.assertEqual([c.name for c in checks], ["token", "expiry", "api", "comments", "link access"])
        self.assertTrue(all(c.ok for c in checks))
        self.assertFalse(any(c.warning for c in checks))

    def test_comments_404_and_403_say_check_the_link(self):
        for status in (403, 404):
            checks, _ = doctor(FakeAPI(comments=FigmaHTTPError(status, "x")), ref=ref())
            c = by_name(checks)["comments"]
            self.assertFalse(c.ok)
            self.assertIn("check the link and that this account can open the file", c.action)

    def test_failed_comments_skip_the_link_access_check(self):
        checks, api = doctor(FakeAPI(comments=FigmaHTTPError(404, "x")), ref=ref())
        self.assertNotIn("link access", by_name(checks))
        self.assertEqual(api.meta_calls, [])

    def test_failed_me_still_checks_the_file(self):
        checks, _ = doctor(FakeAPI(me=FigmaHTTPError(403, "x")), ref=ref())
        self.assertIn("comments", by_name(checks))

    def test_link_access_uses_the_node_of_the_ref_or_the_first_page(self):
        _, api = doctor(ref=ref("1-2"))
        self.assertEqual(api.meta_calls, [(KEY, "1:2")])
        _, api = doctor(ref=ref())
        self.assertEqual(api.meta_calls, [(KEY, None)])

    def test_open_link_access_warns(self):
        for value in ("view", "edit"):
            c = by_name(doctor(FakeAPI(meta={"linkAccess": value}), ref=ref())[0])["link access"]
            self.assertTrue(c.ok)
            self.assertTrue(c.warning)
            self.assertIn("anyone with the link", c.action)
            self.assertIn(value, c.action)

    def test_org_link_access_warns_about_the_organization(self):
        c = by_name(doctor(FakeAPI(meta={"linkAccess": "org_view"}), ref=ref())[0])["link access"]
        self.assertTrue(c.warning)
        self.assertIn("organization", c.action)

    def test_restricted_link_access_is_quiet(self):
        for value in ("inherit", "restricted"):
            c = by_name(doctor(FakeAPI(meta={"linkAccess": value}), ref=ref())[0])["link access"]
            self.assertTrue(c.ok)
            self.assertFalse(c.warning)

    def test_unknown_link_access_value_warns_with_the_value(self):
        c = by_name(doctor(FakeAPI(meta={"linkAccess": "weird"}), ref=ref())[0])["link access"]
        self.assertTrue(c.warning)
        self.assertIn("unknown link access value 'weird'", c.action)

    def test_missing_link_access_field_warns(self):
        c = by_name(doctor(FakeAPI(meta={}), ref=ref())[0])["link access"]
        self.assertTrue(c.warning)

    def test_link_access_http_error_names_the_content_scope(self):
        c = by_name(doctor(FakeAPI(meta=FigmaHTTPError(403, "x")), ref=ref())[0])["link access"]
        self.assertFalse(c.ok)
        self.assertIn("file_content:read", c.action)

    def test_replay_mode_skips_the_token_lookup(self):
        loader = mock.Mock(side_effect=AssertionError("must not look up a token"))
        checks = run_doctor(None, TODAY, lambda: FakeAPI(), token_loader=loader, replay=True)
        self.assertTrue(all(c.ok for c in checks))
        self.assertIn("replay", by_name(checks)["token"].detail)
        self.assertNotIn("expiry", by_name(checks))

    def test_check_is_a_plain_record(self):
        c = Check("token", False, "do this")
        self.assertEqual((c.name, c.ok, c.action, c.warning, c.detail), ("token", False, "do this", False, ""))


class FileMeta(unittest.TestCase):
    """FigmaAPI.file_meta against the replay fixtures (the nodes fixture has linkAccess: view)."""

    def setUp(self):
        import tempfile
        from tests.helpers import api_for, make_replay
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.api, self.transport = api_for(make_replay(tmp.name), record=True)

    def test_returns_top_level_fields_without_nodes(self):
        meta = self.api.file_meta(KEY)
        self.assertEqual(meta["linkAccess"], "view")
        self.assertNotIn("nodes", meta)

    def test_asks_for_the_node_or_the_first_page_at_depth_1(self):
        self.api.file_meta(KEY)
        self.api.file_meta(KEY, "1:2")
        urls = [u for _, u in self.transport.requests]
        self.assertIn("ids=0%3A1", urls[0])
        self.assertIn("ids=1%3A2", urls[1])
        self.assertTrue(all("depth=1" in u and "/nodes" in u for u in urls))


class ExpiryNotice(unittest.TestCase):
    def test_none_beyond_14_days_or_unknown(self):
        self.assertIsNone(expiry_notice(info(15), TODAY))
        self.assertIsNone(expiry_notice(info(None), TODAY))

    def test_warning_text(self):
        self.assertEqual(expiry_notice(info(10), TODAY),
                         "warning: Figma token expires in 10 days (2026-10-23)")
        self.assertEqual(expiry_notice(info(14), TODAY),
                         "warning: Figma token expires in 14 days (2026-10-27)")
        self.assertEqual(expiry_notice(info(1), TODAY),
                         "warning: Figma token expires in 1 day (2026-10-14)")
        self.assertEqual(expiry_notice(info(0), TODAY),
                         "warning: Figma token expires today (2026-10-13)")

    def test_expired_is_a_warning_because_the_command_continues(self):
        text = expiry_notice(info(-2), TODAY)
        self.assertTrue(text.startswith(
            "warning: Figma token has expired (2026-10-11); requests will fail with 403"), text)
        self.assertIn("token set", text)

    def test_expiry_messages_say_run_token_set_again(self):
        for d in (10, 0, -2):
            text = expiry_notice(info(d), TODAY) or ""
            self.assertNotIn("--expires", text)
        self.assertIn("run `token set` again", expiry_notice(info(-2), TODAY))
        for d in (None, 10, -3):
            c = by_name(doctor(token=info(d))[0])["expiry"]
            self.assertIn("run `token set` again", c.action, d)
            self.assertNotIn("--expires", c.action)

    def test_never_contains_the_token(self):
        for d in (10, 0, -2):
            self.assertNotIn(SECRET, expiry_notice(info(d), TODAY))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

class DoctorCli(CliBase):
    def test_replay_doctor_with_a_ref(self):
        code, out, err = self.run_cli("doctor", KEY)
        self.assertEqual(code, 0)
        self.assertIn("not meaningful in replay mode", out)
        self.assertIn("link access", out)
        self.assertIn("anyone with the link", out)  # the fixture says linkAccess: view
        self.assertIn("FIGMA_COMMENTS_REPLAY is set", err)
        self.assertIn("passed", out)

    def test_replay_doctor_without_a_ref(self):
        code, out, err = self.run_cli("doctor")
        self.assertEqual(code, 0)
        self.assertNotIn("link access", out)

    def test_missing_token_exits_1_and_names_the_command(self):
        missing = TokenMissing("No Figma token found. Store one with: python3 scripts/figma_comments.py "
                               "token set --expires YYYY-MM-DD")
        with mock.patch.object(cli, "get_token", side_effect=missing):
            code, out, err = self.run_cli("doctor", replay=False)
        self.assertEqual(code, 1)
        self.assertIn("FAIL", out)
        self.assertIn("token set", out)

    def test_expiring_token_warns_but_exits_0(self):
        with mock.patch.object(cli, "get_token", return_value=info(10)), \
                mock.patch.object(cli, "_today", return_value=TODAY), \
                mock.patch.object(cli, "_make_api", return_value=FakeAPI()):
            code, out, err = self.run_cli("doctor", replay=False)
        self.assertEqual(code, 0)
        self.assertIn("10 days", out)
        self.assertIn("warn", out)
        self.assertNotIn(SECRET, out + err)

    def test_403_exits_1_and_lists_scopes_without_the_token(self):
        api = FakeAPI(me=FigmaHTTPError(403, "Invalid token"))
        with mock.patch.object(cli, "get_token", return_value=info()), \
                mock.patch.object(cli, "_today", return_value=TODAY), \
                mock.patch.object(cli, "_make_api", return_value=api):
            code, out, err = self.run_cli("doctor", replay=False)
        self.assertEqual(code, 1)
        for scope in SCOPES:
            self.assertIn(scope, out)
        self.assertNotIn(SECRET, out + err)

    def test_doctor_does_not_print_the_expiry_warning_twice(self):
        # doctor shows expiry as a check; the generic stderr notice would double it.
        failing = mock.Mock(request=mock.Mock(side_effect=TransportError("offline")))
        with mock.patch.object(cli, "get_token", return_value=info(10)), \
                mock.patch.object(cli, "_today", return_value=TODAY), \
                mock.patch.object(cli, "FallbackTransport", return_value=failing):
            code, out, err = self.run_cli("doctor", replay=False)
        self.assertEqual(code, 1)
        self.assertNotIn("warning: Figma token expires", err)
        self.assertNotIn(SECRET, out + err)


class ExpiryWarningInCommands(unittest.TestCase):
    def make(self, token_info):
        err = io.StringIO()
        with mock.patch.dict(os.environ, {}, clear=False), \
                mock.patch.object(cli, "get_token", return_value=token_info), \
                mock.patch.object(cli, "_today", return_value=TODAY), \
                contextlib.redirect_stderr(err):
            os.environ.pop("FIGMA_COMMENTS_REPLAY", None)
            api = cli._make_api()
        return api, err.getvalue()

    def test_warns_at_14_days_or_fewer(self):
        _, err = self.make(info(10))
        self.assertEqual(err, "warning: Figma token expires in 10 days (2026-10-23)\n")

    def test_quiet_when_far_or_unknown(self):
        self.assertEqual(self.make(info(40))[1], "")
        self.assertEqual(self.make(info(None))[1], "")

    def test_expired_prints_a_warning_and_carries_on(self):
        _, err = self.make(info(-1))
        self.assertTrue(err.startswith(
            "warning: Figma token has expired (2026-10-12); requests will fail with 403"), err)
        self.assertNotIn("error:", err)

    def test_the_token_is_never_printed(self):
        for d in (10, -1, 40):
            self.assertNotIn(SECRET, self.make(info(d))[1])


class TokenSetCli(CliBase):
    def run_set(self, *argv, tty=True, secret=SECRET, answers=("", ""), today=TODAY):
        prompts = []
        line_answers = list(answers)

        def prompt_line(text):
            prompts.append(text)
            return line_answers.pop(0)

        with mock.patch.object(cli, "_stdin_is_tty", return_value=tty), \
                mock.patch.object(cli, "_prompt_secret", return_value=secret) as sec, \
                mock.patch.object(cli, "_prompt_line", side_effect=prompt_line), \
                mock.patch.object(cli, "_today", return_value=today), \
                mock.patch.object(cli, "set_token", return_value="keychain") as store:
            code, out, err = self.run_cli("token", "set", *argv, replay=False)
        return code, out, err, store, sec, prompts

    def test_stores_with_the_given_expiry_and_hides_the_value(self):
        code, out, err, store, sec, prompts = self.run_set("--expires", "2026-12-31")
        self.assertEqual(code, 0)
        store.assert_called_once_with(SECRET, date(2026, 12, 31), allow_file=False)
        self.assertNotIn(SECRET, out + err + "".join(prompts))
        self.assertIn("keychain", out)
        self.assertIn("2026-12-31", out)
        self.assertEqual(prompts, [])  # expiry given: no question

    def test_allow_file_is_passed_on(self):
        _, _, _, store, _, _ = self.run_set("--expires", "2026-12-31", "--allow-file")
        store.assert_called_once_with(SECRET, date(2026, 12, 31), allow_file=True)

    def test_enter_enter_means_created_today_for_90_days(self):
        code, out, err, store, sec, prompts = self.run_set(answers=("", ""))
        self.assertEqual(code, 0, err)
        store.assert_called_once_with(SECRET, date(2027, 1, 11), allow_file=False)
        self.assertEqual(prompts, [
            "When did you create this token in Figma? YYYY-MM-DD, Enter = today, 'unknown' if you don't know: ",
            "Expiration you chose in Figma, in days (1, 7, 30 or 90; Enter = 90): ",
        ])
        self.assertIn("Expires 2027-01-11 (90 days left)", out)
        self.assertNotIn("Figma offers", out + err)
        self.assertNotIn(SECRET, out + err + "".join(prompts))

    def test_created_date_and_days_are_added(self):
        code, out, err, store, _, _ = self.run_set(answers=("2026-10-13", "90"), today=date(2026, 10, 14))
        self.assertEqual(code, 0, err)
        store.assert_called_once_with(SECRET, date(2027, 1, 11), allow_file=False)
        self.assertIn("Expires 2027-01-11 (89 days left)", out)

    def test_the_expiry_is_shown_before_the_token_is_asked_for(self):
        seen = []

        def secret(prompt):
            seen.append(sys.stdout.getvalue())  # run_cli redirects stdout to a StringIO
            return SECRET

        with mock.patch.object(cli, "_stdin_is_tty", return_value=True), \
                mock.patch.object(cli, "_prompt_secret", side_effect=secret), \
                mock.patch.object(cli, "_prompt_line", side_effect=["", "30"]), \
                mock.patch.object(cli, "_today", return_value=TODAY), \
                mock.patch.object(cli, "set_token", return_value="keychain"):
            code, out, err = self.run_cli("token", "set", replay=False)
        self.assertEqual(code, 0, err)
        self.assertIn("Expires 2026-11-12 (30 days left)", seen[0])
        self.assertEqual(out.count("Expires 2026-11-12"), 1)

    def test_a_computed_date_in_the_past_is_refused_before_the_token_prompt(self):
        code, out, err, store, sec, _ = self.run_set(answers=("2026-07-01", "30"))
        self.assertEqual(code, 1)
        self.assertIn("that token has already expired (2026-07-31); create a new one", err)
        store.assert_not_called()
        sec.assert_not_called()

    def test_expiring_today_is_still_accepted(self):
        code, out, err, store, _, _ = self.run_set(answers=("2026-10-06", "7"))
        self.assertEqual(code, 0, err)
        store.assert_called_once_with(SECRET, TODAY, allow_file=False)
        self.assertIn("(0 days left)", out)

    def test_a_creation_date_in_the_future_is_refused(self):
        code, out, err, store, sec, prompts = self.run_set(answers=("2026-10-20", "90"))
        self.assertEqual(code, 1)
        self.assertIn("2026-10-20", err)
        self.assertIn("future", err)
        self.assertEqual(len(prompts), 1)
        store.assert_not_called()
        sec.assert_not_called()
        code, out, err, store, sec, prompts = self.run_set("--created", "2026-10-20", "--days", "7")
        self.assertEqual(code, 1)
        self.assertIn("future", err)
        sec.assert_not_called()

    def test_flags_replace_the_questions(self):
        code, out, err, store, _, prompts = self.run_set("--created", "2026-10-01", "--days", "30")
        self.assertEqual(code, 0, err)
        self.assertEqual(prompts, [])
        store.assert_called_once_with(SECRET, date(2026, 10, 31), allow_file=False)
        self.assertIn("Expires 2026-10-31 (18 days left)", out)

    def test_a_missing_flag_is_asked_for(self):
        code, out, err, store, _, prompts = self.run_set("--created", "2026-10-01", answers=("",))
        self.assertEqual(code, 0, err)
        self.assertEqual(len(prompts), 1)
        self.assertIn("in days", prompts[0])
        store.assert_called_once_with(SECRET, date(2026, 12, 30), allow_file=False)
        code, out, err, store, _, prompts = self.run_set("--days", "7", answers=("",))
        self.assertEqual(code, 0, err)
        self.assertEqual(len(prompts), 1)
        self.assertIn("When did you create", prompts[0])
        store.assert_called_once_with(SECRET, date(2026, 10, 20), allow_file=False)

    def test_expires_with_created_or_days_is_a_usage_error(self):
        for argv in (("--expires", "2026-12-31", "--days", "30"),
                     ("--days", "30", "--expires", "2026-12-31"),
                     ("--expires", "2026-12-31", "--created", "2026-10-01"),
                     ("--created", "2026-10-01", "--expires", "2026-12-31")):
            code, out, err, store, sec, prompts = self.run_set(*argv)
            self.assertEqual(code, 2, argv)
            self.assertIn("not allowed with", err)
            store.assert_not_called()
            sec.assert_not_called()

    def test_unknown_creation_date_stores_an_unknown_expiry_with_a_warning(self):
        for answer in ("unknown", "UNKNOWN "):
            code, out, err, store, sec, prompts = self.run_set(answers=(answer,))
            self.assertEqual(code, 0, err)
            self.assertEqual(len(prompts), 1)
            store.assert_called_once_with(SECRET, None, allow_file=False)
            self.assertIn("expiry unknown", out + err)
            self.assertIn("run `token set` again", out)
            self.assertNotIn("--expires", out + err)
            self.assertNotIn(SECRET, out + err)

    def test_days_other_than_figma_choices_are_accepted_with_a_note(self):
        code, out, err, store, _, _ = self.run_set(answers=("", "14"))
        self.assertEqual(code, 0, err)
        store.assert_called_once_with(SECRET, date(2026, 10, 27), allow_file=False)
        self.assertIn("Figma offers 1, 7, 30 or 90 days; using 14", out)
        for days in ("1", "7", "30", "90"):
            out = self.run_set(answers=("", days))[1]
            self.assertNotIn("Figma offers", out)

    def test_days_must_be_a_whole_number_from_1_to_366(self):
        for answer in ("0", "367", "-5", "ten", "1.5"):
            code, out, err, store, sec, _ = self.run_set(answers=("", answer))
            self.assertEqual(code, 1, answer)
            self.assertIn("1 to 366", err)
            store.assert_not_called()
            sec.assert_not_called()
        code, out, err, store, _, _ = self.run_set(answers=("2025-10-13", "366"))
        self.assertEqual(code, 0, err)
        store.assert_called_once_with(SECRET, date(2026, 10, 14), allow_file=False)
        for argv in (("--days", "0"), ("--days", "400"), ("--days", "x"), ("--created", "13.10.2026")):
            code, out, err, store, _, _ = self.run_set(*argv)
            self.assertEqual(code, 2, argv)
            store.assert_not_called()

    def test_bad_creation_date_is_refused_before_the_token_prompt(self):
        code, out, err, store, sec, _ = self.run_set(answers=("next week",))
        self.assertEqual(code, 1)
        self.assertIn("YYYY-MM-DD", err)
        store.assert_not_called()
        sec.assert_not_called()

    def test_bad_expiry_flag_is_a_usage_error(self):
        code, out, err, store, sec, _ = self.run_set("--expires", "31/12/2026")
        self.assertEqual(code, 2)
        store.assert_not_called()

    def test_past_expiry_is_refused(self):
        code, out, err, store, _, _ = self.run_set("--expires", "2026-10-01")
        self.assertEqual(code, 1)
        self.assertIn("past", err)
        store.assert_not_called()

    def test_refuses_without_a_terminal(self):
        code, out, err, store, sec, _ = self.run_set("--expires", "2026-12-31", tty=False)
        self.assertEqual(code, 1)
        self.assertIn("run this in your own terminal", err)
        store.assert_not_called()
        sec.assert_not_called()

    def test_the_token_cannot_be_an_argument(self):
        code, out, err, store, _, _ = self.run_set(SECRET)
        self.assertEqual(code, 2)
        code, out, err, store, _, _ = self.run_set("--token", SECRET)
        self.assertEqual(code, 2)
        store.assert_not_called()

    def test_empty_token_is_refused(self):
        code, out, err, store, _, _ = self.run_set("--expires", "2026-12-31", secret="  ")
        self.assertEqual(code, 1)
        self.assertIn("no token", err.lower())
        store.assert_not_called()

    def test_store_refusal_is_reported_without_the_token(self):
        with mock.patch.object(cli, "_stdin_is_tty", return_value=True), \
                mock.patch.object(cli, "_prompt_secret", return_value=SECRET), \
                mock.patch.object(cli, "_today", return_value=TODAY), \
                mock.patch.object(cli, "set_token", side_effect=TokenMissing("No OS credential store ... --allow-file")):
            code, out, err = self.run_cli("token", "set", "--expires", "2026-12-31", replay=False)
        self.assertEqual(code, 1)
        self.assertIn("--allow-file", err)
        self.assertNotIn(SECRET, out + err)

    def test_unusable_token_value_is_not_echoed(self):
        with mock.patch.object(cli, "_stdin_is_tty", return_value=True), \
                mock.patch.object(cli, "_prompt_secret", return_value="figd_has space"), \
                mock.patch.object(cli, "_today", return_value=TODAY):
            code, out, err = self.run_cli("token", "set", "--expires", "2026-12-31", replay=False)
        self.assertEqual(code, 1)
        self.assertNotIn("figd_has", out + err)

    def test_env_token_is_mentioned_as_taking_precedence(self):
        with mock.patch.object(cli, "_stdin_is_tty", return_value=True), \
                mock.patch.object(cli, "_prompt_secret", return_value=SECRET), \
                mock.patch.object(cli, "_today", return_value=TODAY), \
                mock.patch.object(cli, "set_token", return_value="keychain"):
            code, out, err = self.run_cli("token", "set", "--expires", "2026-12-31",
                                          replay=False, env={"FIGMA_TOKEN": "x"})
        self.assertIn("FIGMA_TOKEN", out)
        self.assertIn("precedence", out)


class TokenSetPrompts(CliBase):
    """Prompt failures: no echo possible, Ctrl-D / Ctrl-C, no stdin at all."""

    def run_set(self, *patches):
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(cli, "_stdin_is_tty", return_value=True))
            stack.enter_context(mock.patch.object(cli, "_today", return_value=TODAY))
            store = stack.enter_context(mock.patch.object(cli, "set_token", return_value="keychain"))
            for patch in patches:
                stack.enter_context(patch)
            code, out, err = self.run_cli("token", "set", "--expires", "2026-12-31", replay=False)
        return code, out, err, store

    def test_getpass_warning_is_refused_not_echoed(self):
        def echoing_getpass(prompt="Password: ", stream=None):
            warnings.warn("Can not control echo on the terminal.", getpass.GetPassWarning, stacklevel=2)
            return SECRET

        code, out, err, store = self.run_set(mock.patch.object(cli.getpass, "getpass", side_effect=echoing_getpass))
        self.assertEqual(code, 1)
        self.assertIn("cannot read the token without echo; run this in a normal terminal", err)
        store.assert_not_called()
        self.assertNotIn(SECRET, out + err)

    def test_ctrl_d_or_ctrl_c_at_either_date_prompt_cancels(self):
        for answers in ([EOFError], ["", EOFError], [KeyboardInterrupt], ["2026-10-01", KeyboardInterrupt]):
            with mock.patch.object(cli, "_stdin_is_tty", return_value=True), \
                    mock.patch.object(cli, "_today", return_value=TODAY), \
                    mock.patch.object(cli, "_prompt_line", side_effect=answers), \
                    mock.patch.object(cli, "_prompt_secret", return_value=SECRET) as sec, \
                    mock.patch.object(cli, "set_token", return_value="keychain") as store:
                code, out, err = self.run_cli("token", "set", replay=False)
            self.assertEqual(code, 1, answers)
            self.assertIn("cancelled; nothing was stored", err)
            self.assertNotIn("unexpected failure", err)
            store.assert_not_called()
            sec.assert_not_called()

    def test_ctrl_c_and_ctrl_d_at_the_token_prompt_cancel(self):
        for exc in (KeyboardInterrupt, EOFError):
            code, out, err, store = self.run_set(mock.patch.object(cli, "_prompt_secret", side_effect=exc))
            self.assertEqual(code, 1, exc)
            self.assertIn("cancelled; nothing was stored", err)
            store.assert_not_called()

    def test_no_stdin_counts_as_no_terminal(self):
        with mock.patch.object(sys, "stdin", None):
            self.assertFalse(cli._stdin_is_tty())
            with mock.patch.object(cli, "set_token", return_value="keychain") as store:
                code, out, err = self.run_cli("token", "set", "--expires", "2026-12-31", replay=False)
        self.assertEqual(code, 1)
        self.assertIn("run this in your own terminal", err)
        store.assert_not_called()


class TokenSetRealScript(unittest.TestCase):
    def test_piped_token_is_refused_before_anything_is_stored(self):
        import subprocess
        import sys
        import tempfile
        from pathlib import Path
        script = Path(__file__).resolve().parent.parent / "scripts" / "figma_comments.py"
        with tempfile.TemporaryDirectory() as home:
            env = {k: v for k, v in os.environ.items() if not k.startswith("FIGMA")}
            env["HOME"] = home
            proc = subprocess.run([sys.executable, str(script), "token", "set", "--expires", "2099-01-01"],
                                  input=SECRET + "\n", capture_output=True, text=True, env=env, timeout=60)
            self.assertEqual(proc.returncode, 1)
            self.assertIn("run this in your own terminal", proc.stderr)
            self.assertNotIn(SECRET, proc.stdout + proc.stderr)
            self.assertEqual(list(Path(home).rglob("*")), [])


class TokenStatusCli(CliBase):
    def status(self, token_info=None, error=None):
        kw = {"side_effect": error} if error else {"return_value": token_info}
        with mock.patch.object(cli, "get_token", **kw), mock.patch.object(cli, "_today", return_value=TODAY):
            return self.run_cli("token", "status", replay=False)

    def test_source_and_days_without_the_value(self):
        code, out, err = self.status(info(40, "keychain"))
        self.assertEqual(code, 0)
        self.assertIn("keychain", out)
        self.assertIn("40 days left", out)
        self.assertIn("2026-11-22", out)
        self.assertNotIn(SECRET, out + err)

    def test_expiring_and_expired_and_unknown(self):
        self.assertIn("warning", self.status(info(5))[1])
        self.assertIn("EXPIRED", self.status(info(-2))[1])
        self.assertIn("unknown", self.status(info(None))[1])
        for d in (5, -2):
            out = self.status(info(d))[1]
            self.assertIn("run `token set` again", out)
            self.assertNotIn("--expires", out)

    def test_opt_in_keychain_item_is_explained(self):
        code, out, err = self.status(info(None, "keychain:figma-pat"))
        self.assertEqual(code, 0)
        self.assertIn("'figma-pat'", out)
        self.assertIn("FIGMA_TOKEN_KEYCHAIN_SERVICE", out)
        self.assertIn("token clear", out)
        self.assertIn("does not remove", out)

    def test_env_source_is_explained(self):
        out = self.status(info(10, "env"))[1]
        self.assertIn("FIGMA_TOKEN", out)

    def test_no_token_exits_1(self):
        code, out, err = self.status(error=TokenMissing("No Figma token found. Store one with: ... token set ..."))
        self.assertEqual(code, 1)
        self.assertIn("token set", err)

    def test_replay_mode_is_stated_not_looked_up(self):
        code, out, err = self.run_cli("token", "status")
        self.assertEqual(code, 0)
        self.assertIn("eplay", out)


class TokenClearCli(CliBase):
    def clear(self, remaining=None):
        kw = {"side_effect": TokenMissing("none")} if remaining is None else {"return_value": remaining}
        with mock.patch.object(cli, "clear_token") as cleared, mock.patch.object(cli, "get_token", **kw):
            code, out, err = self.run_cli("token", "clear", replay=False)
        return code, out, err, cleared

    def test_nothing_remains(self):
        code, out, err, cleared = self.clear(None)
        self.assertEqual(code, 0)
        cleared.assert_called_once_with()
        self.assertIn("no token", out.lower())

    def test_env_var_remains(self):
        code, out, err, _ = self.clear(info(10, "env"))
        self.assertEqual(code, 0)
        self.assertIn("FIGMA_TOKEN", out)
        self.assertNotIn(SECRET, out + err)

    def test_opt_in_item_remains(self):
        code, out, err, _ = self.clear(info(None, "keychain:figma-pat"))
        self.assertIn("'figma-pat'", out)
        self.assertIn("unset FIGMA_TOKEN_KEYCHAIN_SERVICE", out)
        self.assertIn("Keychain Access", out)
        self.assertNotIn(SECRET, out + err)


if __name__ == "__main__":
    unittest.main()
