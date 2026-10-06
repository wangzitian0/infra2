"""Unit tests for S3 application bucket creation and encryption probes (#1010)."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from unittest.mock import MagicMock, patch

from invoke import Context

REPO_ROOT = Path(__file__).resolve().parents[2]
SHARED_TASKS_PATH = REPO_ROOT / "platform/03.s3/shared_tasks.py"

spec = importlib.util.spec_from_file_location(
    "platform.03.s3.shared", SHARED_TASKS_PATH
)
assert spec and spec.loader
s3_shared = importlib.util.module_from_spec(spec)
sys.modules["platform.03.s3.shared"] = s3_shared
spec.loader.exec_module(s3_shared)
create_app_bucket = s3_shared.create_app_bucket


class MockContext(Context):
    """Invoke Context mock that tracks commands and stubs responses."""

    def __init__(self, responses: dict[str, MagicMock] | None = None) -> None:
        super().__init__()
        self.responses = responses or {}
        self.command_history: list[str] = []

    def run(self, cmd: str, **kwargs):
        self.command_history.append(cmd)
        for pattern, result in self.responses.items():
            if pattern in cmd:
                return result
        res = MagicMock()
        res.ok = True
        res.stdout = "true"
        res.stderr = ""
        return res


@patch.object(s3_shared, "get_env", return_value={"ENV": "production"})
@patch.object(s3_shared, "_ensure_admin_alias", return_value=True)
def test_create_app_bucket_defaults_to_no_encryption(_mock_alias, _mock_env) -> None:
    """Bucket creation must default enable_encryption to False without KMS (#1010)."""
    ctx = MockContext()
    result = create_app_bucket(ctx, bucket_name="test-bucket")

    assert result is not None
    assert result["bucket"] == "test-bucket"

    # Verify mc encrypt was NOT invoked
    assert not any("mc encrypt" in cmd for cmd in ctx.command_history)


@patch.object(s3_shared, "get_env", return_value={"ENV": "production"})
@patch.object(s3_shared, "_ensure_admin_alias", return_value=True)
def test_create_app_bucket_with_encryption_verified_by_probe(
    _mock_alias, _mock_env
) -> None:
    """When encryption is enabled, write probe must verify backend KMS support."""
    ctx = MockContext()
    result = create_app_bucket(
        ctx,
        bucket_name="test-bucket",
        enable_encryption=True,
    )

    assert result is not None
    assert result["bucket"] == "test-bucket"

    assert any("mc encrypt set sse-s3" in cmd for cmd in ctx.command_history)
    assert any(
        ".probe-encryption-test-bucket" in cmd and "mc pipe" in cmd
        for cmd in ctx.command_history
    )
    assert any(
        "mc rm --force" in cmd and ".probe-encryption-test-bucket" in cmd
        for cmd in ctx.command_history
    )


@patch.object(s3_shared, "get_env", return_value={"ENV": "production"})
@patch.object(s3_shared, "_ensure_admin_alias", return_value=True)
def test_create_app_bucket_fails_loud_and_clears_on_probe_failure(
    _mock_alias, _mock_env
) -> None:
    """If PUT probe fails after enabling encryption, clear encryption and return None."""
    probe_fail = MagicMock()
    probe_fail.ok = False
    probe_fail.stderr = "InvalidRequest: SSE-S3 requires RUSTFS_SSE"

    responses = {
        "mc pipe": probe_fail,
    }
    ctx = MockContext(responses)
    result = create_app_bucket(
        ctx,
        bucket_name="test-bucket",
        enable_encryption=True,
    )

    assert result is None

    assert any("mc encrypt set sse-s3" in cmd for cmd in ctx.command_history)
    assert any(
        "mc encrypt clear local/test-bucket" in cmd for cmd in ctx.command_history
    )
