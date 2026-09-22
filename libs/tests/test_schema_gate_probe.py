"""The probe must never leak a credential and must refuse rather than guess."""

from __future__ import annotations

import json
import sys

import pytest

from tools import schema_gate_probe as probe


@pytest.mark.parametrize(
    ("env", "expected"),
    (
        ({}, None),
        ({"INFRA2_WATCHDOG_SSH_HOST": "h"}, None),
        ({"INFRA2_WATCHDOG_SSH_HOST": "h", "INFRA2_WATCHDOG_SSH_USER": "u"}, None),
        # Present but blank is absent -- a secret GitHub does not define arrives
        # as the empty string, and building an `ssh @:22` out of it would fail
        # in a way that reads like the host being down.
        (
            {
                "INFRA2_WATCHDOG_SSH_HOST": "h",
                "INFRA2_WATCHDOG_SSH_USER": "  ",
                "INFRA2_WATCHDOG_SSH_KEY_PATH": "k",
            },
            None,
        ),
    ),
)
def test_incomplete_credentials_yield_no_ssh_command(env: dict, expected) -> None:
    assert probe.ssh_argv(env) is expected


def test_complete_credentials_build_a_batch_mode_command() -> None:
    argv = probe.ssh_argv(
        {
            "INFRA2_WATCHDOG_SSH_HOST": "vps",
            "INFRA2_WATCHDOG_SSH_USER": "ops",
            "INFRA2_WATCHDOG_SSH_KEY_PATH": "/k",
            "INFRA2_WATCHDOG_SSH_PORT": "2222",
        }
    )
    assert argv is not None
    # BatchMode so a missing key fails instead of waiting on a passphrase
    # prompt that no runner will ever answer.
    assert "BatchMode=yes" in argv
    assert argv[-1] == "ops@vps" and "2222" in argv


@pytest.mark.parametrize(
    ("url", "expected"),
    (
        ("", ""),
        ("postgresql+asyncpg://user:hunter2@db:5432/finance", "postgresql+asyncpg"),
        ("not-a-url", "malformed"),
    ),
)
def test_only_the_scheme_survives(url: str, expected: str) -> None:
    """The probe reports that a URL exists and what kind it is, never its body.

    It runs in a workflow whose logs are readable by anyone who can read the
    repository, and DATABASE_URL carries the database password.
    """
    got = probe.scheme_of(url)
    assert got == expected
    assert "hunter2" not in got


def test_the_password_cannot_reach_the_report() -> None:
    """The guard that matters, stated as the property rather than the parse."""
    secret = "hunter2"
    url = f"postgresql://user:{secret}@db:5432/finance"
    assert secret not in probe.scheme_of(url)


def test_a_missing_binary_is_an_answer_not_a_traceback() -> None:
    """A probe that dies with a stack trace has reported nothing.

    Python exits 1 on an uncaught exception, and 1 is not in this tool's
    vocabulary — a caller reading the exit code would see a verdict the probe
    never gave.
    """
    code, _, err = probe.run(["definitely-not-a-binary-xyz"])
    assert code == probe.PROBE_INFRA
    assert "could not execute" in err


def test_a_hanging_remote_is_an_answer_too() -> None:
    code, _, err = probe.run(["sleep", "5"], timeout=1)
    assert code == probe.PROBE_INFRA
    assert "timed out" in err


def _fake_remote(listing: str):
    """Stand in for ssh, answering only the container-discovery call."""

    def remote(ssh, command, *, stdin="", timeout=120):
        if command.startswith("docker --version"):
            return 0, "Docker version 27.0.0", ""
        if command.startswith("docker ps"):
            return 0, listing, ""
        return 0, "", ""

    return remote


@pytest.mark.parametrize(
    ("listing", "why"),
    (
        ("", "nothing to read DATABASE_URL from"),
        (
            "finance_report-backend\timg\nfinance_report-backend-staging\timg",
            "two containers share the prefix",
        ),
    ),
)
def test_ambiguous_or_absent_containers_refuse(
    listing: str, why: str, monkeypatch, capsys
) -> None:
    """Picking one of several would read the wrong database and say nothing.

    `--container` is a prefix because the environment suffix is not known
    here, so prod, staging and every live preview can match it. Reporting
    whichever docker listed first as "the" answer is the failure this refuses.
    """
    monkeypatch.setenv("INFRA2_WATCHDOG_SSH_HOST", "vps")
    monkeypatch.setenv("INFRA2_WATCHDOG_SSH_USER", "ops")
    monkeypatch.setenv("INFRA2_WATCHDOG_SSH_KEY_PATH", "/k")
    monkeypatch.setattr(probe, "remote", _fake_remote(listing))
    monkeypatch.setattr(sys, "argv", ["probe"])

    assert probe.main() == probe.PROBE_INFRA
    report = json.loads(capsys.readouterr().out)
    assert report["verdict"] == "INFRA"
    assert (
        "containers match" in report["reason"] or "nothing to read" in report["reason"]
    )


_URL = "postgresql+asyncpg://app:hunter2@finance_report-postgres:5432/finance"


