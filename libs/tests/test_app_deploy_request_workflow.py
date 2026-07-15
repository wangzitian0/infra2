"""Static contract for the app-deploy-request repository_dispatch receiver."""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/app-deploy-request.yml"


def workflow() -> dict:
    return yaml.load(WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


def test_receiver_has_only_the_versioned_repository_dispatch_trigger() -> None:
    triggers = workflow()["on"]
    assert triggers == {
        "repository_dispatch": {"types": ["app-deploy-request"]},
    }


def test_receiver_validates_before_canary_and_deploy() -> None:
    jobs = workflow()["jobs"]
    assert set(jobs) == {"validate", "preflight_canary", "deploy"}
    assert jobs["preflight_canary"]["needs"] == ["validate"]
    assert jobs["deploy"]["needs"] == ["validate", "preflight_canary"]
    assert "needs.validate.result == 'success'" in jobs["deploy"]["if"]


def test_receiver_never_checks_out_application_or_exposes_dokploy_to_validation() -> (
    None
):
    body = WORKFLOW.read_text(encoding="utf-8")
    validate = workflow()["jobs"]["validate"]
    assert "DOKPLOY_API_KEY" not in str(validate)
    assert "repository:" not in body
    assert body.count("python -m tools.app_deploy_request") >= 3
    assert "python -m tools.deploy_v2_canary" in body
