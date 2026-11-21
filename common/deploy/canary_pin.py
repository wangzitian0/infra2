"""The Canary Todo image pin and the SDK ranges infra2 accepts (infra2-sdk#247, #1823 Step 0).

infra2-sdk builds the Canary Todo image from ``canary/todo`` at each release tag ``vX.Y.Z`` and
publishes ``ghcr.io/wangzitian0/infra2-canary-todo:vX.Y.Z``. The image installs the SDK from the
same source, so the image tag is the SDK version that the Canary proves. The ``todo`` service of
``platform/30.todo/compose.yaml`` holds the one pin: ``<repository>:v<version>@sha256:<digest>``.
This module is the one reader of that line.

infra2 does not track one SDK version for the apps. It accepts:

- an app SDK version inside ``APP_SDK_RANGE`` (lower bound inclusive, upper bound exclusive), and
- only a version that the Canary verified in staging (``verified_sdk_versions``).

The hook manifest schema range is the one of the SDK that infra2 itself pins
(``infra2_sdk.manifests.SUPPORTED_HOOKS_SCHEMA_VERSIONS``); this module does not copy it.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from common.deploy.yaml_text import safe_load_text

REPO_ROOT = Path(__file__).resolve().parents[2]
CANARY_COMPOSE_REL = "platform/30.todo/compose.yaml"
CANARY_IMAGE_REPOSITORY = "ghcr.io/wangzitian0/infra2-canary-todo"
CANARY_SERVICE = "todo"

# Lower bound: the oldest SDK an app still pins (finance_report and truealpha, 3.32.0).
# Upper bound: the next major after 4.x. Narrow the lower bound after every app moves.
APP_SDK_RANGE: tuple[str, str] = ("3.32.0", "5.0.0")

_PIN_RE = re.compile(
    rf"^{re.escape(CANARY_IMAGE_REPOSITORY)}:v(?P<version>\d+\.\d+\.\d+)"
    r"@(?P<digest>sha256:[0-9a-f]{64})$"
)


class CanaryPinError(ValueError):
    """The Canary compose does not hold one valid image pin."""


def canary_image_from_compose_text(text: str) -> tuple[str, str, str]:
    """Return ``(image_ref, sdk_version, digest)`` of the ``todo`` service, or raise."""
    data = safe_load_text(yaml, text) or {}
    service = (data.get("services") or {}).get(CANARY_SERVICE) or {}
    if "build" in service:
        raise CanaryPinError(
            f"{CANARY_COMPOSE_REL} service {CANARY_SERVICE!r} must not build: "
            "the image comes from infra2-sdk"
        )
    image = str(service.get("image") or "")
    match = _PIN_RE.match(image)
    if not match:
        raise CanaryPinError(
            f"{CANARY_COMPOSE_REL} service {CANARY_SERVICE!r} image {image!r} is not "
            f"{CANARY_IMAGE_REPOSITORY}:vX.Y.Z@sha256:<64 hex>"
        )
    return image, match["version"], match["digest"]


def canary_sdk_version(repo_root: Path = REPO_ROOT) -> str:
    """The SDK version that the Canary image of this checkout runs."""
    text = (repo_root / CANARY_COMPOSE_REL).read_text(encoding="utf-8")
    return canary_image_from_compose_text(text)[1]


def canary_sdk_unit(repo_root: Path = REPO_ROOT) -> str:
    """Stage record unit ``sdk:<version>`` of the Canary image. ``sdk:unknown`` on error."""
    try:
        return f"sdk:{canary_sdk_version(repo_root)}"
    except (OSError, CanaryPinError) as exc:
        print(
            f"::warning::Canary SDK pin unreadable ({exc}); the SDK unit is sdk:unknown"
        )
        return "sdk:unknown"


def _version_tuple(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def in_app_sdk_range(version: str) -> bool:
    """True when ``version`` is inside ``APP_SDK_RANGE``."""
    low, high = APP_SDK_RANGE
    try:
        value = _version_tuple(version)
    except ValueError:
        return False
    return _version_tuple(low) <= value < _version_tuple(high)
