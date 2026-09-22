"""Tests for the pre-deploy schema gate's SSH/docker wiring (#698, TODOWRITE:20).

No live SSH/docker here -- ``runner`` is always a fake that records the exact commands
it was asked to run and returns a canned result, so these tests exercise the command
construction and fail-closed decision logic in isolation.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from libs.deploy import schema_gate as sg

FIXED_FINANCE_REPORT_COMPOSE = (
    Path(__file__).parents[2] / "finance_report/finance_report/10.app/compose.yaml"
)


class FakeRunner:
    """Records every call; replays canned CompletedProcess-shaped results in order."""

    def __init__(self, results):
        self._results = list(results)
        self.calls: list[dict] = []

    def __call__(self, args, **kwargs):
        self.calls.append({"args": args, **kwargs})
        return self._results.pop(0)


def _ok(stdout="", stderr=""):
    return SimpleNamespace(returncode=0, stdout=stdout, stderr=stderr)


def _fail(code, stdout="", stderr=""):
    return SimpleNamespace(returncode=code, stdout=stdout, stderr=stderr)


# --- gate_applies -----------------------------------------------------------------


def test_gate_applies_true_for_a_registered_service():
    assert sg.gate_applies("finance_report/app") is True


def test_gate_applies_false_for_an_unregistered_service():
    # truealpha/app has no ENUM_SOURCES entry yet -- must not be gated by accident.
    assert sg.gate_applies("truealpha/app") is False
    assert sg.gate_applies("platform/redis") is False


# --- SSH argv construction ----------------------------------------------------------


def test_ssh_args_uses_watchdog_key_port_user_when_provisioned(monkeypatch):
    monkeypatch.setenv("INFRA2_WATCHDOG_SSH_KEY_PATH", "/tmp/key")
    monkeypatch.setenv("INFRA2_WATCHDOG_SSH_PORT", "2222")
    monkeypatch.setenv("INFRA2_WATCHDOG_SSH_USER", "deploy")
    args = sg._ssh_args("1.2.3.4")
    assert "-i" in args and args[args.index("-i") + 1] == "/tmp/key"
    assert "-p" in args and args[args.index("-p") + 1] == "2222"
    assert args[-1] == "deploy@1.2.3.4"


def test_ssh_args_omits_key_and_port_flags_when_absent(monkeypatch):
    monkeypatch.delenv("INFRA2_WATCHDOG_SSH_KEY_PATH", raising=False)
    monkeypatch.delenv("INFRA2_WATCHDOG_SSH_PORT", raising=False)
    monkeypatch.delenv("INFRA2_WATCHDOG_SSH_USER", raising=False)
    args = sg._ssh_args("1.2.3.4")
    assert "-i" not in args
    assert "-p" not in args
    assert args[-1] == "root@1.2.3.4"  # default user


def test_ssh_host_prefers_watchdog_host_over_vps_host(monkeypatch):
    monkeypatch.setenv("INFRA2_WATCHDOG_SSH_HOST", "watchdog.example")
    monkeypatch.setenv("VPS_HOST", "vps.example")
    assert sg._ssh_host() == "watchdog.example"


def test_ssh_host_falls_back_to_vps_host(monkeypatch):
    monkeypatch.delenv("INFRA2_WATCHDOG_SSH_HOST", raising=False)
    monkeypatch.setenv("VPS_HOST", "vps.example")
    assert sg._ssh_host() == "vps.example"


def test_ssh_host_raises_without_either_env_var(monkeypatch):
    monkeypatch.delenv("INFRA2_WATCHDOG_SSH_HOST", raising=False)
    monkeypatch.delenv("VPS_HOST", raising=False)
    with pytest.raises(sg.SchemaGateError, match="no VPS host"):
        sg._ssh_host()


# --- vault-agent container resolution ------------------------------------------------


def test_vault_agent_container_resolves_from_the_real_compose_file():
    text = FIXED_FINANCE_REPORT_COMPOSE.read_text(encoding="utf-8")
    from libs.deploy.in_service import container_names

    assert container_names(text, "-staging")["vault-agent"] == (
        "finance_report-app-vault-agent-staging"
    )
    name = sg._vault_agent_container(
        "finance_report/app",
        "finance_report/finance_report/10.app/compose.yaml",
        "-staging",
    )
    assert name == "finance_report-app-vault-agent-staging"


def test_vault_agent_container_raises_when_compose_declares_none(tmp_path):
    compose = tmp_path / "compose.yaml"
    compose.write_text("services:\n  backend:\n    container_name: x\n")
    # An ABSOLUTE compose_path: Path(REPO_ROOT) / <absolute> resolves to the absolute
    # path outright (pathlib's own `/` semantics), so this needs no REPO_ROOT patching.
    with pytest.raises(sg.SchemaGateError, match="declares no 'vault-agent'"):
        sg._vault_agent_container("some/service", str(compose), "")


# --- DATABASE_URL retrieval -----------------------------------------------------------


def test_read_database_url_parses_the_rendered_secrets_file():
    runner = FakeRunner(
        [_ok(stdout='OTHER=1\nDATABASE_URL="postgresql+asyncpg://u:p@h:5432/db"\n')]
    )
    url = sg._read_database_url("1.2.3.4", "vault-agent-x", runner=runner, timeout=5)
    assert url == "postgresql+asyncpg://u:p@h:5432/db"
    call = runner.calls[0]
    assert call["args"][0] == "ssh"
    assert "docker exec vault-agent-x" in call["args"][-1]
    assert call["timeout"] == 5


def test_read_database_url_raises_on_ssh_failure():
    runner = FakeRunner([_fail(255, stderr="ssh: connect refused")])
    with pytest.raises(sg.SchemaGateError, match="could not read the rendered secrets"):
        sg._read_database_url("1.2.3.4", "vault-agent-x", runner=runner, timeout=5)


def test_read_database_url_raises_when_key_is_absent():
    runner = FakeRunner([_ok(stdout="OTHER=1\n")])
    with pytest.raises(sg.SchemaGateError, match="rendered no DATABASE_URL"):
        sg._read_database_url("1.2.3.4", "vault-agent-x", runner=runner, timeout=5)


# --- run_schema_gate: the fail-closed decision ----------------------------------------


def _gate_kwargs(**overrides):
    kwargs = dict(
        service="finance_report/app",
        compose_path="finance_report/finance_report/10.app/compose.yaml",
        env_suffix="-staging",
        image_ref="abc1234",
        host="1.2.3.4",
    )
    kwargs.update(overrides)
    return kwargs


def test_run_schema_gate_returns_rollback_class_on_a_clean_pass():
    runner = FakeRunner(
        [
            _ok(stdout='DATABASE_URL="postgresql://u:p@h:5432/db"\n'),
            _ok(stdout="Pre-deploy schema check verified...\nROLLBACK_CLASS: A\n"),
        ]
    )
    result = sg.run_schema_gate(**_gate_kwargs(runner=runner, timeout=5))
    assert result == "A"


def test_run_schema_gate_docker_command_overrides_entrypoint_and_attaches_network():
    runner = FakeRunner(
        [
            _ok(stdout='DATABASE_URL="postgresql://u:p@h:5432/db"\n'),
            _ok(stdout="ROLLBACK_CLASS: A\n"),
        ]
    )
    sg.run_schema_gate(**_gate_kwargs(runner=runner, timeout=5))
    docker_call = runner.calls[1]
    remote_cmd = docker_call["args"][-1]
    assert "docker run --rm -i --network dokploy-network" in remote_cmd
    assert "--entrypoint python3" in remote_cmd
    assert "-e DATABASE_URL=" in remote_cmd
    assert "ghcr.io/wangzitian0/finance_report-backend:abc1234" in remote_cmd
    assert "--service finance_report/app" in remote_cmd
    # the check script itself travels as stdin, never baked into the command string or
    # left for the remote side to fetch/checkout.
    assert docker_call["input"] == sg._SCHEMA_CHECK_SCRIPT.read_text(encoding="utf-8")


def test_run_schema_gate_blocks_on_exit_1_discrepancy():
    runner = FakeRunner(
        [
            _ok(stdout='DATABASE_URL="postgresql://u:p@h:5432/db"\n'),
            _fail(1, stdout="ERROR: Schema discrepancies found\nROLLBACK_CLASS: C\n"),
        ]
    )
    with pytest.raises(
        sg.SchemaGateError, match="BLOCKED finance_report/app \\(exit 1\\)"
    ):
        sg.run_schema_gate(**_gate_kwargs(runner=runner, timeout=5))


def test_run_schema_gate_blocks_on_exit_3_not_evaluated():
    """#718 review: NOT EVALUATED must block exactly like a real discrepancy."""
    runner = FakeRunner(
        [
            _ok(stdout='DATABASE_URL="postgresql://u:p@h:5432/db"\n'),
            _fail(3, stderr="NOT EVALUATED — deploy blocked: no database URL"),
        ]
    )
    with pytest.raises(
        sg.SchemaGateError, match="BLOCKED finance_report/app \\(exit 3\\)"
    ):
        sg.run_schema_gate(**_gate_kwargs(runner=runner, timeout=5))


