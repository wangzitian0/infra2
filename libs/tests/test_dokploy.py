"""Unit tests for libs/dokploy.py."""

import os
from unittest.mock import patch
from libs.dokploy import get_dokploy, DokployClient


class TestDokployClient:
    """Test DokployClient methods"""

    @patch("libs.dokploy.DokployClient._request")
    def test_list_git_providers(self, mock_request):
        # Setup valid client
        with patch.dict(os.environ, {"DOKPLOY_API_KEY": "test-key"}):
            client = DokployClient()
            mock_request.return_value = [{"provider": "github", "gitProviderId": "123"}]

            providers = client.list_git_providers()

            assert providers == [{"provider": "github", "gitProviderId": "123"}]
            mock_request.assert_called_with("GET", "settings.gitProvider.all")

    @patch("libs.dokploy.DokployClient.get_compose")
    def test_get_compose_deployments(self, mock_get_compose):
        with patch.dict(os.environ, {"DOKPLOY_API_KEY": "test-key"}):
            client = DokployClient()
            mock_get_compose.return_value = {"deployments": [{"deploymentId": "d1"}]}
            
            deployments = client.get_compose_deployments("c1")
            
            assert deployments == [{"deploymentId": "d1"}]
            mock_get_compose.assert_called_with("c1")

    @patch("libs.dokploy.DokployClient.get_compose_deployments")
    def test_get_latest_deployment(self, mock_get_depls):
        with patch.dict(os.environ, {"DOKPLOY_API_KEY": "test-key"}):
            client = DokployClient()
            mock_get_depls.return_value = [{"deploymentId": "d1"}, {"deploymentId": "d2"}]
            
            latest = client.get_latest_deployment("c1")
            
            assert latest == {"deploymentId": "d1"}

    @patch("libs.dokploy.DokployClient.get_compose")
    @patch("libs.dokploy.DokployClient.list_projects")
    def test_get_deployment_log_path(self, mock_list_projects, mock_get_compose):
        with patch.dict(os.environ, {"DOKPLOY_API_KEY": "test-key"}):
            client = DokployClient()
            mock_list_projects.return_value = [{
                "environments": [{
                    "compose": [{"composeId": "c1"}]
                }]
            }]
            mock_get_compose.return_value = {
                "deployments": [{"deploymentId": "target", "logPath": "/path/to/log"}]
            }
            
            log_path = client.get_deployment_log_path("target")
            
            assert log_path == "/path/to/log"

    @patch("libs.dokploy.DokployClient.get_compose_deployments")
    def test_get_deployment_log_path_with_hints(self, mock_get_depls):
        with patch.dict(os.environ, {"DOKPLOY_API_KEY": "test-key"}):
            client = DokployClient()
            mock_get_depls.return_value = [{"deploymentId": "target", "logPath": "/hinted/path"}]
            
            # Using compose_id hint should trigger optimized path
            log_path = client.get_deployment_log_path("target", compose_id="c1")
            
            assert log_path == "/hinted/path"
            mock_get_depls.assert_called_with("c1")


class TestGetDokployFactory:
    """Test factory function"""

    @patch("libs.dokploy.DokployClient")
    def test_get_dokploy_defaults(self, mock_cls):
        get_dokploy()
        mock_cls.assert_called_with(base_url=None)

    @patch("libs.dokploy.DokployClient")
    def test_get_dokploy_with_host(self, mock_cls):
        get_dokploy(host="my.host.com")
        mock_cls.assert_called_with(base_url="https://my.host.com/api")
