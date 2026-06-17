"""#372: SigNoz `sync` must ship otel-collector-config.yaml.

`Deployer.sync` deliberately skips `pre_compose` side effects, but it always calls
`composing`. So the collector-config delivery must run from `composing` (not only
`pre_compose`), or config changes (e.g. the CORS block) never reach the host and
the recreated collector re-mounts a stale file. These tests pin that wiring.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _load_signoz_deployer():
    path = ROOT / "platform/11.signoz/deploy.py"
    spec = importlib.util.spec_from_file_location("signoz_deploy_under_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.SigNozDeployer


def test_composing_delivers_collector_config():
    """composing() invokes the shared config-delivery step (so sync ships config)."""
    D = _load_signoz_deployer()
    env = {"INTERNAL_DOMAIN": "zitian.party", "VPS_HOST": "vps"}
    deliver = mock.MagicMock(return_value=True)
    with (
        mock.patch.object(D, "env", mock.MagicMock(return_value=env)),
        mock.patch.object(D, "_deliver_collector_config", deliver),
        # delivery runs BEFORE super().composing(); stub the base to a sentinel so
        # we don't hit Dokploy and can assert delivery happened first.
        mock.patch(
            "libs.deployer.Deployer.composing",
            classmethod(
                lambda cls, c, env_vars: (_ for _ in ()).throw(
                    RuntimeError("stop-after-deliver")
                )
            ),
        ),
    ):
        with pytest.raises(RuntimeError, match="stop-after-deliver"):
            D.composing(mock.MagicMock(), {})
    deliver.assert_called_once()


def test_composing_fails_closed_when_config_delivery_fails():
    """If the collector config can't be delivered, composing() must raise — never
    silently (re)deploy a collector that would mount a stale config."""
    D = _load_signoz_deployer()
    env = {"INTERNAL_DOMAIN": "zitian.party", "VPS_HOST": "vps"}
    with (
        mock.patch.object(D, "env", mock.MagicMock(return_value=env)),
        mock.patch.object(
            D, "_deliver_collector_config", mock.MagicMock(return_value=False)
        ),
    ):
        with pytest.raises(RuntimeError, match="otel-collector config"):
            D.composing(mock.MagicMock(), {})


def test_otel_collector_config_has_durable_exporter_queue() -> None:
    """#369: the OTel collector must retry + persist its sending_queue to disk so a
    transient ClickHouse outage or a collector restart retains telemetry instead of
    dropping the in-flight batch (it was memory-only with no retry before)."""
    import yaml
    from pathlib import Path

    cfg = yaml.safe_load(
        (Path(__file__).resolve().parents[2] / "platform/11.signoz/otel-collector-config.yaml")
        .read_text(encoding="utf-8")
    )
    assert "file_storage" in cfg["extensions"], "file_storage extension missing"
    assert "file_storage" in cfg["service"]["extensions"], "file_storage not enabled"
    for name in ("clickhousetraces", "signozclickhousemetrics", "clickhouselogsexporter"):
        exp = cfg["exporters"][name]
        assert exp["retry_on_failure"]["enabled"] is True, f"{name}: retry_on_failure off"
        assert exp["sending_queue"]["enabled"] is True, f"{name}: sending_queue off"
        assert exp["sending_queue"]["storage"] == "file_storage", f"{name}: queue not on disk"


def test_otel_queue_dir_is_mounted_and_provisioned() -> None:
    """#369: the disk-queue dir must be bind-mounted into the collector AND pre-created +
    chowned to the collector uid (10001) on every deploy path, or the non-root collector
    crash-loops on the file_storage extension."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    compose = (root / "platform/11.signoz/compose.yaml").read_text(encoding="utf-8")
    assert "${DATA_PATH}/otel-queue:/var/lib/otelcol/file_storage" in compose
    deploy = (root / "platform/11.signoz/deploy.py").read_text(encoding="utf-8")
    assert "otel-queue" in deploy and "chown -R 10001:0" in deploy
