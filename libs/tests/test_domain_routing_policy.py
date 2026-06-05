"""Static tests for Dokploy domain vs compose Traefik routing policy."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[2]


def test_compose_owned_traefik_routes_do_not_use_dokploy_domains() -> None:
    """Infra-011.5: explicit compose routers require subdomain=None."""
    violations = []
    for deploy_path in _deploy_paths():
        for attrs in _deployer_attrs(deploy_path):
            compose_path = attrs.get("compose_path")
            if not compose_path:
                continue
            compose = ROOT / str(compose_path)
            if not compose.exists() or not _has_explicit_traefik_routes(compose):
                continue
            if attrs.get("subdomain"):
                violations.append(
                    f"{deploy_path.relative_to(ROOT)} sets subdomain={attrs['subdomain']!r} "
                    f"while {compose.relative_to(ROOT)} owns explicit Traefik routes"
                )

    assert not violations, "Routing policy violations:\n" + "\n".join(violations)


def _deploy_paths() -> list[Path]:
    return [
        path
        for path in ROOT.rglob("deploy.py")
        if ".venv" not in path.parts and "__pycache__" not in path.parts
    ]


def _deployer_attrs(path: Path) -> list[dict[str, Any]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    deployers: list[dict[str, Any]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or not _inherits_deployer(node):
            continue
        attrs: dict[str, Any] = {}
        for stmt in node.body:
            names, value = _class_assignment(stmt)
            for name in names:
                if name in {"compose_path", "subdomain"}:
                    attrs[name] = value
        deployers.append(attrs)
    return deployers


def _inherits_deployer(node: ast.ClassDef) -> bool:
    for base in node.bases:
        if isinstance(base, ast.Name) and base.id == "Deployer":
            return True
    return False


def _class_assignment(stmt: ast.stmt) -> tuple[list[str], Any]:
    if isinstance(stmt, ast.Assign):
        names = [target.id for target in stmt.targets if isinstance(target, ast.Name)]
        return names, _literal_value(stmt.value)
    if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
        return [stmt.target.id], _literal_value(stmt.value)
    return [], None


def _literal_value(node: ast.AST) -> Any:
    if isinstance(node, ast.Constant):
        return node.value
    return None


def _has_explicit_traefik_routes(compose_path: Path) -> bool:
    compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    services = compose.get("services", {}) if isinstance(compose, dict) else {}
    for service in services.values():
        labels = service.get("labels", []) if isinstance(service, dict) else []
        for label in labels:
            if not isinstance(label, str):
                continue
            if label.startswith("traefik.http.routers.") or label.startswith(
                "traefik.http.services."
            ):
                return True
    return False
