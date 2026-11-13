"""#1648 AC5: every failure alert of a scheduled job resolves with exactly one card.

Each case runs the real inline script of the workflow step, on a fake issue store and a
fake delivery: one red run pages once, the next green run sends exactly one resolved
card, and a second green run sends nothing. A job whose alert has no resolve step, or
whose resolve step sends no card, fails here.
"""

from __future__ import annotations

import io
import json
import os
import re
from contextlib import redirect_stdout
from pathlib import Path

import pytest
import yaml

from common.observability import page_dedup
from common.tests.test_page_dedup import FakeIssues

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github/workflows"

REPORTS = {
    "INFRA2_REPORTS_FEISHU_APP_ID": "a",
    "INFRA2_REPORTS_FEISHU_APP_SECRET": "b",
    "INFRA2_REPORTS_FEISHU_CHAT_ID": "oc_reports",
}
SCHEDULED = {
    **REPORTS,
    "GITHUB_EVENT_NAME": "schedule",
    "GITHUB_REPOSITORY": "wangzitian0/infra2",
    "GITHUB_SERVER_URL": "https://github.com",
    "GITHUB_RUN_ID": "7",
    "GITHUB_TOKEN": "token-for-tests",
    "INPUT_AFTER": "v9.9.9",
    "INPUT_PROMOTE_PROD": "false",
    "IAC_RUNNER_URL": "https://iac.invalid",
    "FEISHU_WEBHOOK_URL": "https://open.feishu.cn/open-apis/bot/v2/hook/tok",
}

#: (workflow, job, alert step, resolve step).
SPLIT_STEPS = [
    (
        "ops-checks.yml",
        "digest",
        "Alert on failure",
        "Resolve the digest failure alert",
    ),
    (
        "ops-checks.yml",
        "preview-leak-check",
        "Alert on leaked previews",
        "Resolve the leaked-preview alert",
    ),
    (
        "reconcile-iac-inputs.yml",
        "reconcile",
        "Alert on failure",
        "Resolve the reconcile failure alert",
    ),
]
RUNNER_STEP = "Runner health — 1Password service account and deploy prerequisites"


def _script(workflow: str, job: str, step_name: str) -> str:
    data = yaml.safe_load((WORKFLOWS / workflow).read_text(encoding="utf-8"))
    (step,) = [s for s in data["jobs"][job]["steps"] if s.get("name") == step_name]
    match = re.search(r"python - <<'PY'\n(.*?)\nPY\s*$", step["run"], re.S)
    assert match, step_name
    return match.group(1)


def _step(workflow: str, job: str, step_name: str) -> dict:
    data = yaml.safe_load((WORKFLOWS / workflow).read_text(encoding="utf-8"))
    (step,) = [s for s in data["jobs"][job]["steps"] if s.get("name") == step_name]
    return step


def _runs(
    step: dict, *, ok: bool, env: dict, event: str = "schedule", dry: bool = False
):
    """Would GitHub run this step? Evaluates the `if:` of the workflow (the forms that
    these steps use) for a job whose earlier steps all succeeded (ok) or one failed."""
    expr = str(step.get("if", "success()")).strip()
    if expr.startswith("${{"):
        expr = expr.removeprefix("${{").removesuffix("}}").strip()
    expr = expr.replace("!=", " __NE__ ")
    expr = re.sub(r"!(?!=)", " not ", expr)
    expr = expr.replace("__NE__", "!=").replace("&&", " and ").replace("||", " or ")
    expr = re.sub(r"env\.(\w+)", r'_env.get("\1", "")', expr)
    expr = expr.replace("github.event_name", "_event").replace("inputs.dry_run", "_dry")
    expr = expr.replace("success()", "_ok").replace("failure()", "(not _ok)")
    expr = expr.replace("always()", "True").replace("!cancelled()", "True")
    return bool(
        eval(  # noqa: S307 - the expression is a checked-in workflow condition
            expr,
            {"__builtins__": {}},
            {"_ok": ok, "_env": env, "_event": event, "_dry": dry},
        )
    )


