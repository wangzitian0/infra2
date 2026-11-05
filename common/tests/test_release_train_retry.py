"""#1439: the release train retries a failed unit on a transient fault, and only then.

Owner decision (2026-11-05): the approval of a production tag covers at most 2 automatic
retries of a failed unit, in the same run and on the same tag. Each retry is logged with
its cause. A retry never starts a new run and never changes the tag. A deterministic
failure is never retried.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import tools.reconcile_iac_inputs as reconcile
from tools.reconcile_iac_inputs import (
    DeployCommand,
    build_deploy_commands,
    build_plan,
    main,
    write_summary,
)

SHA = "a" * 40
ROOT = Path(__file__).resolve().parents[2]
REAL_RUN_DEPLOY_COMMANDS = reconcile.run_deploy_commands

# The text a failed Dokploy pull left on 2026-11-05 (run 41978749921, truealpha/data_engine).
DNS_FAILURE = (
    "deploy_v2 failed: platform deploy of truealpha/data_engine to production ended "
    "'failed': {'deployment_id': 'cfb2143480bf2c8d', 'status': 'failed', 'details': "
    "{'failure_summary': [{'error_kind': 'dokploy_deployment_error', 'service': "
    "'truealpha/data_engine', 'summary': 'Dokploy deployment record entered error "
    '(deploymentId=d1, log_tail=Error response from daemon: Get "https://ghcr.io/v2/": '
    "dial tcp: lookup ghcr.io on 127.0.0.53:53: server misbehaving)'}]}}\n"
)
OK_STDOUT = json.dumps({"service": "x", "backend": "iac-runner"})


def _command(deploy_type: str = "prod") -> DeployCommand:
    plan = build_plan(["platform/12.alerting/compose.yaml"])
    commands = build_deploy_commands(
        plan, iac_ref="v3.1.23", domain="zitian.party", timeout=600
    )
    return next(c for c in commands if c.deploy_type == deploy_type)


class _Script:
    """A fake `subprocess.run`: plays (returncode, stderr) outcomes in order."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.argvs: list[list[str]] = []

    def __call__(self, argv, **_kwargs):
        self.argvs.append(list(argv))
        assert len(self.argvs) <= len(self.outcomes), "runaway retry loop"
        returncode, stderr = self.outcomes[len(self.argvs) - 1]
        stdout = OK_STDOUT if returncode == 0 else ""
        return subprocess.CompletedProcess(argv, returncode, stdout, stderr)


def _run(commands, script, *, sleeps=None, clock=None):
    sleeps = sleeps if sleeps is not None else []
    ticks = iter(f"2026-11-05T08:{n:02d}:00Z" for n in range(60))
    return REAL_RUN_DEPLOY_COMMANDS(
        commands,
        runner=script,
        sleep=sleeps.append,
        clock=clock or (lambda: next(ticks)),
    )


def test_a_dns_failure_on_the_first_pull_then_success_ends_green_with_two_attempts():
    """The data_engine failure of run 41978749921: one retry, same argv, green result."""
    command = _command("prod")
    script = _Script((1, DNS_FAILURE), (0, ""))
    sleeps: list[float] = []

    results = _run([command], script, sleeps=sleeps)

    assert len(results) == 1 and results[0]["returncode"] == 0
    attempts = results[0]["attempts"]
    assert [a["n"] for a in attempts] == [1, 2]
    assert attempts[0]["cause"].startswith("dns:") and (
        "server misbehaving" in attempts[0]["cause"]
    )
    assert attempts[1]["cause"] is None  # the second attempt succeeded
    assert [a["at"] for a in attempts] == [
        "2026-11-05T08:00:00Z",
        "2026-11-05T08:01:00Z",
    ]
    # Same run, same tag: both attempts ran the identical command line.
    assert script.argvs[0] == script.argvs[1] == command.argv
    assert "v3.1.23" in script.argvs[1]
    assert results[0]["deploy_v2"] == json.loads(OK_STDOUT)
    assert sleeps == [30.0]  # a pause before the retry, so the resolver can recover


