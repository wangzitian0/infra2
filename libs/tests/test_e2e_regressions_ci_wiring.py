"""e2e_regressions/ must actually be wired into CI, and its SSOT proof links
must point at real files.

Six SSOT docs (`docs/ssot/ops.e2e-regressions.md`, `core.md`,
`bootstrap.dns_and_cert.md`, `platform.domain.md`, `ops.test_coverage.md`,
`MANIFEST.yaml`) cite `e2e_regressions/` test files as "Proof" anchors, and
`ops.e2e-regressions.md`'s own architecture diagram declares
`CI[GitHub Actions] -> Trigger -> Runner -> Pytest -> Suite`. Until the
`e2e-regressions-smoke` job landed in `ops-checks.yml`, no workflow in this
repository ever ran anything under `e2e_regressions/` (`grep -rl
e2e_regressions .github/workflows/` was empty) -- the declared evidence never
produced. `pyproject.toml` independently confirms this was a *known*, *scoped*
gap: the `e2e` dependency group was kept out of `infra-ci`'s default install
"so the infra-ci unit-test job doesn't pay for deps it never imports" (#515) --
a statement about the fast PR gate, not a decision that nothing should ever run
the suite.

This test file holds two independent regressions:

1. The CI job must exist, be schedule + workflow_dispatch gated, and actually
   invoke pytest against `e2e_regressions/tests` with the `smoke` marker (the
   tier `ops.e2e-regressions.md` documents as the fast/first tier of its test
   pyramid). **This test executes against the real workflow YAML**, not a
   description of it -- a future rewrite that drops the job or the marker
   selection fails here, not just in a code review.
2. Every `e2e_regressions/tests/**/*.py` path an SSOT doc or MANIFEST.yaml
   cites as Proof must exist on disk. `docs/ssot/platform.domain.md` once
   cited `e2e_regressions/tests/platform/test_portal.py`, a file that never
   existed -- the real test is `tests/apps/test_portal_sso.py`. A citation
   that resolves to nothing is not evidence.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "ops-checks.yml"
DOCS_SSOT = ROOT / "docs" / "ssot"
JOB = "e2e-regressions-smoke"

# Matches both bare repo-relative paths and github.com/.../blob/main/... URLs --
# both spellings are used across the six citing docs.
PATH_RE = re.compile(r"e2e_regressions/tests/[A-Za-z0-9_./-]+\.py")


def _job() -> dict:
    jobs = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]
    assert JOB in jobs, (
        f"{WORKFLOW.relative_to(ROOT)} has no '{JOB}' job -- e2e_regressions has "
        "no CI wiring, so every SSOT doc citing it as Proof is citing dead evidence"
    )
    return jobs[JOB]


def test_the_job_is_actually_schedule_and_dispatch_gated() -> None:
    job = _job()
    condition = job.get("if", "")
    assert "github.event.schedule" in condition, (
        "the job exists but its `if:` never checks github.event.schedule -- it "
        "would never fire on the cron entry and the 'nightly' claim would be false"
    )
    assert "workflow_dispatch" in condition and JOB in condition, (
        "the job must also be reachable via workflow_dispatch's task input for "
        "on-demand verification"
    )


def test_the_job_runs_the_smoke_tier_against_e2e_regressions() -> None:
    job = _job()
    run_text = "\n".join(str(step.get("run") or "") for step in job.get("steps", []))
    assert "pytest" in run_text and "e2e_regressions/tests" in run_text, (
        "the job exists but no step actually invokes pytest against "
        "e2e_regressions/tests"
    )
    assert "-m smoke" in run_text, (
        "the job must select the 'smoke' tier "
        "(ops.e2e-regressions.md's declared fast/first pyramid level), not run "
        "un-scoped or a heavier marker as a silent scope expansion"
    )


def test_every_uv_invocation_in_the_job_is_locked() -> None:
    """`uv sync`/`uv run` must not be able to silently re-resolve a stale or
    drifted uv.lock in this job (#780 review): every `uv sync`/`uv run` call in
    the job must carry `--locked`, so a stale lockfile fails the job instead of
    uv quietly installing whatever it re-resolves to."""
    job = _job()
    for step in job.get("steps", []):
        run = str(step.get("run") or "")
        for line in run.splitlines():
            line = line.strip()
            if line.startswith("uv sync") or line.startswith("uv run"):
                assert "--locked" in line, (
                    f"{step.get('name', '?')!r} runs {line!r} without --locked -- "
                    "a stale/drifted uv.lock would be silently re-resolved instead "
                    "of failing the job"
                )


def _referenced_proof_paths() -> dict[str, list[str]]:
    """path -> list of citing files, for every e2e_regressions/tests/*.py path
    mentioned across the SSOT docs + MANIFEST.yaml."""
    referenced: dict[str, list[str]] = {}
    sources = sorted(DOCS_SSOT.glob("*.md")) + [DOCS_SSOT / "MANIFEST.yaml"]
    for src in sources:
        text = src.read_text(encoding="utf-8")
        for match in PATH_RE.findall(text):
            referenced.setdefault(match, []).append(str(src.relative_to(ROOT)))
    return referenced


def test_every_ssot_cited_e2e_regressions_path_exists_on_disk() -> None:
    referenced = _referenced_proof_paths()
    assert referenced, "sanity: expected at least one SSOT doc to cite e2e_regressions"
    missing = {
        path: citers for path, citers in referenced.items() if not (ROOT / path).is_file()
    }
    assert not missing, (
        "SSOT docs cite e2e_regressions test paths that do not exist on disk "
        f"(claimed Proof that can never run): {missing}"
    )


def test_manifest_e2e_proof_files_carry_a_smoke_marked_test() -> None:
    """Every e2e_regressions proof MANIFEST.yaml cites for ops.e2e must be a
    file the smoke job actually selects, not merely a file that exists.

    (Scoped to `e2e_regressions/*` proof entries only -- this test file itself
    is also listed as an ops.e2e proof, as the CI-wiring anchor, and is not an
    e2e_regressions smoke test.)
    """
    manifest = yaml.safe_load((DOCS_SSOT / "MANIFEST.yaml").read_text(encoding="utf-8"))
    proofs = manifest["entries"]["ops.e2e"]["proofs"]
    e2e_proofs = [p for p in proofs if p.startswith("e2e_regressions/")]
    assert e2e_proofs, "ops.e2e MANIFEST entry lost its e2e_regressions proofs"
    for rel in e2e_proofs:
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert "pytest.mark.smoke" in text, (
            f"{rel} is cited as the ops.e2e Proof but has no @pytest.mark.smoke "
            f"test, so the {JOB} job (which runs `-m smoke`) never executes it"
        )
