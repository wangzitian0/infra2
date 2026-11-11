"""Parse each distinct YAML text once per process (#1602).

Repository scans parse the same files many times: the merge gate parsed
``infra-ci.yml`` once per blocking gate, and the deploy guard audit parsed each
compose file once per check (about 1,900 parses in one test file). Pure-Python
PyYAML parsing was 80-90% of those tests' time.

``safe_load_text(yaml_mod, text)`` returns ``yaml_mod.safe_load(text)``. The caller
passes its own ``yaml`` module, so a test that replaces that module or its
``safe_load`` keeps its replacement. Rules:

1. The cache key is the ``safe_load`` function and the text, not a path. The same
   text gives the same value; a new loader or a new text gets a new parse.
2. A parse that raises is not stored.
3. The caller gets a deep copy, so a caller that changes the result cannot change
   the result of a later call.
4. Input that is not a ``str`` (bytes, a stream) and a loader that cannot be hashed
   are parsed on every call.

The module imports nothing from PyYAML: some callers import it lazily.
"""

from __future__ import annotations

import copy
import functools
from collections.abc import Callable


@functools.lru_cache(maxsize=512)
def _parse_once(loader: Callable[[str], object], text: str) -> object:
    return loader(text)


def safe_load_text(yaml_mod, text) -> object:
    """Return ``yaml_mod.safe_load(text)`` by the rules of this module."""
    loader = yaml_mod.safe_load
    if not isinstance(text, str):
        return loader(text)
    try:
        hash(loader)
    except TypeError:
        return loader(text)
    return copy.deepcopy(_parse_once(loader, text))
