"""Unit tests for libs/env.py

Uses mock to isolate from external services (1Password, Vault, Dokploy).
"""
import pytest
from unittest.mock import patch, MagicMock
import json


class TestEnvManagerInit:
    """Test EnvManager initialization"""
    
    def test_init_platform_project(self):
        from libs.env import EnvManager, SSOT_CONFIG
        mgr = EnvManager('platform', 'production', 'postgres')
        assert mgr.project == 'platform'
        assert mgr.env == 'production'
        assert mgr.service == 'postgres'
        assert mgr._config == SSOT_CONFIG['platform']
    
    def test_init_bootstrap_project(self):
        from libs.env import EnvManager, SSOT_CONFIG
        mgr = EnvManager('bootstrap', 'production', 'vault')
        assert mgr._config == SSOT_CONFIG['bootstrap']
    
    def test_init_unknown_project_defaults_to_platform(self):
        from libs.env import EnvManager, SSOT_CONFIG
        mgr = EnvManager('unknown_project', 'staging')
        assert mgr._config == SSOT_CONFIG['platform']


class TestEnvManagerPaths:
    """Test path generation"""
    
    def test_path_project_level(self):
        from libs.env import EnvManager
        mgr = EnvManager('platform', 'production', 'postgres')
        assert mgr._get_path('project') == 'platform'
    
    def test_path_environment_level(self):
        from libs.env import EnvManager
        mgr = EnvManager('platform', 'production', 'postgres')
        assert mgr._get_path('environment') == 'platform/production'
    
    def test_path_service_level(self):
        from libs.env import EnvManager
        mgr = EnvManager('platform', 'production', 'postgres')
        assert mgr._get_path('service') == 'platform/production/postgres'
    
    def test_path_service_level_no_service(self):
        from libs.env import EnvManager
        mgr = EnvManager('platform', 'production')
        assert mgr._get_path('service') == 'platform/production'


class TestVaultOperations:
    """Test Vault operations with mocked subprocess"""
    
    @patch('libs.env.subprocess.run')
    def test_vault_get_all_success(self, mock_run):
        from libs.env import EnvManager
        
        # Mock successful vault kv get
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps({
            "data": {"data": {"password": "secret123", "host": "localhost"}}
        })
        mock_run.return_value = mock_result
        
        mgr = EnvManager('platform', 'production', 'postgres')
        result = mgr._vault_get_all()
        
        assert result == {"password": "secret123", "host": "localhost"}
    
    @patch('libs.env.subprocess.run')
    def test_vault_get_all_failure(self, mock_run):
        from libs.env import EnvManager
        from subprocess import CalledProcessError
        
        mock_run.side_effect = CalledProcessError(1, 'vault')
        
        mgr = EnvManager('platform', 'production', 'postgres')
        result = mgr._vault_get_all()
        
        assert result == {}
    
    @patch('libs.env.subprocess.run')
    def test_vault_get_single_key(self, mock_run):
        from libs.env import EnvManager
        
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps({
            "data": {"data": {"password": "secret123"}}
        })
        mock_run.return_value = mock_result
        
        mgr = EnvManager('platform', 'production', 'postgres')
        result = mgr._vault_get('password')
        
        assert result == "secret123"
    
    @patch('libs.env.subprocess.run')
    def test_vault_set_merges_with_existing(self, mock_run):
        from libs.env import EnvManager
        
        # First call: get existing secrets
        get_result = MagicMock()
        get_result.returncode = 0
        get_result.stdout = json.dumps({
            "data": {"data": {"existing_key": "existing_value"}}
        })
        
        # Second call: put merged secrets
        put_result = MagicMock()
        put_result.returncode = 0
        
        mock_run.side_effect = [get_result, put_result]
        
        mgr = EnvManager('platform', 'production', 'postgres')
        result = mgr._vault_set('new_key', 'new_value')
        
        assert result == True
        # Verify put was called with merged data
        put_call = mock_run.call_args_list[-1]
        put_args = put_call[0][0]
        assert any('existing_key' in arg for arg in put_args)
        assert any('new_key' in arg for arg in put_args)


class Test1PasswordOperations:
    """Test 1Password operations with mocked subprocess"""
    
    @patch('libs.env.subprocess.run')
    def test_op_get_all_success(self, mock_run):
        from libs.env import EnvManager
        
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps({
            "fields": [
                {"label": "username", "value": "admin"},
                {"label": "POSTGRES_PASSWORD", "value": "secret"},
                {"label": "notesPlain", "value": "skip this"},
                {"label": "password", "value": "also filtered"},  # 'password' label is filtered
            ]
        })
        mock_run.return_value = mock_result
        
        mgr = EnvManager('bootstrap', 'production', 'vault')
        result = mgr._op_get_all()
        
        # 'notesPlain' label is filtered out
        assert result == {"username": "admin", "POSTGRES_PASSWORD": "secret", "password": "also filtered"}
        assert "notesPlain" not in result

    @patch('libs.env.subprocess.run')
    def test_op_get_all_init_uses_init_item(self, mock_run):
        from libs.env import EnvManager, INIT_ITEM

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps({"fields": []})
        mock_run.return_value = mock_result

        mgr = EnvManager('init', 'production')
        mgr._op_get_all()

        op_call = mock_run.call_args[0][0]
        assert INIT_ITEM in op_call


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


class TestPublicAPI:
    """Test public API methods"""
    
    @patch.object(__import__('libs.env', fromlist=['EnvManager']).EnvManager, '_vault_get')
    def test_get_secret_platform_uses_vault(self, mock_vault_get):
        from libs.env import EnvManager
        
        mock_vault_get.return_value = "vault_secret"
        
        mgr = EnvManager('platform', 'production', 'postgres')
        result = mgr.get_secret('password')
        
        mock_vault_get.assert_called_once_with('password', 'service')
        assert result == "vault_secret"
    
    @patch.object(__import__('libs.env', fromlist=['EnvManager']).EnvManager, '_op_get')
    def test_get_secret_bootstrap_uses_1password(self, mock_op_get):
        from libs.env import EnvManager
        
        mock_op_get.return_value = "op_secret"
        
        mgr = EnvManager('bootstrap', 'production', 'vault')
        result = mgr.get_secret('password')
        
        mock_op_get.assert_called_once_with('password', 'service')
        assert result == "op_secret"
