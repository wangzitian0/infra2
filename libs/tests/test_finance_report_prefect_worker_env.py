"""The finance_report Prefect worker knows where its Prefect server is.

Until #639 the vault-agent template rendered PREFECT_API_URL (with the platform default)
into /secrets/.env for every container of the stack; #639 moved it into the compose file
for `backend` only. The worker then let Prefect start an ephemeral API server, which timed
out, and the container crash-looped at ~1.5 cores per environment (2026-09-08, 71 restarts
on production). Every service of the stack that talks to Prefect names the server here.
"""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "finance_report/finance_report/10.app/compose.yaml"
TEMPLATE = ROOT / "finance_report/finance_report/10.app/secrets.ctmpl"
PREFECT_SERVER = "http://platform-prefect-server${ENV_SUFFIX}:4200/api"


def _environment(service: dict) -> dict[str, str]:
    env = service.get("environment") or {}
    if isinstance(env, list):
        return dict(item.split("=", 1) for item in env)
    return {str(k): str(v) for k, v in env.items()}


def test_backend_and_worker_both_name_the_prefect_server() -> None:
    services = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"]
    for name in ("backend", "prefect-worker"):
        assert _environment(services[name]).get("PREFECT_API_URL") == PREFECT_SERVER, (
            name
        )


def test_the_template_no_longer_carries_the_value_so_compose_is_the_only_source() -> (
    None
):
    assert "PREFECT_API_URL" not in TEMPLATE.read_text(encoding="utf-8")
