from __future__ import annotations

import http.client
import io
import json
import re
import socket
import subprocess
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from common.deploy.release_markers import MarkerStatus
from common.core.harness.status import (
    RepositoryStatus,
    WorkspaceStatus,
    fetch_json,
    observe_online,
    render_online_table,
    repository_status,
    served_release,
    workspace_status,
)
from common.tests.test_harness_manifest import _workspace


def test_workspace_status_exposes_pin_remote_release_and_drift(tmp_path: Path) -> None:
    app = tmp_path / "repos" / "app"
    app.mkdir(parents=True)
    root_sha = "a" * 40
    app_sha = "b" * 40
    remote_app_sha = "c" * 40
    manifest = {
        "repositories": [
            {
                "id": "root",
                "path": ".",
                "checkout": "root",
                "release_identity": "tag",
            },
            {
                "id": "app",
                "path": "repos/app",
                "checkout": "submodule",
                "release_identity": "image",
            },
        ]
    }

    def runner(argv, **_kwargs):
        checkout = Path(argv[2])
        args = argv[3:]
        is_app = checkout == app
        if args == ["rev-parse", "--is-inside-work-tree"]:
            output = "true\n"
        elif args == ["rev-parse", "--show-toplevel"]:
            output = str(checkout) + "\n"
        elif args == ["fetch", "--prune", "--tags", "origin"]:
            output = ""
        elif args == ["rev-parse", "HEAD^{commit}"]:
            output = f"{app_sha if is_app else root_sha}\n"
        elif args == ["symbolic-ref", "--quiet", "--short", "HEAD"]:
            return (
                subprocess.CompletedProcess(argv, 1, "", "")
                if is_app
                else subprocess.CompletedProcess(argv, 0, "main\n", "")
            )
        elif args[:4] == [
            "rev-parse",
            "--abbrev-ref",
            "--symbolic-full-name",
            "@{upstream}",
        ]:
            output = "origin/main\n"
        elif args == ["rev-parse", "origin/main^{commit}"]:
            output = f"{remote_app_sha if is_app else root_sha}\n"
        elif args[:3] == ["rev-list", "--left-right", "--count"]:
            output = "0 1\n" if is_app else "0 0\n"
        elif args == ["status", "--porcelain"]:
            output = " M local.txt\n" if is_app else ""
        elif args == ["describe", "--tags", "--always"]:
            output = "v2.0.0\n" if is_app else "v1.0.0\n"
        elif args == ["ls-tree", "HEAD", "--", "repos/app"]:
            output = f"160000 commit {app_sha}\trepos/app\n"
        else:
            raise AssertionError(f"unexpected git call: {argv}")
        return subprocess.CompletedProcess(argv, 0, output, "")

    result = workspace_status(tmp_path, manifest, fetch=True, runner=runner)

    root, app_status = result.repositories
    assert root.current is True
    assert app_status.parent_pin == app_sha
    assert app_status.pin_matches is True
    assert app_status.remote_head == remote_app_sha
    assert app_status.behind == 1
    assert app_status.dirty_paths == 1
    assert app_status.checkout_release == "v2.0.0"
    assert app_status.current is False
    assert result.ok is True
    assert result.current is False


def test_workspace_status_never_reports_clean_when_git_status_fails(
    tmp_path: Path,
) -> None:
    manifest = {
        "repositories": [
            {
                "id": "root",
                "path": ".",
                "checkout": "root",
                "release_identity": "tag",
            }
        ]
    }

    def runner(argv, **_kwargs):
        args = argv[3:]
        values = {
            ("rev-parse", "--is-inside-work-tree"): "true\n",
            ("rev-parse", "--show-toplevel"): str(tmp_path) + "\n",
            ("rev-parse", "HEAD^{commit}"): "a" * 40 + "\n",
            ("symbolic-ref", "--quiet", "--short", "HEAD"): "main\n",
            (
                "rev-parse",
                "--abbrev-ref",
                "--symbolic-full-name",
                "@{upstream}",
            ): "origin/main\n",
            ("rev-parse", "origin/main^{commit}"): "a" * 40 + "\n",
            (
                "rev-list",
                "--left-right",
                "--count",
                "HEAD...origin/main",
            ): "0 0\n",
            ("describe", "--tags", "--always"): "v1.0.0\n",
        }
        if args == ["status", "--porcelain"]:
            return subprocess.CompletedProcess(argv, 128, "", "index unavailable")
        return subprocess.CompletedProcess(argv, 0, values[tuple(args)], "")

    result = workspace_status(tmp_path, manifest, runner=runner)
    status = result.repositories[0]

    assert status.dirty_paths is None
    assert "working-tree status failed" in status.error
    assert status.current is False
    assert result.ok is False


