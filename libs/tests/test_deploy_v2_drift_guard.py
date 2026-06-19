"""Guard against republishing retired deploy entrypoints in infra2 docs/code."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
TEXT_SUFFIXES = {".md", ".py", ".yaml", ".yml", ".txt", ".toml"}
SCAN_ROOTS = [
    ".github",
    "docs",
    "finance_report",
    "libs/tests",
    "platform",
    "README.md",
    "tools",
]


def _scan_text_files() -> list[tuple[Path, str]]:
    files: list[tuple[Path, str]] = []
    this_file = Path(__file__).resolve()
    for rel in SCAN_ROOTS:
        path = ROOT / rel
        candidates = [path] if path.is_file() else list(path.rglob("*"))
        for candidate in candidates:
            if not candidate.is_file() or candidate.suffix not in TEXT_SUFFIXES:
                continue
            if candidate.resolve() == this_file:
                continue
            if "__pycache__" in candidate.parts:
                continue
            files.append((candidate.relative_to(ROOT), candidate.read_text(encoding="utf-8")))
    return files


def _command(service: str) -> str:
    return "invoke " + service + ".setup"


def _phrase(left: str, right: str) -> str:
    return left + right


def test_deploy_v2_front_door_is_the_documented_deploy_path() -> None:
    """Retired setup commands must not be presented as deploy instructions."""
    retired_public_contract = "deploy(env, code, " + "data)"
    primitive_data_arg = 'parser.add_argument(\n        "--' + 'data"'
    banned_snippets = {
        retired_public_contract: "retired public deploy tuple",
        primitive_data_arg: "primitive data override CLI",
        _command("fr-app"): "old prefixed app setup deploy command",
        _command("fr-postgres"): "old prefixed postgres setup deploy command",
        _command("fr-redis"): "old prefixed redis setup deploy command",
        _command("finance_report.app"): "old namespaced app setup deploy command",
        _command("finance_report.postgres"): "old namespaced postgres setup deploy command",
        _command("finance_report.redis"): "old namespaced redis setup deploy command",
        _command("postgres"): "old platform postgres setup deploy command",
        _command("redis"): "old platform redis setup deploy command",
        _command("clickhouse"): "old platform clickhouse setup deploy command",
        _command("signoz"): "old platform signoz setup deploy command",
        _command("alerting"): "old platform alerting setup deploy command",
        _command("openpanel"): "old platform openpanel setup deploy command",
        _command("authentik"): "old platform authentik setup deploy command",
        _command("minio"): "old platform minio setup deploy command",
        _command("portal"): "old platform portal setup deploy command",
        _command("prefect"): "old platform prefect setup deploy command",
        _command("activepieces"): "old platform activepieces setup deploy command",
        "invoke <service>" + ".setup": "old generic service setup deploy command",
        _phrase("v2 public ", "input"): "stale data-axis public-input wording",
        _phrase("public ", "input"): "stale data-axis public-input wording",
        _phrase("data-axis side", " effects"): "future data-axis wording",
        _phrase("data axis", " lands"): "future data-axis wording",
        _phrase(
            "platform services join when the deployer path is ",
            "unified",
        ): "stale platform cutover wording",
        _phrase("cutover ", "未做"): "stale platform cutover wording",
        _phrase("尚未", "接管"): "stale platform cutover wording",
    }

    offenders: list[str] = []
    for rel, text in _scan_text_files():
        lowered = text.lower()
        for snippet, reason in banned_snippets.items():
            haystack = lowered if snippet.isascii() else text
            needle = snippet.lower() if snippet.isascii() else snippet
            if needle in haystack:
                offenders.append(f"{rel}: {reason}: {snippet!r}")

    assert offenders == [], "deploy_v2 drift found:\n" + "\n".join(offenders)
