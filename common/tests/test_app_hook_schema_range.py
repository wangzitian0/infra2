"""infra2 runs app hook manifests of schema 3 only (infra2#1823, Step 3).

The train reads each app manifest at the app's pinned tag. Every app ships schema 3, and
the pinned SDK refuses schema 2.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from common.deploy import app_contract
from common.deploy.app_contract import (
    ACCEPTED_HOOK_SCHEMA_VERSIONS,
    RuntimeHooksManifest,
    execute_app_hook_slot,
    hook_env_for_slot,
    schema3_empty_cells,
)
from common.deploy import reconcile_iac_inputs as reconcile

ALL_SLOTS = ("smoke", "preview_gate", "staging_gate", "prod_gate", "scheduled_audit")


def _row(check_id: str, **extra: object) -> dict[str, object]:
    return {
        "id": check_id,
        "argv": ["sh", "-c", f"echo {check_id}"],
        "needs": ["running_service"],
        "writes": False,
        "accepts_scale": False,
        "timeout_seconds": 60,
        **extra,
    }


def _v3(*rows: dict[str, object], **top: object) -> RuntimeHooksManifest:
    raw = {
        "schema_version": 3,
        "service": "finance_report/app",
        "required_env": ["APP_URL"],
        "checks": list(rows) or [_row(name) for name in ALL_SLOTS],
        **top,
    }
    return RuntimeHooksManifest.from_dict(raw)


def _writing_only() -> RuntimeHooksManifest:
    """A check that writes never reaches prod, so prod cells stay empty."""
    return _v3(*(_row(name, writes=True) for name in ("preview_gate", "staging_gate")))


def test_the_runner_accepts_schema_3_only() -> None:
    assert ACCEPTED_HOOK_SCHEMA_VERSIONS == (3,)
    with pytest.raises(ValueError, match="schema_version"):
        RuntimeHooksManifest.from_dict(
            {"schema_version": 2, "service": "finance_report/app", "slots": {}}
        )


@pytest.mark.parametrize(
    ("slot", "environment", "cell", "check_id"),
    [
        ("smoke", "staging", "staging.operation", "smoke"),
        ("smoke", "production", "prod.operation", "smoke"),
        ("staging_gate", "staging", "staging.post", "staging_gate"),
        ("prod_gate", "production", "prod.post", "prod_gate"),
    ],
)
def test_a_schema_3_slot_runs_its_cell_without_leaking_secrets(
    slot: str, environment: str, cell: str, check_id: str
) -> None:
    calls: list[tuple[list[str], dict[str, str]]] = []

    def runner(argv, **kw):
        calls.append((list(argv), dict(kw.get("env") or {})))
        return subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")

    result = execute_app_hook_slot(
        _v3(),
        slot,
        app_url="https://app.example",
        environment=environment,
        version_ref="v0.2.92",
        ambient_env={"INFRA2_PAT": "must-not-leak", "PATH": "/usr/bin"},
        runner=runner,
    )
    assert result.status == "pass", result
    assert result.cell == cell
    ran = [argv for argv, _ in calls if argv[-1].startswith("echo ")]
    assert ["sh", "-c", f"echo {check_id}"] in ran
    assert calls, "the runner ran no check"
    for _argv, env in calls:
        assert env.get("APP_URL") == "https://app.example"
        assert "INFRA2_PAT" not in env


def test_schema3_empty_cells_names_a_cell_with_no_check() -> None:
    writing = _writing_only()
    assert schema3_empty_cells(writing, "prod_gate") == ("prod.post",)
    assert schema3_empty_cells(writing, "smoke") == (
        "preview.operation",
        "staging.operation",
        "prod.operation",
    )
    assert schema3_empty_cells(writing, "staging_gate") == ()
    assert schema3_empty_cells(_v3(), "prod_gate") == ()


def test_a_forbidden_secret_in_a_schema_3_manifest_is_refused() -> None:
    with pytest.raises(ValueError, match="forbidden"):
        _v3(optional_env=["GITHUB_TOKEN"])


def test_an_unaccepted_schema_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_contract, "ACCEPTED_HOOK_SCHEMA_VERSIONS", (2,))
    with pytest.raises(ValueError, match=r"is not in \[2\]"):
        hook_env_for_slot(
            _v3(),
            "staging_gate",
            app_url="https://a",
            environment="staging",
            version_ref="v1",
        )


def test_audit_app_hooks_accepts_schema_3_and_reports_an_empty_cell(
    monkeypatch,
) -> None:
    from common.deploy.git_refs import ResolvedRef

    monkeypatch.setattr(
        reconcile, "APP_SOURCES", {"finance_report/app": "wangzitian0/finance_report"}
    )
    monkeypatch.setattr(
        reconcile,
        "resolve_image_ref",
        lambda ref, **_kw: ResolvedRef(sha="a" * 40, image_ref=ref, form="tag"),
    )
    good = _v3()
    monkeypatch.setattr(reconcile, "fetch_hooks_manifest", lambda *_a, **_k: good)
    ok = reconcile.audit_app_hooks(pinned_tags={"finance_report/app": "v0.2.92"})
    assert ok["ok"] is True, ok["errors"]

    bad_manifest = _v3(
        *(_row(name, writes=True) for name in ("preview_gate", "staging_gate")),
        required_env=["APP_URL", "UNSUPPLIED_X"],
    )
    monkeypatch.setattr(
        reconcile, "fetch_hooks_manifest", lambda *_a, **_k: bad_manifest
    )
    bad = reconcile.audit_app_hooks(pinned_tags={"finance_report/app": "v0.2.92"})
    assert bad["ok"] is False
    errors = json.dumps(bad["errors"])
    assert "no check runs in cell 'prod.post' (slot 'prod_gate')" in errors
    assert "unsupplied required_env 'UNSUPPLIED_X'" in errors


def test_an_off_check_fills_no_cell() -> None:
    off = {"reason": "paused", "issue": "infra2#1823"}
    manifest = _v3(*(_row(name, off=off) for name in ALL_SLOTS))
    assert schema3_empty_cells(manifest, "prod_gate") == ("prod.post",)


def test_check_level_required_env_is_audited_and_a_missing_value_raises() -> None:
    from common.deploy.app_contract import schema3_required_env

    manifest = _v3(
        *(_row(name) for name in ALL_SLOTS if name != "staging_gate"),
        _row("staging_gate", required_env=["TA_PASSWORD"]),
    )
    assert "TA_PASSWORD" in schema3_required_env(manifest, "staging_gate")
    with pytest.raises(ValueError, match="missing required hook env: TA_PASSWORD"):
        hook_env_for_slot(
            manifest,
            "staging_gate",
            app_url="https://a",
            environment="staging",
            version_ref="v1",
            ambient_env={"PATH": "/usr/bin"},
        )
    _m, env = hook_env_for_slot(
        manifest,
        "staging_gate",
        app_url="https://a",
        environment="staging",
        version_ref="v1",
        ambient_env={"TA_PASSWORD": "x"},
    )
    assert env["TA_PASSWORD"] == "x"


def test_the_scheduled_audit_runs_and_fails_a_schema_3_app_with_an_empty_cell(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Review of #1849 (severity: high): schema 3 builds slots only for cells that have a
    check. `if slot not in manifest.slots: continue` in run_scheduled_app_audits skipped such
    an app and recorded nothing; schema 2 made every slot mandatory. The audit must run the
    slot, and run_cell fails the empty cell."""
    from common.deploy.git_refs import ResolvedRef

    repos_yaml = tmp_path / "harness" / "repos.yaml"
    repos_yaml.parent.mkdir(parents=True, exist_ok=True)
    repos_yaml.write_text(
        "schema_version: 1\nrepositories:\n"
        "  - id: finance-report\n    role: external-application\n    release_tag: v0.2.92\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        reconcile,
        "resolve_image_ref",
        lambda ref, **_kw: ResolvedRef(sha="a" * 40, image_ref=ref, form="tag"),
    )
    monkeypatch.setattr(
        reconcile, "resolve_app_hook_cwd", lambda *_a, **_k: (tmp_path, None)
    )
    monkeypatch.setenv("GITHUB_TOKEN", "tok-test")
    off = {"reason": "paused", "issue": "infra2#1823"}
    manifest = _v3(*(_row(name, off=off) for name in ALL_SLOTS))
    monkeypatch.setattr(reconcile, "fetch_hooks_manifest", lambda *_a, **_kw: manifest)

    def runner(argv, **_kw):
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    results = reconcile.run_scheduled_app_audits(
        tmp_path,
        services=["finance_report/app"],
        runner=runner,
        verdict_cache_path=tmp_path / "cache.json",
    )
    assert [r.get("service") for r in results] == ["finance_report/app"], results
    assert results[0]["ok"] is False
    assert "0 active checks" in json.dumps(results[0])


