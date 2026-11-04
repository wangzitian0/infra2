"""Tests for the config-drift reconciler's pure pieces + the path-independence guarantee.

The live parts (Dokploy reads, git cat-file at a tag, Feishu post) run only in the scheduled
workflow; the hash purity, report formatting, and delivery no-op — where the logic lives — are
tested offline here.
"""

from __future__ import annotations

from pathlib import Path

from common.deploy.config_hash import config_hash_from_items

ROOT = Path(__file__).resolve().parents[2]


def test_config_hash_is_pure_and_path_independent() -> None:
    """Same (compose, env, repo-relative items) -> same hash, with NO dependence on cwd or
    absolute paths (labels are repo-relative strings). This is what lets the reconciler recompute
    a service's hash from a git ref and match what the deploy computed on disk."""
    compose = "services: {a: {image: x}}"
    env = {"ENV": "production", "INTERNAL_DOMAIN": "zitian.party"}
    artifact = [("common/a.py", b"print(1)"), ("tools/b.py", b"x=2")]
    deps = [("common/dep.py", b"shared")]

    h1 = config_hash_from_items(compose, env, artifact, deps)
    h2 = config_hash_from_items(compose, env, artifact, deps)
    assert h1 == h2  # deterministic
    assert len(h1) == 12  # sha256[:12]

    # content change flips the hash
    assert (
        config_hash_from_items(compose, env, [("common/a.py", b"print(2)")], deps) != h1
    )
    # env change flips the hash
    assert (
        config_hash_from_items(compose, {**env, "ENV": "staging"}, artifact, deps) != h1
    )
    # a dependency change flips the hash (the cross-service fold)
    assert (
        config_hash_from_items(compose, env, artifact, [("common/dep.py", b"changed")])
        != h1
    )


def _drift_mod():
    from common.deploy import config_drift

    return config_drift


def test_format_report_flags_drift_and_confirms_in_sync() -> None:
    m = _drift_mod()
    Row = m.Row
    drift = m.format_report("v1.2.3", [Row("platform/x", "DRIFT", "aaa", "bbb")])
    assert "🔴 DRIFT platform/x" in drift and "v1.2.3" in drift

    ok = m.format_report("v1.2.3", [Row("platform/y", "in_sync", "aaa", "aaa")])
    assert "✅ every comparable service matches production target v1.2.3" in ok


def test_format_report_surfaces_errors_and_does_not_call_them_drift() -> None:
    """A compute failure must be surfaced (not silently skipped) AND must not be counted as
    drift — else a run looks healthy while it never actually checked the service."""
    m = _drift_mod()
    Row = m.Row
    out = m.format_report(
        "v1.2.3", [Row("platform/z", "error", note="compute error: boom")]
    )
    assert "⚠️ ERROR platform/z" in out  # surfaced loudly
    assert "DRIFT 0" in out  # an error is NOT drift
    assert "🔴 DRIFT" not in out


def test_deliver_infra2_report_noops_when_unconfigured(monkeypatch) -> None:
    from common.alerting import deliver_infra2_report

    for k in (
        "INFRA2_REPORTS_FEISHU_APP_ID",
        "INFRA2_REPORTS_FEISHU_APP_SECRET",
        "INFRA2_REPORTS_FEISHU_CHAT_ID",
    ):
        monkeypatch.delenv(k, raising=False)
    assert (
        deliver_infra2_report("hi") is False
    )  # not configured -> clean no-op, no raise


def test_scan_iac_runner_flags_stale_l1_container_and_accepts_current() -> None:
    m = _drift_mod()
    in_sync = m.scan_iac_runner(
        "HEAD",
        health_url="https://iac-runner.example.test/health",
        fetcher=lambda _url: {"git_sha": m._commit_at_ref("HEAD")},
    )
    assert in_sync.service == "bootstrap/iac_runner"
    assert in_sync.verdict == "in_sync"

    stale = m.scan_iac_runner(
        "HEAD",
        health_url="https://iac-runner.example.test/health",
        fetcher=lambda _url: {"git_sha": "0" * 40},
    )
    assert stale.service == "bootstrap/iac_runner"
    assert stale.verdict == "DRIFT"
    assert "bootstrap/06.iac_runner" in stale.note


