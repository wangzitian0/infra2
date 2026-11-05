"""Parse each YAML text once per pytest worker (#1453).

Many tests run an audit over the same repository files, and each audit parses the same
compose and workflow files again with the pure-Python ``yaml.safe_load``. Under coverage on
a CI runner, that parse was the largest cost of the 15 slowest tests.

``ParseOnce`` wraps a parser. A text that was parsed before returns a deep copy of the stored
result, so a caller that changes its result cannot change the next result. A parse error
is raised each time and is not stored. A stream that is not ``str`` or ``bytes`` goes to the
parser each time.
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from typing import Any


class ParseOnce:
    def __init__(self, parse: Callable[[Any], Any]) -> None:
        self._parse = parse
        self._parsed: dict[str | bytes, Any] = {}
        self.parses = 0

    def __call__(self, stream: Any) -> Any:
        if not isinstance(stream, (str, bytes)):
            return self._parse(stream)
        if stream not in self._parsed:
            value = self._parse(stream)
            self.parses += 1
            self._parsed[stream] = value
        return copy.deepcopy(self._parsed[stream])
