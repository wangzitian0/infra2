"""Tests for the pre-deploy schema gate's SSH/docker wiring (#698, #1824, TODOWRITE:20).

No live SSH/docker here. ``runner`` is a fake that records the exact commands and
returns a canned result, so these tests examine the command and the fail-closed
decision in isolation. The ``_LAUNCHER`` tests run the launcher in a real Python
subprocess against a stub SDK and against the installed SDK.
"""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from common.deploy import schema_gate as sg

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
    # truealpha/app has no _SCHEMA_CHECK_SPECS entry (no SQLAlchemy) -- never gated.
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
    from common.deploy.compose_compile import load_stack
    from common.deploy.in_service import container_names

    # The compiled text holds the sidecar of the x-infra2 stack (#1822 Slice 3).
    text = load_stack(FIXED_FINANCE_REPORT_COMPOSE).compiled

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


def test_read_database_url_failure_never_echoes_captured_stdout():
    """The command reads /vault/secrets/.env -- a rendered secrets file. A failure
    exception must never carry its stdout, or one failed read could print a real
    credential straight into a public GitHub Actions step log."""
    secret_marker = "sk-live-SECRET-DO-NOT-LEAK-1234567890"
    runner = FakeRunner(
        [_fail(1, stdout=f'DATABASE_URL="postgresql://u:{secret_marker}@h/db"\n')]
    )
    with pytest.raises(sg.SchemaGateError) as exc_info:
        sg._read_database_url("1.2.3.4", "vault-agent-x", runner=runner, timeout=5)
    message = str(exc_info.value)
    assert secret_marker not in message
    # still identifies the failing step + exit code for ops triage -- withholding the
    # secret content must not degrade into withholding everything.
    assert "vault-agent-x" in message
    assert "1.2.3.4" in message
    assert "exit 1" in message


def test_read_database_url_failure_never_echoes_captured_stderr():
    """Same guarantee for stderr -- the old code fell back to it when stdout was
    empty, which is exactly as capable of carrying rendered secret content."""
    secret_marker = "sk-live-SECRET-DO-NOT-LEAK-in-stderr"
    runner = FakeRunner([_fail(2, stderr=f"partial read: {secret_marker}")])
    with pytest.raises(sg.SchemaGateError) as exc_info:
        sg._read_database_url("1.2.3.4", "vault-agent-x", runner=runner, timeout=5)
    assert secret_marker not in str(exc_info.value)


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


def test_run_schema_gate_docker_command_runs_the_sdk_check_with_the_spec_options():
    url = "postgresql://u:p@h:5432/db"
    runner = FakeRunner(
        [
            _ok(stdout=f'DATABASE_URL="{url}"\n'),
            _ok(stdout="ROLLBACK_CLASS: A\n"),
        ]
    )
    sg.run_schema_gate(**_gate_kwargs(runner=runner, timeout=5))
    docker_call = runner.calls[1]
    assert docker_call["args"][0] == "ssh"
    assert docker_call["args"][-2] == "root@1.2.3.4"
    assert shlex.split(docker_call["args"][-1]) == [
        "docker",
        "run",
        "--rm",
        "-i",
        "--network",
        "dokploy-network",
        "--memory=1g",
        "--cpus=1",
        "--entrypoint",
        "python3",
        "ghcr.io/wangzitian0/finance_report-backend:abc1234",
        "-c",
        sg._LAUNCHER,
        "--metadata",
        "src.database:Base.metadata",
        "--import",
        "src.counter.extension.sql",
        "--import",
        "src.identity.extension.sql",
        "--import",
        "src.orm_registry",
        "--import",
        "src.platform.extension.sql",
        "--label",
        "finance_report/app",
    ]
    # The database URL travels on stdin only; the launcher adds it as --db-url.
    assert docker_call["input"] == f"{url}\n"
    assert url not in docker_call["args"][-1]
    assert docker_call["timeout"] == 5


