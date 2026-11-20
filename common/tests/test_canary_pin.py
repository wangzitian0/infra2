"""The Canary Todo image pin and the app SDK range (common/deploy/canary_pin.py, infra2-sdk#247)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from common.deploy import canary_pin
from common.deploy.canary_pin import (
    CANARY_IMAGE_REPOSITORY,
    CanaryPinError,
    canary_image_from_compose_text,
    canary_sdk_unit,
    canary_sdk_version,
    in_app_sdk_range,
)
from tools import reconcile_iac_inputs as reconcile

REPO_ROOT = Path(__file__).resolve().parents[2]
DIGEST = "sha256:" + "a" * 64


def _compose(todo: str) -> str:
    return f"services:\n  todo:\n{todo}"


def test_the_checkout_pins_one_published_canary_image() -> None:
    ref, version, digest = canary_image_from_compose_text(
        (REPO_ROOT / "platform/30.todo/compose.yaml").read_text(encoding="utf-8")
    )
    assert ref == f"{CANARY_IMAGE_REPOSITORY}:v{version}@{digest}"
    assert re.fullmatch(r"\d+\.\d+\.\d+", version)
    assert in_app_sdk_range(version)
    assert canary_sdk_version() == version


def test_the_preview_compose_runs_the_same_canary_image() -> None:
    staging = canary_image_from_compose_text(
        (REPO_ROOT / "platform/30.todo/compose.yaml").read_text(encoding="utf-8")
    )
    preview = canary_image_from_compose_text(
        (REPO_ROOT / "platform/30.todo/preview.compose.yaml").read_text(
            encoding="utf-8"
        )
    )
    assert preview == staging


def test_the_app_source_is_not_in_infra2() -> None:
    """The app lives in infra2-sdk canary/todo. A copy here would drift from the image."""
    for name in ("app.py", "ui.py", "Dockerfile"):
        assert not (REPO_ROOT / "platform/30.todo" / name).exists(), name


@pytest.mark.parametrize(
    ("todo", "reason"),
    [
        (
            f"    build: .\n    image: {CANARY_IMAGE_REPOSITORY}:v3.35.0@{DIGEST}\n",
            "must not build",
        ),
        (f"    image: {CANARY_IMAGE_REPOSITORY}:v3.35.0\n", "is not"),
        (f"    image: {CANARY_IMAGE_REPOSITORY}@{DIGEST}\n", "is not"),
        (f"    image: ghcr.io/someone/else:v3.35.0@{DIGEST}\n", "is not"),
        (f"    image: {CANARY_IMAGE_REPOSITORY}:latest@{DIGEST}\n", "is not"),
        ("    container_name: x\n", "is not"),
    ],
)
def test_an_invalid_pin_fails_closed(todo: str, reason: str) -> None:
    with pytest.raises(CanaryPinError, match=reason):
        canary_image_from_compose_text(_compose(todo))


def test_the_sdk_unit_names_the_canary_image_not_the_runner(
    tmp_path, monkeypatch
) -> None:
    compose = tmp_path / "platform/30.todo/compose.yaml"
    compose.parent.mkdir(parents=True)
    compose.write_text(
        _compose(f"    image: {CANARY_IMAGE_REPOSITORY}:v9.8.7@{DIGEST}\n")
    )
    assert canary_sdk_unit(tmp_path) == "sdk:9.8.7"
    monkeypatch.setattr(canary_pin, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(reconcile, "canary_sdk_unit", lambda: canary_sdk_unit(tmp_path))
    plan = reconcile.build_plan(["platform/12.alerting/compose.yaml"])
    assert reconcile.plan_units(plan)[-1] == "sdk:9.8.7"


def test_an_unreadable_pin_gives_sdk_unknown(tmp_path, capsys) -> None:
    assert canary_sdk_unit(tmp_path) == "sdk:unknown"
    assert "::warning::" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("version", "inside"),
    [
        ("3.31.9", False),
        ("3.32.0", True),
        ("3.35.0", True),
        ("4.9.9", True),
        ("5.0.0", False),
        ("not-a-version", False),
    ],
)
def test_the_app_sdk_range(version: str, inside: bool) -> None:
    assert in_app_sdk_range(version) is inside


def test_a_verified_sdk_outside_the_range_is_still_held(monkeypatch) -> None:
    monkeypatch.setattr(
        reconcile, "locked_sdk_version_for_app_ref", lambda *a, **k: "3.31.0"
    )
    with pytest.raises(
        reconcile.UnverifiedSdkError, match="outside the accepted app range"
    ):
        reconcile.assert_app_sdk_version_verified(
            "finance_report/app", "v0.0.1", verified_versions={"3.31.0"}
        )


def test_an_sdk_inside_the_range_still_needs_the_canary(monkeypatch) -> None:
    monkeypatch.setattr(
        reconcile, "locked_sdk_version_for_app_ref", lambda *a, **k: "3.35.0"
    )
    with pytest.raises(reconcile.UnverifiedSdkError, match="has not passed"):
        reconcile.assert_app_sdk_version_verified(
            "finance_report/app", "v0.0.1", verified_versions={"3.34.1"}
        )
    assert (
        reconcile.assert_app_sdk_version_verified(
            "finance_report/app", "v0.0.1", verified_versions={"3.35.0"}
        )
        == "3.35.0"
    )
