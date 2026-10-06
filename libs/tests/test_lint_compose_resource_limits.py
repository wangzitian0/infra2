"""The compose resource-limit lint, exercised on the cases a blind audit used.

The first version of the lint shipped into a merge-blocking check with no test
at all, and 10 of 12 mutations to it survived. Every case below is one the audit
either defeated it with or wrongly tripped it with.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from infra2_sdk.rules.compose import inspect_compose

_SPEC = importlib.util.spec_from_file_location(
    "lint_compose_resource_limits",
    Path(__file__).resolve().parent.parent.parent
    / "tools"
    / "lint_compose_resource_limits.py",
)
lint = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(lint)


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "compose.yaml"
    path.write_text(body, encoding="utf-8")
    return path


# --- values: Docker reads 0 as unlimited, so presence is not a ceiling -------


@pytest.mark.parametrize(
    "value",
    ["0", '"0"', "0b", '""', "null", '"please do not limit me"', "false", "-1"],
)
def test_a_value_that_does_not_cap_memory_is_not_a_ceiling(tmp_path, value):
    path = _write(
        tmp_path, f"services:\n  app:\n    image: nginx\n    mem_limit: {value}\n"
    )
    assert list(inspect_compose(path).unlimited_services) == ["app"], value


@pytest.mark.parametrize("value", ["512m", "1.5g", "2G", "1073741824", "256MB", "1b"])
def test_a_real_size_is_a_ceiling(tmp_path, value):
    path = _write(
        tmp_path, f"services:\n  app:\n    image: nginx\n    mem_limit: {value}\n"
    )
    assert list(inspect_compose(path).unlimited_services) == [], value


def test_the_deploy_form_is_not_a_ceiling_because_the_ssot_does_not_name_it(
    tmp_path_factory,
):
    # ops.standards.md §5 names compose fields -- mem_limit / mem_reservation /
    # cpu_shares -- and never `deploy.resources.limits.memory`. Accepting a
    # spelling the standard does not describe, and recommending it in the
    # failure message, would teach contributors to write something the SSOT
    # does not sanction. No service in the tree relies on it.
    path = _write(
        tmp_path_factory.mktemp("d"),
        "services:\n  app:\n    image: nginx\n"
        "    deploy:\n      resources:\n        limits:\n          memory: 512M\n",
    )
    assert list(inspect_compose(path).unlimited_services) == ["app"]


def test_a_malformed_deploy_is_non_compliant_not_a_crash(tmp_path):
    path = _write(tmp_path, 'services:\n  app:\n    image: nginx\n    deploy: "oops"\n')
    assert list(inspect_compose(path).unlimited_services) == ["app"]


# --- per service, not per file ----------------------------------------------


def test_one_limited_and_one_unlimited_service_names_only_the_unlimited_one(tmp_path):
    path = _write(
        tmp_path,
        "services:\n  ok:\n    image: a\n    mem_limit: 256m\n  hog:\n    image: b\n",
    )
    assert list(inspect_compose(path).unlimited_services) == ["hog"]


def test_yaml_anchors_are_resolved_so_a_shared_ceiling_counts(tmp_path):
    path = _write(
        tmp_path,
        "x-d: &d\n  mem_limit: 256m\nservices:\n"
        "  a:\n    <<: *d\n    image: a\n"
        "  b:\n    <<: *d\n    image: b\n",
    )
    assert list(inspect_compose(path).unlimited_services) == []


# --- skipped with a reason, not reported as missing a ceiling ---------------


def test_an_include_only_fragment_is_skipped_not_failed(tmp_path):
    path = _write(tmp_path, "include:\n  - path: ./other.yaml\n")
    report = inspect_compose(path)
    assert list(report.unlimited_services) == []
    assert not report.errors


def test_a_service_that_extends_another_file_is_skipped(tmp_path):
    path = _write(
        tmp_path,
        "services:\n  app:\n    extends:\n      file: base.yaml\n      service: base\n",
    )
    assert list(inspect_compose(path).unlimited_services) == []


@pytest.mark.parametrize(
    "body", ["", "services:\n", "services: null\n", "networks: {}\n"]
)
def test_a_file_with_no_services_is_skipped_not_failed(tmp_path, body):
    report = inspect_compose(_write(tmp_path, body))
    assert list(report.unlimited_services) == []
    assert not report.errors


def test_an_unreadable_file_is_its_own_failure_not_a_missing_ceiling(tmp_path):
    path = _write(tmp_path, "services:\n\tapp:\n  bad: [\n")
    report = inspect_compose(path)
    assert list(report.unlimited_services) == []
    assert len(report.errors) > 0 and "YAML parse error" in report.errors[0]


def test_multi_document_yaml_reads_every_document(tmp_path):
    path = _write(
        tmp_path,
        "services:\n  a:\n    image: a\n    mem_limit: 1g\n"
        "---\nservices:\n  b:\n    image: b\n",
    )
    assert list(inspect_compose(path).unlimited_services) == ["b"]


# --- discovery ---------------------------------------------------------------


def test_every_compose_spelling_is_discovered():
    # The first version scanned only */compose.yaml, so a docker-compose.yml or
    # a repo-root compose.yaml was invisible.
    assert set(lint.COMPOSE_NAMES) == {
        "compose.yaml",
        "compose.yml",
        "docker-compose.yaml",
        "docker-compose.yml",
    }


def test_the_real_repository_is_compliant_against_its_baseline():
    # The end-to-end contract: whatever the tree holds, main must be green.
    assert lint.main() == 0
