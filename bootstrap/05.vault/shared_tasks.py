"""Vault shared tasks - uses libs/ system"""

from __future__ import annotations
import json
from invoke import task
from libs.console import console, success, error


@task
def status(c) -> dict:
    """Check Vault status"""
    result = c.run("vault status", warn=True, hide=True)
    if result.ok:
        success("Vault: ready")
        return {"is_ready": True, "details": "Vault unsealed"}
    error("Vault: not ready")
    return {"is_ready": False, "details": "Vault unavailable"}


@task
def write_secret(c, path: str, data: str) -> bool:
    """Write to Vault KV v2. Example: --data='key1=val1 key2=val2'"""
    if data.startswith("{"):
        try:
            kv = " ".join(f"{k}={v}" for k, v in json.loads(data).items())
        except json.JSONDecodeError as exc:
            error("Invalid JSON payload", str(exc))
            return False
    else:
        kv = data
    result = c.run(f"vault kv put secret/{path} {kv}", warn=True, hide=True)
    if result.ok:
        success(f"Wrote: {path}")
    else:
        error(f"Failed: {path}", result.stderr)
    return result.ok


@task
def read_secret(c, path: str, field: str | None = None) -> str | dict | None:
    """Read from Vault KV v2"""
    if field:
        result = c.run(
            f"vault kv get -field={field} secret/{path}", warn=True, hide=True
        )
        if result.ok:
            val = result.stdout.strip()
            console.print(val)
            return val
    else:
        result = c.run(f"vault kv get -format=json secret/{path}", warn=True, hide=True)
        if result.ok:
            return json.loads(result.stdout).get("data", {}).get("data", {})
    error(f"Read failed: {path}")
    return None


@task
def list_secrets(c, path: str) -> list[str]:
    """List secrets in path"""
    result = c.run(f"vault kv list secret/{path}", warn=True, hide=True)
    if result.ok:
        keys = [k for k in result.stdout.strip().split("\n")[2:] if k]
        for k in keys:
            console.print(f"  - {k}")
        return keys
    error(f"List failed: {path}")
    return []
