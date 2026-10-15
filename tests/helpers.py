"""Test doubles and fixture loaders. No test in this project touches the network."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def fixture_text(relative: str) -> str:
    return (FIXTURES / relative).read_text(encoding="utf-8")


def fixture_json(relative: str) -> Any:
    return json.loads(fixture_text(relative))


class FakeResponse:
    """Minimal stand-in for ``requests.Response``."""

    def __init__(
        self,
        status_code: int = 200,
        payload: Any = None,
        text: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self.headers = dict(headers or {})
        self._payload = payload
        if text is None:
            text = json.dumps(payload) if payload is not None else ""
        self.text = text
        self.content = text.encode("utf-8")

    def json(self) -> Any:
        if self._payload is not None:
            return self._payload
        return json.loads(self.text)  # raises ValueError on invalid JSON


class FakeSession:
    """Records every ``request`` call and delegates to ``handler``.

    ``handler(method, url, kwargs)`` returns a ``FakeResponse`` or raises an
    exception (raised exceptions propagate, as ``requests`` would).
    """

    def __init__(self, handler: Callable[[str, str, dict[str, Any]], Any]) -> None:
        self._handler = handler
        self.calls: list[dict[str, Any]] = []

    def request(self, method: str, url: str, **kwargs: Any) -> Any:
        self.calls.append({"method": method, "url": url, **kwargs})
        result = self._handler(method, url, kwargs)
        if isinstance(result, BaseException):
            raise result
        return result

    def urls(self) -> list[str]:
        return [c["url"] for c in self.calls]


def constant_session(response: FakeResponse) -> FakeSession:
    """A session that answers every call with the same response."""
    return FakeSession(lambda _m, _u, _k: response)


class FakeClock:
    """Injectable clock and sleep for rate-limiter tests. Sleeping advances time."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds
