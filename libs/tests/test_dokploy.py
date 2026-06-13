"""Unit tests for libs/dokploy.py."""

import os
from unittest.mock import patch

import httpx
import pytest

from libs import dokploy
from libs.dokploy import DokployClient, deploy_compose_service, ensure_project, get_dokploy


class FakeResponse:
    def __init__(self, payload=None, *, content=b"{}", status_error=None):
        self._payload = payload if payload is not None else {}
        self.content = content
        self._status_error = status_error
        self.status_code = 200
        self.reason_phrase = "OK"

    def raise_for_status(self):
        if self._status_error:
            raise self._status_error

    def json(self):
        return self._payload


class FakeHttpClient:
    def __init__(self, response=None, request_error=None):
        self.response = response or FakeResponse({"ok": True})
        self.request_error = request_error
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if self.request_error:
            raise self.request_error
        return self.response


@pytest.fixture
def dokploy_env(monkeypatch):
    monkeypatch.setenv("DOKPLOY_API_KEY", "test-key")
    monkeypatch.delenv("DOKPLOY_URL", raising=False)
    monkeypatch.setenv("INTERNAL_DOMAIN", "example.test")


class TestDokployClient:
    """Test DokployClient methods"""

    def test_default_base_url_uses_internal_domain(self, dokploy_env):
        client = DokployClient()

        assert client.base_url == "https://cloud.example.test/api"

    def test_api_key_falls_back_to_1password(self, monkeypatch):
        class FakeOpSecrets:
            def __init__(self, item):
                assert item == "bootstrap-dokploy"

            def get(self, key):
                assert key == "DOKPLOY_API_KEY"
                return "op-key"

        monkeypatch.delenv("DOKPLOY_API_KEY", raising=False)
        monkeypatch.setitem(
            __import__("sys").modules,
            "libs.env",
            type("FakeEnvModule", (), {"OpSecrets": FakeOpSecrets}),
        )

        client = DokployClient(base_url="https://cloud.example.test/api")

        assert client.api_key == "op-key"

    def test_missing_api_key_raises(self, monkeypatch):
        monkeypatch.delenv("DOKPLOY_API_KEY", raising=False)
        monkeypatch.delenv("DOKPLOY_URL", raising=False)

        with pytest.raises(ValueError, match="DOKPLOY_API_KEY not set"):
            DokployClient(base_url="https://cloud.example.test/api")

    def test_request_merges_headers_and_returns_empty_body(self, monkeypatch, dokploy_env):
        fake = FakeHttpClient(FakeResponse(content=b""))
        monkeypatch.setattr(dokploy.httpx, "Client", lambda timeout: fake)
        client = DokployClient(base_url="https://cloud.example.test/api")

        result = client._request("GET", "project.all", headers={"x-extra": "1"})

        assert result == {}
        method, url, kwargs = fake.calls[0]
        assert method == "GET"
        assert url == "https://cloud.example.test/api/project.all"
        assert kwargs["headers"]["x-api-key"] == "test-key"
        assert kwargs["headers"]["x-extra"] == "1"

    def test_request_wraps_http_status_errors(self, monkeypatch, dokploy_env):
        request = httpx.Request("GET", "https://cloud.example.test/api/project.all")
        response = httpx.Response(500, request=request)
        fake = FakeHttpClient(
            FakeResponse(
                status_error=httpx.HTTPStatusError(
                    "boom", request=request, response=response
                )
            )
        )
        monkeypatch.setattr(dokploy.httpx, "Client", lambda timeout: fake)
        client = DokployClient(base_url="https://cloud.example.test/api")

        with pytest.raises(
            httpx.HTTPStatusError, match="status code 500 Internal Server Error"
        ):
            client._request("GET", "project.all")

    def test_request_wraps_transport_errors(self, monkeypatch, dokploy_env):
        request = httpx.Request("GET", "https://cloud.example.test/api/project.all")
        fake = FakeHttpClient(request_error=httpx.ConnectTimeout("timeout", request=request))
        monkeypatch.setattr(dokploy.httpx, "Client", lambda timeout: fake)
        client = DokployClient(base_url="https://cloud.example.test/api")

        with pytest.raises(httpx.RequestError, match="Error while performing Dokploy API request"):
            client._request("GET", "project.all")

    @patch("libs.dokploy.DokployClient._request")
    def test_list_git_providers(self, mock_request):
        # Setup valid client
        with patch.dict(os.environ, {"DOKPLOY_API_KEY": "test-key"}):
            client = DokployClient()
            mock_request.return_value = [
                {"githubId": "123", "gitProvider": {"providerType": "github"}}
            ]

            providers = client.list_git_providers()

            assert providers == [
                {"githubId": "123", "gitProvider": {"providerType": "github"}}
            ]
            mock_request.assert_called_with("GET", "github.githubProviders")

    @patch("libs.dokploy.DokployClient._request")
    def test_get_compose_deployments(self, mock_request):
        with patch.dict(os.environ, {"DOKPLOY_API_KEY": "test-key"}):
            client = DokployClient()
            mock_request.return_value = [{"deploymentId": "d1"}]

            deployments = client.get_compose_deployments("c1")

            assert deployments == [{"deploymentId": "d1"}]
            mock_request.assert_called_with(
                "GET", "deployment.allByCompose?composeId=c1"
            )

    @patch("libs.dokploy.DokployClient._request")
    def test_get_compose_deployments_accepts_wrapped_response(self, mock_request):
        with patch.dict(os.environ, {"DOKPLOY_API_KEY": "test-key"}):
            client = DokployClient()
            mock_request.return_value = {"deployments": [{"deploymentId": "d1"}]}

            deployments = client.get_compose_deployments("c1")

            assert deployments == [{"deploymentId": "d1"}]

    @patch("libs.dokploy.DokployClient.get_compose")
    @patch("libs.dokploy.DokployClient._request")
    def test_get_compose_deployments_falls_back_to_compose_snapshot(
        self, mock_request, mock_get_compose
    ):
        with patch.dict(os.environ, {"DOKPLOY_API_KEY": "test-key"}):
            client = DokployClient()
            mock_request.side_effect = RuntimeError("endpoint unavailable")
            mock_get_compose.return_value = {"deployments": [{"deploymentId": "d1"}]}

            deployments = client.get_compose_deployments("c1")

            assert deployments == [{"deploymentId": "d1"}]
            mock_get_compose.assert_called_with("c1")

    @patch("libs.dokploy.DokployClient.get_compose_deployments")
    def test_get_latest_deployment(self, mock_get_depls):
        with patch.dict(os.environ, {"DOKPLOY_API_KEY": "test-key"}):
            client = DokployClient()
            mock_get_depls.return_value = [
                {"deploymentId": "d1"},
                {"deploymentId": "d2"},
            ]

            latest = client.get_latest_deployment("c1")

            assert latest == {"deploymentId": "d1"}

    @patch("libs.dokploy.DokployClient.get_compose")
    @patch("libs.dokploy.DokployClient.list_projects")
    def test_get_deployment_log_path(self, mock_list_projects, mock_get_compose):
        with patch.dict(os.environ, {"DOKPLOY_API_KEY": "test-key"}):
            client = DokployClient()
            mock_list_projects.return_value = [
                {"environments": [{"compose": [{"composeId": "c1"}]}]}
            ]
            mock_get_compose.return_value = {
                "deployments": [{"deploymentId": "target", "logPath": "/path/to/log"}]
            }

            log_path = client.get_deployment_log_path("target")

            assert log_path == "/path/to/log"

    @patch("libs.dokploy.DokployClient.get_compose_deployments")
    def test_get_deployment_log_path_with_hints(self, mock_get_depls):
        with patch.dict(os.environ, {"DOKPLOY_API_KEY": "test-key"}):
            client = DokployClient()
            mock_get_depls.return_value = [
                {"deploymentId": "target", "logPath": "/hinted/path"}
            ]

            # Using compose_id hint should trigger optimized path
            log_path = client.get_deployment_log_path("target", compose_id="c1")

            assert log_path == "/hinted/path"
            mock_get_depls.assert_called_with("c1")

    def test_ensure_environment_reuses_normalized_existing_env(self, dokploy_env):
        client = DokployClient()
        client.list_projects = lambda: [
            {
                "name": "platform",
                "environments": [{"name": "Staging", "environmentId": "env-1"}],
            }
        ]

        env, created = client.ensure_environment("platform", "staging")

        assert env["environmentId"] == "env-1"
        assert created is False

    def test_ensure_environment_creates_missing_env(self, dokploy_env):
        client = DokployClient()
        client.list_projects = lambda: [
            {"name": "platform", "projectId": "project-1", "environments": []}
        ]
        client.create_environment = lambda project_id, name, description: {
            "projectId": project_id,
            "name": name,
            "description": description,
            "environmentId": "env-new",
        }

        env, created = client.ensure_environment("platform", "preview")

        assert env == {
            "projectId": "project-1",
            "name": "preview",
            "description": "preview env",
            "environmentId": "env-new",
        }
        assert created is True

    def test_ensure_environment_requires_env_name(self, dokploy_env):
        client = DokployClient()

        with pytest.raises(ValueError, match="env_name is required"):
            client.ensure_environment("platform", "")

    def test_find_compose_by_name_filters_project_and_env(self, dokploy_env):
        client = DokployClient()
        client.list_projects = lambda: [
            {
                "name": "platform",
                "environments": [
                    {
                        "name": "production",
                        "compose": [{"name": "app", "composeId": "prod"}],
                    },
                    {
                        "name": "staging",
                        "compose": [{"name": "app", "composeId": "staging"}],
                    },
                ],
            }
        ]

        # find_compose_by_name re-fetches the full compose via compose.one,
        # because project.all returns a truncated object without `env`.
        client.get_compose = lambda cid: {
            "composeId": cid,
            "env": "IAC_CONFIG_HASH=abc123",
        }

        result = client.find_compose_by_name("app", "platform", env_name="staging")
        assert result["composeId"] == "staging"
        # full object (with env), not the truncated project.all entry
        assert result["env"] == "IAC_CONFIG_HASH=abc123"

    def test_get_environment_id_falls_back_for_production_only(self, dokploy_env):
        client = DokployClient()
        client.list_projects = lambda: [
            {
                "name": "platform",
                "environments": [{"name": "default", "environmentId": "env-default"}],
            }
        ]

        assert client.get_environment_id("platform", "production") == "env-default"
        assert client.get_environment_id("platform", "preview") is None

    def test_ensure_domains_skips_conflicts_and_records_create_errors(self, dokploy_env):
        client = DokployClient()
        client.get_compose = lambda compose_id: {
            "domains": [
                {"host": "ok.example.test", "port": 8080},
                {"host": "conflict.example.test", "port": 9000},
            ]
        }

        def fake_create_domain(**kwargs):
            if kwargs["host"] == "bad.example.test":
                raise RuntimeError("create failed")
            return {"domainId": kwargs["host"]}

        client.create_domain = fake_create_domain

        result = client.ensure_domains(
            "compose-1",
            [
                {"host": "ok.example.test", "port": 8080},
                {"host": "conflict.example.test", "port": 8080},
                {"host": "new.example.test", "port": 8080},
                {"host": "bad.example.test", "port": 8080},
            ],
        )

        assert result == {
            "created": 1,
            "skipped": 1,
            "conflicts": [
                {
                    "host": "conflict.example.test",
                    "existing_port": 9000,
                    "desired_port": 8080,
                }
            ],
            "errors": ["create bad.example.test: create failed"],
        }

    def test_update_compose_env_merges_existing_values(self, dokploy_env):
        client = DokployClient()
        client.get_compose_env = lambda compose_id: "A=1\n# comment\nB=old\nINVALID"
        calls = []
        client.update_compose = lambda compose_id, env: calls.append((compose_id, env)) or {
            "composeId": compose_id
        }

        result = client.update_compose_env("compose-1", env_vars={"B": "new", "C": "3"})

        assert result == {"composeId": "compose-1"}
        assert calls == [("compose-1", "A=1\nB=new\nC=3")]

    def test_get_github_provider_id_falls_back_to_existing_compose(self, dokploy_env):
        client = DokployClient()
        client.list_git_providers = lambda: (_ for _ in ()).throw(RuntimeError("api down"))
        client.list_projects = lambda: [
            {
                "environments": [
                    {"compose": [{"githubId": ""}, {"githubId": "github-provider"}]}
                ]
            }
        ]

        assert client.get_github_provider_id() == "github-provider"

    def test_get_github_provider_id_reads_github_providers_endpoint(self, dokploy_env):
        """Method 1 must return the `githubId` from the github.githubProviders
        shape. The old code queried the wrong endpoint and read the wrong field,
        so it silently fell through even when a configured provider existed —
        which broke every deploy with 'No GitHub provider found'."""
        client = DokployClient()
        client.list_git_providers = lambda: [
            {
                "githubId": "126refcRlCoWj6pmPXElU",
                "gitProvider": {"providerType": "github", "name": "Infra2-linker"},
            }
        ]
        client.list_projects = lambda: (_ for _ in ()).throw(
            AssertionError("Method 2 should not be reached when a provider exists")
        )

        assert client.get_github_provider_id() == "126refcRlCoWj6pmPXElU"


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


