"""The compose resource-limit lint, exercised on the cases a blind audit used.

The first version of the lint shipped into a merge-blocking check with no test
at all, and 10 of 12 mutations to it survived. Every case below is one the audit
either defeated it with or wrongly tripped it with.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

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
    assert lint._unlimited_services(path)[0] == ["app"], value


@pytest.mark.parametrize("value", ["512m", "1.5g", "2G", "1073741824", "256MB", "1b"])
def test_a_real_size_is_a_ceiling(tmp_path, value):
    path = _write(
        tmp_path, f"services:\n  app:\n    image: nginx\n    mem_limit: {value}\n"
    )
    assert lint._unlimited_services(path)[0] == [], value


def test_the_deploy_form_counts_and_is_parsed_the_same_way(tmp_path):
    ok = _write(
        tmp_path,
        "services:\n  app:\n    image: nginx\n    deploy:\n"
        "      resources:\n        limits:\n          memory: 512M\n",
    )
    assert lint._unlimited_services(ok)[0] == []
    zero = tmp_path / "z.yaml"
    zero.write_text(
        "services:\n  app:\n    image: nginx\n    deploy:\n"
        "      resources:\n        limits:\n          memory: '0'\n",
        encoding="utf-8",
    )
    assert lint._unlimited_services(zero)[0] == ["app"]


def test_a_malformed_deploy_is_non_compliant_not_a_crash(tmp_path):
    path = _write(tmp_path, 'services:\n  app:\n    image: nginx\n    deploy: "oops"\n')
    assert lint._unlimited_services(path)[0] == ["app"]


# --- per service, not per file ----------------------------------------------


def test_one_limited_and_one_unlimited_service_names_only_the_unlimited_one(tmp_path):
    path = _write(
        tmp_path,
        "services:\n  ok:\n    image: a\n    mem_limit: 256m\n  hog:\n    image: b\n",
    )
    assert lint._unlimited_services(path)[0] == ["hog"]


def test_yaml_anchors_are_resolved_so_a_shared_ceiling_counts(tmp_path):
    path = _write(
        tmp_path,
        "x-d: &d\n  mem_limit: 256m\nservices:\n"
        "  a:\n    <<: *d\n    image: a\n"
        "  b:\n    <<: *d\n    image: b\n",
    )
    assert lint._unlimited_services(path)[0] == []


# --- skipped with a reason, not reported as missing a ceiling ---------------


def test_an_include_only_fragment_is_skipped_not_failed(tmp_path):
    path = _write(tmp_path, "include:\n  - path: ./other.yaml\n")
    assert lint._unlimited_services(path) == ([], "include-only")


def test_a_service_that_extends_another_file_is_skipped(tmp_path):
    path = _write(
        tmp_path,
        "services:\n  app:\n    extends:\n      file: base.yaml\n      service: base\n",
    )
    assert lint._unlimited_services(path)[0] == []


@pytest.mark.parametrize(
    "body", ["", "services:\n", "services: null\n", "networks: {}\n"]
)
def test_a_file_with_no_services_is_skipped_not_failed(tmp_path, body):
    assert lint._unlimited_services(_write(tmp_path, body)) == ([], "no services")


def test_an_unreadable_file_is_its_own_failure_not_a_missing_ceiling(tmp_path):
    path = _write(tmp_path, "services:\n\tapp:\n  bad: [\n")
    services, reason = lint._unlimited_services(path)
    assert services == [] and reason is not None and reason.startswith("unreadable")


def test_multi_document_yaml_reads_every_document(tmp_path):
    path = _write(
        tmp_path,
        "services:\n  a:\n    image: a\n    mem_limit: 1g\n"
        "---\nservices:\n  b:\n    image: b\n",
    )
    assert lint._unlimited_services(path)[0] == ["b"]


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
