"""libs.common: DEPLOY_ENV is the only input of get_env(); switching it in-process works."""

from __future__ import annotations

import libs.common as common


def test_set_deploy_env_resets_the_memoized_config(monkeypatch) -> None:
    class FakeOp:
        def get(self, key):
            return None

    monkeypatch.setattr("libs.env.OpSecrets", lambda *a, **k: FakeOp())
    monkeypatch.setenv("DEPLOY_ENV", "production")
    monkeypatch.delenv("ENV_SUFFIX", raising=False)
    monkeypatch.setattr(common, "_env_cache", None)
    assert common.get_env()["ENV"] == "production"
    common.set_deploy_env("staging")
    env = common.get_env()
    assert (env["ENV"], env["ENV_SUFFIX"], env["ENV_DOMAIN_SUFFIX"]) == (
        "staging",
        "-staging",
        "-staging",
    )
    common.set_deploy_env("production")
    assert (common.get_env()["ENV"], common.get_env()["ENV_SUFFIX"]) == (
        "production",
        "",
    )
    monkeypatch.setattr(common, "_env_cache", None)
