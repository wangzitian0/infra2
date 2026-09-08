"""Unit tests for libs/env.py: the OpSecrets/VaultSecrets shims over the infra2-sdk
backends (plan PR-E). The backends themselves are tested in the SDK; these tests pin
the shim contract that tasks and Deployers rely on."""

import pytest

from infra2_sdk.secrets import SecretsError, WriteResult


class FakeBackend:
    def __init__(self, items=None, *, fail=None):
        self.items = {k: dict(v) for k, v in (items or {}).items()}
        self.fail = fail
        self.writes = []

    def read(self, path):
        if self.fail:
            raise SecretsError(self.fail)
        return dict(self.items.get(path, {}))

    def write(self, path, values):
        if self.fail:
            raise SecretsError(self.fail)
        current = self.items.setdefault(path, {})
        changed = tuple(k for k, v in values.items() if current.get(k) != v)
        current.update(values)
        self.writes.append((path, dict(values)))
        return WriteResult(changed=changed)


class TestOpSecrets:
    """OpSecrets = one 1Password item read through the SDK's ``op`` adapter."""

    def test_op_get_all_filters_builtin_fields(self):
        from libs.env import OpSecrets

        backend = FakeBackend(
            {
                "init/env_vars": {
                    "DOMAIN": "example.com",
                    "notesPlain": "notes",
                    "password": "x",
                    "username": "y",
                }
            }
        )
        assert OpSecrets(backend=backend).get_all() == {"DOMAIN": "example.com"}

    def test_op_get_single_field_and_caches_the_item(self):
        from libs.env import OpSecrets

        backend = FakeBackend({"bootstrap/vault": {"TOKEN": "t"}})
        op = OpSecrets(item="bootstrap/vault", backend=backend)
        assert op.get("TOKEN") == "t"
        backend.items["bootstrap/vault"]["TOKEN"] = "changed-behind-our-back"
        assert op.get("TOKEN") == "t"  # one read per instance, like before
        assert op.get("MISSING") is None

    def test_op_set_writes_the_item_and_drops_the_cache(self):
        from libs.env import OpSecrets

        backend = FakeBackend({"platform/production/alerting": {"A": "1"}})
        op = OpSecrets(item="platform/production/alerting", backend=backend)
        assert op.get("A") == "1"
        assert op.set("B", "2") is True
        assert backend.writes == [("platform/production/alerting", {"B": "2"})]
        assert op.get_all() == {"A": "1", "B": "2"}

    def test_op_failures_degrade_to_empty_reads_and_false_writes(self, capsys):
        from libs.env import OpSecrets

        op = OpSecrets(backend=FakeBackend(fail="op: not signed in"))
        assert op.get_all() == {}
        assert op.set("K", "v") is False
        assert "not signed in" in capsys.readouterr().err


class TestOpSecretsWithoutTheBinary:
    """CI runners have no `op`: reads are empty and writes are False, never a crash."""

    def test_missing_op_binary_degrades_like_before(self, capsys):
        from libs.env import OpSecrets

        class NoBinary:
            def read(self, path):
                raise FileNotFoundError(2, "No such file or directory", "op")

            def write(self, path, values):
                raise FileNotFoundError(2, "No such file or directory", "op")

        op = OpSecrets(backend=NoBinary())
        assert op.get_all() == {}
        assert op.set("K", "v") is False
        assert "op" in capsys.readouterr().err


