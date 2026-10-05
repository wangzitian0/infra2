"""Service discovery and dynamic Deployer class loading.

Part of libs.deploy domain decomposition (#955, #1009).
"""

from __future__ import annotations

import importlib.util
import sys
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from libs.deploy.deployer import Deployer


def load_deployer_class(
    service_id: str,
    layers: dict[str, Any] | None = None,
    base_class: type[Deployer] | None = None,
) -> type[Deployer] | None:
    """Dynamically import `service_id`'s deploy.py and return its Deployer subclass.

    Every OTHER consumer of a service's facts (service_registry, promote.py's
    assert_approle_creds_present) deliberately reads deploy.py via AST, never
    importing it — importing runs the module's own top-level code. This is the
    one place that import is actually needed: a fixed-compose deploy
    (libs.deploy.promote) that wants to call a real Deployer classmethod (e.g.
    ensure_runtime_secrets, truealpha#447) rather than re-derive its logic.
    Safe to do here because a deploy.py's only load-time side effect
    (make_tasks -> invoke task registration) is gated on its own
    ``shared_tasks = sys.modules.get(...)`` lookup finding something — which is
    never true for a standalone load like this one (mirrors the identical
    precedent in tools/dokploy_config_drift.py and tests' _load_deploy_module).
    Returns None if the service has no deploy.py or it defines no Deployer
    subclass — callers treat that as "nothing to provision", not an error.
    """
    if "/" not in service_id or not layers:
        return None
    layer, name = service_id.split("/", 1)
    layer_path = layers.get(layer)
    if layer_path is None:
        return None
    deploy_file = next(layer_path.glob(f"*.{name}/deploy.py"), None)
    if deploy_file is None:
        return None
    if base_class is None:
        from libs.deploy.deployer import Deployer

        base_class = Deployer
    module_name = f"_deployer_{layer}_{name}"
    spec = importlib.util.spec_from_file_location(module_name, deploy_file)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    for obj in vars(module).values():
        if (
            isinstance(obj, type)
            and issubclass(obj, base_class)
            and obj is not base_class
            and obj.__module__ == module_name
        ):
            return obj
    return None


def discover_services(
    layers: dict[str, Any] | None = None,
) -> dict[str, str]:
    """Discover deployable services based on deploy.py files."""
    if not layers:
        return {}

    service_map: dict[str, str] = {}

    for layer, layer_path in layers.items():
        if not layer_path.exists():
            continue

        for service_dir in layer_path.iterdir():
            if not service_dir.is_dir():
                continue

            parts = service_dir.name.split(".", 1)
            if len(parts) != 2:
                continue

            service_name = parts[1]
            deploy_file = service_dir / "deploy.py"
            if not deploy_file.exists():
                continue

            key = f"{layer}/{service_name}"
            # App layers get prefixed task names (tools/loader.py uses the same
            # prefixes) to avoid colliding with platform/postgres etc.
            task_prefix = {"finance_report": "fr-", "truealpha": "ta-"}.get(layer, "")
            # Invoke exposes collection underscores as dashes on its CLI. The runner
            # executes these values verbatim, so discovery must return the CLI name.
            task_name = service_name.replace("_", "-")
            service_map[key] = f"{task_prefix}{task_name}.sync"

    return service_map


__all__ = ["discover_services", "load_deployer_class"]
