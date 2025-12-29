"""Core environment variable and secret operations

This module provides the core logic for reading/writing env vars and secrets.
SSOT per project:
- bootstrap: 1Password for both
- platform/others: Dokploy for env (API pending), Vault for secrets

Usage:
    from libs.env import EnvManager, get_or_set
    
    env = EnvManager(project='platform', env='production', service='postgres')
    password = env.get_secret('POSTGRES_PASSWORD')
"""
from __future__ import annotations
import json
import os
import secrets
import string
import subprocess
import inspect
from typing import TypedDict, NotRequired, Optional


__all__ = [
    'EnvManager',
    'get_or_set',
    'generate_password',
    'op_get_item_field',
    'SSOT_CONFIG',
    'OP_VAULT',
    'INIT_ITEM',
    'REQUIRED_INIT_FIELDS',
    'SRC_1PASSWORD',
    'SRC_VAULT',
    'SRC_DOKPLOY',
    'ProjectConfig',
]


# =========================================================================
# Constants
# =========================================================================
OP_VAULT = "Infra2"
INIT_ITEM = "init/env_vars"
REQUIRED_INIT_FIELDS = ["VPS_HOST", "INTERNAL_DOMAIN"]

SRC_1PASSWORD = '1password'
SRC_VAULT = 'vault'
SRC_DOKPLOY = 'dokploy'


class ProjectConfig(TypedDict):
    env_source: str
    secret_source: str
    op_vault: NotRequired[str]
    op_item: NotRequired[str]


# SSOT configuration per project
SSOT_CONFIG: dict[str, ProjectConfig] = {
    'init': {
        # Special project for bootstrap phase 0-3
        # All vars stored in 1Password item: init/env_vars
        'env_source': SRC_1PASSWORD,
        'secret_source': SRC_1PASSWORD,
        'op_vault': OP_VAULT,
        'op_item': INIT_ITEM,
    },
    'bootstrap': {
        'env_source': SRC_1PASSWORD,
        'secret_source': SRC_1PASSWORD,
        'op_vault': OP_VAULT,
    },
    'platform': {
        'env_source': SRC_DOKPLOY,
        'secret_source': SRC_VAULT,
    },
}


def generate_password(length: int = 24) -> str:
    """Generate a secure random alphanumeric password"""
    alphabet = string.ascii_letters + string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(length))


def get_or_set(
    key: str,
    project: str = 'platform',
    env: str = 'production',
    service: str | None = None,
    length: int = 24,
    generator: callable = None,
) -> str:
    """Get existing secret or generate and store new one.
    
    Core pattern: check or set - if exists, use it; if not, create it.
    
    Args:
        key: Secret key name
        project: Project name (platform, bootstrap, init)
        env: Environment (production, staging)
        service: Service name (postgres, redis, etc)
        length: Password length if generating
        generator: Custom generator function, defaults to generate_password
    
    Returns:
        The existing or newly generated secret value
    """
    mgr = EnvManager(project, env, service)
    existing = mgr.get_secret(key)
    if existing is not None:
        return existing
    
    # Generate new value
    if generator:
        try:
            sig = inspect.signature(generator)
        except (TypeError, ValueError):
            value = generator(length)
        else:
            if not sig.parameters:
                value = generator()
            else:
                value = generator(length)
    else:
        value = generate_password(length)
    mgr.set_secret(key, value)
    return value


