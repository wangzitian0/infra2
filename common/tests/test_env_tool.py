"""invoke env.set: 1Password writes are routine, Vault writes are break-glass (PR-E)."""

from __future__ import annotations

from tools.env_tool import vault_write_guidance, write_secret


class FakeStore:
    def __init__(self, ok=True):
        self.ok = ok
        self.calls = []

    def set(self, key, value):
        self.calls.append((key, value))
        return self.ok


def test_vault_writes_are_refused_with_guidance_by_default() -> None:
    store = FakeStore()
    ok, message = write_secret(
        "platform",
        "alerting",
        "staging",
        None,
        "FEISHU_WEBHOOK_URL",
        "x",
        secrets_factory=lambda *a, **k: store,
    )
    assert ok is False
    assert store.calls == []
    assert "platform/staging/alerting" in message and "--break-glass" in message
    assert message == vault_write_guidance(
        "platform", "alerting", "staging", "FEISHU_WEBHOOK_URL"
    )


def test_break_glass_writes_and_warns() -> None:
    store = FakeStore()
    warnings: list[str] = []
    ok, message = write_secret(
        "platform",
        "alerting",
        "staging",
        "app_vars",
        "K",
        "v",
        break_glass=True,
        secrets_factory=lambda *a, **k: store,
        warn=warnings.append,
    )
    assert ok is True and message == "Set K"
    assert store.calls == [("K", "v")]
    assert (
        warnings
        and "BREAK-GLASS" in warnings[0]
        and "v" not in warnings[0].split("by hand")[0].split("K")[-1]
    )


def test_1password_writes_need_no_flag() -> None:
    store = FakeStore()
    seen = {}

    def factory(project, service, env, credential_type=None):
        seen.update(
            project=project, service=service, env=env, credential_type=credential_type
        )
        return store

    ok, _ = write_secret(
        "platform",
        "alerting",
        "staging",
        "root_vars",
        "K",
        "v",
        secrets_factory=factory,
    )
    assert ok is True
    assert seen["credential_type"] == "root_vars" and store.calls == [("K", "v")]


def test_failed_backend_write_is_reported() -> None:
    ok, message = write_secret(
        "bootstrap",
        "vault",
        "production",
        "bootstrap",
        "K",
        "v",
        secrets_factory=lambda *a, **k: FakeStore(ok=False),
    )
    assert ok is False and message == "Failed to set K"


def test_env_set_accepts_the_documented_type_spelling(monkeypatch) -> None:
    """docs/ssot say `--type=`; invoke derives `--credential-type` from the parameter name.
    Both spellings must reach the same backend selection."""
    import tools.env_tool as env_tool

    seen: list = []

    class Store:
        def set(self, key, value):
            seen.append((key, value))
            return True

    monkeypatch.setattr(
        env_tool,
        "get_secrets",
        lambda p, s, e, credential_type=None: seen.append(credential_type) or Store(),
    )
    env_tool.set_secret.body(
        None, "K=v", project="platform", service="app", env="staging", type="root_vars"
    )
    assert seen == ["root_vars", ("K", "v")]
