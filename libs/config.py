"""Configuration loader using libs/env.py (legacy wrapper).

Usage:
    from libs.config import Config

    config = Config(project='platform', env='production', service='postgres')
    password = config.get_secret('POSTGRES_PASSWORD')
"""
from __future__ import annotations
from typing import Optional
from libs.env import get_secrets


class Config:
    """Legacy secrets wrapper around get_secrets().

    Note: This no longer reads Dokploy env vars; only secrets in Vault/1Password.
    """
    
    def __init__(self, project: str, env: str = 'production', service: Optional[str] = None):
        self._project = project
        self._env = env
        self._service = service
        self._service_secrets = get_secrets(project, service, env)
        self._env_secrets = get_secrets(project, None, env)
    
    def _parse_key(self, key: str) -> tuple[str, str]:
        """Parse Dokploy-style key: 'project.VAR' -> ('project', 'VAR')"""
        if '.' in key:
            prefix, actual = key.split('.', 1)
            if prefix in ('project', 'environment', 'service'):
                return prefix, actual
        return 'service', key
    
    def get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        """Get environment variable. Supports: VAR, project.VAR, environment.VAR"""
        level, actual_key = self._parse_key(key)
        
        if level != 'service':
            return self._env_secrets.get(actual_key) or default

        if self._service is None:
            return self._env_secrets.get(actual_key) or default

        return self._service_secrets.get(actual_key) or self._env_secrets.get(actual_key) or default
    
    def get_secret(self, key: str, default: Optional[str] = None) -> Optional[str]:
        """Get secret. Same syntax as get()."""
        level, actual_key = self._parse_key(key)
        
        if level != 'service':
            return self._env_secrets.get(actual_key) or default

        if self._service is None:
            return self._env_secrets.get(actual_key) or default

        return self._service_secrets.get(actual_key) or self._env_secrets.get(actual_key) or default
    
    def all(self, level: str = 'service') -> dict:
        """Get all env vars from a level."""
        if level == 'service' and self._service is not None:
            return self._service_secrets.get_all()
        return self._env_secrets.get_all()
    
    def all_secrets(self, level: str = 'service') -> dict:
        """Get all secrets from a level."""
        return self.all(level)
    
    def merged(self) -> dict:
        """Get merged env vars (service > environment > project)."""
        result = self._env_secrets.get_all().copy()
        if self._service is not None:
            result.update(self._service_secrets.get_all())
        return result