def test_run_schema_gate_blocked_message_includes_the_rollback_class_when_present():
    """The blocked path is exactly when an operator most wants the classification --
    it must not depend on the raw diagnostic dump happening to still contain the line
    after truncation (#783 review). ROLLBACK_CLASS is printed FIRST here and the dump
    is truncated to its last 2000 chars, so a plain "is the raw text present somewhere"
    check would pass by accident even without parsing it out explicitly; put enough
    filler after it that only an explicit parse (of the FULL, untruncated stdout)
    survives the truncation.
    """
    stdout = "ROLLBACK_CLASS: C\n" + ("noise " * 400)  # > 2000 chars of trailing filler
    assert stdout.strip()[-2000:].count("ROLLBACK_CLASS") == 0  # verifies the setup
    runner = FakeRunner(
        [
            _ok(stdout='DATABASE_URL="postgresql://u:p@h:5432/db"\n'),
            _fail(1, stdout=stdout),
        ]
    )
    with pytest.raises(sg.SchemaGateError) as exc_info:
        sg.run_schema_gate(**_gate_kwargs(runner=runner, timeout=5))
    assert "(ROLLBACK_CLASS: C)" in str(exc_info.value)


def test_run_schema_gate_blocks_on_exit_1_discrepancy():
    runner = FakeRunner(
        [
            _ok(stdout='DATABASE_URL="postgresql://u:p@h:5432/db"\n'),
            _fail(1, stdout="ERROR: Schema discrepancies found\nROLLBACK_CLASS: C\n"),
        ]
    )
    with pytest.raises(
        sg.SchemaGateError, match="BLOCKED finance_report/app \\(exit 1\\)"
    ) as exc_info:
        sg.run_schema_gate(**_gate_kwargs(runner=runner, timeout=5))
    assert exc_info.value.verdict == sg.VERDICT_BLOCKED == "BLOCKED"


def test_run_schema_gate_blocks_on_exit_3_not_evaluated():
    """#718 review: NOT EVALUATED must block exactly like a real discrepancy."""
    runner = FakeRunner(
        [
            _ok(stdout='DATABASE_URL="postgresql://u:p@h:5432/db"\n'),
            _fail(3, stderr="NOT EVALUATED — deploy blocked: no database URL"),
        ]
    )
    with pytest.raises(
        sg.SchemaGateError, match="NOT EVALUATED finance_report/app \\(exit 3\\)"
    ) as exc_info:
        sg.run_schema_gate(**_gate_kwargs(runner=runner, timeout=5))
    assert exc_info.value.verdict == sg.VERDICT_NOT_EVALUATED == "NOT EVALUATED"


def test_run_schema_gate_blocks_on_an_unexpected_exit_code():
    """Not just 1/3 -- ANY non-zero (e.g. a docker-level failure) blocks too."""
    runner = FakeRunner(
        [
            _ok(stdout='DATABASE_URL="postgresql://u:p@h:5432/db"\n'),
            _fail(125, stderr="docker: Error response from daemon: no such image"),
        ]
    )
    with pytest.raises(sg.SchemaGateError, match="NO VERDICT .*exit 125") as exc_info:
        sg.run_schema_gate(**_gate_kwargs(runner=runner, timeout=5))
    assert exc_info.value.verdict == sg.VERDICT_NO_VERDICT


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


def test_sanitize_db_url_masks_credentials() -> None:
    raw = (
        "connection failed: postgresql://admin:super_secret_pwd@db.internal:5432/proddb"
    )
    sanitized = sg._sanitize_db_url(raw)
    assert "super_secret_pwd" not in sanitized
    assert "postgresql://admin:******@db.internal:5432/proddb" in sanitized


def test_run_schema_gate_sanitizes_db_credentials_in_error() -> None:
    runner = FakeRunner(
        [
            _ok(stdout='DATABASE_URL="postgresql://u:p@h:5432/db"\n'),
            _fail(
                1,
                stderr="fatal: connection to postgresql://user:sensitive_pw@db:5432/app failed",
            ),
        ]
    )
    with pytest.raises(sg.SchemaGateError) as exc_info:
        sg.run_schema_gate(**_gate_kwargs(runner=runner, timeout=5))
    err_msg = str(exc_info.value)
    assert "sensitive_pw" not in err_msg
    assert "postgresql://user:******@db:5432/app" in err_msg


