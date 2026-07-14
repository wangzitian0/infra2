"""Tests for the multi-alias preview lifecycle (up/down orchestration).

All Dokploy + HTTP side effects are mocked: NO live control-plane or network call is
made here. The fake client records create/update/deploy/delete so the tests assert the
right calls happen, with the right args, in the right order — the live boot + routing
path itself still needs a real smoke test (see the PR body).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from libs.deploy import preview as pl

COMPOSE_PATH = (
    Path(__file__).resolve().parents[2]
    / "finance_report/finance_report/preview/compose.yaml"
)

# resolve_to_sha is patched in every test so no `git ls-remote` runs.
FULL_SHA = "1af32e6daf17e2c58383dd2c0bfaea13bc11e517"
SHORT_SHA = "1af32e6"


@pytest.fixture(autouse=True)
def _stub_resolver(monkeypatch):
    monkeypatch.setattr(pl, "resolve_to_sha", lambda code, repo=None: FULL_SHA)


class FakeDokploy:
    """Records the compose calls the preview lifecycle makes, no real Dokploy."""

    def __init__(
        self,
        *,
        existing=None,
        environment_id="env-preview",
        github_id="gh-1",
        source_app=True,
        source_creds=True,
        compose_status="done",
    ):
        self._compose_status = compose_status  # Dokploy deploy status the wait polls
        self._existing = existing  # dict returned by find_compose_by_name, or None
        self._environment_id = environment_id
        self._github_id = github_id
        self._source_app = source_app  # is the source env's app compose present?
        self._source_creds = source_creds  # does it carry the AppRole creds?
        self.created: list[dict] = []
        self.updated: list[dict] = []
        self.env_updates: list[tuple[str, dict]] = []
        self.deployed: list[str] = []
        self.deleted: list[tuple[str, bool]] = []
        self.found: list[tuple] = []
        self.ensured: list[tuple] = []

    def get_github_provider_id(self):
        return self._github_id

    def get_environment_id(self, project, env_name=None):
        return self._environment_id

    def ensure_environment(self, project, env_name, description=""):
        self.ensured.append((project, env_name))
        # Mirror the real client: it raises ValueError when the project is absent (it does
        # NOT return an env with environmentId=None). environment_id=None models that.
        if self._environment_id is None:
            raise ValueError(f"Project '{project}' not found in Dokploy")
        return {"environmentId": self._environment_id, "name": env_name}, False

    def find_compose_by_name(self, name, project_name=None, env_name=None):
        self.found.append((name, project_name, env_name))
        # The source env's app compose supplies the AppRole creds previews reuse.
        if name == "app" and env_name == "staging":
            return {"composeId": "cmp-source-app"} if self._source_app else None
        return self._existing

    def get_compose_env(self, compose_id):
        if compose_id == "cmp-source-app":
            if not self._source_creds:
                return "OTHER=x\n"  # compose exists but the AppRole creds are absent
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

    def get_compose(self, compose_id):
        return {"composeId": compose_id, "composeStatus": self._compose_status}

    def delete_compose(self, compose_id, *, delete_volumes=False):
        self.deleted.append((compose_id, delete_volumes))
        return {}


def _ok_get(url, timeout):
    return 200, "ok"


def test_up_fast_fails_when_dokploy_reports_deploy_error():
    # an unpublished image / build failure -> Dokploy composeStatus=error -> bail at once,
    # NOT wait out the full health timeout (the 10-min hang the live canary hit).
    client = FakeDokploy(existing=None, compose_status="error")
    polls = {"n": 0}

    def never_healthy(url, timeout):
        polls["n"] += 1
        return 0, "down"

    with pytest.raises(RuntimeError, match="composeStatus=error"):
        pl.up(
            "pr",
            5,
            code="main",
            domain="z.p",
            client=client,
            http_get=never_healthy,
            health_timeout=600,
            health_interval=10,
        )
    assert polls["n"] == 0  # failed before the first HTTP health poll — no 600s wait


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


def test_up_self_provisions_the_preview_environment():
    # The dynamic preview env is created-if-absent (idempotent) so a first-ever preview on
    # a fresh box needs no out-of-band env-ensure step.
    client = FakeDokploy(existing=None)
    pl.up("pr", 5, code="main", domain="zitian.party", client=client, http_get=_ok_get)
    assert ("finance_report", "preview") in client.ensured


def test_up_fails_closed_when_source_env_app_compose_missing():
    # The source env (staging) has no `app` compose to read creds from -> fail closed,
    # never deploy a preview whose vault-agent would crash-loop.
    client = FakeDokploy(existing=None, source_app=False)
    with pytest.raises(RuntimeError, match="cannot source preview Vault creds"):
        pl.up("pr", 5, code="main", domain="z.p", client=client, http_get=_ok_get)
    assert client.created == [] and client.deployed == []


def test_up_fails_closed_when_source_env_has_no_vault_creds():
    # The source compose exists but carries no VAULT_ROLE_ID/SECRET_ID -> fail closed.
    client = FakeDokploy(existing=None, source_creds=False)
    with pytest.raises(RuntimeError, match="missing Vault creds"):
        pl.up("pr", 5, code="main", domain="z.p", client=client, http_get=_ok_get)
    assert client.created == [] and client.deployed == []


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
        "branch",
        "main",
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
    assert env_vars["ENV"] == "branch-main"
    assert env_vars["ENV_SUFFIX"] == "-branch-main"
    # #375: every preview alias shares the one "preview" OpenPanel project, injected at
    # runtime so preview analytics actually emits (was missing — only deploy_primitive
    # had it). Non-empty client-id + canonical environment, mirroring staging/prod.
    from tools.openpanel_clients import openpanel_env

    assert (
        env_vars["OPENPANEL_CLIENT_ID"]
        == openpanel_env("preview")["OPENPANEL_CLIENT_ID"]
    )
    assert env_vars["OPENPANEL_CLIENT_ID"]  # non-empty: the preview project is issued
    assert env_vars["OPENPANEL_ENVIRONMENT"] == "preview"
    # the source env's AppRole creds are injected on every up (merged on redeploy)
    assert env_vars["VAULT_ROLE_ID"] == "rid-test"
    assert client.deployed == ["cmp-existing"]
    assert result.compose_id == "cmp-existing"
    assert result.url == "https://report-branch-main.zitian.party"


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


def test_up_fails_closed_if_environment_cannot_be_ensured():
    # ensure_environment yields no id (e.g. the project itself is absent) -> fail closed,
    # never try to create a compose in a non-existent environment.
    client = FakeDokploy(existing=None, environment_id=None)
    with pytest.raises(RuntimeError, match="could not ensure"):
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


def test_up_image_ref_overrides_short_sha_for_releases():
    # a release pulls its retained TAG, not the (pruned) short sha
    client = FakeDokploy(existing=None)
    pl.up(
        "branch",
        "main",
        code="main",
        domain="z.p",
        client=client,
        http_get=_ok_get,
        image_ref="v1.2.3",
    )
    _cid, env = client.env_updates[0]
    assert env["IMAGE_TAG"] == "v1.2.3" and env["GIT_COMMIT_SHA"] == "v1.2.3"


def test_preview_entrypoint_overrides_environment_to_match_otel_tag():
    """Regression test: every preview boot borrows PREVIEW_SECRET_ENV's (default
    "staging") Vault secrets, which bake in ENVIRONMENT=staging — while
    OTEL_RESOURCE_ATTRIBUTES is correctly templated per-alias by secrets.ctmpl
    (deployment.environment=<this alias's ENV token>). Nothing overrode
    ENVIRONMENT the way DATABASE_URL already is, so the app's
    #1828 telemetry-tag-consistency guard (config.py::
    _require_telemetry_contract_in_deployed_envs) compared a borrowed "staging"
    against the correct per-alias tag and failed closed on every single preview
    boot (finance_report#1851 investigation) -- alembic migrations crash-looped
    10/10 attempts, the container never went healthy, and Dokploy's compose-up
    call timed out with no output. ENV: ${ENV} must be passed into the backend
    service, and the entrypoint must export ENVIRONMENT="$ENV" AFTER sourcing
    /secrets/.env, mirroring the existing DATABASE_URL override exactly.
    """
    source = COMPOSE_PATH.read_text(encoding="utf-8")
    backend_start = source.index("\n  backend:")
    frontend_start = source.index("\n  frontend:")
    backend_block = source[backend_start:frontend_start]

    assert "ENV: ${ENV}" in backend_block, (
        "backend service must receive ENV so the entrypoint can override "
        "ENVIRONMENT with it"
    )
    database_url_idx = backend_block.index(
        'export DATABASE_URL="$$PREVIEW_DATABASE_URL"'
    )
    environment_idx = backend_block.index('export ENVIRONMENT="$$ENV"')
    assert environment_idx > database_url_idx, (
        "ENVIRONMENT override must come AFTER `. /secrets/.env` is sourced "
        "(same ordering requirement as the DATABASE_URL override), or Vault's "
        "borrowed staging value would win"
    )
