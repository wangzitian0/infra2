"""tools/fetch_app_manifests.py: guard against vendored App manifests in contracts/ (#1320).

App environment manifests are fetched from the deployed release commit at deploy time to
render the Vault Agent template. This guard fails if any vendored manifest or lock file
reappears under ``contracts/`` or if ``SERVICES`` references ``contracts/`` or ``repos/``.
"""

from __future__ import annotations

import ast
import http.client
import io
import os
import ssl
import subprocess
import sys
import urllib.error
from pathlib import Path

import pytest

from common.core import fetch_app_manifests as contracts
from common.security.registry import SERVICES, Service

ROOT = Path(__file__).resolve().parents[2]


def test_real_checkout_has_no_vendored_app_manifests() -> None:
    """Acceptance Criterion 2 (#1320): contracts/truealpha/, contracts/finance_report/ and
    contracts/apps.lock.json do not exist, and check() passes on the repository root."""
    assert not (ROOT / "contracts" / "truealpha").exists()
    assert not (ROOT / "contracts" / "finance_report").exists()
    assert not (ROOT / "contracts" / "apps.lock.json").exists()
    assert contracts.check(ROOT) == []


def test_check_passes_on_clean_tree(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert contracts.check(tmp_path) == []
    assert contracts.main([], root=tmp_path) == 0
    assert "app manifest guard: ok" in capsys.readouterr().out
    assert contracts.main(["--check"], root=tmp_path) == 0


@pytest.mark.parametrize(
    "rel_path",
    [
        "contracts/truealpha/data-engine.required-env.generated.json",
        "contracts/truealpha/app-web.required-env.manifest.json",
        "contracts/truealpha/llm-service.required-env.generated.json",
        "contracts/finance_report/required-env.generated.json",
        "contracts/apps.lock.json",
    ],
)
def test_check_fails_when_a_vendored_app_manifest_or_lock_reappears(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], rel_path: str
) -> None:
    """Falsifiability proof (#1320): re-introducing any vendored manifest or lock fails."""
    target = tmp_path / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text('{"fields": []}\n', encoding="utf-8")

    problems = contracts.check(tmp_path)
    assert len(problems) == 1
    assert rel_path in problems[0]
    assert "vendored app manifest is forbidden (#1320)" in problems[0]
    assert contracts.main([], root=tmp_path) == 1
    assert rel_path in capsys.readouterr().err


def test_check_fails_on_symlinks_in_contracts_or_registered_manifests(
    tmp_path: Path,
) -> None:
    target_dir = tmp_path / "other"
    target_dir.mkdir()
    contracts_link = tmp_path / "contracts"
    contracts_link.symlink_to(target_dir)
    assert contracts.check(tmp_path) == ["contracts: symlink is forbidden (#1320)"]
    contracts_link.unlink()

    contracts_dir = tmp_path / "contracts"
    contracts_dir.mkdir()
    nested_link = contracts_dir / "truealpha"
    nested_link.symlink_to(target_dir)
    assert contracts.check(tmp_path) == [
        "contracts/truealpha: symlink is forbidden (#1320)"
    ]
    nested_link.unlink()

    manifest_link = tmp_path / "truealpha/truealpha/10.app/env.manifest.json"
    manifest_link.parent.mkdir(parents=True, exist_ok=True)
    real_file = tmp_path / "real.json"
    real_file.write_text("{}", encoding="utf-8")
    manifest_link.symlink_to(real_file)
    problems = contracts.check(tmp_path)
    assert (
        "truealpha/truealpha/10.app/env.manifest.json: symlink is forbidden (#1320)"
        in problems
    )


