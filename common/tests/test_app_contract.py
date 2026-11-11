"""Contract preflight, Vault Agent template rendering, and SDK hook scheduling tests (#1297, #1320, #1416)."""

from __future__ import annotations

import base64
import json
import os
import subprocess
from pathlib import Path

import pytest

from common.deploy import app_contract
from common.deploy.release_markers import newest_release_tag, production_marker
from infra2_sdk.deploy import DeployRequest
from infra2_sdk.manifests import HOOK_SLOTS, HookRunResult, RuntimeHooksManifest
from infra2_sdk.refs import ResolvedRef
from infra2_sdk.runtime.config_schema import EnvironmentField, EnvironmentManifest

TA_REPO = "wangzitian0/truealpha"
FR_REPO = "wangzitian0/finance_report"
RELEASE_SHA = "dd066f62" + "0" * 32
UPSTREAM_PATH = "apps/data-engine/required-env.generated.json"
APP_WEB_UPSTREAM = "apps/app-web/required-env.manifest.json"
LLM_UPSTREAM = "apps/llm-service/required-env.generated.json"
FR_UPSTREAM = "common/runtime/required-env.generated.json"
STACK_PATH = "truealpha/truealpha/20.data_engine/env.manifest.json"
APP_STACK_PATH = "truealpha/truealpha/10.app/env.manifest.json"
FR_STACK_PATH = "finance_report/finance_report/10.app/env.manifest.json"
RELEASE_URL = (
    f"https://raw.githubusercontent.com/{TA_REPO}/{RELEASE_SHA}/{UPSTREAM_PATH}"
)
APP_WEB_URL = (
    f"https://raw.githubusercontent.com/{TA_REPO}/{RELEASE_SHA}/{APP_WEB_UPSTREAM}"
)
LLM_URL = f"https://raw.githubusercontent.com/{TA_REPO}/{RELEASE_SHA}/{LLM_UPSTREAM}"
FR_RELEASE_URL = (
    f"https://raw.githubusercontent.com/{FR_REPO}/{RELEASE_SHA}/{FR_UPSTREAM}"
)

TA_APP_POLICY = (
    'path "secret/data/truealpha/{{env}}/app" { capabilities = ["read"] }\n'
    'path "secret/metadata/truealpha/{{env}}/app" { capabilities = ["read", "list"] }\n'
    'path "secret/data/truealpha/staging/app" { capabilities = ["read"] }\n'
    'path "secret/metadata/truealpha/staging/app" { capabilities = ["read", "list"] }\n'
    'path "secret/data/truealpha/{{env}}/data_engine" { capabilities = ["read"] }\n'
    'path "secret/metadata/truealpha/{{env}}/data_engine" { capabilities = ["read", "list"] }\n'
    'path "auth/token/renew-self" { capabilities = ["update"] }\n'
    'path "auth/token/lookup-self" { capabilities = ["read"] }\n'
)
FR_POLICY = (
    'path "secret/data/finance_report/{{env}}/app" { capabilities = ["read"] }\n'
    'path "secret/metadata/finance_report/{{env}}/app" { capabilities = ["read", "list"] }\n'
    'path "secret/data/finance_report/staging/app" { capabilities = ["read"] }\n'
    'path "secret/metadata/finance_report/staging/app" { capabilities = ["read", "list"] }\n'
    'path "auth/token/renew-self" { capabilities = ["update"] }\n'
    'path "auth/token/lookup-self" { capabilities = ["read"] }\n'
)


def _field(env: str, source: str = "code", **overrides) -> EnvironmentField:
    return EnvironmentField(field=env.lower(), env=env, source=source, **overrides)


def _manifest(*fields: EnvironmentField) -> bytes:
    manifest = EnvironmentManifest(source=UPSTREAM_PATH, fields=tuple(fields))
    return (json.dumps(manifest.to_dict(), indent=2) + "\n").encode()


DATABASE = _field("DATABASE_URL", "human")
BEARER = _field("MCP_BEARER_TOKEN", "runtime", mirror_to_1password=True, sensitive=True)
DECISION_FLAG = _field("ENABLE_REALTIME_QUOTES", "decision")


def _git(root: Path, *args: str) -> None:
    command = ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*command, *args], check=True, capture_output=True)


