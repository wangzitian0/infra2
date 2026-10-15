"""tools/fetch_app_manifests.py: a deploy reads infra2's own copies of the App manifests (#1258).

The offline check proves that the copies match contracts/apps.lock.json. Only --sync and
--verify use the network, and the tests replace their calls."""

from __future__ import annotations

import ast
import hashlib
import http.client
import io
import json
import os
import ssl
import subprocess
import sys
import urllib.error
from pathlib import Path
from unittest.mock import Mock, call

import pytest

from tools import fetch_app_manifests as contracts

ROOT = Path(__file__).resolve().parents[2]
SHA = "c" * 40
BODIES = {"one": b'{"fields": [1]}\n', "two": b'{"fields": [2]}\n'}


def _tree(root: Path, bodies=BODIES) -> Path:
    """Two copies of the App ``alpha`` and a lock that matches them. Return the lock."""
    files = []
    for name, body in bodies.items():
        path = root / f"contracts/alpha/{name}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        sha256 = hashlib.sha256(body).hexdigest()
        rel = path.relative_to(root).as_posix()
        files.append(
            {"app": "alpha", "path": rel, "sha256": sha256, "upstream": f"{name}.json"}
        )
    app = {"commit": "a" * 40, "repository": "owner/alpha", "tag": None}
    lock = root / contracts.LOCK
    lock.write_text(json.dumps({"apps": {"alpha": app}, "files": files}))
    return lock


def _files(root: Path) -> dict[Path, bytes]:
    return {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}


def test_check_passes_on_a_consistent_tree(tmp_path: Path, capsys) -> None:
    _tree(tmp_path)

    assert contracts.check(tmp_path) == []
    assert contracts.main([], root=tmp_path) == 0
    assert "ok (2 files)" in capsys.readouterr().out


def _lock_of(path="contracts/alpha/one.json", head="a" * 40, **file) -> bytes:
    entry = {"app": "alpha", "path": path, "sha256": "0" * 64, "upstream": "x", **file}
    return json.dumps({"apps": {"alpha": {"commit": head}}, "files": [entry]}).encode()


@pytest.mark.parametrize(
    ("path", "content", "problem"),
    [
        ("contracts/alpha/one.json", b'{"fields": [7]}\n', "one.json: sha256 differs"),
        ("contracts/alpha/extra.json", b"{}", "extra.json: not listed in contracts/"),
        (contracts.LOCK, _lock_of("contracts/alpha/gone.json"), "gone.json: missing"),
        (contracts.LOCK, _lock_of("../x.json"), "../x.json: outside contracts/"),
        (contracts.LOCK, _lock_of("/tmp/x.json"), "/tmp/x.json: outside contracts/"),
        (contracts.LOCK, _lock_of(head="g" * 40), "apps.alpha.commit is not 40"),
        (contracts.LOCK, _lock_of(app="beta"), "app 'beta' is not in apps"),
        (contracts.LOCK, _lock_of(upstream="../x"), "upstream is not a relative"),
        (contracts.LOCK, _lock_of(upstream="/x"), "upstream is not a relative"),
        (contracts.LOCK, _lock_of(sha256="A" * 64), "sha256 is not 64 lowercase hex"),
        (contracts.LOCK, _lock_of(path=5), "malformed"),
        (contracts.LOCK, b"{not json", "unreadable"),
        (contracts.LOCK, b'{"files": [{}]}', "malformed"),
        (contracts.LOCK, b'{"apps": []}', "malformed"),
    ],
)
def test_check_reports_each_kind_of_drift(
    tmp_path: Path, path, content, problem
) -> None:
    _tree(tmp_path)
    (tmp_path / path).write_bytes(content)

    assert any(problem in line for line in contracts.check(tmp_path))
    assert contracts.main([], root=tmp_path) == 1


