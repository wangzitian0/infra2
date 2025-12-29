"""Configuration loader using libs/env.py

Usage:
    from libs.config import Config
    
    config = Config(project='platform', env='production', service='postgres')
    password = config.get('POSTGRES_PASSWORD')
    secret = config.get_secret('POSTGRES_PASSWORD')
"""
from __future__ import annotations
from typing import Optional
from libs.env import EnvManager


class Config:
    """Load config from remote SSOT using EnvManager."""
    
    def __init__(self, project: str, env: str = 'production', service: Optional[str] = None):
        self._mgr = EnvManager(project, env, service)
        self._project_mgr = EnvManager(project, env, None)
    
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
            return self._project_mgr.get_env(actual_key, level) or default
        
        # Merged: service > environment > project
        for lvl in ['service', 'environment', 'project']:
            mgr = self._mgr if lvl == 'service' else self._project_mgr
            val = mgr.get_env(actual_key, lvl)
            if val:
                return val
        return default
    
    def get_secret(self, key: str, default: Optional[str] = None) -> Optional[str]:
        """Get secret. Same syntax as get()."""
        level, actual_key = self._parse_key(key)
        
        if level != 'service':
            return self._project_mgr.get_secret(actual_key, level) or default
        
        for lvl in ['service', 'environment', 'project']:
            mgr = self._mgr if lvl == 'service' else self._project_mgr
            val = mgr.get_secret(actual_key, lvl)
            if val:
                return val
        return default
    
    def all(self, level: str = 'service') -> dict:
        """Get all env vars from a level."""
        mgr = self._mgr if level == 'service' else self._project_mgr
        return mgr.get_all_env(level)
    
    def all_secrets(self, level: str = 'service') -> dict:
        """Get all secrets from a level."""
        mgr = self._mgr if level == 'service' else self._project_mgr
        return mgr.get_all_secrets(level)
    
    def merged(self) -> dict:
        """Get merged env vars (service > environment > project)."""
        result = {}
        for lvl in ['project', 'environment', 'service']:
            mgr = self._mgr if lvl == 'service' else self._project_mgr
            result.update(mgr.get_all_env(lvl))
        return result
