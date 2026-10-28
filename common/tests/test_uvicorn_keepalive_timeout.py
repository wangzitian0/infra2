"""Verify that Uvicorn compose entrypoints configure keep-alive timeout above Traefik idle timeout."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
MIN_KEEPALIVE_SECONDS = 95

UVICORN_COMPOSE_TARGETS = (
    ("finance_report/finance_report/10.app/compose.yaml", "backend"),
    ("finance_report/finance_report/preview/compose.yaml", "backend"),
    ("truealpha/truealpha/10.app/compose.yaml", "llm"),
    ("truealpha/truealpha/preview/compose.yaml", "llm"),
)

_KEEPALIVE_PATTERN = re.compile(r"--timeout-keep-alive(?:=|\s+)(\d+)\b")


def _entrypoint_text(compose_path: Path, service_name: str) -> str:
    doc = yaml.safe_load(compose_path.read_text(encoding="utf-8")) or {}
    services = doc.get("services") or {}
    service = services.get(service_name) or {}
    entrypoint = service.get("entrypoint") or []
    if isinstance(entrypoint, str):
        return entrypoint
    return "\n".join(str(part) for part in entrypoint)


def extract_uvicorn_keepalive_seconds(command_text: str) -> int | None:
    """Return the integer value of --timeout-keep-alive from a Uvicorn command."""
    match = _KEEPALIVE_PATTERN.search(command_text)
    if match is None:
        return None
    return int(match.group(1))


@pytest.mark.parametrize(("relative_path", "service_name"), UVICORN_COMPOSE_TARGETS)
def test_uvicorn_compose_entrypoint_keepalive_exceeds_traefik_idle_timeout(
    relative_path: str, service_name: str
) -> None:
    """Each Uvicorn service must keep idle sockets open longer than Traefik (90s)."""
    entrypoint = _entrypoint_text(ROOT / relative_path, service_name)
    assert "exec uvicorn " in entrypoint
    keepalive_seconds = extract_uvicorn_keepalive_seconds(entrypoint)
    assert keepalive_seconds is not None, (
        f"{relative_path}:{service_name} omits --timeout-keep-alive on uvicorn"
    )
    assert keepalive_seconds >= MIN_KEEPALIVE_SECONDS, (
        f"{relative_path}:{service_name} sets --timeout-keep-alive {keepalive_seconds}s, "
        f"which must be >= {MIN_KEEPALIVE_SECONDS}s"
    )


def test_extract_uvicorn_keepalive_seconds_parses_flags() -> None:
    """The parser extracts space-separated and equals-separated timeout flags."""
    assert (
        extract_uvicorn_keepalive_seconds(
            "exec uvicorn src.main:app --host 0.0.0.0 --port 8000 --timeout-keep-alive 120"
        )
        == 120
    )
    assert (
        extract_uvicorn_keepalive_seconds(
            "exec uvicorn src.main:app --timeout-keep-alive=95"
        )
        == 95
    )
    assert (
        extract_uvicorn_keepalive_seconds(
            "exec uvicorn src.main:app --host 0.0.0.0 --port 8000"
        )
        is None
    )
