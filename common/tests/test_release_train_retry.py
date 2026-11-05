"""#1439: the release train retries a failed platform unit on a transient fault, only then.

Owner decision (2026-11-05): the approval of a production tag covers at most 2 automatic
retries of a failed unit, in the same run and on the same tag. A deterministic failure and
an app unit are never retried.
"""

from __future__ import annotations

import subprocess

import pytest

from tools.reconcile_iac_inputs import (
    DeployCommand,
    build_deploy_commands,
    build_plan,
    run_deploy_commands,
    write_summary,
)

# What a failed Dokploy pull left on 2026-11-05 (run 41978749921, truealpha/data_engine).
DNS = (
    "deploy_v2 failed: platform deploy of truealpha/data_engine to production ended "
    "'failed': {'summary': 'Dokploy deployment record entered error (log_tail=Get "
    '"https://ghcr.io/v2/": dial tcp: lookup ghcr.io on 127.0.0.53:53: server misbehaving)\'}\n'
)
APP = DeployCommand("finance_report/app", "prod", ["deploy", "app"])
PLAN = build_plan(["platform/12.alerting/compose.yaml"])


def _platform(deploy_type="prod") -> DeployCommand:
    commands = build_deploy_commands(
        PLAN, iac_ref="v3.1.23", domain="z.party", timeout=600
    )
    return next(c for c in commands if c.deploy_type == deploy_type)


class _Script:
    """A fake `subprocess.run` that plays (returncode, stderr) outcomes in order."""

    def __init__(self, *outcomes):
        self.outcomes, self.argvs = list(outcomes), []

    def __call__(self, argv, **_kwargs):
        self.argvs.append(list(argv))
        assert len(self.argvs) <= len(self.outcomes), "runaway retry loop"
        code, stderr = self.outcomes[len(self.argvs) - 1]
        return subprocess.CompletedProcess(
            argv, code, '{"ok": 1}' if code == 0 else "", stderr
        )


def _run(commands, script, sleeps=None):
    ticks = iter(f"2026-11-05T08:{n:02d}:00Z" for n in range(60))
    sleep = (sleeps if sleeps is not None else []).append
    return run_deploy_commands(
        commands, runner=script, sleep=sleep, clock=lambda: next(ticks)
    )


def test_a_dns_failure_on_the_first_pull_then_success_is_green_with_two_attempts(
    tmp_path,
):
    command, script, sleeps = _platform(), _Script((1, DNS), (0, "")), []

    results = _run([command], script, sleeps)

    attempts = results[0]["attempts"]
    assert results[0]["returncode"] == 0 and [a["n"] for a in attempts] == [1, 2]
    assert (
        attempts[0]["cause"].startswith("dns:")
        and "server misbehaving" in attempts[0]["cause"]
    )
    assert attempts[1]["cause"] is None
    assert script.argvs[0] == script.argvs[1] == command.argv  # same run, same tag
    assert sleeps == [30.0]
    summary = tmp_path / "summary.md"
    write_summary(
        plan=PLAN, commands=[command], results=results, dry_run=False, path=str(summary)
    )
    assert (
        "Retried" in summary.read_text() and "server misbehaving" in summary.read_text()
    )


def test_the_third_transient_failure_fails_the_unit():
    script, sleeps = _Script((1, DNS), (1, DNS), (1, DNS), (0, "")), []

    results = _run([_platform("staging"), _platform("staging")], script, sleeps)

    assert len(script.argvs) == 3, (
        "two retries and no third; the next unit does not start"
    )
    assert len(results) == 1 and results[0]["returncode"] == 1
    assert [a["n"] for a in results[0]["attempts"]] == [1, 2, 3] and sleeps == [
        30.0,
        90.0,
    ]


@pytest.mark.parametrize(
    "stderr",
    [
        "deploy_v2 failed: pull access denied for ghcr.io/x/y, requested access is denied\n",
        "deploy_v2 failed: not in service after deploy: backend is unhealthy; i/o timeout\n",
        "deploy_v2 failed: Effective remote config is stale after deploy (expected a, got b)\n",
        "deploy_v2 failed: KeyError: 'compose_id'\n",
        "deploy_v2 failed: version probe https://x.party/version returned 502 Bad Gateway\n",
        "deploy_v2 failed: i/o timeout reaching ghcr.io\ndeploy_v2 failed: KeyError: 'compose_id'\n",
    ],
    ids=[
        "auth",
        "health-check-with-a-transient-phrase",
        "changed-hash",
        "unrecognised",
        "5xx-without-registry",
        "last-verdict-wins",
    ],
)
def test_a_deterministic_failure_is_not_retried(stderr):
    script = _Script((1, stderr), (0, ""))

    results = _run([_platform()], script)

    assert len(script.argvs) == 1 and results[0]["returncode"] == 1
    assert len(results[0]["attempts"]) == 1 and results[0]["attempts"][0]["cause"]


def test_a_unit_that_mixes_a_platform_and_an_app_service_is_never_retried():
    mixed = DeployCommand("platform/alerting,finance_report/app", "prod", ["deploy"])
    script = _Script((1, DNS), (0, ""))

    _run([mixed], script)

    assert len(script.argvs) == 1


def test_an_app_unit_is_never_retried():
    """An app unit goes through promote: its failure rolls production back, and a re-run
    repeats the rollback until the circuit breaker trips."""
    script = _Script((1, DNS), (0, ""))

    results = _run([APP], script)

    assert len(script.argvs) == 1 and results[0]["returncode"] == 1


@pytest.mark.parametrize(
    "stderr",
    [
        "deploy_v2 failed: iac_runner answered not_found for 100s while polling deploy x\n",
        "deploy_v2 failed: iac_runner reported interrupted for deploy x to production\n",
        "deploy_v2 failed: dial tcp 140.82.0.1:443: i/o timeout\n",
        'deploy_v2 failed: Head "https://ghcr.io/v2/x/manifests/v1": 502 Bad Gateway\n',
    ],
    ids=["not-found", "interrupted", "i/o-timeout", "registry-502"],
)
def test_every_transient_class_is_retried_for_a_platform_unit_in_production(stderr):
    script = _Script((1, stderr), (0, ""))

    results = _run([_platform("prod")], script)

    assert len(script.argvs) == 2 and results[0]["returncode"] == 0
