"""Tests for Vault self-refresh audit contracts."""

from __future__ import annotations

from pathlib import Path

import yaml

from libs.vault_self_refresh_audit import (
    OPTIONAL_INERT_FIELD_WATCHLIST,
    _remote_secret_file_state,
    _remote_secret_file_text,
    _vault_addr_from_env,
    audit_from_observations,
    classify_container,
    classify_optional_field_inertness,
    classify_rendered_env,
    classify_token,
    classify_vault_agent_logs,
    discover_vault_agent_compose_paths,
    inventory_compose_paths,
    load_inventory,
    optional_inert_fields_for,
    redact,
    write_report,
)


ROOT = Path(__file__).resolve().parents[2]


def _service():
    services = load_inventory()
    return next(service for service in services if service.id == "finance_report/app")


def test_inventory_covers_every_active_vault_agent_compose() -> None:
    """#166: every active vault-agent compose path must have an audit inventory row."""
    assert discover_vault_agent_compose_paths() == inventory_compose_paths()


def test_inventory_paths_exist_and_match_vault_agent_contract() -> None:
    """#166: inventory rows point at real compose, agent config, and templates."""
    for service in load_inventory():
        for relative in (
            service.compose_path,
            service.vault_agent_config_path,
            service.secret_template_path,
        ):
            assert (ROOT / relative).exists(), f"{service.id}: missing {relative}"

        compose = yaml.safe_load((ROOT / service.compose_path).read_text())
        vault_agent = compose["services"]["vault-agent"]
        agent_env = str(vault_agent.get("environment", {}))
        for env_key in service.auth_env_keys:
            assert env_key in agent_env, f"{service.id}: vault-agent missing {env_key}"
        assert service.rendered_secret_path in str(vault_agent)
        # Healthcheck must validate the live agent token via Vault lookup-self,
        # not merely that a file exists. Functional marker so it holds for both
        # static-token and AppRole agents (only their comment text differs).
        assert "lookup-self" in str(vault_agent.get("healthcheck", {}))
        assert "<no value>" in str(vault_agent.get("healthcheck", {}))
        assert "stat -c %Y" not in str(vault_agent.get("healthcheck", {}))
        assert "VAULT_AGENT_MAX_SECRET_AGE_SECONDS" not in str(
            vault_agent.get("healthcheck", {})
        )


def test_token_classifier_reports_missing_malformed_invalid_nonrenewable_and_low_ttl() -> (
    None
):
    """#166: token lookup failure classes are explicit and severity-tagged."""
    service = _service()

    missing = classify_token(service, "", None)
    assert missing.status == "fail"
    assert missing.severity == "P0"
    assert "missing" in missing.summary

    malformed = classify_token(service, "VAULT_APP_TOKEN=bad token", None)
    assert malformed.status == "fail"
    assert "malformed" in malformed.summary

    invalid = classify_token(
        service,
        "VAULT_APP_TOKEN=hvs.validlookingtoken",
        {"valid": False, "error": "403"},
    )
    assert invalid.status == "fail"
    assert invalid.check_id == "vault-token-lookup"

    nonrenewable = classify_token(
        service,
        "VAULT_APP_TOKEN=hvs.validlookingtoken",
        {"valid": True, "renewable": False, "ttl_hours": 72},
    )
    assert nonrenewable.status == "fail"
    assert nonrenewable.check_id == "vault-token-renewable"

    low_ttl = classify_token(
        service,
        "VAULT_APP_TOKEN=hvs.validlookingtoken",
        {"valid": True, "renewable": True, "ttl_hours": 12},
    )
    assert low_ttl.status == "fail"
    assert low_ttl.severity == "P1"
    assert low_ttl.check_id == "vault-token-ttl"


def test_token_classifier_accepts_healthy_renewable_token() -> None:
    """#166: healthy periodic app tokens pass the audit token check."""
    result = classify_token(
        _service(),
        "VAULT_APP_TOKEN=hvs.validlookingtoken",
        {"valid": True, "renewable": True, "ttl_hours": 240},
    )

    assert result.status == "pass"
    assert result.check_id == "vault-token"


