"""Tests for Vault self-refresh audit contracts."""

from __future__ import annotations

from pathlib import Path
import types

import yaml

from common.deploy.compose_compile import load_stack
from common.security import vault_audit as vault_audit_module
from common.security import vault_self_refresh_audit as vault_self_refresh_audit_module
from common.security.vault_audit import (
    VaultService,
    audit_from_observations,
    classify_container,
    classify_deployed_template,
    classify_rendered_env,
    classify_token,
    classify_vault_agent_logs,
    discover_vault_agent_compose_paths,
    inventory_compose_paths,
    load_inventory,
    redact,
)
from common.security.vault_self_refresh_audit import (
    _remote_container_logs,
    _remote_container_state,
    _remote_secret_file_state,
    _vault_addr_from_env,
    collect_live_observations,
    write_report,
)


ROOT = Path(__file__).resolve().parents[2]


def _service():
    """The real `finance_report/app` inventory row -- `auth_method="approle"`.

    Every registered service is AppRole now (#531); this fixture is the exact
    production case the AppRole regression tests exercise.
    """
    services = load_inventory()
    return next(service for service in services if service.id == "finance_report/app")


def _token_service():
    """A synthetic `auth_method="token"` (the dataclass default) service.

    #531: the real inventory has zero remaining token-auth services, so the classic
    token-model classify_token tests need a fixture that isn't loaded from the live
    inventory to keep exercising that code path.
    """
    return VaultService(
        id="legacy/token-service",
        project="legacy",
        dokploy_service="token-service",
        compose_path="legacy/compose.yaml",
        vault_agent_config_path="legacy/vault-agent.hcl",
        secret_template_path="legacy/secrets.ctmpl",
        vault_path_template="secret/data/legacy/{env}/token-service",
        vault_agent_container="legacy-vault-agent",
        app_containers=("legacy-app",),
    )


def test_inventory_covers_every_active_vault_agent_compose() -> None:
    """#166: every active vault-agent compose path must have an audit inventory row."""
    assert discover_vault_agent_compose_paths() == inventory_compose_paths()
    # The x-infra2 todo has no `vault-agent:` text; the compiler adds the sidecar.
    assert "platform/30.todo/compose.yaml" in inventory_compose_paths()


def test_discovery_finds_an_x_infra2_sidecar_by_its_vault_block(tmp_path) -> None:
    """#1874 left this open: discovery read only the text `  vault-agent:`."""
    stacks = {
        "plain": "services:\n  vault-agent:\n    image: a:1\n",
        "plain-none": "services:\n  app:\n    image: a:1\n",
        "x-vault": "x-infra2:\n  vault:\n    name: s\nservices:\n  app:\n    image: a:1\n",
        "x-none": "x-infra2: {}\nservices:\n  app:\n    image: a:1\n",
    }
    for name, text in stacks.items():
        (tmp_path / name).mkdir()
        (tmp_path / name / "compose.yaml").write_text(text, encoding="utf-8")
    assert discover_vault_agent_compose_paths(tmp_path) == {
        "plain/compose.yaml",
        "x-vault/compose.yaml",
    }


def test_inventory_paths_exist_and_match_vault_agent_contract() -> None:
    """#166: inventory rows point at real compose, agent config, and templates."""
    for service in load_inventory():
        for relative in (
            service.compose_path,
            service.vault_agent_config_path,
            service.secret_template_path,
        ):
            assert (ROOT / relative).exists(), f"{service.id}: missing {relative}"

        # The deployed text: an x-infra2 stack gets its sidecar from the compiler.
        compose = yaml.safe_load(load_stack(service.compose_path).compiled)
        vault_agent = compose["services"]["vault-agent"]
        # The audit runs `docker exec` on this name (#1822 Slice 2).
        assert vault_agent["container_name"] == service.vault_agent_container, (
            service.id
        )
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
    """#166: token lookup failure classes are explicit and severity-tagged.

    #531: uses the synthetic token-model fixture, not the real inventory (which is
    100% AppRole now) -- this test's assertions are byte-identical to before #531.
    """
    service = _token_service()

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
    """#166: healthy periodic app tokens pass the audit token check.

    #531: uses the synthetic token-model fixture (see note above).
    """
    result = classify_token(
        _token_service(),
        "VAULT_APP_TOKEN=hvs.validlookingtoken",
        {"valid": True, "renewable": True, "ttl_hours": 240},
    )

    assert result.status == "pass"
    assert result.check_id == "vault-token"


