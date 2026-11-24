"""#1822: CI runs the real ``verify_app_manifests`` on each App stack, not a stub.

``test_deploy_v2.py`` stubs the verifier to ``{}``, so release v3.1.148 reached staging with
``CONFIGURATION_SHA256: missing``. These tests call the real verifier. The release manifests
come from the committed stack copies (a copy is the projection of the release manifests), so
the tests need no network.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml

from common.deploy import app_manifest_verify as verify
from common.deploy.app_contract import APP_UPSTREAM_MANIFESTS, service_stack_manifest

ROOT = Path(__file__).resolve().parents[2]
TAGS = {"finance_report/app": "v0.2.95", "truealpha/app": "v0.2.139"}
SETS = {"pinned": TAGS, "production": TAGS}


def _fetcher(url: str) -> bytes:
    for entries in APP_UPSTREAM_MANIFESTS.values():
        for service_id, path in entries:
            if url.endswith("/" + path):
                return (ROOT / service_stack_manifest(service_id)).read_bytes()
    raise SystemExit(f"404 {url}")


def _reader(*, drop: str = ""):
    def read(_ref: str, path: str) -> str:
        text = (ROOT / path).read_text(encoding="utf-8")
        if drop and path.endswith("env.manifest.json"):
            data = json.loads(text)
            data["fields"] = [f for f in data["fields"] if f["env"] != drop]
            text = json.dumps(data)
        return text

    return read


def test_the_real_verifier_passes_the_committed_copies() -> None:
    assert (
        verify.verify_all(SETS, root=ROOT, fetcher=_fetcher, contract_reader=_reader())
        == []
    )


def test_a_manifest_copy_without_configuration_sha256_fails_all_three_stacks() -> None:
    """The v3.1.148 escape: the stack copy lacks a field that the release requires."""
    problems = verify.verify_all(
        {"pinned": TAGS},
        root=ROOT,
        fetcher=_fetcher,
        contract_reader=_reader(drop="CONFIGURATION_SHA256"),
    )
    for service in verify.VERIFIED_SERVICES:
        assert any(
            p.startswith(f"pinned {service}@") and "CONFIGURATION_SHA256: missing" in p
            for p in problems
        ), service


def test_each_service_runs_with_staging_and_prod_never_production(monkeypatch) -> None:
    calls: list[tuple[str, str, str, str]] = []

    def spy(service, **kw):
        calls.append((service, kw["source_sha"], kw["deploy_type"], kw["iac_ref"]))
        return {service: "template"}

    assert verify.verify_all(SETS, verifier=spy, fetcher=_fetcher) == []
    assert sorted(calls) == sorted(
        (svc, TAGS[svc.split("/")[0] + "/app"], dt, "HEAD")
        for svc in verify.VERIFIED_SERVICES
        for dt in ("staging", "prod")
    )


def test_pinned_and_production_tags_are_both_verified() -> None:
    seen: list[str] = []

    def spy(service, **kw):
        seen.append(kw["source_sha"])
        return {service: "t"}

    sets = {
        "pinned": TAGS,
        "production": {"finance_report/app": "v0.2.80", "truealpha/app": "v0.2.120"},
    }
    assert verify.verify_all(sets, verifier=spy, fetcher=_fetcher) == []
    assert {"v0.2.95", "v0.2.139", "v0.2.80", "v0.2.120"} <= set(seen)


def test_a_missing_tag_and_an_empty_render_are_problems() -> None:
    problems = verify.verify_all(
        {"production": {"finance_report/app": "v0.2.80"}},
        verifier=lambda service, **kw: {},
        fetcher=_fetcher,
    )
    assert "production truealpha/app: no App release tag" in problems
    assert any("the verifier rendered no template" in p for p in problems)


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=root,
        check=True,
        capture_output=True,
    )


def _repo_with_marker(tmp_path: Path, tag: str = "v0.2.80") -> Path:
    (tmp_path / "harness").mkdir()
    (tmp_path / "harness/repos.yaml").write_text(
        "repositories:\n  - id: truealpha\n    role: external-application\n"
        f"    release_tag: {tag}\n",
        encoding="utf-8",
    )
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "x")
    return tmp_path


def test_production_tags_come_from_repos_yaml_at_the_newest_marker(tmp_path) -> None:
    root = _repo_with_marker(tmp_path, "v0.2.80")
    _git(root, "tag", "production/v3.1.9")
    (root / "harness/repos.yaml").write_text(
        (root / "harness/repos.yaml").read_text().replace("v0.2.80", "v0.2.99")
    )
    _git(root, "commit", "-qam", "bump")
    _git(root, "tag", "production/v3.1.10")
    assert verify.production_app_tags(root) == {"truealpha/app": "v0.2.99"}
    _git(root, "tag", "-d", "production/v3.1.10")
    assert verify.production_app_tags(root) == {"truealpha/app": "v0.2.80"}


def test_no_production_marker_is_an_error_not_an_empty_set(tmp_path) -> None:
    root = _repo_with_marker(tmp_path)
    with pytest.raises(ValueError, match="no production/v"):
        verify.production_app_tags(root)


def test_main_fails_when_production_tags_are_unreadable(tmp_path, capsys) -> None:
    root = _repo_with_marker(tmp_path)
    assert verify.main([], root=root) == 1
    assert "no production/v" in capsys.readouterr().out


def test_infra_ci_runs_the_check_in_shard_one_after_fetching_the_markers() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/infra-ci.yml").read_text())
    steps = workflow["jobs"]["test-unit-shard"]["steps"]
    names = [s.get("name", "") for s in steps]
    fetch = next(s for s in steps if s.get("name") == "Fetch the production markers")
    run = next(s for s in steps if s.get("name") == "Run infra unit tests")
    assert fetch["if"] == "matrix.shard == 1"
    assert "refs/tags/production/*" in fetch["run"]
    assert names.index(fetch["name"]) < names.index(run["name"])
    script = run["run"]
    assert (
        script.index('if [[ "${{ matrix.shard }}" == "1" ]]')
        < script.index("tools/app_manifest_verify.py")
        < script.index("fi\n", script.index("tools/app_manifest_verify.py"))
    )


def test_a_system_exit_of_the_fetch_is_a_problem_not_a_crash() -> None:
    def dead(_url: str) -> bytes:
        raise SystemExit("fetching failed: HTTP Error 503")

    problems = verify.verify_all({"pinned": TAGS}, root=ROOT, fetcher=dead)
    assert len(problems) == 6 and all("503" in p for p in problems)


def test_the_check_covers_exactly_the_three_stacks_and_two_deploy_types() -> None:
    assert verify.VERIFIED_SERVICES == (
        "finance_report/app",
        "truealpha/app",
        "truealpha/data_engine",
    )
    assert verify.VERIFIED_DEPLOY_TYPES == ("staging", "prod")