class World:
    def __init__(self, monkeypatch, tmp_path) -> None:
        self.cards: list[dict] = []
        self.reports: list[dict] = []
        self.api = FakeIssues()
        self.monkeypatch = monkeypatch
        for key, value in SCHEDULED.items():
            monkeypatch.setenv(key, value)
        monkeypatch.delenv("INFRA2_WATCHDOG_VERDICTS_PATH", raising=False)
        monkeypatch.chdir(tmp_path)  # no reconcile-result.json: the crash alert
        monkeypatch.setattr(page_dedup, "_api_from_env", lambda env: self.api)
        import common.alerting as alerting
        import tools.out_of_band_watchdog as watchdog

        monkeypatch.setattr(
            watchdog,
            "deliver_out_of_band_card",
            lambda env, card, **kw: self.cards.append(card),
        )
        monkeypatch.setattr(
            alerting,
            "deliver_feishu_app_card",
            lambda **kw: self.reports.append(kw["card"]),
        )

    def set(self, **env: str) -> None:
        for key, value in env.items():
            self.monkeypatch.setenv(key, value)

    def run(self, script: str) -> int:
        code = 0
        try:
            with redirect_stdout(io.StringIO()):
                exec(
                    compile(script, "<workflow step>", "exec"), {"__name__": "__main__"}
                )
        except SystemExit as exit_:
            code = exit_.code or 0
        return code

    def step(self, workflow, job, name, *, ok, event="schedule", **env) -> bool:
        """Run a step the way the workflow gates it; True when GitHub would run it."""
        self.set(GITHUB_EVENT_NAME=event, **env)
        step = _step(workflow, job, name)
        merged = {**os.environ}
        if not _runs(step, ok=ok, env=merged, event=event):
            return False
        self.run(_script(workflow, job, name))
        return True

    def titles(self) -> list[str]:
        return [card["header"]["title"]["content"] for card in self.cards]


@pytest.mark.parametrize(("workflow", "job", "alert", "resolve"), SPLIT_STEPS)
def test_one_red_run_and_one_green_run_send_one_resolved_card(
    monkeypatch, tmp_path, workflow, job, alert, resolve
) -> None:
    world = World(monkeypatch, tmp_path)
    deploy = {"RECONCILE_PHASE": "reconcile-deploy"}

    assert world.step(workflow, job, alert, ok=False, **deploy)
    assert len(world.cards) == 1
    assert world.cards[0]["header"]["template"] == "red"
    assert world.api.open_titles()  # the open state that the resolve step closes

    assert world.step(workflow, job, resolve, ok=True, **deploy)
    assert len(world.cards) == 2
    assert world.cards[1]["header"]["template"] == "green"
    assert world.titles()[1].startswith("✅ [已恢复]")
    assert world.api.open_titles() == []

    world.step(workflow, job, resolve, ok=True, **deploy)
    assert len(world.cards) == 2  # nothing is left to resolve


@pytest.mark.parametrize(("workflow", "job", "alert", "resolve"), SPLIT_STEPS)
def test_each_resolve_step_runs_on_success_after_its_alert_with_its_token(
    workflow, job, alert, resolve
) -> None:
    """The scripts above run in isolation; this reads the workflow: the resolve step runs
    on a green run (not on a red one), carries the token the state needs, and comes
    after the step it resolves."""
    data = yaml.safe_load((WORKFLOWS / workflow).read_text(encoding="utf-8"))
    names = [s.get("name") for s in data["jobs"][job]["steps"]]
    step = _step(workflow, job, resolve)
    env = {"RECONCILE_PHASE": "reconcile-deploy"}

    assert names.index(resolve) > names.index(alert)
    assert _runs(step, ok=True, env=env)
    assert not _runs(step, ok=False, env=env)
    assert step["env"]["GITHUB_TOKEN"] == "${{ github.token }}"


