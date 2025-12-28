"""Three-tier configuration loader"""
from pathlib import Path
from dotenv import dotenv_values
from typing import Optional, Dict


class Config:
    """Load config from project/environment/service levels. Priority: service > env > project"""
    
    def __init__(self, project: str = 'platform', env: str = 'prod', service: Optional[str] = None):
        self.project = project
        self.env = env
        self.service = service
        self.root = Path(__file__).parent.parent
        
        self._project = self._load('.env')
        self._env = self._load(f'.env.{env}')
        self._service = self._load_service() if service else {}
    
    def _load(self, filename: str) -> Dict[str, str]:
        f = self.root / filename
        return dict(dotenv_values(f)) if f.exists() else {}
    
    def _load_service(self) -> Dict[str, str]:
        for d in (self.root / self.project).iterdir():
            if d.is_dir() and (d.name == self.service or d.name.endswith(f'.{self.service}')):
                f = d / f'.env.{self.env}.local'
                return dict(dotenv_values(f)) if f.exists() else {}
        return {}
    
    def get(self, key: str, level: Optional[str] = None, default: Optional[str] = None) -> Optional[str]:
        if level == 'project': return self._project.get(key, default)
        if level == 'environment': return self._env.get(key, default)
        if level == 'service': return self._service.get(key, default)
        return self._service.get(key) or self._env.get(key) or self._project.get(key) or default
    
    def all(self) -> Dict[str, str]:
        return {**self._project, **self._env, **self._service}
