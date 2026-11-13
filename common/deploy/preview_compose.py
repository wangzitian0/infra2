"""The compose file of a Preview slot that the runner uploads to Dokploy (#1647).

A PR can change a unit's Preview compose file. The runner reads that file from the exact PR
head commit, checks it against an allow list, and uploads the checked text as a raw compose.
Dokploy never clones the PR branch. Anything the allow list does not name is refused: host
path mounts (the Docker socket), ``privileged``, ``cap_add``, ``devices``, ``ports``, host
``network_mode``/``pid``/``ipc``, extra networks and network options (aliases), an ``image``
beside ``build``, missing or large resource limits, and Traefik rules outside the slot's host.
The renderer renames each service to a slot-unique name and gives each built service a
slot-scoped image name, so a PR compose can neither shadow a production DNS name on the shared
``dokploy-network`` nor overwrite a production image tag.
"""

from __future__ import annotations

import functools
import re
import subprocess
from pathlib import Path

import yaml

from common.deploy.env_config import preview_service_config
from common.deploy.yaml_text import safe_load_text

ROOT = Path(__file__).resolve().parents[2]
INFRA2_GIT_URL = "https://github.com/wangzitian0/infra2.git"
SHA40_RE = re.compile(r"\A[0-9a-f]{40}\Z")

# The build context a Preview compose file names; it becomes the git context of the PR head.
_REPO_CONTEXT = "../.."
_NAME_RE = re.compile(r"\A[A-Za-z][A-Za-z0-9_-]*\Z")
_SLOT_NETWORK = "slot"
_PROXY_NETWORK = "dokploy-network"
_SERVICE_KEYS = {
    "image",
    "cpus",
    "build",
    "pull_policy",
    "container_name",
    "mem_limit",
    "cpu_shares",
    "command",
    "entrypoint",
    "environment",
    "healthcheck",
    "depends_on",
    "restart",
    "tmpfs",
    "logging",
    "labels",
    "networks",
    "volumes",
}
_SUFFIX = r"\$\{ENV_DOMAIN_SUFFIX\}"
_LABEL_RE = re.compile(
    r"\Atraefik\.http\.(?P<kind>routers|services)\.(?P<name>[A-Za-z0-9_-]+"
    + _SUFFIX
    + r")\.(?P<rest>[A-Za-z0-9_.]+)\Z"
)
_ROUTER_KEYS = {"rule", "entrypoints", "tls", "tls.certresolver", "priority", "service"}
_ROUTER_VALUES = {
    "entrypoints": "websecure",
    "tls": "true",
    "tls.certresolver": "letsencrypt",
}
_SERVICE_LABEL_KEYS = {"loadbalancer.server.port"}
_MAX_SERVICES = 6
_MAX_BUILDS = 3
_MAX_MEM_BYTES = 1024**3
_MAX_TMPFS_BYTES = 256 * 1024**2
_MAX_CPU_SHARES = 1024
_MAX_CPUS = 1.0
_SERVICE_NAME_RE = re.compile(r"\A[a-z0-9][a-z0-9-]*\Z")
_SIZE_RE = re.compile(r"\A([0-9]+)([bkmg]?)b?\Z", re.IGNORECASE)
_SIZE_UNITS = {"": 1, "b": 1, "k": 1024, "m": 1024**2, "g": 1024**3}


class PreviewComposeRefused(ValueError):
    """The compose file breaks the allow list; nothing is sent to Dokploy."""


def assert_runner_unit(service: str) -> None:
    """Only platform units use ``/preview``: they run on throwaway secrets (no Vault)."""
    config = preview_service_config(service)  # ValueError for an unregistered unit
    if not config.throwaway_secrets:
        raise ValueError(
            f"{service!r} is an App unit; App Previews belong to the App repositories, "
            "not to the runner /preview route"
        )


def _refuse(service: str, detail: str) -> PreviewComposeRefused:
    return PreviewComposeRefused(f"Preview compose of {service} refused: {detail}")


def _labels(service: str, raw) -> dict[str, str]:
    if isinstance(raw, dict):
        items = {str(k): str(v) for k, v in raw.items()}
    elif isinstance(raw, list) and all(isinstance(i, str) and "=" in i for i in raw):
        items = dict(i.split("=", 1) for i in raw)
    else:
        raise _refuse(service, "labels must be a list of key=value strings")
    return items


