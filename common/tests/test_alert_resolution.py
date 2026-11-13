"""#1648 AC5: every failure alert of a scheduled job resolves with exactly one card.

Each case runs the real inline script of the workflow step, on a fake issue store and a
fake delivery: one red run pages once, the next green run sends exactly one resolved
card, and a second green run sends nothing. A job whose alert has no resolve step, or
whose resolve step sends no card, fails here.
"""

from __future__ import annotations

import io
import json
import re
from contextlib import redirect_stdout
from pathlib import Path

import pytest
import yaml

from common.observability import page_dedup
from common.tests.test_page_dedup import FakeIssues

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github/workflows"

SCHEDULED = {
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

#: (workflow, job, alert step, resolve step). The runner-health step does both.
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
        "ops-checks.yml",
        "host-hygiene-schedule",
        "Alert on failure",
        "Resolve the host-hygiene alert",
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


class World:
    def __init__(self, monkeypatch, tmp_path) -> None:
        self.cards: list[dict] = []
        self.api = FakeIssues()
        for key, value in SCHEDULED.items():
            monkeypatch.setenv(key, value)
        monkeypatch.delenv("INFRA2_WATCHDOG_VERDICTS_PATH", raising=False)
        monkeypatch.chdir(tmp_path)  # no reconcile-result.json: the crash alert
        monkeypatch.setattr(page_dedup, "_api_from_env", lambda env: self.api)
        import tools.out_of_band_watchdog as watchdog

        monkeypatch.setattr(
            watchdog,
            "deliver_out_of_band_card",
            lambda env, card, **kw: self.cards.append(card),
        )

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

    def titles(self) -> list[str]:
        return [card["header"]["title"]["content"] for card in self.cards]


@pytest.mark.parametrize(("workflow", "job", "alert", "resolve"), SPLIT_STEPS)
def test_one_red_run_and_one_green_run_send_one_resolved_card(
    monkeypatch, tmp_path, workflow, job, alert, resolve
) -> None:
    world = World(monkeypatch, tmp_path)

    world.run(_script(workflow, job, alert))
    assert len(world.cards) == 1
    assert world.cards[0]["header"]["template"] == "red"
    assert world.api.open_titles()  # the open state that the resolve step closes

    world.run(_script(workflow, job, resolve))
    assert len(world.cards) == 2
    assert world.cards[1]["header"]["template"] == "green"
    assert world.titles()[1].startswith("✅ [已恢复]")
    assert world.api.open_titles() == []

    world.run(_script(workflow, job, resolve))
    assert len(world.cards) == 2  # nothing is left to resolve


def test_the_runner_health_step_pages_once_and_resolves_once(
    monkeypatch, tmp_path
) -> None:
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
    assert len(world.cards) == 1 and world.cards[0]["header"]["template"] == "red"

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
    assert len(world.cards) == 2
    assert world.titles()[1].startswith("✅ [已恢复]")
    assert world.api.open_titles() == []
