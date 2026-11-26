"""#161: staging/production environment isolation fail-fast.

A reference to a per-environment internal service as a NETWORK HOST must carry
${ENV_SUFFIX}; otherwise the staging stack silently talks to the production
instance (e.g. staging Prefect's forward-auth hitting prod Authentik). This lint
fails CI so such a leak can't land again.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from common.deploy.compose_compile import compile_stack, has_x_infra2

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


def _vault_name_line(text: str) -> int | None:
    """The 1-based line of `x-infra2.vault.name`, if the compiled text proves its suffix.

    The compiler names the sidecar `<name>-vault-agent${ENV_SUFFIX}`, so the source key
    is not a host. The line is skipped only when the compiled text carries that name.
    """
    if not has_x_infra2(text):
        return None
    root = yaml.compose(text)
    for key, value in root.value:
        if key.value != "x-infra2" or not isinstance(value, yaml.MappingNode):
            continue
        for vkey, vault in value.value:
            if vkey.value != "vault" or not isinstance(vault, yaml.MappingNode):
                continue
            for nkey, name in vault.value:
                if nkey.value == "name":
                    compiled = compile_stack(text, vault_template="X=1\n")
                    proof = f"container_name: {name.value}-vault-agent${{ENV_SUFFIX}}"
                    return name.start_mark.line + 1 if proof in compiled else None
    return None


def _violations(label: str, text: str) -> list[str]:
    skip = _vault_name_line(text)
    found = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        # Skip comments and image tags (image:name:tag is not a host ref).
        if stripped.startswith("#") or "image:" in stripped or lineno == skip:
            continue
        if _HOST_REF.search(line):
            found.append(f"{label}:{lineno}: {stripped[:110]}")
    return found


def test_internal_service_host_refs_carry_env_suffix() -> None:
    violations: list[str] = []
    for compose in _platform_composes():
        text = compose.read_text(encoding="utf-8")
        violations += _violations(str(compose.relative_to(ROOT)), text)

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
    assert not _HOST_REF.search(
        "PREFECT_REDIS_MESSAGING_HOST: platform-redis${ENV_SUFFIX}"
    )
    # a longer container name must not match on a shorter service prefix:
    assert not _HOST_REF.search(
        "container_name: platform-postgres-vault-agent${ENV_SUFFIX}"
    )
    # prod_only shared services (no suffix is correct) must NOT match:
    assert not _HOST_REF.search("http://platform-signoz:8080/api/v1/health")
    assert not _HOST_REF.search("http://platform-clickhouse:8123/ping")


def test_the_vault_name_is_skipped_only_when_the_compiled_sidecar_has_the_suffix(
    monkeypatch,
) -> None:
    """`x-infra2.vault.name` is a container prefix, not a host (#1822 step B).

    The compiler appends `-vault-agent${ENV_SUFFIX}`. A host reference elsewhere in
    the source is still found, and a compiler without the suffix loses the skip.
    """
    source = (
        "x-infra2:\n  vault:\n    name: platform-redis\n"
        "services:\n  app:\n    image: a:1\n    command: [run]\n    x-vault: {}\n"
        "    environment:\n      REDIS_HOST: platform-redis\n"
    )
    assert _violations("src", source) == ["src:10: REDIS_HOST: platform-redis"]
    redis = (ROOT / "platform/02.redis/compose.yaml").read_text(encoding="utf-8")
    assert _vault_name_line(redis) is not None
    assert _violations("redis", redis) == []

    import common.tests.test_env_isolation as mod

    real = mod.compile_stack
    monkeypatch.setattr(
        mod,
        "compile_stack",
        lambda text, **k: real(text, **k).replace("${ENV_SUFFIX}", ""),
    )
    assert _vault_name_line(redis) is None
    assert _violations("redis", redis) == ["redis:9: name: platform-redis"]
