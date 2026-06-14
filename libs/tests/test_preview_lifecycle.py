"""Tests for the multi-alias preview lifecycle (up/down orchestration).

All Dokploy + HTTP side effects are mocked: NO live control-plane or network call is
made here. The fake client records create/update/deploy/delete so the tests assert the
right calls happen, with the right args, in the right order — the live boot + routing
path itself still needs a real smoke test (see the PR body).
"""

from __future__ import annotations

import pytest

from tools import preview_lifecycle as pl

# resolve_to_sha is patched in every test so no `git ls-remote` runs.
FULL_SHA = "1af32e6daf17e2c58383dd2c0bfaea13bc11e517"
SHORT_SHA = "1af32e6"


@pytest.fixture(autouse=True)
def _stub_resolver(monkeypatch):
    monkeypatch.setattr(pl, "resolve_to_sha", lambda code, repo=None: FULL_SHA)


class FakeDokploy:
    """Records the compose calls the preview lifecycle makes, no real Dokploy."""

    def __init__(
        self, *, existing=None, environment_id="env-preview", github_id="gh-1"
    ):
        self._existing = existing  # dict returned by find_compose_by_name, or None
        self._environment_id = environment_id
        self._github_id = github_id
        self.created: list[dict] = []
        self.updated: list[dict] = []
        self.env_updates: list[tuple[str, dict]] = []
        self.deployed: list[str] = []
        self.deleted: list[tuple[str, bool]] = []
        self.found: list[tuple] = []

    def get_github_provider_id(self):
        return self._github_id

    def get_environment_id(self, project, env_name=None):
        return self._environment_id

    def find_compose_by_name(self, name, project_name=None, env_name=None):
        self.found.append((name, project_name, env_name))
        # The source env's app compose supplies the AppRole creds previews reuse.
        if name == "app" and env_name == "staging":
            return {"composeId": "cmp-source-app"}
        return self._existing

    def get_compose_env(self, compose_id):
        if compose_id == "cmp-source-app":
            return (
                "VAULT_ADDR=https://vault.test\n"
                "VAULT_ROLE_ID=rid-test\n"
                "VAULT_SECRET_ID=sid-test\n"
            )
        return ""

    def create_compose(self, environment_id, name, **kwargs):
        self.created.append({"environment_id": environment_id, "name": name, **kwargs})
        return {"composeId": "cmp-new"}

    def update_compose(self, compose_id, **kwargs):
        self.updated.append({"composeId": compose_id, **kwargs})
        return {}

    def update_compose_env(self, compose_id, env_vars=None, env_str=None):
        self.env_updates.append((compose_id, dict(env_vars or {})))
        return {}

    def deploy_compose(self, compose_id):
        self.deployed.append(compose_id)
        return {}

    def delete_compose(self, compose_id, *, delete_volumes=False):
        self.deleted.append((compose_id, delete_volumes))
        return {}


def _ok_get(url, timeout):
    return 200, "ok"


# --- up: create path -------------------------------------------------------------


def test_up_creates_compose_when_absent_and_deploys():
    client = FakeDokploy(existing=None)
    result = pl.up(
        "pr",
        5,
        code="main",
        domain="zitian.party",
        client=client,
        http_get=_ok_get,
        _now=lambda: 1000,
    )
    # one compose created with the deterministic alias name — BASIC fields only.
    # Dokploy's compose.create silently drops source/env, so they must NOT be relied on
    # here; passing them to create is the bug that left a first-ever preview source-less.
    assert len(client.created) == 1
    created = client.created[0]
    assert created["name"] == "finance-report-preview-pr-5"
    assert created["app_name"] == "finance-report-preview-pr-5"
    assert created["environment_id"] == "env-preview"
    # created as github source from the start (never a raw/empty compose); the github
    # binding is still re-applied via update_compose since compose.create drops it.
    assert created["source_type"] == "github"
    assert len(client.updated) == 1
    upd = client.updated[0]
    assert upd["composeId"] == "cmp-new"
    assert upd["source_type"] == "github"
    assert upd["composePath"] == "finance_report/finance_report/preview/compose.yaml"
    assert upd["autoDeploy"] is False
    # env applied via update_compose_env (also dropped by compose.create)
    assert len(client.env_updates) == 1 and client.env_updates[0][0] == "cmp-new"
    # then deployed
    assert client.deployed == ["cmp-new"]
    # result identity
    assert result.action == "up"
    assert result.alias == "pr-5"
    assert result.compose_id == "cmp-new"
    assert result.sha == FULL_SHA
    assert result.url == "https://report-pr-5.zitian.party"
    assert result.healthy is True