def _check_labels(service: str, labels: dict[str, str], base_subdomain: str) -> None:
    host = re.escape(base_subdomain)
    rule_re = re.compile(
        r"\AHost\(`" + host + _SUFFIX + r"\.\$\{INTERNAL_DOMAIN\}`\)"
        r"( && Path(Prefix)?\(`/[A-Za-z0-9_./-]*`\))?\Z"
    )
    enabled = has_rule = False
    for key, value in labels.items():
        if key == "traefik.enable" and value == "true":
            enabled = True
            continue
        if key == "traefik.docker.network" and value == _PROXY_NETWORK:
            continue
        match = _LABEL_RE.match(key)
        if not match:
            raise _refuse(service, f"label {key!r} is not allowed")
        kind, rest = match["kind"], match["rest"]
        if kind == "services" and rest in _SERVICE_LABEL_KEYS:
            if not value.isdigit() or not 1 <= int(value) <= 65535:
                raise _refuse(service, f"server port {value!r} is not 1-65535")
            continue
        if kind == "routers" and rest in _ROUTER_KEYS:
            if rest == "rule":
                if not rule_re.match(value):
                    raise _refuse(
                        service, f"router rule {value!r} is outside the slot's own host"
                    )
                has_rule = True
            elif rest == "service":
                if not re.search(_SUFFIX + r"\Z", value):
                    raise _refuse(
                        service, f"router service {value!r} is not slot-scoped"
                    )
            elif rest == "priority":
                if not re.fullmatch(r"[0-9]{1,4}", value):
                    raise _refuse(
                        service, f"router priority {value!r} is not an integer"
                    )
            elif value != _ROUTER_VALUES[rest]:
                raise _refuse(
                    service, f"label {key!r} must be {_ROUTER_VALUES[rest]!r}"
                )
            continue
        raise _refuse(service, f"label {key!r} is not allowed")
    if enabled and not has_rule:
        raise _refuse(service, "traefik.enable=true needs a router rule")


@functools.lru_cache(maxsize=4)
def reserved_service_names(root: Path = ROOT) -> frozenset[str]:
    """Every service key and container name that a fixed-environment compose file of the
    checkout in ``root`` declares. The one place that reads them."""
    names: set[str] = set()
    for top in ("platform", "bootstrap", "finance_report", "truealpha"):
        for path in sorted((root / top).glob("**/compose.yaml")):
            if "preview" in path.parts:
                continue
            try:
                doc = safe_load_text(yaml, path.read_text(encoding="utf-8"))
            except (OSError, yaml.YAMLError):
                continue
            for name, spec in ((doc or {}).get("services") or {}).items():
                names.add(str(name))
                if isinstance(spec, dict) and spec.get("container_name"):
                    names.add(str(spec["container_name"]).split("$", 1)[0])
    names.discard("")
    return frozenset(names)


def _size_bytes(service: str, what: str, value) -> int:
    match = _SIZE_RE.match(str(value).strip())
    if not match:
        raise _refuse(service, f"{what} {value!r} is not a size")
    return int(match[1]) * _SIZE_UNITS[match[2].lower()]


def _check_resources(service: str, name: str, spec: dict) -> None:
    if "mem_limit" not in spec:
        raise _refuse(service, f"service {name!r} sets no mem_limit")
    if _size_bytes(service, "mem_limit", spec["mem_limit"]) > _MAX_MEM_BYTES:
        raise _refuse(service, f"mem_limit of {name!r} is over 1g")
    shares, cpus = spec.get("cpu_shares"), spec.get("cpus")
    if (shares is None) == (cpus is None):
        raise _refuse(
            service, f"service {name!r} sets exactly one of cpu_shares and cpus"
        )
    if shares is not None and (
        isinstance(shares, bool)
        or not isinstance(shares, int)
        or not 1 <= shares <= _MAX_CPU_SHARES
    ):
        raise _refuse(service, f"cpu_shares of {name!r} must be 1-{_MAX_CPU_SHARES}")
    if cpus is not None and (
        isinstance(cpus, bool)
        or not isinstance(cpus, (int, float))
        or not 0 < cpus <= _MAX_CPUS
    ):
        raise _refuse(
            service, f"cpus of {name!r} must be above 0 and at most {_MAX_CPUS}"
        )
    for tmp in spec.get("tmpfs", []):
        options = str(tmp).split(":", 1)[1] if ":" in str(tmp) else ""
        sizes = [o[5:] for o in options.split(",") if o.startswith("size=")]
        if (
            len(sizes) != 1
            or _size_bytes(service, "tmpfs size", sizes[0]) > _MAX_TMPFS_BYTES
        ):
            raise _refuse(service, f"tmpfs {tmp!r} needs size= of at most 256m")


