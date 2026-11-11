"""Deterministic configuration and artifact hashing for Deployer.

Part of common.deploy domain decomposition (#955, #1009).
"""

from __future__ import annotations

from common.core.yaml_text import safe_load_text

from collections.abc import Iterable
import glob as _glob
import hashlib
import json
from pathlib import Path
import re
import shlex

_REPO_ROOT = Path(__file__).resolve().parents[2]
_GLOB_CHARS_RE = re.compile(r"[*?[]")
_HOST_VAR_PREFIX_RE = re.compile(r"^\$\{[A-Za-z_][A-Za-z0-9_]*(?::?-[^}]*)?\}/")
_HOST_DATA_VAR_RE = re.compile(r"^\$\{DATA_PATH(?::?-[^}]*)?\}/")
_MOUNT_FINGERPRINT_PREFIX = "IAC_MOUNT_SHA256_"
_ENV_SUBST_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?:(:?-)([^}]*))?\}")


def _expand_compose_env_defaults(value: str) -> str | None:
    """Substitute `${VAR:-default}` and `${VAR-default}` with `default`.

    Returns `None` if `value` contains an unresolvable `${...}` without a default.
    """
    if "${" not in value:
        return value
    unresolvable = False

    def _replace(match: re.Match[str]) -> str:
        nonlocal unresolvable
        op = match.group(2)
        default = match.group(3)
        if op is None or default is None:
            unresolvable = True
            return ""
        return default

    expanded = _ENV_SUBST_RE.sub(_replace, value)
    if unresolvable or "${" in expanded:
        return None
    return expanded


def _join_dockerfile_continuations(text: str) -> list[str]:
    """Join Dockerfile logical lines continued with a trailing `\\`, skipping comments."""
    logical_lines: list[str] = []
    current: list[str] = []
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            if current:
                logical_lines.append(" ".join(current))
                current = []
            continue
        if stripped.startswith("#"):
            # Inside a backslash continuation, Docker treats comment lines as ignored
            # and continues the logical line across them.
            continue
        if stripped.endswith("\\"):
            piece = stripped[:-1].rstrip()
            if piece:
                current.append(piece)
        else:
            current.append(stripped)
            logical_lines.append(" ".join(current))
            current = []
    if current:
        logical_lines.append(" ".join(current))
    return logical_lines


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


def _is_transient_file(path: Path) -> bool:
    return "__pycache__" in path.parts or path.name.endswith((".pyc", ".pyo"))


def _iter_path_files(path: Path) -> list[Path]:
    if path.is_file():
        return [path]
    if not path.is_dir():
        return []
    return sorted(
        child
        for child in path.rglob("*")
        if child.is_file() and not _is_transient_file(child)
    )


def _split_volume_source(volume: str) -> str:
    """Extract the host/source part of a short-syntax compose volume entry, preserving `${VAR:-default}` colons."""
    depth = 0
    for idx, ch in enumerate(volume):
        if ch == "$" and volume[idx : idx + 2] == "${":
            depth += 1
        elif ch == "}" and depth > 0:
            depth -= 1
        elif ch == ":" and depth == 0:
            return volume[:idx]
    return volume


def _resolve_compose_relative(compose_dir: Path, source: str) -> Path | None:
    if not source:
        return None
    expanded = _expand_compose_env_defaults(source)
    if not expanded or expanded.startswith("/"):
        return None
    return (compose_dir / expanded).resolve()


def _resolve_build_relative(context_dir: Path, source: str) -> Path | None:
    if not source or source.startswith("--"):
        return None
    return _resolve_compose_relative(context_dir, source)


