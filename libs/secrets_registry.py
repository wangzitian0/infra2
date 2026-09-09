"""Which manifests describe each deployed service (plan PR-D/E).

One registry for everything derived from a service's environment manifest: the Vault
Agent template and policy (tools/secrets_render.py), the secret supply on deploy
(libs/secrets_supply.py) and the daily reconcile (tools/secrets_reconcile.py).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from infra2_sdk.runtime.config_schema import EnvironmentField, EnvironmentManifest

from libs import app_manifests
from libs.app_manifests import CACHE_DIR  # noqa: F401  (re-exported)

ROOT = Path(__file__).resolve().parent.parent
ENVIRONMENTS = ("production", "staging")


@dataclass(frozen=True)
class Service:
    directory: str
    project: str
    service: str
    manifests: tuple[str, ...]
    source_env: str | None = None
    exclude_groups: tuple[str, ...] = ()
    # Variables the stack supplies another way (a preview's ephemeral database DSN).
    exclude_envs: tuple[str, ...] = ()
    # Fixed environments the service deploys to; the reconcile walks these.
    environments: tuple[str, ...] = ENVIRONMENTS
    # Store keys an operator task reads directly and the Vault Agent template never
    # renders (they must not reach the container). Declared here so the reconcile can
    # tell "documented, ops-only" from "nobody knows what this is", and so the prune
    # keeps them. Each entry names its reader.
    store_only_keys: tuple[str, ...] = ()
    # Hand-written files stay authoritative until the service is migrated: while
    # ``generated`` is False the manifest is still rendered and gated (so it cannot
    # rot), but the files on disk are neither compared nor rewritten.
    generated: bool = True
    # The IaC runner's policy is its *writer* identity (create/update on every project),
    # a bootstrap concern that no manifest describes; its template is still generated.
    policy: bool = True

    @property
    def id(self) -> str:
        return f"{self.project}/{self.service}"

    @property
    def preview(self) -> bool:
        return self.source_env is not None


SERVICES: tuple[Service, ...] = (
    Service(
        "bootstrap/06.iac_runner",
        "bootstrap",
        "iac_runner",
        ("bootstrap/06.iac_runner/env.manifest.json",),
        policy=False,
        environments=("production",),
    ),
    Service(
        "platform/01.postgres",
        "platform",
        "postgres",
        ("platform/01.postgres/env.manifest.json",),
    ),
    Service(
        "platform/02.redis",
        "platform",
        "redis",
        ("platform/02.redis/env.manifest.json",),
    ),
    Service(
        "platform/03.minio",
        "platform",
        "minio",
        ("platform/03.minio/env.manifest.json",),
    ),
    Service(
        "platform/10.authentik",
        "platform",
        "authentik",
        ("platform/10.authentik/env.manifest.json",),
        # platform/10.authentik/shared_tasks.py reads root_token for the admin API; it is
        # an operator credential and deliberately never rendered into the container.
        store_only_keys=("root_token",),
    ),
    Service(
        "platform/12.alerting",
        "platform",
        "alerting",
        ("platform/12.alerting/env.manifest.json",),
    ),
    Service(
        "platform/23.prefect",
        "platform",
        "prefect",
        ("platform/23.prefect/env.manifest.json",),
    ),
    Service(
        "platform/24.openpanel",
        "platform",
        "openpanel",
        ("platform/24.openpanel/env.manifest.json",),
    ),
    Service(
        "finance_report/finance_report/01.postgres",
        "finance_report",
        "postgres",
        ("finance_report/finance_report/01.postgres/env.manifest.json",),
    ),
    Service(
        "finance_report/finance_report/02.redis",
        "finance_report",
        "redis",
        ("finance_report/finance_report/02.redis/env.manifest.json",),
    ),
    Service(
        "truealpha/truealpha/01.postgres",
        "truealpha",
        "postgres",
        ("truealpha/truealpha/01.postgres/env.manifest.json",),
    ),
    # App stacks: the application's own manifest (checked out through the submodule at the
    # deployed commit) plus stack-only additions kept here. Environment-specific config
    # (S3 endpoint, rate limits, telemetry) lives in the compose file / Deployer, not in
    # the template.
    Service(
        "finance_report/finance_report/10.app",
        "finance_report",
        "app",
        ("repos/finance_report/common/runtime/required-env.generated.json",),
        # _ensure_minio_bucket reads the bucket name back to decide whether the scoped
        # user already exists; the compose supplies the same name to the container.
        store_only_keys=("S3_BUCKET",),
    ),
    Service(
        "finance_report/finance_report/preview",
        "finance_report",
        "app",
        ("repos/finance_report/common/runtime/required-env.generated.json",),
        source_env="staging",
        exclude_envs=("DATABASE_URL", "REDIS_URL"),
    ),
    Service(
        "truealpha/truealpha/10.app",
        "truealpha",
        "app",
        (
            "repos/truealpha/apps/app-web/required-env.manifest.json",
            "repos/truealpha/apps/llm-service/required-env.generated.json",
            "truealpha/truealpha/10.app/env.manifest.json",
        ),
        # same bucket-provisioning read as finance_report/app
        store_only_keys=("S3_BUCKET",),
    ),
    Service(
        "truealpha/truealpha/preview",
        "truealpha",
        "app",
        (
            "repos/truealpha/apps/app-web/required-env.manifest.json",
            "repos/truealpha/apps/llm-service/required-env.generated.json",
            "truealpha/truealpha/10.app/env.manifest.json",
        ),
        source_env="staging",
        exclude_envs=("DATABASE_URL", "MIGRATIONS_DATABASE_URL"),
    ),
    Service(
        "truealpha/truealpha/20.data_engine",
        "truealpha",
        "data_engine",
        ("repos/truealpha/apps/data-engine/required-env.generated.json",),
        # The deploy identity the Deployer reads at compose time (pin_release writes the
        # digest; the release id and the capture approval are governance values). They
        # move to the release/decision plane with truealpha#781 and this line goes away.
        # S3_BUCKET joins them for a different reason: the template stopped rendering it
        # (the bucket reaches the container another way) but
        # DataEngineDeployer._REQUIRED_SECRET_KEYS still reads it from this path and
        # fails the deploy when it is absent — so a prune that removed it would break
        # the next data-engine deploy, not the running one.
        store_only_keys=(
            "CAPTURE_APPROVED_BY",
            "DATA_ENGINE_IMAGE_DIGEST",
            "GIT_COMMIT_SHA",
            "RELEASE_MANIFEST_ID",
            "S3_BUCKET",
        ),
    ),
)


def manifest_file(path: str, *, root: Path = ROOT) -> Path:
    """The submodule checkout when present, else the cache libs/app_manifests fills."""
    return app_manifests.manifest_file(path, root=root)


def load_manifest(path: str, *, root: Path = ROOT, fetch=None) -> EnvironmentManifest:
    """Read one manifest; an app manifest absent from both checkout and cache is fetched
    at the submodule's pinned commit first (CI and the iac-runner have no submodules)."""
    file = manifest_file(path, root=root)
    if not file.exists() and path.startswith("repos/"):
        app_manifests.ensure_present(
            [path], root=root, **({"fetch": fetch} if fetch else {})
        )
        file = manifest_file(path, root=root)
    return EnvironmentManifest.from_dict(json.loads(file.read_text(encoding="utf-8")))


