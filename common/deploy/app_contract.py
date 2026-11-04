"""The part of an App env manifest that infra2 acts on (infra2#1320).

infra2 turns an App manifest into three things: a Vault Agent template and policy rendered
from the deployed release manifest merged with the stack manifest, the Vault values it
supplies at deploy time, and the compose environment (release and decision fields, and the
hand-written compose file). A field is in the contract when infra2 must supply it: its
``source`` is not ``code``, the template renders it, or the image refuses to boot without
it (``required``, or ``injected`` under ``assert_environment(require_injected=True)``). A
``code`` field that none of these holds is a default inside the image, and infra2 reads
none of it.
"""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from infra2_sdk.deploy import (
    PRODUCTION_DEPLOY_BUDGET_SECONDS,
    PRODUCTION_REQUEST_MAX_AGE_SECONDS,
    VERSION_KEYS,
    DeployOperation,
    DeployRequest,
    DeployType,
    default_http_get,
    parse_sender_deadline,
    production_request_accept_until,
    validate_evidence_urls,
    verify_production_evidence,
)
from infra2_sdk.manifests import (
    FORBIDDEN_HOOK_ENV_NAMES,
    FORBIDDEN_HOOK_ENV_PREFIXES,
    HOOK_SLOTS,
    HookRunResult,
    HookSlotSpec,
    RuntimeHooksManifest,
    build_hook_env,
    load as load_sdk_manifest,
    load_hooks_manifest,
    run_hook_slot,
)
from infra2_sdk.refs import (
    ResolvedRef,
    next_patch_tag,
    resolve_image_ref,
)
from infra2_sdk.runtime.config_schema import EnvironmentField, EnvironmentManifest
from infra2_sdk.secrets import render_agent_policy, render_agent_template

from common.core.constants import APP_SOURCES, REPO_ROOT
from common.core.fetch_app_manifests import http_get
from common.core.registry import domain_for_service
from common.deploy.release_markers import PRODUCTION_MARKER_PREFIX
from common.security.registry import _RENDER_KEYS, SERVICES, Service

ROOT = REPO_ROOT
FIXED_DEPLOY_TYPES = frozenset(
    {"staging", "prod", DeployType.STAGING, DeployType.PRODUCTION}
)
APP_HOOKS_MANIFEST_PATH = "runtime-hooks.manifest.json"
ContractReader = Callable[[str, str], str]

#: The attributes of a field that change what infra2 renders, supplies or composes.
CONTRACT_ATTRIBUTES: tuple[str, ...] = (
    "env",
    "source",
    "scope",
    "provided_by",
    "composed_from",
    "store_key",
    "empty_ok",
    "required",
    "mirror_to_1password",
    "group",
    "injected",
    "aliases",
)
#: The attributes that no infra2 code path reads when it renders, supplies or composes.
#: Together with CONTRACT_ATTRIBUTES they name every attribute of the SDK field. A test
#: fails when a new SDK version adds an attribute that is in neither tuple.
IGNORED_ATTRIBUTES: tuple[str, ...] = (
    "field",
    "has_default",
    "sensitive",
    "description",
    "ci",
)

#: Upstream release manifest paths in each App repository, paired with the infra2 service id.
APP_UPSTREAM_MANIFESTS: dict[str, tuple[tuple[str, str], ...]] = {
    "finance_report": (
        ("finance_report/app", "common/runtime/required-env.generated.json"),
    ),
    "truealpha": (
        ("truealpha/app", "apps/app-web/required-env.manifest.json"),
        ("truealpha/app", "apps/llm-service/required-env.generated.json"),
        ("truealpha/data_engine", "apps/data-engine/required-env.generated.json"),
    ),
}

#: Environment name -> the contract attributes of the field.
Contract = dict[str, dict[str, object]]

_POLICY_PATH_RE = re.compile(r'^path\s+"([^"]+)"', re.MULTILINE)
_ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


@dataclass(frozen=True)
class Gap:
    """One environment variable that a release needs and infra2 does not supply."""

    env: str
    detail: str

    def __str__(self) -> str:
        return f"{self.env}: {self.detail}"