def _infra2_checkout(
    root: Path,
    releases: list[tuple[bytes | None, tuple[str, ...]]],
    *,
    app_stack: bytes | None = None,
    policy: str = TA_APP_POLICY,
    annotated: tuple[str, ...] = (),
):
    """Build a git checkout whose commits carry the stack manifests and Vault policies."""
    _git(root, "init", "-q", "-b", "main")
    default_app_stack = app_stack if app_stack is not None else _manifest(DATABASE)
    for index, (stack_bytes, tags_here) in enumerate(releases):
        if stack_bytes is not None:
            for path, content in (
                (STACK_PATH, stack_bytes),
                (APP_STACK_PATH, default_app_stack),
                (FR_STACK_PATH, _manifest(DATABASE)),
            ):
                target = root / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
            for policy_path, text in (
                ("truealpha/truealpha/10.app/vault-policy.hcl", policy),
                ("truealpha/truealpha/preview/vault-policy.hcl", policy),
                ("truealpha/truealpha/20.data_engine/vault-policy.hcl", policy),
                ("finance_report/finance_report/10.app/vault-policy.hcl", FR_POLICY),
                ("finance_report/finance_report/preview/vault-policy.hcl", FR_POLICY),
            ):
                target = root / policy_path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text, encoding="utf-8")
        (root / "marker.txt").write_text(str(index), encoding="utf-8")
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "-m", f"release {index}")
        for tag in tags_here:
            _git(
                root,
                *(("tag", "-a", tag, "-m", tag) if tag in annotated else ("tag", tag)),
            )
    return root


def _request(*, production: bool = False, **overrides) -> dict:
    repo = str(overrides.get("source_repository", TA_REPO))
    base = {
        "contract_version": 1,
        "request_id": "req-12345678",
        "operation": "deploy",
        "service": "truealpha/app",
        "deploy_type": "prod" if production else "staging",
        "version_ref": "v0.2.13",
        "source_repository": repo,
        "source_sha": RELEASE_SHA,
        "evidence": {
            "source_run_url": f"https://github.com/{repo}/actions/runs/100",
            "source_run_id": "100",
            "staging_run_url": f"https://github.com/{repo}/actions/runs/101"
            if production
            else "",
            "reviewed_change_url": f"https://github.com/{repo}/pull/10"
            if production
            else "",
        },
    }
    base.update(overrides)
    return base


def _manifest_map(
    release: bytes,
    *,
    app_web: bytes | None = None,
    llm: bytes | None = None,
    fr: bytes | None = None,
) -> dict[str, bytes]:
    return {
        RELEASE_URL: release,
        APP_WEB_URL: app_web if app_web is not None else _manifest(DATABASE),
        LLM_URL: llm if llm is not None else _manifest(DATABASE),
        FR_RELEASE_URL: fr if fr is not None else _manifest(DATABASE),
    }


def _select_ref(root: Path, deploy_type: str) -> str:
    if deploy_type not in ("staging", "prod", "production"):
        return "main"
    if deploy_type in ("prod", "production"):
        return production_marker(repo_root=root)
    return newest_release_tag(repo_root=root)


def _preflight(
    root: Path,
    release: bytes,
    *,
    production: bool = False,
    app_web: bytes | None = None,
    llm: bytes | None = None,
    fr: bytes | None = None,
    **overrides,
) -> tuple[str, dict[str, str]]:
    bodies = _manifest_map(release, app_web=app_web, llm=llm, fr=fr)
    req_dict = _request(production=production, **overrides)
    request = DeployRequest.from_dict(req_dict)
    iac_ref = _select_ref(root, request.deploy_type.value)
    rendered = app_contract.verify_app_manifests(
        request,
        repo_root=root,
        iac_ref=iac_ref,
        manifest_fetcher=bodies.__getitem__,
    )
    return iac_ref, rendered


def test_preflight_ignores_a_release_change_in_code_fields(tmp_path: Path) -> None:
    """Staging v0.2.29 was refused because truealpha#1461 removed a ``code`` field."""
    root = _infra2_checkout(
        tmp_path,
        [(_manifest(DATABASE, _field("MOOMOO_MONTHLY_CALL_BUDGET")), ("v1.0.0",))],
    )
    release = _manifest(
        DATABASE,
        _field("MOOMOO_KLINE_QUOTA", description="renamed", group="Quota"),
    )
    iac_ref, _ = _preflight(root, release)
    assert iac_ref == "v1.0.0"