@pytest.mark.parametrize(
    ("option", "expected"), [(None, True), ("--no-submodules-expected", False)]
)
def test_status_passes_submodule_expectation_to_manifest_validation(
    monkeypatch, tmp_path: Path, option: str | None, expected: bool
) -> None:
    import common.core.harness_cli as harness

    observed = []

    def check_workspace(_root, _manifest, *, submodules_expected=True):
        observed.append(submodules_expected)
        return SimpleNamespace(ok=True)

    monkeypatch.setattr(harness, "check_workspace", check_workspace)
    monkeypatch.setattr(harness, "load_manifest", lambda _path: {"repositories": []})
    monkeypatch.setattr(
        harness,
        "workspace_status",
        lambda *_args, **_kwargs: SimpleNamespace(ok=True, current=False),
    )
    monkeypatch.setattr(harness, "_print_status", lambda _result: None)
    argv = ["status", "--root", str(tmp_path)]
    if option:
        argv.append(option)

    assert harness.main(argv) == 0
    assert observed == [expected]


def test_empty_optional_submodule_is_not_reported_as_its_parent(tmp_path: Path) -> None:
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    (tmp_path / "tooling").mkdir()
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv[3:])
        return subprocess.run(argv, **kwargs)

    result = repository_status(
        tmp_path,
        {
            "id": "tooling",
            "path": "tooling",
            "checkout": "submodule",
            "release_identity": "tag",
        },
        fetch=True,
        runner=runner,
    )
    assert not result.initialized
    assert result.checkout_head is None
    assert str(tmp_path.resolve()) in result.error
    assert "git submodule update --init" in result.error
    assert not any(call[0] == "fetch" for call in calls)


def test_repository_status_contract_snapshot_allows_pin_drift() -> None:
    pinned = RepositoryStatus(
        repository_id="pinned-repo",
        path="repos/pinned",
        release_identity="tag",
        initialized=True,
        remote_ref="origin/main",
        remote_head="a" * 40,
        ahead=0,
        behind=0,
        dirty_paths=0,
        pin_matches=False,
        contract="pinned",
    )
    assert pinned.contract == "pinned"
    assert pinned.current is False

    snapshot = RepositoryStatus(
        repository_id="app",
        path="repos/app",
        release_identity="image",
        initialized=True,
        remote_ref="origin/main",
        remote_head="a" * 40,
        ahead=0,
        behind=0,
        dirty_paths=0,
        pin_matches=False,
        contract="snapshot",
    )
    assert snapshot.contract == "snapshot"
    assert snapshot.current is True


