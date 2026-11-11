"""`.python-version` is the one source of the runtime Python version (#1506).

CI must test the Python that the runtime images run. Before #1506 the version was
written in 19 workflow steps, 4 Dockerfiles and 2 operator tasks. Now:

1. Every setup-python step reads `.python-version`, except the jobs that AGENTS.md
   makes production paths (`PRODUCTION_JOBS`). Those keep an explicit pin equal to
   the file: a later edit of only `.python-version` must not change a production job
   without an edit of that job, which needs the owner.
2. No workflow pins Python through `astral-sh/setup-uv`.
3. Every `python:<tag>` image reference in tracked code carries `<version>-slim`
   pinned by digest, except `PENDING_OWNER`: images that a merge can redeploy to
   production at once, so their move is a separate owner-approved change.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
VERSION_FILE = ROOT / ".python-version"
# A reusable workflow runs in the caller's checkout, so it cannot read this file.
REUSABLE_TEMPLATES = ".github/workflows/templates/"
PRODUCTION_JOBS = {
    (".github/workflows/ops-checks.yml", "host-hygiene-schedule"),
    (".github/workflows/ops-checks.yml", "facet-reconcile"),
}
# bootstrap/05.vault is on Dokploy-native autoDeploy (tools/dokploy_env.py
# AUTODEPLOY_ALLOWLIST): a merge can rebuild the production unsealer at once.
PENDING_OWNER = {"bootstrap/05.vault/Dockerfile": "python:3.11-slim"}
IMAGE_REF = re.compile(r"(?<![\w./-])(?:docker\.io/)?(?:library/)?python:([\w.@:-]+)")
SCANNED_SUFFIXES = (".py", ".sh", ".yml", ".yaml", ".toml")
SKIPPED_PREFIXES = (
    "repos/",
    "oh-my-code-agent/",
    "common/tests/",
    "docs/project/archive/",
)


def _version() -> str:
    return VERSION_FILE.read_text(encoding="utf-8").strip()


def _tracked(*patterns: str) -> list[str]:
    return subprocess.run(
        ["git", "ls-files", "--", *patterns],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()


def _workflow_steps():
    for rel in _tracked(
        ".github/workflows/*.yml",
        ".github/workflows/*.yaml",
        ".github/workflows/**/*.yml",
        ".github/workflows/**/*.yaml",
    ):
        if rel.startswith(REUSABLE_TEMPLATES):
            continue
        doc = yaml.safe_load((ROOT / rel).read_text(encoding="utf-8")) or {}
        for job_id, job in (doc.get("jobs") or {}).items():
            for step in job.get("steps") or []:
                yield rel, job_id, step


def _pinned_image(tag: str) -> bool:
    return bool(
        re.fullmatch(rf"{re.escape(_version())}-slim@sha256:[0-9a-f]{{64}}", tag)
    )


def test_the_version_file_names_one_minor_version() -> None:
    assert re.fullmatch(r"3\.\d+", _version()), _version()


def test_every_setup_python_step_reads_the_version_file_or_is_a_production_pin() -> (
    None
):
    seen: dict[tuple[str, str], dict] = {}
    for rel, job, step in _workflow_steps():
        if str(step.get("uses", "")).startswith("actions/setup-python"):
            seen[(rel, job)] = step.get("with") or {}
    assert len(seen) >= 10, "the scan found too few setup-python steps"
    assert PRODUCTION_JOBS <= set(seen), "a production job lost its setup-python step"
    wrong = []
    for key, inputs in seen.items():
        if key in PRODUCTION_JOBS:
            if (
                str(inputs.get("python-version")) != _version()
                or "python-version-file" in inputs
            ):
                wrong.append((key, inputs))
        elif (
            "python-version" in inputs
            or inputs.get("python-version-file") != ".python-version"
        ):
            wrong.append((key, inputs))
    assert not wrong, f"setup-python steps that break the version rule: {wrong}"


def test_no_workflow_pins_python_through_setup_uv() -> None:
    pinned = [
        (rel, job)
        for rel, job, step in _workflow_steps()
        if str(step.get("uses", "")).startswith("astral-sh/setup-uv")
        and "python-version" in (step.get("with") or {})
    ]
    assert not pinned, f"setup-uv steps that pin their own Python: {pinned}"


def test_every_python_image_reference_has_the_version_of_the_file() -> None:
    refs = []
    for rel in _tracked("*"):
        name = rel.rsplit("/", 1)[-1]
        if rel.startswith(SKIPPED_PREFIXES):
            continue
        if not ("Dockerfile" in name or rel.endswith(SCANNED_SUFFIXES)):
            continue
        path = ROOT / rel
        if not path.is_file():
            continue
        for tag in IMAGE_REF.findall(
            path.read_text(encoding="utf-8", errors="replace")
        ):
            refs.append((rel, tag))
    dockerfiles = {rel for rel, _ in refs if "Dockerfile" in rel}
    assert len(dockerfiles) >= 4, f"the scan found too few Python base images: {refs}"
    wrong = [
        (rel, tag)
        for rel, tag in refs
        if not _pinned_image(tag) and f"python:{tag}" != PENDING_OWNER.get(rel)
    ]
    assert not wrong, (
        f"python images not on {_version()}-slim pinned by digest: {wrong}"
    )


def test_each_pending_owner_image_still_exists_and_is_still_pending() -> None:
    # An entry whose move happened must leave the list; the list never grows silently.
    for rel, image in PENDING_OWNER.items():
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert f"FROM {image}" in text, (
            f"{rel} no longer uses {image}: remove the entry"
        )