def _resolve_build_sources(context_dir: Path, source: str) -> list[Path]:
    """Resolve a COPY/ADD source (including `*?[` globs) against `context_dir`.

    Raises `FileNotFoundError` when a glob matches zero files on disk so the config
    hash and fan-out audit never silently skip a wildcard COPY (#1123).
    """
    if not source or source.startswith("--"):
        return []
    expanded = _expand_compose_env_defaults(source)
    if not expanded or expanded.startswith("/"):
        return []
    if _GLOB_CHARS_RE.search(expanded):
        hits = sorted(
            Path(p).resolve() for p in _glob.glob(str(context_dir / expanded))
        )
        if not hits:
            raise FileNotFoundError(
                f"Dockerfile COPY/ADD glob {source!r} matched no files under {context_dir}"
            )
        files: list[Path] = []
        for hit in hits:
            files.extend(_iter_path_files(hit))
        return files
    resolved = (context_dir / expanded).resolve()
    return _iter_path_files(resolved)


def _dockerfile_copy_sources(dockerfile: Path, context_dir: Path) -> list[Path]:
    if not dockerfile.exists():
        return []

    sources: list[Path] = []
    for line in _join_dockerfile_continuations(dockerfile.read_text(encoding="utf-8")):
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
            sources.extend(_resolve_build_sources(context_dir, source))

    return sources


def _compose_has_build(compose_content: str) -> bool:
    """Return True when a compose YAML document declares at least one service `build` section."""
    try:
        import yaml
    except ModuleNotFoundError:
        return False
    try:
        compose = safe_load_text(yaml, compose_content) or {}
    except yaml.YAMLError:
        return False
    services = compose.get("services", {}) if isinstance(compose, dict) else {}
    if not isinstance(services, dict):
        return False
    return any(
        isinstance(svc, dict) and bool(svc.get("build")) for svc in services.values()
    )


def _bind_mount_sources(service: dict) -> list[str]:
    """Source strings of the short and long syntax bind mounts of one compose service."""
    volumes = service.get("volumes", [])
    if not isinstance(volumes, list):
        return []
    sources: list[str] = []
    for volume in volumes:
        if isinstance(volume, str):
            sources.append(_split_volume_source(volume))
        elif isinstance(volume, dict):
            if volume.get("type") not in (None, "bind"):
                continue
            sources.append(str(volume.get("source") or ""))
    return sources


def _host_mount_name(source: str) -> str | None:
    """File name of a host mount under a variable, such as `${DATA_PATH}/<name>`.

    The deploy.py renders the file to the host path. Any leading `${VAR}`,
    `${VAR:-default}` or `${VAR-default}` counts, except a default that is a
    relative path: the hash reads that path from the repository (`./` sources).
    """
    if not _HOST_VAR_PREFIX_RE.match(source):
        return None
    expanded = _expand_compose_env_defaults(source)
    if expanded is not None and expanded.startswith("."):
        return None
    name = source.rstrip("/").rsplit("/", 1)[-1]
    if not name or "${" in name:
        return None
    return name


def _host_mount_template(compose_dir: Path, source: str) -> Path | None:
    """Template file in the service directory that renders a `${VAR}/name` host mount.

    The service deploy.py renders `name` (or `name.tmpl`) from the service directory
    to the host path. The compose mount is the one declaration of which file that is.
    """
    name = _host_mount_name(source)
    if name is None:
        return None
    for candidate in (name, f"{name}.tmpl"):
        path = compose_dir / candidate
        if path.is_file():
            return path.resolve()
    return None


def parse_compose_services(compose_content: str) -> dict:
    """Services of a compose text. Raises ValueError when the text cannot be read.

    A guard that parses nothing checks nothing, so the guard uses this strict form.
    """
    try:
        import yaml
    except ModuleNotFoundError as exc:
        raise ValueError("PyYAML is not installed") from exc
    try:
        compose = safe_load_text(yaml, compose_content)
    except yaml.YAMLError as exc:
        raise ValueError(f"compose is not valid YAML: {exc}") from exc
    services = compose.get("services") if isinstance(compose, dict) else None
    if not isinstance(services, dict):
        raise ValueError("compose has no `services` mapping")
    return services