@pytest.mark.parametrize(
    ("label", "stderr"),
    [
        (
            "auth",
            "deploy_v2 failed: Dokploy deployment record entered error (log_tail=Error "
            "response from daemon: pull access denied for ghcr.io/x/y, repository does "
            "not exist or may require 'docker login': denied: requested access to the "
            "resource is denied)\n",
        ),
        (
            "health-check-after-pull",
            "deploy_v2 failed: platform deploy of platform/postgres to production ended "
            "'failed': {'error': 'postgres: not in service after deploy: backend is "
            "unhealthy (Up 3 minutes (unhealthy)); i/o timeout talking to vault'}\n",
        ),
        (
            "config-error",
            "deploy_v2 failed: missing release_tag in harness/repos.yaml for "
            "'truealpha/app'\n",
        ),
        (
            "changed-hash",
            "deploy_v2 failed: platform deploy of platform/redis to production ended "
            "'failed': {'error': 'Effective remote config is stale after deploy "
            "(expected h1, got h0); runtime may still be running prior config'}\n",
        ),
        (
            "runner-error-kind",
            "deploy_v2 failed: ended 'failed': {'details': {'failure_summary': "
            "[{'error_kind': 'vault_secret_missing', 'summary': 'lookup vault: server "
            "misbehaving'}]}}\n",
        ),
        ("unrecognized", "deploy_v2 failed: KeyError: 'compose_id'\n"),
        ("no-diagnostic", ""),
    ],
)
def test_a_deterministic_failure_is_not_retried(label, stderr):
    """Auth, config, a health check after a good pull, a changed hash, an unknown error:
    a second run repeats the failure. Only a recognised transient fault earns a retry. A
    deterministic marker wins over a transient phrase in the same message."""
    script = _Script((1, stderr), (0, ""))
    sleeps: list[float] = []

    results = _run([_command("prod")], script, sleeps=sleeps)

    assert len(script.argvs) == 1, label
    assert results[0]["returncode"] == 1
    assert len(results[0]["attempts"]) == 1
    assert results[0]["attempts"][0]["cause"]  # the failure is recorded, not hidden
    assert sleeps == []


def test_the_third_transient_failure_fails_the_unit():
    """At most 2 retries: three runs in all. A unit after it is not started."""
    first = _command("staging")
    second = _command("staging")
    script = _Script((1, DNS_FAILURE), (1, DNS_FAILURE), (1, DNS_FAILURE), (0, ""))
    sleeps: list[float] = []

    results = _run([first, second], script, sleeps=sleeps)

    assert len(script.argvs) == 3, "two retries and no third"
    assert len(results) == 1 and results[0]["returncode"] == 1
    assert [a["n"] for a in results[0]["attempts"]] == [1, 2, 3]
    assert all(a["cause"].startswith("dns:") for a in results[0]["attempts"])
    assert sleeps == [30.0, 90.0]


@pytest.mark.parametrize("deploy_type", ["staging", "prod"])
@pytest.mark.parametrize(
    ("kind", "stderr"),
    [
        (
            "runner_not_found",
            "deploy_v2 failed: iac_runner answered not_found for 100s while polling "
            "deploy dddddddddddd to production (deployment x): the runner has no record "
            "of this deployment; it was probably recreated (#666)\n",
        ),
        (
            "runner_interrupted",
            "deploy_v2 failed: iac_runner reported interrupted for deploy dddddddddddd to "
            "production (deployment x): the runner restarted while this deployment was in "
            "flight and its outcome is unknown (#1439)\n",
        ),
        (
            "network_timeout",
            'deploy_v2 failed: Get "https://ghcr.io/v2/": dial tcp 140.82.0.1:443: '
            "i/o timeout\n",
        ),
        (
            "tls_timeout",
            "deploy_v2 failed: Dokploy deployment record entered error (log_tail=net/http: "
            "TLS handshake timeout)\n",
        ),
        (
            "registry_5xx",
            "deploy_v2 failed: Dokploy deployment record entered error (log_tail=Error "
            "response from daemon: received unexpected HTTP status: 503 Service "
            "Unavailable)\n",
        ),
    ],
)
def test_every_transient_class_is_retried_in_both_environments(
    deploy_type, kind, stderr
):
    """The runner losing a request is retried for production too: the owner's decision
    replaces the old rule that a production deploy never re-submits."""
    script = _Script((1, stderr), (0, ""))

    results = _run([_command(deploy_type)], script)

    assert len(script.argvs) == 2
    assert results[0]["returncode"] == 0
    assert results[0]["attempts"][0]["cause"].startswith(f"{kind}:")


