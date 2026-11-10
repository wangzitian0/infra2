"""Infra2 Core Domain Constants (SSOT)."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
HARNESS_REPOS_YAML = REPO_ROOT / "harness" / "repos.yaml"

PRODUCTION = "production"
STAGING = "staging"
PREVIEW = "preview"

DEPLOYMENT_ENV_PRODUCTION = PRODUCTION
DEPLOYMENT_ENV_STAGING = STAGING
DEPLOYMENT_ENV_PREVIEW = PREVIEW
STATEFUL_DEPLOY_ENVIRONMENTS = (
    DEPLOYMENT_ENV_PREVIEW,
    DEPLOYMENT_ENV_STAGING,
    DEPLOYMENT_ENV_PRODUCTION,
)

MANAGED_BY = "infra2"
DOCKER_LABEL_PREFIX = "party.zitian.infra"
IDENTITY_SCHEMA_VERSION = "v1"
PREVIEW_KINDS = ("branch", "pr")

# GitHub Repository Configuration
GITHUB_OWNER = "wangzitian0"
GITHUB_REPO = "infra2"
GITHUB_BRANCH = "main"

_DEFAULT_APP_SOURCES: dict[str, str] = {
    "finance_report/app": "wangzitian0/finance_report",
    "truealpha/app": "wangzitian0/truealpha",
}


def _repo_slug(raw_source: str) -> str:
    cleaned = raw_source.strip().removesuffix(".git").rstrip("/")
    if cleaned.startswith("https://github.com/"):
        return cleaned.removeprefix("https://github.com/")
    if cleaned.startswith("http://github.com/"):
        return cleaned.removeprefix("http://github.com/")
    return cleaned


def _parse_repos_yaml_stdlib(raw_text: str) -> dict[str, object]:
    """Minimal stdlib parser for ``harness/repos.yaml`` when PyYAML is not installed."""
    repositories: list[dict[str, str]] = []
    repos_map: dict[str, dict[str, str]] = {}
    section = ""
    current_item: dict[str, str] | None = None
    current_repo_key = ""

    for raw_line in raw_text.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        if indent == 0 and stripped.endswith(":"):
            section = stripped[:-1].strip()
            current_item = None
            current_repo_key = ""
            continue
        if section == "repositories":
            if stripped.startswith("- "):
                current_item = {}
                repositories.append(current_item)
                kv = stripped[2:].strip()
                if ":" in kv:
                    k, _, v = kv.partition(":")
                    current_item[k.strip()] = v.strip().strip("'\"")
            elif indent == 4 and current_item is not None and ":" in stripped:
                k, _, v = stripped.partition(":")
                val = v.strip().strip("'\"")
                if val:
                    current_item[k.strip()] = val
        elif section == "repos":
            if indent == 2 and stripped.endswith(":"):
                current_repo_key = stripped[:-1].strip().strip("'\"")
                repos_map[current_repo_key] = {}
            elif indent >= 4 and current_repo_key and ":" in stripped:
                k, _, v = stripped.partition(":")
                val = v.strip().strip("'\"")
                if val:
                    repos_map[current_repo_key][k.strip()] = val

    out: dict[str, object] = {}
    if repositories:
        out["repositories"] = repositories
    if repos_map:
        out["repos"] = repos_map
    return out


def _load_app_sources_from_repos_yaml(
    repos_yaml: Path = HARNESS_REPOS_YAML,
) -> dict[str, str]:
    """Derive ``APP_SOURCES`` from ``harness/repos.yaml`` external-application entries."""
    if not repos_yaml.is_file():
        return dict(_DEFAULT_APP_SOURCES)
    try:
        raw_text = repos_yaml.read_text(encoding="utf-8")
    except OSError:
        return dict(_DEFAULT_APP_SOURCES)
    try:
        import yaml  # type: ignore[import-untyped]

        data = yaml.safe_load(raw_text) or {}
    except ModuleNotFoundError:
        data = _parse_repos_yaml_stdlib(raw_text)
    except Exception:  # noqa: BLE001
        return dict(_DEFAULT_APP_SOURCES)
    if not isinstance(data, Mapping):
        return dict(_DEFAULT_APP_SOURCES)

    out: dict[str, str] = {}
    repositories = data.get("repositories")
    if isinstance(repositories, list):
        for entry in repositories:
            if not isinstance(entry, Mapping):
                continue
            if (
                entry.get("role") != "external-application"
                and "release_tag" not in entry
            ):
                continue
            raw_source = str(entry.get("source") or entry.get("repo") or "").strip()
            raw_path = str(entry.get("path") or "").strip()
            raw_id = str(entry.get("id") or "").strip()
            app_name = Path(raw_path).name if raw_path else raw_id.replace("-", "_")
            slug = _repo_slug(raw_source)
            if app_name and slug:
                out[f"{app_name}/app"] = slug

    repos_map = data.get("repos")
    if isinstance(repos_map, Mapping):
        for raw_key, entry in repos_map.items():
            if not isinstance(entry, Mapping):
                continue
            raw_source = str(entry.get("repo") or entry.get("source") or "").strip()
            app_name = str(raw_key).strip().replace("-", "_")
            slug = _repo_slug(raw_source)
            if app_name and slug:
                key = app_name if "/" in app_name else f"{app_name}/app"
                out[key] = slug

    return out if out else dict(_DEFAULT_APP_SOURCES)


APP_SOURCES: dict[str, str] = _load_app_sources_from_repos_yaml()
PROJECT_REPOS: dict[str, str] = {
    service.split("/", 1)[0]: repo for service, repo in APP_SOURCES.items()
}


__all__ = [
    "APP_SOURCES",
    "DEPLOYMENT_ENV_PREVIEW",
    "DEPLOYMENT_ENV_PRODUCTION",
    "DEPLOYMENT_ENV_STAGING",
    "DOCKER_LABEL_PREFIX",
    "GITHUB_BRANCH",
    "GITHUB_OWNER",
    "GITHUB_REPO",
    "HARNESS_REPOS_YAML",
    "IDENTITY_SCHEMA_VERSION",
    "MANAGED_BY",
    "PREVIEW",
    "PREVIEW_KINDS",
    "PRODUCTION",
    "PROJECT_REPOS",
    "REPO_ROOT",
    "STAGING",
    "STATEFUL_DEPLOY_ENVIRONMENTS",
]