def test_rendered_env_classifier_reports_missing_empty_stale_and_unreadable() -> None:
    """#166: rendered secret freshness failures are classified independently."""
    service = _service()

    assert classify_rendered_env(service, {"exists": False}, now=1000).status == "fail"
    assert (
        classify_rendered_env(
            service,
            {"exists": True, "readable": False, "size": 1, "mtime": 999},
            now=1000,
        ).summary
        == "/vault/secrets/.env is not readable"
    )
    assert (
        classify_rendered_env(
            service,
            {"exists": True, "readable": True, "size": 0, "mtime": 999},
            now=1000,
        ).summary
        == "/vault/secrets/.env is empty"
    )
    stale = classify_rendered_env(
        service,
        {"exists": True, "readable": True, "size": 20, "mtime": 0},
        now=1000,
    )
    assert stale.status == "fail"
    assert stale.check_id == "rendered-env-freshness"

    unresolved = classify_rendered_env(
        service,
        {
            "exists": True,
            "readable": True,
            "size": 20,
            "mtime": 999,
            "has_no_value": True,
        },
        now=1000,
    )
    assert unresolved.status == "fail"
    assert unresolved.severity == "P0"
    assert unresolved.check_id == "rendered-env-template-values"


def test_rendered_env_classifier_accepts_fresh_nonempty_file() -> None:
    """#166: fresh rendered env files pass the audit."""
    result = classify_rendered_env(
        _service(),
        {"exists": True, "readable": True, "size": 20, "mtime": 950},
        now=1000,
    )

    assert result.status == "pass"
    assert result.evidence["age_seconds"] == 50


def test_vault_agent_log_classifier_detects_refresh_errors_and_redacts() -> None:
    """#166: agent log audit catches known render/renewal failures."""
    result = classify_vault_agent_logs(
        _service(),
        "template render failed: permission denied token=hvs.should-not-leak",
    )

    assert result.status == "fail"
    assert "permission denied" in result.evidence["matched_patterns"]
    assert "should-not-leak" not in result.evidence["log_excerpt"]


def test_vault_agent_log_classifier_detects_approle_missing_creds() -> None:
    """AppRole services crash-loop with this message (not the legacy
    VAULT_APP_TOKEN one) when role_id/secret_id are unset/wiped; the audit must
    catch it."""
    result = classify_vault_agent_logs(
        _service(),
        "VAULT_ROLE_ID and VAULT_SECRET_ID are required",
    )

    assert result.status == "fail"
    assert (
        "VAULT_ROLE_ID and VAULT_SECRET_ID are required"
        in result.evidence["matched_patterns"]
    )


def test_container_classifier_checks_state_health_restarts_and_mounts() -> None:
    """#166: live container state is classified without mutating containers."""
    service = _service()

    stopped = classify_container(
        service,
        {"name": "backend", "exists": True, "state": "exited"},
        check_id="app-container",
    )
    assert stopped.status == "fail"
    assert stopped.severity == "P0"

    unhealthy = classify_container(
        service,
        {"name": "backend", "exists": True, "state": "running", "health": "unhealthy"},
        check_id="app-container",
    )
    assert unhealthy.status == "fail"

    high_restart = classify_container(
        service,
        {
            "name": "backend",
            "exists": True,
            "state": "running",
            "health": "healthy",
            "restart_count": 9,
            "max_restart_count": 3,
        },
        check_id="app-container",
    )
    assert high_restart.status == "fail"
    assert high_restart.severity == "P1"

    missing_mount = classify_container(
        service,
        {
            "name": "backend",
            "exists": True,
            "state": "running",
            "health": "healthy",
            "restart_count": 0,
            "mounts": [],
        },
        check_id="app-container",
        expected_mount="/secrets/.env",
    )
    assert missing_mount.status == "fail"

    directory_mount = classify_container(
        service,
        {
            "name": "backend",
            "exists": True,
            "state": "running",
            "health": "healthy",
            "restart_count": 0,
            "mounts": ["/secrets"],
        },
        check_id="app-container",
        expected_mount="/secrets/.env",
    )
    assert directory_mount.status == "pass"

    healthy = classify_container(
        service,
        {
            "name": "backend",
            "exists": True,
            "state": "running",
            "health": "healthy",
            "restart_count": 0,
            "mounts": ["/secrets/.env"],
        },
        check_id="app-container",
        expected_mount="/secrets/.env",
    )
    assert healthy.status == "pass"


