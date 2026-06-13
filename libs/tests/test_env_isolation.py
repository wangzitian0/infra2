"""#161: staging/production environment isolation fail-fast.

A reference to a per-environment internal service as a NETWORK HOST must carry
${ENV_SUFFIX}; otherwise the staging stack silently talks to the production
instance (e.g. staging Prefect's forward-auth hitting prod Authentik). This lint
fails CI so such a leak can't land again.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# Internal services that run a separate instance per environment. A host
# reference to them MUST be suffixed with ${ENV_SUFFIX}.
SUFFIXABLE = (
    "postgres",
    "redis",
    "authentik-server",
    "authentik-worker",
    "minio",
    "prefect-server",
    "prefect-services",
    "prefect-worker",
    "portal",
    "alerting",
)
# prod_only services (signoz, clickhouse, openpanel) run as a single shared
# instance for all envs — no suffix is correct, so they are NOT in SUFFIXABLE.

# `platform-<svc>` used as a host where the service name is NOT immediately
# followed by ${ENV_SUFFIX}. `(?![\w-])` ensures <svc> is a complete token (so
# `platform-postgres` does not match inside `platform-postgres-vault-agent`),
# which also lets us catch bare host-only refs (no `:port`/`/path`), e.g.
# `SOME_HOST: platform-redis`, not just URL/DSN forms.
_HOST_REF = re.compile(
    r"platform-(?:" + "|".join(SUFFIXABLE) + r")(?![\w-])(?!\$\{ENV_SUFFIX\})"
)


def _platform_composes() -> list[Path]:
    return sorted((ROOT / "platform").glob("*/compose.yaml"))


def test_internal_service_host_refs_carry_env_suffix() -> None:
    violations: list[str] = []
    for compose in _platform_composes():
        for lineno, line in enumerate(
            compose.read_text(encoding="utf-8").splitlines(), start=1
        ):
            stripped = line.strip()
            # Skip comments and image tags (image:name:tag is not a host ref).
            if stripped.startswith("#") or "image:" in stripped:
                continue
            if _HOST_REF.search(line):
                violations.append(
                    f"{compose.relative_to(ROOT)}:{lineno}: {stripped[:110]}"
                )

    assert not violations, (
        "Internal service host reference(s) missing ${ENV_SUFFIX} — staging would "
        "talk to the PRODUCTION instance (env-isolation leak). Add ${ENV_SUFFIX} "
        "to the host, or, for a prod_only shared service, this lint should not "
        "match it:\n" + "\n".join(violations)
    )


def test_lint_detects_missing_suffix_and_allows_correct() -> None:
    """Self-check: the lint flags a bare host ref and passes a suffixed/prod_only
    one — so the test above isn't a vacuous pass."""
    # the exact #161 bug (staging would hit prod Authentik):
    assert _HOST_REF.search("http://platform-authentik-server:9000/auth")
    assert _HOST_REF.search("@platform-postgres:5432/prefect")
    # bare host-only refs (no :port or /path) must also be caught:
    assert _HOST_REF.search("PREFECT_REDIS_MESSAGING_HOST: platform-redis")
    assert _HOST_REF.search("PREFECT_API_URL: platform-prefect-server")
    # correct (env-suffixed) forms must NOT match — incl. bare host-only:
    assert not _HOST_REF.search("http://platform-authentik-server${ENV_SUFFIX}:9000/")
    assert not _HOST_REF.search("@platform-postgres${ENV_SUFFIX}:5432/prefect")
    assert not _HOST_REF.search("PREFECT_REDIS_MESSAGING_HOST: platform-redis${ENV_SUFFIX}")
    # a longer container name must not match on a shorter service prefix:
    assert not _HOST_REF.search("container_name: platform-postgres-vault-agent${ENV_SUFFIX}")
    # prod_only shared services (no suffix is correct) must NOT match:
    assert not _HOST_REF.search("http://platform-signoz:8080/api/v1/health")
    assert not _HOST_REF.search("http://platform-clickhouse:8123/ping")
