"""Unit tests for deployer safety checks."""

from unittest.mock import MagicMock


def test_preserve_runtime_env_keeps_existing_vault_app_token() -> None:
    """Infra-011.3: deployer updates must not erase injected Vault app tokens."""
    from libs.deployer import _preserve_runtime_env

    result = _preserve_runtime_env(
        "ENV=production\nINTERNAL_DOMAIN=zitian.party",
        "ENV=old\nVAULT_APP_TOKEN=hvs.existing\nSTALE=value",
    )

    assert result.splitlines() == [
        "ENV=production",
        "INTERNAL_DOMAIN=zitian.party",
        "VAULT_APP_TOKEN=hvs.existing",
    ]


def test_preserve_runtime_env_keeps_explicit_new_vault_app_token() -> None:
    from libs.deployer import _preserve_runtime_env

    result = _preserve_runtime_env(
        "ENV=production\nVAULT_APP_TOKEN=hvs.new",
        "VAULT_APP_TOKEN=hvs.existing",
    )

    assert result.splitlines() == ["ENV=production", "VAULT_APP_TOKEN=hvs.new"]


class TestDeployerVaultTokenPreflight:
    """Deployment sync must fail before touching runtime when Vault tokens are bad."""

    def _deployer(self):
        from libs.deployer import Deployer

        class DummyDeployer(Deployer):
            service = "app"
            project = "finance_report"
            compose_path = "finance_report/finance_report/10.app/compose.yaml"
            data_path = ""
            secret_key = ""

        return DummyDeployer

    def test_sync_fails_when_vault_token_is_invalid(self, monkeypatch):
        import libs.deployer as deployer

        dummy = self._deployer()
        monkeypatch.setattr(deployer, "validate_env", lambda: [])
        monkeypatch.setattr(
            dummy,
            "verify_vault_app_token",
            classmethod(lambda cls: {"valid": False, "details": "Token invalid"}),
        )

        result = dummy.sync(MagicMock())

        assert result["action"] == "failed"
        assert "VAULT_APP_TOKEN issue" in result["details"]
        assert "Token invalid" in result["details"]

    def test_sync_fails_when_vault_token_ttl_is_low(self, monkeypatch):
        import libs.deployer as deployer

        dummy = self._deployer()
        monkeypatch.setattr(deployer, "validate_env", lambda: [])
        monkeypatch.setattr(
            dummy,
            "verify_vault_app_token",
            classmethod(lambda cls: {"valid": True, "ttl_hours": 12}),
        )

        result = dummy.sync(MagicMock())

        assert result["action"] == "failed"
        assert "expires in 12h" in result["details"]

    def test_sync_fails_when_vault_token_cannot_be_verified(self, monkeypatch):
        import libs.deployer as deployer

        dummy = self._deployer()
        monkeypatch.setattr(deployer, "validate_env", lambda: [])

        def raise_error():
            raise RuntimeError("Vault unavailable")

        monkeypatch.setattr(dummy, "verify_vault_app_token", classmethod(lambda cls: raise_error()))

        result = dummy.sync(MagicMock())

        assert result["action"] == "failed"
        assert "Could not verify VAULT_APP_TOKEN" in result["details"]
        assert "Vault unavailable" in result["details"]