@pytest.mark.parametrize(
    "bad_manifest",
    [
        "contracts/truealpha/data-engine.required-env.generated.json",
        "repos/truealpha/apps/data-engine/required-env.generated.json",
    ],
)
def test_check_fails_when_a_service_references_contracts_or_repos(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad_manifest: str
) -> None:
    bad_service = Service(
        "truealpha/truealpha/20.data_engine",
        "truealpha",
        "data_engine",
        (bad_manifest,),
    )
    monkeypatch.setattr(contracts, "SERVICES", (bad_service,))
    problems = contracts.check(tmp_path)
    assert problems == [
        f"truealpha/data_engine: {bad_manifest} references forbidden path (#1320)"
    ]


def test_sync_flag_is_rejected_with_explanation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = contracts.main(["--sync", "truealpha", "main"], root=tmp_path)
    assert rc == 2
    assert "removed in #1320" in capsys.readouterr().err


def _http(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://x", code, "error", {}, None)  # type: ignore[arg-type]


class _Dies(io.BytesIO):
    """A response whose body dies midway."""

    def read(self, *args):
        raise http.client.IncompleteRead(b"x")


@pytest.mark.parametrize(
    ("steps", "calls", "waits", "body"),
    [
        ([_http(503)], 3, [2, 4], None),  # 5xx: 2 retries, then an error
        ([_http(404)], 1, [], None),  # 404: an error at once
        ([ConnectionResetError(104, "reset")], 3, [2, 4], None),  # a bare OSError
        ([_Dies], 3, [2, 4], None),  # a body that dies midway
        ([urllib.error.URLError(ssl.SSLCertVerificationError())], 1, [], None),
        ([urllib.error.URLError(ConnectionResetError()), b"ok"], 2, [2], b"ok"),
        ([_http(429), _http(408), b"ok"], 3, [2, 4], b"ok"),
    ],
)
def test_the_retry_rule(steps, calls, waits, body) -> None:
    played, waited = [], []

    def opener(url: str, timeout: int):
        step = steps[min(len(played), len(steps) - 1)]
        played.append(url)
        if isinstance(step, Exception):
            raise step
        return step() if isinstance(step, type) else io.BytesIO(step)

    def get() -> bytes:
        return contracts.http_get("https://x", sleep=waited.append, opener=opener)

    if body is None:
        with pytest.raises(SystemExit, match="fetching https://x failed"):
            get()
    else:
        assert get() == body
    assert (len(played), waited) == (calls, waits)


def test_guard_runs_as_a_stdlib_only_script(tmp_path: Path) -> None:
    """infra-ci runs the script by path with PYTHONSAFEPATH=1. ``-S`` hides site-packages."""
    run = subprocess.run(
        [sys.executable, "-S", str(ROOT / "tools/fetch_app_manifests.py")],
        cwd=tmp_path,
        env={**os.environ, "PYTHONSAFEPATH": "1"},
        capture_output=True,
        text=True,
    )

    assert run.returncode == 0, run.stdout + run.stderr
    assert run.stdout.startswith("app manifest guard: ok")


def test_registered_manifest_paths_are_stack_local_and_exist() -> None:
    paths = {p for service in SERVICES for p in service.manifests}
    assert [p for p in paths if p.startswith("contracts/")] == []
    assert [p for p in paths if p.startswith("repos/")] == []
    assert [p for p in sorted(paths) if not (ROOT / p).is_file()] == []


def _imports_app_manifests(source: str) -> bool:
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [a.name for a in node.names] + [getattr(node, "module", None) or ""]
            if any("app_manifests" in name.split(".") for name in names):
                return True
    return False


def test_no_tracked_file_under_libs_or_tools_imports_app_manifests() -> None:
    assert _imports_app_manifests("from common.security import app_manifests")
    assert _imports_app_manifests("import common.security.app_manifests as m")
    assert not _imports_app_manifests("from tools import fetch_app_manifests")
    listing = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "-z", "--", "common", "tools"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split("\0")
    files = [n for n in listing if n.endswith(".py") and (ROOT / n).is_file()]

    assert "common/security/registry.py" in files  # git listed the tree
    assert [n for n in files if _imports_app_manifests((ROOT / n).read_text())] == []
