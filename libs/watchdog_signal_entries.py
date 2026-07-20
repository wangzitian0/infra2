"""Service-scoped watchdog-signal entries: DERIVED, not handwritten (#543).

The ``primary_owner: internal`` section of docs/ssot/watchdog-signals.yaml
used to be a handwritten mirror of every ProbeFacet — 39 entries edited in
lockstep with each service's deploy.py, which is exactly the parallel-registry
drift #541/#543 exist to kill. That mirror is deleted:
:func:`render_internal_signal_entries` derives the same entries from the SAME
``ProbeFacet`` declarations the probe runner consumes, stamped with each
declaring service's ``SignalFacet`` (#425 T5 tier/type + structured debounce).

Cross-plane entries (cloudflare / github / self / excluded) STAY handwritten
in the YAML — they describe watchers outside the probe runner whose configs
live in wrangler.toml, tools/out_of_band_watchdog.py, or their own sidecar
code, so no single in-repo declaration can derive them.

Equivalence proof: the pre-deletion handwritten section is frozen at
``libs/tests/fixtures/watchdog_internal_signals_frozen.yaml`` and
``libs/tests/test_watchdog_signal_entries.py`` keeps the derivation
field-level equivalent to it forever.
"""

from __future__ import annotations

from typing import Any

# Every internal probe runs on the probe runner's fixed fast loop
# (INFRA_PROBE_INTERVAL_SECONDS=60) — the cadence is a runner fact, not a
# per-entry choice, which is why the handwritten entries all said "60s".
CADENCE = "60s"


def _component_for(service_id: str, meta_service: str) -> str:
    """The monitoring-component alias that resolves back to ``service_id``.

    For registry services that is the Deployer's ``service`` attribute. For
    probes declared on behalf of an out-of-registry component (bootstrap
    plane, the host), invert ``_EXTERNAL_COMPONENT_IDS`` — sorted-first alias
    wins on collision so the choice is deterministic.
    """
    from libs.service_registry import _EXTERNAL_COMPONENT_IDS

    for alias in sorted(_EXTERNAL_COMPONENT_IDS):
        if _EXTERNAL_COMPONENT_IDS[alias] == service_id:
            return alias
    return meta_service


def render_internal_signal_entries(attrs=None) -> list[dict[str, Any]]:
    """Derive the ``primary_owner: internal`` watchdog-signal entries.

    One entry per (environment, ProbeFacet): production + staging for normal
    probes, production-only for ``kind="resource"`` host probes (mirrors
    ``infra_probe_runner._host_specs_for_env`` — the host is shared, so only
    the production runner checks it). Entries carry ``service_id`` resolved
    the same way the probe spec's eighth field is, so the consistency audit's
    identity check holds by construction.

    ``tier``/``type``/``consecutive_failures``/``renotify_window_sec`` come
    from the declaring service's ``SignalFacet``. Declaring probes without a
    SignalFacet is the Infra-012.10 gap the facet matrix flags, so entries
    without the classification only exist while that backlog does.
    """
    if attrs is None:
        from libs.service_registry import service_attrs

        attrs = service_attrs()
    entries: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for service_id in sorted(attrs):
        meta = attrs[service_id]
        signal_facet = meta.signals[0] if meta.signals else None
        for probe in meta.probes:
            probe_service_id = probe.service_id or service_id
            environments = (
                ("production",)
                if probe.kind == "resource"
                else ("production", "staging")
            )
            for environment in environments:
                signal_id = f"{environment}.{probe.name}"
                if signal_id in seen_ids:
                    raise ValueError(
                        f"duplicate derived signal_id {signal_id!r} — two "
                        f"ProbeFacets share a name, which render_probe_spec_text "
                        f"should already have rejected"
                    )
                seen_ids.add(signal_id)
                entry: dict[str, Any] = {
                    "signal_id": signal_id,
                    "environment": environment,
                    "component": _component_for(probe_service_id, meta.service),
                    "signal": probe.name,
                    "primary_owner": "internal",
                    "severity": probe.severity,
                    "cadence": CADENCE,
                    "service_id": probe_service_id,
                }
                if probe.expected:
                    entry["expected"] = probe.expected
                if signal_facet is not None:
                    entry["tier"] = signal_facet.tier
                    entry["type"] = signal_facet.type
                    if signal_facet.type == "alert":
                        entry["consecutive_failures"] = (
                            signal_facet.consecutive_failures
                        )
                        entry["renotify_window_sec"] = signal_facet.renotify_window_sec
                entries.append(entry)
    if not entries:
        # Fail-closed, same rationale as render_probe_spec_text: an empty walk
        # means the registry scan broke, never that the fleet has no signals.
        raise ValueError(
            "render_internal_signal_entries produced ZERO entries — the "
            "ProbeFacet registry walk found nothing, which is never a valid "
            "state; refusing to report an empty internal signal plane"
        )
    return entries