def test_audit_report_schema_and_redaction() -> None:
    """#166: audit output is machine-readable and redacts secrets."""
    service = _service()
    report = audit_from_observations(
        [service],
        {
            "services": {
                service.id: {
                    "dokploy_env": "VAULT_APP_TOKEN=hvs.validlookingtoken",
                    "token_lookup": {
                        "valid": True,
                        "renewable": True,
                        "ttl_hours": 240,
                    },
                    "rendered_env": {
                        "exists": True,
                        "readable": True,
                        "size": 20,
                        "mtime": 950,
                    },
                    "vault_agent_logs": "template rendered successfully",
                    "vault_agent_container": {
                        "name": "finance_report-app-vault-agent",
                        "exists": True,
                        "state": "running",
                        "health": "healthy",
                        "restart_count": 0,
                    },
                    "app_containers": [
                        {
                            "name": "finance_report-backend",
                            "exists": True,
                            "state": "running",
                            "health": "healthy",
                            "restart_count": 0,
                            "mounts": ["/secrets/.env"],
                        }
                    ],
                }
            }
        },
        env="production",
        now=1000,
    )

    assert report["schema_version"] == 1
    assert report["status"] == "pass"
    assert report["results"]
    assert "validlookingtoken" not in str(report)
    assert "- PASS P0 finance_report/app::vault-token" in write_report(report)


def test_redact_masks_nested_secret_like_keys() -> None:
    assert redact({"token": "abc", "nested": [{"PASSWORD": "def", "ok": "yes"}]}) == {
        "token": "***REDACTED***",
        "nested": [{"PASSWORD": "***REDACTED***", "ok": "yes"}],
    }


def test_vault_addr_prefers_explicit_addr_then_internal_domain() -> None:
    assert _vault_addr_from_env({"VAULT_ADDR": "https://vault.example"}) == (
        "https://vault.example"
    )
    assert _vault_addr_from_env({"INTERNAL_DOMAIN": "example.test"}) == (
        "https://vault.example.test"
    )
    assert _vault_addr_from_env({}) is None


def test_remote_secret_file_state_parses_stat_json(monkeypatch) -> None:
    """#166: live adapter emits valid JSON from remote shell stat output."""
    captured = {}

    def fake_ssh(host, command):
        captured["host"] = host
        captured["command"] = command

        class Result:
            returncode = 0
            stdout = (
                '{"exists":true,"readable":true,"size":44,'
                '"mtime":123,"has_no_value":false}'
            )
            stderr = ""

        return Result()

    monkeypatch.setattr("libs.vault_self_refresh_audit._ssh", fake_ssh)

    result = _remote_secret_file_state("vps.example", "platform-postgres-vault-agent")

    assert result == {
        "exists": True,
        "readable": True,
        "size": 44,
        "mtime": 123,
        "has_no_value": False,
    }
    assert captured["host"] == "vps.example"
    assert "docker exec platform-postgres-vault-agent sh -lc" in captured["command"]


def test_remote_secret_file_text_returns_raw_cat_output(monkeypatch) -> None:
    """#526: the raw-content fetch used for optional-field inertness checks
    just `cat`s the rendered file and returns stdout verbatim."""
    captured = {}

    def fake_ssh(host, command):
        captured["host"] = host
        captured["command"] = command

        class Result:
            returncode = 0
            stdout = 'LLM_ENCRYPTION_KEYS=""\nOTHER_KEY="value"\n'
            stderr = ""

        return Result()

    monkeypatch.setattr("libs.vault_self_refresh_audit._ssh", fake_ssh)

    result = _remote_secret_file_text("vps.example", "finance_report-app-vault-agent")

    assert result == 'LLM_ENCRYPTION_KEYS=""\nOTHER_KEY="value"\n'
    assert captured["host"] == "vps.example"
    assert "docker exec finance_report-app-vault-agent sh -lc" in captured["command"]
    assert "cat /vault/secrets/.env" in captured["command"]


def test_remote_secret_file_text_returns_empty_on_ssh_failure(monkeypatch) -> None:
    def fake_ssh(host, command):
        class Result:
            returncode = 1
            stdout = ""
            stderr = "connection refused"

        return Result()

    monkeypatch.setattr("libs.vault_self_refresh_audit._ssh", fake_ssh)

    assert _remote_secret_file_text("vps.example", "some-vault-agent") == ""