def test_redaction_removes_the_whole_url_and_the_password_alone() -> None:
    """A driver may echo the connection string, or quote only the credential.

    This is not pattern matching: the probe holds the exact value it read, so
    the set of strings to remove is closed and removing them is exact.
    """
    secrets = probe.secrets_in(_URL)
    assert _URL in secrets and "hunter2" in secrets

    whole = probe.redact(f"could not connect to {_URL}", *secrets)
    assert "hunter2" not in whole and "***" in whole

    just_the_password = probe.redact(
        'FATAL: password authentication failed for "hunter2"', *secrets
    )
    assert "hunter2" not in just_the_password


def test_redaction_does_not_eat_the_diagnosis() -> None:
    """A secret short enough to appear everywhere would redact the message.

    Without the length floor, a two-character password turns every stderr into
    asterisks and the probe stops being able to say what went wrong.
    """
    assert probe.redact("connection refused", "ab") == "connection refused"
    assert probe.secrets_in("") == ()
    assert probe.secrets_in("postgresql://nouserinfo/db") == (
        "postgresql://nouserinfo/db",
    )


def test_docker_ps_failing_is_not_reported_as_no_containers(
    monkeypatch, capsys
) -> None:
    """Two different facts, and conflating them hides the one that matters.

    A dead daemon or a permission error used to arrive here as an empty
    listing — because the command piped into grep and swallowed the code with
    `|| true` — and got reported as "nothing is running".
    """
    monkeypatch.setenv("INFRA2_WATCHDOG_SSH_HOST", "vps")
    monkeypatch.setenv("INFRA2_WATCHDOG_SSH_USER", "ops")
    monkeypatch.setenv("INFRA2_WATCHDOG_SSH_KEY_PATH", "/k")

    def remote(ssh, command, *, stdin="", timeout=120):
        if command.startswith("docker --version"):
            return 0, "Docker version 27.0.0", ""
        if command.startswith("docker ps"):
            return 1, "", "permission denied while trying to connect to the daemon"
        return 0, "", ""

    monkeypatch.setattr(probe, "remote", remote)
    monkeypatch.setattr(sys, "argv", ["probe"])

    assert probe.main() == probe.PROBE_INFRA
    report = json.loads(capsys.readouterr().out)
    assert "docker ps failed" in report["reason"]
    assert "permission denied" in report["reason"]


def test_a_percent_encoded_password_is_redacted_in_both_forms() -> None:
    """The URL carries it encoded; the driver reports it decoded.

    `p@ss word` appears as `p%40ss%20word` in the URL, so redacting only what
    the URL contained would leave the decoded form intact in exactly the
    message most likely to quote it — an authentication failure.
    """
    url = "postgresql://app:p%40ss%20word@db:5432/finance"
    secrets = probe.secrets_in(url)
    assert "p%40ss%20word" in secrets and "p@ss word" in secrets

    for message in (
        f"could not connect to {url}",
        'FATAL: password authentication failed for "p@ss word"',
        "driver echoed p%40ss%20word",
    ):
        cleaned = probe.redact(message, *secrets)
        assert "p@ss word" not in cleaned and "p%40ss%20word" not in cleaned


def test_the_gate_script_is_found_from_any_working_directory(
    tmp_path, monkeypatch
) -> None:
    """A relative path would make the probe refuse from anywhere but the root.

    The script sits next to this tool, not next to whoever invoked it.
    """
    monkeypatch.chdir(tmp_path)
    assert probe.GATE_SCRIPT.is_file()


def test_container_matching_is_a_prefix_not_a_substring(monkeypatch, capsys) -> None:
    """`--container` is documented as a prefix; a substring match would create
    the ambiguity this refusal exists to prevent."""
    monkeypatch.setenv("INFRA2_WATCHDOG_SSH_HOST", "vps")
    monkeypatch.setenv("INFRA2_WATCHDOG_SSH_USER", "ops")
    monkeypatch.setenv("INFRA2_WATCHDOG_SSH_KEY_PATH", "/k")

    def remote(ssh, command, *, stdin="", timeout=120):
        if command.startswith("docker --version"):
            return 0, "Docker version 27.0.0", ""
        if command.startswith("docker ps"):
            # Only the first starts with the prefix; the second merely
            # contains it and must not be treated as a candidate.
            return 0, "finance_report-backend\timg\nold-finance_report-backend\timg", ""
        return 0, "", ""

    monkeypatch.setattr(probe, "remote", remote)
    monkeypatch.setattr(sys, "argv", ["probe"])
    probe.main()
    report = json.loads(capsys.readouterr().out)
    names = [row[0] for row in report["probes"]["running_containers"]]
    assert names == ["finance_report-backend"]


def test_the_ssh_options_match_the_rest_of_the_repository() -> None:
    """Three other tools reach this same host; diverging here is a surprise.

    `accept-new` writes known_hosts, so it needs a usable ~/.ssh and fails
    where those three succeed.
    """
    argv = probe.ssh_argv(
        {
            "INFRA2_WATCHDOG_SSH_HOST": "vps",
            "INFRA2_WATCHDOG_SSH_USER": "ops",
            "INFRA2_WATCHDOG_SSH_KEY_PATH": "/k",
        }
    )
    assert argv is not None
    assert "StrictHostKeyChecking=no" in argv
    assert "UserKnownHostsFile=/dev/null" in argv
    assert "accept-new" not in " ".join(argv)
