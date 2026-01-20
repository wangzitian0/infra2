"""Unit tests for libs/config.py (secrets wrapper)."""

from unittest.mock import patch, MagicMock


class TestConfigInit:
    """Test Config initialization"""

    @patch("libs.config.get_secrets")
    def test_init_creates_backends(self, mock_get_secrets):
        from libs.config import Config

        mock_get_secrets.side_effect = [MagicMock(), MagicMock()]
        config = Config("platform", "production", "postgres")

        assert config._service_secrets is not None
        assert config._env_secrets is not None
        assert mock_get_secrets.call_count == 2


class TestConfigKeyParsing:
    """Test Dokploy-style key parsing"""

    def test_parse_simple_key(self):
        from libs.config import Config

        config = Config("platform", "production")
        level, key = config._parse_key("PASSWORD")
        assert level == "service"
        assert key == "PASSWORD"

    def test_parse_project_key(self):
        from libs.config import Config

        config = Config("platform", "production")
        level, key = config._parse_key("project.DATABASE_URL")
        assert level == "project"
        assert key == "DATABASE_URL"

    def test_parse_environment_key(self):
        from libs.config import Config

        config = Config("platform", "production")
        level, key = config._parse_key("environment.API_KEY")
        assert level == "environment"
        assert key == "API_KEY"

    def test_parse_service_key(self):
        from libs.config import Config

        config = Config("platform", "production")
        level, key = config._parse_key("service.PASSWORD")
        assert level == "service"
        assert key == "PASSWORD"

    def test_parse_key_with_dots_in_value(self):
        from libs.config import Config

        config = Config("platform", "production")
        level, key = config._parse_key("custom.key.name")
        assert level == "service"
        assert key == "custom.key.name"


class TestConfigGet:
    """Test Config.get() behavior"""

    @patch("libs.config.get_secrets")
    def test_get_prefers_service(self, mock_get_secrets):
        from libs.config import Config

        service_secrets = MagicMock()
        env_secrets = MagicMock()
        service_secrets.get.return_value = "service_value"
        env_secrets.get.return_value = "env_value"
        mock_get_secrets.side_effect = [service_secrets, env_secrets]

        config = Config("platform", "production", "postgres")
        result = config.get("PASSWORD")

        assert result == "service_value"

    @patch("libs.config.get_secrets")
    def test_get_falls_back_to_env(self, mock_get_secrets):
        from libs.config import Config

        service_secrets = MagicMock()
        env_secrets = MagicMock()
        service_secrets.get.return_value = None
        env_secrets.get.return_value = "env_value"
        mock_get_secrets.side_effect = [service_secrets, env_secrets]

        config = Config("platform", "production", "postgres")
        result = config.get("PASSWORD")

        assert result == "env_value"

    @patch("libs.config.get_secrets")
    def test_get_project_level_direct(self, mock_get_secrets):
        from libs.config import Config

        service_secrets = MagicMock()
        env_secrets = MagicMock()
        env_secrets.get.return_value = "project_value"
        mock_get_secrets.side_effect = [service_secrets, env_secrets]

        config = Config("platform", "production", "postgres")
        result = config.get("project.DATABASE_URL")

        assert result == "project_value"

    @patch("libs.config.get_secrets")
    def test_get_returns_default_when_missing(self, mock_get_secrets):
        from libs.config import Config

        service_secrets = MagicMock()
        env_secrets = MagicMock()
        service_secrets.get.return_value = None
        env_secrets.get.return_value = None
        mock_get_secrets.side_effect = [service_secrets, env_secrets]

        config = Config("platform", "production", "postgres")
        result = config.get("MISSING_KEY", default="default_value")

        assert result == "default_value"


class TestConfigSecrets:
    """Test Config secrets helpers"""

    @patch("libs.config.get_secrets")
    def test_get_secret_matches_get(self, mock_get_secrets):
        from libs.config import Config

        service_secrets = MagicMock()
        env_secrets = MagicMock()
        service_secrets.get.return_value = "secret_value"
        mock_get_secrets.side_effect = [service_secrets, env_secrets]

        config = Config("platform", "production", "postgres")
        result = config.get_secret("PASSWORD")

        assert result == "secret_value"

    @patch("libs.config.get_secrets")
    def test_all_returns_service_level(self, mock_get_secrets):
        from libs.config import Config

        service_secrets = MagicMock()
        env_secrets = MagicMock()
        service_secrets.get_all.return_value = {"KEY1": "val1"}
        env_secrets.get_all.return_value = {"KEY2": "val2"}
        mock_get_secrets.side_effect = [service_secrets, env_secrets]

        config = Config("platform", "production", "postgres")
        result = config.all("service")

        assert result == {"KEY1": "val1"}

    @patch("libs.config.get_secrets")
    def test_merged_combines_env_and_service(self, mock_get_secrets):
        from libs.config import Config

        service_secrets = MagicMock()
        env_secrets = MagicMock()
        env_secrets.get_all.return_value = {"ENV": "env_val"}
        service_secrets.get_all.return_value = {"SVC": "svc_val"}
        mock_get_secrets.side_effect = [service_secrets, env_secrets]

        config = Config("platform", "production", "postgres")
        result = config.merged()

        assert result == {"ENV": "env_val", "SVC": "svc_val"}
