"""The compose file of a Preview slot that the runner uploads to Dokploy (#1647).

A PR can change a unit's Preview compose file. The runner reads that file from the exact PR
head commit, checks it against an allow list, and uploads the checked text as a raw compose.
Dokploy never clones the PR branch. Anything the allow list does not name is refused: host
path mounts (the Docker socket), ``privileged``, ``cap_add``, ``devices``, ``ports``, host
``network_mode``/``pid``/``ipc``, extra networks, and Traefik rules outside the slot's host.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import yaml

from common.deploy.env_config import preview_service_config

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
_SERVICE_LABEL_KEYS = {"loadbalancer.server.port"}


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
    for key, value in labels.items():
        if key == "traefik.enable" and value == "true":
            continue
        if key == "traefik.docker.network" and value == _PROXY_NETWORK:
            continue
        match = _LABEL_RE.match(key)
        if not match:
            raise _refuse(service, f"label {key!r} is not allowed")
        kind, rest = match["kind"], match["rest"]
        if kind == "services" and rest in _SERVICE_LABEL_KEYS:
            continue
        if kind == "routers" and rest in _ROUTER_KEYS:
            if rest == "rule" and not rule_re.match(value):
                raise _refuse(
                    service, f"router rule {value!r} is outside the slot's own host"
                )
            if rest == "service" and not re.search(_SUFFIX + r"\Z", value):
                raise _refuse(service, f"router service {value!r} is not slot-scoped")
            continue
        raise _refuse(service, f"label {key!r} is not allowed")


def _check_service(
    service: str, name: str, spec, declared_volumes: set[str], base_subdomain: str, sha
) -> dict:
    if not isinstance(spec, dict):
        raise _refuse(service, f"service {name!r} is not a mapping")
    extra = sorted(set(spec) - _SERVICE_KEYS)
    if extra:
        raise _refuse(
            service, f"service {name!r} uses keys {extra} outside the allow list"
        )
    if "container_name" in spec and not str(spec["container_name"]).endswith(
        "${ENV_SUFFIX}"
    ):
        raise _refuse(
            service, f"container_name of {name!r} must end with ${{ENV_SUFFIX}}"
        )
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
        spec = {
            **spec,
            "build": {**build, "context": f"{INFRA2_GIT_URL}#{sha}"},
        }
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
    names = set(networks if isinstance(networks, (list, dict)) else {None})
    if not names <= {_SLOT_NETWORK, _PROXY_NETWORK}:
        raise _refuse(service, f"networks {sorted(map(str, names))} are not allowed")
    if spec.get("logging", {}).get("driver", "json-file") != "json-file":
        raise _refuse(service, "only the json-file log driver is allowed")
    if "labels" in spec:
        _check_labels(service, _labels(service, spec["labels"]), base_subdomain)
    return spec


def render_runner_compose(service: str, text: str, sha: str) -> str:
    """Check the Preview compose ``text`` of ``service`` and return the text to upload.

    Raises :class:`PreviewComposeRefused` when it breaks the allow list. Each ``build``
    context becomes the git context of commit ``sha``.
    """
    assert_runner_unit(service)
    if not SHA40_RE.match(sha):
        raise ValueError(f"sha must be 40 lowercase hex characters, got {sha!r}")
    config = preview_service_config(service)
    try:
        doc = yaml.safe_load(text)
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
    doc["services"] = {
        name: _check_service(service, name, spec, declared, config.base_subdomain, sha)
        for name, spec in doc["services"].items()
    }
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