def parse_manifest(body: str | bytes, *, label: str) -> EnvironmentManifest:
    """Parse and validate one App environment manifest.

    Raises ``ValueError`` when ``body`` is not valid manifest JSON or declares no fields.
    """
    try:
        manifest = EnvironmentManifest.from_dict(json.loads(body))
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise ValueError(f"{label} is not an App env manifest: {exc!r}") from None
    if not manifest.fields:
        raise ValueError(f"{label} is not an App env manifest: it declares no field")
    for field in manifest.fields:
        if not _ENV_NAME_RE.fullmatch(field.env):
            raise ValueError(
                f"{label} declares invalid environment variable name {field.env!r}"
            )
    return manifest


def parse_contract(body: str | bytes, *, label: str) -> Contract:
    """The contract of one manifest. ``label`` names the file in an error.

    A body that is not a manifest, or declares no field, raises ``ValueError``. An empty
    contract from a truncated file would otherwise ask for nothing and pass.
    """
    manifest = parse_manifest(body, label=label)
    return {
        field.env: {name: _attribute(field, name) for name in CONTRACT_ATTRIBUTES}
        for field in manifest.fields
        if _supplied_by_infra2(field)
    }


def parse_compose_contract(body: str | bytes, *, label: str) -> Contract:
    """The non-Vault compose contract of one manifest.

    Vault-rendered fields (``source`` in ``("human", "runtime")`` or ``composed_from``) are
    rendered into the Vault Agent template from the release manifest (#1320). Fields that the
    Vault template does not render (``release``, ``decision``, or ``required``/``injected``
    ``code`` fields) must be supplied by the stack compose file and deployer at ``iac_ref``.
    """
    manifest = parse_manifest(body, label=label)
    return {
        field.env: {name: _attribute(field, name) for name in CONTRACT_ATTRIBUTES}
        for field in manifest.fields
        if _supplied_by_infra2(field) and not field.rendered
    }


def _supplied_by_infra2(field: EnvironmentField) -> bool:
    return field.source != "code" or field.rendered or field.required or field.injected


def _attribute(field: EnvironmentField, name: str) -> object:
    value = getattr(field, name)
    return tuple(sorted(value)) if name == "aliases" else value


def unmet(release: Contract, provided: Sequence[Contract]) -> list[Gap]:
    """What ``release`` needs and none of the ``provided`` contracts supplies.

    ``provided`` holds one contract per manifest of the stack. A variable is met when one of
    them declares it with the same attributes. ``provided`` may supply more than the release
    needs: an older release deploys on a newer infra2, and a field that a release dropped
    stays until no release needs it.
    """
    gaps = []
    for env, wanted in sorted(release.items()):
        declared = [contract[env] for contract in provided if env in contract]
        if wanted in declared:
            continue
        if not declared:
            gaps.append(Gap(env, f"missing (source {wanted['source']})"))
            continue
        have = declared[0]
        changes = "; ".join(
            f"{name}: release {wanted[name]!r}, infra2 {have[name]!r}"
            for name in CONTRACT_ATTRIBUTES
            if wanted[name] != have[name]
        )
        gaps.append(Gap(env, f"differs ({changes})"))
    return gaps


def service_for_id(service_id: str, *, preview: bool = False) -> Service:
    """Return the registered ``Service`` for ``service_id`` and ``preview`` mode."""
    for service in SERVICES:
        if service.id == service_id and service.preview == preview:
            return service
    raise ValueError(f"unknown service {service_id!r} (preview={preview})")


def service_stack_manifest(service_id: str, *, preview: bool = False) -> str:
    """Return the stack manifest path for ``service_id``."""
    return service_for_id(service_id, preview=preview).manifests[0]


def service_vault_policy(service_id: str, *, preview: bool = False) -> str:
    """Return the committed Vault policy path for ``service_id``."""
    service = service_for_id(service_id, preview=preview)
    return f"{service.directory}/vault-policy.hcl"


def policy_paths(policy_text: str) -> set[str]:
    """Return the set of ``path "..."`` targets declared in a Vault HCL policy."""
    return set(_POLICY_PATH_RE.findall(policy_text))


