"""Items of the Homer portal "Applications" group, derived from the registry (#1467).

The portal template (`platform/21.portal/config.yml.tmpl`) holds no application
host. For one environment this module derives, for every business application in
``APP_SOURCES``:

- the application link, from the web-root ``PublicRouteFacet`` of its Deployer,
  through ``public_route_host``, the function the public-route probes use;
- a SigNoz dashboard link, only when the application layer holds an
  ``observability/dashboard.json`` file. The SigNoz host comes from the same
  function, so a ``prod_only`` SigNoz has one host in every environment.

A fact the registry lacks raises ``ValueError``. The portal then fails to render
instead of showing a partial group.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from urllib.parse import quote

from common.core.constants import APP_SOURCES
from common.observability.probe_specs import public_route_host

SIGNOZ_SERVICE_ID = "platform/signoz"
# Under the layer root, next to `alert_rules.json`.
DASHBOARD_RELATIVE_PATH = Path("observability") / "dashboard.json"
WEB_ROOT_PATH = "/"
APP_ICON = "fas fa-briefcase"
DASHBOARD_ICON = "fas fa-tachometer-alt"


def _dashboard_title(path: Path) -> str:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{path}: cannot read the dashboard ({exc})") from exc
    title = data.get("title") if isinstance(data, dict) else None
    if not isinstance(title, str) or not title.strip():
        raise ValueError(f"{path}: the dashboard needs a non-empty title")
    return title


def _signoz_base_url(attrs, internal_domain: str, domain_suffix: str) -> str:
    meta = attrs.get(SIGNOZ_SERVICE_ID)
    if meta is None or not meta.public_routes:
        raise ValueError(f"{SIGNOZ_SERVICE_ID} declares no public route")
    host = public_route_host(
        meta,
        meta.public_routes[0],
        domain=internal_domain,
        domain_suffix=domain_suffix,
    )
    return f"https://{host}"


def application_items(
    *,
    internal_domain: str,
    domain_suffix: str,
    attrs: Mapping | None = None,
    layer_paths: Mapping[str, Path] | None = None,
) -> list[dict[str, str]]:
    """Portal items for every business application, in service-id order.

    ``domain_suffix`` is "" for the production portal and "-<env>" for the others.
    ``attrs`` and ``layer_paths`` default to the registry; tests pass their own.
    """
    if attrs is None:
        from common.core.registry import service_attrs

        attrs = service_attrs()
    if layer_paths is None:
        from common.core.registry import _LAYERS

        layer_paths = _LAYERS

    items: list[dict[str, str]] = []
    signoz_base: str | None = None
    for service_id in sorted(APP_SOURCES):
        meta = attrs.get(service_id)
        if meta is None:
            raise ValueError(f"application {service_id} is not registered")
        if meta.prod_only and domain_suffix:
            continue  # the host of a prod_only service exists in production only
        facet = next((f for f in meta.public_routes if f.path == WEB_ROOT_PATH), None)
        if facet is None:
            raise ValueError(f"{service_id} declares no public route at /")
        layer = service_id.split("/", 1)[0]
        name = layer.replace("_", " ").title()
        host = public_route_host(
            meta, facet, domain=internal_domain, domain_suffix=domain_suffix
        )
        items.append(
            {
                "name": name,
                "icon": APP_ICON,
                "subtitle": "Business application",
                "tag": "app",
                "url": f"https://{host}{facet.path}",
                "target": "_blank",
            }
        )
        layer_path = layer_paths.get(layer)
        dashboard = layer_path / DASHBOARD_RELATIVE_PATH if layer_path else None
        if dashboard is None or not dashboard.is_file():
            continue
        title = _dashboard_title(dashboard)
        if signoz_base is None:
            signoz_base = _signoz_base_url(attrs, internal_domain, domain_suffix)
        items.append(
            {
                "name": f"{name} Dashboard",
                "icon": DASHBOARD_ICON,
                "subtitle": title,
                "tag": "dashboard",
                # SigNoz addresses a dashboard by a generated id; its list page
                # filters by title through the `search` query parameter.
                "url": f"{signoz_base}/dashboard?search={quote(title, safe='')}",
                "target": "_blank",
            }
        )
    if not items:
        raise ValueError("no application is registered for this environment")
    return items


def render_items_yaml(items: Iterable[Mapping[str, str]], indent: int = 6) -> str:
    """YAML list items for a Homer group. Values are ASCII JSON strings, valid YAML."""
    pad = " " * indent
    lines: list[str] = []
    for item in items:
        for position, (key, value) in enumerate(item.items()):
            lead = f"{pad}- " if position == 0 else f"{pad}  "
            lines.append(f"{lead}{key}: {json.dumps(value)}")
    return "\n".join(lines)
