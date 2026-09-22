"""The probe must never leak a credential and must refuse rather than guess."""

from __future__ import annotations

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
