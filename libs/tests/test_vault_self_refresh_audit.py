"""Tests for Vault self-refresh audit contracts."""

from __future__ import annotations

from pathlib import Path

import yaml

from libs import vault_self_refresh_audit as vault_self_refresh_audit_module
from libs.vault_self_refresh_audit import (
    VaultService,
    _remote_container_logs,
    _remote_container_state,
    _remote_secret_file_state,
    _remote_secret_file_text,
    _vault_addr_from_env,
    audit_from_observations,
    classify_container,
    classify_optional_field_inertness,
    classify_rendered_env,
    classify_token,
    classify_vault_agent_logs,
    collect_live_observations,
    discover_vault_agent_compose_paths,
    inventory_compose_paths,
    load_inventory,
    redact,
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
    """#531: platform-prefect-worker genuinely has no secrets mount by design (it only
    needs PREFECT_API_URL, a plain env var -- confirmed against the live compose file
    and #163's independent investigation). Fixing the restart-count false positive
    unmasked this pre-existing, different check-vs-reality mismatch: applying
    app_secret_mount_path uniformly to every app_container assumed a uniformity that
    doesn't hold. The prefect SecretsFacet's `mount_exempt_containers` (#542, formerly
    the MOUNT_EXEMPT_CONTAINERS constant) must suppress the mount check for it, while
    every OTHER app_container in the same service (e.g. platform-prefect-services,
    which genuinely does read secrets) keeps being checked normally."""
    services = load_inventory()
    prefect = next(service for service in services if service.id == "platform/prefect")

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
                    "rendered_env": {"exists": True, "readable": True, "size": 20, "mtime": 0},
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
    captured = {}

    def fake_run(args, **kwargs):
        captured["args"] = args

        class Result:
            returncode = 0
            stdout = "ok"
            stderr = ""

        return Result()

    monkeypatch.setattr(vault_self_refresh_audit_module.subprocess, "run", fake_run)

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
    captured = {}

    def fake_run(args, **kwargs):
        captured["args"] = args

        class Result:
            returncode = 0
            stdout = "ok"
            stderr = ""

        return Result()

    monkeypatch.setattr(vault_self_refresh_audit_module.subprocess, "run", fake_run)

    vault_self_refresh_audit_module._ssh("vps.example", "echo hi")

    args = captured["args"]
    assert args[-2:] == ["deploy@vps.example", "echo hi"]
    assert "-i" in args and args[args.index("-i") + 1] == "/home/runner/.ssh/infra2_ci"
    assert "-p" in args and args[args.index("-p") + 1] == "2222"
    assert "StrictHostKeyChecking=no" in args


def test_remote_container_state_captures_started_at(monkeypatch) -> None:
    """#531: the live adapter must thread Docker's State.StartedAt through so
    classify_container can derive restart recency from it."""
    captured = {}

    def fake_ssh(host, command):
        captured["host"] = host
        captured["command"] = command

        class Result:
            returncode = 0
            stdout = (
                '{"State":{"Status":"running",'
                '"StartedAt":"2026-07-06T12:56:39.626381256Z",'
                '"Health":{"Status":"healthy"}},'
                '"RestartCount":1781,'
                '"Mounts":[{"Destination":"/secrets/.env"}]}'
            )
            stderr = ""

        return Result()

    monkeypatch.setattr("libs.vault_self_refresh_audit._ssh", fake_ssh)

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
    captured = {}

    def fake_ssh(host, command):
        captured["host"] = host
        captured["command"] = command

        class Result:
            returncode = 0
            stdout = "recent log line"
            stderr = ""

        return Result()

    monkeypatch.setattr("libs.vault_self_refresh_audit._ssh", fake_ssh)

    result = _remote_container_logs("vps.example", "platform-prefect-services")

    assert result == "recent log line"
    assert "--since 1h" in captured["command"]
    assert "--tail 200" in captured["command"]

    captured.clear()
    _remote_container_logs("vps.example", "platform-prefect-services", since="6h")
    assert "--since 6h" in captured["command"]


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
    optional-by-architecture and outside every other observability signal.
    #542: the watchlist now lives as `optional_inert_fields` on the owning
    service's SecretsFacet, derived into the inventory."""
    by_id = {service.id: service for service in load_inventory()}
    assert by_id["finance_report/app"].optional_inert_fields == (
        "LLM_ENCRYPTION_KEYS",
    )
    # No watchlist entries for services never flagged as having this gap.
    assert by_id["platform/postgres"].optional_inert_fields == ()


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


def test_optional_field_inertness_classifier_reports_populated_value_as_active() -> (
    None
):
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
    reports overall status "pass" -- the inertness signal never gates.

    #531: `_service()` is auth_method="approle" in the real inventory.
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

    def fake_get_env():
        return {"VPS_HOST": "vps.example", "INTERNAL_DOMAIN": "example.test"}

    def fake_get_dokploy(host=None):
        return FakeDokployClient()

    def fail_if_called(*args, **kwargs):
        raise AssertionError(
            "verify_vault_token must not be called for an AppRole service"
        )

    def fake_ssh(host, command):
        class Result:
            returncode = 0
            stdout = (
                '{"exists":true,"readable":true,"size":10,"mtime":1,'
                '"has_no_value":false}'
            )
            stderr = ""

        return Result()

    monkeypatch.setattr("libs.common.get_env", fake_get_env)
    monkeypatch.setattr("libs.dokploy.get_dokploy", fake_get_dokploy)
    monkeypatch.setattr(
        vault_self_refresh_audit_module, "verify_vault_token", fail_if_called
    )
    monkeypatch.setattr(vault_self_refresh_audit_module, "_ssh", fake_ssh)

    observations = collect_live_observations([service], env="production")

    assert observations["services"][service.id]["token_lookup"] is None
