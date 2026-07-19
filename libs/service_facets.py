"""Typed per-service facet declarations for the Deployer registry (#541).

Convergence part 1: the Deployer subclass in each service's ``deploy.py`` is the
SINGLE declaration point for that service's operational facts, and
``libs.service_registry.service_attrs()`` is the single derivation function.
These dataclasses are the typed vocabulary those declarations use:

- :class:`ProbeFacet`     — one infra probe line (fields aligned with
                            ``libs.infra_probes.ProbeSpec``); the alerting stack
                            renders the aggregate ``INFRA_PROBE_SPECS`` from them.
- :class:`SignalFacet`    — watchdog signal classification (fields aligned with
                            #425 T5's ``docs/ssot/watchdog-signals.yaml``:
                            tier / type / consecutive_failures / renotify_window_sec).
- :class:`BackupFacet`    — backup method facts (fields aligned with
                            ``libs.backup_verification.BackupEntry``; ``service_id``
                            and ``data_path`` stay on the Deployer itself).
- :class:`SecretsFacet`   — Vault self-refresh facts (fields aligned with
                            ``libs.vault_self_refresh_audit.VaultService``); the
                            audit inventory is DERIVED from these (#542), so the
                            audit's expectations and the deployed vault-agent
                            wiring come from the same declaration.
- :class:`Exemption`      — an explicit, justified "this facet does not apply"
                            declaration, so a completeness matrix cell can be
                            *exempt* instead of silently MISSING.

EXTRACTION CONSTRAINT (load-bearing): the registry reads Deployer attributes via
AST (``libs/service_registry.py``), never by importing ``deploy.py`` (no import
side effects). Facet declarations must therefore be LITERAL constructor calls
with literal arguments, assigned directly on the top-level Deployer subclass::

    class MinioDeployer(Deployer):
        probes = (
            ProbeFacet(name="minio-internal-http", kind="http",
                       target="http://platform-minio${ENV_SUFFIX}:9000/minio/health/live",
                       expected="200"),
        )

No names, f-strings, comprehensions, or computed values — the AST reader
fails closed (raises) on anything it cannot evaluate, so a malformed facet can
never silently drop a probe from the rendered specs.

Field values must not contain ``|`` (the spec-line separator), ``"`` or ``\\``
(the env-transport quoting), or ``$`` other than the literal ``${ENV_SUFFIX}``
placeholder (resolved at render time by the alerting deployer).
"""

from __future__ import annotations

from dataclasses import dataclass

# The literal ``${ENV_SUFFIX}`` placeholder used inside per-environment probe
# targets. Facets keep it symbolic (so declarations stay env-neutral and the
# env-isolation lint semantics survive the migration off compose.yaml); the
# alerting deployer resolves it from the deploy env at render time. prod_only
# services deliberately do NOT carry it (single shared prod instance — a
# suffixed host never exists and would be a permanent false-positive alert);
# that rule is enforced from the registry's ``prod_only`` fact, not comments.
ENV_SUFFIX_PLACEHOLDER = "${ENV_SUFFIX}"


@dataclass(frozen=True)
class ProbeFacet:
    """One probe of the owning service, as consumed by tools/infra_probe_runner.

    Renders to one ``INFRA_PROBE_SPECS`` line:
    ``name|kind|target|expected|severity|timeout_seconds|depends_on|service_id``
    (field semantics == ``libs.infra_probes.ProbeSpec``).

    ``service_id`` is normally left empty and derived from the DECLARING
    service's registry id. It is set explicitly only for probes a service
    declares on behalf of an out-of-registry component (the bootstrap plane and
    the host itself have no registry Deployer scan — see AlertingDeployer).
    """

    name: str
    kind: str  # http | tcp | command | resource
    target: str
    expected: str = ""
    severity: str = "critical"
    timeout_seconds: int = 5
    depends_on: str = ""  # cascade root probe name; empty = independent
    service_id: str = ""  # empty = the declaring service's registry id

    def spec_line(self, default_service_id: str = "") -> str:
        """The 8-field ``INFRA_PROBE_SPECS`` line for this facet."""
        return "|".join(
            (
                self.name,
                self.kind,
                self.target,
                self.expected,
                self.severity,
                str(self.timeout_seconds),
                self.depends_on,
                self.service_id or default_service_id,
            )
        )


@dataclass(frozen=True)
class SignalFacet:
    """Watchdog signal classification (#425 T5 structured fields).

    ``tier`` in {minute, hour, day, month}; ``type`` in {alert, report}.
    For ``type="alert"`` the debounce fields are mandatory semantics (what
    distinguishes a real failure from a transient blip — #475/#531):
    ``consecutive_failures`` bad polls before firing, ``renotify_window_sec``
    between re-alerts on an active incident.
    """

    tier: str
    type: str
    consecutive_failures: int = 0
    renotify_window_sec: int = 0


