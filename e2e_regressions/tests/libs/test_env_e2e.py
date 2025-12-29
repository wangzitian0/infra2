"""E2E tests for libs/env.py against real services

These tests require:
- 1Password CLI signed in (op signin)
- Vault authenticated (vault login)
- Network access to services

Run with: pytest e2e_regressions/tests/libs/ -v
"""
import pytest
import os


# Skip all tests if not in CI or explicit E2E mode
pytestmark = pytest.mark.skipif(
    os.environ.get('E2E_LIBS_TEST') != 'true',
    reason="E2E libs tests disabled. Set E2E_LIBS_TEST=true to run."
)


class TestVaultE2E:
    """E2E tests for Vault operations"""
    
    @pytest.fixture
    def env_manager(self):
        from libs.env import EnvManager
        return EnvManager('platform', 'production', 'e2e_test')
    
    def test_vault_write_and_read(self, env_manager):
        """Test writing and reading from Vault"""
        import uuid
        test_key = f"e2e_test_key_{uuid.uuid4().hex[:8]}"
        test_value = f"e2e_test_value_{uuid.uuid4().hex[:8]}"
        
        # Write
        result = env_manager.set_secret(test_key, test_value)
        assert result == True, "Failed to write to Vault"
        
        # Read
        retrieved = env_manager.get_secret(test_key)
        assert retrieved == test_value, f"Expected {test_value}, got {retrieved}"
        
        # Cleanup: we don't delete, just let it be overwritten next time
    
    def test_vault_get_nonexistent_key(self, env_manager):
        """Test getting a key that doesn't exist"""
        result = env_manager.get_secret('nonexistent_key_12345')
        assert result is None


class Test1PasswordE2E:
    """E2E tests for 1Password operations"""
    
    @pytest.fixture
    def env_manager(self):
        from libs.env import EnvManager
        return EnvManager('bootstrap', 'production', 'e2e_test')
    
    @pytest.mark.skip(reason="Requires specific 1Password vault setup")
    def test_1password_read(self, env_manager):
        """Test reading from 1Password"""
        # This would require a pre-configured item in 1Password
        result = env_manager.get_secret('test_key')
        assert result is not None


class TestGenerateAndStoreE2E:
    """E2E tests for generate_and_store_secret"""
    
    @pytest.fixture
    def env_manager(self):
        from libs.env import EnvManager
        return EnvManager('platform', 'production', 'e2e_test')
    
    def test_generate_and_store_creates_password(self, env_manager):
        """Test generating and storing a password in Vault"""
        import uuid
        test_key = f"e2e_generated_{uuid.uuid4().hex[:8]}"
        
        # Generate and store
        password = env_manager.generate_and_store_secret(test_key, length=16)
        assert len(password) == 16
        assert password.isalnum()
        
        # Verify it's stored
        retrieved = env_manager.get_secret(test_key)
        assert retrieved == password
