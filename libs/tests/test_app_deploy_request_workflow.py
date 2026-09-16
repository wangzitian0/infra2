"""Static contract for the app-deploy-request repository_dispatch receiver."""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/app-deploy-request.yml"


def workflow() -> dict:
    return yaml.load(WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


def steps() -> list[dict]:
    return workflow()["jobs"]["deploy"]["steps"]


def step(name: str) -> dict:
    for entry in steps():
        if entry.get("name") == name:
            return entry
    raise AssertionError(
        f"no step named {name!r}; steps were {[s.get('name') for s in steps()]}"
    )


def test_receiver_has_only_the_versioned_repository_dispatch_trigger() -> None:
    definition = workflow()
    triggers = definition["on"]
    assert triggers == {
        "repository_dispatch": {"types": ["app-deploy-request"]},
    }
    assert definition["permissions"] == {"actions": "read", "contents": "read"}
    assert definition["env"]["GITHUB_TOKEN"] == "${{ github.token }}"


def test_receiver_is_one_job_not_three() -> None:
    """#truealpha critical-path (2026-09-15/16): three jobs each paid their own
    checkout + pip install (35-50s total) plus 10-20s of inter-job queueing. Neither
    truealpha's nor finance_report's sender keys on this workflow's job/step names
    (verified 2026-09-16: they watermark the RECEIVER'S RUN via infra2_sdk.dispatch,
    wait for its overall conclusion, and grep the run's logs for the request id) —
    only `run-name` (display_title) is a cross-repo contract, so collapsing the three
    jobs into one job of three steps is safe."""
    jobs = workflow()["jobs"]
    assert set(jobs) == {"deploy"}
    names = [s.get("name") for s in steps()]
    assert names == [
        "Checkout released infra context",
        "Set up Python",
        "Install receiver dependencies",
        "Validate and display the side-effect-free plan",
        "Canary the exact app and IaC coordinates",
        "Execute through deploy_v2",
    ]
    # Exactly one checkout / setup / install for the whole run, not one per stage.
    assert names.count("Checkout released infra context") == 1
    assert names.count("Install receiver dependencies") == 1


def test_receiver_never_checks_out_application_or_exposes_dokploy_to_validation() -> (
    None
):
    body = WORKFLOW.read_text(encoding="utf-8")
    plan_step = step("Validate and display the side-effect-free plan")
    assert "DOKPLOY_API_KEY" not in str(plan_step)
    assert "repository:" not in body
    assert body.count("python -m tools.app_deploy_request") >= 3
    assert "python -m tools.deploy_v2_canary" in body


def test_canary_runs_only_when_the_plan_requires_it_and_validate_succeeded() -> None:
    """Whether the canary step runs must come from DeployPlan.requires_preflight_canary
    (libs/app_deploy_request.py), via the plan step's own output — not a deploy_type
    literal hand-copied into this YAML's `if:`. A hand-copied list here can drift from
    FIXED_DEPLOY_TYPES the moment a new fixed deploy_type is added, and nothing would
    catch it. `success()` is required explicitly: an `if:` on a step replaces its
    default success() check, so without it a failed plan step (whose outputs never
    got written) would only accidentally read as not-'true' rather than being stated
    as the real gate."""
    body = WORKFLOW.read_text(encoding="utf-8")
    canary_step = step("Canary the exact app and IaC coordinates")
    condition = canary_step["if"]
    assert "success()" in condition
    assert "steps.plan.outputs.requires_preflight_canary == 'true'" in condition
    assert "deploy_type == 'staging'" not in body
    assert "deploy_type == 'prod'" not in body
    assert '--expected-iac-ref "$VALIDATED_IAC_REF"' in body
    assert body.count("steps.plan.outputs.iac_ref") == 2


def test_execute_step_has_no_explicit_if_so_a_skipped_canary_still_runs_it() -> None:
    """execute's default `if:` is the implicit success() — true unless a PRIOR step
    actually FAILED. A canary that did not run because this request's plan did not
    require one is "skipped", not "failed", so success() still holds and execute
    proceeds — this is "execute only if canary success/skipped" without needing to
    spell out the skipped case by hand."""
    execute_step = step("Execute through deploy_v2")
    assert "if" not in execute_step


def test_canary_and_execute_steps_are_unbuffered_for_visible_phase_timing() -> None:
    canary_step = step("Canary the exact app and IaC coordinates")
    execute_step = step("Execute through deploy_v2")
    assert canary_step["env"]["PYTHONUNBUFFERED"] == "1"
    assert execute_step["env"]["PYTHONUNBUFFERED"] == "1"


def test_deploy_job_carries_the_shared_canary_singleton_lock() -> None:
    """deploy-v2-canary is the cross-workflow lock for the one reserved preview slot
    (pr-999) every deploy_v2 canary — this receiver's, deploy.yml's, and
    ops-checks.yml's scheduled one — runs on. It cannot be scoped to just the canary
    step (concurrency groups gate a whole job, resolved before requires_preflight_canary
    is known), so the single job carries it unconditionally."""
    assert workflow()["jobs"]["deploy"]["concurrency"] == {
        "group": "deploy-v2-canary",
        "cancel-in-progress": "false",
    }


def test_preflight_canary_targets_the_requested_service() -> None:
    """A canary that ignores which service was requested silently validates the wrong
    app: a truealpha/app request canaried finance_report/app's reserved slot instead
    (report-pr-999), because --service was never wired through from the plan."""
    body = WORKFLOW.read_text(encoding="utf-8")
    assert 'json.load(sys.stdin)["request"]["service"]' in body
    assert '--service "$service"' in body


def test_receiver_run_name_matches_what_senders_poll_for() -> None:
    """infra2#537: without a matching run-name, a sender's receipt-polling loop
    (e.g. truealpha's deploy-release.yml, which searches this repo's Actions API for a
    run whose display_title equals "Deploy <service> <deploy_type> <version_ref>
    <source_sha> [<request_id>]") can never find its own receiver run — GitHub's default
    display_title for repository_dispatch is just the workflow name. This run-name must
    reproduce that exact string from the dispatch payload, in that exact order. This is
    the ONE cross-repo naming contract the receiver carries (job/step names are not
    matched by any sender — see test_receiver_is_one_job_not_three).
    """
    run_name = workflow()["run-name"]
    assert run_name == (
        "Deploy ${{ github.event.client_payload.service }} "
        "${{ github.event.client_payload.deploy_type }} "
        "${{ github.event.client_payload.version_ref }} "
        "${{ github.event.client_payload.source_sha }} "
        "[${{ github.event.client_payload.request_id }}]"
    )
