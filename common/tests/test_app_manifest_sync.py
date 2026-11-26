"""The App stack manifest copies are projections of the pinned App release (#1822, #1823)."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest
from infra2_sdk.config import EnvironmentManifest

from common.deploy import app_manifest_sync as sync
from common.deploy.app_contract import (
    parse_compose_contract,
    render_release_template,
    service_for_id,
)
from common.deploy.compose_compile import VAULT_TEMPLATE_LABEL, has_x_infra2
from common.security import secrets_render
from common.security.registry import ROOT, SERVICES
from common.tests.compose_env import (
    compiled_vault_template,
    compose_services,
    raw_environment,
)

FR_SHA = "f" * 40


def _body(source: str, *fields: dict[str, object]) -> bytes:
    return json.dumps(
        {"contract_version": 2, "source": source, "fields": list(fields)}
    ).encode()


_BACKEND = _body(
    "apps/backend",
    {
        "field": "secret_key",
        "env": "SECRET_KEY",
        "required": True,
        "has_default": False,
        "sensitive": True,
        "source": "runtime",
    },
    {"field": "llm_small_model", "env": "LLM_SMALL_MODEL", "description": "small"},
)
_FRONTEND = _body(
    "apps/frontend",
    {
        "field": "next_public_git_sha",
        "env": "NEXT_PUBLIC_GIT_SHA",
        "injected": True,
        "source": "release",
    },
)


def _fetcher(seen: list[str]):
    bodies = {
        "common/runtime/required-env.generated.json": _BACKEND,
        "apps/frontend/required-env.manifest.json": _FRONTEND,
    }

    def fetch(url: str) -> bytes:
        seen.append(url)
        for path, body in bodies.items():
            if url.endswith("/" + path):
                return body
        raise SystemExit(f"fetching {url} failed: HTTP Error 404")

    return fetch


def test_projected_services_are_the_three_app_stacks() -> None:
    assert [s.id for s in sync.projected_services()] == [
        "finance_report/app",
        "truealpha/app",
        "truealpha/data_engine",
    ]


def test_upstream_paths_follow_app_upstream_manifests_plus_compose_only() -> None:
    assert sync.upstream_paths("finance_report/app") == (
        "common/runtime/required-env.generated.json",
        "apps/frontend/required-env.manifest.json",
    )
    assert sync.upstream_paths("truealpha/app") == (
        "apps/app-web/required-env.manifest.json",
        "apps/llm-service/required-env.generated.json",
    )
    assert sync.upstream_paths("truealpha/data_engine") == (
        "apps/data-engine/required-env.generated.json",
    )
    with pytest.raises(ValueError, match="no upstream manifests"):
        sync.upstream_paths("platform/todo")


def test_manifest_url_reads_a_tag_as_a_tag_ref_and_a_sha_as_is() -> None:
    assert sync.manifest_url("o/r", "v0.2.95", "a/b.json") == (
        "https://raw.githubusercontent.com/o/r/refs/tags/v0.2.95/a/b.json"
    )
    assert sync.manifest_url("o/r", FR_SHA, "a/b.json") == (
        f"https://raw.githubusercontent.com/o/r/{FR_SHA}/a/b.json"
    )


def test_project_merges_backend_then_frontend_under_the_infra2_source() -> None:
    seen: list[str] = []
    manifest = sync.project(
        service_for_id("finance_report/app"), FR_SHA, fetcher=_fetcher(seen)
    )
    assert manifest.source == "infra2/finance_report/app"
    assert [f.env for f in manifest.fields] == [
        "SECRET_KEY",
        "LLM_SMALL_MODEL",
        "NEXT_PUBLIC_GIT_SHA",
    ]
    assert [f.source for f in manifest.fields] == ["runtime", "code", "release"]
    assert seen == [
        f"https://raw.githubusercontent.com/wangzitian0/finance_report/{FR_SHA}/"
        "common/runtime/required-env.generated.json",
        f"https://raw.githubusercontent.com/wangzitian0/finance_report/{FR_SHA}/"
        "apps/frontend/required-env.manifest.json",
    ]


def test_project_fails_on_a_missing_upstream_manifest() -> None:
    def missing(url: str) -> bytes:
        raise SystemExit(f"fetching {url} failed: HTTP Error 404")

    with pytest.raises(ValueError, match="failed to fetch manifest"):
        sync.project(service_for_id("truealpha/app"), "v0.0.1", fetcher=missing)


@pytest.mark.parametrize("service", sync.projected_services(), ids=lambda s: s.id)
def test_committed_copy_is_in_the_projection_text_form(service) -> None:
    text = (ROOT / service.manifests[0]).read_text(encoding="utf-8")
    manifest = EnvironmentManifest.from_dict(json.loads(text))
    assert manifest.source == f"infra2/{service.id}"
    assert sync.render(manifest) == text


@pytest.mark.parametrize("service", sync.projected_services(), ids=lambda s: s.id)
def test_release_template_from_the_copy_equals_the_committed_template(service) -> None:
    """When the release manifests equal the copy, the release render matches deployed."""
    copy = (ROOT / service.manifests[0]).read_text(encoding="utf-8")
    template, policy = render_release_template(
        service.id, [("release", copy)], stack_manifest_body=copy
    )
    directory = ROOT / service.directory
    assert template == compiled_vault_template(directory)
    if has_x_infra2((directory / "compose.yaml").read_text(encoding="utf-8")):
        vault_agent = compose_services(directory / "compose.yaml")["vault-agent"]
        labels = vault_agent.get("labels") or []
        want = f"{VAULT_TEMPLATE_LABEL}={hashlib.sha256(template.encode('utf-8')).hexdigest()}"
        assert want in labels
    assert policy == (directory / "vault-policy.hcl").read_text(encoding="utf-8")


def test_compiled_vault_template_fails_closed_when_x_infra2_sidecar_lacks_inline_template(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "compose.yaml").write_text(
        "x-infra2:\n  vault:\n    name: svc\nservices:\n  app:\n    image: a:1\n",
        encoding="utf-8",
    )
    (tmp_path / "secrets.ctmpl").write_text("stale\n", encoding="utf-8")
    monkeypatch.setattr(
        "common.tests.compose_env.compose_services",
        lambda _path: {"vault-agent": {"environment": {}}},
    )
    with pytest.raises(AssertionError, match="lacks VAULT_SECRETS_TEMPLATE"):
        compiled_vault_template(tmp_path)


def _seed(root: Path, text: str) -> None:
    for service in SERVICES:
        if (
            "finance_report/finance_report/10.app/env.manifest.json"
            in service.manifests
        ):
            (root / service.directory).mkdir(parents=True, exist_ok=True)
    (root / "finance_report/finance_report/10.app/env.manifest.json").write_text(
        text, encoding="utf-8"
    )
    # The stack compose file is an x-infra2 source, so secrets_render commits no template.
    (root / "finance_report/finance_report/10.app/compose.yaml").write_text(
        (ROOT / "finance_report/finance_report/10.app/compose.yaml").read_text(
            encoding="utf-8"
        ),
        encoding="utf-8",
    )


def test_check_reports_a_drifted_copy_and_passes_an_equal_copy(tmp_path: Path) -> None:
    fetch = _fetcher([])
    services = (service_for_id("finance_report/app"),)
    expected = sync.render(sync.project(services[0], "v0.2.95", fetcher=fetch))
    tags = {"finance_report/app": "v0.2.95"}

    _seed(tmp_path, expected.replace('"small"', '"stale"'))
    problems = sync.check(tags, root=tmp_path, services=services, fetcher=fetch)
    assert len(problems) == 1
    assert '-      "description": "stale",' in problems[0]
    assert '+      "description": "small",' in problems[0]

    _seed(tmp_path, expected)
    assert sync.check(tags, root=tmp_path, services=services, fetcher=fetch) == []
    assert sync.check({}, root=tmp_path, services=services, fetcher=fetch) == [
        "finance_report/app: harness/repos.yaml pins no release tag"
    ]


def test_write_copies_rewrites_the_copy_and_the_vault_files_of_each_reader(
    tmp_path: Path,
) -> None:
    fetch = _fetcher([])
    _seed(tmp_path, "{}\n")
    changed = sync.write_copies(
        {"finance_report/app": FR_SHA}, root=tmp_path, fetcher=fetch
    )
    assert changed == [
        "finance_report/finance_report/10.app/env.manifest.json",
        "finance_report/finance_report/10.app/vault-policy.hcl",
        "finance_report/finance_report/preview/secrets.ctmpl",
        "finance_report/finance_report/preview/vault-agent.hcl",
        "finance_report/finance_report/preview/vault-policy.hcl",
    ]
    copy = tmp_path / "finance_report/finance_report/10.app/env.manifest.json"
    assert copy.read_text(encoding="utf-8") == sync.render(
        sync.project(service_for_id("finance_report/app"), FR_SHA, fetcher=fetch)
    )
    # The x-infra2 stack commits neither file: the deploy compiles them.
    for name in ("secrets.ctmpl", "vault-agent.hcl"):
        assert not (tmp_path / "finance_report/finance_report/10.app" / name).exists()
    template = compiled_vault_template(ROOT / "finance_report/finance_report/10.app")
    assert 'SECRET_KEY={{ printf "%q" .Data.data.SECRET_KEY }}' in template
    preview = secrets_render.render(
        service_for_id("finance_report/app", preview=True), root=tmp_path
    )
    assert (tmp_path / "finance_report/finance_report/preview/secrets.ctmpl").read_text(
        encoding="utf-8"
    ) == preview["secrets.ctmpl"]
    # A second run finds nothing to change.
    assert (
        sync.write_copies({"finance_report/app": FR_SHA}, root=tmp_path, fetcher=fetch)
        == []
    )


def _supplied_names(directory: Path) -> set[str]:
    """Names that the stack compose environment or the committed Vault template sets."""
    names: set[str] = set()
    for container in compose_services(directory / "compose.yaml").values():
        names |= set(raw_environment(container))
    template = compiled_vault_template(directory)
    names |= set(re.findall(r"(?m)(?:^|\}\})([A-Z_][A-Z0-9_]*)=", template))
    return names


@pytest.mark.parametrize("service", sync.projected_services(), ids=lambda s: s.id)
def test_the_stack_supplies_every_contract_field_of_the_generated_copy(service) -> None:
    """The copy comes from the release, so the deploy preflight cannot catch a gap.

    This check stays independent of the release: each field that the image needs from
    infra2 (``release``, ``decision``, ``required`` or ``injected``) must be set by the
    hand-written compose file or rendered by the Vault template.
    """
    copy = (ROOT / service.manifests[0]).read_text(encoding="utf-8")
    contract = parse_compose_contract(copy, label=service.id)
    assert contract, f"{service.id}: the copy declares no contract field"
    unsupplied = sorted(set(contract) - _supplied_names(ROOT / service.directory))
    assert unsupplied == []


def test_canary_todo_manifest_conforms_to_shared_env_and_supplies_contract() -> None:
    """platform/30.todo/env.manifest.json conforms to shared_env and stack supply."""
    from infra2_sdk.config import check_shared_env

    directory = ROOT / "platform/30.todo"
    copy = (directory / "env.manifest.json").read_text(encoding="utf-8")
    manifest = EnvironmentManifest.from_dict(json.loads(copy))
    assert manifest.validate_offline() == []
    assert check_shared_env([f.env for f in manifest.fields]) == []
    vault_backed = {f.env for f in manifest.fields if f.source != "code"}
    assert vault_backed
    assert sorted(vault_backed - _supplied_names(directory)) == []
