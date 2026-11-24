"""The one authenticated GET of the GitHub REST API for the stage tools (standard library only).

``tools/ci_tree_reuse.py``, ``tools/promotion_reminder.py`` and
``common/deploy/stage_record_reader.py`` call it. A GET that fails with HTTP 5xx or a network
error is sent again after each delay of ``RETRY_DELAYS``. The last error is raised.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any, TypeVar

API = "https://api.github.com"
Opener = Callable[..., Any]
#: Seconds to wait before each repeat of a failed GET: 4 attempts, at most 7 s of waits.
RETRY_DELAYS: tuple[float, ...] = (1.0, 2.0, 4.0)
_sleep: Callable[[float], None] = time.sleep
_T = TypeVar("_T")


def github_headers(token: str, agent: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": agent,
    }


def is_transient(exc: BaseException) -> bool:
    """True for HTTP 5xx and network errors; False for HTTP 4xx, 3xx and data errors."""
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code >= 500
    return isinstance(exc, (urllib.error.URLError, TimeoutError, ConnectionError))


def with_retry(call: Callable[[], _T]) -> _T:
    """Return ``call()``. Repeat it after a transient error, at most ``len(RETRY_DELAYS)`` times."""
    for delay in RETRY_DELAYS:
        try:
            return call()
        except Exception as exc:
            if not is_transient(exc):
                raise
            _sleep(delay)
    return call()


def get_json(path: str, token: str, opener: Opener, agent: str) -> Any:
    request = urllib.request.Request(
        f"{API}{path}", headers=github_headers(token, agent)
    )

    def call() -> Any:
        with opener(request, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))

    return with_retry(call)