def test_run_schema_gate_sanitizes_db_credentials_on_missing_rollback_class() -> None:
    """#569-R4: exit 0 without a ROLLBACK_CLASS line must also sanitize DB URLs in the
    captured output before raising SchemaGateError."""
    runner = FakeRunner(
        [
            _ok(stdout='DATABASE_URL="postgresql://u:p@h:5432/db"\n'),
            _ok(
                stdout="connected to postgresql://user:leak_secret_pw@db.internal:5432/app without class\n"
            ),
        ]
    )
    with pytest.raises(sg.SchemaGateError, match="no ROLLBACK_CLASS") as exc_info:
        sg.run_schema_gate(**_gate_kwargs(runner=runner, timeout=5))
    err_msg = str(exc_info.value)
    assert "leak_secret_pw" not in err_msg
    assert "postgresql://user:******@db.internal:5432/app" in err_msg


def test_run_schema_gate_timeout_blocks_without_echoing_the_command():
    """subprocess.TimeoutExpired prints its command; the gate message must not."""
    secret = "leak_on_timeout_pw"

    def runner(args, **kwargs):
        if "docker exec" in args[-1]:
            return _ok(stdout=f'DATABASE_URL="postgresql://u:{secret}@h/db"\n')
        raise subprocess.TimeoutExpired(cmd=[*args, secret], timeout=5)

    with pytest.raises(sg.SchemaGateError, match="TimeoutExpired") as exc_info:
        sg.run_schema_gate(**_gate_kwargs(runner=runner, timeout=5))
    assert secret not in str(exc_info.value)
    assert exc_info.value.verdict == sg.VERDICT_NO_VERDICT


def test_run_schema_gate_oserror_blocks_without_echoing_the_command():
    """An OSError (no ssh binary, a broken pipe) can carry the command; never echo it."""
    marker = "launcher-command-marker"

    def runner(args, **kwargs):
        if "docker exec" in args[-1]:
            return _ok(stdout='DATABASE_URL="postgresql://u:p@h/db"\n')
        raise OSError(f"cannot run {args[-1]} {marker}")

    with pytest.raises(
        sg.SchemaGateError, match="did not finish \\(OSError\\)"
    ) as exc_info:
        sg.run_schema_gate(**_gate_kwargs(runner=runner, timeout=5))
    message = str(exc_info.value)
    assert marker not in message
    assert "docker run" not in message
    assert exc_info.value.verdict == sg.VERDICT_NO_VERDICT


# --- exit codes: one mapping, equal to the SDK ---------------------------------------


@pytest.mark.parametrize(
    ("returncode", "verdict"),
    [
        (1, "BLOCKED"),
        (3, "NOT EVALUATED"),
        (2, "NO VERDICT"),
        (125, "NO VERDICT"),
        (255, "NO VERDICT"),
    ],
)
def test_verdict_for_exit_maps_each_code(returncode, verdict):
    assert sg.verdict_for_exit(returncode) == verdict


def test_exit_codes_equal_the_sdk_schema_check_codes():
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "infra2_sdk.storage",
            "check-schema",
            "--metadata",
            "src.database:Base.metadata",
            "--db-url",
            "",
            "--label",
            "test",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert (sg.EXIT_OK, sg.EXIT_BLOCKED, sg.EXIT_NOT_EVALUATED) == (0, 1, 3)
    assert proc.returncode == sg.EXIT_NOT_EVALUATED


# --- _LAUNCHER: runs in the target image ------------------------------------------------


def _run_launcher(cwd: Path, argv: list[str], stdin: str, pythonpath: Path | None):
    env = {"PATH": "/usr/bin:/bin"}
    if pythonpath is not None:
        env["PYTHONPATH"] = str(pythonpath)
    return subprocess.run(
        [sys.executable, "-c", sg._LAUNCHER, *argv],
        input=stdin,
        text=True,
        capture_output=True,
        cwd=cwd,
        env=env,
        timeout=60,
    )