def test_preflight_passes_when_infra2_supplies_more_than_the_release_needs(
    tmp_path: Path,
) -> None:
    """Staging v0.2.28 predates a stack field. infra2 supplies it extra."""
    root = _infra2_checkout(
        tmp_path, [(_manifest(DATABASE, DECISION_FLAG), ("v1.0.0",))]
    )
    iac_ref, _ = _preflight(root, _manifest(DATABASE))
    assert iac_ref == "v1.0.0"


def test_replay_2026_10_19_sec_user_agent_renders_into_vault_template(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Replay of 2026-10-19 (#1320): truealpha#1382 tightened SEC_USER_AGENT in the release
    manifest without a manual sync in infra2. The Vault Agent template renders SEC_USER_AGENT
    directly from the deployed release manifest and set_template_env exports VAULT_SECRETS_TEMPLATE."""
    stack_before_sync = _manifest(
        DATABASE,
        _field("SEC_USER_AGENT", "human", empty_ok=True),
    )
    root = _infra2_checkout(tmp_path, [(stack_before_sync, ("v1.0.0",))])
    release_1382 = _manifest(
        DATABASE,
        _field("SEC_USER_AGENT", "human", empty_ok=False),
    )
    iac_ref, rendered = _preflight(root, release_1382)
    assert iac_ref == "v1.0.0"
    de_template = rendered["truealpha/data_engine"]
    assert 'SEC_USER_AGENT={{ printf "%q" .Data.data.SEC_USER_AGENT }}' in de_template

    monkeypatch.delenv("VAULT_SECRETS_TEMPLATE", raising=False)
    app_contract.set_template_env(de_template)
    raw_b64 = os.environ.get("VAULT_SECRETS_TEMPLATE", "")
    decoded = base64.b64decode(raw_b64).decode("utf-8")
    assert "SEC_USER_AGENT" in decoded
    app_contract.set_template_env(None)
    assert "VAULT_SECRETS_TEMPLATE" not in os.environ


@pytest.mark.parametrize(
    "service, repo, secret_name",
    [
        ("truealpha/app", TA_REPO, "TA_NEW_VAULT_SECRET"),
        ("finance_report/app", FR_REPO, "FR_NEW_VAULT_SECRET"),
    ],
)
def test_release_manifest_adding_vault_variable_renders_without_manual_sync(
    tmp_path: Path, service: str, repo: str, secret_name: str
) -> None:
    root = _infra2_checkout(tmp_path, [(_manifest(DATABASE), ("v1.0.0",))])
    added = _manifest(DATABASE, _field(secret_name, "human"))
    _, rendered = _preflight(
        root,
        _manifest(DATABASE),
        app_web=added if service == "truealpha/app" else None,
        fr=added if service == "finance_report/app" else None,
        service=service,
        source_repository=repo,
    )
    assert secret_name in rendered[service]


def test_preflight_refuses_when_release_manifest_needs_vault_path_outside_policy(
    tmp_path: Path,
) -> None:
    root = _infra2_checkout(tmp_path, [(_manifest(DATABASE), ("v1.0.0",))])
    redis_secret = _manifest(
        DATABASE,
        _field(
            "REDIS_PASSWORD",
            "runtime",
            provided_by="truealpha/redis:REDIS_PASSWORD",
        ),
    )
    with pytest.raises(
        ValueError,
        match=r"secret/data/truealpha/\{\{env\}\}/redis.*vault-policy\.hcl",
    ):
        _preflight(root, redis_secret)


def test_preflight_reads_vault_policy_at_iac_ref_not_head(tmp_path: Path) -> None:
    root = _infra2_checkout(
        tmp_path,
        [(_manifest(DATABASE), ("v1.2.46", "production/v1.2.46"))],
    )
    extended_policy = (
        TA_APP_POLICY
        + 'path "secret/data/truealpha/{{env}}/redis" { capabilities = ["read"] }\n'
        + 'path "secret/metadata/truealpha/{{env}}/redis" { capabilities = ["read", "list"] }\n'
    )
    (root / "truealpha/truealpha/20.data_engine/vault-policy.hcl").write_text(
        extended_policy, encoding="utf-8"
    )
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "grant redis path on main")
    _git(root, "tag", "v1.2.47")
    redis_secret = _manifest(
        DATABASE,
        _field(
            "REDIS_PASSWORD",
            "runtime",
            provided_by="truealpha/redis:REDIS_PASSWORD",
        ),
    )
    with pytest.raises(
        ValueError,
        match=r"secret/data/truealpha/\{\{env\}\}/redis.*vault-policy\.hcl",
    ):
        _preflight(root, redis_secret, production=True)
    staging_ref, rendered = _preflight(root, redis_secret)
    assert staging_ref == "v1.2.47"
    assert "REDIS_PASSWORD" in rendered["truealpha/data_engine"]


def test_preflight_refuses_a_compose_field_that_infra2_does_not_supply(
    tmp_path: Path,
) -> None:
    root = _infra2_checkout(tmp_path, [(_manifest(DATABASE), ("v1.0.0",))])
    release = _manifest(DATABASE, DECISION_FLAG)
    with pytest.raises(ValueError) as refusal:
        _preflight(root, release)
    message = str(refusal.value)
    assert "ENABLE_REALTIME_QUOTES" in message
    assert "DATABASE_URL" not in message
    assert STACK_PATH in message
    assert UPSTREAM_PATH in message
    assert "env.manifest.json" in message
    assert "--sync" not in message


def test_preflight_counts_a_variable_that_a_stack_manifest_supplies(
    tmp_path: Path,
) -> None:
    wanted = _field("RELEASE_MANIFEST_ID", "release", injected=True)
    (tmp_path / "with").mkdir()
    (tmp_path / "bare").mkdir()
    supplied = _infra2_checkout(
        tmp_path / "with",
        [(_manifest(DATABASE, wanted), ("v1.0.0",))],
    )
    iac_ref, _ = _preflight(supplied, _manifest(DATABASE, wanted))
    assert iac_ref == "v1.0.0"
    bare = _infra2_checkout(tmp_path / "bare", [(_manifest(DATABASE), ("v1.0.0",))])
    with pytest.raises(ValueError, match="RELEASE_MANIFEST_ID"):
        _preflight(bare, _manifest(DATABASE, wanted))


@pytest.mark.parametrize(
    "attribute, theirs, ours",
    [
        ("source", "release", "decision"),
        ("empty_ok", True, False),
        ("store_key", "OTHER", ""),
        ("scope", "project", "env"),
        ("group", "Other", "Application"),
        ("injected", True, False),
        ("aliases", ("OLD_NAME",), ()),
    ],
)
def test_preflight_refuses_a_compose_field_whose_supply_attribute_differs(
    tmp_path: Path, attribute: str, theirs: object, ours: object
) -> None:
    kept = _field(
        "APP_TOGGLE", "decision", **({} if attribute == "source" else {attribute: ours})
    )
    changed = _field("APP_TOGGLE", "release" if attribute == "source" else "decision")
    if attribute != "source":
        changed = _field("APP_TOGGLE", "decision", **{attribute: theirs})
    root = _infra2_checkout(tmp_path, [(_manifest(kept), ("v1.0.0",))])
    with pytest.raises(ValueError, match=f"APP_TOGGLE.*{attribute}"):
        _preflight(root, _manifest(changed))


@pytest.mark.parametrize(
    "attribute, theirs",
    [
        ("description", "now documented"),
        ("sensitive", True),
        ("has_default", False),
        ("ci", "allowed"),
    ],
)
def test_preflight_ignores_attributes_infra2_never_reads(
    tmp_path: Path, attribute: str, theirs: object
) -> None:
    root = _infra2_checkout(
        tmp_path, [(_manifest(_field("APP_TOGGLE", "decision")), ("v1.0.0",))]
    )
    release = _manifest(_field("APP_TOGGLE", "decision", **{attribute: theirs}))
    iac_ref, _ = _preflight(root, release)
    assert iac_ref == "v1.0.0"


@pytest.mark.parametrize("attribute", ["injected", "required"])
def test_preflight_refuses_a_code_field_that_the_image_cannot_start_without(
    tmp_path: Path, attribute: str
) -> None:
    root = _infra2_checkout(tmp_path, [(_manifest(DATABASE), ("v1.0.0",))])
    release = _manifest(DATABASE, _field("NEW_ENDPOINT", **{attribute: True}))
    with pytest.raises(ValueError, match="NEW_ENDPOINT"):
        _preflight(root, release)
    plain = _manifest(DATABASE, _field("NEW_ENDPOINT"))
    iac_ref, _ = _preflight(root, plain)
    assert iac_ref == "v1.0.0"


def test_a_lagging_production_marker_asks_for_a_promotion_not_a_sync(
    tmp_path: Path,
) -> None:
    root = _infra2_checkout(
        tmp_path,
        [
            (_manifest(DATABASE), ("v1.2.46", "production/v1.2.46")),
            (_manifest(DATABASE, DECISION_FLAG), ("v1.2.47",)),
        ],
    )
    with pytest.raises(ValueError) as refusal:
        _preflight(root, _manifest(DATABASE, DECISION_FLAG), production=True)
    message = str(refusal.value)
    assert "ENABLE_REALTIME_QUOTES" in message
    assert "v1.2.46" in message
    assert "promote infra2 to production" in message
    assert "Reconcile IaC Inputs with promote_prod" in message
    assert "--sync" not in message
    assert "secrets_render" not in message


def test_a_lagging_staging_release_asks_for_a_release_not_a_sync(
    tmp_path: Path,
) -> None:
    root = _infra2_checkout(
        tmp_path,
        [
            (_manifest(DATABASE), ("v1.2.46",)),
            (_manifest(DATABASE, DECISION_FLAG), ()),
        ],
    )
    with pytest.raises(ValueError) as refusal:
        _preflight(root, _manifest(DATABASE, DECISION_FLAG))
    message = str(refusal.value)
    assert "ENABLE_REALTIME_QUOTES" in message
    assert "cut an infra2 release" in message.lower()
    assert "--sync" not in message


def test_preflight_runs_at_the_marker_not_at_main(tmp_path: Path) -> None:
    root = _infra2_checkout(
        tmp_path,
        [
            (_manifest(DATABASE, DECISION_FLAG), ("v1.2.46", "production/v1.2.46")),
            (_manifest(DATABASE), ("v1.2.47",)),
        ],
        annotated=("v1.2.46",),
    )
    prod_ref, _ = _preflight(root, _manifest(DATABASE, DECISION_FLAG), production=True)
    assert prod_ref == "v1.2.46"
    staging_ref, _ = _preflight(root, _manifest(DATABASE))
    assert staging_ref == "v1.2.47"


def test_preflight_at_main_reads_the_checkout(tmp_path: Path) -> None:
    root = _infra2_checkout(tmp_path, [(_manifest(DATABASE), ("v1.0.0",))])
    preview = {"deploy_type": "preview/pr", "version_ref": "42"}
    iac_ref, _ = _preflight(root, _manifest(DATABASE), **preview)
    assert iac_ref == "main"
    with pytest.raises(ValueError, match="ENABLE_REALTIME_QUOTES"):
        _preflight(root, _manifest(DATABASE, DECISION_FLAG), **preview)


def test_a_rollback_is_checked_and_a_removal_is_not(tmp_path: Path) -> None:
    root = _infra2_checkout(tmp_path, [(_manifest(DATABASE), ("v1.0.0",))])
    unmet = _manifest(DATABASE, DECISION_FLAG)
    with pytest.raises(ValueError, match="ENABLE_REALTIME_QUOTES"):
        _preflight(root, unmet, operation="rollback")
    removal = {"operation": "remove", "deploy_type": "preview/pr", "version_ref": "42"}
    iac_ref, rendered = _preflight(root, b"never fetched", **removal)
    assert iac_ref == "main"
    assert rendered == {}


def test_preflight_checks_every_manifest_of_the_app(tmp_path: Path) -> None:
    root = _infra2_checkout(tmp_path, [(_manifest(DATABASE), ("v1.0.0",))])
    with pytest.raises(ValueError) as refusal:
        _preflight(
            root,
            _manifest(DATABASE),
            llm=_manifest(DATABASE, _field("LLM_MODEL_TIER", "decision")),
        )
    message = str(refusal.value)
    assert LLM_UPSTREAM in message and "LLM_MODEL_TIER" in message
    assert UPSTREAM_PATH not in message


def test_preflight_refuses_a_release_manifest_that_cannot_be_fetched(
    tmp_path: Path,
) -> None:
    root = _infra2_checkout(tmp_path, [(_manifest(DATABASE), ("v1.0.0",))])

    def unreachable(url: str) -> bytes:
        raise SystemExit(f"fetching {url} failed: HTTP 404")

    with pytest.raises(ValueError, match="failed to fetch manifest"):
        app_contract.verify_app_manifests(
            DeployRequest.from_dict(_request()),
            repo_root=root,
            iac_ref="v1.0.0",
            manifest_fetcher=unreachable,
        )


def test_preflight_fails_closed_when_the_infra2_release_has_no_stack_manifest(
    tmp_path: Path,
) -> None:
    root = _infra2_checkout(
        tmp_path,
        [(None, ("v1.2.46",)), (_manifest(DATABASE), ())],
    )
    with pytest.raises(ValueError, match=r"v1\.2\.46.*env\.manifest\.json"):
        _preflight(root, _manifest(DATABASE))


@pytest.mark.parametrize(
    "release",
    [b"", b"not json", b'{"vars": ["APP_URL"]}\n', _manifest()],
    ids=["empty", "not-json", "not-a-manifest", "no-fields"],
)
def test_preflight_refuses_a_release_manifest_that_is_not_a_manifest(
    tmp_path: Path, release: bytes
) -> None:
    root = _infra2_checkout(tmp_path, [(_manifest(DATABASE), ("v1.0.0",))])
    with pytest.raises(
        ValueError, match=r"apps/data-engine/required-env\.generated\.json"
    ):
        _preflight(root, release)


def test_preflight_refuses_an_infra2_stack_manifest_that_is_not_a_manifest(
    tmp_path: Path,
) -> None:
    root = _infra2_checkout(tmp_path, [(b'{"vars": []}\n', ("v1.0.0",))])
    with pytest.raises(ValueError, match=STACK_PATH):
        _preflight(root, _manifest(DATABASE))


def test_query_serving_release_returns_authoritative_dokploy_and_health_state() -> None:
    class _FakeDokploy:
        def get_compose_env(self, compose_id: str) -> str:
            assert compose_id == "lNn9gVS1Zyw79Jzw5dlbu"
            return (
                f"IMAGE_TAG=v0.1.45\nGIT_COMMIT_SHA=v0.1.45\nINFRA_IAC_REF={'b' * 40}\n"
            )

    serving = app_contract.query_serving_release(
        "finance_report/app",
        "prod",
        domain="zitian.party",
        client=_FakeDokploy(),
        http_get_fn=lambda _u: (200, '{"status": "ok", "version": "v0.1.45"}'),
        resolve_image=lambda tag, **_k: ResolvedRef(
            sha=RELEASE_SHA, image_ref=tag, form="tag"
        ),
    )
    assert serving.service == "finance_report/app"
    assert serving.environment == "production"
    assert serving.image_tag == "v0.1.45"
    assert serving.source_sha == RELEASE_SHA
    assert serving.iac_ref == "b" * 40
    assert serving.public_version == "v0.1.45"
    assert serving.healthy is True


def test_fetch_and_execute_app_hook_slot(tmp_path: Path) -> None:
    raw_hooks = {
        "schema_version": 2,
        "service": "truealpha/app",
        "slots": {
            slot: {
                "run": "python3 -c \"print('ok')\"",
                "bootstrap": "",
                "timeout_seconds": 30,
                "required_env": [],
                "optional_env": [],
                "artifacts_dir": f"artifacts/{slot}",
            }
            for slot in HOOK_SLOTS
        },
    }
    manifest_bytes = json.dumps(raw_hooks).encode("utf-8")
    manifest = app_contract.fetch_hooks_manifest(
        "truealpha/app",
        RELEASE_SHA,
        manifest_fetcher=lambda _url: manifest_bytes,
    )
    assert isinstance(manifest, RuntimeHooksManifest)
    assert tuple(manifest.slots) == HOOK_SLOTS

    result = app_contract.execute_app_hook_slot(
        manifest,
        "smoke",
        app_url="https://truealpha-staging.zitian.party",
        environment="staging",
        version_ref="v0.2.77",
        cwd=tmp_path,
    )
    assert isinstance(result, HookRunResult)
    assert result.status == "pass"
    assert result.exit_code == 0
