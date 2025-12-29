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
    def secrets(self):
        from libs.env import get_secrets
        return get_secrets('platform', 'e2e_test', 'production')
    
    def test_vault_write_and_read(self, secrets):
        """Test writing and reading from Vault"""
        import uuid
        test_key = f"e2e_test_key_{uuid.uuid4().hex[:8]}"
        test_value = f"e2e_test_value_{uuid.uuid4().hex[:8]}"
        
        # Write
        result = secrets.set(test_key, test_value)
        assert result == True, "Failed to write to Vault"
        
        # Read
        retrieved = secrets.get(test_key)
        assert retrieved == test_value, f"Expected {test_value}, got {retrieved}"
        
        # Cleanup: we don't delete, just let it be overwritten next time
    
    def test_vault_get_nonexistent_key(self, secrets):
        """Test getting a key that doesn't exist"""
        result = secrets.get('nonexistent_key_12345')
        assert result is None


class Test1PasswordE2E:
    """E2E tests for 1Password operations"""
    
    @pytest.fixture
    def secrets(self):
        from libs.env import OpSecrets
        return OpSecrets(item='bootstrap/e2e_test')
    
    @pytest.mark.skip(reason="Requires specific 1Password vault setup")
    def test_1password_read(self, secrets):
        """Test reading from 1Password"""
        # This would require a pre-configured item in 1Password
        result = secrets.get('test_key')
        assert result is not None


class TestGenerateAndStoreE2E:
    """E2E tests for secret generation + storage"""
    
    @pytest.fixture
    def secrets(self):
        from libs.env import get_secrets
        return get_secrets('platform', 'e2e_test', 'production')
    
    def test_generate_and_store_creates_password(self, secrets):
        """Test generating and storing a password in Vault"""
        import uuid
        from libs.env import generate_password
        test_key = f"e2e_generated_{uuid.uuid4().hex[:8]}"
        
        # Generate and store
        password = generate_password(16)
        result = secrets.set(test_key, password)
        assert result is True
        assert len(password) == 16
        assert password.isalnum()
        
        # Verify it's stored
        retrieved = secrets.get(test_key)
        assert retrieved == password