def test_token_classifier_approle_service_passes_on_role_and_secret_id() -> None:
    """#531: an AppRole service with VAULT_ROLE_ID + VAULT_SECRET_ID present and NO
    VAULT_APP_TOKEN must report PASS -- the exact case that was silently broken in
    production (classify_token still hardcoded the legacy vault_token_env_key)."""
    service = _service()  # finance_report/app, auth_method="approle"
    assert service.auth_method == "approle"

    result = classify_token(
        service,
        "VAULT_ROLE_ID=test-role-id-not-a-real-secret\n"
        "VAULT_SECRET_ID=test-secret-id-not-a-real-secret\n",
        None,
    )

    assert result.status == "pass"
    assert result.severity == "P0"
    assert "VAULT_APP_TOKEN" not in result.summary


def test_token_classifier_approle_service_fails_when_role_id_missing() -> None:
    """#531: missing VAULT_ROLE_ID (VAULT_SECRET_ID present) is a named P0 fail."""
    service = _service()

    result = classify_token(
        service,
        "VAULT_SECRET_ID=test-secret-id-not-a-real-secret\n",
        None,
    )

    assert result.status == "fail"
    assert result.severity == "P0"
    assert "VAULT_ROLE_ID" in result.summary
    assert "missing" in result.summary
    assert result.evidence["missing"] == ["VAULT_ROLE_ID"]


def test_token_classifier_approle_service_fails_when_secret_id_missing() -> None:
    """#531: missing VAULT_SECRET_ID (VAULT_ROLE_ID present) is a named P0 fail."""
    service = _service()

    result = classify_token(
        service,
        "VAULT_ROLE_ID=test-role-id-not-a-real-secret\n",
        None,
    )

    assert result.status == "fail"
    assert result.severity == "P0"
    assert "VAULT_SECRET_ID" in result.summary
    assert result.evidence["missing"] == ["VAULT_SECRET_ID"]


def test_token_classifier_approle_service_fails_when_both_missing() -> None:
    """#531: neither AppRole credential present -- both named in the fail summary."""
    service = _service()

    result = classify_token(service, "", None)

    assert result.status == "fail"
    assert result.severity == "P0"
    assert "VAULT_ROLE_ID" in result.summary
    assert "VAULT_SECRET_ID" in result.summary
    assert result.evidence["missing"] == ["VAULT_ROLE_ID", "VAULT_SECRET_ID"]


def test_rendered_env_classifier_reports_missing_empty_and_unreadable_as_fail() -> None:
    """#166: rendered secret render-breakage failures are classified independently --
    these are genuinely broken renders, unlike the age-only staleness signal (#531,
    see test_rendered_env_classifier_reports_stale_as_info below)."""
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


def test_rendered_env_classifier_reports_stale_as_info() -> None:
    """#531: vault-agent's static_secret_render_interval only rewrites the file when
    the underlying secret's content changes, not on every poll -- an old mtime on an
    otherwise-valid file is informational (the container healthcheck is the real
    liveness signal), never a hard fail."""
    service = _service()

    stale = classify_rendered_env(
        service,
        {"exists": True, "readable": True, "size": 20, "mtime": 0},
        now=1000,
    )

    assert stale.status == "info"
    assert stale.severity == "P3"
    assert stale.check_id == "rendered-env-freshness"
    assert "healthcheck" in stale.summary
    assert "stale" not in stale.summary


