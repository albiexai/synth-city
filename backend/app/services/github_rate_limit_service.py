"""Observe GitHub REST quota headers and pause requests sharing a credential."""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from hashlib import sha256

import httpx

from app.config import settings
from app.services.work_item_states import DONE_STATUSES

GITHUB_RATE_LIMITED = "github_rate_limited"
_FALLBACK = timedelta(minutes=1)
_MAX_CREDENTIALS = 128


def _unsigned(value: str | None) -> int | None:
    if value and len(value) <= 18 and value.isascii() and value.isdecimal():
        return int(value)
    return None


def _reset_time(value: str | None) -> datetime | None:
    epoch = _unsigned(value)
    if epoch is not None:
        try:
            return datetime.fromtimestamp(epoch, timezone.utc)
        except (OverflowError, OSError, ValueError):
            pass
    return None


def _retry_time(value: str | None, now: datetime) -> datetime | None:
    if not value:
        return None
    try:
        delay = _unsigned(value)
        if delay is not None:
            return now + timedelta(seconds=delay)
        retry = parsedate_to_datetime(value)
        return retry.astimezone(timezone.utc) if retry.tzinfo is not None else None
    except (OverflowError, TypeError, ValueError):
        return None


def _is_rest_request(request: httpx.Request) -> bool:
    return (request.url.scheme == "https" and request.url.host == "api.github.com"
            and request.url.port in (None, 443) and request.url.path != "/graphql")


@dataclass(frozen=True)
class _Observation:
    remaining: int | None
    reset_at: datetime | None
    retry_at: datetime | None
    observed_at: datetime


class GithubRateLimited(httpx.HTTPStatusError):
    """A quota refusal, including an unsent request during a known cooldown."""

    def __init__(self, retry_at: datetime, request: httpx.Request, response: httpx.Response | None = None):
        self.retry_at = retry_at
        super().__init__(f"{GITHUB_RATE_LIMITED}: retry after {retry_at.isoformat()}", request=request,
                         response=response if response is not None else httpx.Response(429, request=request))


class GithubRateLimitService:
    def __init__(self, now=None):
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._observations: OrderedDict[bytes, _Observation] = OrderedDict()

    @staticmethod
    def _key(authorization: str | None) -> bytes:
        # Keys stay in memory and are never part of the error or public observation.
        return sha256((authorization or "").encode()).digest()

    def install(self, client: httpx.AsyncClient) -> None:
        for event, hook in (("request", self._before_request), ("response", self._after_response)):
            if hook not in client.event_hooks[event]:
                client.event_hooks[event].append(hook)

    def require_available(self, authorization: str | None, request: httpx.Request | None = None) -> None:
        observation = self._observations.get(self._key(authorization))
        if observation and observation.retry_at and self._now() < observation.retry_at:
            raise GithubRateLimited(observation.retry_at, request or httpx.Request("GET", "https://api.github.com/"))

    async def _before_request(self, request: httpx.Request) -> None:
        if _is_rest_request(request):
            self.require_available(request.headers.get("authorization"), request)

    async def _after_response(self, response: httpx.Response) -> None:
        request = response.request
        if not _is_rest_request(request) or response.headers.get("x-ratelimit-resource", "core") != "core":
            return
        now = self._now()
        remaining = _unsigned(response.headers.get("x-ratelimit-remaining"))
        reset_at = _reset_time(response.headers.get("x-ratelimit-reset"))
        retry_after = _retry_time(response.headers.get("retry-after"), now)
        rate_error = response.status_code == 429 or (
            response.status_code == 403 and (remaining == 0 or retry_after is not None))
        exhausted = remaining == 0 and (rate_error or response.is_success)
        if remaining is None and not rate_error:
            return
        retry_at = None
        if exhausted or rate_error:
            deadlines = (reset_at if exhausted else None, retry_after if rate_error else None)
            deadline = max((when for when in deadlines if when is not None), default=now + _FALLBACK)
            if deadline > now:
                retry_at = deadline

        key = self._key(request.headers.get("authorization"))
        previous = self._observations.get(key)
        observed_at = now
        if previous and previous.retry_at and previous.retry_at > now:
            # A response from a request already in flight cannot shorten an observed pause.
            retry_at = max(previous.retry_at, retry_at) if retry_at else previous.retry_at
        if previous and previous.reset_at is not None:
            if reset_at is not None and reset_at < previous.reset_at:
                # Quota headers can arrive out of order across requests and reset windows.
                remaining, reset_at, observed_at = previous.remaining, previous.reset_at, previous.observed_at
            elif reset_at == previous.reset_at or (reset_at is None and previous.reset_at > now):
                reset_at = previous.reset_at
                if remaining is None or (previous.remaining is not None and previous.remaining <= remaining):
                    remaining, observed_at = previous.remaining, previous.observed_at
        self._observations[key] = _Observation(remaining, reset_at, retry_at, observed_at)
        self._observations.move_to_end(key)
        overflow = len(self._observations) - _MAX_CREDENTIALS
        if overflow > 0:
            # Only evict quota observations without a live pause. All live pauses survive until their deadline.
            evictable = [candidate for candidate, value in self._observations.items()
                         if value.retry_at is None or value.retry_at <= now]
            for candidate in evictable[:overflow]:
                del self._observations[candidate]
        if rate_error:
            raise GithubRateLimited(retry_at or now, request, response)

    def polling_status(self) -> dict | None:
        token = settings.github_token
        observation = self._observations.get(self._key(f"Bearer {token}" if token else None))
        if observation is None:
            return None
        now = self._now()
        retry_at = observation.retry_at if observation.retry_at and observation.retry_at > now else None
        window_expired = observation.reset_at is not None and observation.reset_at <= now
        if retry_at is None and (window_expired or observation.remaining in (None, 0)):
            return None
        return {
            "reason": GITHUB_RATE_LIMITED if retry_at else None,
            "remaining": None if window_expired else observation.remaining,
            "reset_at": observation.reset_at.isoformat() if observation.reset_at else None,
            "retry_at": retry_at.isoformat() if retry_at else None,
            "observed_at": observation.observed_at.isoformat(),
        }

    def work_item_note(self, dispatch_status: str, note: str | None) -> str | None:
        quota = self.polling_status()
        if dispatch_status in DONE_STATUSES or not quota or not quota["reason"]:
            return note
        pause = f"{GITHUB_RATE_LIMITED}: GitHub polling paused until {quota['retry_at']}"
        return f"{note} · {pause}" if note else pause


github_rate_limit_service = GithubRateLimitService()