class TestVaultSecrets:
    """VaultSecrets = one KV v2 path; errors keep their historical classes."""

    def test_vault_get_all_success(self):
        from libs.env import VaultSecrets

        backend = FakeBackend({"platform/production/postgres": {"PASSWORD": "p"}})
        vault = VaultSecrets(path="platform/production/postgres", backend=backend)
        assert vault.get_all() == {"PASSWORD": "p"}
        assert vault.get("PASSWORD") == "p"

    def test_vault_missing_path_raises_not_found(self):
        from libs.env import VaultSecrets

        vault = VaultSecrets(path="platform/production/nothing", backend=FakeBackend())
        with pytest.raises(VaultSecrets.VaultSecretNotFoundError):
            vault.get("ANY")

    def test_vault_permission_denied_raises_auth_error(self):
        from libs.env import VaultSecrets

        vault = VaultSecrets(
            path="platform/production/postgres",
            backend=FakeBackend(
                fail="vault read platform/production/postgres: HTTP 403"
            ),
        )
        with pytest.raises(VaultSecrets.VaultAuthError):
            vault.get_all()

    def test_vault_sealed_raises_connection_error(self):
        from libs.env import VaultSecrets

        vault = VaultSecrets(
            path="platform/production/postgres",
            backend=FakeBackend(fail="vault read: HTTP 503"),
        )
        with pytest.raises(VaultSecrets.VaultConnectionError):
            vault.get_all()

    def test_vault_set_is_a_merge_patch_of_one_key(self):
        from libs.env import VaultSecrets

        backend = FakeBackend({"platform/production/postgres": {"A": "1"}})
        vault = VaultSecrets(path="platform/production/postgres", backend=backend)
        assert vault.set("B", "2") is True
        # the backend merges (KV v2 patch); the shim never re-sends the other keys
        assert backend.writes == [("platform/production/postgres", {"B": "2"})]
        assert vault.get_all() == {"A": "1", "B": "2"}

    def test_vault_set_creates_missing_secret_path(self):
        from libs.env import VaultSecrets

        backend = FakeBackend()
        vault = VaultSecrets(path="platform/production/new", backend=backend)
        assert vault.set("K", "v") is True
        assert vault.get_all() == {"K": "v"}

    def test_vault_without_a_token_fails_closed_with_the_new_guidance(
        self, monkeypatch
    ):
        from libs.env import VaultSecrets

        monkeypatch.delenv("VAULT_TOKEN", raising=False)
        monkeypatch.delenv("VAULT_ROOT_TOKEN", raising=False)
        with pytest.raises(VaultSecrets.VaultAuthError, match="VAULT_TOKEN not set"):
            VaultSecrets(path="platform/production/postgres").get_all()

    def test_vault_token_prefers_vault_token_over_the_transition_alias(self):
        from libs.env import vault_token

        assert vault_token({"VAULT_TOKEN": "a", "VAULT_ROOT_TOKEN": "b"}) == "a"
        assert vault_token({"VAULT_ROOT_TOKEN": "b"}) == "b"
        assert vault_token({}) is None


class TestGetSecrets:
    """Test get_secrets factory"""

    def test_get_secrets_default_returns_vault(self):
        from libs.env import get_secrets, VaultSecrets

        result = get_secrets("platform", "postgres", "production")
        assert isinstance(result, VaultSecrets)

    def test_get_secrets_app_vars_returns_vault(self):
        from libs.env import get_secrets, VaultSecrets

        result = get_secrets(
            "platform", "postgres", "production", credential_type="app_vars"
        )
        assert isinstance(result, VaultSecrets)

    def test_get_secrets_bootstrap_returns_op(self):
        from libs.env import get_secrets, OpSecrets

        result = get_secrets("bootstrap", "vault", credential_type="bootstrap")
        assert isinstance(result, OpSecrets)

    def test_get_secrets_root_vars_returns_op(self):
        from libs.env import get_secrets, OpSecrets

        result = get_secrets(
            "platform", "postgres", "production", credential_type="root_vars"
        )
        assert isinstance(result, OpSecrets)

    def test_get_secrets_bootstrap_path_no_env(self):
        from libs.env import get_secrets

        result = get_secrets("bootstrap", "vault", credential_type="bootstrap")
        assert result.item == "bootstrap/vault"

    def test_get_secrets_root_vars_path_includes_env(self):
        from libs.env import get_secrets

        result = get_secrets(
            "platform", "postgres", "production", credential_type="root_vars"
        )
        assert result.item == "platform/production/postgres"

    def test_get_secrets_app_vars_path_includes_env(self):
        from libs.env import get_secrets

        result = get_secrets(
            "platform", "postgres", "production", credential_type="app_vars"
        )
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
