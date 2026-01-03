"""
Dokploy API Client

Wraps Dokploy REST API for automated compose deployments.
Requires DOKPLOY_API_KEY in environment (generate from /settings/profile).
"""
from __future__ import annotations
import os
import httpx
from dotenv import load_dotenv
from libs.common import normalize_env_name as _common_normalize_env_name

load_dotenv()


def _normalize_env_name(env_name: str | None) -> str | None:
    if not env_name:
        return None
    return _common_normalize_env_name(env_name)


class DokployClient:
    """Client for Dokploy REST API"""
    
    def __init__(self, base_url: str | None = None, api_key: str | None = None):
        internal_domain = os.getenv("INTERNAL_DOMAIN", "localhost")
        self.base_url = base_url or os.getenv("DOKPLOY_URL") or f"https://cloud.{internal_domain}/api"
        self.api_key = api_key or os.getenv("DOKPLOY_API_KEY")
        
        # Fallback to 1Password
        if not self.api_key:
            try:
                from libs.env import OpSecrets
                op = OpSecrets(item="bootstrap-dokploy")
                self.api_key = op.get("DOKPLOY_API_KEY")
            except (ImportError, AttributeError, KeyError):
                # If 1Password integration or secret is unavailable, fall back to env var / final validation below.
                pass
        
        if not self.api_key:
            raise ValueError("DOKPLOY_API_KEY not set. Generate from Dokploy /settings/profile or store in 1Password")
    
    def _request(self, method: str, endpoint: str, **kwargs) -> dict | list:
        """Make authenticated request to Dokploy API"""
        headers = {
            "accept": "application/json",
            "content-type": "application/json",
            "x-api-key": self.api_key,
            **kwargs.pop("headers", {})
        }
        url = f"{self.base_url}/{endpoint}"
        try:
            with httpx.Client(timeout=30.0) as client:
                resp = client.request(method, url, headers=headers, **kwargs)
                resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise httpx.HTTPStatusError(
                f"Dokploy API request failed for {method} {url}: "
                f"status code {exc.response.status_code} {exc.response.reason_phrase}",
                request=exc.request,
                response=exc.response,
            ) from exc
        except httpx.RequestError as exc:
            raise httpx.RequestError(
                f"Error while performing Dokploy API request {method} {url}: {exc}",
                request=exc.request,
            ) from exc

        return resp.json() if resp.content else {}
    
    # Project endpoints
    def list_projects(self) -> list[dict]:
        """List all projects"""
        return self._request("GET", "project.all")
    
    def create_project(self, name: str, description: str = "") -> dict:
        """Create a new project"""
        return self._request("POST", "project.create", json={
            "name": name,
            "description": description
        })
    
    def get_project(self, project_id: str) -> dict:
        """Get project by ID"""
        return self._request("GET", f"project.one?projectId={project_id}")

    def list_environments(self, project_name: str) -> list[dict]:
        """List environments for a project by name."""
        projects = self.list_projects()
        for project in projects:
            if project.get("name") == project_name:
                return project.get("environments", [])
        return []

    def create_environment(self, project_id: str, name: str, description: str = "") -> dict:
        """Create a new environment under a project."""
        payload = {
            "projectId": project_id,
            "name": name,
        }
        if description:
            payload["description"] = description
        return self._request("POST", "environment.create", json=payload)

    def ensure_environment(self, project_name: str, env_name: str, description: str = "") -> tuple[dict, bool]:
        """Ensure environment exists, returns (environment, created)."""
        target = _normalize_env_name(env_name)
        if not target:
            raise ValueError("env_name is required")

        projects = self.list_projects()
        for project in projects:
            if project.get("name") != project_name:
                continue
            for env in project.get("environments", []):
                if _normalize_env_name(env.get("name")) == target:
                    return env, False
            env_desc = description or f"{target} env"
            created = self.create_environment(project["projectId"], target, env_desc)
            return created, True

        raise ValueError(f"Project '{project_name}' not found in Dokploy")
    
    # Compose endpoints
    def create_compose(
        self,
        environment_id: str,
        name: str,
        compose_file: str = "",
        env: str = "",
        compose_type: str = "docker-compose",
        app_name: str | None = None,
        source_type: str = "raw",  # raw = use composeFile content, github = pull from repo
        **kwargs
    ) -> dict:
        """Create a new compose application in an environment"""
        payload = {
            "name": name,
            "environmentId": environment_id,
            "composeType": compose_type,
            "sourceType": source_type,
            **kwargs
        }
        if compose_file:
            payload["composeFile"] = compose_file
        if app_name:
            payload["appName"] = app_name
        if env:
            payload["env"] = env
            
        return self._request("POST", "compose.create", json=payload)
    
    def update_compose(
        self,
        compose_id: str,
        compose_file: str | None = None,
        env: str | None = None,

        source_type: str | None = None,
        **kwargs
    ) -> dict:
        """Update compose application"""
        payload = {"composeId": compose_id}
        if compose_file is not None:
            payload["composeFile"] = compose_file
        if env is not None:
            payload["env"] = env
        if source_type is not None:
            payload["sourceType"] = source_type
        
        # Merge extra args (e.g. repository, branch, githubId)
        payload.update(kwargs)
        
        return self._request("POST", "compose.update", json=payload)
    
    def deploy_compose(self, compose_id: str) -> dict:
        """Trigger deployment for compose application"""
        return self._request("POST", "compose.deploy", json={"composeId": compose_id})
    
    def get_compose(self, compose_id: str) -> dict:
        """Get compose details"""
        return self._request("GET", f"compose.one?composeId={compose_id}")
    
    def find_compose_by_name(
        self,
        name: str,
        project_name: str = None,
        env_name: str | None = None,
    ) -> dict | None:
        """Find compose by name across all projects/environments."""
        target_env = _normalize_env_name(env_name)
        projects = self.list_projects()
        for project in projects:
            if project_name and project["name"] != project_name:
                continue
            for env in project.get("environments", []):
                if target_env and _normalize_env_name(env.get("name")) != target_env:
                    continue
                for compose in env.get("compose", []):
                    if compose.get("name") == name:
                        return compose
        return None
    
    def get_default_environment_id(self, project_name: str) -> str | None:
        """Get the default environment ID for a project"""
        projects = self.list_projects()
        for project in projects:
            if project["name"] == project_name:
                for env in project.get("environments", []):
                    if env.get("isDefault"):
                        return env["environmentId"]
                # If no default, return first
                environments = project.get("environments", [])
                if environments:
                    return environments[0]["environmentId"]
        return None

    def get_environment_id(self, project_name: str, env_name: str | None = None, require: bool = False) -> str | None:
        """Get environment ID by name (falls back to default when env_name is omitted or production)."""
        target = _normalize_env_name(env_name)
        projects = self.list_projects()
        for project in projects:
            if project["name"] != project_name:
                continue
            if target:
                for env in project.get("environments", []):
                    if _normalize_env_name(env.get("name")) == target:
                        return env.get("environmentId")
                # For production, allow fallback to default environment to avoid breaking existing setups.
                if target != "production":
                    return None
            for env in project.get("environments", []):
                if env.get("isDefault"):
                    return env.get("environmentId")
            environments = project.get("environments", [])
            if environments:
                return environments[0].get("environmentId")
        return None
    
    # Domain endpoints
    def create_domain(self, compose_id: str, host: str, port: int, https: bool = True, path: str = "/", service_name: str = None) -> dict:
        """Add domain to compose service
        
        Args:
            compose_id: ID of the compose application
            host: Domain name (e.g., 'sso.example.com')
            port: Container port (e.g., 9000)
            https: Enable HTTPS with Let's Encrypt
            path: Path prefix (default: '/')
            service_name: Optional service name (for multi-service composes)
        """
        payload = {
            "composeId": compose_id,
            "host": host,
            "port": port,
            "https": https,
            "path": path,
        }
        if service_name:
            payload["serviceName"] = service_name
        return self._request("POST", "domain.create", json=payload)
    
    def list_domains(self, compose_id: str) -> list[dict]:
        """List all domains for a compose service"""
        return self._request("GET", f"domain.all?composeId={compose_id}")
    
    def delete_domain(self, domain_id: str) -> dict:
        """Delete a domain"""
        # Try DELETE first, then POST if it fails
        try:
            return self._request("DELETE", f"domain.delete?domainId={domain_id}")
        except Exception:
            pass
        # Fallback to POST
        return self._request("POST", "domain.delete", json={"domainId": domain_id})
    
    def ensure_domains(
        self,
        compose_id: str,
        desired_domains: list[dict],
        service_name: str = None,
    ) -> dict:
        """Ensure required domains exist (create if missing, warn on conflict).
        
        This method NEVER deletes existing domains. If a domain exists with
        a different port, it warns the user to manually resolve the conflict.
        
        Args:
            compose_id: ID of the compose application
            desired_domains: List of dicts with 'host', 'port', 'https' keys
            service_name: Optional service name for multi-service composes
            
        Returns:
            Dict with 'created', 'skipped', 'conflicts' info
        """
        result = {"created": 0, "skipped": 0, "conflicts": [], "errors": []}
        
        # Get existing domains
        compose = self.get_compose(compose_id)
        existing = compose.get("domains", [])
        existing_hosts = {d["host"]: d for d in existing}
        
        for spec in desired_domains:
            host = spec["host"]
            desired_port = spec["port"]
            
            if host in existing_hosts:
                existing_port = existing_hosts[host].get("port")
                if existing_port == desired_port:
                    # Already configured correctly
                    result["skipped"] += 1
                else:
                    # Conflict: same host, different port
                    result["conflicts"].append({
                        "host": host,
                        "existing_port": existing_port,
                        "desired_port": desired_port,
                    })
            else:
                # Domain doesn't exist, create it
                try:
                    self.create_domain(
                        compose_id=compose_id,
                        host=host,
                        port=desired_port,
                        https=spec.get("https", True),
                        path=spec.get("path", "/"),
                        service_name=service_name,
                    )
                    result["created"] += 1
                except Exception as e:
                    result["errors"].append(f"create {host}: {e}")
        
        return result
    
    # Environment endpoints
    def get_compose_env(self, compose_id: str) -> str:
        """Get environment variables for a compose service"""
        compose = self.get_compose(compose_id)
        return compose.get("env") or ""
    
    def update_compose_env(self, compose_id: str, env_vars: dict[str, str] = None, env_str: str = None) -> dict:
        """Update environment variables for a compose service
        
        Args:
            compose_id: ID of the compose application
            env_vars: Dict of key-value pairs (will be merged with existing)
            env_str: Raw env string (will replace existing if provided)
        Note:
            This parser expects simple KEY=VALUE lines and does not handle quoted,
            escaped, or multiline values.
        """
        if env_str is None:
            # Merge with existing
            existing_env = self.get_compose_env(compose_id)
            env_dict = {}
            
            # Parse existing
            for line in existing_env.split('\n'):
                line = line.strip()
                if line and '=' in line and not line.startswith('#'):
                    key, value = line.split('=', 1)
                    env_dict[key] = value
            
            # Update with new values
            if env_vars:
                env_dict.update(env_vars)
            
            # Convert back to string
            env_str = '\n'.join(f"{k}={v}" for k, v in env_dict.items())
        
        return self.update_compose(compose_id, env=env_str)


    # Git Provider endpoints
    def list_git_providers(self) -> list[dict]:
        """List all configured git providers"""
        return self._request("GET", "settings.gitProvider.all")

    def get_github_provider_id(self) -> str | None:
        """Get GitHub provider ID by querying API or inferring from existing services."""
        # Method 1: Query API directly
        try:
            providers = self.list_git_providers()
            for p in providers:
                if p.get("provider") == "github":
                    return p.get("gitProviderId")
        except Exception:
            pass

        # Method 2: Infer from existing services
        try:
            projects = self.list_projects()
            for proj in projects:
                for env in proj.get('environments', []):
                    for comp in env.get('compose', []):
                        if comp.get('githubId'):
                            return comp.get('githubId')
        except Exception:
            pass

        return None

    