def test_ensure_project_rejects_missing_required_environment(monkeypatch):
    class FakeClient:
        def list_projects(self):
            return [{"name": "platform", "projectId": "project-1"}]

        def get_environment_id(self, project_name, env_name, require=False):
            return None

    monkeypatch.setattr(dokploy, "get_dokploy", lambda host=None: FakeClient())

    with pytest.raises(ValueError, match="Environment 'staging' not found"):
        ensure_project("platform", env_name="staging", require_env=True)


def test_ensure_project_rejects_invalid_create_response(monkeypatch):
    class FakeClient:
        def list_projects(self):
            return []

        def create_project(self, name, description):
            return {"project": {}}

    monkeypatch.setattr(dokploy, "get_dokploy", lambda host=None: FakeClient())

    with pytest.raises(ValueError, match="Failed to create project platform"):
        ensure_project("platform")


def test_deploy_compose_service_updates_existing_compose(monkeypatch):
    class FakeClient:
        def __init__(self):
            self.updated = []
            self.deployed = []

        def find_compose_by_name(self, service_name, project_name, env_name=None):
            assert env_name == "staging"
            return {"composeId": "compose-existing"}

        def update_compose(self, compose_id, **kwargs):
            self.updated.append((compose_id, kwargs))

        def deploy_compose(self, compose_id):
            self.deployed.append(compose_id)

    client = FakeClient()
    monkeypatch.setattr(dokploy, "get_dokploy", lambda host=None: client)
    monkeypatch.setattr(dokploy, "ensure_project", lambda *args, **kwargs: ("p1", "e1"))

    compose_id = deploy_compose_service(
        "platform",
        "alerting",
        "services: {}",
        {"A": "1", "DROP": None},
        env_name="staging",
    )

    assert compose_id == "compose-existing"
    assert client.updated == [
        (
            "compose-existing",
            {
                "compose_file": "services: {}",
                "env": "A=1",
                "source_type": "raw",
            },
        )
    ]
    assert client.deployed == ["compose-existing"]


def test_deploy_compose_service_creates_missing_compose(monkeypatch):
    class FakeClient:
        def __init__(self):
            self.created = []
            self.deployed = []

        def find_compose_by_name(self, service_name, project_name, env_name=None):
            return None

        def create_compose(self, **kwargs):
            self.created.append(kwargs)
            return {"composeId": "compose-new"}

        def deploy_compose(self, compose_id):
            self.deployed.append(compose_id)

    client = FakeClient()
    monkeypatch.setattr(dokploy, "get_dokploy", lambda host=None: client)
    monkeypatch.setattr(dokploy, "ensure_project", lambda *args, **kwargs: ("p1", "e1"))

    assert (
        deploy_compose_service("platform", "alerting", "services: {}", {"A": "1"})
        == "compose-new"
    )
    assert client.created[0]["environment_id"] == "e1"
    assert client.created[0]["app_name"] == "platform-alerting"
    assert client.deployed == ["compose-new"]
