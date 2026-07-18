"""Tests for tools/vault_self_refresh_audit_check.py (#531).

Exercises the scheduled-CI wrapper's wiring and exit-code contract without any live
Dokploy/SSH access, matching the style of libs/tests/test_app_compose_id_drift.py.
"""

from __future__ import annotations

from tools import vault_self_refresh_audit_check as check


def test_run_loads_real_inventory_and_wires_collection_into_classification(
    monkeypatch,
) -> None:
    """`run()` must load the real inventory, collect live observations for it, and
    classify the result -- collection/classification are stubbed so this needs no live
    Dokploy/SSH access."""
    captured = {}

    def fake_collect_live_observations(services, *, env):
        captured["collect_services"] = services
        captured["collect_env"] = env
        return {"services": {}}

    def fake_audit_from_observations(services, observations, *, env):
        captured["audit_services"] = services
        captured["observations"] = observations
        captured["audit_env"] = env
        return {"status": "pass", "results": []}

    monkeypatch.setattr(
        check, "collect_live_observations", fake_collect_live_observations
    )
    monkeypatch.setattr(check, "audit_from_observations", fake_audit_from_observations)

    report = check.run(env="production")

    assert report == {"status": "pass", "results": []}
    assert captured["collect_env"] == "production"
    assert captured["audit_env"] == "production"
    # The real docs/ssot/vault-self-refresh-inventory.yaml inventory, not a stub.
    assert captured["collect_services"]
    assert captured["audit_services"] is captured["collect_services"]
    assert captured["observations"] == {"services": {}}


def test_main_returns_zero_and_prints_pass_on_a_clean_audit(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        check,
        "run",
        lambda env="production": {
            "schema_version": 1,
            "env": "production",
            "status": "pass",
            "generated_at": 0,
            "results": [],
        },
    )

    exit_code = check.main()

    assert exit_code == 0
    assert "PASS" in capsys.readouterr().out


def test_main_returns_nonzero_on_a_confirmed_fail(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        check,
        "run",
        lambda env="production": {
            "schema_version": 1,
            "env": "production",
            "status": "fail",
            "generated_at": 0,
            "results": [
                {
                    "service_id": "some/service",
                    "check_id": "dokploy-env-approle",
                    "status": "fail",
                    "severity": "P0",
                    "summary": "VAULT_ROLE_ID missing from Dokploy env",
                    "evidence": {},
                }
            ],
        },
    )

    exit_code = check.main()

    assert exit_code == 1
    assert "FAIL" in capsys.readouterr().out


def test_production_run_excludes_services_not_yet_in_production(monkeypatch) -> None:
    """Verified live (2026-07-18): truealpha's Dokploy `production` environment has
    zero composes for postgres/app/data_engine (#500 scoped its rollout to staging
    only so far) -- a daily env="production" run must not check them, or it pages
    Feishu forever on the same non-actionable "missing from production" finding."""
    captured = {}

    def fake_collect_live_observations(services, *, env):
        captured["ids"] = {s.id for s in services}
        return {"services": {}}

    monkeypatch.setattr(
        check, "collect_live_observations", fake_collect_live_observations
    )
    monkeypatch.setattr(
        check, "audit_from_observations", lambda *a, **k: {"status": "pass"}
    )

    check.run(env="production")

    assert captured["ids"].isdisjoint(check.NOT_YET_IN_PRODUCTION)
    assert "finance_report/app" in captured["ids"]  # real prod services untouched


def test_staging_run_does_not_exclude_anything(monkeypatch) -> None:
    """The NOT_YET_IN_PRODUCTION filter is production-specific -- truealpha genuinely
    is deployed to staging, so a staging run must still cover it."""
    captured = {}

    def fake_collect_live_observations(services, *, env):
        captured["ids"] = {s.id for s in services}
        return {"services": {}}

    monkeypatch.setattr(
        check, "collect_live_observations", fake_collect_live_observations
    )
    monkeypatch.setattr(
        check, "audit_from_observations", lambda *a, **k: {"status": "pass"}
    )

    check.run(env="staging")

    assert "truealpha/app" in captured["ids"]


def test_main_returns_zero_when_only_info_results_are_present(monkeypatch) -> None:
    """#531: an info-only result (e.g. the demoted rendered-env staleness note) must
    never fail the CI step -- only a real `fail` does."""
    monkeypatch.setattr(
        check,
        "run",
        lambda env="production": {
            "schema_version": 1,
            "env": "production",
            "status": "pass",
            "generated_at": 0,
            "results": [
                {
                    "service_id": "some/service",
                    "check_id": "rendered-env-freshness",
                    "status": "info",
                    "severity": "P3",
                    "summary": "not rewritten recently",
                    "evidence": {},
                }
            ],
        },
    )

    assert check.main() == 0