def get_dokploy(host: str | None = None) -> DokployClient:
    """Get configured Dokploy client
    
    Args:
        host: Optional host override (e.g. 'cloud.example.com')
    """
    base_url = None
    if host:
        base_url = f"https://{host}/api"
    return DokployClient(base_url=base_url)


# Convenience functions
def ensure_project(
    name: str,
    description: str = "",
    host: str | None = None,
    env_name: str | None = None,
    require_env: bool = False,
) -> tuple[str, str | None]:
    """Ensure project exists, return (projectId, environmentId)."""
    client = get_dokploy(host=host)
    projects = client.list_projects()
    normalized_env = _normalize_env_name(env_name)
    
    for project in projects:
        if project["name"] == name:
            env_id = client.get_environment_id(name, normalized_env, require=require_env)
            if (normalized_env or require_env) and not env_id:
                raise ValueError(
                    f"Environment '{normalized_env}' not found in Dokploy project '{name}'. "
                    "Create it in Dokploy UI before deploying."
                )
            return project["projectId"], env_id
    
    result = client.create_project(name, description)
    # API returns {'project': {...}, 'environment': {...}}
    project_id = result.get("project", {}).get("projectId") if isinstance(result, dict) else None
    if not project_id:
        raise ValueError(f"Failed to create project {name}: invalid API response")
    env_id = client.get_environment_id(name, normalized_env, require=False)
    if (normalized_env or require_env) and not env_id:
        raise ValueError(
            f"Environment '{normalized_env}' not found in Dokploy project '{name}'. "
            "Create it in Dokploy UI before deploying."
        )
    return project_id, env_id