def test_a_resolve_step_that_runs_on_failure_is_caught() -> None:
    """The check above is falsifiable: `if: failure()` would run on a red run."""
    broken = {"if": "failure() && !(github.event_name == 'workflow_dispatch')"}
    assert _runs(broken, ok=False, env={})
    assert not _runs(broken, ok=True, env={})


def test_a_failed_promotion_is_not_resolved_by_a_sync_only_run(
    monkeypatch, tmp_path
) -> None:
    """#1648 review (high): a scheduled run only syncs App releases. It neither pages nor
    resolves; the next green deploy run sends the one resolved card."""
    w = World(monkeypatch, tmp_path)
    name = "reconcile-iac-inputs.yml"
    alert, resolve = "Alert on failure", "Resolve the reconcile failure alert"

    w.step(
        name,
        "reconcile",
        alert,
        ok=False,
        event="workflow_dispatch",
        RECONCILE_PHASE="reconcile-deploy",
        INPUT_PROMOTE_PROD="true",
        GITHUB_REF="refs/heads/main",
    )
    assert len(w.cards) == 1 and w.api.open_titles()

    # the next scheduled run: sync-only, green
    ran = w.step(
        name, "reconcile", resolve, ok=True, RECONCILE_PHASE="sync-app-releases"
    )
    assert not ran and len(w.cards) == 1 and w.api.open_titles()
    # a dry run of a deploy phase does not resolve either
    monkeypatch.setenv("INPUT_DRY_RUN", "true")
    assert not _runs(
        _step(name, "reconcile", resolve),
        ok=True,
        env={"RECONCILE_PHASE": "reconcile-deploy"},
        event="workflow_dispatch",
        dry=True,
    )

    # the next green deploy run
    w.step(name, "reconcile", resolve, ok=True, RECONCILE_PHASE="reconcile-deploy")
    assert len(w.cards) == 2 and w.titles()[1].startswith("✅ [已恢复]")
    assert w.api.open_titles() == []


def test_the_tag_push_train_pages_once_and_resolves(monkeypatch, tmp_path) -> None:
    """#1648 review: a tag push is the staging train. The state key is the workflow, not
    the event: a red tag run pages once, a second red run reports, a green run resolves."""
    w = World(monkeypatch, tmp_path)
    name = "reconcile-iac-inputs.yml"
    push = {
        "event": "push",
        "RECONCILE_PHASE": "reconcile-deploy",
        "GITHUB_REF": "refs/tags/v9.9.9",
    }

    w.step(name, "reconcile", "Alert on failure", ok=False, **push)
    assert len(w.cards) == 1
    w.step(name, "reconcile", "Alert on failure", ok=False, **push)
    assert len(w.cards) == 1  # the same failure again: not a second page
    assert len(w.reports) == 1  # ... it is a report in the reports chat

    w.step(name, "reconcile", "Resolve the reconcile failure alert", ok=True, **push)
    assert len(w.cards) == 2 and w.titles()[1].startswith("✅ [已恢复]")
    assert w.api.open_titles() == []


def test_the_runner_health_step_goes_red_and_never_pages(monkeypatch, tmp_path) -> None:
    """#1648: the 60 s probe iac-runner-http pages the iac-runner condition; the daily
    step keeps its check and goes red; it sends no Feishu message."""
    import urllib.request

    world = World(monkeypatch, tmp_path)
    health: dict = {"checks": {"op_service_account_token": False}}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps(health).encode()

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: Response())
    script = _script("ops-checks.yml", "watchdog", RUNNER_STEP)

    assert world.run(script) == 1
    assert world.cards == [] and world.api.open_titles() == []  # no page, no state
    assert world.reports == []

    health["checks"] = {
        name: True
        for name in (
            "op_service_account_token",
            "dokploy_api_key",
            "git_repo_url",
            "binary:op",
        )
    }
    assert world.run(script) == 0
    assert world.cards == [] and world.reports == []
