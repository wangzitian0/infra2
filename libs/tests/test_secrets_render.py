"""tools/secrets_render.py: every Vault Agent template and policy is what its manifest says (#PR-D)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from infra2_sdk.ci import validate_manifest_offline
from infra2_sdk.runtime.config_schema import EnvironmentField, EnvironmentManifest

from tools import secrets_render

ROOT = Path(__file__).resolve().parents[2]


def test_committed_templates_and_policies_match_their_manifests() -> None:
    assert secrets_render.check() == []


def test_every_registered_manifest_exists_and_passes_the_offline_gate() -> None:
    for service in secrets_render.SERVICES:
        for path in service.manifests:
            assert (ROOT / path).exists(), path
            manifest = EnvironmentManifest.from_dict(
                json.loads((ROOT / path).read_text(encoding="utf-8"))
            )
            assert validate_manifest_offline(manifest) == [], path


def test_generated_files_carry_the_marker_and_no_silent_empty_fallback() -> None:
    for service in secrets_render.SERVICES:
        if not service.generated:
            continue
        template = (ROOT / service.directory / "secrets.ctmpl").read_text(
            encoding="utf-8"
        )
        assert template.startswith("{{- /* Generated from"), service.directory
        assert '{{ else }}""' not in template, service.directory
        if service.policy:
            policy = (ROOT / service.directory / "vault-policy.hcl").read_text(
                encoding="utf-8"
            )
            assert policy.startswith("# Generated from"), service.directory
            assert 'path "auth/token/lookup-self"' in policy


def test_runner_policy_is_the_writer_identity_and_stays_hand_written() -> None:
    runner = next(s for s in secrets_render.SERVICES if s.service == "iac_runner")
    assert runner.policy is False
    policy = (ROOT / runner.directory / "vault-policy.hcl").read_text(encoding="utf-8")
    assert 'path "secret/data/platform/+/*"' in policy and '"create"' in policy


def test_merged_manifest_rejects_conflicting_declarations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = tmp_path / "a.json"
    b = tmp_path / "b.json"
    field_a = EnvironmentField(
        "k", "KEY", source="runtime", required=True, has_default=False
    )
    field_b = EnvironmentField("k", "KEY", source="human", empty_ok=True)
    a.write_text(
        json.dumps(EnvironmentManifest(source="a", fields=(field_a,)).to_dict()),
        encoding="utf-8",
    )
    b.write_text(
        json.dumps(EnvironmentManifest(source="b", fields=(field_b,)).to_dict()),
        encoding="utf-8",
    )
    monkeypatch.setattr(secrets_render, "ROOT", tmp_path)
    service = secrets_render.Service("svc", "proj", "svc", ("a.json", "b.json"))
    with pytest.raises(ValueError, match="KEY.source declared differently"):
        secrets_render.merged_manifest(service)
    same = secrets_render.Service("svc", "proj", "svc", ("a.json", "a.json"))
    assert [f.env for f in secrets_render.merged_manifest(same).fields] == ["KEY"]


def test_check_reports_a_stale_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "svc").mkdir()
    manifest = EnvironmentManifest(
        source="x",
        fields=(
            EnvironmentField(
                "p", "PASSWORD", source="runtime", required=True, has_default=False
            ),
        ),
    )
    (tmp_path / "m.json").write_text(json.dumps(manifest.to_dict()), encoding="utf-8")
    monkeypatch.setattr(secrets_render, "ROOT", tmp_path)
    service = secrets_render.Service("svc", "proj", "svc", ("m.json",))
    problems = secrets_render.check((service,))
    assert len(problems) == 2 and "svc/secrets.ctmpl" in problems[0]
    secrets_render.write((service,))
    assert secrets_render.check((service,)) == []


def test_every_generated_service_fails_the_render_on_a_missing_required_key() -> None:
    for service in secrets_render.SERVICES:
        if not service.generated:
            continue
        agent = (ROOT / service.directory / "vault-agent.hcl").read_text(
            encoding="utf-8"
        )
        assert "error_on_missing_key = true" in agent, service.directory
