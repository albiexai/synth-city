"""Shared pieces of the Supervisor scripts: token handling, a bounded read-only client and safe output (#1116)."""
from __future__ import annotations

import getpass
import http.client
import ipaddress
import json
import os
import stat
import time
import urllib.error
import urllib.parse
import urllib.request
import warnings

OPERATOR_ENV = "SYNTH_CITY_OPERATOR_TOKEN"
OPERATOR_HEADER = "x-synth-city-operator-token"
MAX_BYTES = 1_000_000
TIMEOUT_SECONDS = 10
REMOVED = "[token removed]"


class ConfigError(Exception):
    """The command line or a token source is unsafe or unusable. The text never holds a token."""


class StepError(Exception):
    """One read failed. The text holds no answer body and no token."""


class Redactor:
    """Replace every known token value in any text. Empty values are ignored."""

    def __init__(self, *secrets):
        self._secrets = sorted({s for s in secrets if s}, key=len, reverse=True)

    def __call__(self, text):
        for secret in self._secrets:
            text = text.replace(secret, REMOVED)
        return text


def clean(text, limit=120):
    """Text from another party for the terminal: no control characters, bounded length."""
    text = "".join(c if c.isprintable() else " " for c in str(text if text is not None else ""))
    return " ".join(text.split())[:limit]


def printable(text):
    """A line for the terminal: every control character becomes a space. Run it last, on the whole line."""
    return "".join(c if c.isprintable() else " " for c in text)


def hidden_prompt(text):
    """Hidden input only. A terminal that cannot hide the input is a setup error, never a visible fallback."""
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        try:
            return getpass.getpass(text)
        except (getpass.GetPassWarning, EOFError):
            raise ConfigError(f"the terminal cannot hide the input; set {OPERATOR_ENV} instead") from None


def check_base_url(url):
    """Plain http only to this machine. A token never crosses the network unencrypted or goes with a login in the URL."""
    try:
        parts = urllib.parse.urlsplit(url)
        host, _ = parts.hostname, parts.port
    except ValueError:
        raise ConfigError("the base URL is not valid") from None
    if parts.scheme not in ("http", "https") or not host:
        raise ConfigError("the base URL must start with http:// or https://")
    if parts.username or parts.password:
        raise ConfigError("the base URL must not hold a user name or password")
    if parts.scheme == "http" and not _is_loopback(host):
        raise ConfigError("plain http is allowed only for localhost or a loopback address; use https")
    return url.rstrip("/")


def _is_loopback(host):
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def read_token_file(path):
    """The token inside a regular file that only its owner can access. A link is refused."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as error:
        raise ConfigError(f"cannot read the token file ({error.strerror})") from None
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ConfigError("the token file is not a regular file")
        if info.st_mode & 0o077 or info.st_uid != os.geteuid():
            raise ConfigError("the token file mode must be 600 and the file must belong to you")
        value = stream.read(4097).strip()
    if not value or len(value) > 4096:
        raise ConfigError("the token file must hold one token of up to 4096 characters")
    return value


def operator_token(env, ask, prompt):
    """The operator token from the environment, or from a hidden prompt when asked. None when neither is given."""
    if ask:
        value = prompt("Operator token (input hidden): ").strip()
        if not value:
            raise ConfigError("no operator token was entered")
        return value
    return (env.get(OPERATOR_ENV) or "").strip() or None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Client:
    """Read-only GET client. It follows no redirect, ignores proxy settings and bounds time and size.

    Each wait for data stops after TIMEOUT_SECONDS. The whole answer is checked against the same limit between reads,
    so a slow answer can run over by at most one wait.
    """

    def __init__(self, base_url, redact):
        self.base_url = base_url
        self._redact = redact
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)

    def get(self, path, headers=None, params=None):
        url = self.base_url + path + ("?" + urllib.parse.urlencode(params) if params else "")
        request = urllib.request.Request(url, headers=headers or {}, method="GET")
        started, chunks, size = time.monotonic(), [], 0
        try:
            with self._opener.open(request, timeout=TIMEOUT_SECONDS) as answer:
                while chunk := answer.read1(8192):
                    size += len(chunk)
                    if size > MAX_BYTES:
                        raise StepError(f"the answer is larger than {MAX_BYTES} bytes")
                    if time.monotonic() - started > TIMEOUT_SECONDS:
                        raise StepError(f"the answer took longer than {TIMEOUT_SECONDS} seconds")
                    chunks.append(chunk)
        except urllib.error.HTTPError as error:
            raise StepError(f"HTTP {error.code}") from None
        except (urllib.error.URLError, http.client.HTTPException, OSError) as error:
            raise StepError(self._redact(f"cannot read the answer ({type(error).__name__})")) from None
        try:
            return self._scrub(json.loads(b"".join(chunks)))
        except ValueError:
            raise StepError("the answer is not JSON") from None

    def _scrub(self, value):
        """Redact every text of the answer once, before any cut or reformat, so no part of a token can stay."""
        if isinstance(value, str):
            return self._redact(value)
        if isinstance(value, list):
            return [self._scrub(item) for item in value]
        if isinstance(value, dict):
            return {key: self._scrub(item) for key, item in value.items()}
        return value
