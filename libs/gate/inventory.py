"""CI Gate inventory parser, deployment glob detector, and direction proofs."""

from __future__ import annotations

import fnmatch
import functools
import os

from libs.gate.types import (
    DEPLOY_MARKERS,
    DEPLOY_TRIGGERING_GLOBS,
    NON_PROD_DEPLOY_WORKFLOWS,
    _as_list,
    _get_root,
    _get_workflow_dir,
    _get_yaml,
    _normalized_path,
)


def _blocking_coordinates(text: str) -> frozenset[tuple[str, str, str]] | None:
    """(gate id, workflow, job) for every gate with merge authority, or None."""
    yaml_mod = _get_yaml()
    try:
        doc = yaml_mod.safe_load(text)
        gates = (doc or {}).get("gates")
    except (yaml_mod.YAMLError, AttributeError):
        return None
    if not isinstance(gates, list):
        return None
    out: set[tuple[str, str, str]] = set()
    for gate in gates:
        if not isinstance(gate, dict):
            return None
        if gate.get("blocks_merge"):
            out.add(
                (
                    str(gate.get("id", "")),
                    str(gate.get("workflow", "")),
                    str(gate.get("job", "")),
                )
            )
    return frozenset(out)


def _inventory_only_gained_authority(base_text: str, head_text: str) -> bool:
    """True when every check that blocked a merge before still blocks it, unchanged."""
    base = _blocking_coordinates(base_text)
    head = _blocking_coordinates(head_text)
    if base is None or head is None:
        return False
    if not base:
        return False
    return base <= head


def _workflow_only_gained_authority(base_text: str, head_text: str) -> bool:
    """True when a workflow's change only makes this gate say no more often."""
    yaml_mod = _get_yaml()

    def read(text):
        try:
            doc = yaml_mod.safe_load(text)
        except yaml_mod.YAMLError:
            return None
        if not isinstance(doc, dict):
            return None
        on = doc.get(True, doc.get("on"))
        push = on.get("push") if isinstance(on, dict) else None
        if isinstance(push, dict) and "paths" in push:
            raw = push["paths"]
            if not isinstance(raw, list):
                return None
            paths = frozenset(str(x) for x in raw)
        else:
            paths = None  # None means every path
        jobs = doc.get("jobs")
        if not isinstance(jobs, dict):
            return None
        names = frozenset(
            str((spec.get("name") if isinstance(spec, dict) else None) or job_id)
            for job_id, spec in jobs.items()
        )
        return paths, names

    base, head = read(base_text), read(head_text)
    if base is None or head is None:
        return False
    base_paths, head_paths = base[0], head[0]
    if base_paths is None:
        if head_paths is not None:
            return False
    elif head_paths is not None and not base_paths <= head_paths:
        return False
    return base[1] <= head[1]


DIRECTION_PROOFS = {
    "docs/ssot/ci-gate-inventory.yaml": _inventory_only_gained_authority,
}


@functools.lru_cache(maxsize=1)
def _declared_deploy_globs() -> tuple[tuple[tuple[str, str], ...], bool]:
    """`on.push.paths` of every workflow that pushes to main and deploys."""
    workflow_dir = _get_workflow_dir()
    yaml_mod = _get_yaml()
    globs: list[tuple[str, str]] = []
    try:
        names = sorted(workflow_dir.glob("*.y*ml"))
    except OSError:
        return (), False
    if not names:
        try:
            os.close(os.open(workflow_dir, os.O_RDONLY))
            with os.scandir(workflow_dir):
                pass
        except OSError:
            return (), False
        return (), True
    parsed = 0
    for path in names:
        try:
            text = path.read_text(encoding="utf-8")
            doc = yaml_mod.safe_load(text)
        except (OSError, UnicodeDecodeError, getattr(yaml_mod, "YAMLError", Exception)):
            continue
        parsed += 1
        if not isinstance(doc, dict):
            continue
        triggers = doc.get("on") if isinstance(doc.get("on"), dict) else doc.get(True)
        push = (triggers or {}).get("push") if isinstance(triggers, dict) else None
        if not isinstance(push, dict):
            continue
        if "tags" in push and "branches" not in push:
            continue
        if "branches" in push and "main" not in _as_list(push["branches"]):
            continue
        if not any(marker in text for marker in DEPLOY_MARKERS):
            continue
        paths = _as_list(push.get("paths"))
        if "paths" not in push:
            paths = ["*", "**"]
        globs.extend((str(p), path.name) for p in paths)
    return tuple(dict.fromkeys(globs)), parsed == len(names)


def _deploy_triggering(path: str) -> str:
    """The workflow a merge of `path` would start, or "" for none."""
    for glob in DEPLOY_TRIGGERING_GLOBS:
        if fnmatch.fnmatch(path, glob):
            return "on merge"
    for glob, workflow in _declared_deploy_globs()[0]:
        if fnmatch.fnmatch(path, glob) and workflow not in NON_PROD_DEPLOY_WORKFLOWS:
            return workflow
    return ""


INVENTORY = "docs/ssot/ci-gate-inventory.yaml"


def _required_check_workflows() -> frozenset[str] | None:
    """Workflow paths that define a `blocks_merge: true` gate (#1138), derived from
    the inventory through `_blocking_coordinates`. None when the inventory cannot be
    read or parsed, has no blocking gate, or has a blocking gate without a workflow."""
    try:
        text = (_get_root() / INVENTORY).read_text(encoding="utf-8")
    except OSError:
        return None
    coordinates = _blocking_coordinates(text)
    if not coordinates:
        return None
    # One form for matching a changed file: `./x`, `a//b`, a space or a case variant
    # names the same file (#1138).
    workflows = frozenset(_normalized_path(w) for _, w, _ in coordinates)
    return None if "" in workflows else workflows


@functools.lru_cache(maxsize=1)
def _required_checks() -> tuple[frozenset[str], bool]:
    """Display names of the `blocks_merge: true` gates, and whether they read."""
    root = _get_root()
    yaml_mod = _get_yaml()
    try:
        doc = yaml_mod.safe_load(
            (root / "docs/ssot/ci-gate-inventory.yaml").read_text(encoding="utf-8")
        )
        gates = [g for g in (doc.get("gates") or []) if g.get("blocks_merge")]
    except (OSError, getattr(yaml_mod, "YAMLError", Exception), AttributeError):
        return frozenset(), False
    if not gates:
        return frozenset(), False
    names: set[str] = set()
    for gate in gates:
        job, workflow = gate.get("job"), gate.get("workflow")
        display = job
        try:
            spec = yaml_mod.safe_load(
                (root / str(workflow)).read_text(encoding="utf-8")
            )
            display = ((spec.get("jobs") or {}).get(job) or {}).get("name") or job
        except (
            OSError,
            getattr(yaml_mod, "YAMLError", Exception),
            AttributeError,
            TypeError,
        ):
            pass
        if display:
            names.add(str(display))
    return frozenset(names), True