def test_check_reports_an_upper_case_sha256_as_a_format_problem_only(tmp_path) -> None:
    lock = _tree(tmp_path)
    digest = hashlib.sha256(BODIES["one"]).hexdigest()
    lock.write_text(lock.read_text().replace(digest, digest.upper()))
    lines = contracts.check(tmp_path)
    assert len(lines) == 1 and "one.json: sha256 is not 64 lowercase hex" in lines[0]


def test_check_reports_a_symlink_that_hides_json_files(tmp_path: Path) -> None:
    (_tree(tmp_path).parent / "link").symlink_to(tmp_path / "contracts/alpha")
    assert "contracts/link: symlink" in contracts.check(tmp_path)


# agree | one changed upstream byte | copy and lock edited together | truncated copy and upstream
@pytest.mark.parametrize(
    ("copy", "upstream", "bad"),
    [
        ({}, {}, ()),
        ({}, {"one": b"{}"}, ("one",)),
        ({"two": b"{}"}, {}, ("two",)),
        ({"one": b""}, {"one": b""}, ("one",)),
    ],
)
def test_verify_compares_with_the_upstream_bytes(tmp_path, copy, upstream, bad) -> None:
    _tree(tmp_path, {**BODIES, **copy})
    url = "https://raw.githubusercontent.com/owner/alpha/" + "a" * 40
    # a body per exact URL: a wrong repository, commit or path raises KeyError and fails the test
    served = {f"{url}/{n}.json": b for n, b in {**BODIES, **upstream}.items()}
    fetch = served.__getitem__
    got = contracts.check(tmp_path, fetch)
    assert [x.split(":")[0] for x in got] == [f"contracts/alpha/{b}.json" for b in bad]
    assert contracts.check(tmp_path) == []  # the offline check passes in every case
    assert contracts.main(["--verify"], root=tmp_path, fetch=fetch) == (1 if bad else 0)


@pytest.mark.parametrize(
    ("ref", "listing", "tag"),
    [
        ("main", f"{SHA}\trefs/heads/main\n", None),
        # an annotated tag lists the tag object first; the peeled ^{} line names the commit
        ("v2", f"{'d' * 40}\trefs/tags/v2\n{SHA}\trefs/tags/v2^{{}}\n", "v2"),
        ("v3", f"{SHA}\trefs/tags/v3\n", "v3"),
        ("rel/tags/x", f"{SHA}\trefs/heads/rel/tags/x\n", None),  # a branch, not a tag
        (SHA, "", None),
    ],
)
def test_sync_writes_the_copies_and_a_deterministic_lock(
    tmp_path: Path, ref: str, listing: str, tag
) -> None:
    lock_file = _tree(tmp_path)
    ls_remote = Mock(return_value=listing)

    # the fake download returns its own URL, so each copy shows what was asked for
    contracts.sync("alpha", ref, tmp_path, ls_remote, str.encode)

    text = lock_file.read_text()
    app = json.loads(text)["apps"]["alpha"]
    assert app == {"commit": SHA, "repository": "owner/alpha", "tag": tag}
    assert text == json.dumps(json.loads(text), indent=2, sort_keys=True) + "\n"
    url = f"https://raw.githubusercontent.com/owner/alpha/{SHA}/one.json"
    assert (tmp_path / "contracts/alpha/one.json").read_text() == url
    assert contracts.check(tmp_path) == []
    # a full sha needs no lookup; any other ref asks the remote once
    asked = [] if ref == SHA else [call("https://github.com/owner/alpha.git", ref)]
    assert ls_remote.call_args_list == asked
    contracts.sync("alpha", ref, tmp_path, ls_remote, str.encode)
    assert lock_file.read_text() == text  # the same run writes the same bytes