def render_release_template(
    service_id: str,
    release_bodies: Sequence[tuple[str, str | bytes]],
    *,
    stack_manifest_body: str | bytes | None = None,
    committed_policy: str | None = None,
    preview: bool = False,
    root: Path = ROOT,
) -> tuple[str, str]:
    """Render the Vault Agent template and policy for ``service_id`` from release manifests.

    Merges the upstream ``release_bodies`` (``(label, body)`` pairs) with the stack baseline
    manifest so Vault-backed fields added or tightened in the release render directly into
    ``secrets.ctmpl`` (#1320). Verifies that every Vault path in the rendered policy is
    granted by the stack's committed ``vault-policy.hcl``.
    """
    service = service_for_id(service_id, preview=preview)
    if not release_bodies:
        raise ValueError(f"no release manifests provided for {service_id}")

    release_fields: dict[str, EnvironmentField] = {}
    for label, body in release_bodies:
        manifest = parse_manifest(body, label=label)
        for entry in manifest.fields:
            if entry.env in service.exclude_envs:
                continue
            existing = release_fields.get(entry.env)
            if existing is not None:
                for key in _RENDER_KEYS:
                    if getattr(existing, key) != getattr(entry, key):
                        raise ValueError(
                            f"{service.directory}: {entry.env}.{key} declared differently in {label}"
                        )
                release_fields[entry.env] = replace(
                    existing,
                    required=existing.required or entry.required,
                    injected=existing.injected or entry.injected,
                )
                continue
            release_fields[entry.env] = entry

    stack_path = service.manifests[0]
    raw_stack = (
        stack_manifest_body
        if stack_manifest_body is not None
        else load_sdk_manifest(stack_path, root=root)
    )
    stack_manifest = (
        raw_stack
        if isinstance(raw_stack, EnvironmentManifest)
        else parse_manifest(raw_stack, label=stack_path)
    )

    merged_fields: dict[str, EnvironmentField] = {}
    for entry in stack_manifest.fields:
        if entry.env in service.exclude_envs:
            continue
        merged_fields[entry.env] = release_fields.get(entry.env, entry)
    for env, entry in release_fields.items():
        if env not in merged_fields:
            merged_fields[env] = entry

    merged = EnvironmentManifest(
        source=service.id,
        fields=tuple(merged_fields.values()),
    )
    template = render_agent_template(
        merged,
        project=service.project,
        service=service.service,
        source_env=service.source_env,
        exclude_groups=service.exclude_groups,
    )
    policy = render_agent_policy(
        merged,
        project=service.project,
        service=service.service,
        source_env=service.source_env,
    )

    policy_file = f"{service.directory}/vault-policy.hcl"
    raw_policy = (
        committed_policy
        if committed_policy is not None
        else (root / policy_file).read_text(encoding="utf-8")
    )
    granted = policy_paths(raw_policy)
    if not granted:
        raise ValueError(f"{policy_file} is not a valid Vault policy")
    required = policy_paths(policy)
    ungranted = sorted(required - granted)
    if ungranted:
        raise ValueError(
            f"release manifest for {service_id} requires Vault path "
            f"{', '.join(ungranted)} not granted by {policy_file}"
        )
    return template, policy