def _stub_sdk(
    root: Path,
    storage_source: str | None = None,
) -> Path:
    sdk_dir = root / "stub" / "infra2_sdk"
    sdk_dir.mkdir(parents=True)
    (sdk_dir / "__init__.py").write_text("")
    if storage_source is not None:
        (sdk_dir / "storage.py").write_text(storage_source)
    return root / "stub"


def test_launcher_maps_an_image_without_schema_check_to_not_evaluated(tmp_path):
    """An image without infra2_sdk.storage exits EXIT_NOT_EVALUATED."""
    stub = _stub_sdk(tmp_path, storage_source=None)
    result = _run_launcher(tmp_path, ["--metadata", "m:x"], "postgresql://h/db\n", stub)
    assert result.returncode == sg.EXIT_NOT_EVALUATED == 3
    assert "NOT EVALUATED" in result.stderr
    assert "infra2_sdk.storage" in result.stderr
    assert result.stdout == ""


def test_launcher_maps_an_image_without_infra2_sdk_to_not_evaluated(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    code = (
        "import sys, importlib.abc\n"
        "class Block(importlib.abc.MetaPathFinder):\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name.split('.')[0] == 'infra2_sdk':\n"
        "            raise ModuleNotFoundError(name)\n"
        "sys.meta_path.insert(0, Block())\n"
    )
    (empty / "sitecustomize.py").write_text(code)
    result = _run_launcher(
        tmp_path, ["--metadata", "m:x"], "postgresql://h/db\n", empty
    )
    assert result.returncode == sg.EXIT_NOT_EVALUATED
    assert "NOT EVALUATED" in result.stderr


def test_launcher_passes_the_spec_options_and_stdin_url_as_db_url(tmp_path):
    (tmp_path / "x_mod.py").write_text("VALUE = 1\n")
    stub = _stub_sdk(
        tmp_path,
        "import json\ndef check_schema_cli(argv):\n    print(json.dumps({'argv': argv}))\n    return 7\n",
    )
    spec_argv = [
        "--metadata",
        "m:x",
        "--import",
        "x_mod",
        "--label",
        "finance_report/app",
    ]
    url = "postgresql+asyncpg://u:p@h:5432/db"
    result = _run_launcher(tmp_path, spec_argv, f"{url}\n", stub)
    # The launcher returns the SDK exit code unchanged.
    assert result.returncode == 7, result.stderr
    payload = json.loads(result.stdout)
    assert payload["argv"] == [
        "check-schema",
        "--metadata",
        "m:x",
        "--label",
        "finance_report/app",
        "--db-url",
        url,
        "--json",
    ]


@pytest.mark.parametrize(
    "source",
    [
        "raise AttributeError('broken module attribute')\n",
        "def check_schema_cli(argv:\n",  # SyntaxError at import
    ],
    ids=["attribute-error", "syntax-error"],
)
def test_launcher_maps_a_broken_sdk_import_to_not_evaluated(tmp_path, source):
    stub = _stub_sdk(tmp_path, source)
    result = _run_launcher(tmp_path, ["--metadata", "m:x"], "postgresql://h/db\n", stub)
    assert result.returncode == sg.EXIT_NOT_EVALUATED, result.stderr
    assert "NOT EVALUATED" in result.stderr
    assert "does not import" in result.stderr


def test_launcher_maps_an_exception_in_main_to_not_evaluated(tmp_path):
    stub = _stub_sdk(
        tmp_path,
        "def check_schema_cli(argv):\n    raise RuntimeError('crash inside main')\n",
    )
    result = _run_launcher(tmp_path, ["--metadata", "m:x"], "postgresql://h/db\n", stub)
    assert result.returncode == sg.EXIT_NOT_EVALUATED, result.stderr
    assert "storage.check_schema_cli raised RuntimeError" in result.stderr


@pytest.mark.parametrize("code", [0, 1, 3])
def test_launcher_keeps_the_return_code_of_main(tmp_path, code):
    """A real verdict of check_schema_cli, including BLOCKED (1), is never rewritten."""
    stub = _stub_sdk(tmp_path, f"def check_schema_cli(argv):\n    return {code}\n")
    result = _run_launcher(tmp_path, ["--metadata", "m:x"], "postgresql://h/db\n", stub)
    assert result.returncode == code, result.stderr
    assert "NOT EVALUATED" not in result.stderr


def test_launcher_prefers_storage_check_schema_on_v410_sdk(tmp_path):
    """_LAUNCHER imports --import modules first and invokes check_schema_cli without --import flags."""
    (tmp_path / "x_mod.py").write_text("import sys\nsys._order = ['x']\n")
    (tmp_path / "y_mod.py").write_text("import sys\nsys._order.append('y')\n")
    stub = _stub_sdk(
        tmp_path,
        storage_source=(
            "import json, sys\n"
            "def check_schema_cli(argv):\n"
            "    print(json.dumps({'argv': argv, 'order': sys._order}))\n"
            "    return 0\n"
        ),
    )
    spec_argv = [
        "--metadata",
        "src.database:Base.metadata",
        "--import",
        "x_mod",
        "--import",
        "y_mod",
        "--label",
        "finance_report/app",
    ]
    url = "postgresql+asyncpg://u:p@h:5432/db"
    result = _run_launcher(tmp_path, spec_argv, f"{url}\n", stub)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["order"] == ["x", "y"]
    assert payload["argv"] == [
        "check-schema",
        "--metadata",
        "src.database:Base.metadata",
        "--label",
        "finance_report/app",
        "--db-url",
        url,
        "--json",
    ]


def test_launcher_falls_back_to_storage_runpy_and_runtime_schema_check(tmp_path):
    """Older images (4.10..4.14.1 storage package CLI and 4.9 runtime.schema_check) still evaluate."""
    pkg = tmp_path / "runpy_stub" / "infra2_sdk" / "storage"
    pkg.mkdir(parents=True)
    (tmp_path / "runpy_stub" / "infra2_sdk" / "__init__.py").write_text("")
    (pkg / "__init__.py").write_text("")
    (pkg / "__main__.py").write_text(
        "import json, sys\nprint(json.dumps({'rollback_class': 'A', 'migrations_pending': False, 'label': 'finance_report/app', 'argv': sys.argv[1:]}))\n"
    )
    res_pkg = _run_launcher(
        tmp_path,
        ["--metadata", "m:x", "--label", "finance_report/app"],
        "postgresql://h/db\n",
        tmp_path / "runpy_stub",
    )
    assert res_pkg.returncode == 0, res_pkg.stderr
    assert "[finance_report/app] ROLLBACK_CLASS: A pending=False" in res_pkg.stdout

    rt = tmp_path / "rt_stub" / "infra2_sdk" / "runtime"
    rt.mkdir(parents=True)
    (tmp_path / "rt_stub" / "infra2_sdk" / "__init__.py").write_text("")
    (rt / "__init__.py").write_text("")
    (rt / "schema_check.py").write_text(
        "def main(argv):\n    print('ROLLBACK_CLASS: A')\n    return 0\n"
    )
    res_rt = _run_launcher(
        tmp_path,
        ["--metadata", "m:x"],
        "postgresql://h/db\n",
        tmp_path / "rt_stub",
    )
    assert res_rt.returncode == 0, res_rt.stderr
    assert "ROLLBACK_CLASS: A" in res_rt.stdout


def test_launcher_storage_emits_rollback_class_and_diff_details_on_blocked(tmp_path):
    """On blocked exit, _LAUNCHER prints ROLLBACK_CLASS to stdout and SCHEMA_DIFF JSON to stderr."""
    payload = {
        "label": "finance_report/app",
        "rollback_class": "C",
        "migrations_pending": False,
        "dropped_tables": [["public", "legacy_tbl"]],
        "dropped_columns": [],
        "enum_discrepancies": [],
        "precondition_failures": [],
    }
    stub = _stub_sdk(
        tmp_path,
        storage_source=(
            f"import json\ndef main(argv):\n    print(json.dumps({payload!r}))\n    return 1\n"
        ),
    )
    result = _run_launcher(
        tmp_path,
        ["--metadata", "m:x", "--label", "finance_report/app"],
        "postgresql://h/db\n",
        stub,
    )
    assert result.returncode == sg.EXIT_BLOCKED == 1, result.stderr
    assert "[finance_report/app] ROLLBACK_CLASS: C pending=False" in result.stdout
    assert "SCHEMA_DIFF:" in result.stderr
    assert "legacy_tbl" in result.stderr


def test_launcher_storage_omits_diff_details_on_ok(tmp_path):
    """On exit 0 pass, _LAUNCHER prints ROLLBACK_CLASS and emits no SCHEMA_DIFF on stderr."""
    payload = {
        "label": "finance_report/app",
        "rollback_class": "A",
        "migrations_pending": False,
        "dropped_tables": [],
        "dropped_columns": [],
        "enum_discrepancies": [],
        "precondition_failures": [],
    }
    stub = _stub_sdk(
        tmp_path,
        storage_source=(
            f"import json\ndef main(argv):\n    print(json.dumps({payload!r}))\n    return 0\n"
        ),
    )
    result = _run_launcher(
        tmp_path,
        ["--metadata", "m:x", "--label", "finance_report/app"],
        "postgresql://h/db\n",
        stub,
    )
    assert result.returncode == sg.EXIT_OK == 0, result.stderr
    assert "[finance_report/app] ROLLBACK_CLASS: A pending=False" in result.stdout
    assert "SCHEMA_DIFF:" not in result.stderr


def test_launcher_runpy_alter_sys_resolves_pydantic_forward_refs_in_storage_module(
    tmp_path,
):
    """Running storage via runpy with alter_sys=True binds sys.modules['__main__'] so postponed annotations resolve."""
    storage_source = """\
from __future__ import annotations

import json
import sys
from pydantic import BaseModel


class _Child(BaseModel):
    name: str


class _Parent(BaseModel):
    children: tuple[_Child, ...] = ()


def check_schema_cli(_argv=None) -> int:
    _Parent(children=(_Child(name="ok"),))
    print(json.dumps({"label": "finance_report/app", "rollback_class": "A", "migrations_pending": False}))
    return 0
"""
    stub = _stub_sdk(tmp_path, storage_source=storage_source)
    result = _run_launcher(
        tmp_path,
        ["--metadata", "m:x", "--label", "finance_report/app"],
        "postgresql://h/db\n",
        stub,
    )
    assert result.returncode == sg.EXIT_OK == 0, result.stderr
    assert "[finance_report/app] ROLLBACK_CLASS: A pending=False" in result.stdout


def test_launcher_imports_modules_against_real_storage_cli(tmp_path):
    """Real installed infra2_sdk.storage CLI accepts remaining args after _LAUNCHER imports --import modules."""
    (tmp_path / "mod_a.py").write_text("VALUE = 1\n")
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            sg._LAUNCHER,
            "--metadata",
            "m:x",
            "--import",
            "mod_a",
            "--label",
            "finance_report/app",
        ],
        input="\n",
        text=True,
        capture_output=True,
        cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(tmp_path)},
        timeout=60,
    )
    assert result.returncode == sg.EXIT_NOT_EVALUATED, result.stderr
    assert "DATABASE_URL is required" in result.stderr