def test_run_schema_gate_blocks_on_an_unexpected_exit_code():
    """Not just 1/3 -- ANY non-zero (e.g. a docker-level failure) blocks too."""
    runner = FakeRunner(
        [
            _ok(stdout='DATABASE_URL="postgresql://u:p@h:5432/db"\n'),
            _fail(125, stderr="docker: Error response from daemon: no such image"),
        ]
    )
    with pytest.raises(sg.SchemaGateError, match="exit 125"):
        sg.run_schema_gate(**_gate_kwargs(runner=runner, timeout=5))


def test_run_schema_gate_blocks_on_ssh_transport_failure():
    runner = FakeRunner([_fail(255, stderr="ssh: connect refused")])
    with pytest.raises(sg.SchemaGateError, match="could not read the rendered secrets"):
        sg.run_schema_gate(**_gate_kwargs(runner=runner, timeout=5))


def test_run_schema_gate_blocks_an_unparseable_pass_rather_than_trusting_it():
    runner = FakeRunner(
        [
            _ok(stdout='DATABASE_URL="postgresql://u:p@h:5432/db"\n'),
            _ok(stdout="looks fine but no ROLLBACK_CLASS line\n"),
        ]
    )
    with pytest.raises(sg.SchemaGateError, match="no ROLLBACK_CLASS"):
        sg.run_schema_gate(**_gate_kwargs(runner=runner, timeout=5))


def test_run_schema_gate_raises_for_a_service_with_no_registered_backend_image():
    runner = FakeRunner([])
    with pytest.raises(sg.SchemaGateError, match="no registered backend image"):
        sg.run_schema_gate(
            **_gate_kwargs(service="not/registered-but-gated", runner=runner, timeout=5)
        )
    assert runner.calls == []  # never even attempted SSH