_RENDER_KEYS = (
    "source",
    "provided_by",
    "composed_from",
    "store_key",
    "empty_ok",
    "scope",
)


def merged_manifest(service: Service, *, root: Path = ROOT) -> EnvironmentManifest:
    """Union of a service's manifests.

    Two apps in one stack may declare the same variable (both truealpha apps read
    DATABASE_URL); they must agree on how it is produced and rendered, and the first
    declaration wins for the rest (requiredness, description).
    """
    fields: dict[str, EnvironmentField] = {}
    for path in service.manifests:
        for entry in load_manifest(path, root=root).fields:
            if entry.env in service.exclude_envs:
                continue
            existing = fields.get(entry.env)
            if existing is not None:
                for key in _RENDER_KEYS:
                    if getattr(existing, key) != getattr(entry, key):
                        raise ValueError(
                            f"{service.directory}: {entry.env}.{key} declared differently in {path}"
                        )
                continue
            fields[entry.env] = entry
    return EnvironmentManifest(source=service.id, fields=tuple(fields.values()))


def lookup(project: str, service: str, *, preview: bool = False) -> Service | None:
    """The registered service for a Deployer (fixed environments) or a preview stack."""
    for candidate in SERVICES:
        if (
            candidate.project == project
            and candidate.service == service
            and candidate.preview == preview
        ):
            return candidate
    return None


def store_keys(service: Service, *, root: Path = ROOT) -> frozenset[str]:
    """Every key this service's Vault path is allowed to hold.

    The manifest's own store-backed fields (what the Vault Agent template renders) plus
    the declared operator-only keys. A value a *consumer* composes from this service —
    ``provided_by: "platform/postgres:root_password"`` — is a store-backed field of the
    provider's own manifest, so it is already in this set on the provider's path.
    """
    manifest = merged_manifest(service, root=root)
    return frozenset(
        {field.key for field in manifest.store_backed} | set(service.store_only_keys)
    )
