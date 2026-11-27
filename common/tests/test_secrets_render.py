"""tools/secrets_render.py: every Vault Agent template and policy is what its manifest says (#PR-D)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from common.core.ci_contract import validate_manifest_offline
from infra2_sdk.config import EnvironmentField, EnvironmentManifest

from common.security import secrets_render

ROOT = Path(__file__).resolve().parents[2]


def test_committed_templates_and_policies_match_their_manifests() -> None:
    assert secrets_render.check() == []


def test_every_registered_manifest_exists_and_passes_the_offline_gate() -> None:
    for service in secrets_render.SERVICES:
        for path in service.manifests:
            manifest = secrets_render.load_manifest(path)
            assert validate_manifest_offline(manifest) == [], path


def _deployed(service) -> dict[str, str]:
    """The template and agent config that the deploy ships for ``service``.

    A plain stack mounts the committed files. An ``x-infra2`` stack ships them inline in
    its compiled compose (``VAULT_SECRETS_TEMPLATE``, ``VAULT_AGENT_HCL``, infra2#1822).
    """
    import yaml

    from common.deploy.compose_compile import has_x_infra2, load_stack

    compose = f"{service.directory}/compose.yaml"
    path = ROOT / compose
    if path.is_file() and has_x_infra2(path.read_text(encoding="utf-8")):
        doc = yaml.safe_load(load_stack(compose).compiled)
        env = doc["services"]["vault-agent"]["environment"]
        return {
            "secrets.ctmpl": env["VAULT_SECRETS_TEMPLATE"].replace("$$", "$"),
            "vault-agent.hcl": env["VAULT_AGENT_HCL"].replace("$$", "$"),
        }
    return {
        name: (ROOT / service.directory / name).read_text(encoding="utf-8")
        for name in secrets_render.COMPILED_FILES
    }


def test_generated_files_carry_the_marker_and_no_silent_empty_fallback() -> None:
    for service in secrets_render.SERVICES:
        if not service.generated:
            continue
        template = _deployed(service)["secrets.ctmpl"]
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
    assert 'path "secret/data/platform/{{env}}/*"' in policy and '"create"' in policy


def test_merged_manifest_rejects_conflicting_declarations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = tmp_path / "a.json"
    b = tmp_path / "b.json"
    field_a = EnvironmentField(
        field="k", env="KEY", source="runtime", required=True, has_default=False
    )
    field_b = EnvironmentField(field="k", env="KEY", source="human", empty_ok=True)
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
                field="p",
                env="PASSWORD",
                source="runtime",
                required=True,
                has_default=False,
            ),
        ),
    )
    (tmp_path / "m.json").write_text(json.dumps(manifest.to_dict()), encoding="utf-8")
    monkeypatch.setattr(secrets_render, "ROOT", tmp_path)
    service = secrets_render.Service("svc", "proj", "svc", ("m.json",))
    problems = secrets_render.check((service,))
    assert len(problems) == 3 and "svc/secrets.ctmpl" in problems[0]
    secrets_render.write((service,))
    assert secrets_render.check((service,)) == []


def _one_key_service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, compose: str):
    (tmp_path / "svc").mkdir()
    (tmp_path / "svc/compose.yaml").write_text(compose, encoding="utf-8")
    manifest = EnvironmentManifest(
        source="x",
        fields=(
            EnvironmentField(
                field="p",
                env="PASSWORD",
                source="runtime",
                required=True,
                has_default=False,
            ),
        ),
    )
    (tmp_path / "m.json").write_text(json.dumps(manifest.to_dict()), encoding="utf-8")
    monkeypatch.setattr(secrets_render, "ROOT", tmp_path)
    return secrets_render.Service("svc", "proj", "svc", ("m.json",))


_X_INFRA2 = "x-infra2:\n  vault:\n    name: svc\nservices:\n  app:\n    image: a:1\n"
_PLAIN = "services:\n  app:\n    image: a:1\n"


def test_an_x_infra2_stack_commits_only_its_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """infra2#1822 Cleanup: the deploy compiles secrets.ctmpl and vault-agent.hcl of an
    x-infra2 stack, so check() requires and write() creates only vault-policy.hcl."""
    service = _one_key_service(tmp_path, monkeypatch, _X_INFRA2)
    problems = secrets_render.check((service,))
    assert len(problems) == 1 and "svc/vault-policy.hcl" in problems[0], problems
    secrets_render.write((service,))
    assert sorted(p.name for p in (tmp_path / "svc").iterdir()) == [
        "compose.yaml",
        "vault-policy.hcl",
    ]
    assert secrets_render.check((service,)) == []
    # A stale policy still fails.
    policy = tmp_path / "svc/vault-policy.hcl"
    policy.write_text(policy.read_text(encoding="utf-8") + "# edit\n", "utf-8")
    problems = secrets_render.check((service,))
    assert len(problems) == 1 and "svc/vault-policy.hcl" in problems[0], problems
    # A manifest error still fails: the render runs for an x-infra2 stack too.
    (tmp_path / "m.json").unlink()
    problems = secrets_render.check((service,))
    assert len(problems) == 1 and "m.json" in problems[0], problems


def test_a_plain_stack_without_its_template_and_config_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _one_key_service(tmp_path, monkeypatch, _PLAIN)
    secrets_render.write((service,))
    assert secrets_render.check((service,)) == []
    for name in secrets_render.COMPILED_FILES:
        (tmp_path / "svc" / name).unlink()
    problems = secrets_render.check((service,))
    assert [p.splitlines()[0] for p in problems] == [
        "--- svc/secrets.ctmpl",
        "--- svc/vault-agent.hcl",
    ]


def test_every_generated_service_fails_the_render_on_a_missing_required_key() -> None:
    for service in secrets_render.SERVICES:
        if not service.generated:
            continue
        agent = _deployed(service)["vault-agent.hcl"]
        assert "error_on_missing_key = true" in agent, service.directory


def test_optional_keys_never_use_a_direct_map_access() -> None:
    """Under error_on_missing_key a direct `.Data.data.KEY` access aborts the render when
    the key is absent, which is exactly what an optional key may be. Optional keys must
    render through `index`; this is the 2026-09-07 alerting outage."""
    for service in secrets_render.SERVICES:
        if not service.generated:
            continue
        template = _deployed(service)["secrets.ctmpl"]
        assert "{{ with .Data.data." not in template, service.directory
        assert "{{- with .Data.data." not in template, service.directory


def test_composed_url_templates_wrap_secrets_with_urlquery_for_reserved_chars(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """v3.2.14 root cause: a 44-char base64 password containing `/`, `+`, `=` breaks URL parsing unless urlquery-escaped."""
    import urllib.parse

    from infra2_sdk.storage import RedisSettings

    (tmp_path / "svc").mkdir()
    (tmp_path / "svc/compose.yaml").write_text(_X_INFRA2, encoding="utf-8")
    manifest = EnvironmentManifest(
        source="x",
        fields=(
            EnvironmentField(
                field="redis_url",
                env="REDIS_URL",
                source="runtime",
                provided_by="platform/redis:password",
                composed_from="redis://:{password}@platform-redis{env:ENV_SUFFIX}:6379/0",
                sensitive=True,
            ),
            EnvironmentField(
                field="api_key",
                env="API_KEY",
                source="human",
                sensitive=True,
                empty_ok=True,
            ),
        ),
    )
    (tmp_path / "m.json").write_text(json.dumps(manifest.to_dict()), encoding="utf-8")
    monkeypatch.setattr(secrets_render, "ROOT", tmp_path)
    service = secrets_render.Service("svc", "proj", "svc", ("m.json",), policy=False)
    template = secrets_render.render(service)["secrets.ctmpl"]

    assert (
        'REDIS_URL={{ printf "%q" (printf "redis://:%s@platform-redis%s:6379/0" '
        '(urlquery .Data.data.password) (env "ENV_SUFFIX")) }}'
    ) in template
    assert "(urlquery .Data.data.API_KEY)" not in template

    raw_pw = "aB+cD/eF1234567890123456789012345678901234="
    encoded = urllib.parse.quote(raw_pw, safe="")
    parsed = RedisSettings(url=f"redis://:{encoded}@platform-redis-staging:6379/0")
    split = urllib.parse.urlsplit(parsed.url)
    assert split.hostname == "platform-redis-staging"
    assert split.port == 6379
    assert split.path == "/0"
    assert urllib.parse.unquote(split.password or "") == raw_pw