def deploy_compose_service(
    project_name: str,
    service_name: str,
    compose_content: str,
    env_vars: dict[str, str],
    host: str | None = None,
    env_name: str | None = None,
) -> str:
    """Deploy a compose service, creating project if needed. Returns composeId."""
    client = get_dokploy(host=host)
    normalized_env = _normalize_env_name(env_name)
    
    # Ensure project and get environment
    project_id, environment_id = ensure_project(
        project_name,
        f"Platform services: {project_name}",
        host=host,
        env_name=normalized_env,
        require_env=normalized_env not in (None, "production"),
    )
    
    if not environment_id:
        raise ValueError(f"No environment found for project {project_name}")
    
    # Format env vars
    env_str = "\n".join(f"{k}={v}" for k, v in env_vars.items() if v is not None)
    
    # Check if compose already exists
    existing = client.find_compose_by_name(service_name, project_name, env_name=normalized_env)
    
    if existing:
        compose_id = existing["composeId"]
        # Update and redeploy
        client.update_compose(compose_id, compose_file=compose_content, env=env_str, source_type="raw")
    else:
        # Create new
        result = client.create_compose(
            environment_id=environment_id,
            name=service_name,
            compose_file=compose_content,
            env=env_str,
            app_name=f"platform-{service_name}",
        )
        compose_id = result["composeId"]
    
    # Deploy
    client.deploy_compose(compose_id)
    return compose_id
