"""Three-tier configuration loader

Structure:
- Project: .env (root)
- Environment: .env.<env> (root)
- Service: {project}/.env.<env> (project directory)

Priority: service > environment > project
"""
from pathlib import Path
from dotenv import dotenv_values
from typing import Optional, Dict


class Config:
    """Load config from project/environment/service levels."""
    
    def __init__(self, project: str, env: str = 'production', service: Optional[str] = None):
        """
        Args:
            project: bootstrap, platform, e2e_regression, or tools
            env: production, staging, or test_xxx
            service: Deprecated - will be removed in future version
        """
        if service is not None:
            import warnings
            warnings.warn("service parameter is deprecated and ignored", DeprecationWarning, stacklevel=2)
        self.project = project
        self.env = env
        self.root = Path(__file__).parent.parent
        
        self._project = self._load('.env')
        self._environment = self._load(f'.env.{env}')
        self._service = self._load_service()
    
    def _load(self, filename: str) -> Dict[str, str]:
        f = self.root / filename
        return dict(dotenv_values(f)) if f.exists() else {}
    
    def _load_service(self) -> Dict[str, str]:
        """Load service-level config from {project}/.env.<env>"""
        f = self.root / self.project / f'.env.{self.env}'
        return dict(dotenv_values(f)) if f.exists() else {}
    
    def get(self, key: str, level: Optional[str] = None, default: Optional[str] = None) -> Optional[str]:
        if level == 'project': return self._project.get(key, default)
        if level == 'environment': return self._environment.get(key, default)
        if level == 'service': return self._service.get(key, default)
        # Priority: service > environment > project
        return self._service.get(key) or self._environment.get(key) or self._project.get(key) or default
    
    def all(self) -> Dict[str, str]:
        return {**self._project, **self._environment, **self._service}
