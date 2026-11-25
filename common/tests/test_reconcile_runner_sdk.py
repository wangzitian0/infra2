"""The reconcile stops before any deploy unit when the ref locks a newer SDK than the runner (#1822).

Staging v3.1.174 (run 45537760470) failed midway: the IaC runner held infra2-sdk 4.5.0, and
the ref locked 4.7.0 and used ``x-infra2.vault.env``, which 4.5.0 rejects. The runner image
is rebuilt only in a production promotion, so a staging run must stop early and say so.

### Mutation evidence
- Removing the ``runner_older`` raise lets ``4.5.0`` runner vs ``4.7.0`` ref pass:
  ``test_runner_sdk_older_than_ref_refuses`` fails.
- Comparing version strings instead of int tuples refuses ``4.10.0`` vs ``4.7.0``:
  ``test_runner_sdk_newer_passes`` fails.
- Using ``<=`` instead of ``<`` refuses equal versions: ``test_runner_sdk_equal_passes`` fails.
- Treating a missing ``sdk_version`` as a refusal fails ``test_runner_sdk_unknown_warns_and_passes``.
- Moving the check after ``run_deploy_commands`` (or deleting its call) fails
  ``test_runner_sdk_refusal_stops_before_any_deploy_unit``.
"""

from __future__ import annotations

import argparse
import json
import tomllib
from pathlib import Path

import pytest

import common.deploy.reconcile_iac_inputs as reconcile

ROOT = Path(__file__).resolve().parents[2]
URL = "https://iac.example"


def _check(runner: dict, ref_version: str) -> str:
    return reconcile.assert_runner_sdk_covers_ref(
        "v3.1.174",
        ROOT,
        runner_url=URL,
        read_health=lambda _url: runner,
        locked_version=lambda _ref, _root: ref_version,
    )


def test_runner_sdk_older_than_ref_refuses() -> None:
    with pytest.raises(SystemExit) as exc:
        _check({"sdk_version": "4.5.0", "git_sha": "52bb8bb"}, "4.7.0")
    message = str(exc.value)
    assert "infra2-sdk 4.7.0" in message
    assert "infra2-sdk 4.5.0" in message
    assert "production promotion" in message


def test_runner_sdk_equal_passes() -> None:
    detail = _check({"sdk_version": "4.7.0"}, "4.7.0")
    assert detail == "runner infra2-sdk 4.7.0 >= v3.1.174 locked 4.7.0"


def test_runner_sdk_newer_passes() -> None:
    detail = _check({"sdk_version": "4.10.0"}, "4.7.0")
    assert detail == "runner infra2-sdk 4.10.0 >= v3.1.174 locked 4.7.0"


@pytest.mark.parametrize("health", [{"git_sha": "52bb8bb"}, {"sdk_version": "unknown"}])
def test_runner_sdk_unknown_warns_and_passes(health, capsys) -> None:
    detail = _check(health, "4.7.0")
    assert detail.startswith("runner SDK unknown")
    assert "4.7.0" in detail
    assert "::warning::runner SDK unknown" in capsys.readouterr().out


def test_runner_sdk_unreadable_health_fails_closed() -> None:
    def unreachable(url: str) -> dict:
        raise RuntimeError(f"cannot read {url}/health: timed out")

    with pytest.raises(SystemExit, match="runner SDK check failed: cannot read"):
        reconcile.assert_runner_sdk_covers_ref(
            "v3.1.174",
            ROOT,
            runner_url=URL,
            read_health=unreachable,
            locked_version=lambda _ref, _root: "4.7.0",
        )


def test_runner_sdk_without_runner_url_fails_closed(monkeypatch) -> None:
    monkeypatch.delenv("IAC_RUNNER_URL", raising=False)
    with pytest.raises(SystemExit, match="IAC_RUNNER_URL is not set"):
        reconcile.assert_runner_sdk_covers_ref(
            "v3.1.174",
            ROOT,
            read_health=lambda _url: {"sdk_version": "9.9.9"},
            locked_version=lambda _ref, _root: "4.7.0",
        )


def test_runner_sdk_locked_version_reads_the_ref_lock() -> None:
    """The ref version comes from ``git show <ref>:uv.lock``, not from the working tree."""
    lock = tomllib.loads(
        reconcile.subprocess.run(
            ["git", "show", "HEAD:uv.lock"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    )
    expected = [p["version"] for p in lock["package"] if p["name"] == "infra2-sdk"]
    assert reconcile.locked_sdk_version_at_ref("HEAD", ROOT) == expected[0]
    with pytest.raises(ValueError, match="cannot read"):
        reconcile.locked_sdk_version_at_ref("no-such-ref-1822", ROOT)


def test_runner_sdk_health_reads_degraded_body(monkeypatch) -> None:
    """A 503 runner body still gives its ``sdk_version``."""
    import io
    import urllib.error

    def degraded(url, timeout):
        raise urllib.error.HTTPError(
            url,
            503,
            "degraded",
            {},
            io.BytesIO(json.dumps({"sdk_version": "4.5.0"}).encode()),
        )

    monkeypatch.setattr(reconcile.urllib.request, "urlopen", degraded)
    assert reconcile._read_runner_health(URL) == {"sdk_version": "4.5.0"}


def test_runner_sdk_refusal_stops_before_any_deploy_unit(monkeypatch, tmp_path) -> None:
    """Replay of v3.1.174: runner 4.5.0, ref 4.7.0. No deploy unit runs; the record is red."""
    plan = reconcile.build_plan(["platform/12.alerting/compose.yaml"])
    commands = reconcile.build_deploy_commands(
        plan, iac_ref="v3.1.174", domain="zitian.party", timeout=600
    )
    train = reconcile.TrainPlan(
        plan=plan,
        commands=commands,
        applied=[c for c in commands if c.deploy_type == "staging"],
        slots="smoke,staging_gate",
        before="v3.1.173",
    )
    assert train.applied
    monkeypatch.setenv("IAC_RUNNER_URL", URL)
    monkeypatch.setattr(
        reconcile, "_read_runner_health", lambda _url: {"sdk_version": "4.5.0"}
    )
    monkeypatch.setattr(
        reconcile, "locked_sdk_version_at_ref", lambda _ref, _root: "4.7.0"
    )
    monkeypatch.setattr(
        reconcile, "audit_app_hooks", lambda *_a, **_k: {"ok": True, "errors": []}
    )
    deployed: list[object] = []
    monkeypatch.setattr(
        reconcile, "run_deploy_commands", lambda cmds, **_k: deployed.append(cmds) or []
    )
    args = argparse.Namespace(
        after="v3.1.174", dry_run=False, output_json=str(tmp_path / "result.json")
    )
    stage = reconcile.StageRun(
        env="staging.release_validation", subject="tag:v3.1.174", dry_run=False
    )

    with pytest.raises(SystemExit, match="4.7.0.*4.5.0"):
        reconcile.run_operation(
            args, ROOT, stage, train, promote_prod=False, target_env="staging"
        )

    assert deployed == []
    record = stage.records[-1]
    sdk = next(c for c in record.checks if c.name == "runner_sdk")
    assert sdk.ok is False
    assert "4.5.0" in sdk.detail
    assert record.verdict == "fail"