def test_workspace_status_propagates_contract_and_respects_snapshot_pin_drift(
    tmp_path: Path,
) -> None:
    app = tmp_path / "repos" / "app"
    app.mkdir(parents=True)
    root_sha = "a" * 40
    app_sha = "b" * 40
    remote_app_sha = "b" * 40
    parent_pin_sha = "c" * 40
    manifest = {
        "repositories": [
            {
                "id": "root",
                "path": ".",
                "checkout": "root",
                "release_identity": "tag",
                "contract": "pinned",
            },
            {
                "id": "app",
                "path": "repos/app",
                "checkout": "submodule",
                "release_identity": "image",
                "contract": "snapshot",
            },
        ]
    }

    def runner(argv, **_kwargs):
        checkout = Path(argv[2])
        args = argv[3:]
        is_app = checkout == app
        if args == ["rev-parse", "--is-inside-work-tree"]:
            return subprocess.CompletedProcess(argv, 0, "true\n", "")
        if args == ["rev-parse", "--show-toplevel"]:
            return subprocess.CompletedProcess(argv, 0, str(checkout) + "\n", "")
        if args == ["rev-parse", "HEAD^{commit}"]:
            return subprocess.CompletedProcess(
                argv, 0, f"{app_sha if is_app else root_sha}\n", ""
            )
        if args == ["symbolic-ref", "--quiet", "--short", "HEAD"]:
            return subprocess.CompletedProcess(argv, 0, "main\n", "")
        if args[:4] == [
            "rev-parse",
            "--abbrev-ref",
            "--symbolic-full-name",
            "@{upstream}",
        ]:
            return subprocess.CompletedProcess(argv, 0, "origin/main\n", "")
        if args == ["rev-parse", "origin/main^{commit}"]:
            return subprocess.CompletedProcess(
                argv, 0, f"{remote_app_sha if is_app else root_sha}\n", ""
            )
        if args[:3] == ["rev-list", "--left-right", "--count"]:
            return subprocess.CompletedProcess(argv, 0, "0 0\n", "")
        if args == ["status", "--porcelain"]:
            return subprocess.CompletedProcess(argv, 0, "", "")
        if args == ["describe", "--tags", "--always"]:
            return subprocess.CompletedProcess(argv, 0, "v1.0.0\n", "")
        if args == ["ls-tree", "HEAD", "--", "repos/app"]:
            return subprocess.CompletedProcess(
                argv, 0, f"160000 commit {parent_pin_sha}\trepos/app\n", ""
            )
        raise AssertionError(f"unexpected git call: {argv}")

    result = workspace_status(tmp_path, manifest, runner=runner)
    root_status, app_status = result.repositories

    assert root_status.contract == "pinned"
    assert root_status.current is True

    assert app_status.contract == "snapshot"
    assert app_status.parent_pin == parent_pin_sha
    assert app_status.checkout_head == app_sha
    assert app_status.pin_matches is False
    assert app_status.current is True
    assert result.ok is True
    assert result.current is True


# --- `harness status --online`: the release each App serves -----------------------------

URL = "https://app.example.test/api/health"
BEHIND = MarkerStatus("v1.2.35", "v1.2.38", 3)
ONE_BEHIND = MarkerStatus("v1.2.37", "v1.2.38", 1)
HTTP_503 = urllib.error.HTTPError(URL, 503, "Unavailable", {}, None)
GIT_ERROR = subprocess.CalledProcessError(128, "git")


def _url(app: str, environment: str) -> str:
    return f"https://{app}-{environment}.example.test/api/health"


BODIES = {  # one distinct release per URL: a swapped cell or row changes the output
    _url("finance-report", "staging"): {"version": "v0.2.2", "git_sha": "v0.2.2"},
    _url("finance-report", "production"): {"version": "v0.2.1", "git_sha": "x"},
    _url("truealpha", "staging"): {"git_sha": "v0.1.16", "status": "ok"},
    _url("truealpha", "production"): {"git_sha": "v0.1.12", "status": "ok"},
}


class FakeFetcher:
    """Stands in for the network: maps a URL to a JSON body, or to an exception to raise."""

    def __init__(self, outcomes: dict) -> None:
        self.outcomes = outcomes
        self.calls: list[tuple[str, float]] = []

    def __call__(self, url: str, timeout: float):
        self.calls.append((url, timeout))
        if isinstance(self.outcomes[url], BaseException):
            raise self.outcomes[url]
        return self.outcomes[url]


def _markers(outcome):
    """A fake `marker_status` that returns `outcome`, or raises it, and records its roots."""

    def marker_status(*, repo_root):
        marker_status.roots.append(repo_root)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    marker_status.roots = []
    return marker_status


