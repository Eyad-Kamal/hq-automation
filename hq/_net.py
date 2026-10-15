"""Shared HTTP plumbing for the source clients.

All requests go through a caller-supplied ``requests.Session`` so tests can
substitute a fake. The helpers here never raise on network or parse errors:
they log a warning and return ``None``.
"""

from __future__ import annotations

import functools
import logging
import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

import requests

log = logging.getLogger("hq.net")

DEFAULT_TIMEOUT: float = 30.0
USER_AGENT = "hq-automation/1.0 (competitive-programming training sync)"
DEFAULT_HEADERS: dict[str, str] = {"User-Agent": USER_AGENT}


class RateLimiter:
    """Enforces a minimum interval between successive calls.

    ``clock`` and ``sleep`` are injectable so tests run without real delays.
    """

    def __init__(
        self,
        min_interval: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.min_interval = float(min_interval)
        self._clock = clock
        self._sleep = sleep
        self._last: float | None = None

    def wait(self) -> None:
        """Block until at least ``min_interval`` has passed since the last call."""
        if self._last is not None and self.min_interval > 0:
            remaining = self.min_interval - (self._clock() - self._last)
            if remaining > 0:
                self._sleep(remaining)
        self._last = self._clock()


def send(
    session: requests.Session,
    method: str,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    json_body: Any = None,
    headers: dict[str, str] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> requests.Response | None:
    """Send one request; return None (and log) on transport errors."""
    merged = {**DEFAULT_HEADERS, **(headers or {})}
    try:
        return session.request(
            method,
            url,
            params=params,
            json=json_body,
            headers=merged,
            timeout=timeout,
        )
    except requests.RequestException as exc:
        log.warning("%s %s failed: %s", method, url, exc)
        return None


def get_json(session: requests.Session, method: str, url: str, **kwargs: Any) -> Any | None:
    """Return the decoded JSON body for an HTTP 200 response, else None."""
    resp = send(session, method, url, **kwargs)
    if resp is None:
        return None
    if resp.status_code != 200:
        log.warning("%s %s -> HTTP %s", method, url, resp.status_code)
        return None
    try:
        return resp.json()
    except ValueError:
        log.warning("%s %s returned non-JSON body", method, url)
        return None


def get_text(session: requests.Session, method: str, url: str, **kwargs: Any) -> str | None:
    """Return the body text for an HTTP 200 response, else None."""
    resp = send(session, method, url, **kwargs)
    if resp is None:
        return None
    if resp.status_code != 200:
        log.warning("%s %s -> HTTP %s", method, url, resp.status_code)
        return None
    return resp.text


def guarded(fallback: Callable[[], Any]) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator: a source function never raises; on any exception it logs and
    returns ``fallback()``. Used so that unexpected payload shapes degrade to an
    empty result instead of crashing a sync job."""

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                return fn(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001 - sources must never raise
                log.warning("%s failed: %s: %s", fn.__qualname__, type(exc).__name__, exc)
                return fallback()

        return wrapper

    return decorator


def from_epoch(seconds: int | float) -> datetime:
    """Unix seconds to an aware UTC datetime."""
    return datetime.fromtimestamp(float(seconds), tz=timezone.utc)