def test_up_env_has_short_sha_suffix_and_ephemeral_db_knobs():
    client = FakeDokploy(existing=None)
    pl.up(
        "commit",
        "1ab32d5e6f",
        code="abc1234",
        domain="zitian.party",
        client=client,
        http_get=_ok_get,
        _now=lambda: 1000,
    )
    # env is applied via update_compose_env (compose.create drops the env blob), so it is
    # asserted on the env_update, not on create.
    _cid, env = client.env_updates[0]
    assert env["IMAGE_TAG"] == SHORT_SHA
    assert env["GIT_COMMIT_SHA"] == SHORT_SHA
    assert env["ENV_SUFFIX"] == "-commit-1ab32d5"
    assert env["ENV_DOMAIN_SUFFIX"] == "-commit-1ab32d5"
    assert env["ENV"] == "commit-1ab32d5"  # telemetry deployment.environment label
    assert env["NEXT_PUBLIC_APP_URL"] == "https://report-commit-1ab32d5.zitian.party"
    assert env["INTERNAL_DOMAIN"] == "zitian.party"
    # cache-bust is keyed on the RESOLVED sha (short form), not the alias token
    assert env["IAC_CONFIG_HASH"] == f"preview-{SHORT_SHA}-1000000"
    # ephemeral DB knobs the compose template reads to override DATABASE_URL
    assert env["PREVIEW_DB_USER"] == "preview"
    assert env["PREVIEW_DB_NAME"] == "finance_report"
    # app secrets are read from a fixed source env (preview has no per-alias Vault path)
    assert env["PREVIEW_SECRET_ENV"] == "staging"


def test_up_injects_source_env_vault_creds():
    # The preview vault-agent logs in with the SOURCE env's AppRole creds (the same role
    # staging runs with), read off that env's app compose and merged into the preview env.
    client = FakeDokploy(existing=None)
    pl.up("pr", 5, code="main", domain="zitian.party", client=client, http_get=_ok_get)
    _cid, env = client.env_updates[0]
    assert env["VAULT_ADDR"] == "https://vault.test"
    assert env["VAULT_ROLE_ID"] == "rid-test"
    assert env["VAULT_SECRET_ID"] == "sid-test"
    # it read them from the source env's app compose, not the preview alias
    assert ("app", "finance_report", "staging") in client.found


# --- up: update path (idempotent re-deploy of an alias) --------------------------


def test_up_updates_existing_compose_in_place():
    client = FakeDokploy(existing={"composeId": "cmp-existing"})
    result = pl.up(
        "main",
        None,
        code="main",
        domain="zitian.party",
        client=client,
        http_get=_ok_get,
    )
    # re-run finds + updates the SAME compose; never creates a second one
    assert client.created == []
    assert len(client.updated) == 1
    assert client.updated[0]["composeId"] == "cmp-existing"
    assert client.updated[0]["composePath"].endswith("preview/compose.yaml")
    # env is MERGED (not replaced) so runtime VAULT creds survive a redeploy
    assert len(client.env_updates) == 1
    cid, env_vars = client.env_updates[0]
    assert cid == "cmp-existing"
    assert env_vars["ENV"] == "main"
    assert env_vars["ENV_SUFFIX"] == "-main"
    # the source env's AppRole creds are injected on every up (merged on redeploy)
    assert env_vars["VAULT_ROLE_ID"] == "rid-test"
    assert client.deployed == ["cmp-existing"]
    assert result.compose_id == "cmp-existing"
    assert result.url == "https://report-main.zitian.party"


def test_up_looks_up_compose_by_deterministic_preview_name():
    client = FakeDokploy(existing=None)
    pl.up("pr", 7, code="main", domain="z.p", client=client, http_get=_ok_get)
    # the alias is looked up by its deterministic name in the preview env (alongside the
    # source-app creds lookup, which is a separate find)
    assert ("finance-report-preview-pr-7", "finance_report", "preview") in client.found


# --- up: ordering + gates --------------------------------------------------------


def test_up_no_wait_skips_health_check():
    called = []
    client = FakeDokploy(existing=None)
    result = pl.up(
        "pr",
        5,
        code="main",
        domain="z.p",
        client=client,
        wait=False,
        http_get=lambda u, t: called.append(u) or (200, "ok"),
    )
    assert called == []  # health getter never consulted
    assert result.healthy is None
    assert client.deployed == ["cmp-new"]