def _check_service(
    service: str,
    name: str,
    spec,
    declared_volumes: set[str],
    config,
    sha: str,
    pr: int,
) -> dict:
    """Check one service; return it with its build context and image name rewritten."""
    if not isinstance(spec, dict):
        raise _refuse(service, f"service {name!r} is not a mapping")
    extra = sorted(set(spec) - _SERVICE_KEYS)
    if extra:
        raise _refuse(
            service, f"service {name!r} uses keys {extra} outside the allow list"
        )
    if not _SERVICE_NAME_RE.match(name):
        raise _refuse(service, f"service name {name!r} must match [a-z0-9][a-z0-9-]*")
    container = spec.get("container_name")
    if container is not None and not (
        str(container).endswith("${ENV_SUFFIX}")
        and str(container).startswith(f"{config.project}-")
    ):
        raise _refuse(
            service,
            f"container_name of {name!r} must start with {config.project}- and end with ${{ENV_SUFFIX}}",
        )
    _check_resources(service, name, spec)
    if "build" in spec:
        build = spec["build"]
        dockerfile = (
            build.get("dockerfile", "Dockerfile") if isinstance(build, dict) else ""
        )
        if (
            not isinstance(build, dict)
            or set(build) - {"context", "dockerfile"}
            or build.get("context") != _REPO_CONTEXT
            or not isinstance(dockerfile, str)
            or dockerfile.startswith("/")
            or ".." in Path(dockerfile).parts
        ):
            raise _refuse(
                service,
                f"build of {name!r} must be context {_REPO_CONTEXT!r} plus a relative dockerfile",
            )
        if "image" in spec:
            raise _refuse(
                service,
                f"service {name!r} names an image beside build; the runner names it",
            )
        if spec.get("pull_policy", "build") != "build":
            raise _refuse(
                service, f"pull_policy of built service {name!r} must be build"
            )
        slug = service.replace("/", "-")
        spec = {
            **spec,
            "build": {**build, "context": f"{INFRA2_GIT_URL}#{sha}"},
            "image": f"{slug}-{name}-preview-pr-{pr}:{sha[:12]}",
        }
    elif "pull_policy" in spec:
        raise _refuse(
            service, f"pull_policy of image service {name!r} must be the default"
        )
    elif not isinstance(spec.get("image"), str):
        raise _refuse(service, f"service {name!r} needs an image or a build")
    for volume in spec.get("volumes", []):
        source = volume.split(":", 1)[0] if isinstance(volume, str) else ""
        if (
            ":" not in str(volume)
            or source not in declared_volumes
            or not str(volume).split(":")[1].startswith("/")
        ):
            raise _refuse(service, f"volume {volume!r} is not a declared named volume")
    for tmp in spec.get("tmpfs", []):
        if not isinstance(tmp, str) or not tmp.startswith("/"):
            raise _refuse(service, f"tmpfs {tmp!r} must be an absolute container path")
    networks = spec.get("networks", [])
    if isinstance(networks, dict):
        if any(options for options in networks.values()):
            raise _refuse(
                service, "network options (aliases, addresses) are not allowed"
            )
    elif not isinstance(networks, list):
        raise _refuse(service, "networks must be a list")
    if not set(networks) <= {_SLOT_NETWORK, _PROXY_NETWORK}:
        raise _refuse(service, f"networks {sorted(map(str, networks))} are not allowed")
    if spec.get("logging", {}).get("driver", "json-file") != "json-file":
        raise _refuse(service, "only the json-file log driver is allowed")
    if "labels" in spec:
        _check_labels(service, _labels(service, spec["labels"]), config.base_subdomain)
    return spec


