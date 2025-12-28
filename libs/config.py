"""Three-tier configuration loader

Structure (matches Dokploy):
- Project: {project}/.env
- Environment: {project}/.env.{env}
- Service: {project}/{service}/.env.{env}

Priority: service > environment > project
"""
from __future__ import annotations
from pathlib import Path
from dotenv import dotenv_values
from typing import Optional


class Config:
    """Load config from project/environment/service levels."""
    
    def __init__(self, project: str, env: str = 'production', service: Optional[str] = None):
        """
        Args:
            project: bootstrap, platform, e2e_regression, or tools
            env: production, staging, or test_xxx
            service: Service name (e.g., postgres, redis)
        """
        self.project = project
        self.env = env
        self.service = service
        self.root = Path(__file__).parent.parent
        self.project_dir = self.root / project
        
        self._project_vars = self._load(self.project_dir / '.env')
        self._env_vars = self._load(self.project_dir / f'.env.{env}')
        self._service_vars = self._load_service() if service else {}
    
    def _load(self, filepath: Path) -> dict[str, str]:
        return dict(dotenv_values(filepath)) if filepath.exists() else {}
    
    def _load_service(self) -> dict[str, str]:
        """Load service-level config from {project}/{service}/.env.{env}"""
        if not self.project_dir.exists():
            return {}
        for d in self.project_dir.iterdir():
            if d.is_dir() and (d.name == self.service or d.name.endswith(f'.{self.service}')):
                return self._load(d / f'.env.{self.env}')
        return {}
    
    def get(self, key: str, level: Optional[str] = None, default: Optional[str] = None) -> Optional[str]:
        if level == 'project': return self._project_vars.get(key, default)
        if level == 'environment': return self._env_vars.get(key, default)
        if level == 'service': return self._service_vars.get(key, default)
        # Priority: service > environment > project
        return self._service_vars.get(key) or self._env_vars.get(key) or self._project_vars.get(key) or default
    
    def all(self) -> dict[str, str]:
        return {**self._project_vars, **self._env_vars, **self._service_vars}
