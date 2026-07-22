from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
SERVICE_DIR = ROOT / "truealpha/truealpha/10.app"


def test_api_router_does_not_claim_the_bare_api_prefix() -> None:
    """truealpha#463: a bare PathPrefix(`/api`) on the llm router claims every
    /api/* path on the shared Host, including web's own Next.js API routes
    (/api/auth/login, /api/auth/me) -- silently 404ing them at llm instead of
    ever reaching web. Login was broken on both staging and production for
    this exact reason. The llm router's rule must stay scoped to exactly what
    apps/llm-service/src/llm_service/main.py serves (GET /health, and the MCP
    app mounted at /mcp) so the rest of /api/* falls through to web's
    lower-priority Host()-only router."""
    compose = yaml.safe_load((SERVICE_DIR / "compose.yaml").read_text(encoding="utf-8"))
    llm_labels = compose["services"]["llm"]["labels"]
    rule_label = next(label for label in llm_labels if ".rule=" in label and "truealpha-api" in label)
    rule = rule_label.split(".rule=", 1)[1]

    assert "PathPrefix(`/api`)" not in rule, "the llm router must not claim the bare /api prefix"
    assert "PathPrefix(`/api/mcp`)" in rule
    assert "Path(`/api/health`)" in rule

    web_labels = compose["services"]["web"]["labels"]
    web_rule_label = next(label for label in web_labels if ".rule=" in label and "truealpha-web" in label)
    # web's router must stay a bare Host() match (no PathPrefix of its own) so it
    # remains the catch-all for every /api/* path the llm router no longer claims.
    assert "PathPrefix" not in web_rule_label.split(".rule=", 1)[1]