def _manifest() -> dict:
    root = {"id": "infra2", "path": ".", "checkout": "root"}
    sdk = {"id": "infra2-sdk", "path": "repos/infra2-sdk", "checkout": "submodule"}
    apps = [
        {
            **sdk,
            "id": name,
            "path": "repos/" + name.replace("-", "_"),
            "health": {env: _url(name, env) for env in ("staging", "production")},
        }
        for name in ("finance-report", "truealpha")
    ]
    return {"repositories": [root, sdk, *apps]}


def _status() -> WorkspaceStatus:
    def repo(repository_id: str, path: str, head: str, pin: str | None = None):
        pin_fields = (
            {"parent_pin": pin + "0" * 32, "pin_matches": pin == head} if pin else {}
        )
        return RepositoryStatus(
            repository_id,
            path,
            "tag",
            True,
            checkout_head=head + "0" * 32,
            ahead=0,
            behind=0,
            dirty_paths=0,
            contract="snapshot",
            **pin_fields,
        )

    return WorkspaceStatus(
        (
            repo("infra2", ".", "ed5ced96"),
            repo("infra2-sdk", "repos/infra2-sdk", "7cfe2b10", "7cfe2b10"),
            repo("finance-report", "repos/finance_report", "75149f66", "c3fde359"),
            repo("truealpha", "repos/truealpha", "d2c95f40", "0203431d"),
        )
    )


def _online(root: Path, outcomes: dict = BODIES, marker_status=None) -> WorkspaceStatus:
    return observe_online(
        _status(),
        root,
        _manifest(),
        fetcher=FakeFetcher(outcomes),
        marker_status=marker_status or _markers(BEHIND),
        now=lambda: datetime.fromisoformat("2026-10-15T13:41:53+08:00"),  # 05:41:53 UTC
    )


def _write_lock(root: Path, content: str) -> None:
    (root / "contracts").mkdir(exist_ok=True)
    (root / "contracts" / "apps.lock.json").write_text(content, encoding="utf-8")


NO_FIELD = ("unknown", "no version or git_sha")
RELEASE_CASES = [  # (what the fetcher returns or raises, (release, note))
    ({"version": "v0.2.0", "git_sha": "v0.1.0"}, ("v0.2.0", "")),
    ({"git_sha": " v0.1.16\n", "status": "ok"}, ("v0.1.16", "")),
    ({"version": None, "git_sha": "v0.1.16"}, ("v0.1.16", "")),
    ({"version": " ", "git_sha": "v0.1.16"}, ("v0.1.16", "")),
    ({"status": "ok"}, NO_FIELD),
    ({"version": 7}, ("unknown", "non-string version")),
    ({"version": 7, "git_sha": "v0.1.16"}, ("unknown", "non-string version")),
    (["v0.2.0"], NO_FIELD),
    ({"version": "v1\x1b[31m"}, ("unknown", "unprintable version")),
    (json.JSONDecodeError("bad", "<html>", 0), ("unknown", "JSONDecodeError")),
    (urllib.error.URLError("refused"), ("unreachable", "URLError")),
    (HTTP_503, ("unreachable", "HTTPError")),
    (TimeoutError("timed out"), ("unreachable", "TimeoutError")),
    (http.client.IncompleteRead(b"{"), ("unreachable", "IncompleteRead")),
]
RELEASE_IDS = "version-wins git-sha-only version-null version-blank no-field version-int version-int-no-fallback list escape not-json refused http-503 timeout cut-off".split()


@pytest.mark.parametrize(("outcome", "expected"), RELEASE_CASES, ids=RELEASE_IDS)
def test_served_release(outcome: object, expected: tuple[str, str]) -> None:
    fetcher = FakeFetcher({URL: outcome})

    assert served_release(URL, fetcher) == expected
    assert fetcher.calls == [(URL, 8.0)]