def test_up_raises_when_health_never_passes():
    client = FakeDokploy(existing=None)
    with pytest.raises(TimeoutError, match="did not become healthy"):
        pl.up(
            "pr",
            5,
            code="main",
            domain="z.p",
            client=client,
            http_get=lambda u, t: (503, "down"),
            health_timeout=600,
            _sleep=lambda _s: None,
            _monotonic=iter([0, 700]).__next__,
        )
    # the deploy still happened; health is a post-deploy gate, not a pre-gate
    assert client.deployed == ["cmp-new"]


def test_up_rejects_bad_domain_before_any_side_effect():
    client = FakeDokploy(existing=None)
    with pytest.raises(ValueError, match="invalid domain"):
        pl.up("pr", 5, code="main", domain="z.p\nINJECT=1", client=client)
    assert client.created == []
    assert client.deployed == []


def test_up_fails_closed_without_github_provider():
    client = FakeDokploy(existing=None, github_id=None)
    with pytest.raises(RuntimeError, match="no GitHub provider"):
        pl.up("pr", 5, code="main", domain="z.p", client=client)
    assert client.created == []
    assert client.deployed == []


def test_up_fails_when_preview_environment_missing():
    client = FakeDokploy(existing=None, environment_id=None)
    with pytest.raises(RuntimeError, match="environment 'preview' not found"):
        pl.up("pr", 5, code="main", domain="z.p", client=client)
    assert client.created == []


def test_up_invalid_alias_makes_no_calls():
    client = FakeDokploy(existing=None)
    with pytest.raises(ValueError, match="positive PR number"):
        pl.up("pr", "0", code="main", domain="z.p", client=client)
    assert client.created == []
    assert client.deployed == []


# --- down: teardown destroys the ephemeral DB volume -----------------------------


def test_down_deletes_compose_with_volumes():
    client = FakeDokploy(existing={"composeId": "cmp-existing"})
    result = pl.down("pr", 5, domain="zitian.party", client=client)
    assert client.deleted == [("cmp-existing", True)]  # delete_volumes=True
    assert result.action == "down"
    assert result.alias == "pr-5"
    assert result.compose_id == "cmp-existing"


def test_down_is_idempotent_when_alias_absent():
    client = FakeDokploy(existing=None)
    result = pl.down("commit", "1ab32d5", domain="zitian.party", client=client)
    assert client.deleted == []  # nothing to delete -> no call
    assert result.compose_id is None
    assert result.url == "https://report-commit-1ab32d5.zitian.party"


# --- the readiness poller --------------------------------------------------------


def test_wait_for_health_returns_true_on_first_200():
    snapshots = iter([(503, ""), (200, "ok")])
    healthy = pl._wait_for_health(
        "https://x/api/health",
        timeout=600,
        interval=0,
        http_get=lambda u, t: next(snapshots),
        _sleep=lambda _s: None,
        _now=iter([0, 1]).__next__,
    )
    assert healthy is True


def test_wait_for_health_returns_false_on_timeout():
    healthy = pl._wait_for_health(
        "https://x/api/health",
        timeout=600,
        interval=0,
        http_get=lambda u, t: (0, "conn refused"),
        _sleep=lambda _s: None,
        _now=iter([0, 700]).__next__,
    )
    assert healthy is False


# --- main(): domain validated BEFORE the Dokploy client is built -----------------


def _no_dokploy(monkeypatch):
    """Fail the test if main() ever tries to build a Dokploy client."""

    def _boom(*_args, **_kwargs):  # pragma: no cover - only hit on regression
        raise AssertionError("get_dokploy must not be called for a malformed domain")

    monkeypatch.setattr("libs.dokploy.get_dokploy", _boom)


@pytest.mark.parametrize("action", ["up", "down"])
def test_main_rejects_bad_domain_before_building_client(action, monkeypatch, capsys):
    # A whitespace-bearing domain must be rejected up front for BOTH up and down, so the
    # malformed domain never reaches get_dokploy(host=f"cloud.{domain}").
    _no_dokploy(monkeypatch)
    argv = [action, "--kind", "pr", "--value", "5", "--domain", "z.p\nINJECT=1"]
    if action == "up":
        argv += ["--code", "main"]

    rc = pl.main(argv)

    assert rc == 2
    err = capsys.readouterr().err
    assert "invalid domain" in err
