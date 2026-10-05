"""Deterministic configuration and artifact hashing for Deployer.

Part of libs.deploy domain decomposition (#955, #1009).
"""

from __future__ import annotations

from collections.abc import Iterable
import glob as _glob
import hashlib
import json
from pathlib import Path
import shlex

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _compute_config_hash(
    compose_content: str,
    env_vars: dict[str, str],
    artifact_payload: str = "",
) -> str:
    """Compute hash of compose content + env vars for change detection."""
    # Normalize env vars (sort keys). Empty values COUNT: a variable flipping from a
    # value to "" changes the container env just as surely (#663 review) and must
    # redeploy; only an absent key is absent.
    env_str = "\n".join(
        f"{k}={'' if v is None else v}" for k, v in sorted(env_vars.items())
    )
    combined = (
        f"{compose_content}\n---ENV---\n{env_str}\n---ARTIFACTS---\n{artifact_payload}"
    )
    return hashlib.sha256(combined.encode()).hexdigest()[:12]


def _iter_path_files(path: Path) -> list[Path]:
    if path.is_file():
        return [path]
    if not path.is_dir():
        return []
    return sorted(
        child
        for child in path.rglob("*")
        if child.is_file()
        and "__pycache__" not in child.parts
        and not child.name.endswith((".pyc", ".pyo"))
    )


def _resolve_compose_relative(compose_dir: Path, source: str) -> Path | None:
    if not source or "${" in source or source.startswith("/"):
        return None
    return (compose_dir / source).resolve()


def _resolve_build_relative(context_dir: Path, source: str) -> Path | None:
    if (
        not source
        or "${" in source
        or source.startswith("/")
        or source.startswith("--")
    ):
        return None
    return (context_dir / source).resolve()


def _dockerfile_copy_sources(dockerfile: Path, context_dir: Path) -> list[Path]:
    if not dockerfile.exists():
        return []

    sources: list[Path] = []
    for raw_line in dockerfile.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        instruction, _, remainder = line.partition(" ")
        if instruction.upper() not in {"COPY", "ADD"} or not remainder:
            continue
        if "--from=" in remainder:
            continue

        parsed_sources: list[str] = []
        if remainder.startswith("["):
            try:
                values = json.loads(remainder)
            except json.JSONDecodeError:
                values = []
            if isinstance(values, list) and len(values) >= 2:
                parsed_sources = [str(value) for value in values[:-1]]
        else:
            parts = [
                part for part in shlex.split(remainder) if not part.startswith("--")
            ]
            if len(parts) >= 2:
                parsed_sources = parts[:-1]

        for source in parsed_sources:
            resolved = _resolve_build_relative(context_dir, source)
            if resolved:
                sources.extend(_iter_path_files(resolved))

    return sources


def _compose_artifact_files(compose_path: str, compose_content: str) -> list[Path]:
    try:
        import yaml
    except ModuleNotFoundError:
        return []

    compose_file = Path(compose_path)
    compose_dir = compose_file.parent.resolve()
    try:
        compose = yaml.safe_load(compose_content) or {}
    except yaml.YAMLError:
        return []

    services = compose.get("services", {})
    if not isinstance(services, dict):
        return []

    files: list[Path] = []
    for service in services.values():
        if not isinstance(service, dict):
            continue

        build = service.get("build")
        if build:
            if isinstance(build, str):
                context_dir = _resolve_compose_relative(compose_dir, build)
                dockerfile = context_dir / "Dockerfile" if context_dir else None
            elif isinstance(build, dict):
                context = str(build.get("context") or ".")
                context_dir = _resolve_compose_relative(compose_dir, context)
                dockerfile_name = str(build.get("dockerfile") or "Dockerfile")
                dockerfile = (
                    _resolve_build_relative(context_dir, dockerfile_name)
                    if context_dir
                    else None
                )
            else:
                context_dir = None
                dockerfile = None

            if dockerfile:
                files.extend(_iter_path_files(dockerfile))
                if context_dir:
                    files.extend(_dockerfile_copy_sources(dockerfile, context_dir))

        volumes = service.get("volumes", [])
        if isinstance(volumes, list):
            for volume in volumes:
                if isinstance(volume, str):
                    source = volume.split(":", 1)[0]
                elif isinstance(volume, dict):
                    source = str(volume.get("source") or "")
                    if volume.get("type") not in (None, "bind"):
                        continue
                else:
                    continue
                if not source.startswith("."):
                    continue
                resolved = _resolve_compose_relative(compose_dir, source)
                if resolved:
                    files.extend(_iter_path_files(resolved))

    return sorted(set(files))


def _repo_rel(path: Path) -> str:
    """Repo-relative label for a file, anchored at the REPO ROOT (not ``Path.cwd()``), so the
    config hash is reproducible from any working directory / checkout — the property the
    config-drift reconciler needs to recompute a service's hash at an arbitrary git ref. An
    out-of-tree path keeps its absolute form (never happens for in-repo artifacts/deps)."""
    try:
        return str(path.resolve().relative_to(_REPO_ROOT))
    except ValueError:
        return str(path)


def config_hash_from_items(
    compose_content: str,
    env_vars: dict[str, str],
    artifact_items: list[tuple[str, bytes]],
    dep_items: list[tuple[str, bytes]],
) -> str:
    """Pure config hash from explicit inputs — no filesystem / cwd access in here.

    ``artifact_items`` / ``dep_items`` are ``(repo-relative-label, content-bytes)`` pairs
    (artifact in discovery order; deps caller-sorted). Because labels are repo-relative and
    content is passed in, feeding the SAME (compose, env, files) yields the SAME hash whether
    the files were gathered from disk (the deploy path) or from a git ref (the drift
    reconciler) — there is no second, divergent implementation to disagree with the deploy.
    """
    art = [f"{lbl}:{hashlib.sha256(c).hexdigest()}" for lbl, c in artifact_items]
    deps = [f"dep:{lbl}:{hashlib.sha256(c).hexdigest()}" for lbl, c in dep_items]
    payload = "\n".join(art)
    if deps:
        payload = f"{payload}\n" + "\n".join(deps)
    return _compute_config_hash(compose_content, env_vars, payload)


def _artifact_items_from_disk(
    compose_path: str, compose_content: str
) -> list[tuple[str, bytes]]:
    """(repo-relative label, content) for each compose build-context file, on disk."""
    return [
        (_repo_rel(path), path.read_bytes())
        for path in _compose_artifact_files(compose_path, compose_content)
    ]


def _dependency_items_from_globs(
    globs: Iterable[str] = (),
) -> list[tuple[str, bytes]]:
    """(repo-relative label, content) for declared build/config dependency globs."""
    matched: set[Path] = set()
    for pattern in globs:
        for hit in _glob.glob(str(_REPO_ROOT / pattern), recursive=True):
            p = Path(hit)
            # Exclude transient __pycache__/.pyc/.pyo (mirrors _iter_path_files).
            if (
                p.is_file()
                and "__pycache__" not in p.parts
                and not p.name.endswith((".pyc", ".pyo"))
            ):
                matched.add(p.resolve())

    return [(_repo_rel(path), path.read_bytes()) for path in sorted(matched)]


__all__ = [
    "_artifact_items_from_disk",
    "_compose_artifact_files",
    "_compute_config_hash",
    "_dependency_items_from_globs",
    "_dockerfile_copy_sources",
    "_iter_path_files",
    "_repo_rel",
    "_resolve_build_relative",
    "_resolve_compose_relative",
    "config_hash_from_items",
]
