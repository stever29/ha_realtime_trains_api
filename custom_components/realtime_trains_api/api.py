"""Client for the Realtime Trains API at data.rtt.io.

Kept free of Home Assistant imports so it can be tested on its own.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, time, timedelta, timezone
import logging
from typing import Any
from zoneinfo import ZoneInfo

import aiohttp

_LOGGER = logging.getLogger(__name__)

API_BASE = "https://data.rtt.io"
# Pin the response format so later API releases can't change it under us.
API_VERSION = "2026-07-25"
LOCAL_TZ = ZoneInfo("Europe/London")

TOKEN_TYPE_REFRESH = "refresh"
TOKEN_TYPE_ACCESS = "access"

# Fetch a new access token this long before the current one expires.
TOKEN_REFRESH_MARGIN = timedelta(minutes=5)
DEFAULT_RETRY_AFTER = 60


class RttError(Exception):
    """The API could not be queried."""


class RttAuthError(RttError):
    """The token was rejected."""


class RttRateLimited(RttError):
    """The API rate limit was hit, or we are backing off after hitting it."""


def parse_datetime(value: str | None) -> datetime | None:
    """Parse an ISO 8601 datetime from the API into an aware datetime."""
    if not value:
        return None
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=LOCAL_TZ)
    return parsed


def best_time(temporal: dict | None) -> datetime | None:
    """Return the most accurate known time for an arrival or departure."""
    if not temporal:
        return None
    for key in ("realtimeActual", "realtimeForecast", "realtimeEstimate", "scheduleAdvertised"):
        parsed = parse_datetime(temporal.get(key))
        if parsed is not None:
            return parsed
    return None


def has_code(location: dict | None, code: str) -> bool:
    """Whether a location object matches a CRS or TIPLOC code."""
    if not location:
        return False
    code = code.upper()
    return code in (location.get("shortCodes") or []) or code in (location.get("longCodes") or [])


def is_public_call(temporal: dict | None) -> bool:
    """Whether the train is booked to stop here for passengers."""
    if not temporal or temporal.get("displayAs") in ("CANCELLED", "DIVERTED", "PASS", None):
        return False
    call_type = temporal.get("realtimeCallType") or temporal.get("scheduledCallType")
    return bool(call_type) and call_type.startswith("ADVERTISED")


def platform_of(location_metadata: dict | None) -> str | None:
    platform = (location_metadata or {}).get("platform") or {}
    return platform.get("actual") or platform.get("planned")


class PollSchedule:
    """Decides how often to call the API depending on the time of day."""

    def __init__(self, windows: list[dict], active_interval: timedelta, idle_interval: timedelta):
        # Each window: {"start": time, "end": time, "days": ["mon", ...]}
        self._windows = windows
        self.active_interval = active_interval
        self.idle_interval = idle_interval

    def in_window(self, now: datetime) -> bool:
        if not self._windows:
            return True
        local = now.astimezone(LOCAL_TZ)
        day = local.strftime("%a").lower()
        previous_day = (local - timedelta(days=1)).strftime("%a").lower()
        current = local.time()
        for window in self._windows:
            start: time = window["start"]
            end: time = window["end"]
            days = window.get("days") or []
            if start <= end:
                if (not days or day in days) and start <= current < end:
                    return True
            else:
                # Window crosses midnight: the part after midnight belongs to the previous day.
                if (not days or day in days) and current >= start:
                    return True
                if (not days or previous_day in days) and current < end:
                    return True
        return False

    def interval(self, now: datetime) -> timedelta:
        return self.active_interval if self.in_window(now) else self.idle_interval

    def is_due(self, now: datetime, last_poll: datetime | None) -> bool:
        if last_poll is None:
            return True
        # Allow a little slack so a 120s interval on a 60s tick polls every 2 ticks, not 3.
        return now - last_poll >= self.interval(now) - timedelta(seconds=5)


class RttClient:
    """Shared client: one token, one rate-limit back-off and one cache for all sensors."""

    def __init__(self, session: aiohttp.ClientSession, token: str, token_type: str = TOKEN_TYPE_REFRESH):
        self._session = session
        self._token = token
        self._token_type = token_type
        self._access_token: str | None = token if token_type == TOKEN_TYPE_ACCESS else None
        self._access_valid_until: datetime | None = None
        self._token_lock = asyncio.Lock()
        self._blocked_until: datetime | None = None
        self._cache: dict[tuple, tuple[datetime, Any]] = {}
        self._locks: dict[tuple, asyncio.Lock] = {}
        self._time_window_supported = True

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    async def _access(self) -> str:
        if self._token_type == TOKEN_TYPE_ACCESS:
            return self._token
        async with self._token_lock:
            now = self._now()
            if (
                self._access_token is not None
                and self._access_valid_until is not None
                and self._access_valid_until - TOKEN_REFRESH_MARGIN > now
            ):
                return self._access_token
            status, data, _ = await self._raw_get("/api/get_access_token", {}, self._token)
            if status in (401, 403):
                raise RttAuthError("Refresh token rejected by data.rtt.io")
            if status != 200 or not data or "token" not in data:
                raise RttError(f"Could not get an access token (HTTP {status})")
            self._access_token = data["token"]
            self._access_valid_until = parse_datetime(data.get("validUntil")) or now + timedelta(minutes=30)
            _LOGGER.debug("New RTT access token valid until %s", self._access_valid_until)
            return self._access_token

    async def _raw_get(self, path: str, params: dict, bearer: str):
        headers = {"Authorization": f"Bearer {bearer}", "Version": API_VERSION}
        try:
            async with self._session.get(API_BASE + path, params=params, headers=headers) as response:
                self._note_rate_limit(response)
                if response.status == 429:
                    retry_after = _int(response.headers.get("Retry-After"), DEFAULT_RETRY_AFTER)
                    self._blocked_until = self._now() + timedelta(seconds=retry_after)
                    raise RttRateLimited(f"Rate limited by data.rtt.io for {retry_after}s")
                data = None
                if response.status == 200:
                    data = await response.json(content_type=None)
                return response.status, data, response.headers
        except aiohttp.ClientError as err:
            raise RttError(f"Request to {path} failed: {err}") from err

    def _note_rate_limit(self, response) -> None:
        remaining = {
            dim: response.headers.get(f"X-RateLimit-Remaining-{dim}")
            for dim in ("Minute", "Hour", "Day", "Week")
        }
        if any(remaining.values()):
            _LOGGER.debug("RTT requests remaining: %s", remaining)

    async def _get(self, path: str, params: dict):
        now = self._now()
        if self._blocked_until is not None and now < self._blocked_until:
            raise RttRateLimited(f"Backing off until {self._blocked_until.isoformat()}")
        status, data, _ = await self._raw_get(path, params, await self._access())
        if status == 401 and self._token_type == TOKEN_TYPE_REFRESH:
            # Access token may have been revoked early: fetch a new one and retry once.
            self._access_token = None
            status, data, _ = await self._raw_get(path, params, await self._access())
        if status in (401, 403):
            raise RttAuthError(f"Token rejected by data.rtt.io (HTTP {status})")
        return status, data

    async def _cached(self, key: tuple, ttl: timedelta, fetch):
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            cached = self._cache.get(key)
            if cached is not None and self._now() - cached[0] < ttl:
                return cached[1]
            value = await fetch()
            self._cache[key] = (self._now(), value)
            return value

    async def departures(self, origin: str, destination: str, time_window: int, ttl: timedelta) -> list[dict]:
        """Services from origin calling later at destination, starting now. Shared between sensors."""

        async def fetch():
            params = {"code": origin, "filterTo": destination}
            if self._time_window_supported and time_window != 60:
                params["timeWindow"] = time_window
            status, data = await self._get("/gb-nr/location", params)
            if status == 400 and "timeWindow" in params:
                _LOGGER.warning(
                    "data.rtt.io rejected timeWindow=%s for this token; using the default 60 minutes", time_window
                )
                self._time_window_supported = False
                params.pop("timeWindow")
                status, data = await self._get("/gb-nr/location", params)
            if status == 204:
                return []
            if status != 200:
                raise RttError(f"Location query {origin}->{destination} failed (HTTP {status})")
            return (data or {}).get("services") or []

        return await self._cached(("location", origin, destination, time_window), ttl, fetch)

    async def service(self, unique_identity: str, ttl: timedelta) -> dict | None:
        """Full calling pattern for one service. Shared between sensors."""

        async def fetch():
            identity = unique_identity.split(":", 1)[1] if unique_identity.startswith("gb-nr:") else unique_identity
            status, data = await self._get("/gb-nr/service", {"uniqueIdentity": identity})
            if status == 404:
                return None
            if status != 200:
                raise RttError(f"Service query {unique_identity} failed (HTTP {status})")
            return (data or {}).get("service")

        return await self._cached(("service", unique_identity), ttl, fetch)


def _int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