def _compose_services(compose_content: str) -> dict:
    try:
        return parse_compose_services(compose_content)
    except ValueError:
        return {}


def compose_service_host_mounts(compose_content: str) -> dict[str, list[str]]:
    """Map each compose service to the file names of its `${DATA_PATH}/file.ext` mounts.

    Only mounts whose last path part has a file suffix count. A directory mount
    such as `${DATA_PATH}/data` holds runtime data, not a rendered template.
    A host file without a suffix (for example `htpasswd`) is left out on purpose:
    the name alone cannot tell it from a data directory. Any `${VAR}/`, `${VAR:-x}/`
    prefix counts, not only `${DATA_PATH}`. Only a `${DATA_PATH}` mount can name a
    template, so a file under another variable always fails the guard.
    This is the one declaration of which host files a service mounts (#1438).
    """
    found: dict[str, list[str]] = {}
    for service_name, service in _compose_services(compose_content).items():
        if not isinstance(service, dict):
            continue
        names: list[str] = []
        for source in _bind_mount_sources(service):
            name = _host_mount_name(source)
            if name is None or "." not in name.lstrip("."):
                continue
            names.append(name)
        if names:
            found[str(service_name)] = sorted(set(names))
    return found


def compose_host_mount_files(compose_content: str) -> list[str]:
    """File name of each `${DATA_PATH}/file.ext` host mount of a compose."""
    return [
        name
        for names in compose_service_host_mounts(compose_content).values()
        for name in names
    ]


def mount_fingerprint_var(name: str) -> str:
    """Compose variable that carries the content fingerprint of host file `name`."""
    return _MOUNT_FINGERPRINT_PREFIX + re.sub(r"[^A-Za-z0-9]", "_", name).upper()


def mount_fingerprint_env(
    rendered: dict[str, str | bytes | None],
    *,
    service: str = "",
    template_dir: Path | str | None = None,
) -> dict[str, str]:
    """sha256 of each rendered host file, keyed by its compose variable.

    The compose service reads the variable in a label. A content change changes the
    resolved compose config, so `docker compose up -d` recreates that service only (#1438).
    A `None` content means the render failed (a missing template): raise an error
    that names the service and the template path instead of failing in the hash.
    """
    out: dict[str, str] = {}
    for name, content in rendered.items():
        if content is None:
            where = f"{Path(template_dir) / name}" if template_dir else name
            raise RuntimeError(
                f"{service or 'service'}: cannot render host file {name!r}; "
                f"template missing or unreadable: {where} (or {where}.tmpl)"
            )
        data = content.encode("utf-8") if isinstance(content, str) else content
        out[mount_fingerprint_var(name)] = hashlib.sha256(data).hexdigest()
    return out


def service_label_text(service: dict) -> str:
    """All label keys and values of one compose service as one string."""
    labels = service.get("labels", [])
    if isinstance(labels, dict):
        return "\n".join(f"{k}={v}" for k, v in labels.items())
    if isinstance(labels, list):
        return "\n".join(str(item) for item in labels)
    return ""


def compose_mount_fingerprint_gaps(compose_content: str) -> list[tuple[str, str]]:
    """(service, file name) of each host mount whose service label lacks the fingerprint."""
    services = _compose_services(compose_content)
    gaps: list[tuple[str, str]] = []
    for service_name, names in compose_service_host_mounts(compose_content).items():
        text = service_label_text(services[service_name])
        for name in names:
            var = re.escape(mount_fingerprint_var(name))
            # Compare the whole variable name: `X_OLD` must not satisfy `X`.
            if not re.search(r"\$\{" + var + r"(?![A-Za-z0-9_])", text):
                gaps.append((service_name, name))
    return gaps