def test_sync_changes_nothing_when_a_download_or_a_ref_fails(tmp_path: Path) -> None:
    _tree(tmp_path)
    before = _files(tmp_path)
    ls_remote = Mock(return_value=f"{SHA}\trefs/heads/main\n")
    second_fails = Mock(side_effect=[b"{}", SystemExit("fetching failed")])

    for app, ref, fetch, message in [
        ("alpha", "main", second_fails, "fetching failed"),
        ("alpha", "main", Mock(return_value=b""), "upstream body is empty"),
        ("alpha", "gone", str.encode, "no tag or branch 'gone'"),
        ("beta", "main", str.encode, "unknown app 'beta'"),
    ]:
        with pytest.raises(SystemExit, match=message):
            contracts.sync(app, ref, tmp_path, ls_remote, fetch)

    assert _files(tmp_path) == before


@pytest.mark.parametrize(
    "path", ["../escape.json", "other/x.json", "contracts/../x.json"]
)
def test_sync_refuses_a_lock_path_outside_contracts(tmp_path: Path, path: str) -> None:
    lock = _tree(tmp_path)
    data = json.loads(lock.read_text())
    data["files"][0]["path"] = path
    lock.write_text(json.dumps(data))
    before = _files(tmp_path)

    with pytest.raises(SystemExit, match="outside contracts/"):
        contracts.sync("alpha", SHA, tmp_path, Mock(), str.encode)

    assert _files(tmp_path) == before
    assert not (tmp_path.parent / "escape.json").exists()


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


def test_the_real_copies_match_the_lock_as_a_stdlib_only_script(tmp_path: Path) -> None:
    """infra-ci runs the script by path with PYTHONSAFEPATH=1. ``-S`` hides site-packages."""
    run = subprocess.run(
        [sys.executable, "-S", str(ROOT / "tools/fetch_app_manifests.py")],
        cwd=tmp_path,
        env={**os.environ, "PYTHONSAFEPATH": "1"},
        capture_output=True,
        text=True,
    )

    assert run.returncode == 0, run.stdout + run.stderr
    assert run.stdout.startswith("app manifest copies: ok")


def test_lock_apps_are_the_directory_names_of_the_external_applications() -> None:
    """``harness status --online`` reads ``apps.<directory name>`` of each App (#1259)."""
    from libs.core.harness.manifest import load_manifest
    from libs.core.harness.status import _contract_lock

    repositories = load_manifest(ROOT / "harness/repos.yaml")["repositories"]
    paths = [r["path"] for r in repositories if r.get("role") == "external-application"]
    apps = json.loads((ROOT / contracts.LOCK).read_text())["apps"]

    assert {Path(path).name for path in paths} == set(apps)
    for path in paths:  # the tag, else the first 8 characters of the commit
        app = apps[Path(path).name]
        assert _contract_lock(ROOT, path) == (app["tag"] or app["commit"][:8])


def test_registered_contract_paths_are_all_in_the_real_lock_and_exist() -> None:
    from libs.security.registry import SERVICES

    paths = {p for service in SERVICES for p in service.manifests}
    copies = {p for p in paths if p.startswith("contracts/")}

    assert copies and copies == set(contracts.origins(ROOT))
    assert [p for p in sorted(copies) if not (ROOT / p).is_file()] == []
    assert [p for p in paths if p.startswith("repos/")] == []


def _imports_app_manifests(source: str) -> bool:
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [a.name for a in node.names] + [getattr(node, "module", None) or ""]
            if any("app_manifests" in name.split(".") for name in names):
                return True
    return False


def test_no_tracked_file_under_libs_or_tools_imports_app_manifests() -> None:
    assert _imports_app_manifests("from libs.security import app_manifests")
    assert _imports_app_manifests("import libs.security.app_manifests as m")
    assert not _imports_app_manifests("from tools import fetch_app_manifests")
    listing = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "-z", "--", "libs", "tools"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split("\0")
    files = [n for n in listing if n.endswith(".py") and (ROOT / n).is_file()]

    assert "libs/security/registry.py" in files  # git listed the tree
    assert [n for n in files if _imports_app_manifests((ROOT / n).read_text())] == []