def test_the_train_runs_and_fails_a_schema_3_slot_with_an_empty_cell(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Review of #1849 (severity: high): schema 3 builds slots only for cells that have a
    check, so a `slot not in manifest.slots: continue` would skip an empty prod.post and
    record nothing. The train must run it, and run_cell fails it."""
    from common.deploy.git_refs import ResolvedRef

    monkeypatch.setattr(
        reconcile,
        "resolve_image_ref",
        lambda ref, **_kw: ResolvedRef(sha="a" * 40, image_ref=ref, form="tag"),
    )
    monkeypatch.setenv("GITHUB_TOKEN", "tok-test")
    manifest = _writing_only()
    plan = reconcile.build_plan(
        ["finance_report/finance_report/10.app/compose.yaml"],
        app_release_tags={"finance_report/app": "v0.2.92"},
    )
    applied = [
        reconcile.DeployCommand(
            service="finance_report/app", deploy_type="prod", argv=["true"]
        )
    ]

    def runner(argv, **_kw):
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(
        reconcile, "resolve_app_hook_cwd", lambda *_a, **_k: (tmp_path, None)
    )
    results = reconcile.run_post_deploy_app_hooks(
        plan,
        applied,
        repo_root=tmp_path,
        after="v3.1.130",
        target_env="production",
        slot="prod_gate",
        manifest_fetcher=lambda _url: json.dumps(manifest.to_dict()).encode("utf-8"),
        runner=runner,
    )
    assert [r["slot"] for r in results] == ["prod_gate"], results
    assert results[0]["ok"] is False
    assert "0 active checks" in json.dumps(results[0])
