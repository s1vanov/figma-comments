"""HTTP transport layer and Figma REST client (stdlib only, Python 3.9+).

The access token is only ever placed in the ``X-Figma-Token`` request header.
It never goes into a URL, a process argument list, an exception message or a
log line.
"""
from __future__ import annotations

import json
import os
import shutil
import ssl
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional, Protocol

BASE_URL = "https://api.figma.com"
TIMEOUT = 30
DEFAULT_RETRY_WAIT = 30
MAX_RETRY_WAIT = 300  # never block an agent shell longer than this
MAX_TOTAL_RETRY_WAIT = 600  # all Retry-After sleeping of one Client (one command run)
TOKEN_HEADER = "X-Figma-Token"
ERROR_MESSAGE_LIMIT = 200


@dataclass
class Response:
    status: int
    headers: Dict[str, str] = field(default_factory=dict)
    body: bytes = b""


class Transport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        headers: Dict[str, str],
        body: Optional[bytes],
    ) -> Response:
        ...


class TransportError(Exception):
    """Transport-level failure (never carries request headers or the token)."""


class FigmaHTTPError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(status, message)
        self.status = status
        self.message = message

    def __str__(self) -> str:
        return "HTTP %d: %s" % (self.status, self.message)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow redirects, so the token is not forwarded to another host.
    Returning None makes urllib surface the 3xx as an HTTPError."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class UrllibTransport:
    def __init__(self) -> None:
        self._opener = urllib.request.build_opener(_NoRedirect)

    def request(
        self,
        method: str,
        url: str,
        headers: Dict[str, str],
        body: Optional[bytes],
    ) -> Response:
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with self._opener.open(req, timeout=TIMEOUT) as r:
                return Response(r.status, dict(r.headers.items()), r.read())
        except urllib.error.HTTPError as e:
            return Response(e.code, dict(e.headers.items()) if e.headers else {}, e.read())
        except urllib.error.URLError as e:
            if isinstance(e.reason, ssl.SSLCertVerificationError):
                raise e.reason from e
            raise


# ``-q`` must be the first argument (curl reads ~/.curlrc otherwise, which could add a
# proxy, a trace file or a redirect policy); then HTTPS only, no redirects (the token must
# never be sent to another host), TLS 1.2 or newer.
CURL_SAFETY_ARGS = ("-q", "--proto", "=https", "--proto-redir", "=https", "--max-redirs", "0", "--tlsv1.2")