def test_warning_lines_before_the_verdict_do_not_make_a_failure_transient():
    """deploy_v2 prints the Dokploy retry warning ("server misbehaving") before it fails
    for another reason. Only the failure verdict is classified."""
    stderr = (
        "Dokploy deploy failed (lookup ghcr.io: server misbehaving); retrying once with "
        "compose.redeploy\n"
        "deploy_v2 failed: KeyError: 'compose_id'\n"
    )
    script = _Script((1, stderr), (0, ""))

    results = _run([_command("prod")], script)

    assert len(script.argvs) == 1 and results[0]["returncode"] == 1


def test_a_first_try_success_has_one_attempt_without_cause():
    script = _Script((0, ""))

    results = _run([_command("staging")], script)

    assert results[0]["attempts"] == [
        {"n": 1, "cause": None, "at": "2026-11-05T08:00:00Z"}
    ]


def test_each_retry_is_logged_with_its_cause(capsys):
    script = _Script((1, DNS_FAILURE), (0, ""))

    _run([_command("prod")], script)

    err = capsys.readouterr().err
    assert "reconcile retry" in err
    assert "attempt 2 of 3" in err
    assert "dns:" in err and "server misbehaving" in err


def test_the_run_summary_names_each_retry_and_its_cause(tmp_path):
    script = _Script((1, DNS_FAILURE), (0, ""))
    command = _command("prod")
    results = _run([command], script)
    summary = tmp_path / "summary.md"

    write_summary(
        plan=build_plan(["platform/12.alerting/compose.yaml"]),
        commands=[command],
        results=results,
        dry_run=False,
        path=str(summary),
    )

    text = summary.read_text(encoding="utf-8")
    assert "Unit retries" in text
    assert "attempt 1" in text and "server misbehaving" in text
    assert "attempt 2" in text


def _main_with_script(monkeypatch, capsys, script):
    """`main --promote-prod` over the real retry loop and a scripted runner."""
    marker_recorded: list[str] = []
    monkeypatch.setattr(reconcile, "assert_after_on_main", lambda *_a, **_kw: None)
    monkeypatch.setattr(
        reconcile, "assert_production_marker_advances", lambda *_a, **_kw: "v3.1.22"
    )
    monkeypatch.setattr(
        reconcile,
        "record_production_marker",
        lambda tag, *_a, **_kw: marker_recorded.append(tag) or f"production/{tag}",
    )
    monkeypatch.setattr(
        reconcile,
        "run_deploy_commands",
        lambda cmds, **_kw: REAL_RUN_DEPLOY_COMMANDS(
            cmds, runner=script, sleep=lambda _s: None
        ),
    )
    rc = main(
        [
            "--repo-root",
            str(ROOT),
            "--before",
            "v3.1.22",
            "--after",
            "v3.1.23",
            "--promote-prod",
            "--changed-file",
            "platform/12.alerting/compose.yaml",
        ]
    )
    return rc, marker_recorded, json.loads(capsys.readouterr().out)


def test_the_production_marker_advances_only_after_the_unit_succeeds_on_retry(
    monkeypatch, capsys
):
    script = _Script((1, DNS_FAILURE), (0, ""))

    rc, marker_recorded, payload = _main_with_script(monkeypatch, capsys, script)

    assert rc == 0 and marker_recorded == ["v3.1.23"]
    assert payload["results"][0]["attempts"][0]["cause"].startswith("dns:")
    assert payload["production_marker"] == "production/v3.1.23"
    # The tag never changed between the attempts.
    assert script.argvs[0] == script.argvs[1]
    assert payload["applied"][0]["argv"] == script.argvs[0]