MARKER_CASES = [  # (what marker_status returns or raises, (staging, production, note))
    (BEHIND, ("v1.2.38", "v1.2.35", "marker 3 releases behind")),
    (ONE_BEHIND, ("v1.2.38", "v1.2.37", "marker 1 release behind")),
    (MarkerStatus("v1.2.38", "v1.2.38", 0), ("v1.2.38", "v1.2.38", "marker current")),
    (ValueError("no marker"), ("unknown", "unknown", "markers: ValueError: no marker")),
    (GIT_ERROR, ("unknown", "unknown", f"markers: CalledProcessError: {GIT_ERROR}")),
]


MARKER_IDS = "behind one-behind current no-tags no-repo".split()


@pytest.mark.parametrize(("outcome", "expected"), MARKER_CASES, ids=MARKER_IDS)
def test_infra2_row_comes_from_the_release_markers(
    tmp_path: Path, outcome, expected
) -> None:
    marker_status = _markers(outcome)

    served = _online(tmp_path, marker_status=marker_status).repositories[0].served

    assert (served["staging"], served["production"], served["note"]) == expected
    assert marker_status.roots == [tmp_path]


LOCK = '{"apps": {"finance_report": {"tag": "v0.1.88"}, "truealpha": {"tag": null, "commit": "0203431d00"}}}'
TABLE_ROWS = [
    "repo            pin       checkout  contract(lock)  staging  production",
    "infra2          -         ed5ced96  -               v1.2.38  v1.2.35     (marker 3 releases behind)",
    "infra2-sdk      7cfe2b10  7cfe2b10  -               -        -",
    "finance-report  c3fde359  75149f66  v0.1.88         v0.2.2   v0.2.1",
    "truealpha       0203431d  d2c95f40  0203431d        v0.1.16  v0.1.12",
]


def test_online_table_matches_the_golden_text(tmp_path: Path) -> None:
    _write_lock(tmp_path, LOCK)
    header = "online (observed 2026-10-15T05:41:53Z; infra2 row from local git tags):"

    assert render_online_table(_online(tmp_path)) == [header, *TABLE_ROWS]


TAG_LOCK = '{"apps": {"truealpha": {"tag": %s}}}'


@pytest.mark.parametrize("lock", ["{not json", TAG_LOCK % "5", TAG_LOCK % r'"v1\n"'])
def test_bad_contract_lock_is_unknown(tmp_path: Path, lock: str) -> None:
    _write_lock(tmp_path, lock)

    assert _online(tmp_path).repositories[3].contract_lock == "unknown"


def _no_network(*_args, **_kwargs):
    raise AssertionError("the network was used")


def _patch_cli(monkeypatch, fetcher: FakeFetcher):
    """Point `tools.harness` at fixtures: no git, no manifest file, no network."""
    import common.core.harness_cli as harness

    check = SimpleNamespace(
        ok=True, repository_count=4, errors=(), warnings=(), findings=()
    )
    monkeypatch.setattr(harness, "check_workspace", lambda *_a, **_k: check)
    monkeypatch.setattr(harness, "load_manifest", lambda _path: _manifest())
    monkeypatch.setattr(harness, "workspace_status", lambda *_a, **_k: _status())
    monkeypatch.setattr(harness, "fetch_json", fetcher)
    monkeypatch.setattr(urllib.request, "urlopen", _no_network)
    return harness


@pytest.mark.parametrize("argv", [["status"], ["status", "--json"], ["check"]])
def test_default_commands_open_no_socket(monkeypatch, tmp_path, capsys, argv):
    """The real `main` on a fixture workspace: the real `fetch_json` would fail the test."""
    from common.core.harness_cli import main

    manifest = _workspace(tmp_path)
    manifest["repositories"][1]["health"] = {"production": URL}
    (tmp_path / "harness").mkdir()
    (tmp_path / "harness" / "repos.yaml").write_text(yaml.safe_dump(manifest))
    monkeypatch.setattr(socket, "getaddrinfo", _no_network)
    monkeypatch.setattr(socket.socket, "connect", _no_network)

    main([*argv, "--root", str(tmp_path), "--no-submodules-expected"])

    output = capsys.readouterr().out
    assert output.strip()
    online = r'online \(|observed_at|"served"|contract_lock|contract\(lock\)'
    assert not re.search(online, output)


