"""Every workflow mutating the reserved pr-999 slot must hold one job lock."""

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]


def _workflow(name: str) -> dict:
    path = ROOT / ".github" / "workflows" / name
    return yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


def test_all_reserved_canaries_share_one_non_cancelling_job_lock() -> None:
    receiver = _workflow("app-deploy-request.yml")
    manual = _workflow("deploy.yml")
    scheduled = _workflow("ops-checks.yml")
    locks = (
        receiver["jobs"]["preflight_canary"]["concurrency"],
        manual["jobs"]["preflight_canary"]["concurrency"],
        scheduled["jobs"]["deploy-v2-canary"]["concurrency"],
    )
    assert locks[0] == locks[1] == locks[2]
    assert locks[0]["queue"] == "max"
    assert locks[0]["cancel-in-progress"] == "false"
    # The scheduled workflow itself also has a concurrency group. Reusing that
    # name for its child job could make the workflow wait on its own lock.
    outer_group_expr = scheduled["concurrency"]["group"]
    assert f"'{locks[0]['group']}'" not in outer_group_expr
