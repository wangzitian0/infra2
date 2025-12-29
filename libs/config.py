"""Remote-first configuration loader (no local storage)

Loads config directly from remote SSOT:
- bootstrap: 1Password
- platform/others: Dokploy (env) + Vault (secrets)

Supports Dokploy variable syntax:
- {VAR} or VAR - service level
- {project.VAR} - project level  
- {environment.VAR} - environment level

Usage:
    from libs.config import Config
    
    config = Config(project='platform', env='production', service='postgres')
    
    # Get merged variable (service > environment > project)
    password = config.get('POSTGRES_PASSWORD')
    
    # Get variable from specific level (Dokploy syntax)
    project_var = config.get('project.DATABASE_URL')
    env_var = config.get('environment.API_KEY')
"""
from __future__ import annotations
import json
import os
import subprocess
from typing import Optional


class Config:
    """Load config from remote SSOT (no local storage).
    
    Three-tier structure matching Dokploy:
    - Project: {project}
    - Environment: {project}/{env}
    - Service: {project}/{env}/{service}
    """
    
    # SSOT configuration per project
    SSOT_CONFIG = {
        'bootstrap': {
            'env_source': '1password',
            'secret_source': '1password',
            'op_vault': 'infra2-bootstrap',
        },
        'platform': {
            'env_source': 'dokploy',
            'secret_source': 'vault',
        },
    }
    
    def __init__(self, project: str, env: str = 'production', service: Optional[str] = None):
        """
        Args:
            project: bootstrap, platform, etc.
            env: production, staging
            service: Service name (e.g., postgres, redis)
        """
        self.project = project
        self.env = env
        self.service = service
        self._config = self.SSOT_CONFIG.get(project, self.SSOT_CONFIG['platform'])
        self._cache: dict = {}
    
    def _get_path(self, level: str = 'service') -> str:
        """Build path for specific level"""
        if level == 'project':
            return self.project
        elif level == 'environment':
            return f"{self.project}/{self.env}"
        else:  # service
            if self.service:
                return f"{self.project}/{self.env}/{self.service}"
            return f"{self.project}/{self.env}"
    
    # =========================================================================
    # 1Password
    # =========================================================================
    
    def _op_get_item(self, level: str = 'service') -> dict:
        """Get all fields from 1Password item"""
        cache_key = f'_op_{level}'
        if cache_key in self._cache:
            return self._cache[cache_key]
        
        op_vault = self._config.get('op_vault', 'infra2')
        path = self._get_path(level)
        try:
            result = subprocess.run(
                f'op item get "{path}" --vault="{op_vault}" --format=json',
                shell=True, capture_output=True, text=True, check=True
            )
            item = json.loads(result.stdout)
            data = {f["label"]: f.get("value", "") for f in item.get("fields", [])
                    if f.get("label") and f.get("label") not in ["notesPlain", "password"]}
            self._cache[cache_key] = data
            return data
        except (subprocess.CalledProcessError, json.JSONDecodeError):
            return {}
    
    # =========================================================================
    # Vault
    # =========================================================================
    
    def _vault_get_all(self, level: str = 'service') -> dict:
        """Get all secrets from Vault"""
        cache_key = f'_vault_{level}'
        if cache_key in self._cache:
            return self._cache[cache_key]
        
        domain = os.environ.get("INTERNAL_DOMAIN", "")
        vault_addr = f"https://vault.{domain}" if domain else os.environ.get("VAULT_ADDR", "")
        path = self._get_path(level)
        
        env = os.environ.copy()
        if vault_addr:
            env["VAULT_ADDR"] = vault_addr
        
        try:
            result = subprocess.run(
                f"vault kv get -format=json secret/{path}",
                shell=True, capture_output=True, text=True, check=True, env=env
            )
            data = json.loads(result.stdout).get("data", {}).get("data", {})
            self._cache[cache_key] = data
            return data
        except (subprocess.CalledProcessError, json.JSONDecodeError):
            return {}
    
    # =========================================================================
    # Dokploy (placeholder)
    # =========================================================================
    
    def _dokploy_get_all(self, level: str = 'service') -> dict:
        """Get env vars from Dokploy (placeholder)"""
        # TODO: Implement Dokploy API/CLI integration
        return {}
    
    # =========================================================================
    # Internal helpers
    # =========================================================================
    
    def _parse_key(self, key: str) -> tuple[str, str]:
        """Parse Dokploy-style key into (level, actual_key)
        
        Syntax:
        - 'project.VAR' -> ('project', 'VAR')
        - 'environment.VAR' -> ('environment', 'VAR')
        - 'VAR' or 'service.VAR' -> ('service', 'VAR')
        """
        if '.' in key:
            prefix, actual_key = key.split('.', 1)
            if prefix in ('project', 'environment', 'service'):
                return prefix, actual_key
        return 'service', key
    
    def _get_from_level(self, level: str) -> dict:
        """Get all vars from a specific level"""
        source = self._config['env_source']
        if source == '1password':
            return self._op_get_item(level)
        elif source == 'dokploy':
            return self._dokploy_get_all(level)
        return {}
    
    def _get_secrets_from_level(self, level: str) -> dict:
        """Get all secrets from a specific level"""
        source = self._config['secret_source']
        if source == '1password':
            return self._op_get_item(level)
        elif source == 'vault':
            return self._vault_get_all(level)
        return {}
    
    # =========================================================================
    # Public API
    # =========================================================================
    
    def get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        """Get environment variable from remote SSOT.
        
        Supports Dokploy-style syntax:
        - 'VAR' -> merged (service > environment > project)
        - 'project.VAR' -> project level only
        - 'environment.VAR' -> environment level only
        - 'service.VAR' -> service level only
        """
        level, actual_key = self._parse_key(key)
        
        if level != 'service' or '.' in key:
            # Specific level requested
            data = self._get_from_level(level)
            return data.get(actual_key, default)
        
        # Merged: service > environment > project
        for lvl in ['service', 'environment', 'project']:
            data = self._get_from_level(lvl)
            if actual_key in data:
                return data[actual_key]
        return default
    
    def get_secret(self, key: str, default: Optional[str] = None) -> Optional[str]:
        """Get secret from remote SSOT (Vault or 1Password).
        
        Same Dokploy-style syntax as get().
        """
        level, actual_key = self._parse_key(key)
        
        if level != 'service' or '.' in key:
            data = self._get_secrets_from_level(level)
            return data.get(actual_key, default)
        
        # Merged: service > environment > project
        for lvl in ['service', 'environment', 'project']:
            data = self._get_secrets_from_level(lvl)
            if actual_key in data:
                return data[actual_key]
        return default
    
    def all(self, level: str = 'service') -> dict:
        """Get all environment variables from a specific level.
        
        Args:
            level: 'project', 'environment', or 'service'
        """
        return self._get_from_level(level)
    
    def all_secrets(self, level: str = 'service') -> dict:
        """Get all secrets from a specific level."""
        return self._get_secrets_from_level(level)
    
    def merged(self) -> dict:
        """Get merged environment variables (service > environment > project)."""
        result = {}
        for lvl in ['project', 'environment', 'service']:
            result.update(self._get_from_level(lvl))
        return result
    
    def merged_secrets(self) -> dict:
        """Get merged secrets (service > environment > project)."""
        result = {}
        for lvl in ['project', 'environment', 'service']:
            result.update(self._get_secrets_from_level(lvl))
        return result