def test_scan_iac_runner_rejects_malformed_git_sha_before_git_merge_base(
    monkeypatch,
) -> None:
    """Reject malformed or flag-like git_sha values before invoking git merge-base."""
    import subprocess as sp

    m = _drift_mod()
    real_run = sp.run
    merge_base_calls: list[list[str]] = []

    def _spy_run(cmd, *args, **kwargs):
        if isinstance(cmd, list) and "merge-base" in cmd:
            merge_base_calls.append(cmd)
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(m.subprocess, "run", _spy_run)
    row = m.scan_iac_runner(
        "HEAD",
        health_url="https://iac-runner.example.test/health",
        fetcher=lambda _url: {"git_sha": "-bad-flag"},
    )
    assert row.service == "bootstrap/iac_runner"
    assert row.verdict == "error"
    assert "invalid running git_sha" in row.note
    assert merge_base_calls == []


def test_scan_iac_runner_derives_default_url_from_internal_domain(monkeypatch) -> None:
    """Derive https://iac.<INTERNAL_DOMAIN>/health when IAC_RUNNER_HEALTH_URL is unset."""
    m = _drift_mod()
    monkeypatch.delenv("IAC_RUNNER_HEALTH_URL", raising=False)
    monkeypatch.setenv("INTERNAL_DOMAIN", "zitian.party")
    seen_urls: list[str] = []

    def _fetcher(url: str) -> dict[str, str]:
        seen_urls.append(url)
        return {"git_sha": m._commit_at_ref("HEAD")}

    row = m.scan_iac_runner("HEAD", fetcher=_fetcher)
    assert seen_urls == ["https://iac.zitian.party/health"]
    assert row.service == "bootstrap/iac_runner"
    assert row.verdict == "in_sync"


def test_scan_appends_iac_runner_on_live_dokploy_run(monkeypatch) -> None:
    """Append scan_iac_runner(tag) when DOKPLOY_API_KEY and INTERNAL_DOMAIN are set."""
    m = _drift_mod()
    monkeypatch.delenv("IAC_RUNNER_HEALTH_URL", raising=False)
    monkeypatch.setenv("DOKPLOY_API_KEY", "live-key")
    monkeypatch.setenv("INTERNAL_DOMAIN", "zitian.party")
    monkeypatch.setattr(m.service_registry, "all_services", lambda: [])
    monkeypatch.setattr(m, "_deployed_identities", lambda: {})
    monkeypatch.setattr(m, "_commit_at_ref", lambda _tag: "a" * 40)
    monkeypatch.setattr(
        m,
        "scan_iac_runner",
        lambda tag: m.Row("bootstrap/iac_runner", "in_sync", expected=tag),
    )

    rows = m.scan("v1.2.3")
    assert [r.service for r in rows] == ["bootstrap/iac_runner"]
    assert rows[0].verdict == "in_sync"


def test_scan_omits_iac_runner_when_env_unconfigured(monkeypatch) -> None:
    """Omit bootstrap/iac_runner from scan() when neither IAC_RUNNER_HEALTH_URL nor live Dokploy env is set."""
    m = _drift_mod()
    monkeypatch.setenv("IAC_RUNNER_HEALTH_URL", "   ")
    monkeypatch.delenv("DOKPLOY_API_KEY", raising=False)
    monkeypatch.setenv("INTERNAL_DOMAIN", "zitian.party")
    monkeypatch.setattr(m.service_registry, "all_services", lambda: [])
    monkeypatch.setattr(m, "_deployed_identities", lambda: {})
    monkeypatch.setattr(m, "_commit_at_ref", lambda _tag: "a" * 40)

    called = False

    def _unexpected_call(_tag: str):
        nonlocal called
        called = True
        return m.Row("bootstrap/iac_runner", "in_sync")

    monkeypatch.setattr(m, "scan_iac_runner", _unexpected_call)

    rows = m.scan("v1.2.3")
    assert rows == []
    assert not called


def test_iac_runner_dockerfile_and_webhook_expose_git_sha() -> None:
    """Verify Dockerfile sets ENV GIT_SHA and webhook_server.py /health returns git_sha via AST."""
    import ast

    dockerfile = (ROOT / "bootstrap/06.iac_runner/Dockerfile").read_text(
        encoding="utf-8"
    )
    assert 'ENV GIT_SHA="${GIT_SHA}"' in dockerfile

    webhook_src = (ROOT / "bootstrap/06.iac_runner/webhook_server.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(webhook_src)
    health_fn = next(
        (
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "health"
        ),
        None,
    )
    assert health_fn is not None, "def health() not found in webhook_server.py"
    dict_keys: set[str] = set()
    for node in ast.walk(health_fn):
        if isinstance(node, ast.Return) and node.value is not None:
            for sub in ast.walk(node.value):
                if isinstance(sub, ast.Dict):
                    for k in sub.keys:
                        if isinstance(k, ast.Constant) and isinstance(k.value, str):
                            dict_keys.add(k.value)
    assert "git_sha" in dict_keys