class CurlTransport:
    """Shells out to curl. Headers go via stdin (``-H @-``), the body via a
    temp file, so the token never appears in argv. Requires curl >= 7.55
    (``-H @-``). The curl executable is resolved once, with ``shutil.which``."""

    def __init__(self, curl: str = "curl") -> None:
        self.curl = shutil.which(curl)

    def request(
        self,
        method: str,
        url: str,
        headers: Dict[str, str],
        body: Optional[bytes],
    ) -> Response:
        if self.curl is None:
            raise TransportError("curl was not found on PATH (needed when Python cannot verify the TLS certificate)")
        hdr_fd, hdr_path = tempfile.mkstemp(prefix="fc-hdr-")
        os.close(hdr_fd)
        body_path = None  # type: Optional[str]
        try:
            args = [
                self.curl,
                *CURL_SAFETY_ARGS,
                "-sS",
                "--max-time",
                str(TIMEOUT),
                "-X",
                method,
                "-H",
                "@-",
                "-D",
                hdr_path,
                "-w",
                "\n%{http_code}",
            ]
            if body is not None:
                body_fd, body_path = tempfile.mkstemp(prefix="fc-body-")
                with os.fdopen(body_fd, "wb") as fh:
                    fh.write(body)
                args += ["--data-binary", "@" + body_path]
            args.append(url)
            stdin = "".join("%s: %s\n" % (k, v) for k, v in headers.items()).encode("utf-8")
            proc = subprocess.run(
                args, input=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
            if proc.returncode != 0:
                msg = proc.stderr.decode("utf-8", "replace").strip()[:ERROR_MESSAGE_LIMIT]
                raise TransportError("curl exited with %d: %s" % (proc.returncode, msg))
            payload, _, code = proc.stdout.rpartition(b"\n")
            try:
                status = int(code.strip())
            except ValueError:
                raise TransportError("curl returned no HTTP status") from None
            with open(hdr_path, "r", encoding="utf-8", errors="replace") as fh:
                raw_headers = fh.read()
            return Response(status, _parse_header_block(raw_headers), payload)
        finally:
            for p in (hdr_path, body_path):
                if p:
                    try:
                        os.unlink(p)
                    except OSError:
                        pass


def _parse_header_block(raw: str) -> Dict[str, str]:
    """Parse the last header block of a ``curl -D`` dump (earlier blocks are
    redirects or 100-continue)."""
    blocks = [b for b in raw.replace("\r\n", "\n").split("\n\n") if b.strip()]
    if not blocks:
        return {}
    result = {}  # type: Dict[str, str]
    for line in blocks[-1].split("\n")[1:]:
        name, sep, value = line.partition(":")
        if sep:
            result[name.strip()] = value.strip()
    return result


class FallbackTransport:
    """Use ``primary``; switch to ``secondary`` only on a TLS certificate
    verification error."""

    def __init__(self, primary: Transport, secondary: Transport) -> None:
        self.primary = primary
        self.secondary = secondary

    def request(
        self,
        method: str,
        url: str,
        headers: Dict[str, str],
        body: Optional[bytes],
    ) -> Response:
        try:
            return self.primary.request(method, url, headers, body)
        except ssl.SSLCertVerificationError:
            return self.secondary.request(method, url, headers, body)


class Client:
    def __init__(
        self,
        token: str,
        transport: Transport,
        sleep: Callable[[float], Any] = time.sleep,
        max_retries: int = 5,
    ) -> None:
        token = token.strip()
        if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in token):
            raise ValueError("invalid token: contains control characters")
        self._token = token
        self.transport = transport
        self.sleep = sleep
        self.max_retries = max_retries
        self._slept = 0  # seconds of Retry-After waiting so far, across every call

    def __repr__(self) -> str:
        return "Client(transport=%r)" % (self.transport,)

    def call(
        self,
        method: str,
        path: str,
        body: Optional[dict] = None,
        query: Optional[dict] = None,
    ) -> Any:
        url = BASE_URL + path
        if query:
            url += "?" + urllib.parse.urlencode(
                {k: v for k, v in query.items() if v is not None}
            )
        headers = {TOKEN_HEADER: self._token, "Accept": "application/json"}
        data = None  # type: Optional[bytes]
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"

        attempt = 0
        while True:
            try:
                resp = self.transport.request(method, url, headers, data)
            except Exception as e:  # any type: a message that carries the token is redacted
                if self._token and (
                    self._token in str(e) or self._token in repr(e)
                ):
                    # `from None`: do not chain the original, it holds the token.
                    raise TransportError(self._redact(str(e))) from None
                raise
            if 200 <= resp.status < 300:
                return _decode_success(resp.body)
            if resp.status == 429 and attempt < self.max_retries:
                wait = _retry_wait(resp.headers)
                if wait > MAX_RETRY_WAIT:
                    raise FigmaHTTPError(
                        429,
                        "rate limited: Figma asks to wait %d s; try again later" % wait,
                    )
                if self._slept + wait > MAX_TOTAL_RETRY_WAIT:
                    raise FigmaHTTPError(429, "rate limited for too long; try again later")
                attempt += 1
                self._slept += wait
                self.sleep(wait)
                continue
            raise FigmaHTTPError(resp.status, self._error_message(resp.body))

    def _error_message(self, raw: bytes) -> str:
        text = raw.decode("utf-8", "replace")
        message = None  # type: Optional[str]
        try:
            parsed = json.loads(text)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            for key in ("message", "err"):
                value = parsed.get(key)
                if isinstance(value, str) and value:
                    message = value
                    break
        if message is None:
            message = text
        return self._redact(message)[:ERROR_MESSAGE_LIMIT]

    def _redact(self, text: str) -> str:
        if self._token:
            text = text.replace(self._token, "***")
        return text


def _retry_wait(headers: Dict[str, str]) -> int:
    for name, value in headers.items():
        if name.lower() == "retry-after":
            try:
                wait = int(value.strip())
            except (ValueError, AttributeError):
                return DEFAULT_RETRY_WAIT
            return wait if wait >= 0 else DEFAULT_RETRY_WAIT
    return DEFAULT_RETRY_WAIT


def _decode_success(raw: bytes) -> Any:
    if not raw or not raw.strip():
        return {}
    text = raw.decode("utf-8", "replace")
    try:
        return json.loads(text)
    except ValueError:
        return text
