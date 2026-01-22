"""Unit tests for libs/env.py (OpSecrets/VaultSecrets)."""

from unittest.mock import patch, MagicMock


class TestOpSecrets:
    """Test OpSecrets behavior"""

    @patch("libs.env.subprocess.run")
    def test_op_get_all_filters_fields(self, mock_run):
        from libs.env import OpSecrets
        import json

        mock_result = MagicMock()
        mock_result.stdout = json.dumps(
            {
                "fields": [
                    {"label": "username", "value": "admin"},
                    {"label": "POSTGRES_PASSWORD", "value": "secret"},
                    {"label": "notesPlain", "value": "skip this"},
                    {"label": "password", "value": "skip this too"},
                ]
            }
        )
        mock_run.return_value = mock_result

        op = OpSecrets(item="init/env_vars")
        result = op.get_all()

        assert result == {"POSTGRES_PASSWORD": "secret"}

    @patch("libs.env.subprocess.run")
    def test_op_get_single_field(self, mock_run):
        from libs.env import OpSecrets
        import json

        mock_result = MagicMock()
        mock_result.stdout = json.dumps(
            {
                "fields": [
                    {"label": "VPS_HOST", "value": "10.0.0.1"},
                ]
            }
        )
        mock_run.return_value = mock_result

        op = OpSecrets(item="init/env_vars")
        assert op.get("VPS_HOST") == "10.0.0.1"

    @patch("libs.env.subprocess.run")
    def test_op_set_success(self, mock_run):
        from libs.env import OpSecrets

        mock_run.return_value = MagicMock()
        op = OpSecrets(item="init/env_vars")

        assert op.set("VPS_HOST", "10.0.0.2") is True

    @patch("libs.env.subprocess.run")
    def test_op_set_failure(self, mock_run):
        from libs.env import OpSecrets
        from subprocess import CalledProcessError

        mock_run.side_effect = CalledProcessError(1, "op")
        op = OpSecrets(item="init/env_vars")

        assert op.set("VPS_HOST", "10.0.0.2") is False


class TestVaultSecrets:
    """Test VaultSecrets behavior"""

    @patch("libs.env.httpx.Client")
    def test_vault_get_all_success(self, mock_client):
        from libs.env import VaultSecrets

        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"data": {"data": {"password": "secret"}}}

        client = MagicMock()
        client.get.return_value = resp
        mock_client.return_value.__enter__.return_value = client

        secrets = VaultSecrets(
            path="platform/production/postgres",
            token="token",
            addr="https://vault.example",
        )
        assert secrets.get_all() == {"password": "secret"}

    @patch("libs.env.httpx.Client")
    def test_vault_get_all_non_200(self, mock_client):
        from libs.env import VaultSecrets

        resp = MagicMock()
        resp.status_code = 403

        client = MagicMock()
        client.get.return_value = resp
        mock_client.return_value.__enter__.return_value = client

        secrets = VaultSecrets(
            path="platform/production/postgres",
            token="token",
            addr="https://vault.example",
        )
        assert secrets.get_all() == {}

    @patch("libs.env.httpx.Client")
    def test_vault_set_merges_existing(self, mock_client):
        from libs.env import VaultSecrets

        get_resp = MagicMock()
        get_resp.status_code = 200
        get_resp.json.return_value = {"data": {"data": {"existing": "value"}}}

        post_resp = MagicMock()
        post_resp.status_code = 204

        client = MagicMock()
        client.get.return_value = get_resp
        client.post.return_value = post_resp
        mock_client.return_value.__enter__.return_value = client

        secrets = VaultSecrets(
            path="platform/production/postgres",
            token="token",
            addr="https://vault.example",
        )
        assert secrets.set("new", "value") is True


class TestGetSecrets:
    """Test get_secrets factory"""

    def test_get_secrets_default_returns_vault(self):
        from libs.env import get_secrets, VaultSecrets

        result = get_secrets("platform", "postgres", "production")
        assert isinstance(result, VaultSecrets)

    def test_get_secrets_app_vars_returns_vault(self):
        from libs.env import get_secrets, VaultSecrets

        result = get_secrets("platform", "postgres", "production", credential_type="app_vars")
        assert isinstance(result, VaultSecrets)

    def test_get_secrets_bootstrap_returns_op(self):
        from libs.env import get_secrets, OpSecrets

        result = get_secrets("bootstrap", "vault", credential_type="bootstrap")
        assert isinstance(result, OpSecrets)

    def test_get_secrets_root_vars_returns_op(self):
        from libs.env import get_secrets, OpSecrets

        result = get_secrets("platform", "postgres", "production", credential_type="root_vars")
        assert isinstance(result, OpSecrets)

    def test_get_secrets_bootstrap_path_no_env(self):
        from libs.env import get_secrets

        result = get_secrets("bootstrap", "vault", credential_type="bootstrap")
        assert result.item == "bootstrap/vault"

    def test_get_secrets_root_vars_path_includes_env(self):
        from libs.env import get_secrets

        result = get_secrets("platform", "postgres", "production", credential_type="root_vars")
        assert result.item == "platform/production/postgres"

    def test_get_secrets_app_vars_path_includes_env(self):
        from libs.env import get_secrets

        result = get_secrets("platform", "postgres", "production", credential_type="app_vars")
        assert result.path == "platform/production/postgres"


class TestGetSecretsValidation:
    """Test get_secrets input validation"""

    def test_project_with_dash_raises(self):
        import pytest
        from libs.env import get_secrets

        with pytest.raises(ValueError, match="must not include"):
            get_secrets("my-project", "postgres", "production")

    def test_project_with_slash_raises(self):
        import pytest
        from libs.env import get_secrets

        with pytest.raises(ValueError, match="must not include"):
            get_secrets("my/project", "postgres", "production")

    def test_env_with_dash_raises(self):
        import pytest
        from libs.env import get_secrets

        with pytest.raises(ValueError, match="must not include"):
            get_secrets("platform", "postgres", "prod-staging")

    def test_service_with_dash_raises(self):
        import pytest
        from libs.env import get_secrets

        with pytest.raises(ValueError, match="must not include"):
            get_secrets("platform", "my-postgres", "production")

    def test_empty_project_raises(self):
        import pytest
        from libs.env import get_secrets

        with pytest.raises(ValueError, match="must not be empty"):
            get_secrets("", "postgres", "production")

    def test_whitespace_only_project_raises(self):
        import pytest
        from libs.env import get_secrets

        with pytest.raises(ValueError, match="must not be empty"):
            get_secrets("   ", "postgres", "production")


class TestGeneratePassword:
    """Test password generation"""

    def test_generate_password_default_length(self):
        from libs.env import generate_password

        pwd = generate_password()
        assert len(pwd) == 24

    def test_generate_password_custom_length(self):
        from libs.env import generate_password

        pwd = generate_password(32)
        assert len(pwd) == 32

    def test_generate_password_alphanumeric(self):
        from libs.env import generate_password

        pwd = generate_password(100)
        assert pwd.isalnum()
