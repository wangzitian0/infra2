"""`.python-version` is the one source of the runtime Python version (#1506).

CI must test the Python that the runtime images run. Before #1506 the version was
written in 19 workflow steps and 4 Dockerfiles. Now each workflow job reads
`.python-version`, and each Python base image must carry the same `X.Y` tag.
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
PYTHON_FROM = re.compile(r"^FROM\s+(?:--platform=\S+\s+)?python:(\S+)", re.MULTILINE)


def _version() -> str:
    return VERSION_FILE.read_text(encoding="utf-8").strip()


def _tracked(*patterns: str) -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "--", *patterns],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    return [ROOT / p for p in out]


def _setup_python_steps():
    for path in _tracked(".github/workflows/*.yml", ".github/workflows/**/*.yml"):
        rel = path.relative_to(ROOT).as_posix()
        if rel.startswith(REUSABLE_TEMPLATES):
            continue
        jobs = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get(
            "jobs"
        ) or {}
        for job_id, job in jobs.items():
            for step in job.get("steps") or []:
                if str(step.get("uses", "")).startswith("actions/setup-python"):
                    yield rel, job_id, step.get("with") or {}


def test_the_version_file_names_one_minor_version() -> None:
    assert re.fullmatch(r"3\.\d+", _version()), _version()


def test_every_workflow_job_reads_the_version_file() -> None:
    steps = list(_setup_python_steps())
    assert len(steps) >= 10, "the scan found too few setup-python steps"
    wrong = [
        (rel, job, inputs)
        for rel, job, inputs in steps
        if "python-version" in inputs
        or inputs.get("python-version-file") != ".python-version"
    ]
    assert not wrong, f"setup-python steps that do not read .python-version: {wrong}"


def test_every_python_base_image_has_the_version_of_the_file() -> None:
    images = []
    for path in _tracked("*Dockerfile*", "**/Dockerfile*"):
        rel = path.relative_to(ROOT).as_posix()
        if rel.startswith(("repos/", "oh-my-code-agent/")) or not path.is_file():
            continue
        for tag in PYTHON_FROM.findall(path.read_text(encoding="utf-8")):
            images.append((rel, tag))
    assert len(images) >= 4, f"the scan found too few Python base images: {images}"
    wrong = [
        (rel, tag)
        for rel, tag in images
        if not re.fullmatch(rf"{re.escape(_version())}-slim@sha256:[0-9a-f]{{64}}", tag)
    ]
    assert not wrong, (
        f"Python base images not on {_version()}-slim pinned by digest: {wrong}"
    )