def test_launcher_fails_closed_when_storage_import_module_fails(tmp_path):
    """Failing to import an --import module exits EXIT_NOT_EVALUATED with a clear message."""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            sg._LAUNCHER,
            "--metadata",
            "m:x",
            "--import",
            "nonexistent_schema_mod",
            "--label",
            "finance_report/app",
        ],
        input="postgresql://h/db\n",
        text=True,
        capture_output=True,
        cwd=tmp_path,
        timeout=60,
    )
    assert result.returncode == sg.EXIT_NOT_EVALUATED, result.stderr
    assert "failed to import nonexistent_schema_mod" in result.stderr


def test_run_schema_gate_parses_labeled_storage_cli_rollback_class():
    """Prefixed `[label] ROLLBACK_CLASS: <cls> pending=...` output parses cleanly on pass and block."""
    pass_runner = FakeRunner(
        [
            _ok(stdout='DATABASE_URL="postgresql://u:p@h:5432/db"\n'),
            _ok(stdout="[finance_report/app] ROLLBACK_CLASS: A pending=0\n"),
        ]
    )
    assert sg.run_schema_gate(**_gate_kwargs(runner=pass_runner, timeout=5)) == "A"

    block_runner = FakeRunner(
        [
            _ok(stdout='DATABASE_URL="postgresql://u:p@h:5432/db"\n'),
            _fail(1, stdout="[finance_report/app] ROLLBACK_CLASS: C pending=1\n"),
        ]
    )
    with pytest.raises(sg.SchemaGateError) as exc_info:
        sg.run_schema_gate(**_gate_kwargs(runner=block_runner, timeout=5))
    assert "(ROLLBACK_CLASS: C)" in str(exc_info.value)


