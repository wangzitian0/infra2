"""Strict primitives shared by versioned wire contracts (absorbed from infra2-sdk)."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def canonical_json_bytes(payload: Any) -> bytes:
    """Serialize payload into canonical UTF-8 JSON bytes."""
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode()


def canonical_sha256(payload: Any) -> str:
    """Return lowercase hex SHA-256 of canonical UTF-8 JSON bytes."""
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def require_contract_version(value: Any, expected: int, *, description: str) -> int:
    if type(value) is not int:
        raise ValueError("contract_version must be an integer")
    if value != expected:
        raise ValueError(f"unsupported {description} {value}")
    return value


def parse_contract_version(
    raw: Mapping[str, Any], expected: int, *, description: str
) -> int:
    return require_contract_version(
        raw.get("contract_version", 0), expected, description=description
    )


def parse_string(
    raw: Mapping[str, Any], key: str, *, required: bool = True, default: str = ""
) -> str:
    value = raw.get(key)
    if value is None:
        if required:
            raise ValueError(f"{key} is required")
        return default
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    value = value.strip()
    if required and not value:
        raise ValueError(f"{key} is required")
    return value


def reject_unknown(data: Mapping[str, Any], allowed: Iterable[str], label: str) -> None:
    if unknown := sorted(set(data) - set(allowed)):
        raise ValueError(f"unknown {label} field(s): {unknown}")


def parse_bool(raw: Mapping[str, Any], key: str, *, default: bool = False) -> bool:
    value = raw.get(key, default)
    if type(value) is not bool:
        raise ValueError(f"{key} must be a boolean")
    return value


def validate_http_url(
    value: str, *, label: str, allow_fragment: bool = True, validate_port: bool = False
) -> None:
    try:
        ep = urlsplit(value)
        host = ep.hostname
        if validate_port:
            _ = ep.port
    except ValueError:
        raise ValueError(f"{label} must be a valid HTTP URL") from None
    if (
        ep.scheme not in {"http", "https"}
        or not ep.netloc
        or not host
        or any(c.isspace() for c in ep.netloc)
    ):
        raise ValueError(f"{label} must use http:// or https:// with a host")
    if ep.username is not None or ep.password is not None:
        raise ValueError(f"{label} must not contain credentials")
    if not allow_fragment and ep.fragment:
        raise ValueError(f"{label} must not contain a fragment")


def _is_loopback_host(host: str) -> bool:
    return host.strip("[]").lower() in _LOOPBACK_HOSTS


def parse_iso_utc(value: object, *, allow_naive: bool = False) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("expected non-empty ISO 8601 string")
    parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        if not allow_naive:
            raise ValueError("timestamp must have a time zone")
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def redact_text(text: str, env: Mapping[str, str], env_re: re.Pattern[str]) -> str:
    vals = {
        v.strip()
        for k, v in env.items()
        if env_re.search(k) and isinstance(v, str) and len(v.strip()) >= 4
    }
    for val in sorted(vals, key=len, reverse=True):
        text = text.replace(val, "***")
    return text


def _yaml() -> Any:
    import yaml

    return yaml


_LEGACY_TRUEALPHA_SOURCES = frozenset(
    {
        "apps/llm-service",
        "apps/data-engine",
        "infra2/truealpha/app",
        "infra2/truealpha/data_engine",
    }
)


def strip_legacy_manifest_fields(raw: Any) -> Any:
    """Strip retired v4.9 manifest fields before strict v4.11 ContractModel validation."""
    # TODO(wave3): remove once apps release v4.11.1 manifests (no mirror_to_1password, sensitive DATABASE_URL).
    if not isinstance(raw, Mapping):
        return raw
    if "contract_version" not in raw:
        raise ValueError("environment manifest requires 'contract_version'")
    parse_contract_version(raw, 2, description="environment manifest version")
    fields = raw.get("fields")
    if not isinstance(fields, list):
        return raw
    is_legacy_truealpha = str(raw.get("source") or "") in _LEGACY_TRUEALPHA_SOURCES
    cleaned_fields = []
    for fld in fields:
        if isinstance(fld, Mapping):
            item = {k: v for k, v in fld.items() if k != "mirror_to_1password"}
            if (
                is_legacy_truealpha
                and item.get("env") == "DATABASE_URL"
                and item.get("sensitive") is False
            ):
                item["sensitive"] = True
            cleaned_fields.append(item)
        else:
            cleaned_fields.append(fld)
    return {**raw, "fields": cleaned_fields}


@dataclass(frozen=True)
class CheckVerdict:
    name: str
    ok: bool
    detail: str = ""
    severity: str = ""
    failure_domain: str = ""
    seconds: float = field(default=0.0, kw_only=True)
    skipped: bool = field(default=False, kw_only=True)

    def __post_init__(self) -> None:
        if type(self.skipped) is not bool:
            raise ValueError("skipped must be a bool")
        if self.skipped and self.ok:
            raise ValueError("a skipped check cannot have ok=True")

    @property
    def status(self) -> str:
        return "skipped" if self.skipped else ("pass" if self.ok else "fail")


def record_verdicts(
    path: str | Path,
    *,
    source: str,
    checks: Iterable[CheckVerdict],
    complete: bool = True,
) -> None:
    line = json.dumps(
        {"source": source, "checks": [asdict(c) for c in checks], "complete": complete},
        sort_keys=True,
    )
    target = Path(path)
    with target.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def field_store_key(field: Any) -> str:
    """Return the Vault key name for an ``EnvironmentField``."""
    return field.store_key or field.env


def field_rendered(field: Any) -> bool:
    """Return True when an ``EnvironmentField`` is rendered into the Vault template."""
    return field.store_backed or bool(field.provided_by) or bool(field.composed_from)


__all__ = [
    "CheckVerdict",
    "_is_loopback_host",
    "_yaml",
    "canonical_json_bytes",
    "canonical_sha256",
    "field_rendered",
    "field_store_key",
    "parse_bool",
    "parse_contract_version",
    "parse_iso_utc",
    "parse_string",
    "record_verdicts",
    "redact_text",
    "reject_unknown",
    "require_contract_version",
    "strip_legacy_manifest_fields",
    "validate_http_url",
]