def _git_contract_reader(
    repo_root: str | Path, runner=subprocess.run
) -> ContractReader:
    root = Path(repo_root)

    def read(ref: str, path: str) -> str:
        if ref == "main":
            try:
                return (root / path).read_text(encoding="utf-8")
            except OSError as exc:
                raise ValueError(f"infra2 main has no readable {path}: {exc}") from None
        try:
            shown = runner(
                ["git", "-C", str(root), "show", f"{ref}:{path}"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=True,
            )
        except (OSError, subprocess.SubprocessError):
            shown = None
        if shown is None or not isinstance(shown.stdout, str) or not shown.stdout:
            raise ValueError(
                f"infra2 release {ref} has no {path}: the release predates the file, "
                "or its tag is not fetched"
            )
        return shown.stdout

    return read


def _contract_gaps(
    read: ContractReader, ref: str, release: dict[tuple[str, str], Contract]
) -> list[tuple[str, Gap]]:
    """What the release needs in compose and the infra2 release ``ref`` does not supply."""
    gaps = []
    for (service_id, upstream), wanted in release.items():
        stack_path = service_stack_manifest(service_id)
        provided = [
            parse_contract(read(ref, stack_path), label=f"{stack_path} at infra2 {ref}")
        ]
        gaps.extend(
            (f"{stack_path} ({upstream})", gap) for gap in unmet(wanted, provided)
        )
    return gaps


def _is_production_type(deploy_type: str | DeployType) -> bool:
    return deploy_type in ("prod", "production", DeployType.PRODUCTION)


def _gap_remedy(
    deploy_type: str | DeployType | DeployRequest,
    *,
    read: ContractReader,
    iac_ref: str,
    release: dict[tuple[str, str], Contract],
) -> str:
    """The step that closes a non-Vault compose contract gap (#1320)."""
    effective_type = (
        deploy_type.deploy_type
        if isinstance(deploy_type, DeployRequest)
        else getattr(deploy_type, "deploy_type", deploy_type)
    )
    is_prod = _is_production_type(effective_type)
    if iac_ref != "main":
        try:
            main_has_it = not _contract_gaps(read, "main", release)
        except ValueError:
            main_has_it = False
        if main_has_it:
            if is_prod:
                return (
                    f"infra2 main already supplies it, so no sync is needed: production runs at "
                    f"{PRODUCTION_MARKER_PREFIX}{iac_ref}, which predates main. Cut an infra2 "
                    "release from main if none is newer than that, then promote infra2 to "
                    "production (Reconcile IaC Inputs with promote_prod, owner-reserved), "
                    "then deploy again"
                )
            return (
                f"infra2 main already supplies it, so no sync is needed: staging runs at {iac_ref}, "
                "which predates main. Cut an infra2 release from main, then deploy again"
            )
    return (
        "add the variable to the stack compose file and env.manifest.json in infra2, "
        "merge, cut an infra2 release"
        f"{' and promote it to production' if is_prod else ''}"
        ", then deploy again"
    )


def verify_app_manifests(
    request_or_service: DeployRequest | Any,
    *,
    source_sha: str | None = None,
    source_repository: str | None = None,
    deploy_type: str | DeployType = "staging",
    operation: str | DeployOperation = "deploy",
    repo_root: str | Path = ROOT,
    iac_ref: str = "main",
    manifest_fetcher: Callable[[str], bytes] | None = None,
    contract_reader: ContractReader | None = None,
    runner=subprocess.run,
) -> dict[str, str]:
    """Render each service's Vault Agent template from the release manifest and check the stack contract (#1320).

    Vault-backed fields from the release manifest are merged with the stack baseline manifest
    at ``iac_ref`` to render ``secrets.ctmpl``, gated by the stack's ``vault-policy.hcl``.
    Non-Vault fields (``release``, ``decision``, and ``required``/``injected`` ``code`` fields)
    must be supplied by the stack manifest at ``iac_ref``.
    """
    if hasattr(request_or_service, "service") and hasattr(
        request_or_service, "source_sha"
    ):
        service = str(request_or_service.service)
        sha = str(request_or_service.source_sha)
        repo_slug = str(
            getattr(request_or_service, "source_repository", None)
            or APP_SOURCES.get(service, "")
        )
        eff_deploy_type = getattr(request_or_service, "deploy_type", deploy_type)
        eff_operation = getattr(request_or_service, "operation", operation)
    else:
        service = str(request_or_service)
        sha = str(source_sha or "")
        repo_slug = str(source_repository or APP_SOURCES.get(service, ""))
        eff_deploy_type = deploy_type
        eff_operation = operation

    if eff_operation in ("remove", DeployOperation.REMOVE):
        return {}
    if not sha:
        raise ValueError(f"source_sha is required to verify manifests for {service!r}")
    if not repo_slug:
        app_prefix = service.split("/", 1)[0]
        repo_slug = APP_SOURCES.get(f"{app_prefix}/app", "")
    if not repo_slug:
        raise ValueError(f"no source repository registered for {service!r}")

    read = contract_reader or _git_contract_reader(repo_root, runner)
    app_name = service.split("/", 1)[0]
    upstreams = APP_UPSTREAM_MANIFESTS.get(app_name)
    if not upstreams:
        raise ValueError(f"no upstream manifests registered for {app_name!r}")

    fetch = manifest_fetcher or http_get
    compose_release: dict[tuple[str, str], Contract] = {}
    bodies_by_service: dict[str, list[tuple[str, bytes]]] = {}
    for service_id, upstream in upstreams:
        url = f"https://raw.githubusercontent.com/{repo_slug}/{sha}/{upstream}"
        try:
            body = fetch(url)
        except SystemExit as exc:
            raise ValueError(
                f"failed to fetch manifest {upstream} from {url}: {exc}"
            ) from None
        label = f"{upstream} at {url}"
        compose_release[(service_id, upstream)] = parse_compose_contract(
            body, label=label
        )
        bodies_by_service.setdefault(service_id, []).append((label, body))

    gaps = _contract_gaps(read, iac_ref, compose_release)
    if gaps:
        lines = [
            f"release manifests at {sha[:12]} need what infra2 release {iac_ref} "
            "does not supply:"
        ]
        lines.extend(f"  {path}: {gap}" for path, gap in gaps)
        lines.append(
            _gap_remedy(
                eff_deploy_type, read=read, iac_ref=iac_ref, release=compose_release
            )
        )
        raise ValueError("\n".join(lines))

    preview = eff_deploy_type not in FIXED_DEPLOY_TYPES
    rendered_templates: dict[str, str] = {}
    for service_id, service_bodies in bodies_by_service.items():
        is_preview = preview and service_id == service
        stack_path = service_stack_manifest(service_id, preview=is_preview)
        policy_path = service_vault_policy(service_id, preview=is_preview)
        stack_body = read(iac_ref, stack_path)
        policy_body = read(iac_ref, policy_path)
        template, _policy = render_release_template(
            service_id,
            service_bodies,
            stack_manifest_body=stack_body,
            committed_policy=policy_body,
            preview=is_preview,
        )
        rendered_templates[service_id] = template
    return rendered_templates


def encode_rendered_templates(
    templates: Mapping[str, str] | str | None,
    service: str | None = None,
) -> str:
    """Base64-encode a rendered Vault Agent template for ``VAULT_SECRETS_TEMPLATE``."""
    if templates is None:
        return ""
    if isinstance(templates, Mapping):
        if service is not None:
            raw = templates.get(service, "")
        elif len(templates) == 1:
            raw = next(iter(templates.values()))
        else:
            raw = ""
    else:
        raw = str(templates)
    if not raw:
        return ""
    return base64.b64encode(raw.encode("utf-8")).decode("ascii")


def set_template_env(template: str | None) -> None:
    """Export or clear ``VAULT_SECRETS_TEMPLATE`` in ``os.environ`` from raw template text."""
    encoded = encode_rendered_templates(template)
    if encoded:
        os.environ["VAULT_SECRETS_TEMPLATE"] = encoded
    else:
        os.environ.pop("VAULT_SECRETS_TEMPLATE", None)


def fetch_hooks_manifest(
    service: str,
    source_sha: str,
    *,
    source_repository: str | None = None,
    hooks_path: str = APP_HOOKS_MANIFEST_PATH,
    manifest_fetcher: Callable[[str], bytes] | None = None,
) -> RuntimeHooksManifest:
    """Fetch and validate ``runtime-hooks.manifest.json`` for ``service`` at ``source_sha``."""
    repo_slug = source_repository or APP_SOURCES.get(service, "")
    if not repo_slug:
        app_prefix = service.split("/", 1)[0]
        repo_slug = APP_SOURCES.get(f"{app_prefix}/app", "")
    if not repo_slug:
        raise ValueError(f"no source repository registered for {service!r}")
    url = f"https://raw.githubusercontent.com/{repo_slug}/{source_sha}/{hooks_path}"
    fetch = manifest_fetcher or http_get
    try:
        body = fetch(url)
    except SystemExit as exc:
        raise ValueError(
            f"failed to fetch hooks manifest {hooks_path} from {url}: {exc}"
        ) from None
    raw = json.loads(body.decode("utf-8") if isinstance(body, bytes) else body)
    return RuntimeHooksManifest.from_dict(raw)


def execute_app_hook_slot(
    manifest_or_path: RuntimeHooksManifest | str | Path,
    slot: str,
    *,
    app_url: str,
    environment: str = "staging",
    version_ref: str = "main",
    cwd: Path | None = None,
    ambient_env: Mapping[str, str] | None = None,
    result_path: Path | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> HookRunResult:
    """Load (if needed) and execute one verification hook slot via ``infra2_sdk.manifests``."""
    if slot not in HOOK_SLOTS:
        raise ValueError(f"unknown hook slot {slot!r}; allowed: {list(HOOK_SLOTS)}")
    manifest = (
        manifest_or_path
        if isinstance(manifest_or_path, RuntimeHooksManifest)
        else load_hooks_manifest(manifest_or_path, root=cwd)
    )
    slot_spec: HookSlotSpec = manifest.slots[slot]
    for name in (*slot_spec.required_env, *slot_spec.optional_env):
        if name in FORBIDDEN_HOOK_ENV_NAMES or name.startswith(
            FORBIDDEN_HOOK_ENV_PREFIXES
        ):
            raise ValueError(f"forbidden secret in hook slot {slot!r}: {name}")
    _ = build_hook_env(
        slot_spec,
        slot=slot,
        app_url=app_url,
        environment=environment,
        version_ref=version_ref,
        artifacts_dir=slot_spec.artifacts_dir,
        ambient_env=os.environ if ambient_env is None else ambient_env,
    )
    return run_hook_slot(
        manifest,
        slot,
        app_url=app_url,
        environment=environment,
        version_ref=version_ref,
        cwd=cwd,
        ambient_env=ambient_env,
        result_path=result_path,
        runner=runner,
    )


def _git_url(repository: str) -> str:
    return f"https://github.com/{repository}.git"


@dataclass(frozen=True)
class ServingRelease:
    """Authoritative deployed/serving state for a fixed app environment (#588)."""

    service: str
    environment: str
    compose_id: str
    image_tag: str
    source_sha: str
    iac_ref: str
    public_url: str
    public_version: str
    healthy: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "service": self.service,
            "environment": self.environment,
            "compose_id": self.compose_id,
            "image_tag": self.image_tag,
            "source_sha": self.source_sha,
            "iac_ref": self.iac_ref,
            "public_url": self.public_url,
            "public_version": self.public_version,
            "healthy": self.healthy,
        }


def query_serving_release(
    service: str,
    environment: str,
    *,
    domain: str,
    client: object | None = None,
    http_get_fn: Callable[[str], tuple[int, str]] | None = None,
    resolve_image: Callable[..., ResolvedRef] = resolve_image_ref,
) -> ServingRelease:
    """Query infra2's authoritative serving state for ``(service, environment)`` (#588)."""
    from common.deploy.env_config import app_compose_env_config
    from common.deploy.env_util import _parse_env_text

    expected_source = APP_SOURCES.get(service)
    if expected_source is None:
        raise ValueError(
            f"service {service!r} is not an app service with serving state"
        )
    norm_env = "prod" if environment in ("prod", "production") else environment
    if norm_env not in ("staging", "prod"):
        raise ValueError(
            f"serving state query supports fixed environments ('staging' or 'prod'), got {environment!r}"
        )
    effective_domain = domain_for_service(service) or domain
    if not effective_domain or any(c.isspace() for c in effective_domain):
        raise ValueError("domain must be non-empty and contain no whitespace")
    cfg = app_compose_env_config(service, norm_env)
    compose_id = cfg.compose_id or ""
    public_url = f"{cfg.app_url(domain=effective_domain).rstrip('/')}/api/health"

    env_map: dict[str, str] = {}
    if client is not None and compose_id and hasattr(client, "get_compose_env"):
        env_text = client.get_compose_env(compose_id) or ""
        env_map = _parse_env_text(env_text)

    getter = http_get_fn or default_http_get()
    public_version = ""
    healthy = False
    try:
        status_code, body = getter(public_url)
        if status_code == 200 and body:
            parsed = json.loads(body)
            if isinstance(parsed, Mapping):
                healthy = str(parsed.get("status") or "ok").lower() in (
                    "ok",
                    "healthy",
                )
                for key in VERSION_KEYS:
                    val = parsed.get(key)
                    if isinstance(val, str) and val.strip():
                        public_version = val.strip()
                        break
    except (OSError, ValueError):
        healthy = False

    image_tag = (
        env_map.get("IMAGE_TAG")
        or env_map.get("INFRA_SERVICE_VERSION")
        or public_version
    )
    if not image_tag:
        raise RuntimeError(
            f"cannot determine serving release for {service} ({norm_env}): "
            f"neither Dokploy compose {compose_id!r} nor {public_url} reported a version"
        )
    iac_ref = env_map.get("INFRA_IAC_REF", "")
    source_sha = ""
    raw_sha = env_map.get("GIT_COMMIT_SHA", "")
    if re.fullmatch(r"[0-9a-f]{40}", raw_sha):
        source_sha = raw_sha
    else:
        try:
            resolved = resolve_image(image_tag, repo=_git_url(expected_source))
            source_sha = resolved.sha.lower()
        except Exception:  # noqa: BLE001
            source_sha = raw_sha
    return ServingRelease(
        service=service,
        environment="production" if norm_env == "prod" else "staging",
        compose_id=compose_id,
        image_tag=image_tag,
        source_sha=source_sha,
        iac_ref=iac_ref,
        public_url=public_url,
        public_version=public_version or image_tag,
        healthy=healthy,
    )


def validate_app_release_evidence(
    request: DeployRequest,
    *,
    verify_prod: bool = False,
    production_evidence_verifier: Callable[[DeployRequest], None] | None = None,
) -> dict[str, int]:
    """Validate a ``DeployRequest`` evidence envelope against its registered App source."""
    expected_source = APP_SOURCES.get(request.service)
    if expected_source is None:
        raise ValueError(f"no source repository registered for {request.service!r}")
    validate_evidence_urls(request, expected_repository=expected_source)
    if verify_prod and request.deploy_type == DeployType.PRODUCTION:
        verifier = production_evidence_verifier or verify_production_evidence
        verifier(request)
    return {
        "max_age_seconds": PRODUCTION_REQUEST_MAX_AGE_SECONDS,
        "deploy_budget_seconds": PRODUCTION_DEPLOY_BUDGET_SECONDS,
    }


def next_app_release_tag(
    existing_tags: Sequence[str],
    *,
    initial: str = "v0.1.0",
) -> str:
    """Compute the next ``vX.Y.(Z+1)`` App release tag via ``infra2_sdk.refs.next_patch_tag``."""
    return next_patch_tag(existing_tags, initial=initial)


__all__ = [
    "APP_HOOKS_MANIFEST_PATH",
    "APP_UPSTREAM_MANIFESTS",
    "CONTRACT_ATTRIBUTES",
    "Contract",
    "ContractReader",
    "FIXED_DEPLOY_TYPES",
    "Gap",
    "HOOK_SLOTS",
    "HookRunResult",
    "HookSlotSpec",
    "IGNORED_ATTRIBUTES",
    "PRODUCTION_DEPLOY_BUDGET_SECONDS",
    "PRODUCTION_REQUEST_MAX_AGE_SECONDS",
    "RuntimeHooksManifest",
    "ServingRelease",
    "_contract_gaps",
    "_gap_remedy",
    "_git_contract_reader",
    "encode_rendered_templates",
    "execute_app_hook_slot",
    "fetch_hooks_manifest",
    "load_hooks_manifest",
    "next_app_release_tag",
    "parse_compose_contract",
    "parse_contract",
    "parse_manifest",
    "parse_sender_deadline",
    "policy_paths",
    "production_request_accept_until",
    "query_serving_release",
    "render_release_template",
    "run_hook_slot",
    "service_for_id",
    "service_stack_manifest",
    "service_vault_policy",
    "set_template_env",
    "unmet",
    "validate_app_release_evidence",
    "verify_app_manifests",
]
