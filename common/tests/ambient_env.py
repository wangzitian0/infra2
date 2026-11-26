"""Clear the telemetry variables of the developer shell before a test loads an app.

`platform/12.alerting/app.py` calls `init_service` when the module
loads. `init_service` reads the service name, the version and the `OTEL_*` variables of the SDK
contract. In its default mode it also reads the tier variables. infra2-sdk reads no
retired alias (`ENV`, `APP_ENV`, `IMAGE_TAG`), so only contract names can stop a strict
start (#1238, #1823 N1). A shell that exports an endpoint but no
`OTEL_SERVICE_NAME` made the todo app raise while a test only loaded it.

The helper removes every variable and alias that the SDK contract declares, and every name that
starts with `OTEL_`. It reads the contract from the SDK, so it keeps no list of its own and
follows each SDK release. A test that needs a variable sets it after this call.
"""

from __future__ import annotations

from importlib.resources import files
import os

import pytest
import yaml


def load_shared_env_contract() -> dict:
    """Load the canonical SDK ``data/shared_env.yaml`` contract."""
    return (
        yaml.safe_load(
            files("infra2_sdk")
            .joinpath("data/shared_env.yaml")
            .read_text(encoding="utf-8")
        )
        or {}
    )


def _contract_names() -> set[str]:
    """Every variable and every alias that the SDK contract declares."""
    names: set[str] = set()
    for variable in (load_shared_env_contract().get("groups") or {}).get(
        "runtime"
    ) or ():
        names.add(variable["name"])
        names.update(variable.get("aliases") or ())
    return names


def clear_telemetry_env(monkeypatch: pytest.MonkeyPatch) -> None:
    declared = _contract_names()
    for key in list(os.environ):
        if key in declared or key.startswith("OTEL_"):
            monkeypatch.delenv(key, raising=False)
