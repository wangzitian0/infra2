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