def _slot_name(name: str, pr: int) -> str:
    return f"{name}-pr-{pr}"


def _rename_depends_on(spec: dict, pr: int, known: set[str], service: str) -> dict:
    deps = spec.get("depends_on")
    if deps is None:
        return spec
    names = list(deps) if isinstance(deps, (list, dict)) else [None]
    if not all(isinstance(n, str) and n in known for n in names):
        raise _refuse(service, "depends_on must name services of this compose")
    renamed = (
        {_slot_name(n, pr): v for n, v in deps.items()}
        if isinstance(deps, dict)
        else [_slot_name(n, pr) for n in deps]
    )
    return {**spec, "depends_on": renamed}


def render_runner_compose(service: str, text: str, sha: str, pr: int) -> str:
    """Check the Preview compose ``text`` of ``service`` and return the text to upload.

    Raises :class:`PreviewComposeRefused` when it breaks the allow list. Each ``build``
    context becomes the git context of commit ``sha`` and each built service gets a
    slot-scoped image name. Each service is renamed ``<name>-pr-<pr>``, so no service name
    (a DNS alias on ``dokploy-network``) matches a production one.
    """
    assert_runner_unit(service)
    if not SHA40_RE.match(sha):
        raise ValueError(f"sha must be 40 lowercase hex characters, got {sha!r}")
    if isinstance(pr, bool) or not isinstance(pr, int) or pr < 1:
        raise ValueError(f"pr must be a positive integer, got {pr!r}")
    config = preview_service_config(service)
    try:
        return _render(service, config, text, sha, pr)
    except RecursionError as exc:
        raise _refuse(service, "the YAML is nested too deeply") from exc


def _render(service: str, config, text: str, sha: str, pr: int) -> str:
    try:
        doc = safe_load_text(yaml, text)
    except yaml.YAMLError as exc:
        raise _refuse(
            service, f"YAML does not parse ({exc.__class__.__name__})"
        ) from exc
    if not isinstance(doc, dict) or not isinstance(doc.get("services"), dict):
        raise _refuse(service, "top level must be a mapping with services")
    extra = sorted(set(doc) - {"services", "networks", "volumes"})
    if extra:
        raise _refuse(service, f"top-level keys {extra} are not allowed")
    for net, definition in (doc.get("networks") or {}).items():
        if net == _SLOT_NETWORK and not definition:
            continue
        if net == _PROXY_NETWORK and definition == {"external": True}:
            continue
        raise _refuse(service, f"network {net!r} is not allowed")
    declared = set()
    for volume, definition in (doc.get("volumes") or {}).items():
        if definition or not _NAME_RE.match(str(volume)):
            raise _refuse(service, f"volume {volume!r} must be a plain named volume")
        declared.add(str(volume))
    services = doc["services"]
    if len(services) > _MAX_SERVICES:
        raise _refuse(service, f"more than {_MAX_SERVICES} services")
    if (
        sum(1 for s in services.values() if isinstance(s, dict) and "build" in s)
        > _MAX_BUILDS
    ):
        raise _refuse(service, f"more than {_MAX_BUILDS} builds")
    known = set(services)
    checked = {
        name: _check_service(service, name, spec, declared, config, sha, pr)
        for name, spec in services.items()
    }
    reserved = reserved_service_names()
    renamed = {}
    for name, spec in checked.items():
        spec = _rename_depends_on(spec, pr, known, service)
        if "container_name" in spec:
            final = spec["container_name"].replace("${ENV_SUFFIX}", f"-pr-{pr}")
            if final in reserved:
                raise _refuse(service, f"container name {final!r} is a production name")
        if _slot_name(name, pr) in reserved:
            raise _refuse(service, f"service name {name!r} is a production name")
        renamed[_slot_name(name, pr)] = spec
    doc["services"] = renamed
    return yaml.safe_dump(doc, sort_keys=False)


def read_compose_at(service: str, sha: str, *, root: Path = ROOT) -> str:
    """The Preview compose text of ``service`` at commit ``sha`` of the checkout in ``root``."""
    path = preview_service_config(service).compose_path
    result = subprocess.run(
        ["git", "-C", str(root), "show", f"{sha}:{path}"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise ValueError(f"commit {sha[:12]} has no {path}")
    return result.stdout
