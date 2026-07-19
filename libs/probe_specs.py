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
    return "\n".join(lines)


def resolve_env_suffix(specs_text: str, env_suffix: str) -> str:
    """Substitute the ``${ENV_SUFFIX}`` placeholders with the deploy env's
    actual suffix ('' on production). This used to be docker compose's
    interpolation job when the specs lived inside compose.yaml; the renderer
    resolves it explicitly so the env-var transport needs no second
    interpolation pass."""
    return specs_text.replace(ENV_SUFFIX_PLACEHOLDER, env_suffix)


def encode_specs_env_value(specs_text: str) -> str:
    """Encode multi-line spec text as ONE dotenv-safe env value.

    Dokploy stores the compose env as newline-joined ``KEY=value`` lines and
    writes them verbatim to the ``.env`` docker compose reads, so the value must
    be single-line. Double quotes + ``\\n`` escapes are the documented compose
    dotenv form for embedded newlines (compose-go expands escape sequences
    inside double-quoted values). Fails closed on characters that would break
    the quoting or trigger dotenv interpolation — including an unresolved
    ``${ENV_SUFFIX}`` placeholder (call :func:`resolve_env_suffix` first).
    """
    for forbidden in ('"', "\\", "$"):
        if forbidden in specs_text:
            raise ValueError(
                f"probe spec text contains {forbidden!r}, which the dotenv "
                "env-value transport cannot carry safely"
            )
    return '"' + specs_text.replace("\n", "\\n") + '"'


def normalize_specs_text(specs_text: str) -> str:
    """Undo the env-value transport encoding, tolerantly.

    Accepts plain multi-line text (the expected form after compose's dotenv
    expands the double-quoted value) AND the still-encoded single-line form
    (enclosing quotes and/or literal ``\\n`` sequences), so both the runner and
    the runtime verification behave identically no matter which side of the
    dotenv expansion they observe."""
    text = specs_text.strip()
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        text = text[1:-1]
    return text.replace("\\n", "\n")


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
