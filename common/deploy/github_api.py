"""The one authenticated GET of the GitHub REST API for the stage tools (standard library only).

``tools/ci_tree_reuse.py`` and ``common/deploy/stage_record_reader.py`` both call it.
"""

from __future__ import annotations

import json
import urllib.request
from collections.abc import Callable
from typing import Any

API = "https://api.github.com"
Opener = Callable[..., Any]


def github_headers(token: str, agent: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": agent,
    }


def get_json(path: str, token: str, opener: Opener, agent: str) -> Any:
    request = urllib.request.Request(
        f"{API}{path}", headers=github_headers(token, agent)
    )
    with opener(request, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))
