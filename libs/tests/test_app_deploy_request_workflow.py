"""Static contract for the app-deploy-request repository_dispatch receiver."""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/app-deploy-request.yml"


def workflow() -> dict:
    return yaml.load(WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


def test_receiver_has_only_the_versioned_repository_dispatch_trigger() -> None:
    definition = workflow()
    triggers = definition["on"]
    assert triggers == {
        "repository_dispatch": {"types": ["app-deploy-request"]},
    }
    assert definition["permissions"] == {"actions": "read", "contents": "read"}
    assert definition["env"]["GITHUB_TOKEN"] == "${{ github.token }}"


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


def test_receiver_run_name_matches_what_senders_poll_for() -> None:
    """infra2#537: without a matching run-name, a sender's receipt-polling loop
    (e.g. truealpha's deploy-release.yml, which searches this repo's Actions API for a
    run whose display_title equals "Deploy <service> <deploy_type> <version_ref>
    <source_sha> [<request_id>]") can never find its own receiver run — GitHub's default
    display_title for repository_dispatch is just the workflow name. This run-name must
    reproduce that exact string from the dispatch payload, in that exact order.
    """
    run_name = workflow()["run-name"]
    assert run_name == (
        "Deploy ${{ github.event.client_payload.service }} "
        "${{ github.event.client_payload.deploy_type }} "
        "${{ github.event.client_payload.version_ref }} "
        "${{ github.event.client_payload.source_sha }} "
        "[${{ github.event.client_payload.request_id }}]"
    )
