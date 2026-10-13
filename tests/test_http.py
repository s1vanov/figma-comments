import http.server
import json
import os
import ssl
import subprocess
import sys
import threading
import unittest
import urllib.error
from unittest import mock

# `discover -s tests` imports test modules as top-level, so tests/__init__.py
# does not run; make scripts/ importable here too.
sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
)

from fclib.http import (  # noqa: E402
    Client,
    CurlTransport,
    FallbackTransport,
    FigmaHTTPError,
    Response,
    TransportError,
    UrllibTransport,
)

TOKEN = "figd_FAKE_TOKEN_123"


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, headers, body):
        self.calls.append((method, url, dict(headers), body))
        return self.responses.pop(0)


def resp(status=200, body=b"{}", headers=None):
    return Response(status, headers or {}, body)


class ClientTests(unittest.TestCase):
    def make(self, responses, **kw):
        t = FakeTransport(responses)
        sleeps = []
        c = Client(TOKEN, t, sleep=sleeps.append, **kw)
        return c, t, sleeps

    def test_retry_after_honoured(self):
        c, t, sleeps = self.make([resp(429, b"", {"Retry-After": "7"}), resp(200, b"{}")])
        self.assertEqual(c.call("GET", "/v1/me"), {})
        self.assertEqual(sleeps, [7])
        self.assertEqual(len(t.calls), 2)

    def test_retry_after_header_case_insensitive(self):
        c, _, sleeps = self.make([resp(429, b"", {"retry-after": "3"}), resp(200)])
        c.call("GET", "/v1/me")
        self.assertEqual(sleeps, [3])

    def test_429_without_header_waits_30(self):
        c, _, sleeps = self.make([resp(429), resp(200)])
        c.call("GET", "/v1/me")
        self.assertEqual(sleeps, [30])

    def test_non_numeric_retry_after_waits_30(self):
        c, _, sleeps = self.make([resp(429, b"", {"Retry-After": "soon"}), resp(200)])
        c.call("GET", "/v1/me")
        self.assertEqual(sleeps, [30])

    def test_negative_retry_after_waits_30(self):
        c, _, sleeps = self.make([resp(429, b"", {"Retry-After": "-5"}), resp(200)])
        c.call("GET", "/v1/me")
        self.assertEqual(sleeps, [30])

    def test_retry_after_300_is_still_slept(self):
        c, _, sleeps = self.make([resp(429, b"", {"Retry-After": "300"}), resp(200)])
        c.call("GET", "/v1/me")
        self.assertEqual(sleeps, [300])

    def test_retry_after_over_300_raises_without_sleep(self):
        c, t, sleeps = self.make([resp(429, b"", {"Retry-After": "301"}), resp(200)])
        with self.assertRaises(FigmaHTTPError) as cm:
            c.call("GET", "/v1/me")
        self.assertEqual(cm.exception.status, 429)
        self.assertEqual(
            cm.exception.message,
            "rate limited: Figma asks to wait 301 s; try again later",
        )
        self.assertEqual(sleeps, [])
        self.assertEqual(len(t.calls), 1)

    def test_token_trailing_whitespace_stripped(self):
        t = FakeTransport([resp(200)])
        Client("abc\n", t).call("GET", "/x")
        self.assertEqual(t.calls[0][2]["X-Figma-Token"], "abc")

    def test_token_trailing_newline_works_with_urllib_transport(self):
        t = UrllibTransport()
        fake = mock.MagicMock()
        fake.__enter__.return_value = fake
        fake.status = 200
        fake.read.return_value = b"{}"
        fake.headers = {}
        with mock.patch.object(t._opener, "open", return_value=fake) as op:
            Client("abc\n", t).call("GET", "/x")
        self.assertEqual(op.call_args[0][0].get_header("X-figma-token"), "abc")

    def test_token_with_control_chars_rejected_without_echo(self):
        for bad in ("ab\ncd", "ab\x00cd", "ab\x7fcd", "ab\tcd"):
            with self.assertRaises(ValueError) as cm:
                Client(bad, FakeTransport([]))
            self.assertEqual(str(cm.exception), "invalid token: contains control characters")
            self.assertNotIn("ab", repr(cm.exception.args))

    def test_transport_exception_redacted(self):
        class Boom:
            def request(self, *a):
                raise ValueError("Invalid header value b'%s'" % TOKEN)

        c = Client(TOKEN, Boom())
        with self.assertRaises(Exception) as cm:
            c.call("GET", "/x")
        self.assertNotIn(TOKEN, str(cm.exception))
        self.assertNotIn(TOKEN, repr(cm.exception))
        self.assertIn("***", str(cm.exception))
        self.assertIsNone(cm.exception.__cause__)
        self.assertTrue(cm.exception.__suppress_context__)

    def test_any_transport_exception_carrying_the_token_is_redacted(self):
        import http.client

        for exc in (RuntimeError("boom %s" % TOKEN), http.client.InvalidURL("bad %s" % TOKEN),
                    KeyError(TOKEN), UnicodeEncodeError("latin-1", TOKEN, 0, 1, "no")):
            with self.assertRaises(TransportError, msg=type(exc).__name__) as cm:
                Client(TOKEN, Raiser(exc)).call("GET", "/x")
            self.assertNotIn(TOKEN, str(cm.exception))
            self.assertNotIn(TOKEN, repr(cm.exception))
            self.assertIsNone(cm.exception.__cause__)
            self.assertTrue(cm.exception.__suppress_context__)

    def test_total_retry_after_sleep_is_capped_at_600_s_per_client(self):
        c, t, sleeps = self.make([resp(429, b"", {"Retry-After": "300"}), resp(200),
                                  resp(429, b"", {"Retry-After": "300"}), resp(200),
                                  resp(429, b"", {"Retry-After": "1"}), resp(200)])
        c.call("GET", "/v1/me")
        c.call("GET", "/v1/me")  # 600 s slept in all: still allowed
        with self.assertRaises(FigmaHTTPError) as cm:
            c.call("GET", "/v1/me")  # one more second would pass the budget
        self.assertEqual(cm.exception.status, 429)
        self.assertEqual(cm.exception.message, "rate limited for too long; try again later")
        self.assertEqual(sleeps, [300, 300])
        self.assertEqual(len(t.calls), 5)

    def test_transport_exception_without_token_unchanged(self):
        err = TransportError("curl exited with 6: Could not resolve host")

        class Boom:
            def request(self, *a):
                raise err

        with self.assertRaises(TransportError) as cm:
            Client(TOKEN, Boom()).call("GET", "/x")
        self.assertIs(cm.exception, err)

    def test_gives_up_after_max_retries(self):
        c, t, _ = self.make([resp(429)] * 6)
        with self.assertRaises(FigmaHTTPError) as cm:
            c.call("GET", "/v1/me")
        self.assertEqual(cm.exception.status, 429)
        self.assertEqual(len(t.calls), 6)

    def test_403_no_retry(self):
        c, t, sleeps = self.make([resp(403, b'{"err": "Forbidden"}')])
        with self.assertRaises(FigmaHTTPError) as cm:
            c.call("GET", "/v1/me")
        self.assertEqual(cm.exception.status, 403)
        self.assertEqual(cm.exception.message, "Forbidden")
        self.assertEqual(len(t.calls), 1)
        self.assertEqual(sleeps, [])

    def test_500_no_retry(self):
        c, t, _ = self.make([resp(500, b"boom")])
        with self.assertRaises(FigmaHTTPError) as cm:
            c.call("GET", "/v1/me")
        self.assertEqual(cm.exception.status, 500)
        self.assertEqual(cm.exception.message, "boom")
        self.assertEqual(len(t.calls), 1)

    def test_token_never_in_error(self):
        body = json.dumps({"message": "bad token %s here" % TOKEN}).encode()
        c, _, _ = self.make([resp(401, body)])
        with self.assertRaises(FigmaHTTPError) as cm:
            c.call("GET", "/v1/me")
        self.assertNotIn(TOKEN, str(cm.exception))
        self.assertNotIn(TOKEN, repr(cm.exception))
        self.assertIn("***", cm.exception.message)

    def test_token_redacted_in_plain_body_and_truncated(self):
        body = (TOKEN + "x" * 500).encode()
        c, _, _ = self.make([resp(400, body)])
        with self.assertRaises(FigmaHTTPError) as cm:
            c.call("GET", "/v1/me")
        self.assertNotIn(TOKEN, repr(cm.exception))
        self.assertLessEqual(len(cm.exception.message), 200)

    def test_token_sent_in_header_only(self):
        c, t, _ = self.make([resp(200)])
        c.call("GET", "/v1/me")
        method, url, headers, body = t.calls[0]
        self.assertEqual(headers["X-Figma-Token"], TOKEN)
        self.assertNotIn(TOKEN, url)

    def test_url_and_query(self):
        c, t, _ = self.make([resp(200)])
        c.call("GET", "/v1/files/K/comments", query={"as_md": "true", "x": "a b"})
        self.assertEqual(
            t.calls[0][1], "https://api.figma.com/v1/files/K/comments?as_md=true&x=a+b"
        )
        self.assertIsNone(t.calls[0][3])
        self.assertNotIn("Content-Type", t.calls[0][2])

    def test_no_query_no_question_mark(self):
        c, t, _ = self.make([resp(200)])
        c.call("GET", "/v1/me")
        self.assertEqual(t.calls[0][1], "https://api.figma.com/v1/me")

    def test_json_body_utf8(self):
        c, t, _ = self.make([resp(200)])
        c.call("POST", "/v1/files/K/comments", body={"message": "Привіт"})
        _, _, headers, body = t.calls[0]
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual(body, '{"message": "Привіт"}'.encode("utf-8"))

    def test_empty_dict_body_is_sent(self):
        c, t, _ = self.make([resp(200)])
        c.call("POST", "/x", body={})
        self.assertEqual(t.calls[0][3], b"{}")

    def test_empty_success_body_returns_empty_dict(self):
        c, _, _ = self.make([resp(200, b"")])
        self.assertEqual(c.call("GET", "/x"), {})

    def test_json_success_body(self):
        c, _, _ = self.make([resp(200, b'{"a": [1, 2]}')])
        self.assertEqual(c.call("GET", "/x"), {"a": [1, 2]})

    def test_non_json_success_body_returns_text(self):
        c, _, _ = self.make([resp(200, "привіт".encode("utf-8"))])
        self.assertEqual(c.call("GET", "/x"), "привіт")


