"""Deploy declaration tests for platform/30.todo.

The Canary app and its behavior tests live in infra2-sdk canary/todo (#247).
"""

from __future__ import annotations

from pathlib import Path

from common.core import registry as reg
from common.deploy.deployer import discover_services

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_todo_service_registered_in_service_registry() -> None:
    attrs = reg.service_attrs()
    assert "platform/todo" in attrs

    meta = attrs["platform/todo"]
    assert meta.service == "todo"
    assert meta.subdomain is None
    assert meta.service_port == 8000
    assert meta.service_name == "todo-v4-13"
    assert meta.compose_path == "platform/30.todo/compose.yaml"
    assert meta.data_path == ""
    assert meta.prod_only is False


def test_todo_service_discovered_by_deployer() -> None:
    discovered = discover_services()
    assert "platform/todo" in discovered
    assert discovered["platform/todo"] == "todo.sync"


def test_prepare_dirs_noop_on_empty_data_path(monkeypatch) -> None:
    """Verify _prepare_dirs returns True without calling ssh when data_path is empty."""
    import common.deploy.deployer as d
    from unittest.mock import MagicMock

    cmds: list[str] = []
    monkeypatch.setattr(d, "validate_env", lambda: [])
    monkeypatch.setattr(d, "run_with_status", lambda c, cmd, label: cmds.append(cmd))

    class DummyStatelessDeployer(d.Deployer):
        service = "stateless-dummy"
        data_path = ""

        @classmethod
        def env(cls):
            return {"ENV": "production", "VPS_HOST": "vps"}

    result = DummyStatelessDeployer._prepare_dirs(MagicMock())
    assert result is True
    assert len(cmds) == 0


def test_todo_traefik_dual_router_auth_contract() -> None:
    """Verify compose.yaml specifies dual routers for Authentik ForwardAuth + public probe bypass."""
    import yaml

    compose_file = REPO_ROOT / "platform/30.todo/compose.yaml"
    assert compose_file.exists()
    from common.deploy.compose_compile import load_stack

    # The deployed text: x-routes compiles to these labels (#1822 Slice 2).
    content = yaml.safe_load(load_stack(compose_file).compiled)

    labels = content["services"]["todo-v4-13"]["labels"]
    labels_dict = {}
    for item in labels:
        if "=" in item:
            k, v = item.split("=", 1)
            labels_dict[k] = v

    # 1. Traefik enabled
    assert labels_dict.get("traefik.enable") == "true"

    # 2. Public bypass router: priority 100, no auth middleware, and it serves ONLY the
    #    health path. Evaluate the rule instead of matching its text (#991).
    from common.tests.traefik_rules import rule_matches

    public_rule = labels_dict.get(
        "traefik.http.routers.platform-todo-public${ENV_DOMAIN_SUFFIX}.rule"
    )
    assert public_rule is not None
    public_rule = public_rule.replace("${ENV_DOMAIN_SUFFIX}", "").replace(
        "${INTERNAL_DOMAIN}", "zitian.party"
    )
    host = "todo.zitian.party"
    assert rule_matches(public_rule, host=host, path="/api/health")
    for path in (
        "/api/canary/status",
        "/api/canary/status/",
        "/api/health/extra",
        "/api/todos",
        "/",
    ):
        assert not rule_matches(public_rule, host=host, path=path), path
    assert not rule_matches(public_rule, host="evil.example", path="/api/health")
    assert (
        labels_dict.get(
            "traefik.http.routers.platform-todo-public${ENV_DOMAIN_SUFFIX}.priority"
        )
        == "100"
    )
    assert (
        "traefik.http.routers.platform-todo-public${ENV_DOMAIN_SUFFIX}.middlewares"
        not in labels_dict
    )

    # 3. Protected router: priority 10, protected by Authentik ForwardAuth middleware
    assert (
        labels_dict.get(
            "traefik.http.routers.platform-todo${ENV_DOMAIN_SUFFIX}.priority"
        )
        == "10"
    )
    assert (
        labels_dict.get(
            "traefik.http.routers.platform-todo${ENV_DOMAIN_SUFFIX}.middlewares"
        )
        == "todo-auth${ENV_DOMAIN_SUFFIX}@docker"
    )

    # 4. Middleware forwardauth points to authentik server
    mw_addr = labels_dict.get(
        "traefik.http.middlewares.todo-auth${ENV_DOMAIN_SUFFIX}.forwardauth.address"
    )
    assert mw_addr is not None
    assert "platform-authentik-server${ENV_SUFFIX}:9000" in mw_addr
    assert "/outpost.goauthentik.io/auth/traefik" in mw_addr


def test_portal_config_template_contains_canary_todo() -> None:
    """Verify platform/21.portal/config.yml.tmpl includes Canary Todo card."""
    import yaml

    portal_tmpl = REPO_ROOT / "platform/21.portal/config.yml.tmpl"
    assert portal_tmpl.exists()
    content = yaml.safe_load(portal_tmpl.read_text())

    platform_services = None
    for group in content.get("services", []):
        if group.get("name") == "Platform Services":
            platform_services = group
            break
    assert platform_services is not None, (
        "Platform Services section not found in Homer config"
    )

    todo_item = None
    for item in platform_services.get("items", []):
        if item.get("name") == "Canary Todo":
            todo_item = item
            break
    assert todo_item is not None, "Canary Todo item not found in Homer config"
    assert todo_item["url"] == "https://todo{{ENV_DOMAIN_SUFFIX}}.{{INTERNAL_DOMAIN}}"
    assert todo_item["tag"] == "canary"


def test_todo_slot_commands_escape_database_url_for_compose() -> None:
    """Slot shell commands must escape $$DATABASE_URL so Compose preserves the Vault-sourced variable."""
    import re
    import yaml

    from common.deploy.compose_compile import load_stack

    compose_file = REPO_ROOT / "platform/30.todo/compose.yaml"
    content = yaml.safe_load(load_stack(compose_file).compiled)

    slot_services = [
        (name, svc)
        for name, svc in content["services"].items()
        if name.startswith("todo-")
    ]
    assert slot_services, "expected todo-* slot services in compiled compose.yaml"

    for name, svc in slot_services:
        cmd = svc.get("command")
        cmd_text = (
            " ".join(str(part) for part in cmd)
            if isinstance(cmd, list)
            else str(cmd or "")
        )
        assert "$$DATABASE_URL" in cmd_text, f"{name} must escape $$DATABASE_URL"
        assert re.search(r"(?<!\$)\$DATABASE_URL", cmd_text) is None, (
            f"{name} contains unescaped $DATABASE_URL"
        )