def test_launcher_maps_storage_main_exception_to_not_evaluated(tmp_path):
    """An uncaught exception in storage.check_schema_cli maps to EXIT_NOT_EVALUATED."""
    stub = _stub_sdk(
        tmp_path,
        storage_source="def check_schema_cli(argv):\n    raise RuntimeError('storage boom')\n",
    )
    result = _run_launcher(tmp_path, ["--metadata", "m:x"], "postgresql://h/db\n", stub)
    assert result.returncode == sg.EXIT_NOT_EVALUATED, result.stderr
    assert "storage.check_schema_cli raised RuntimeError" in result.stderr


def test_launcher_with_the_installed_sdk_and_no_url_is_not_evaluated(tmp_path):
    """End to end with the real SDK CLI: an empty --db-url compares nothing (exit 3)."""
    result = subprocess.run(
        [sys.executable, "-c", sg._LAUNCHER, "--metadata", "absent_module:metadata"],
        input="\n",
        text=True,
        capture_output=True,
        cwd=tmp_path,
        timeout=60,
    )
    assert result.returncode == sg.EXIT_NOT_EVALUATED, result.stderr
    assert "DATABASE_URL is required" in result.stderr


# --- the service declaration ---------------------------------------------------------


def test_schema_check_specs_name_a_registered_backend_image():
    """Each gated service is a deploy_v2 service, and its image is one it publishes."""
    from common.deploy.contract import SERVICES

    assert set(sg._SCHEMA_CHECK_SPECS) == {"finance_report/app"}
    for service, spec in sg._SCHEMA_CHECK_SPECS.items():
        assert spec.image_repository in SERVICES[service].image_repositories
    spec = sg._SCHEMA_CHECK_SPECS["finance_report/app"]
    assert spec.image_repository == "ghcr.io/wangzitian0/finance_report-backend"
    assert spec.metadata == "src.database:Base.metadata"
    assert spec.imports == (
        "src.counter.extension.sql",
        "src.identity.extension.sql",
        "src.orm_registry",
        "src.platform.extension.sql",
    )


def test_schema_check_argv_lists_each_import_in_order():
    spec = sg.SchemaCheckSpec(image_repository="r", metadata="a:b", imports=("x", "y"))
    assert sg.schema_check_argv("s/app", spec) == [
        "--metadata",
        "a:b",
        "--import",
        "x",
        "--import",
        "y",
        "--label",
        "s/app",
    ]


def test_infra2_keeps_no_copy_of_the_schema_check():
    """The SDK owns the check; infra2 holds only the gate wiring (#1824)."""
    root = Path(__file__).resolve().parents[2]
    assert not (root / "tools" / "pre_deploy_schema_check.py").exists()
    gate_source = (root / "common" / "deploy" / "schema_gate.py").read_text()
    assert "def check_enums" not in gate_source
    assert "pre_deploy_schema_check" not in gate_source