class CurlTransportTests(unittest.TestCase):
    def fake_run(self, captured, status="200", body=b'{"ok": true}', resp_headers=""):
        def run(args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs
            if "--data-binary" in args:
                path = args[args.index("--data-binary") + 1]
                self.assertTrue(path.startswith("@"))
                captured["body_path"] = path[1:]
                with open(path[1:], "rb") as fh:
                    captured["body_content"] = fh.read()
            dpath = args[args.index("-D") + 1]
            with open(dpath, "w") as fh:
                fh.write(resp_headers)
            return subprocess.CompletedProcess(
                args, 0, stdout=body + b"\n" + status.encode(), stderr=b""
            )

        return run

    def test_curl_token_not_in_argv(self):
        captured = {}
        t = CurlTransport()
        with mock.patch(
            "fclib.http.subprocess.run", side_effect=self.fake_run(captured)
        ):
            r = t.request(
                "POST",
                "https://api.figma.com/v1/x",
                {"X-Figma-Token": TOKEN, "Content-Type": "application/json"},
                b'{"a": 1}',
            )
        args = captured["args"]
        self.assertFalse(any(TOKEN in str(a) for a in args))
        self.assertIn(TOKEN, captured["kwargs"]["input"].decode())
        self.assertIn("-H", args)
        self.assertEqual(args[args.index("-H") + 1], "@-")
        self.assertEqual(captured["body_content"], b'{"a": 1}')
        self.assertEqual(r.status, 200)
        self.assertEqual(r.body, b'{"ok": true}')

    def test_curl_argv_ignores_curlrc_and_allows_only_https_without_redirects(self):
        captured = {}
        with mock.patch("fclib.http.shutil.which", return_value="/opt/bin/curl") as which:
            t = CurlTransport()
        with mock.patch("fclib.http.subprocess.run", side_effect=self.fake_run(captured)):
            t.request("GET", "https://api.figma.com/v1/me", {"A": "b"}, None)
            t.request("GET", "https://api.figma.com/v1/me", {"A": "b"}, None)
        which.assert_called_once_with("curl")  # resolved once, at construction
        args = captured["args"]
        self.assertEqual(args[0], "/opt/bin/curl")
        self.assertEqual(args[1], "-q")  # must be the first option, or ~/.curlrc is read
        joined = " ".join(args)
        for flag in ("--proto =https", "--proto-redir =https", "--max-redirs 0", "--tlsv1.2"):
            self.assertIn(flag, joined)

    def test_curl_not_found_raises_transport_error(self):
        with mock.patch("fclib.http.shutil.which", return_value=None):
            t = CurlTransport()
        with mock.patch("fclib.http.subprocess.run") as run:
            with self.assertRaises(TransportError) as cm:
                t.request("GET", "https://api.figma.com/v1/me", {"X-Figma-Token": TOKEN}, None)
        run.assert_not_called()
        self.assertIn("curl", str(cm.exception))
        self.assertNotIn(TOKEN, str(cm.exception))

    def test_curl_body_tempfile_deleted(self):
        captured = {}
        with mock.patch(
            "fclib.http.subprocess.run", side_effect=self.fake_run(captured)
        ):
            CurlTransport().request("POST", "https://h/x", {"A": "b"}, b"{}")
        self.assertFalse(os.path.exists(captured["body_path"]))

    def test_curl_no_body_no_data_flag(self):
        captured = {}
        with mock.patch(
            "fclib.http.subprocess.run", side_effect=self.fake_run(captured)
        ):
            CurlTransport().request("GET", "https://h/x", {"A": "b"}, None)
        self.assertNotIn("--data-binary", captured["args"])

    def test_curl_parses_status_and_retry_after(self):
        captured = {}
        hdrs = "HTTP/2 429 \r\nretry-after: 12\r\ncontent-type: application/json\r\n\r\n"
        with mock.patch(
            "fclib.http.subprocess.run",
            side_effect=self.fake_run(captured, status="429", body=b"", resp_headers=hdrs),
        ):
            r = CurlTransport().request("GET", "https://h/x", {"A": "b"}, None)
        self.assertEqual(r.status, 429)
        self.assertEqual(
            {k.lower(): v for k, v in r.headers.items()}["retry-after"], "12"
        )

    def test_curl_failure_raises_without_token(self):
        def run(args, **kwargs):
            return subprocess.CompletedProcess(
                args, 6, stdout=b"", stderr=b"Could not resolve host"
            )

        with mock.patch("fclib.http.subprocess.run", side_effect=run):
            with self.assertRaises(Exception) as cm:
                CurlTransport().request(
                    "GET", "https://h/x", {"X-Figma-Token": TOKEN}, None
                )
        self.assertNotIn(TOKEN, str(cm.exception))


class Raiser:
    def __init__(self, exc):
        self.exc = exc

    def request(self, *a):
        raise self.exc


class FallbackTests(unittest.TestCase):
    def test_fallback_only_on_cert_error(self):
        primary = Raiser(ssl.SSLCertVerificationError("bad cert"))
        secondary = FakeTransport([resp(200)])
        r = FallbackTransport(primary, secondary).request("GET", "u", {}, None)
        self.assertEqual(r.status, 200)
        self.assertEqual(len(secondary.calls), 1)

    def test_no_fallback_on_url_error(self):
        primary = Raiser(urllib.error.URLError("offline"))
        secondary = FakeTransport([resp(200)])
        with self.assertRaises(urllib.error.URLError):
            FallbackTransport(primary, secondary).request("GET", "u", {}, None)
        self.assertEqual(secondary.calls, [])

    def test_primary_success_skips_secondary(self):
        primary = FakeTransport([resp(200)])
        secondary = FakeTransport([])
        FallbackTransport(primary, secondary).request("GET", "u", {}, None)
        self.assertEqual(secondary.calls, [])


class UrllibTransportTests(unittest.TestCase):
    def test_http_error_becomes_response(self):
        err = urllib.error.HTTPError("u", 429, "Too Many", {"Retry-After": "5"}, None)
        err.read = lambda: b"slow down"
        t = UrllibTransport()
        with mock.patch.object(t._opener, "open", side_effect=err):
            r = t.request("GET", "https://h/x", {}, None)
        self.assertEqual(r.status, 429)
        self.assertEqual(r.body, b"slow down")
        self.assertEqual(r.headers["Retry-After"], "5")

    def test_cert_error_unwrapped_from_url_error(self):
        cert = ssl.SSLCertVerificationError("bad")
        t = UrllibTransport()
        with mock.patch.object(
            t._opener, "open", side_effect=urllib.error.URLError(cert)
        ):
            with self.assertRaises(ssl.SSLCertVerificationError):
                t.request("GET", "https://h/x", {}, None)

    def test_other_url_error_propagates(self):
        t = UrllibTransport()
        with mock.patch.object(
            t._opener, "open", side_effect=urllib.error.URLError("offline")
        ):
            with self.assertRaises(urllib.error.URLError):
                t.request("GET", "https://h/x", {}, None)

    def test_success_passes_timeout_and_body(self):
        fake = mock.MagicMock()
        fake.__enter__.return_value = fake
        fake.status = 200
        fake.read.return_value = b"{}"
        fake.headers = {"A": "b"}
        t = UrllibTransport()
        with mock.patch.object(t._opener, "open", return_value=fake) as op:
            r = t.request("POST", "https://h/x", {"K": "v"}, b"{}")
        req = op.call_args[0][0]
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(req.data, b"{}")
        self.assertEqual(op.call_args[1]["timeout"], 30)
        self.assertEqual(r.status, 200)

    def test_redirect_is_not_followed(self):
        hits = {"target": 0, "origin": 0}

        class Target(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                hits["target"] += 1
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *a):
                pass

        target = http.server.HTTPServer(("127.0.0.1", 0), Target)
        location = "http://127.0.0.1:%d/stolen" % target.server_address[1]

        class Origin(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                hits["origin"] += 1
                hits["token"] = self.headers.get("X-Figma-Token")
                self.send_response(302)
                self.send_header("Location", location)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *a):
                pass

        origin = http.server.HTTPServer(("127.0.0.1", 0), Origin)
        for srv in (target, origin):
            th = threading.Thread(target=srv.serve_forever, daemon=True)
            th.start()
        try:
            r = UrllibTransport().request(
                "GET",
                "http://127.0.0.1:%d/x" % origin.server_address[1],
                {"X-Figma-Token": TOKEN},
                None,
            )
        finally:
            for srv in (target, origin):
                srv.shutdown()
                srv.server_close()
        self.assertEqual(r.status, 302)
        self.assertEqual(hits["origin"], 1)
        self.assertEqual(hits["target"], 0)


if __name__ == "__main__":
    unittest.main()