OFFLINE_LINES = [  # the output of `_print_status` on origin/main (1b2cdb65)
    "harness status: CURRENT",
    "CURRENT infra2             root      ahead=0 behind=0   dirty=0   release=unknown",
    "        checkout=ed5ced960000 parent=root remote=unknown@unknown",
    "CURRENT infra2-sdk         pin=ok    ahead=0 behind=0   dirty=0   release=unknown",
    "        checkout=7cfe2b100000 parent=7cfe2b100000 remote=unknown@unknown",
    "CURRENT finance-report     pin=DRIFT ahead=0 behind=0   dirty=0   release=unknown",
    "        checkout=75149f660000 parent=c3fde3590000 remote=unknown@unknown",
    "CURRENT truealpha          pin=DRIFT ahead=0 behind=0   dirty=0   release=unknown",
    "        checkout=d2c95f400000 parent=0203431d0000 remote=unknown@unknown",
]


def test_offline_status_text_is_unchanged(monkeypatch, tmp_path: Path, capsys) -> None:
    harness = _patch_cli(monkeypatch, FakeFetcher(BODIES))

    harness.main(["status", "--root", str(tmp_path)])

    assert capsys.readouterr().out.splitlines() == OFFLINE_LINES


def test_online_text_has_a_blank_line_and_the_table(monkeypatch, tmp_path, capsys):
    harness = _patch_cli(monkeypatch, FakeFetcher(BODIES))
    monkeypatch.setattr("common.deploy.release_markers.marker_status", _markers(BEHIND))
    _write_lock(tmp_path, LOCK)

    assert harness.main(["status", "--online", "--root", str(tmp_path)]) == 0

    lines = capsys.readouterr().out.splitlines()
    assert lines[:10] == [*OFFLINE_LINES, ""]
    assert re.fullmatch(r"online \(observed [\dT:Z-]+; infra2 row.*\):", lines[10])
    assert lines[11:] == TABLE_ROWS


def test_online_json_shape(monkeypatch, tmp_path: Path, capsys) -> None:
    outcomes = {**BODIES, _url("truealpha", "staging"): TimeoutError("timed out")}
    harness = _patch_cli(monkeypatch, FakeFetcher(outcomes))
    monkeypatch.setattr("common.deploy.release_markers.marker_status", _markers(BEHIND))
    _write_lock(
        tmp_path, '{"apps": {"truealpha": {"tag": null, "commit": "0203431d00"}}}'
    )

    exit_code = harness.main(["status", "--online", "--json", "--root", str(tmp_path)])

    payload = json.loads(capsys.readouterr().out)
    served = {item["repository_id"]: item["served"] for item in payload["repositories"]}
    locks = [item["contract_lock"] for item in payload["repositories"]]
    assert exit_code == 0
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", payload["observed_at"])

    def row(staging, production, note=""):
        return {"staging": staging, "production": production, "note": note}

    assert served == {
        "infra2": row("v1.2.38", "v1.2.35", "marker 3 releases behind"),
        "infra2-sdk": row(None, None),
        "finance-report": row("v0.2.2", "v0.2.1"),
        "truealpha": row("unreachable", "v0.1.12", "staging: TimeoutError"),
    }
    assert locks == [None, None, None, "0203431d"]


def _response(body: bytes, final_url: str = URL) -> io.BytesIO:
    response = io.BytesIO(body)
    response.geturl = lambda: final_url  # urlopen returns the body and the final URL
    return response


HOSTILE = [  # (final URL of the response, body): the body names a release, but fetch_json must refuse it
    ("http://app.example.test/api/health", b'{"version": "v9.9.9"}'),
    ("https://evil.example.test/api/health", b'{"version": "v9.9.9"}'),
    (URL, b'{"version": "v9.9.9"}' + b" " * 1_048_576),  # valid JSON, then padding
    (URL, b"[" * 100_000 + b"]" * 100_000),
]