def test_the_production_marker_does_not_advance_when_the_retries_are_spent(
    monkeypatch, capsys
):
    script = _Script((1, DNS_FAILURE), (1, DNS_FAILURE), (1, DNS_FAILURE))

    rc, marker_recorded, payload = _main_with_script(monkeypatch, capsys, script)

    assert rc == 1 and marker_recorded == []
    assert payload["production_marker"] == ""
    assert len(payload["results"][0]["attempts"]) == 3
    assert len(script.argvs) == 3


def test_the_alert_names_the_attempt_count_of_a_unit_that_stayed_red():
    result = {
        "service": "truealpha/data_engine",
        "type": "prod",
        "returncode": 1,
        "stdout": "",
        "stderr": DNS_FAILURE,
        "attempts": [
            {"n": 1, "cause": "dns: x", "at": "t1"},
            {"n": 2, "cause": "dns: x", "at": "t2"},
            {"n": 3, "cause": "dns: x", "at": "t3"},
        ],
    }

    text = reconcile.failure_alert_text(
        after="v3.1.23", results=[result], run_url="u", promote_prod=True
    )

    assert text is not None and "after 3 attempts" in text


@pytest.mark.parametrize("fault", ["not_found", "interrupted"])
def test_the_messages_of_the_real_client_are_classified_as_transient(fault):
    """Join the two halves: the error text the REAL poll raises, as deploy_v2 prints it,
    must be recognised by the classifier. A reworded client message breaks this."""
    from common.deploy.iac_runner_client import poll_platform_deploy_status
    from common.deploy.transient_failure import classify_failure

    class Answer:
        content = b"x"

        def __init__(self, payload, status_code):
            self.payload, self.status_code = payload, status_code

        def json(self):
            return self.payload

        def raise_for_status(self):
            pass

    moment = [1700000000.0]

    def transport(url, *, content, headers, timeout):
        if fault == "interrupted":
            return Answer({"status": "interrupted", "deployment_id": "b" * 16}, 200)
        return Answer({"status": "not_found"}, 404)

    with pytest.raises(RuntimeError) as excinfo:
        poll_platform_deploy_status(
            env="production",
            ref=SHA,
            base_url="https://iac.example",
            secret="s",
            attempts=335,
            interval=10.0,
            now=lambda: moment[0],
            sleep=lambda seconds: moment.__setitem__(0, moment[0] + seconds),
            nonce_factory=lambda: "nonce123",
            transport=transport,
        )

    verdict = classify_failure(f"deploy_v2 failed: {excinfo.value}\n")
    assert verdict.transient is True
    assert verdict.kind == f"runner_{fault}"


# --- #1439 review of 4c8d50b: rollback, breaker, narrower patterns, which line ---------


def _classify(stderr: str):
    from common.deploy.transient_failure import classify_failure

    return classify_failure(stderr)


def test_a_rollback_circuit_breaker_message_is_never_transient():
    """The breaker message carries the original failure text, `server misbehaving`
    included. A train that retried on it would re-promote an app whose automatic rollback
    is switched off (P0 alert, manual intervention required)."""
    stderr = (
        "deploy_v2 failed: automatic rollback circuit breaker tripped for "
        "finance_report/app (production): attempt 3 exceeds max_rollback_attempts=2; "
        "manual intervention required (P0 alert emitted): deploy rollout entered error "
        "(errorMessage=lookup ghcr.io on 127.0.0.53:53: server misbehaving)\n"
    )
    verdict = _classify(stderr)
    assert verdict.transient is False
    assert verdict.kind == "rollback"


