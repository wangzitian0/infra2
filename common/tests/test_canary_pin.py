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


def test_plan_units_carry_sdk_unknown_when_the_pin_is_unreadable(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(reconcile, "canary_sdk_unit", lambda: canary_sdk_unit(tmp_path))
    plan = reconcile.build_plan(["platform/12.alerting/compose.yaml"])
    assert reconcile.plan_units(plan)[-1] == "sdk:unknown"


def test_canary_verify_production_fallback_never_reads_the_new_pin(tmp_path) -> None:
    from tools import canary_verify

    compose = tmp_path / "platform/30.todo/compose.yaml"
    compose.parent.mkdir(parents=True)
    compose.write_text(
        _compose(f"    image: {CANARY_IMAGE_REPOSITORY}:v9.8.7@{DIGEST}\n")
    )

    def no_git(*_a, **_k):
        raise OSError("no git")

    staging = canary_verify._resolve_locked_sdk_version(
        environment="staging", repo_root=tmp_path, runner=no_git
    )
    production = canary_verify._resolve_locked_sdk_version(
        environment="production", repo_root=tmp_path, runner=no_git
    )
    assert staging == "9.8.7"
    assert production != "9.8.7"


def test_probe_health_reads_the_pin_format_that_canary_pin_owns() -> None:
    """probe_health runs in the hook cwd without `common` on the path, so it keeps its own
    regex. This test keeps the two in step on the real compose and on invalid pins."""
    from common.tests.conftest import load_repo_module

    probe = load_repo_module(
        "platform/30.todo/probe_health.py", "probe_health_pin_format"
    )
    real = (REPO_ROOT / "platform/30.todo/compose.yaml").read_text(encoding="utf-8")
    assert probe.CANARY_PIN_RE.findall(real) == [
        canary_image_from_compose_text(real)[1]
    ]
    for bad in (
        f"{CANARY_IMAGE_REPOSITORY}:v3.35.0",
        f"{CANARY_IMAGE_REPOSITORY}@{DIGEST}",
        f"ghcr.io/someone/else:v3.35.0@{DIGEST}",
        f"{CANARY_IMAGE_REPOSITORY}:latest@{DIGEST}",
    ):
        assert probe.CANARY_PIN_RE.findall(bad) == [], bad
        with pytest.raises(CanaryPinError):
            canary_image_from_compose_text(_compose(f"    image: {bad}\n"))


def test_rewrite_canary_pin_replaces_exactly_one_pin() -> None:
    from common.deploy.canary_pin import rewrite_canary_pin

    new_digest = "sha256:" + "c" * 64
    text = _compose(f"    image: {CANARY_IMAGE_REPOSITORY}:v3.35.0@{DIGEST}\n")
    out = rewrite_canary_pin(text, "3.36.0", new_digest)
    assert canary_image_from_compose_text(out)[1:] == ("3.36.0", new_digest)
    with pytest.raises(CanaryPinError, match="found 2"):
        rewrite_canary_pin(
            text + f"# {CANARY_IMAGE_REPOSITORY}:v1.0.0@{DIGEST}\n",
            "3.36.0",
            new_digest,
        )
    with pytest.raises(CanaryPinError, match="found 0"):
        rewrite_canary_pin("services: {}\n", "3.36.0", new_digest)
    with pytest.raises(CanaryPinError, match="invalid pin"):
        rewrite_canary_pin(text, "latest", new_digest)


def test_resolve_canary_digest_reads_the_registry_header() -> None:
    import io
    import json as _json

    from common.deploy.canary_pin import resolve_canary_digest

    seen: list[str] = []

    class _Reply(io.BytesIO):
        def __init__(self, body: bytes = b"", headers: dict | None = None):
            super().__init__(body)
            self.headers = headers or {}

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    def opener(target, timeout):
        url = getattr(target, "full_url", target)
        seen.append(f"{getattr(target, 'method', 'GET')} {url}")
        if "/token?" in url:
            return _Reply(_json.dumps({"token": "t"}).encode())
        assert target.headers["Authorization"] == "Bearer t"
        accept = target.headers["Accept"]
        assert "application/vnd.oci.image.index.v1+json" in accept
        assert "application/vnd.docker.distribution.manifest.v2+json" in accept
        return _Reply(headers={"Docker-Content-Digest": "sha256:" + "d" * 64})

    assert resolve_canary_digest("3.35.1", opener=opener) == "sha256:" + "d" * 64
    assert (
        seen[1]
        == "HEAD https://ghcr.io/v2/wangzitian0/infra2-canary-todo/manifests/v3.35.1"
    )

    def bad(target, timeout):
        url = getattr(target, "full_url", target)
        if "/token?" in url:
            return _Reply(_json.dumps({"token": "t"}).encode())
        return _Reply(headers={})

    with pytest.raises(CanaryPinError, match="no digest"):
        resolve_canary_digest("3.35.1", opener=bad)
