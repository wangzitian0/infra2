"""Unit tests for libs/dokploy.py."""
import os
from unittest.mock import patch
from libs.dokploy import get_dokploy, DokployClient

class TestDokployClient:
    """Test DokployClient methods"""

    @patch('libs.dokploy.DokployClient._request')
    def test_list_git_providers(self, mock_request):
        # Setup valid client
        with patch.dict(os.environ, {"DOKPLOY_API_KEY": "test-key"}):
            client = DokployClient()
            mock_request.return_value = [{"provider": "github", "gitProviderId": "123"}]
            
            providers = client.list_git_providers()
            
            assert providers == [{"provider": "github", "gitProviderId": "123"}]
            mock_request.assert_called_with("GET", "settings.gitProvider.all")


class TestGetDokployFactory:
    """Test factory function"""
    
    @patch('libs.dokploy.DokployClient')
    def test_get_dokploy_defaults(self, mock_cls):
        get_dokploy()
        mock_cls.assert_called_with(base_url=None)
        
    @patch('libs.dokploy.DokployClient')
    def test_get_dokploy_with_host(self, mock_cls):
        get_dokploy(host="my.host.com")
        mock_cls.assert_called_with(base_url="https://my.host.com/api")