def test_optional_inert_field_watchlist_covers_llm_encryption_keys() -> None:
    """#526: LLM_ENCRYPTION_KEYS is the one field the issue identified as
    optional-by-architecture and outside every other observability signal."""
    assert ("finance_report/app", "LLM_ENCRYPTION_KEYS") in OPTIONAL_INERT_FIELD_WATCHLIST
    assert optional_inert_fields_for("finance_report/app") == ("LLM_ENCRYPTION_KEYS",)
    # No watchlist entries for services never flagged as having this gap.
    assert optional_inert_fields_for("platform/postgres") == ()


def test_optional_field_inertness_classifier_reports_empty_value_as_inert() -> None:
    """#526: LLM_ENCRYPTION_KEYS="" (the secrets.ctmpl unset-render shape) is
    reported informationally, never as a failure."""
    service = _service()

    result = classify_optional_field_inertness(
        service, "LLM_ENCRYPTION_KEYS", 'LLM_ENCRYPTION_KEYS=""\n'
    )

    assert result.status == "info"
    assert result.severity == "P3"
    assert "inert" in result.summary
    assert result.evidence == {"field": "LLM_ENCRYPTION_KEYS", "populated": False}


def test_optional_field_inertness_classifier_reports_missing_field_as_inert() -> None:
    """#526: a field absent entirely from the rendered file is also inert."""
    service = _service()

    result = classify_optional_field_inertness(
        service, "LLM_ENCRYPTION_KEYS", "OTHER_KEY=value\n"
    )

    assert result.status == "info"
    assert result.evidence["populated"] is False


def test_optional_field_inertness_classifier_reports_populated_value_as_active() -> None:
    """#526: once a real value is provisioned, the field reports as active --
    still informational, not a pass/fail gate."""
    service = _service()

    result = classify_optional_field_inertness(
        service,
        "LLM_ENCRYPTION_KEYS",
        'LLM_ENCRYPTION_KEYS="gAAAAA-fake-fernet-key-material"\n',
    )

    assert result.status == "info"
    assert "active" in result.summary
    assert result.evidence == {"field": "LLM_ENCRYPTION_KEYS", "populated": True}
    # The actual key material must never leak into the report evidence.
    assert "fake-fernet-key-material" not in str(result.evidence)


def test_audit_from_observations_reports_inert_field_without_failing_audit() -> None:
    """#526: an otherwise-healthy service with an inert optional field still
    reports overall status "pass" -- the inertness signal never gates."""
    service = _service()
    report = audit_from_observations(
        [service],
        {
            "services": {
                service.id: {
                    "dokploy_env": "VAULT_APP_TOKEN=hvs.validlookingtoken",
                    "token_lookup": {
                        "valid": True,
                        "renewable": True,
                        "ttl_hours": 240,
                    },
                    "rendered_env": {
                        "exists": True,
                        "readable": True,
                        "size": 20,
                        "mtime": 950,
                    },
                    "rendered_env_text": 'LLM_ENCRYPTION_KEYS=""\n',
                    "vault_agent_logs": "template rendered successfully",
                    "vault_agent_container": {
                        "name": "finance_report-app-vault-agent",
                        "exists": True,
                        "state": "running",
                        "health": "healthy",
                        "restart_count": 0,
                    },
                    "app_containers": [
                        {
                            "name": "finance_report-backend",
                            "exists": True,
                            "state": "running",
                            "health": "healthy",
                            "restart_count": 0,
                            "mounts": ["/secrets/.env"],
                        }
                    ],
                }
            }
        },
        env="production",
        now=1000,
    )

    assert report["status"] == "pass"
    inertness_results = [
        result
        for result in report["results"]
        if result["check_id"] == "optional-field-inertness::LLM_ENCRYPTION_KEYS"
    ]
    assert len(inertness_results) == 1
    assert inertness_results[0]["status"] == "info"
    assert inertness_results[0]["evidence"]["populated"] is False
    assert (
        "INFO P3 finance_report/app::optional-field-inertness::LLM_ENCRYPTION_KEYS"
        in write_report(report)
    )
