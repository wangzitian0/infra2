"""INFRA_PROBE_SPECS: registry-derived rendering + live-runner verification.

Rendering (#541): the probe lines are DERIVED from the ProbeFacet declarations
on each service's Deployer class via ``libs.service_registry.service_attrs()``
— :func:`render_probe_spec_text` is the single aggregation point the alerting
deployer feeds into Dokploy's compose env (``compose_env_base``, NOT
``pre_compose`` — the iac-runner's sync path skips pre_compose entirely).

Verification: the deploy hash gate proves "the intended config changed and was
recorded", not "the running container actually carries it". The pure helpers
below let a deployer compare the source `INFRA_PROBE_SPECS` against what a live
container reports, so a deploy that silently failed to recreate the container
can be caught instead of being reported as a success while the catalog claims
the probes are "Live".
"""

from __future__ import annotations

# The literal placeholder ProbeFacet targets keep for per-environment hosts.
# The alerting deployer resolves it at render time (compose no longer sees it).
ENV_SUFFIX_PLACEHOLDER = "${ENV_SUFFIX}"


def render_probe_spec_text(attrs=None) -> str:
    """Aggregate every registry service's ProbeFacets into the canonical
    ``INFRA_PROBE_SPECS`` text (one 8-field line per probe, ``${ENV_SUFFIX}``
    placeholders intact, no comments).

    Deterministic: services in sorted service_id order, probes in declaration
    order within each service. Duplicate probe names across services fail
    closed — two facets silently overwriting one runner probe is exactly the
    drift class #541 exists to kill.
    """
    if attrs is None:
        from libs.service_registry import service_attrs

        attrs = service_attrs()
    lines: list[str] = []
    seen: dict[str, str] = {}
    for service_id in sorted(attrs):
        meta = attrs[service_id]
        for probe in meta.probes:
            if probe.name in seen:
                raise ValueError(
                    f"duplicate ProbeFacet name {probe.name!r} declared by both "
                    f"{seen[probe.name]} and {service_id}"
                )
            seen[probe.name] = service_id
            lines.append(probe.spec_line(default_service_id=service_id))
    if not lines:
        # Fail-closed (#541): an empty render means the registry walk itself
        # broke (bad glob, import failure swallowed upstream, ...) — this repo
        # always declares probes, so "no probes" is never a deployable state.
        # Raising here stops compose_env_base cold instead of shipping an empty
        # env value that would leave the whole fleet silently unmonitored.
        raise ValueError(
            "render_probe_spec_text produced ZERO probes — the ProbeFacet "
            "registry walk found nothing, which is never a valid state; "
            "refusing to render an empty INFRA_PROBE_SPECS"
        )
    return "\n".join(lines)


def resolve_env_suffix(specs_text: str, env_suffix: str) -> str:
    """Substitute the ``${ENV_SUFFIX}`` placeholders with the deploy env's
    actual suffix ('' on production). This used to be docker compose's
    interpolation job when the specs lived inside compose.yaml; the renderer
    resolves it explicitly so the env-var transport needs no second
    interpolation pass."""
    return specs_text.replace(ENV_SUFFIX_PLACEHOLDER, env_suffix)


# Line separator for the single-line env transport. Chosen to be inert through
# EVERY layer the value crosses (Dokploy env storage -> .env writer -> compose
# dotenv parse -> container env): plain ASCII, no backslashes, no quotes, no $.
# The original double-quote + \n-escape encoding was proven wrong LIVE
# (v1.1.33 staging deploy, 2026-07-19): Dokploy's .env writer expands \n
# escapes into REAL newlines, splitting the value across lines and crashing
# compose's dotenv parse ('unexpected character "|" in variable name').
SPECS_LINE_SEPARATOR = ";;"


def encode_specs_env_value(specs_text: str) -> str:
    """Encode multi-line spec text as ONE dotenv-safe env value.

    Joins lines with :data:`SPECS_LINE_SEPARATOR` — no quoting, no escape
    sequences, nothing any env layer re-interprets. Internal spaces are fine
    (dotenv preserves them in unquoted values; ``command`` probe targets need
    them). Fails closed on content that could break the transport:
    quote/backslash/``$`` (dotenv quoting and interpolation), ``#`` (starts an
    inline comment in an unquoted dotenv value), an embedded separator token,
    and an unresolved ``${ENV_SUFFIX}`` placeholder (call
    :func:`resolve_env_suffix` first; caught by the ``$`` check).
    """
    for forbidden in ('"', "\\", "$", "#", SPECS_LINE_SEPARATOR):
        if forbidden in specs_text:
            raise ValueError(
                f"probe spec text contains {forbidden!r}, which the env-value "
                "transport cannot carry safely"
            )
    return specs_text.replace("\n", SPECS_LINE_SEPARATOR)


def normalize_specs_text(specs_text: str) -> str:
    """Undo the env-value transport encoding, tolerantly.

    Accepts plain multi-line text, the current ``;;``-separated single-line
    transport form, AND the retired quoted/``\\n``-escaped form (still decoded
    so a container carrying the old encoding is read correctly during the
    transition), so the runner and the runtime verification behave identically
    no matter which form they observe."""
    text = specs_text.strip()
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        text = text[1:-1]
    return text.replace(SPECS_LINE_SEPARATOR, "\n").replace("\\n", "\n")


def parse_probe_names(specs_text: str) -> set[str]:
    """Extract probe names (the first `|`-delimited field) from an
    INFRA_PROBE_SPECS block. Blank, comment, and malformed lines are ignored."""
    names: set[str] = set()
    for raw in normalize_specs_text(specs_text).splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "|" not in line:
            continue
        name = line.split("|", 1)[0].strip()
        if name:
            names.add(name)
    return names


def missing_probe_names(source_specs: str, running_specs: str) -> list[str]:
    """Probe names declared in the source specs but absent from what is actually
    running. Empty list means the running specs are a superset of the source."""
    return sorted(parse_probe_names(source_specs) - parse_probe_names(running_specs))


def normalized_probe_fields(specs_text: str) -> list[list[str]]:
    """Order-normalized field-level view of a specs block for equivalence
    proofs: comments/blanks stripped, each line split on ``|`` with fields
    stripped, rows sorted. Two blocks with equal output declare the same probes
    field-for-field regardless of ordering or commentary."""
    rows: list[list[str]] = []
    for raw in normalize_specs_text(specs_text).splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "|" not in line:
            continue
        rows.append([field.strip() for field in line.split("|")])
    return sorted(rows)