def test_audit_from_observations_stays_pass_with_only_stale_render_and_healthy_container() -> (
    None
):
    """#531: when the ONLY non-pass result for a service is the info-level rendered-env
    staleness note, paired with a healthy vault_agent_container result, the overall
    audit status must stay "pass" -- the mtime signal never gates."""
    service = _service()  # auth_method="approle"
    report = audit_from_observations(
        [service],
        {
            "services": {
                service.id: {
                    "dokploy_env": (
                        "VAULT_ROLE_ID=test-role-id-not-a-real-secret\n"
                        "VAULT_SECRET_ID=test-secret-id-not-a-real-secret\n"
                    ),
                    "token_lookup": None,
                    "rendered_env": {
                        "exists": True,
                        "readable": True,
                        "size": 20,
                        "mtime": 0,
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

    assert report["status"] == "pass"
    freshness_results = [
        result
        for result in report["results"]
        if result["check_id"] == "rendered-env-freshness"
    ]
    assert len(freshness_results) == 1
    assert freshness_results[0]["status"] == "info"


def test_mount_exempt_app_container_not_flagged_for_missing_secrets_mount() -> None:
    """A SecretsFacet with mount_exempt_containers suppresses mount checks for the
    exempted container while still failing unexempt app_containers without mounts."""
    from dataclasses import replace

    services = load_inventory()
    prefect_real = next(
        service for service in services if service.id == "platform/prefect"
    )
    assert prefect_real.mount_exempt_containers == ()
    prefect = replace(
        prefect_real,
        app_containers=(*prefect_real.app_containers, "platform-prefect-worker"),
        mount_exempt_containers=("platform-prefect-worker",),
    )

    worker_state = {
        "name": "platform-prefect-worker",
        "exists": True,
        "state": "running",
        "health": "healthy",
        "restart_count": 0,
        "mounts": [],  # genuinely no secrets mount -- must NOT be flagged
    }
    services_state = {
        "name": "platform-prefect-services",
        "exists": True,
        "state": "running",
        "health": "healthy",
        "restart_count": 0,
        "mounts": [],  # DOES need the mount and doesn't have it -- must fail
    }
    report = audit_from_observations(
        [prefect],
        {
            "services": {
                prefect.id: {
                    "dokploy_env": (
                        "VAULT_ROLE_ID=test-role-id-not-a-real-secret\n"
                        "VAULT_SECRET_ID=test-secret-id-not-a-real-secret\n"
                    ),
                    "token_lookup": None,
                    "rendered_env": {
                        "exists": True,
                        "readable": True,
                        "size": 20,
                        "mtime": 0,
                    },
                    "vault_agent_logs": "",
                    "vault_agent_container": {
                        "name": "platform-prefect-vault-agent",
                        "exists": True,
                        "state": "running",
                        "health": "healthy",
                        "restart_count": 0,
                    },
                    "app_containers": [worker_state, services_state],
                }
            }
        },
        env="production",
        now=1000,
    )

    app_results = {
        (r["evidence"].get("name")): r
        for r in report["results"]
        if r["check_id"] == "app-container"
    }
    assert app_results["platform-prefect-worker"]["status"] == "pass"
    assert app_results["platform-prefect-services"]["status"] == "fail"
    assert "mount" in app_results["platform-prefect-services"]["summary"].lower()


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

    # #531: a high restart_count only fails when the most recent restart is
    # ALSO recent (State.StartedAt within the recency window of `now`) --
    # Docker's RestartCount is lifetime-cumulative, so count alone is not
    # enough. See test_container_classifier_restart_count_is_recency_bounded
    # below for the full true-positive/false-positive pair this replaced.
    high_restart_and_recent = classify_container(
        service,
        {
            "name": "backend",
            "exists": True,
            "state": "running",
            "health": "healthy",
            "restart_count": 9,
            "max_restart_count": 3,
            "started_at": "2026-07-19T11:59:30Z",
        },
        check_id="app-container",
        now=1_784_462_400,  # 2026-07-19T12:00:00Z -- 30s after started_at
    )
    assert high_restart_and_recent.status == "fail"
    assert high_restart_and_recent.severity == "P1"

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


def test_container_classifier_restart_count_is_recency_bounded() -> None:
    """#531: restart_count alone is Docker's lifetime-cumulative counter -- a
    container must be BOTH over the count threshold AND recently restarted
    (State.StartedAt within the recency window) to fail. Reproduces the
    exact live false positive (platform-prefect-services: 1781 restarts
    between 2026-06-11 and 2026-07-06, then 12+ stable days) alongside a
    true-positive control proving a genuinely-still-flapping container is
    still caught."""
    service = _service()
    now = 1_784_462_400  # 2026-07-19T12:00:00Z, matching the live 2026-07-19 re-audit

    # True positive: high count AND the last restart was moments ago -- still
    # actively flapping, must fail.
    actively_flapping = classify_container(
        service,
        {
            "name": "platform-prefect-services",
            "exists": True,
            "state": "running",
            "health": "healthy",
            "restart_count": 9,
            "max_restart_count": 3,
            "started_at": "2026-07-19T11:55:00Z",  # 5 minutes before `now`
        },
        check_id="app-container",
        now=now,
    )
    assert actively_flapping.status == "fail"
    assert actively_flapping.severity == "P1"
    assert "flapping" in actively_flapping.summary
    assert actively_flapping.evidence["restart_age_seconds"] == 300

    # False positive fix: the SAME high cumulative count, but the last
    # restart was 12+ days ago (verified live, #531) -- must pass, not fail
    # forever.
    historically_bad_now_stable = classify_container(
        service,
        {
            "name": "platform-prefect-services",
            "exists": True,
            "state": "running",
            "health": "healthy",
            "restart_count": 1781,
            "max_restart_count": 3,
            "started_at": "2026-07-06T12:56:39.626381256Z",  # 13 days before `now`
        },
        check_id="app-container",
        now=now,
    )
    assert historically_bad_now_stable.status == "pass"

    # A restart count at/under the threshold never fails, however recent.
    low_count_recent = classify_container(
        service,
        {
            "name": "platform-prefect-services",
            "exists": True,
            "state": "running",
            "health": "healthy",
            "restart_count": 3,
            "max_restart_count": 3,
            "started_at": "2026-07-19T11:59:59Z",  # 1 second before `now`
        },
        check_id="app-container",
        now=now,
    )
    assert low_count_recent.status == "pass"

    # Missing/unparseable started_at is treated as "no recency evidence" --
    # conservative, does not flag -- rather than silently defaulting to
    # either extreme.
    no_started_at = classify_container(
        service,
        {
            "name": "platform-prefect-services",
            "exists": True,
            "state": "running",
            "health": "healthy",
            "restart_count": 1781,
            "max_restart_count": 3,
        },
        check_id="app-container",
        now=now,
    )
    assert no_started_at.status == "pass"


def test_audit_report_schema_and_redaction() -> None:
    """#166: audit output is machine-readable and redacts secrets.

    #531: `_service()` (finance_report/app) is auth_method="approle" in the real
    inventory, so the env carries AppRole credentials, not a legacy VAULT_APP_TOKEN.
    """
    service = _service()
    report = audit_from_observations(
        [service],
        {
            "services": {
                service.id: {
                    "dokploy_env": (
                        "VAULT_ROLE_ID=test-role-id-not-a-real-secret\n"
                        "VAULT_SECRET_ID=test-secret-id-not-a-real-secret\n"
                    ),
                    "token_lookup": None,
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
    assert "test-role-id-not-a-real-secret" not in str(report)
    assert "test-secret-id-not-a-real-secret" not in str(report)
    assert "- PASS P0 finance_report/app::dokploy-env-approle" in write_report(report)


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


def test_ssh_uses_default_root_identity_when_no_ci_override_env_set(
    monkeypatch,
) -> None:
    """Default (in-VPS operator) invocation: byte-identical to before #531 -- no
    `-i`/`-p` flags, bare `root@host`."""
    monkeypatch.delenv("INFRA2_WATCHDOG_SSH_KEY_PATH", raising=False)
    monkeypatch.delenv("INFRA2_WATCHDOG_SSH_PORT", raising=False)
    monkeypatch.delenv("INFRA2_WATCHDOG_SSH_USER", raising=False)


def _patch_subprocess_ssh_capture(monkeypatch, stdout: str = "ok"):
    captured = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        return types.SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    monkeypatch.setattr(vault_self_refresh_audit_module.subprocess, "run", fake_run)
    return captured


def test_ssh_defaults_when_ci_override_env_vars_absent(monkeypatch) -> None:
    """When INFRA2_WATCHDOG_SSH_* are unset, `_ssh` falls back to root@<host> and
    default SSH port/key (preserves local developer execution)."""
    monkeypatch.delenv("INFRA2_WATCHDOG_SSH_KEY_PATH", raising=False)
    monkeypatch.delenv("INFRA2_WATCHDOG_SSH_PORT", raising=False)
    monkeypatch.delenv("INFRA2_WATCHDOG_SSH_USER", raising=False)
    captured = _patch_subprocess_ssh_capture(monkeypatch)

    vault_self_refresh_audit_module._ssh("vps.example", "echo hi")

    assert captured["args"][-2:] == ["root@vps.example", "echo hi"]
    assert "-i" not in captured["args"]
    assert "-p" not in captured["args"]


def test_ssh_uses_ci_override_env_vars_when_present(monkeypatch) -> None:
    """#531: a GitHub Actions runner has no ambient SSH trust for the VPS -- when the
    route-canary/watchdog jobs' INFRA2_WATCHDOG_SSH_* env vars are present, `_ssh` must
    use them explicitly (key path, port, user) instead of relying on default SSH
    config."""
    monkeypatch.setenv("INFRA2_WATCHDOG_SSH_KEY_PATH", "/home/runner/.ssh/infra2_ci")
    monkeypatch.setenv("INFRA2_WATCHDOG_SSH_PORT", "2222")
    monkeypatch.setenv("INFRA2_WATCHDOG_SSH_USER", "deploy")
    captured = _patch_subprocess_ssh_capture(monkeypatch)

    vault_self_refresh_audit_module._ssh("vps.example", "echo hi")

    args = captured["args"]
    assert args[-2:] == ["deploy@vps.example", "echo hi"]
    assert "-i" in args and args[args.index("-i") + 1] == "/home/runner/.ssh/infra2_ci"
    assert "-p" in args and args[args.index("-p") + 1] == "2222"
    assert "StrictHostKeyChecking=no" in args


def _patch_fake_ssh(
    monkeypatch,
    *,
    stdout: str = "",
    returncode: int = 0,
    stderr: str = "",
):
    captured = {}

    def fake_ssh(host, command):
        captured["host"] = host
        captured["command"] = command
        return types.SimpleNamespace(
            returncode=returncode, stdout=stdout, stderr=stderr
        )

    monkeypatch.setattr(vault_self_refresh_audit_module, "_ssh", fake_ssh)
    return captured


def test_remote_container_state_captures_started_at(monkeypatch) -> None:
    """#531: the live adapter must thread Docker's State.StartedAt through so
    classify_container can derive restart recency from it."""
    captured = _patch_fake_ssh(
        monkeypatch,
        stdout=(
            '{"State":{"Status":"running",'
            '"StartedAt":"2026-07-06T12:56:39.626381256Z",'
            '"Health":{"Status":"healthy"}},'
            '"RestartCount":1781,'
            '"Mounts":[{"Destination":"/secrets/.env"}]}'
        ),
    )

    result = _remote_container_state("vps.example", "platform-prefect-services")

    assert result["started_at"] == "2026-07-06T12:56:39.626381256Z"
    assert result["restart_count"] == 1781
    assert result["state"] == "running"
    assert captured["host"] == "vps.example"
    assert "docker inspect" in captured["command"]


def test_remote_container_logs_bounds_with_since_flag(monkeypatch) -> None:
    """#531: `docker logs` must carry a `--since` bound (default DEFAULT_LOG_SINCE)
    so a long-lived container's tail window can't still contain a resolved
    incident's log spam from weeks ago."""
    captured = _patch_fake_ssh(monkeypatch, stdout="recent log line")

    result = _remote_container_logs("vps.example", "platform-prefect-services")

    assert result == "recent log line"
    assert "--since 1h" in captured["command"]
    assert "--tail 200" in captured["command"]

    captured.clear()
    _remote_container_logs("vps.example", "platform-prefect-services", since="6h")
    assert "--since 6h" in captured["command"]


def test_remote_secret_file_state_parses_stat_json(monkeypatch) -> None:
    """#166: live adapter emits valid JSON from remote shell stat output."""
    captured = _patch_fake_ssh(
        monkeypatch,
        stdout='{"exists":true,"readable":true,"size":44,"mtime":123,"has_no_value":false}',
    )

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


def test_collect_live_observations_skips_token_lookup_for_approle_service(
    monkeypatch,
) -> None:
    """#531: the duplicate live token-read/lookup in collect_live_observations must be
    a no-op for AppRole services -- there is no static token to look up, mirroring the
    classify_token skip."""
    service = _service()  # finance_report/app, auth_method="approle"
    assert service.auth_method == "approle"

    class FakeDokployClient:
        def find_compose_by_name(self, name, project_name=None, env_name=None):
            return {
                "env": (
                    "VAULT_ROLE_ID=test-role-id-not-a-real-secret\n"
                    "VAULT_SECRET_ID=test-secret-id-not-a-real-secret\n"
                )
            }

    requested_envs: list[str | None] = []

    def fake_get_env(env_name=None):
        requested_envs.append(env_name)
        return {"VPS_HOST": "vps.example", "INTERNAL_DOMAIN": "example.test"}

    def fake_get_dokploy(host=None):
        return FakeDokployClient()

    def fail_if_called(*args, **kwargs):
        raise AssertionError(
            "verify_vault_token must not be called for an AppRole service"
        )

    monkeypatch.setattr("common.core.environ.get_env", fake_get_env)
    monkeypatch.setattr("common.deploy.dokploy_client.get_dokploy", fake_get_dokploy)
    monkeypatch.setattr(
        vault_self_refresh_audit_module, "verify_vault_token", fail_if_called
    )
    _patch_fake_ssh(
        monkeypatch,
        stdout='{"exists":true,"readable":true,"size":10,"mtime":1,"has_no_value":false}',
    )

    observations = collect_live_observations([service], env="production")

    assert observations["services"][service.id]["token_lookup"] is None
    # The audit names its environment: it never relies on a process default (#1039).
    assert requested_envs == ["production"]


def test_age_alone_is_information_however_old_the_render_is() -> None:
    """#688: treating age as a fault failed five services at 65-67 days on 2026-09-10 —
    authentik, openpanel, postgres, prefect, redis — every one a long-lived secret nobody
    had changed, each with `vault-agent-logs` passing in the same run. vault-agent only
    rewrites this file when the CONTENT changes, so mtime answers "when did the secret
    last change", never "did a change fail to arrive"."""
    service = _service()
    for age in (8 * 86400, 67 * 86400):
        old = classify_rendered_env(
            service,
            {"exists": True, "readable": True, "size": 20, "mtime": 0},
            now=age,
        )
        assert old.status == "info", age
        assert old.check_id == "rendered-env-freshness"
    recent = classify_rendered_env(
        service, {"exists": True, "readable": True, "size": 20, "mtime": 0}, now=1000
    )
    assert recent.status == "info" and recent.check_id == "rendered-env-freshness"


def test_the_deployed_template_is_compared_by_content(monkeypatch) -> None:
    """The check #628 actually needs: a single-file bind mount left pointing at a
    replaced inode has stale CONTENT and a perfectly consistent mtime, so only the
    content answers. An unreadable copy is information, never a page."""
    service = _service()
    monkeypatch.setattr(
        vault_audit_module,
        "_release_template_sha256",
        lambda _service: "a" * 64,
    )

    assert classify_deployed_template(service, "a" * 64).status == "pass"

    drifted = classify_deployed_template(service, "b" * 64)
    assert drifted.status == "fail" and drifted.severity == "P1"
    assert (
        "never reached the container" in drifted.summary and "#628" in drifted.summary
    )
    assert drifted.evidence["deployed_sha256"] == "b" * 12

    unreadable = classify_deployed_template(service, "")
    assert unreadable.status == "info"
    assert "could not compare" in unreadable.summary


def test_the_audit_does_not_fail_on_age_but_does_on_a_template_mismatch(
    monkeypatch,
) -> None:
    service = _service()

    def _observations(template_sha: str) -> dict:
        return {
            "services": {
                service.id: {
                    "dokploy_env": (
                        "VAULT_ROLE_ID=test-role-id-not-a-real-secret\n"
                        "VAULT_SECRET_ID=test-secret-id-not-a-real-secret\n"
                    ),
                    "token_lookup": None,
                    "rendered_env": {
                        "exists": True,
                        "readable": True,
                        "size": 20,
                        "mtime": 0,
                    },
                    "deployed_template_sha256": template_sha,
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
        }

    monkeypatch.setattr(
        vault_audit_module,
        "_release_template_sha256",
        lambda _service: "a" * 64,
    )

    healthy = audit_from_observations(
        [service], _observations("a" * 64), env="production", now=67 * 86400
    )
    assert healthy["status"] != "fail", "a 67-day-old render of an unchanged secret"

    mismatched = audit_from_observations(
        [service], _observations("b" * 64), env="production", now=1000
    )
    assert mismatched["status"] == "fail"
    template = [
        r for r in mismatched["results"] if r["check_id"] == "deployed-template"
    ]
    assert len(template) == 1 and template[0]["status"] == "fail"


def test_a_preview_stack_is_reported_as_skipped_not_measured_on_the_fixed_stack(
    monkeypatch,
) -> None:
    """A preview facet names its containers with ``${ENV_SUFFIX}``, which the audit
    resolves per fixed env — so `finance_report/preview` was measured on
    `finance_report-app-vault-agent`, the PRODUCTION agent: trivially green for months,
    then a P1 template mismatch on 2026-09-15 once #692 compared the preview template
    against the fixed agent's. The audit now says what it cannot measure."""
    services = {service.id: service for service in load_inventory()}
    assert services["finance_report/preview"].ephemeral
    assert services["truealpha/preview"].ephemeral
    assert not services["finance_report/app"].ephemeral
    assert not services["truealpha/app"].ephemeral

    monkeypatch.setattr(
        vault_audit_module,
        "_release_template_sha256",
        lambda _service: "a" * 64,
    )
    preview = services["finance_report/preview"]
    # No observation at all for the preview (the collector skips it) — and no
    # observation must never read as a mismatch or a missing container.
    report = audit_from_observations([preview], {"services": {}}, env="production")
    assert report["status"] == "pass"
    (only,) = report["results"]
    assert (only["check_id"], only["status"], only["severity"]) == (
        "preview-stack",
        "info",
        "P3",
    )
    assert "-branch-main" in only["summary"]


def test_safe_excerpt_redacts_keys_with_spaces_and_quotes() -> None:
    raw_log = (
        "INFO Starting service\n"
        "DEBUG password: my_super_secret_password\n"
        'DEBUG key = "super_secret_key"\n'
        "DEBUG authorization: Bearer s.vaultToken123456\n"
        "INFO Running normally"
    )
    excerpt = vault_audit_module._safe_excerpt(raw_log)
    assert "my_super_secret_password" not in excerpt
    assert "super_secret_key" not in excerpt
    assert "s.vaultToken123456" not in excerpt
    assert "***REDACTED***" in excerpt


def test_audit_token_redacts_malformed_token_in_evidence() -> None:
    import dataclasses
    import json

    service = dataclasses.replace(load_inventory()[0], auth_method="token")
    malformed_token = "secret1234"
    result = classify_token(
        service,
        env_text=f"{service.vault_token_env_key}={malformed_token}\n",
        lookup=None,
    )
    assert result.status == "fail"
    assert result.check_id == "dokploy-env-token"
    assert malformed_token not in json.dumps(result.evidence)
    assert "token_hint" in result.evidence
    assert result.evidence["token_length"] == 10
