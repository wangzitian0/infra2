"""Tests for the deploy primitive deploy(env, code, data) (finance_report#883 P2 step 3)."""

from __future__ import annotations

import pytest

from tools import deploy_primitive as dp

# A realistic full commit sha and its 7-char short form (the tag images are published
# under). resolve_to_sha returns a full sha; IMAGE_TAG must be the short form.
FULL_SHA = "1af32e6daf17e2c58383dd2c0bfaea13bc11e517"
SHORT_SHA = "1af32e6"


class FakeDokploy:
    """Records the compose env-update + deploy calls instead of hitting Dokploy."""

    def __init__(self, deployments=None, env_str=None):
        self.updated: list[tuple[str, dict]] = []
        self.deployed: list[str] = []
        # list, or callable(self) -> list, used by wait_for_rollout's rollout poll.
        self._deployments = deployments
        # str/callable override for get_compose_env; if None, reflect the last push.
        self._env_str = env_str

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
        return ""


def test_staging_deploy_assembles_axes_and_triggers():
    client = FakeDokploy()
    plan = dp.deploy("staging", FULL_SHA, domain="zitian.party", client=client)

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
    assert client.deployed == ["A6V-hbJlgHMwgPDoTDnhH"]


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


def test_data_override_is_passed_through():
    client = FakeDokploy()
    plan = dp.deploy(
        "staging", "deadbeef", domain="zitian.party", client=client, data="empty"
    )
    assert plan.data == "empty"


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


def test_empty_data_override_is_honored_not_silently_defaulted():
    # data="" is an explicit caller value, not a fallback trigger (is-None check)
    client = FakeDokploy()
    plan = dp.deploy(
        "staging", "deadbeef", domain="zitian.party", client=client, data=""
    )
    assert plan.data == ""


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
        dp.wait_for_rollout(client, "cmp", {"old"}, _sleep=lambda _s: None, _now=_clock(0))


def test_wait_for_rollout_times_out_if_no_new_record_finishes():
    # the new record never leaves running; the clock crosses the deadline.
    client = FakeDokploy(deployments=[{"id": "new", "status": "running"}])
    with pytest.raises(TimeoutError, match="did not finish"):
        dp.wait_for_rollout(
            client, "cmp", {"old"}, timeout=600, _sleep=lambda _s: None,
            _now=_clock(0, 700),
        )


def test_wait_for_rollout_ignores_pre_existing_records():
    # only `before_ids`-excluded records count; an old done record must not satisfy it.
    client = FakeDokploy(deployments=[{"id": "old", "status": "done"}])
    with pytest.raises(TimeoutError):
        dp.wait_for_rollout(
            client, "cmp", {"old"}, timeout=600, _sleep=lambda _s: None,
            _now=_clock(0, 700),
        )


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
    h1 = dp.deploy("staging", FULL_SHA, domain="z.p", client=c1, _now=lambda: 1.2).env_vars
    h2 = dp.deploy("staging", FULL_SHA, domain="z.p", client=c2, _now=lambda: 1.8).env_vars
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
        model_overrides={"PRIMARY_MODEL": "gpt-x", "OCR_MODEL": "", "VISION_MODEL": None},
    )
    assert plan.env_vars["PRIMARY_MODEL"] == "gpt-x"
    # empty/None overrides must not blank the running model
    assert "OCR_MODEL" not in plan.env_vars
    assert "VISION_MODEL" not in plan.env_vars


# --- bash-parity: vault preflight ------------------------------------------------


def test_preflight_vault_token_skips_when_compose_has_no_token():
    # a compose with no VAULT_APP_TOKEN is left alone (not every compose uses Vault)
    client = FakeDokploy(env_str="IMAGE_TAG=abc\nFOO=bar")
    dp.preflight_vault_token(client, "cmp", "zitian.party")  # must not raise / not call vault


def test_preflight_vault_token_raises_on_expiring_token(monkeypatch):
    import libs.env as env_mod

    monkeypatch.setattr(
        env_mod, "verify_vault_token",
        lambda token, addr=None, min_ttl_hours=24: {"valid": False, "error": "TTL too low"},
    )
    client = FakeDokploy(env_str="VAULT_APP_TOKEN=hvs.deadbeef")
    with pytest.raises(RuntimeError, match="VAULT_APP_TOKEN preflight failed"):
        dp.preflight_vault_token(client, "cmp", "zitian.party")


def test_deploy_verify_vault_gates_before_any_mutation(monkeypatch):
    import libs.env as env_mod

    monkeypatch.setattr(
        env_mod, "verify_vault_token",
        lambda token, addr=None, min_ttl_hours=24: {"valid": False, "error": "expired"},
    )
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
            client, "cmp", "deploy-NEW-1", timeout=600,
            _sleep=lambda _s: None, _now=_clock(0, 700),
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
        "staging", FULL_SHA, domain="zitian.party", client=client,
        verify_config=True, _now=lambda: 1000,
    )
    assert plan.env_vars["IAC_CONFIG_HASH"] == f"deploy-{SHORT_SHA}-1000000"
    assert client.deployed == ["A6V-hbJlgHMwgPDoTDnhH"]
