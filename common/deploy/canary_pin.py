"""The Canary Todo slots/pins and the SDK ranges infra2 accepts (infra2-sdk#247, #1823 Step 0).

infra2-sdk builds ``infra2-canary-todo:vX.Y.Z`` at each release tag. Active minor-series
slots live in ``platform/30.todo/slots.yaml`` and run side-by-side in
``platform/30.todo/compose.yaml`` so every active consumer minor series retains a live canary.
This module is the single reader for both ``slots.yaml`` and ``compose.yaml`` image pins.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from common.deploy.yaml_text import safe_load_text

REPO_ROOT = Path(__file__).resolve().parents[2]
CANARY_COMPOSE_REL = "platform/30.todo/compose.yaml"
CANARY_SLOTS_REL = "platform/30.todo/slots.yaml"
CANARY_IMAGE_REPOSITORY = "ghcr.io/wangzitian0/infra2-canary-todo"
CANARY_SERVICE = "todo"
PRIMARY_CANARY_SLOT = "v4-13"

# Lower bound: the oldest SDK an app runs in staging or production (4.5.0).
# Upper bound: the next major after 4.x.
APP_SDK_RANGE: tuple[str, str] = ("4.5.0", "5.0.0")

_PIN_RE = re.compile(
    rf"^{re.escape(CANARY_IMAGE_REPOSITORY)}:v(?P<version>\d+\.\d+\.\d+)"
    r"@(?P<digest>sha256:[0-9a-f]{64})$"
)
_SLOT_ID_RE = re.compile(r"^v\d+-\d+$")
_PG_IDENT_RE = re.compile(r"^[a-z][a-z0-9_]{1,62}$")


@dataclass(frozen=True)
class CanarySlot:
    """One declarative Canary Todo minor-version runtime slot."""

    slot: str
    version: str
    digest: str
    compose_service: str
    container_prefix: str
    otel_service_name: str
    database: str
    pg_role: str
    redis_key_prefix: str
    s3_key_prefix: str
    primary: bool = False

    @property
    def image(self) -> str:
        return f"{CANARY_IMAGE_REPOSITORY}:v{self.version}@{self.digest}"

    @property
    def minor_series(self) -> tuple[int, int]:
        parts = _version_tuple(self.version)
        return (parts[0], parts[1])


class CanaryPinError(ValueError):
    """The Canary compose or slots manifest does not hold valid image pins."""


class CanaryImageMissing(CanaryPinError):
    """ghcr.io answers 404 for the manifest of the tag: the image is not published yet."""


def _parse_slot_mapping(raw: object) -> CanarySlot:
    if not isinstance(raw, dict):
        raise CanaryPinError(f"canary slot entry must be a mapping, got {raw!r}")
    slot = str(raw.get("slot") or "")
    if not _SLOT_ID_RE.fullmatch(slot):
        raise CanaryPinError(
            f"invalid canary slot id {slot!r}; expected v<major>-<minor>"
        )
    version = str(raw.get("version") or "")
    digest = str(raw.get("digest") or "")
    if not re.fullmatch(r"\d+\.\d+\.\d+", version) or not re.fullmatch(
        r"sha256:[0-9a-f]{64}", digest
    ):
        raise CanaryPinError(f"slot {slot}: invalid pin v{version}@{digest}")
    database = str(raw.get("database") or f"todo_{slot.replace('-', '_')}")
    pg_role = str(raw.get("pg_role") or f"todo_{slot.replace('-', '_')}_app")
    if not _PG_IDENT_RE.fullmatch(database) or not _PG_IDENT_RE.fullmatch(pg_role):
        raise CanaryPinError(
            f"slot {slot}: invalid Postgres database/role {database!r}/{pg_role!r}"
        )
    return CanarySlot(
        slot=slot,
        version=version,
        digest=digest,
        compose_service=str(raw.get("compose_service") or f"todo-{slot}"),
        container_prefix=str(raw.get("container_prefix") or f"platform-todo-{slot}"),
        otel_service_name=str(raw.get("otel_service_name") or f"platform-todo-{slot}"),
        database=database,
        pg_role=pg_role,
        redis_key_prefix=str(raw.get("redis_key_prefix") or f"canary:{slot}:probe:"),
        s3_key_prefix=str(raw.get("s3_key_prefix") or f"canary/{slot}/"),
        primary=bool(raw.get("primary", False)),
    )


def load_canary_slots(repo_root: Path = REPO_ROOT) -> tuple[CanarySlot, ...]:
    """Load and validate ``platform/30.todo/slots.yaml`` against ``compose.yaml``."""
    slots_path = repo_root / CANARY_SLOTS_REL
    if not slots_path.is_file():
        raise CanaryPinError(f"missing {CANARY_SLOTS_REL}")
    data = safe_load_text(yaml, slots_path.read_text(encoding="utf-8")) or {}
    raw_slots = data.get("slots")
    if not isinstance(raw_slots, list) or not raw_slots:
        raise CanaryPinError(f"{CANARY_SLOTS_REL} must list at least one slot")
    slots = tuple(_parse_slot_mapping(item) for item in raw_slots)
    primaries = [s for s in slots if s.primary]
    if len(primaries) != 1:
        raise CanaryPinError(
            f"{CANARY_SLOTS_REL} must mark exactly one primary slot, found {len(primaries)}"
        )
    if len({s.slot for s in slots}) != len(slots):
        raise CanaryPinError(f"{CANARY_SLOTS_REL} contains duplicate slot ids")
    compose_path = repo_root / CANARY_COMPOSE_REL
    if compose_path.is_file():
        compose_data = (
            safe_load_text(yaml, compose_path.read_text(encoding="utf-8")) or {}
        )
        services = compose_data.get("services") or {}
        for s in slots:
            svc = services.get(s.compose_service)
            if not isinstance(svc, dict):
                raise CanaryPinError(
                    f"{CANARY_COMPOSE_REL} missing slot service {s.compose_service!r}"
                )
            if "build" in svc:
                raise CanaryPinError(
                    f"{CANARY_COMPOSE_REL} service {s.compose_service!r} must not build"
                )
            if str(svc.get("image") or "") != s.image:
                raise CanaryPinError(
                    f"{CANARY_COMPOSE_REL} service {s.compose_service!r} image "
                    f"{svc.get('image')!r} != {s.image!r} from {CANARY_SLOTS_REL}"
                )
    return slots


def primary_canary_slot(repo_root: Path = REPO_ROOT) -> CanarySlot:
    """Return the primary active Canary slot from ``slots.yaml``."""
    return next(s for s in load_canary_slots(repo_root) if s.primary)


def canary_image_from_compose_text(text: str) -> tuple[str, str, str]:
    """Return ``(image_ref, sdk_version, digest)`` of the primary todo service, or raise."""
    data = safe_load_text(yaml, text) or {}
    services = data.get("services") or {}
    if not isinstance(services, dict):
        raise CanaryPinError(f"{CANARY_COMPOSE_REL} has invalid services mapping")

    candidates: list[tuple[str, str, str]] = []
    preferred: tuple[str, str, str] | None = None
    slot_services = [
        (name, spec)
        for name, spec in services.items()
        if (name == CANARY_SERVICE or name.startswith(f"{CANARY_SERVICE}-"))
        and isinstance(spec, dict)
    ]
    if not slot_services:
        raise CanaryPinError(
            f"{CANARY_COMPOSE_REL} service {CANARY_SERVICE!r} image '' is not "
            f"{CANARY_IMAGE_REPOSITORY}:vX.Y.Z@sha256:<64 hex>"
        )
    for name, service in slot_services:
        if "build" in service:
            raise CanaryPinError(
                f"{CANARY_COMPOSE_REL} service {name!r} must not build: "
                "the image comes from infra2-sdk"
            )
        image = str(service.get("image") or "")
        match = _PIN_RE.match(image)
        if not match:
            raise CanaryPinError(
                f"{CANARY_COMPOSE_REL} service {name!r} image {image!r} is not "
                f"{CANARY_IMAGE_REPOSITORY}:vX.Y.Z@sha256:<64 hex>"
            )
        parsed = (image, match["version"], match["digest"])
        candidates.append(parsed)
        env = service.get("environment") or {}
        if isinstance(env, dict) and str(env.get("CANARY_DOMAIN", "")).startswith(
            "todo${"
        ):
            preferred = parsed
    if preferred is not None:
        return preferred
    # Highest semantic version wins across multi-slot compose files.
    return max(candidates, key=lambda item: _version_tuple(item[1]))


def canary_sdk_version(repo_root: Path = REPO_ROOT) -> str:
    """The primary SDK version that the Canary stack of this checkout proves."""
    slots_path = repo_root / CANARY_SLOTS_REL
    if slots_path.is_file():
        return primary_canary_slot(repo_root).version
    text = (repo_root / CANARY_COMPOSE_REL).read_text(encoding="utf-8")
    return canary_image_from_compose_text(text)[1]


def canary_sdk_unit(repo_root: Path = REPO_ROOT) -> str:
    """Stage record unit ``sdk:<version>`` of the primary Canary image. Fail closed on error."""
    return f"sdk:{canary_sdk_version(repo_root)}"


CANARY_COMPOSE_RELS: tuple[str, ...] = (
    CANARY_COMPOSE_REL,
    "platform/30.todo/preview.compose.yaml",
)
_REGISTRY = "https://ghcr.io"
_MANIFEST_ACCEPT = ", ".join(
    (
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    )
)


def resolve_canary_digest(version: str, *, opener=None) -> str:
    """The ``sha256:`` digest of ``infra2-canary-todo:v<version>`` on ghcr.io (public pull)."""
    import json
    import urllib.error
    import urllib.request

    open_url = opener or urllib.request.urlopen
    repository = CANARY_IMAGE_REPOSITORY.removeprefix("ghcr.io/")
    with open_url(
        f"{_REGISTRY}/token?scope=repository:{repository}:pull", timeout=20
    ) as reply:
        token = json.load(reply)["token"]
    request = urllib.request.Request(
        f"{_REGISTRY}/v2/{repository}/manifests/v{version}",
        method="HEAD",
        headers={"Authorization": f"Bearer {token}", "Accept": _MANIFEST_ACCEPT},
    )
    try:
        with open_url(request, timeout=20) as reply:
            digest = reply.headers.get("Docker-Content-Digest", "")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise CanaryImageMissing(
                f"infra2-canary-todo:v{version} is not on ghcr.io"
            ) from exc
        raise
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise CanaryPinError(f"ghcr.io gave no digest for v{version}: {digest!r}")
    return digest


def rewrite_canary_pin(text: str, version: str, digest: str) -> str:
    """Replace the Canary image pin for matching minor series (or unique pin) in compose text."""
    if not re.fullmatch(r"\d+\.\d+\.\d+", version) or not re.fullmatch(
        r"sha256:[0-9a-f]{64}", digest
    ):
        raise CanaryPinError(f"invalid pin v{version}@{digest}")
    major, minor, _ = version.split(".")
    series_pattern = re.compile(
        rf"{re.escape(CANARY_IMAGE_REPOSITORY)}:v{re.escape(major)}\.{re.escape(minor)}\.\d+@sha256:[0-9a-f]{{64}}"
    )
    series_matches = series_pattern.findall(text)
    if len(series_matches) == 1:
        return series_pattern.sub(
            f"{CANARY_IMAGE_REPOSITORY}:v{version}@{digest}", text
        )
    pattern = re.compile(
        rf"{re.escape(CANARY_IMAGE_REPOSITORY)}:v\d+\.\d+\.\d+@sha256:[0-9a-f]{{64}}"
    )
    found = pattern.findall(text)
    if len(found) != 1:
        raise CanaryPinError(f"expected one Canary image pin, found {len(found)}")
    return pattern.sub(f"{CANARY_IMAGE_REPOSITORY}:v{version}@{digest}", text)


_UV_SDK_VERSION_RE = re.compile(
    r'name\s*=\s*"infra2-sdk"\s*(?:\n[^\[]*?)*?(?:version\s*=\s*"(?P<ver>\d+\.\d+\.\d+)"|infra2_sdk-(?P<whl>\d+\.\d+\.\d+)-)',
    re.MULTILINE,
)


def can_retire_canary_slot(
    slot_version: str, lock_texts: dict[str, str]
) -> tuple[bool, tuple[str, ...]]:
    """Return ``(safe_to_retire, referencing_repos)`` across consumer ``uv.lock`` texts.

    A Canary slot minor series ``M.m.*`` is safe to retire only when zero consumer repos
    lock ``infra2-sdk`` in that ``M.m`` minor series (or if it is not the only remaining slot).
    """
    target_minor = _version_tuple(slot_version)[:2]
    users: list[str] = []
    for repo_id, lock_text in sorted(lock_texts.items()):
        for match in _UV_SDK_VERSION_RE.finditer(lock_text):
            locked_ver = match.group("ver") or match.group("whl") or ""
            if locked_ver and _version_tuple(locked_ver)[:2] == target_minor:
                if repo_id not in users:
                    users.append(repo_id)
    return (len(users) == 0, tuple(users))


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