def test_a_failure_after_the_rollout_started_is_never_transient():
    """promote prints the rollout marker before its first write to the compose. From then
    on the compose env, the containers or a rollback may have changed, so the train does not
    run the unit again, whatever the verdict says."""
    from common.deploy.rollout import ROLLOUT_STARTED_MARKER

    stderr = (
        f"{ROLLOUT_STARTED_MARKER} finance_report/app (compose c1)\n"
        "deploy_v2 failed: deploy rollout entered error (errorMessage=lookup ghcr.io "
        "on 127.0.0.53:53: server misbehaving)\n"
    )
    verdict = _classify(stderr)
    assert verdict.transient is False
    assert verdict.kind == "rollout_started"


def test_a_platform_unit_has_no_rollout_marker_and_is_still_retried():
    """The platform path goes through the runner, never through promote: no marker, so the
    unit keeps its retry. The result says so."""
    script = _Script((1, DNS_FAILURE), (0, ""))

    results = _run([_command("prod")], script)

    assert len(script.argvs) == 2
    assert results[0]["rollout_started"] is False
    assert results[0]["rolled_back"] is False


@pytest.mark.parametrize(
    ("label", "stderr"),
    [
        (
            "version-probe-502",
            "deploy_v2 failed: version probe https://report.zitian.party/version "
            "returned 502 Bad Gateway\n",
        ),
        (
            "ci-side-504",
            "deploy_v2 failed: GET https://api.github.com/repos/x/y/git/ref/tags/v1: "
            "504 Gateway Time-out\n",
        ),
        (
            "ci-side-name-resolution-no-verdict",
            "Traceback (most recent call last):\n"
            '  File "x.py", line 1, in <module>\n'
            "httpx.ConnectError: [Errno -3] Temporary failure in name resolution\n",
        ),
    ],
)
def test_the_transient_patterns_need_their_own_context(label, stderr):
    """`registry_5xx` needs a registry or pull line; `dns` from the glibc resolver needs
    the `deploy_v2 failed:` verdict. A bare 502 or a stray CI-side line is not enough."""
    assert _classify(stderr).transient is False, label


@pytest.mark.parametrize(
    ("kind", "stderr"),
    [
        (
            "registry_5xx",
            "deploy_v2 failed: Dokploy deployment record entered error (log_tail=Head "
            '"https://ghcr.io/v2/wangzitian0/app/manifests/v1": 502 Bad Gateway)\n',
        ),
        (
            "registry_5xx",
            "deploy_v2 failed: pulling image from registry-1.docker.io failed: "
            "504 Gateway Time-out\n",
        ),
        (
            "dns",
            "deploy_v2 failed: [Errno -3] Temporary failure in name resolution\n",
        ),
        (
            "dns",
            'Error response from daemon: Get "https://ghcr.io/v2/": dial tcp: lookup '
            "ghcr.io on 127.0.0.53:53: server misbehaving\n",
        ),
    ],
)
def test_the_transient_patterns_still_match_in_their_own_context(kind, stderr):
    verdict = _classify(stderr)
    assert (verdict.transient, verdict.kind) == (True, kind)


def test_the_last_failure_verdict_is_the_one_that_is_read():
    """Two `deploy_v2 failed:` lines (a wrapper printed the first): the LAST one is the
    verdict. Reading the first would retry a unit whose final failure is deterministic,
    or skip the retry of one whose final failure is transient."""
    transient = "deploy_v2 failed: i/o timeout reaching ghcr.io\n"
    deterministic = "deploy_v2 failed: KeyError: 'compose_id'\n"
    assert _classify(transient + deterministic).transient is False
    assert _classify(deterministic + transient).transient is True


def test_every_attempt_reports_whether_its_rollout_ran_or_was_rolled_back():
    from common.deploy.rollout import ROLLBACK_EVENT_MARKER, ROLLOUT_STARTED_MARKER

    stderr = (
        f"{ROLLOUT_STARTED_MARKER} finance_report/app (compose c1)\n"
        f"{ROLLBACK_EVENT_MARKER} [WARN] Deploy rollback event | Action: rolled_back\n"
        "deploy_v2 failed: deploy rollout entered error\n"
    )
    script = _Script((1, stderr), (0, ""))

    results = _run([_command("prod")], script)

    assert len(script.argvs) == 1
    assert results[0]["rollout_started"] is True
    assert results[0]["rolled_back"] is True