@dataclass(frozen=True)
class BackupFacet:
    """Backup facts for the service's ``data_path`` (aligned with
    ``libs.backup_verification.BackupEntry``; service_id/data_path derive from
    the owning Deployer, so they are usually not repeated here).

    ``service_id``/``data_path`` overrides follow SecretsFacet's convention:
    set explicitly only when declaring on behalf of a surface whose identity
    differs from the owning Deployer's — e.g. bootstrap/1password and
    bootstrap/vault, which have no deploy.py of their own and are declared on
    the bootstrap plane's single declaration point (iac_runner's deploy.py).

    ``retention_days``/``rpo_hours``/``remote`` left at their zero-values mean
    "use the inventory defaults" (30d / 24h / r2 — see
    ``libs.backup_verification.INVENTORY_DEFAULTS``), mirroring how the deleted
    handwritten YAML's ``defaults:`` block worked.
    """

    method: str
    restore_command: str = ""
    remote: str = ""
    retention_days: int = 0
    rpo_hours: int = 0
    service_id: str = ""
    data_path: str = ""


@dataclass(frozen=True)
class SecretsFacet:
    """Vault self-refresh facts for one vault-agent surface the service owns.

    The vault self-refresh audit's inventory (formerly the handwritten
    ``vault-self-refresh-inventory.yaml`` SSOT, deleted) is DERIVED from these
    declarations by ``libs.vault_self_refresh_audit.load_inventory`` (#542).
    Everything derivable from the owning Deployer is NOT repeated here:

    - inventory ``id`` / ``project`` / ``dokploy_service`` derive from the
      declaring Deployer's registry id, ``project``, and ``service`` attrs;
    - ``vault_path_template`` derives as
      ``secret/data/{project}/{env}/{service}`` — the SAME (project, service)
      facts ``libs.env.get_secrets`` uses to build the deploy-side Vault path,
      so the audit expectation and the deployed path cannot drift (#531);
    - ``compose_path`` defaults to the Deployer's own ``compose_path``, and the
      vault-agent config / secrets template are its ``vault-agent.hcl`` /
      ``secrets.ctmpl`` siblings.

    ``service_id`` + ``compose_path`` are set explicitly only for a surface the
    service declares on behalf of a non-registry inventory id — e.g. the
    ephemeral ``finance_report/preview`` / ``truealpha/preview`` alias stacks
    (owned by the app Deployer, same vault-agent, preview compose dir) —
    mirroring ProbeFacet's out-of-registry ``service_id`` convention.

    ``mount_exempt_containers`` (#531) names app_containers that by design run
    WITHOUT the ``/secrets/.env`` mount (e.g. platform-prefect-worker), and
    ``optional_inert_fields`` (#526) names optional vault:true fields whose
    populated-ness is reported informationally — both formerly module-level
    constants in libs/vault_self_refresh_audit.py, now declared where the
    service lives.
    """

    vault_agent_container: str
    app_containers: tuple[str, ...]
    auth_method: str = "token"  # "token" | "approle"
    service_id: str = ""  # empty = the declaring service's registry id
    compose_path: str = ""  # empty = the declaring Deployer's compose_path
    vault_token_env_key: str = "VAULT_APP_TOKEN"
    rendered_secret_path: str = "/vault/secrets/.env"
    app_secret_mount_path: str = "/secrets/.env"
    max_rendered_secret_age_seconds: int = 900
    min_token_ttl_hours: int = 48
    mount_exempt_containers: tuple[str, ...] = ()
    optional_inert_fields: tuple[str, ...] = ()


@dataclass(frozen=True)
class Exemption:
    """An explicit, justified opt-out from one facet completeness check.

    ``check_id`` names the facet column being exempted (``probes`` | ``signals``
    | ``backup`` | ``secrets``), or a namespaced per-item check id for
    finer-grained opt-outs (e.g. ``required-env-drift:<VAR>`` — consumed by
    tools/validate_required_env.py as its drift allowlist, #542). ``reason``
    must say WHY the facet genuinely does not apply — an exemption is a
    conscious decision, not a forgotten gap.
    """

    check_id: str
    reason: str


# Constructor-name -> class map for the registry's AST facet reader. Only calls
# to these names (bare or attribute-qualified) are evaluated.
FACET_CLASSES: dict[str, type] = {
    "ProbeFacet": ProbeFacet,
    "SignalFacet": SignalFacet,
    "BackupFacet": BackupFacet,
    "SecretsFacet": SecretsFacet,
    "Exemption": Exemption,
}
