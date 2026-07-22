"""Tests for the internal fixed-compose app deploy backend used by deploy_v2."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from libs.deploy import promote as dp
from libs.deploy.promote import (
    ensure_generated_secrets as _real_ensure_generated_secrets,
)
from libs.deploy_queue import parse_epoch_seconds

# A realistic full commit sha and its 7-char short form (the tag images are published
# under). resolve_to_sha returns a full sha; IMAGE_TAG must be the short form.
FULL_SHA = "1af32e6daf17e2c58383dd2c0bfaea13bc11e517"
SHORT_SHA = "1af32e6"


class FakeDokploy:
    """Records the compose env-update + deploy calls instead of hitting Dokploy."""

    def __init__(self, deployments=None, env_str=None):
        self.updated: list[tuple[str, dict]] = []
        self.deployed: list[str] = []
        self.branch_updates: list[tuple[str, str]] = []
        # list, or callable(self) -> list, used by wait_for_rollout's rollout poll.
        self._deployments = deployments
        # str/callable override for get_compose_env; if None, reflect the last push.
        self._env_str = env_str

    def update_compose(self, compose_id, **kwargs):
        if "branch" in kwargs:
            self.branch_updates.append((compose_id, kwargs["branch"]))
        return {}

    def update_compose_env(self, compose_id, env_vars=None, env_str=None):
        self.updated.append((compose_id, dict(env_vars or {})))
        return {}

    def deploy_compose(self, compose_id):
        self.deployed.append(compose_id)
        return {}

    def get_compose_deployments(self, compose_id):
        d = self._deployments
        return d(self) if callable(d) else (d or [])

    def get_compose_env(self, compose_id):
        if self._env_str is not None:
            e = self._env_str
            return e(self) if callable(e) else e
        # reflect the last pushed env as a KEY=VALUE blob (effective-config verify reads it)
        if self.updated:
            return "\n".join(f"{k}={v}" for k, v in self.updated[-1][1].items())
        # Baseline: a realistically-provisioned compose already has its AppRole creds
        # (set once at initial `invoke vault.setup-approle`, never touched by promote's
        # own env push) — assert_approle_creds_present reads this BEFORE any push, so
        # this is the state it sees. Tests exercising the missing-creds case override
        # via env_str=.
        return (
            "VAULT_ROLE_ID=r\nVAULT_SECRET_ID=s\nVAULT_ADDR=https://vault.zitian.party"
        )


@pytest.fixture(autouse=True)
def _stub_secret_provisioning(monkeypatch):
    """deploy() now self-heals missing Vault-generated secrets before mutating
    (truealpha#447 — see ensure_generated_secrets), via a real dynamic import +
    a real Vault-backed secrets_backend() call. That is a different external
    system than the Dokploy `client` this whole file fakes, and none of the
    tests below care about it — so neutralize it by default; the dedicated
    tests in the "generated-secret provisioning" section re-arm it via their
    own monkeypatch.
    """
    monkeypatch.setattr(dp, "ensure_generated_secrets", lambda *a, **k: None)


def test_staging_deploy_assembles_axes_and_triggers():
    client = FakeDokploy()
    plan = dp.deploy(
        "staging",
        FULL_SHA,
        domain="zitian.party",
        client=client,
        iac_ref="b" * 40,
    )

    # the plan keeps the canonical full sha as the commit identity...
    assert plan.sha == FULL_SHA
    assert plan.compose_id == "A6V-hbJlgHMwgPDoTDnhH"
    assert plan.data == "staging"
    # ...but the pushed env uses the short, image-addressable form (registry tag).
    cid, env = client.updated[0]
    assert cid == "A6V-hbJlgHMwgPDoTDnhH"
    assert env["IMAGE_TAG"] == SHORT_SHA
    assert env["GIT_COMMIT_SHA"] == SHORT_SHA
    assert env["NEXT_PUBLIC_APP_URL"] == "https://report-staging.zitian.party"
    assert env["ENV_SUFFIX"] == "-staging"
    assert env["INFRA_IDENTITY_SCHEMA"] == "v1"
    assert env["INFRA_SERVICE_ID"] == "finance_report/app"
    assert env["INFRA_ENVIRONMENT"] == "staging"
    assert env["INFRA_SERVICE_VERSION"] == SHORT_SHA
    assert env["INFRA_IAC_REF"] == "b" * 40
    assert "deployment.environment.name=staging" in env["OTEL_RESOURCE_ATTRIBUTES"]
    assert client.deployed == ["A6V-hbJlgHMwgPDoTDnhH"]


def test_branch_is_reasserted_on_every_deploy_when_given():
    # truealpha#447 root cause: a fixed compose's Dokploy github-source ref is whatever
    # it was set to at creation and never advances on its own — deploy_v2's iac_ref was
    # validated/recorded but never reached the actual git source Dokploy clones. branch
    # must be re-asserted on every call, mirroring libs.deploy.preview.up.
    client = FakeDokploy()
    dp.deploy(
        "staging",
        FULL_SHA,
        domain="zitian.party",
        client=client,
        iac_ref="b" * 40,
        branch="v1.1.38",
    )
    assert client.branch_updates == [("A6V-hbJlgHMwgPDoTDnhH", "v1.1.38")]


def test_branch_omitted_leaves_the_composes_source_untouched():
    # Backward compatible: a caller (or test) that doesn't care about the source ref
    # gets exactly today's behavior — no update_compose call at all.
    client = FakeDokploy()
    dp.deploy(
        "staging",
        FULL_SHA,
        domain="zitian.party",
        client=client,
        iac_ref="b" * 40,
    )
    assert client.branch_updates == []


def test_truealpha_staging_deploy_resolves_its_own_compose_and_identity():
    # #500: a second bespoke app's fixed-compose deploy, generalized from the
    # finance_report-only path above. Same shape, different service/compose/identity —
    # proves the generalization didn't just special-case finance_report's literals.
    client = FakeDokploy()
    plan = dp.deploy(
        "staging",
        FULL_SHA,
        domain="zitian.party",
        client=client,
        service="truealpha/app",
        iac_ref="b" * 40,
    )

    assert plan.compose_id == "w4zo_fm9d2PnUY8ULzNO7"
    cid, env = client.updated[0]
    assert cid == "w4zo_fm9d2PnUY8ULzNO7"
    assert env["IMAGE_TAG"] == SHORT_SHA
    assert env["NEXT_PUBLIC_APP_URL"] == "https://truealpha-staging.zitian.party"
    assert env["ENV_SUFFIX"] == "-staging"
    assert env["INFRA_SERVICE_ID"] == "truealpha/app"
    # the two truealpha images (app-web, llm-service) share one IMAGE_TAG by design
    # (confirmed against the live compose: both read ${IMAGE_TAG:-latest}) — nothing
    # else to assert here since promote.py pushes a single compose-level env, not
    # per-image tags.


def test_app_domain_override_does_not_leak_into_the_shared_control_plane_endpoints():
    # truealpha/app routes its own public traffic under truealpha.club (Deployer.domain,
    # #550) while Vault/SigNoz stay the single shared zitian.party instance. #561 fixed
    # this conflation in tools/deploy_v2.py's Dokploy-client-host build only — promote.py's
    # vault./otel. sites (this test) were NOT part of that fix: a truealpha deploy's OTLP
    # endpoint still pointed at otel.truealpha.club (no collector there — every truealpha
    # deploy silently shipped a broken telemetry endpoint to the frontend), and the same
    # conflation would send the Vault preflight probe to vault.truealpha.club (a Cloudflare
    # 526 — dormant here only because AppRole auth skips that preflight).
    client = FakeDokploy()
    plan = dp.deploy(
        "staging",
        FULL_SHA,
        domain="truealpha.club",
        client=client,
        service="truealpha/app",
        iac_ref="b" * 40,
    )
    cid, env = client.updated[0]
    assert cid == plan.compose_id
    # app's OWN routing: follows the override, unchanged.
    assert env["NEXT_PUBLIC_APP_URL"] == "https://truealpha-staging.truealpha.club"
    assert env["INTERNAL_DOMAIN"] == "truealpha.club"
    # shared control plane: NEVER follows the override.
    assert (
        env["NEXT_PUBLIC_OTEL_EXPORTER_OTLP_ENDPOINT"]
        == "https://otel.zitian.party/v1/traces"
    )


def test_truealpha_prod_deploy_resolves_its_own_compose_and_identity():
    # truealpha/app's production compose went live 2026-07-19 (Vault AppRole + app
    # secret + MinIO bucket provisioned, postgres + app deployed and health-checked at
    # https://truealpha.zitian.party) — mirrors the staging test above, prod env.
    client = FakeDokploy()
    plan = dp.deploy(
        "prod",
        FULL_SHA,
        domain="zitian.party",
        client=client,
        service="truealpha/app",
        staging_validated=True,
        iac_ref="b" * 40,
    )

    assert plan.compose_id == "j-gIAk0GfF0bGOitZN-og"
    cid, env = client.updated[0]
    assert cid == "j-gIAk0GfF0bGOitZN-og"
    assert env["NEXT_PUBLIC_APP_URL"] == "https://truealpha.zitian.party"
    assert env["ENV_SUFFIX"] == ""
    assert env["INFRA_SERVICE_ID"] == "truealpha/app"
    assert env["INFRA_ENVIRONMENT"] == "production"
    assert client.deployed == ["j-gIAk0GfF0bGOitZN-og"]


def test_prod_refuses_unvalidated_digest_promote_not_rebuild():
    client = FakeDokploy()
    with pytest.raises(ValueError, match="requires a staging deploy"):
        dp.deploy("prod", "deadbeef", domain="zitian.party", client=client)
    # the gate prevents ANY side effect: neither env mutation nor deploy
    assert client.updated == []
    assert client.deployed == []


def test_prod_deploys_a_staging_validated_digest():
    client = FakeDokploy()
    plan = dp.deploy(
        "prod", "deadbeef", domain="zitian.party", client=client, staging_validated=True
    )
    assert plan.compose_id == "lNn9gVS1Zyw79Jzw5dlbu"
    assert plan.data == "prod"
    assert client.updated[0][1]["NEXT_PUBLIC_APP_URL"] == "https://report.zitian.party"
    assert client.deployed == ["lNn9gVS1Zyw79Jzw5dlbu"]


def test_prod_break_glass_overrides_the_staging_gate():
    client = FakeDokploy()
    plan = dp.deploy(
        "prod", "deadbeef", domain="zitian.party", client=client, break_glass=True
    )
    assert plan.env == "prod"
    assert client.deployed == ["lNn9gVS1Zyw79Jzw5dlbu"]


def test_preview_is_rejected_as_dynamic():
    client = FakeDokploy()
    with pytest.raises(ValueError, match="per-PR dynamic env"):
        dp.deploy("preview", "deadbeef", domain="zitian.party", client=client)
    # the gate prevents ANY side effect: neither env mutation nor deploy
    assert client.updated == []
    assert client.deployed == []


def test_sha_is_lowercased_via_the_resolver():
    client = FakeDokploy()
    plan = dp.deploy("staging", "DEADBEEF", domain="zitian.party", client=client)
    assert plan.sha == "deadbeef"


def test_domain_with_newline_is_rejected_before_any_side_effect():
    # a newline in domain would corrupt the line-based compose env / inject a var
    client = FakeDokploy()
    with pytest.raises(ValueError, match="invalid domain"):
        dp.deploy("staging", "deadbeef", domain="zitian.party\nINJECT=1", client=client)
    assert client.updated == []
    assert client.deployed == []


def test_data_lane_is_derived_from_env_config_not_caller_supplied():
    client = FakeDokploy()
    plan = dp.deploy("staging", "deadbeef", domain="zitian.party", client=client)
    assert plan.data == "staging"


# --- rollout poll (P2 step 4a) ---------------------------------------------------


def _clock(*ticks):
    it = iter(ticks)
    return lambda: next(it)


def test_wait_for_rollout_returns_when_a_new_record_reaches_done():
    # poll 1: the new record is still running; poll 2: it is done.
    snapshots = iter(
        [
            [{"id": "old", "status": "done"}, {"id": "new", "status": "running"}],
            [{"id": "new", "status": "done"}],
        ]
    )
    client = FakeDokploy(deployments=lambda _self: next(snapshots))
    record = dp.wait_for_rollout(
        client, "cmp", {"old"}, interval=0, _sleep=lambda _s: None, _now=_clock(0, 1)
    )
    assert record["id"] == "new"


def test_wait_for_rollout_raises_on_a_new_record_error():
    client = FakeDokploy(deployments=[{"id": "new", "status": "error"}])
    with pytest.raises(RuntimeError, match="rollout entered error"):
        dp.wait_for_rollout(
            client, "cmp", {"old"}, _sleep=lambda _s: None, _now=_clock(0)
        )


def test_wait_for_rollout_times_out_if_no_new_record_finishes():
    # the new record never leaves running; the clock crosses the deadline.
    client = FakeDokploy(deployments=[{"id": "new", "status": "running"}])
    with pytest.raises(TimeoutError, match="did not finish"):
        dp.wait_for_rollout(
            client,
            "cmp",
            {"old"},
            timeout=600,
            _sleep=lambda _s: None,
            _now=_clock(0, 700),
        )


def test_wait_for_rollout_ignores_pre_existing_records():
    # only `before_ids`-excluded records count; an old done record must not satisfy it.
    client = FakeDokploy(deployments=[{"id": "old", "status": "done"}])
    with pytest.raises(TimeoutError):
        dp.wait_for_rollout(
            client,
            "cmp",
            {"old"},
            timeout=600,
            _sleep=lambda _s: None,
            _now=_clock(0, 700),
        )


# --- infra2#525: rollout cross-contamination -------------------------------------


def test_wait_for_rollout_ignores_a_record_from_an_unrelated_concurrent_deploy():
    """infra2#525 finding 2: before_ids alone can't tell an unrelated deploy's rollout
    record apart from our own -- if a second, unrelated deploy_compose() call lands
    between our before_ids snapshot and our own trigger, its resulting record is also
    "not in before_ids" and looks new to us. Dokploy gives no correlation id, so the
    only available signal is the start timestamp: a record that started BEFORE we
    called our own deploy_compose() cannot be ours. Construct exactly that: an
    unrelated record appears first (predates our trigger), then our own record
    appears later -- the function must wait past the unrelated one and return ours,
    not report the unrelated deploy's outcome as our own.
    """
    unrelated = {
        "id": "unrelated-deploy",
        "status": "done",
        "createdAt": "2026-07-18T00:00:00.000Z",
    }
    ours = {
        "id": "our-deploy",
        "status": "done",
        "createdAt": "2026-07-18T00:05:00.000Z",
    }
    # poll 1: only the unrelated record has shown up yet; poll 2: ours has landed too.
    snapshots = iter([[unrelated], [unrelated, ours]])
    client = FakeDokploy(deployments=lambda _self: next(snapshots))
    trigger_epoch = parse_epoch_seconds("2026-07-18T00:04:00.000Z")

    record = dp.wait_for_rollout(
        client,
        "cmp",
        set(),
        interval=0,
        _sleep=lambda _s: None,
        _now=_clock(0, 1),
        min_started_at=trigger_epoch,
    )

    assert record["id"] == "our-deploy"


def test_wait_for_rollout_times_out_if_only_an_unrelated_record_ever_appears():
    """Complements the test above: if the ONLY "new" record is the unrelated one and
    our own never shows up, the function must time out rather than falsely report the
    unrelated deploy's "done" status as our own."""
    unrelated = {
        "id": "unrelated-deploy",
        "status": "done",
        "createdAt": "2026-07-18T00:00:00.000Z",
    }
    client = FakeDokploy(deployments=[unrelated])
    trigger_epoch = parse_epoch_seconds("2026-07-18T00:04:00.000Z")

    with pytest.raises(TimeoutError, match="did not finish"):
        dp.wait_for_rollout(
            client,
            "cmp",
            set(),
            timeout=600,
            interval=0,
            _sleep=lambda _s: None,
            _now=_clock(0, 700),
            min_started_at=trigger_epoch,
        )


def test_wait_for_rollout_does_not_exclude_records_with_no_parseable_timestamp():
    """A record with no createdAt/startedAt/updatedAt is ambiguous, not excluded --
    min_started_at only ever rules a record OUT, never rules one in. This keeps
    behavior unchanged for callers/fakes (like the rest of this suite) that don't
    populate timestamps on their deployment records."""
    client = FakeDokploy(deployments=[{"id": "new", "status": "done"}])

    record = dp.wait_for_rollout(
        client,
        "cmp",
        set(),
        interval=0,
        _sleep=lambda _s: None,
        _now=_clock(0, 1),
        min_started_at=parse_epoch_seconds("2026-07-18T00:04:00.000Z"),
    )

    assert record["id"] == "new"


def test_deploy_with_wait_snapshots_before_then_polls_the_new_record():
    # before deploy: only the old record; after deploy_compose: a new done record.
    def deployments(self):
        if self.deployed:
            return [{"id": "old", "status": "done"}, {"id": "new", "status": "done"}]
        return [{"id": "old", "status": "done"}]

    client = FakeDokploy(deployments=deployments)
    plan = dp.deploy(
        "staging", "deadbeef", domain="zitian.party", client=client, wait=True
    )
    assert plan.sha == "deadbeef"
    assert client.deployed == ["A6V-hbJlgHMwgPDoTDnhH"]


def test_deploy_without_wait_does_not_poll_deployments():
    # default wait=False: the rollout poll is never consulted (no get_compose_deployments).
    calls = []
    client = FakeDokploy(deployments=lambda self: calls.append(1) or [])
    dp.deploy("staging", "deadbeef", domain="zitian.party", client=client)
    assert calls == []


# --- bash-parity: env keys, cache-bust, model overrides (P2 step 4b) -------------


def test_deploy_sets_cache_bust_and_static_infra_keys():
    client = FakeDokploy()
    plan = dp.deploy(
        "staging", FULL_SHA, domain="zitian.party", client=client, _now=lambda: 1000
    )
    # IAC_CONFIG_HASH is the per-deploy cache-bust (short sha, forces redeploy even on
    # the same digest)
    assert plan.env_vars["IAC_CONFIG_HASH"] == f"deploy-{SHORT_SHA}-1000000"
    assert plan.env_vars["COMPOSE_PROFILES"] == "app"
    assert plan.env_vars["TRAEFIK_ENABLE"] == "true"
    assert plan.env_vars["INTERNAL_DOMAIN"] == "zitian.party"


def test_cache_bust_differs_for_two_deploys_in_the_same_second():
    # promote-not-rebuild deploys the same sha repeatedly; the hash must still change,
    # even for two deploys within the same wall second (whole-second granularity would
    # collide here and re-introduce a no-op — ms resolution must keep them distinct).
    c1 = FakeDokploy()
    c2 = FakeDokploy()
    h1 = dp.deploy(
        "staging", FULL_SHA, domain="z.p", client=c1, _now=lambda: 1.2
    ).env_vars
    h2 = dp.deploy(
        "staging", FULL_SHA, domain="z.p", client=c2, _now=lambda: 1.8
    ).env_vars
    assert h1["IAC_CONFIG_HASH"] == f"deploy-{SHORT_SHA}-1200"
    assert h2["IAC_CONFIG_HASH"] == f"deploy-{SHORT_SHA}-1800"
    assert h1["IAC_CONFIG_HASH"] != h2["IAC_CONFIG_HASH"]


def test_model_overrides_merged_only_when_non_empty():
    client = FakeDokploy()
    plan = dp.deploy(
        "staging",
        "deadbeef",
        domain="zitian.party",
        client=client,
        model_overrides={
            "PRIMARY_MODEL": "gpt-x",
            "OCR_MODEL": "",
            "VISION_MODEL": None,
        },
    )
    assert plan.env_vars["PRIMARY_MODEL"] == "gpt-x"
    # empty/None overrides must not blank the running model
    assert "OCR_MODEL" not in plan.env_vars
    assert "VISION_MODEL" not in plan.env_vars


# --- bash-parity: vault preflight ------------------------------------------------


def test_preflight_vault_token_skips_when_compose_has_no_token():
    # a compose with no VAULT_APP_TOKEN is left alone (not every compose uses Vault)
    client = FakeDokploy(env_str="IMAGE_TAG=abc\nFOO=bar")
    dp.preflight_vault_token(client, "cmp")  # must not raise / not call vault


def test_preflight_vault_token_skips_for_approle_service(monkeypatch):
    # #369: an AppRole service (VAULT_ROLE_ID/VAULT_SECRET_ID present) must NOT be gated
    # on a vestigial VAULT_APP_TOKEN — it would expire un-renewed and hard-block deploys.
    import libs.env as env_mod

    def _boom(*_a, **_k):
        raise AssertionError("AppRole service must not hit the VAULT_APP_TOKEN check")

    monkeypatch.setattr(env_mod, "verify_vault_token", _boom)
    client = FakeDokploy(
        env_str="VAULT_ROLE_ID=role-x\nVAULT_SECRET_ID=secret-y\nVAULT_APP_TOKEN=hvs.stale"
    )
    dp.preflight_vault_token(client, "cmp")  # must not raise / not call vault


def test_preflight_vault_token_raises_on_expiring_token(monkeypatch):
    import libs.env as env_mod

    monkeypatch.setattr(
        env_mod,
        "verify_vault_token",
        lambda token, addr=None, min_ttl_hours=24: {
            "valid": False,
            "error": "TTL too low",
        },
    )
    client = FakeDokploy(env_str="VAULT_APP_TOKEN=hvs.deadbeef")
    with pytest.raises(RuntimeError, match="VAULT_APP_TOKEN preflight failed"):
        dp.preflight_vault_token(client, "cmp")


# --- #290's fixed staging/prod twin: assert_approle_creds_present ----------------


def test_assert_approle_creds_present_passes_when_all_three_present():
    # finance_report/app's real compose.yaml declares VAULT_ROLE_ID/SECRET_ID; the
    # FakeDokploy default baseline env already carries all three creds.
    dp.assert_approle_creds_present("finance_report/app", FakeDokploy(), "cmp")


def test_assert_approle_creds_present_raises_when_all_missing():
    client = FakeDokploy(env_str="")
    with pytest.raises(ValueError, match="VAULT_ROLE_ID, VAULT_SECRET_ID, VAULT_ADDR"):
        dp.assert_approle_creds_present("truealpha/app", client, "cmp")


def test_assert_approle_creds_present_raises_when_only_addr_missing():
    client = FakeDokploy(env_str="VAULT_ROLE_ID=r\nVAULT_SECRET_ID=s")
    with pytest.raises(ValueError, match="VAULT_ADDR is missing"):
        dp.assert_approle_creds_present("finance_report/app", client, "cmp")


def test_assert_approle_creds_present_skips_a_non_approle_service(
    tmp_path, monkeypatch
):
    compose = tmp_path / "compose.yaml"
    compose.write_text("services:\n  app:\n    image: example\n")
    fake_meta = SimpleNamespace(compose_path=str(compose))
    monkeypatch.setattr(
        "libs.service_registry.service_attrs",
        lambda: {"some/service": fake_meta},
    )
    # Empty env would fail closed if this service used AppRole auth — it doesn't.
    dp.assert_approle_creds_present("some/service", FakeDokploy(env_str=""), "cmp")


def test_assert_approle_creds_present_skips_an_unregistered_service():
    # A service absent from service_attrs() (no static compose file to inspect) is
    # left alone rather than raising — same fail-open-on-unknown as the rest of this
    # module's optional lookups.
    dp.assert_approle_creds_present("not/registered", FakeDokploy(env_str=""), "cmp")


def test_deploy_raises_before_any_mutation_when_approle_creds_missing():
    client = FakeDokploy(env_str="")
    with pytest.raises(ValueError, match="VAULT_ROLE_ID"):
        dp.deploy("staging", "deadbeef", domain="z.p", client=client)
    assert client.updated == []
    assert client.deployed == []
    assert client.branch_updates == []


# --- truealpha#447: deploy() self-heals this service's generated Vault secrets ---


def test_deploy_calls_ensure_generated_secrets_before_any_mutation(monkeypatch):
    # Same "before any mutation" idiom as the AppRole guard above, but for a
    # self-healing call rather than a hard gate: made to raise here purely to
    # prove ordering, not because a real failure raises (it normally doesn't).
    calls = []

    def _record_and_raise(service, env):
        calls.append((service, env))
        raise ValueError("boom")

    monkeypatch.setattr(dp, "ensure_generated_secrets", _record_and_raise)
    client = FakeDokploy()
    with pytest.raises(ValueError, match="boom"):
        dp.deploy(
            "prod",
            "deadbeef",
            domain="truealpha.club",
            client=client,
            service="truealpha/app",
            staging_validated=True,
        )
    # env is resolved (prod -> production) before the call, matching identity's env.
    assert calls == [("truealpha/app", "production")]
    assert client.updated == []
    assert client.deployed == []
    assert client.branch_updates == []


def test_deploy_calls_ensure_generated_secrets_with_the_unmapped_staging_env(
    monkeypatch,
):
    calls = []
    monkeypatch.setattr(
        dp,
        "ensure_generated_secrets",
        lambda service, env: calls.append((service, env)),
    )
    client = FakeDokploy()
    dp.deploy(
        "staging", FULL_SHA, domain="zitian.party", client=client, iac_ref="b" * 40
    )
    assert calls == [("finance_report/app", "staging")]


class _FakeGeneratedSecretsDeployer:
    def __init__(self):
        self.calls: list[str] = []

    def as_class(self):
        calls = self.calls

        class _Deployer:
            @classmethod
            def ensure_runtime_secrets(cls, *, env):
                calls.append(env)
                return True

        return _Deployer


def test_ensure_generated_secrets_calls_the_services_own_deployer(monkeypatch):
    # Uses the directly-imported real function, not dp.ensure_generated_secrets —
    # the autouse fixture above stubs THAT attribute for every test in this file
    # (these three are exactly the tests that want the real body instead).
    recorder = _FakeGeneratedSecretsDeployer()
    monkeypatch.setattr(
        "libs.deploy.deployer.load_deployer_class",
        lambda service: recorder.as_class(),
    )
    _real_ensure_generated_secrets("truealpha/app", "production")
    assert recorder.calls == ["production"]


def test_ensure_generated_secrets_raises_when_provisioning_fails(monkeypatch):
    class _FailingDeployer:
        @classmethod
        def ensure_runtime_secrets(cls, *, env):
            return False

    monkeypatch.setattr(
        "libs.deploy.deployer.load_deployer_class", lambda service: _FailingDeployer
    )
    with pytest.raises(ValueError, match="failed to auto-provision"):
        _real_ensure_generated_secrets("truealpha/app", "staging")


def test_ensure_generated_secrets_is_a_noop_when_the_service_has_no_deployer(
    monkeypatch,
):
    monkeypatch.setattr(
        "libs.deploy.deployer.load_deployer_class", lambda service: None
    )
    _real_ensure_generated_secrets("unregistered/service", "staging")  # must not raise


@pytest.mark.parametrize(
    "exc_attr",
    ["VaultAuthError", "VaultConnectionError"],
)
def test_ensure_generated_secrets_degrades_when_this_context_has_no_vault_access(
    monkeypatch, exc_attr
):
    # Real bug found via a live deploy (not caught by mocked unit tests): the
    # app-deploy-request receiver — promote.deploy()'s actual caller in
    # production — has no VAULT_ROOT_TOKEN (deliberately: DOKPLOY_API_KEY /
    # IAC_WEBHOOK_SECRET only). VaultSecrets._load() raises VaultAuthError
    # immediately in that context. A deploy must still proceed — this context
    # simply cannot perform the self-heal, which is not the same as the
    # self-heal having failed.
    from libs.env import VaultSecrets

    exc_cls = getattr(VaultSecrets, exc_attr)

    class _NoVaultAccessDeployer:
        @classmethod
        def ensure_runtime_secrets(cls, *, env):
            raise exc_cls("no route to Vault in this context")

    monkeypatch.setattr(
        "libs.deploy.deployer.load_deployer_class",
        lambda service: _NoVaultAccessDeployer,
    )
    _real_ensure_generated_secrets("truealpha/app", "production")  # must not raise


def test_deploy_proceeds_when_secret_provisioning_has_no_vault_access(monkeypatch):
    """End-to-end: deploy() itself must not fail closed on this — the whole point
    is that a routine staging/prod deploy keeps working even though its receiver
    has no Vault credentials to self-heal with."""
    from libs.env import VaultSecrets

    class _NoVaultAccessDeployer:
        @classmethod
        def ensure_runtime_secrets(cls, *, env):
            raise VaultSecrets.VaultAuthError("VAULT_ROOT_TOKEN not set")

    monkeypatch.setattr(dp, "ensure_generated_secrets", _real_ensure_generated_secrets)
    monkeypatch.setattr(
        "libs.deploy.deployer.load_deployer_class",
        lambda service: _NoVaultAccessDeployer,
    )
    client = FakeDokploy()
    plan = dp.deploy("staging", FULL_SHA, domain="zitian.party", client=client)
    assert plan.sha == FULL_SHA
    assert client.deployed  # the deploy still happened


def test_deploy_verify_vault_gates_before_any_mutation(monkeypatch):
    import libs.env as env_mod

    monkeypatch.setattr(
        env_mod,
        "verify_vault_token",
        lambda token, addr=None, min_ttl_hours=24: {"valid": False, "error": "expired"},
    )
    # This test isolates the legacy VAULT_APP_TOKEN gate specifically — a real compose
    # with a live, invalid legacy token but NO AppRole creds at all no longer exists
    # post-#257 (every service's compose requires VAULT_ROLE_ID/SECRET_ID to even
    # start), so assert_approle_creds_present would otherwise fire first here on a
    # fixture this test doesn't intend to exercise. See test_assert_approle_creds_*
    # below for that gate's own coverage.
    monkeypatch.setattr(dp, "assert_approle_creds_present", lambda *a, **k: None)
    client = FakeDokploy(env_str="VAULT_APP_TOKEN=hvs.x")
    with pytest.raises(RuntimeError, match="preflight failed"):
        dp.deploy("staging", "deadbeef", domain="z.p", client=client, verify_vault=True)
    # gate runs before mutation: neither env-update nor deploy happened
    assert client.updated == []
    assert client.deployed == []


# --- bash-parity: post-deploy effective-config verify ----------------------------


def test_verify_effective_config_hash_returns_on_match():
    client = FakeDokploy(env_str="IAC_CONFIG_HASH=deploy-abc-1\nX=y")
    got = dp.verify_effective_config_hash(
        client, "cmp", "deploy-abc-1", _sleep=lambda _s: None, _now=_clock(0)
    )
    assert got == "deploy-abc-1"


def test_verify_effective_config_hash_raises_if_never_advances():
    client = FakeDokploy(env_str="IAC_CONFIG_HASH=deploy-OLD-0")
    with pytest.raises(RuntimeError, match="never advanced"):
        dp.verify_effective_config_hash(
            client,
            "cmp",
            "deploy-NEW-1",
            timeout=600,
            _sleep=lambda _s: None,
            _now=_clock(0, 700),
        )


def test_verify_effective_config_hash_tolerates_transient_read_error():
    # first read raises, second returns the match -> must not fail on the blip
    calls = {"n": 0}

    def flaky(_self):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("Dokploy compose.one blip")
        return "IAC_CONFIG_HASH=deploy-abc-1"

    client = FakeDokploy(env_str=flaky)
    got = dp.verify_effective_config_hash(
        client, "cmp", "deploy-abc-1", _sleep=lambda _s: None, _now=_clock(0, 1, 2)
    )
    assert got == "deploy-abc-1"


def test_deploy_verify_config_confirms_pushed_hash_rolled_out():
    # the reflecting fake echoes the pushed env back, so the effective hash matches.
    client = FakeDokploy()
    plan = dp.deploy(
        "staging",
        FULL_SHA,
        domain="zitian.party",
        client=client,
        verify_config=True,
        _now=lambda: 1000,
    )
    assert plan.env_vars["IAC_CONFIG_HASH"] == f"deploy-{SHORT_SHA}-1000000"
    assert client.deployed == ["A6V-hbJlgHMwgPDoTDnhH"]


def test_staging_deploy_injects_openpanel_client_id():
    """#372: the per-env OpenPanel client id reaches the compose env on the live
    deploy_v2/deploy_primitive path (it previously only lived in the unused
    pre_compose, so analytics never started)."""
    from tools.openpanel_clients import OPENPANEL_CLIENTS

    client = FakeDokploy()
    dp.deploy("staging", FULL_SHA, domain="zitian.party", client=client)
    _, env = client.updated[0]
    assert env["OPENPANEL_CLIENT_ID"] == OPENPANEL_CLIENTS["staging"]
    assert env["OPENPANEL_ENVIRONMENT"] == "staging"


def test_prod_deploy_injects_openpanel_client_id_normalizing_prod_alias():
    """`prod` (deploy_v2 naming) maps to the `production` OpenPanel project."""
    from tools.openpanel_clients import OPENPANEL_CLIENTS

    client = FakeDokploy()
    dp.deploy(
        "prod", FULL_SHA, domain="zitian.party", client=client, staging_validated=True
    )
    _, env = client.updated[0]
    assert env["OPENPANEL_CLIENT_ID"] == OPENPANEL_CLIENTS["production"]
    assert env["OPENPANEL_ENVIRONMENT"] == "production"


def test_deploy_emits_platform_snapshot_on_failure(monkeypatch):
    """#768: a failed deploy emits the platform-health snapshot (keyed on the resolved
    compose id) and re-raises the original error unchanged."""
    emitted: list[str] = []
    monkeypatch.setattr(
        dp,
        "emit_failure_snapshot",
        lambda client, compose_id: emitted.append(compose_id),
    )

    class _Boom(FakeDokploy):
        def deploy_compose(self, compose_id):
            raise RuntimeError("dokploy 500")

    with pytest.raises(RuntimeError, match="dokploy 500"):
        dp.deploy(
            "staging", FULL_SHA, domain="zitian.party", client=_Boom(), wait=False
        )

    # snapshot emitted exactly once, keyed on the staging compose id resolved by env_config
    assert len(emitted) == 1
    assert emitted[0]