class EnvManager:
    """Manage environment variables and secrets across different backends."""
    
    def __init__(self, project: str, env: str = 'production', service: str | None = None):
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
    
    def _run_cli(self, cmd: list[str], env: dict[str, str] | None = None) -> tuple[bool, str]:
        """Run a CLI command and return (ok, stdout/stderr)."""
        try:
            full_env = os.environ.copy()
            if env:
                full_env.update(env)
            result = subprocess.run(cmd, capture_output=True, text=True, check=True, env=full_env)
            return True, result.stdout
        except subprocess.CalledProcessError as e:
            return False, e.stderr
        except (FileNotFoundError, OSError) as e:
            return False, str(e)
    
    # =========================================================================
    # 1Password Operations
    # =========================================================================

    def _op_item_path(self, level: str = 'service') -> str:
        """Resolve 1Password item path, honoring explicit op_item overrides."""
        op_item = self._config.get('op_item')
        if op_item:
            return op_item
        return self._get_path(level)

    def _op_get_all(self, level: str = 'service') -> dict[str, str]:
        """Get all fields from 1Password item"""
        cache_key = f'_op_{level}'
        if cache_key in self._cache:
            return self._cache[cache_key]
        
        op_vault = self._config.get('op_vault', OP_VAULT)
        path = self._op_item_path(level)
        ok, output = self._run_cli([
            "op",
            "item",
            "get",
            path,
            f"--vault={op_vault}",
            "--format=json",
        ])
        if not ok:
            return {}
        try:
            item = json.loads(output)
            data = {
                f["label"]: f.get("value", "")
                for f in item.get("fields", [])
                if f.get("label") and f.get("label") != "notesPlain"
            }
            self._cache[cache_key] = data
            return data
        except json.JSONDecodeError:
            return {}
    
    def _op_get(self, key: str, level: str = 'service') -> Optional[str]:
        return self._op_get_all(level).get(key)
    
    def _op_set(self, key: str, value: str, level: str = 'service') -> bool:
        op_vault = self._config.get('op_vault', OP_VAULT)
        path = self._op_item_path(level)
        
        if self._op_item_exists(path, op_vault):
            ok, _ = self._run_cli([
                "op",
                "item",
                "edit",
                path,
                f"--vault={op_vault}",
                f"{key}={value}",
            ])
        else:
            ok, _ = self._run_cli([
                "op",
                "item",
                "create",
                "--category=login",
                f"--title={path}",
                f"--vault={op_vault}",
                f"{key}={value}",
            ])
        
        # Clear cache
        self._cache.pop(f'_op_{level}', None)
        return ok

    def _op_item_exists(self, path: str, op_vault: str) -> bool:
        ok, _ = self._run_cli([
            "op",
            "item",
            "get",
            path,
            f"--vault={op_vault}",
        ])
        return ok
    
    # =========================================================================
    # Vault Operations
    # =========================================================================
    
    def _vault_get_all(self, level: str = 'service') -> dict[str, str]:
        """Get all secrets from Vault"""
        cache_key = f'_vault_{level}'
        if cache_key in self._cache:
            return self._cache[cache_key]
        
        path = self._get_path(level)
        
        domain = os.environ.get("INTERNAL_DOMAIN", "")
        vault_addr = os.environ.get("VAULT_ADDR") or os.environ.get("VAULT_URL") or ""
        if not vault_addr and domain:
            vault_addr = f"https://vault.{domain}"
        env = {"VAULT_ADDR": vault_addr} if vault_addr else None
        
        ok, output = self._run_cli(
            ["vault", "kv", "get", "-format=json", f"secret/{path}"],
            env=env,
        )
        if not ok:
            return {}
        try:
            data = json.loads(output).get("data", {}).get("data", {})
            self._cache[cache_key] = data
            return data
        except json.JSONDecodeError:
            return {}
    
    def _vault_get(self, key: str, level: str = 'service') -> Optional[str]:
        return self._vault_get_all(level).get(key)
    
    def _vault_set(self, key: str, value: str, level: str = 'service') -> bool:
        path = self._get_path(level)
        existing = self._vault_get_all(level)
        existing[key] = value
        kv_pairs = [f"{k}={v}" for k, v in existing.items()]
        
        domain = os.environ.get("INTERNAL_DOMAIN", "")
        vault_addr = os.environ.get("VAULT_ADDR") or os.environ.get("VAULT_URL") or ""
        if not vault_addr and domain:
            vault_addr = f"https://vault.{domain}"
        env = {"VAULT_ADDR": vault_addr} if vault_addr else None
        
        ok, _ = self._run_cli(
            ["vault", "kv", "put", f"secret/{path}", *kv_pairs],
            env=env,
        )
        
        # Clear cache
        self._cache.pop(f'_vault_{level}', None)
        return ok
    
    # =========================================================================
    # Dokploy Operations (placeholder)
    # =========================================================================
    
    def _dokploy_get_all(self, level: str = 'service') -> dict[str, str]:
        """Get env vars from Dokploy (placeholder)"""
        # TODO: Implement Dokploy CLI/API
        from libs.console import warning
        warning("Dokploy API not yet implemented; returning empty env vars.")
        return {}
    
    def _dokploy_get(self, key: str, level: str = 'service') -> Optional[str]:
        return self._dokploy_get_all(level).get(key)
    
    def _dokploy_set(self, key: str, value: str, level: str = 'service') -> bool:
        """Set env var in Dokploy (placeholder)"""
        # TODO: Implement Dokploy CLI/API
        from libs.console import warning
        warning(f"Dokploy API not yet implemented. Would set {key} at {self._get_path(level)}")
        return False
    
    # =========================================================================
    # Public API
    # =========================================================================
    
    def get_env(self, key: str, level: str = 'service') -> Optional[str]:
        """Get environment variable from SSOT"""
        source = self._config['env_source']
        if source == SRC_1PASSWORD:
            return self._op_get(key, level)
        elif source == SRC_DOKPLOY:
            return self._dokploy_get(key, level)
        return None
    
    def set_env(self, key: str, value: str, level: str = 'service') -> bool:
        """Set environment variable in SSOT"""
        source = self._config['env_source']
        if source == SRC_1PASSWORD:
            return self._op_set(key, value, level)
        elif source == SRC_DOKPLOY:
            return self._dokploy_set(key, value, level)
        return False
    
    def get_secret(self, key: str, level: str = 'service') -> Optional[str]:
        """Get secret from SSOT"""
        source = self._config['secret_source']
        if source == SRC_1PASSWORD:
            return self._op_get(key, level)
        elif source == SRC_VAULT:
            return self._vault_get(key, level)
        return None
    
    def set_secret(self, key: str, value: str, level: str = 'service') -> bool:
        """Set secret in SSOT"""
        source = self._config['secret_source']
        if source == SRC_1PASSWORD:
            return self._op_set(key, value, level)
        elif source == SRC_VAULT:
            return self._vault_set(key, value, level)
        return False
    
    def get_all_env(self, level: str = 'service') -> dict[str, str]:
        """Get all environment variables"""
        source = self._config['env_source']
        if source == SRC_1PASSWORD:
            return self._op_get_all(level)
        elif source == SRC_DOKPLOY:
            return self._dokploy_get_all(level)
        return {}
    
    def get_all_secrets(self, level: str = 'service') -> dict[str, str]:
        """Get all secrets"""
        source = self._config['secret_source']
        if source == SRC_1PASSWORD:
            return self._op_get_all(level)
        elif source == SRC_VAULT:
            return self._vault_get_all(level)
        return {}
    
    def generate_and_store_secret(self, key: str, length: int = 24, level: str = 'service') -> str:
        """Generate a password and store it in SSOT, return the value"""
        password = generate_password(length)
        if not self.set_secret(key, password, level):
            raise RuntimeError(f"Failed to store secret '{key}' at level '{level}'")
        return password


def op_get_item_field(item_name: str, field_label: str, vault: str = OP_VAULT) -> Optional[str]:
    """Get a specific field from an arbitrary 1Password item."""
    mgr = EnvManager('init')
    ok, output = mgr._run_cli([
        "op",
        "item",
        "get",
        item_name,
        f"--vault={vault}",
        "--format=json",
    ])
    if not ok:
        return None
    try:
        item = json.loads(output)
    except json.JSONDecodeError:
        return None
    for field in item.get("fields", []):
        if field.get("label") == field_label:
            return field.get("value", "")
    return None
