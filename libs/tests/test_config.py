"""Unit tests for libs/config.py

Uses mock to isolate from EnvManager.
"""
import pytest
from unittest.mock import patch, MagicMock


class TestConfigInit:
    """Test Config initialization"""
    
    def test_init_creates_env_manager(self):
        from libs.config import Config
        config = Config('platform', 'production', 'postgres')
        assert config._mgr is not None
        assert config._project_mgr is not None


class TestConfigKeyParsing:
    """Test Dokploy-style key parsing"""
    
    def test_parse_simple_key(self):
        from libs.config import Config
        config = Config('platform', 'production')
        level, key = config._parse_key('PASSWORD')
        assert level == 'service'
        assert key == 'PASSWORD'
    
    def test_parse_project_key(self):
        from libs.config import Config
        config = Config('platform', 'production')
        level, key = config._parse_key('project.DATABASE_URL')
        assert level == 'project'
        assert key == 'DATABASE_URL'
    
    def test_parse_environment_key(self):
        from libs.config import Config
        config = Config('platform', 'production')
        level, key = config._parse_key('environment.API_KEY')
        assert level == 'environment'
        assert key == 'API_KEY'
    
    def test_parse_service_key(self):
        from libs.config import Config
        config = Config('platform', 'production')
        level, key = config._parse_key('service.PASSWORD')
        assert level == 'service'
        assert key == 'PASSWORD'
    
    def test_parse_key_with_dots_in_value(self):
        from libs.config import Config
        config = Config('platform', 'production')
        # Should treat 'custom.key.name' as service level 'custom.key.name'
        level, key = config._parse_key('custom.key.name')
        assert level == 'service'
        assert key == 'custom.key.name'


class TestConfigGet:
    """Test Config.get() method"""
    
    @patch('libs.env.EnvManager.get_env')
    def test_get_simple_key_merged(self, mock_get_env):
        from libs.config import Config
        
        # Simulate: service has the key
        mock_get_env.side_effect = lambda key, level: 'service_value' if level == 'service' else None
        
        config = Config('platform', 'production', 'postgres')
        result = config.get('PASSWORD')
        
        assert result == 'service_value'
    
    @patch('libs.env.EnvManager.get_env')
    def test_get_falls_back_to_environment(self, mock_get_env):
        from libs.config import Config
        
        # Simulate: service doesn't have it, environment does
        def side_effect(key, level):
            if level == 'environment':
                return 'env_value'
            return None
        
        mock_get_env.side_effect = side_effect
        
        config = Config('platform', 'production', 'postgres')
        result = config.get('PASSWORD')
        
        assert result == 'env_value'
    
    @patch('libs.env.EnvManager.get_env')
    def test_get_returns_default_when_not_found(self, mock_get_env):
        from libs.config import Config
        mock_get_env.return_value = None
        
        config = Config('platform', 'production', 'postgres')
        result = config.get('MISSING_KEY', default='default_value')
        
        assert result == 'default_value'
    
    @patch('libs.env.EnvManager.get_env')
    def test_get_project_level_directly(self, mock_get_env):
        from libs.config import Config
        mock_get_env.return_value = 'project_value'
        
        config = Config('platform', 'production', 'postgres')
        result = config.get('project.DATABASE_URL')
        
        # Should call with level='project'
        mock_get_env.assert_called_with('DATABASE_URL', 'project')


class TestConfigGetSecret:
    """Test Config.get_secret() method"""
    
    @patch('libs.env.EnvManager.get_secret')
    def test_get_secret_works(self, mock_get_secret):
        from libs.config import Config
        mock_get_secret.return_value = 'secret_value'
        
        config = Config('platform', 'production', 'postgres')
        result = config.get_secret('PASSWORD')
        
        assert result == 'secret_value'


class TestConfigAll:
    """Test Config.all() and merged() methods"""
    
    @patch('libs.env.EnvManager.get_all_env')
    def test_all_returns_level_data(self, mock_get_all):
        from libs.config import Config
        mock_get_all.return_value = {'KEY1': 'val1', 'KEY2': 'val2'}
        
        config = Config('platform', 'production', 'postgres')
        result = config.all('service')
        
        assert result == {'KEY1': 'val1', 'KEY2': 'val2'}
    
    @patch('libs.env.EnvManager.get_all_env')
    def test_merged_combines_all_levels(self, mock_get_all):
        from libs.config import Config
        
        def side_effect(level):
            if level == 'project':
                return {'PROJ': 'proj_val'}
            elif level == 'environment':
                return {'ENV': 'env_val'}
            elif level == 'service':
                return {'SVC': 'svc_val'}
            return {}
        
        mock_get_all.side_effect = side_effect
        
        config = Config('platform', 'production', 'postgres')
        result = config.merged()
        
        assert 'PROJ' in result
        assert 'ENV' in result
        assert 'SVC' in result
