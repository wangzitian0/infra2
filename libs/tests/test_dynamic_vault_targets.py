"""Tests for dynamic Vault token targets discovery (SSOT convergence)."""

from libs.core.constants import REPO_ROOT
from libs.tests.test_vault_token_lifecycle import _load_vault_tasks


def test_vault_token_targets_contains_all_core_services(monkeypatch):
    """Verify dynamic discovery finds all platform, finance_report, and truealpha targets."""
    tasks, _exit_cls = _load_vault_tasks(monkeypatch)
    targets = tasks._vault_token_targets(str(REPO_ROOT))

    expected_services = {
        ("bootstrap", "iac_runner"),
        ("platform", "postgres"),
        ("platform", "redis"),
        ("platform", "s3"),
        ("platform", "minio"),
        ("platform", "authentik"),
        ("platform", "alerting"),
        ("platform", "prefect"),
        ("platform", "openpanel"),
        ("platform", "todo"),
        ("finance_report", "postgres"),
        ("finance_report", "redis"),
        ("finance_report", "app"),
        ("truealpha", "postgres"),
        ("truealpha", "app"),
        ("truealpha", "data_engine"),
    }

    found = {(t.project, t.service) for t in targets}
    for expected in expected_services:
        assert expected in found, f"Missing target: {expected}"


def test_vault_token_targets_dynamically_discovers_new_service(monkeypatch, tmp_path):
    """Verify that adding a new service directory automatically registers as a Vault target without code edits."""
    tasks, _exit_cls = _load_vault_tasks(monkeypatch)

    # Create a mock repo structure
    apps_dir = tmp_path / "apps" / "payment_gateway"
    apps_dir.mkdir(parents=True)
    (apps_dir / "compose.yaml").write_text("services: {}\n")

    targets = tasks._vault_token_targets(str(tmp_path))
    assert any(
        t.project == "apps"
        and t.service == "payment_gateway"
        and t.service_dir == "payment_gateway"
        for t in targets
    ), "Dynamic discovery failed to detect new app"
