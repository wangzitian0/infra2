"""Core environment variable and secret operations

This module provides the core logic for reading/writing env vars and secrets.
SSOT per project:
- bootstrap: 1Password for both
- platform/others: Dokploy for env, Vault for secrets

Usage:
    from libs.env import EnvManager
    
    env = EnvManager(project='platform', env='production', service='postgres')
    password = env.get_secret('POSTGRES_PASSWORD')
    env.set_secret('POSTGRES_PASSWORD', 'newvalue')
"""
from __future__ import annotations
import json
import os
import secrets
import string
import subprocess
from typing import Optional


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


def generate_password(length: int = 24) -> str:
    """Generate a secure random password"""
    alphabet = string.ascii_letters + string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(length))


class EnvManager:
    """Manage environment variables and secrets across different backends."""
    
    def __init__(self, project: str, env: str = 'production', service: Optional[str] = None):
        self.project = project
        self.env = env
        self.service = service
        self._config = SSOT_CONFIG.get(project, SSOT_CONFIG['platform'])
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
    # 1Password Operations
    # =========================================================================
    
    def _op_cmd(self, cmd: str) -> tuple[bool, str]:
        """Run 1Password CLI command"""
        try:
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True, check=True)
            return True, result.stdout
        except subprocess.CalledProcessError as e:
            return False, e.stderr
    
    def _op_get_all(self, level: str = 'service') -> dict:
        """Get all fields from 1Password item"""
        cache_key = f'_op_{level}'
        if cache_key in self._cache:
            return self._cache[cache_key]
        
        op_vault = self._config.get('op_vault', 'infra2')
        path = self._get_path(level)
        ok, output = self._op_cmd(f'op item get "{path}" --vault="{op_vault}" --format=json')
        if not ok:
            return {}
        try:
            item = json.loads(output)
            data = {f["label"]: f.get("value", "") for f in item.get("fields", [])
                    if f.get("label") and f.get("label") not in ["notesPlain", "password"]}
            self._cache[cache_key] = data
            return data
        except json.JSONDecodeError:
            return {}
    
    def _op_get(self, key: str, level: str = 'service') -> Optional[str]:
        """Get single field from 1Password"""
        return self._op_get_all(level).get(key)
    
    def _op_set(self, key: str, value: str, level: str = 'service') -> bool:
        """Set single field in 1Password"""
        op_vault = self._config.get('op_vault', 'infra2')
        path = self._get_path(level)
        
        # Check if item exists
        ok, _ = self._op_cmd(f'op item get "{path}" --vault="{op_vault}"')
        if ok:
            ok, _ = self._op_cmd(f'op item edit "{path}" --vault="{op_vault}" "{key}={value}"')
        else:
            ok, _ = self._op_cmd(f'op item create --category=login --title="{path}" --vault="{op_vault}" "{key}={value}"')
        
        # Clear cache
        self._cache.pop(f'_op_{level}', None)
        return ok
    
    # =========================================================================
    # Vault Operations
    # =========================================================================
    
    def _vault_cmd(self, cmd: str) -> tuple[bool, str]:
        """Run vault command"""
        domain = os.environ.get("INTERNAL_DOMAIN", "")
        vault_addr = f"https://vault.{domain}" if domain else os.environ.get("VAULT_ADDR", "")
        env = os.environ.copy()
        if vault_addr:
            env["VAULT_ADDR"] = vault_addr
        try:
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True, check=True, env=env)
            return True, result.stdout
        except subprocess.CalledProcessError as e:
            return False, e.stderr
    
    def _vault_get_all(self, level: str = 'service') -> dict:
        """Get all secrets from Vault"""
        cache_key = f'_vault_{level}'
        if cache_key in self._cache:
            return self._cache[cache_key]
        
        path = self._get_path(level)
        ok, output = self._vault_cmd(f"vault kv get -format=json secret/{path}")
        if not ok:
            return {}
        try:
            data = json.loads(output).get("data", {}).get("data", {})
            self._cache[cache_key] = data
            return data
        except json.JSONDecodeError:
            return {}
    
    def _vault_get(self, key: str, level: str = 'service') -> Optional[str]:
        """Get single field from Vault"""
        return self._vault_get_all(level).get(key)
    
    def _vault_set(self, key: str, value: str, level: str = 'service') -> bool:
        """Set single field in Vault (merge with existing)"""
        path = self._get_path(level)
        existing = self._vault_get_all(level)
        existing[key] = value
        kv_pairs = " ".join(f'{k}="{v}"' for k, v in existing.items())
        ok, _ = self._vault_cmd(f"vault kv put secret/{path} {kv_pairs}")
        
        # Clear cache
        self._cache.pop(f'_vault_{level}', None)
        return ok
    
    # =========================================================================
    # Dokploy Operations (placeholder)
    # =========================================================================
    
    def _dokploy_get_all(self, level: str = 'service') -> dict:
        """Get env vars from Dokploy (placeholder)"""
        # TODO: Implement Dokploy CLI/API
        return {}
    
    def _dokploy_get(self, key: str, level: str = 'service') -> Optional[str]:
        return self._dokploy_get_all(level).get(key)
    
    def _dokploy_set(self, key: str, value: str, level: str = 'service') -> bool:
        """Set env var in Dokploy (placeholder)"""
        # TODO: Implement Dokploy CLI/API
        print(f"⚠️ Dokploy API not yet implemented. Would set {key} at {self._get_path(level)}")
        return False
    
    # =========================================================================
    # Public API
    # =========================================================================
    
    def get_env(self, key: str, level: str = 'service') -> Optional[str]:
        """Get environment variable from SSOT"""
        source = self._config['env_source']
        if source == '1password':
            return self._op_get(key, level)
        elif source == 'dokploy':
            return self._dokploy_get(key, level)
        return None
    
    def set_env(self, key: str, value: str, level: str = 'service') -> bool:
        """Set environment variable in SSOT"""
        source = self._config['env_source']
        if source == '1password':
            return self._op_set(key, value, level)
        elif source == 'dokploy':
            return self._dokploy_set(key, value, level)
        return False
    
    def get_secret(self, key: str, level: str = 'service') -> Optional[str]:
        """Get secret from SSOT"""
        source = self._config['secret_source']
        if source == '1password':
            return self._op_get(key, level)
        elif source == 'vault':
            return self._vault_get(key, level)
        return None
    
    def set_secret(self, key: str, value: str, level: str = 'service') -> bool:
        """Set secret in SSOT"""
        source = self._config['secret_source']
        if source == '1password':
            return self._op_set(key, value, level)
        elif source == 'vault':
            return self._vault_set(key, value, level)
        return False
    
    def get_all_env(self, level: str = 'service') -> dict:
        """Get all environment variables"""
        source = self._config['env_source']
        if source == '1password':
            return self._op_get_all(level)
        elif source == 'dokploy':
            return self._dokploy_get_all(level)
        return {}
    
    def get_all_secrets(self, level: str = 'service') -> dict:
        """Get all secrets"""
        source = self._config['secret_source']
        if source == '1password':
            return self._op_get_all(level)
        elif source == 'vault':
            return self._vault_get_all(level)
        return {}
    
    def generate_and_store_secret(self, key: str, length: int = 24, level: str = 'service') -> str:
        """Generate a password and store it in SSOT, return the value"""
        password = generate_password(length)
        self.set_secret(key, password, level)
        return password
