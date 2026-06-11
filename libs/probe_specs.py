"""Helpers for verifying INFRA_PROBE_SPECS actually reached a running probe runner.

The deploy hash gate proves "the intended config changed and was recorded", not
"the running container actually carries it". These pure helpers let a deployer
compare the source `INFRA_PROBE_SPECS` against what a live container reports, so a
deploy that silently failed to recreate the container can be caught instead of
being reported as a success while the catalog claims the probes are "Live".
"""

from __future__ import annotations


def parse_probe_names(specs_text: str) -> set[str]:
    """Extract probe names (the first `|`-delimited field) from an
    INFRA_PROBE_SPECS block. Blank and malformed lines are ignored."""
    names: set[str] = set()
    for raw in specs_text.splitlines():
        line = raw.strip()
        if not line or "|" not in line:
            continue
        name = line.split("|", 1)[0].strip()
        if name:
            names.add(name)
    return names


def missing_probe_names(source_specs: str, running_specs: str) -> list[str]:
    """Probe names declared in the source specs but absent from what is actually
    running. Empty list means the running specs are a superset of the source."""
    return sorted(parse_probe_names(source_specs) - parse_probe_names(running_specs))