@pytest.mark.parametrize(
    "url, body",
    HOSTILE,
    ids=["redirect", "cross-host-redirect", "1-mib", "nesting"],
)
def test_fetch_json_refusals_give_unknown(monkeypatch, url, body) -> None:
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _response(body, url))

    assert served_release(URL, fetch_json)[0] == "unknown"


def test_observe_online_fetches_endpoints_concurrently(tmp_path: Path) -> None:
    import threading

    barrier = threading.Barrier(len(BODIES), timeout=2.0)

    def concurrent_fetcher(url: str, timeout: float):
        barrier.wait()  # blocks unless all 4 endpoints are fetched concurrently
        return BODIES[url]

    result = observe_online(
        _status(),
        tmp_path,
        _manifest(),
        fetcher=concurrent_fetcher,
        marker_status=_markers(BEHIND),
    )
    assert result.repositories[2].served["staging"] == "v0.2.2"
    assert result.repositories[3].served["production"] == "v0.1.12"


def test_fetch_json_request_and_https_only(monkeypatch) -> None:
    seen: dict = {}

    def urlopen(request, timeout):
        seen.update(request=request, timeout=timeout)
        return _response(b'{"version": "v1.0.0"}')

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)

    assert fetch_json(URL) == {"version": "v1.0.0"}
    for refused in ("http://app.example.test/api/health", "file:///etc/passwd"):
        with pytest.raises(ValueError, match="not an https URL"):
            fetch_json(refused)

    agent = seen["request"].get_header("User-agent")
    assert seen["request"].full_url == URL  # the refused URLs never reached urlopen
    assert seen["request"].get_method() == "GET"
    assert agent.startswith("Mozilla/5.0 (compatible; infra2-")
    assert seen["timeout"] == 8.0


def test_infra2_row_sees_remote_tags_not_merged_into_local_head(tmp_path: Path) -> None:
    """#1289: a checkout with HEAD at v1.2.41 and remote tags through v1.2.44 shows
    staging v1.2.44 and marker 3 releases behind."""
    remote = tmp_path / "origin.git"
    checkout = tmp_path / "checkout"
    subprocess.run(
        ["git", "init", "--bare", str(remote)], check=True, capture_output=True
    )
    subprocess.run(["git", "init", str(checkout)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(checkout), "config", "user.email", "test@example.com"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(checkout), "config", "user.name", "Test"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(checkout), "remote", "add", "origin", str(remote)],
        check=True,
    )
    commits: dict[str, str] = {}
    for ver in ("v1.2.41", "v1.2.42", "v1.2.43", "v1.2.44"):
        (checkout / "VERSION").write_text(ver, encoding="utf-8")
        subprocess.run(["git", "-C", str(checkout), "add", "VERSION"], check=True)
        subprocess.run(
            ["git", "-C", str(checkout), "commit", "-m", ver],
            check=True,
            capture_output=True,
        )
        sha = subprocess.run(
            ["git", "-C", str(checkout), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        commits[ver] = sha
        subprocess.run(["git", "-C", str(checkout), "tag", ver], check=True)
        if ver == "v1.2.41":
            subprocess.run(
                ["git", "-C", str(checkout), "tag", "production/v1.2.41"],
                check=True,
            )
    subprocess.run(
        ["git", "-C", str(checkout), "push", "origin", "HEAD", "--tags"],
        check=True,
        capture_output=True,
    )
    # Move local HEAD back to v1.2.41 and delete local newer tags so local --merged HEAD only sees v1.2.41.
    subprocess.run(
        ["git", "-C", str(checkout), "checkout", "--detach", commits["v1.2.41"]],
        check=True,
        capture_output=True,
    )
    for newer in ("v1.2.42", "v1.2.43", "v1.2.44"):
        subprocess.run(
            ["git", "-C", str(checkout), "tag", "-d", newer],
            check=True,
            capture_output=True,
        )

    result = observe_online(
        _status(),
        checkout,
        _manifest(),
        fetcher=FakeFetcher(BODIES),
    )
    infra2_served = result.repositories[0].served
    assert infra2_served == {
        "staging": "v1.2.44",
        "production": "v1.2.41",
        "note": "marker 3 releases behind",
    }
