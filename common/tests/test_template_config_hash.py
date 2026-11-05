"""A template rendered to a `${DATA_PATH}` host mount must change the config hash (#1438).

Before the fix, `sync` printed "config unchanged, skipping deploy" after a change to
`platform/11.signoz/otel-collector-config.yaml`, and the host kept the old file.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from common.deploy import deploy_guard_audit
from common.deploy.config_hash import (
    _artifact_items_from_disk,
    _compose_artifact_files,
    config_hash_from_items,
)

ROOT = Path(__file__).resolve().parents[2]

# service directory -> template file that deploy.py renders to a ${DATA_PATH} host file
TEMPLATE_SERVICES = {
    "platform/03.clickhouse": "config.xml",
    "platform/11.signoz": "otel-collector-config.yaml",
    "platform/21.portal": "config.yml.tmpl",
    "platform/25.free": "config.json.tmpl",
}

COMPOSE = (
    "services:\n"
    "  app:\n"
    "    image: x\n"
    "    volumes:\n"
    "      - ${DATA_PATH}/app.cfg:/etc/app.cfg:ro\n"
    "      - ${DATA_PATH}/data:/var/lib/app\n"
)


def _hash(compose_path: Path) -> str:
    text = compose_path.read_text(encoding="utf-8")
    return config_hash_from_items(
        text, {"A": "1"}, _artifact_items_from_disk(str(compose_path), text), []
    )


def _service(tmp_path: Path, template: str = "app.cfg.tmpl") -> Path:
    service_dir = tmp_path / "platform" / "99.ghost"
    service_dir.mkdir(parents=True)
    compose = service_dir / "compose.yaml"
    compose.write_text(COMPOSE, encoding="utf-8")
    if template:
        (service_dir / template).write_text("listen: {{PORT}}\n", encoding="utf-8")
    return compose


@pytest.mark.parametrize("template_name", ["app.cfg", "app.cfg.tmpl"])
def test_template_edit_changes_config_hash(tmp_path: Path, template_name: str) -> None:
    compose = _service(tmp_path, template_name)
    before = _hash(compose)
    (compose.parent / template_name).write_text("listen: {{PORT}}\nnew: 1\n")
    assert _hash(compose) != before


def test_data_directory_mount_is_not_a_hash_input(tmp_path: Path) -> None:
    compose = _service(tmp_path)
    (compose.parent / "data").mkdir()
    (compose.parent / "data" / "runtime.db").write_text("x")
    names = {p.name for p in _compose_artifact_files(str(compose), COMPOSE)}
    assert names == {"app.cfg.tmpl"}


@pytest.mark.parametrize("service_dir,template", sorted(TEMPLATE_SERVICES.items()))
def test_real_service_hashes_its_template(service_dir: str, template: str) -> None:
    compose = ROOT / service_dir / "compose.yaml"
    files = _compose_artifact_files(str(compose), compose.read_text(encoding="utf-8"))
    assert (ROOT / service_dir / template).resolve() in files


def test_guard_passes_on_real_tree() -> None:
    composes = deploy_guard_audit.find_service_composes()
    assert deploy_guard_audit.host_mount_violations(composes) == []
    assert deploy_guard_audit.audit() == []


def test_guard_names_service_that_has_no_template(tmp_path: Path) -> None:
    compose = _service(tmp_path, template="")
    violations = deploy_guard_audit.host_mount_violations([compose], root=tmp_path)
    assert len(violations) == 1
    assert "platform/99.ghost/compose.yaml" in violations[0]
    assert "'app.cfg'" in violations[0]


def test_guard_ignores_directory_mounts(tmp_path: Path) -> None:
    compose = _service(tmp_path)
    assert deploy_guard_audit.host_mount_violations([compose], root=tmp_path) == []


def _load_deploy_module(service_dir: str):
    spec = importlib.util.spec_from_file_location(
        f"deploy_{service_dir.split('.')[-1]}", ROOT / service_dir / "deploy.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "service_dir,cls_name",
    [
        ("platform/03.clickhouse", "ClickHouseDeployer"),
        ("platform/21.portal", "PortalDeployer"),
    ],
)
def test_composing_delivers_config_so_sync_ships_template(
    monkeypatch, service_dir: str, cls_name: str
) -> None:
    """`sync` runs composing but never pre_compose; delivery must happen in composing."""
    from common.deploy.deployer import Deployer

    cls = getattr(_load_deploy_module(service_dir), cls_name)
    calls: list[str] = []
    monkeypatch.setattr(cls, "env", classmethod(lambda _c: {"ENV": "x"}))
    monkeypatch.setattr(
        cls,
        "_deliver_config",
        classmethod(lambda _c, _ctx, _e: calls.append("d") or "p"),
    )
    monkeypatch.setattr(
        Deployer,
        "composing",
        classmethod(lambda _c, _ctx, _v: calls.append("c") or "id"),
    )
    assert cls.composing(None, {}) == "id"
    assert calls == ["d", "c"]