def missing_mount_fingerprints(compose_path: str, env_vars: dict) -> list[str]:
    """Fingerprint variables the compose needs that `env_vars` does not carry.

    A missing variable resolves to an empty label, so the container would keep the
    old file after a template change. A missing file needs none; a file that does
    not parse raises ValueError.
    """
    path = Path(compose_path)
    if not path.is_absolute():
        path = _REPO_ROOT / path
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    parse_compose_services(text)  # raises ValueError on an unreadable compose
    return [
        var for var in required_mount_fingerprint_vars(text) if not env_vars.get(var)
    ]


def required_mount_fingerprint_vars(compose_content: str) -> list[str]:
    """Fingerprint variables a deploy must supply for this compose."""
    return sorted(
        {mount_fingerprint_var(n) for n in compose_host_mount_files(compose_content)}
    )


def _compose_artifact_files(compose_path: str, compose_content: str) -> list[Path]:
    try:
        import yaml
    except ModuleNotFoundError:
        return []

    compose_file = Path(compose_path)
    compose_dir = compose_file.parent.resolve()
    try:
        compose = safe_load_text(yaml, compose_content) or {}
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
            explicit_dockerfile = False
            if isinstance(build, str):
                context_dir = _resolve_compose_relative(compose_dir, build)
                dockerfile = context_dir / "Dockerfile" if context_dir else None
            elif isinstance(build, dict):
                context = str(build.get("context") or ".")
                context_dir = _resolve_compose_relative(compose_dir, context)
                explicit_dockerfile = (
                    "dockerfile" in build and build.get("dockerfile") is not None
                )
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
                if explicit_dockerfile and not dockerfile.is_file():
                    raise FileNotFoundError(
                        f"compose {compose_path}: build dockerfile {dockerfile} does not exist"
                    )
                files.extend(_iter_path_files(dockerfile))
                if context_dir:
                    files.extend(_dockerfile_copy_sources(dockerfile, context_dir))

        for source in _bind_mount_sources(service):
            if _host_mount_name(source) is not None and _HOST_DATA_VAR_RE.match(source):
                # The host file is rendered by the service deploy.py from a template
                # in the service directory (#1438). The compose mount is the declaration.
                template = _host_mount_template(compose_dir, source)
                if template:
                    files.append(template)
                continue
            expanded_source = _expand_compose_env_defaults(source)
            if not expanded_source or not expanded_source.startswith("."):
                continue
            resolved = _resolve_compose_relative(compose_dir, expanded_source)
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


def _dependency_paths_from_globs(
    globs: Iterable[str] = (), root: Path = _REPO_ROOT
) -> list[Path]:
    """Resolved non-transient files matching declared build/config dependency globs."""
    matched: set[Path] = set()
    for pattern in globs:
        for hit in _glob.glob(str(root / pattern), recursive=True):
            p = Path(hit)
            if p.is_file() and not _is_transient_file(p):
                matched.add(p.resolve())
    return sorted(matched)


def _dependency_items_from_globs(
    globs: Iterable[str] = (),
) -> list[tuple[str, bytes]]:
    """(repo-relative label, content) for declared build/config dependency globs."""
    return [
        (_repo_rel(path), path.read_bytes())
        for path in _dependency_paths_from_globs(globs)
    ]


__all__ = [
    "_artifact_items_from_disk",
    "_compose_artifact_files",
    "_compose_has_build",
    "compose_host_mount_files",
    "compose_mount_fingerprint_gaps",
    "compose_service_host_mounts",
    "missing_mount_fingerprints",
    "mount_fingerprint_env",
    "mount_fingerprint_var",
    "parse_compose_services",
    "required_mount_fingerprint_vars",
    "_compute_config_hash",
    "_dependency_items_from_globs",
    "_dependency_paths_from_globs",
    "_dockerfile_copy_sources",
    "_is_transient_file",
    "_iter_path_files",
    "_repo_rel",
    "_resolve_build_relative",
    "_resolve_compose_relative",
    "config_hash_from_items",
]
