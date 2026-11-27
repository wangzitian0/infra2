"""Control-plane stage hook planning, manifest parsing, and cell runner (absorbed from infra2-sdk)."""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from importlib.resources import files as _pkg_files
from pathlib import Path
from types import MappingProxyType
from typing import Any

import yaml
from infra2_sdk.config import EnvironmentManifest
from infra2_sdk.delivery import (
    CELL_POLICIES,
    CellPolicy,
    CheckRow,
    RuntimeHooksManifest,
    resolve_check_cells,
)

from common.core._sdk_wire import (
    CheckVerdict,
    parse_env_manifest_dict,
    record_verdicts,
    redact_text,
)
from common.deploy.yaml_text import safe_load_text

_STAGES_DOC = (
    safe_load_text(
        yaml,
        _pkg_files("infra2_sdk")
        .joinpath("data/stages.yaml")
        .read_text(encoding="utf-8"),
    )
    or {}
)
POLICY_VERSION = int(_STAGES_DOC["policy_version"])
BOOTSTRAP_CAP_SECONDS = int(_STAGES_DOC["bootstrap_cap_seconds"])
FORBIDDEN_HOOK_ENV_NAMES: frozenset[str] = frozenset(
    {
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "DATABASE_URL",
        "GITHUB_TOKEN",
        "GH_TOKEN",
        "LLM_API_KEY",
        "OP_SERVICE_ACCOUNT_TOKEN",
        "REDIS_URL",
        "S3_ACCESS_KEY",
        "S3_SECRET_KEY",
    }
)
FORBIDDEN_HOOK_ENV_PREFIXES: tuple[str, ...] = (
    "AWS_",
    "CLOUDFLARE_",
    "DOKPLOY_",
    "INFRA2_SECRET_",
    "OP_",
    "PRIVATE_",
    "SECRET_",
    "SSH_",
    "VAULT_",
)
HOOK_CHECKS_FILE = "checks.jsonl"
HOOK_ARTIFACTS_DIR = ".artifacts/hooks"
_HOOK_FAILED_STEPS = frozenset({"", "env", "bootstrap", "run", "timeout"})
_HOOK_STDERR_TAIL_LIMIT = 2000
_SLOT_TO_CELL: Mapping[str, str] = MappingProxyType(
    {
        "preview_gate": "preview.post",
        "staging_gate": "staging.post",
        "prod_gate": "prod.post",
    }
)
_HOOK_PASSTHROUGH_KEYS = frozenset(
    {
        "PATH",
        "HOME",
        "LANG",
        "LC_ALL",
        "TERM",
        "TMPDIR",
        "USER",
        "VIRTUAL_ENV",
        "UV_CACHE_DIR",
        "PLAYWRIGHT_BROWSERS_PATH",
    }
)
_SECRET_ENV_NAME = re.compile(
    r"(?:^|_)(?:SECRET|TOKEN|PASSWORD|PASSWD|KEY|AUTH|CREDENTIAL|WEBHOOK|DSN)(?:_URL)?$"
    r"|^(?:DATABASE|REDIS)_URL$",
    re.I,
)


@dataclass(frozen=True)
class StageHookPlan:
    stage: str
    pre_checks: tuple[str, ...]
    operation_slot: str | None
    post_slot: str | None
    stage_id: str = field(kw_only=True)


def _derive_stage_plans(
    policies: Mapping[str, CellPolicy],
) -> Mapping[str, StageHookPlan]:
    plans: dict[str, StageHookPlan] = {}
    for st in ("preview", "staging", "prod", "schedule"):
        pre_p = policies.get(f"{st}.pre")
        op_p = policies.get(f"{st}.operation")
        post_p = (
            policies["scheduled_audit"] if st == "schedule" else policies[f"{st}.post"]
        )
        pre_checks = (
            pre_p.check_list if pre_p and isinstance(pre_p.check_list, tuple) else ()
        )
        op_slot = op_p.check_list if op_p and isinstance(op_p.check_list, str) else None
        post_slot = post_p.check_list if isinstance(post_p.check_list, str) else None
        plans[st] = StageHookPlan(
            st, pre_checks, op_slot, post_slot, stage_id=post_p.stage_id
        )
    return MappingProxyType(plans)


STAGE_HOOK_PLANS = _derive_stage_plans(CELL_POLICIES)


def stage_hook_plan(stage: str) -> StageHookPlan:
    if not isinstance(stage, str):
        raise ValueError(f"stage must be a string, got {type(stage).__name__}")
    key = stage.strip().lower()
    if key == "production":
        key = "prod"
    if key not in STAGE_HOOK_PLANS:
        raise ValueError(f"unknown stage {stage!r}; allowed: {list(STAGE_HOOK_PLANS)}")
    return STAGE_HOOK_PLANS[key]


def load(path: str | Path, *, root: Path | None = None) -> EnvironmentManifest:
    target = Path(path) if root is None else root / path
    return parse_env_manifest_dict(json.loads(target.read_text(encoding="utf-8")))


def parse_hooks_manifest(raw: Any) -> RuntimeHooksManifest:
    """Parse ``raw`` JSON dict into a Schema v3 ``RuntimeHooksManifest``."""
    if not isinstance(raw, Mapping):
        raise ValueError("runtime-hooks manifest must be a JSON object")
    if "schema_version" not in raw:
        raise ValueError("runtime hooks manifest requires 'schema_version'")
    return RuntimeHooksManifest.from_dict(dict(raw))


def load_hooks_manifest(
    path: str | Path, *, root: Path | None = None
) -> RuntimeHooksManifest:
    target = Path(path) if root is None else root / path
    return parse_hooks_manifest(json.loads(target.read_text(encoding="utf-8")))


@dataclass(frozen=True)
class HookRunResult:
    service: str
    slot: str
    environment: str
    app_url: str
    version_ref: str
    status: str
    exit_code: int
    duration_seconds: float
    failed_step: str = ""
    summary: str = ""
    stderr_tail: str = ""
    checks: tuple[CheckVerdict, ...] = field(default=(), kw_only=True)
    bootstrap_seconds: float = field(default=0.0, kw_only=True)
    cell: str = field(default="", kw_only=True)
    policy_version: int = field(default=POLICY_VERSION, kw_only=True)

    def __post_init__(self) -> None:
        if self.status not in ("pass", "fail"):
            raise ValueError(f"status must be 'pass' or 'fail', got {self.status!r}")
        if self.failed_step not in _HOOK_FAILED_STEPS:
            raise ValueError(
                f"failed_step must be one of {sorted(_HOOK_FAILED_STEPS)}, got {self.failed_step!r}"
            )

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["duration_seconds"] = round(self.duration_seconds, 3)
        d["bootstrap_seconds"] = round(self.bootstrap_seconds, 3)
        return d


def build_hook_env(
    *,
    required_env: tuple[str, ...] = (),
    optional_env: tuple[str, ...] = (),
    slot: str,
    app_url: str,
    environment: str,
    version_ref: str,
    artifacts_dir: str,
    ambient_env: Mapping[str, str],
    extra_env: Mapping[str, str] = MappingProxyType({}),
) -> dict[str, str]:
    clean = {
        k: ambient_env[k]
        for k in ambient_env
        if k in _HOOK_PASSTHROUGH_KEYS and ambient_env[k]
    }
    clean["PYTHONPATH"] = "."
    if missing := [k for k in required_env if not ambient_env.get(k, "").strip()]:
        raise ValueError(f"missing required hook env: {', '.join(missing)}")
    for k in required_env:
        clean[k] = ambient_env[k]
    for k in optional_env:
        if (v := ambient_env.get(k, "")).strip():
            clean[k] = v
    for env_key, env_val in (
        *((("APP_URL", app_url),) if app_url else ()),
        ("ENVIRONMENT", environment),
        ("APP_VERSION_REF", version_ref),
        ("HOOK_SLOT", slot),
        ("HOOK_ARTIFACTS_DIR", artifacts_dir),
    ):
        clean[env_key] = env_val
    for k, v in extra_env.items():
        clean[k] = v
    return clean


_WARNING_LINE_MARKERS = (
    "DeprecationWarning:",
    "PytestDeprecationWarning:",
    "PendingDeprecationWarning:",
    "UserWarning:",
    "FutureWarning:",
    "warnings.warn(",
)


def _first_diagnostic_line(
    stderr: str | bytes | None, stdout: str | bytes | None, fallback: str
) -> str:
    """Return first non-warning diagnostic line for CheckVerdict.detail."""
    raw = _strip_stream(stderr) or _strip_stream(stdout) or fallback
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    for line in lines:
        if any(marker in line for marker in _WARNING_LINE_MARKERS):
            continue
        return line
    return lines[0] if lines else fallback


def _strip_stream(v: str | bytes | None) -> str:
    return (
        ""
        if v is None
        else (v.decode("utf-8", "replace") if isinstance(v, bytes) else v).strip()
    )


def _format_hook_output_tail(
    stdout: str | bytes | None, stderr: str | bytes | None, fallback: str
) -> str:
    out, err = _strip_stream(stdout), _strip_stream(stderr)
    if out and err:
        lim = (_HOOK_STDERR_TAIL_LIMIT - len("--- stdout ---\n\n--- stderr ---\n")) // 2
        return f"--- stdout ---\n{out[-lim:]}\n--- stderr ---\n{err[-lim:]}"
    return (err or out)[-_HOOK_STDERR_TAIL_LIMIT:] if (err or out) else fallback


def _resolve_cell_id(
    stage_or_cell: str, phase: str | None = None, *, environment: str = ""
) -> str:
    if phase is None:
        key = stage_or_cell.strip().lower()
        if key in ("schedule", "scheduled_audit"):
            return "scheduled_audit"
        if key == "smoke":
            env_k = environment.strip().lower()
            if env_k in ("prod", "production"):
                return "prod.operation"
            if env_k == "staging":
                return "staging.operation"
            return "preview.operation"
        if key in _SLOT_TO_CELL:
            return _SLOT_TO_CELL[key]
        if key in CELL_POLICIES:
            return key
        raise ValueError(f"unknown cell {stage_or_cell!r}")
    st = (
        "prod"
        if stage_or_cell.strip().lower() == "production"
        else stage_or_cell.strip().lower()
    )
    ph = phase.strip().lower()
    if st == "schedule" and ph == "post":
        return "scheduled_audit"
    cid = f"{st}.{ph}"
    if cid not in CELL_POLICIES:
        raise ValueError(f"unknown cell ({stage_or_cell!r}, {phase!r})")
    return cid


def _excluded_override(check: CheckRow, cell_id: str) -> Any:
    if check.off is not None:
        return check.off
    return check.cells.remove.get(cell_id)


def _cell_rows_and_spec(
    manifest: RuntimeHooksManifest, cell_id: str
) -> tuple[list[CheckRow], int, tuple[str, ...], tuple[str, ...]]:
    rows = [c for c in manifest.checks if cell_id in resolve_check_cells(c)]
    policy = CELL_POLICIES[cell_id]
    req = tuple(
        dict.fromkeys(
            [*manifest.required_env, *(e for r in rows for e in r.required_env)]
        )
    )
    opt = tuple(
        dict.fromkeys(
            e
            for e in [
                *manifest.optional_env,
                *(e for r in rows for e in r.optional_env),
            ]
            if e not in req
        )
    )
    budget = (
        min(sum(r.timeout_seconds for r in rows), policy.budget_seconds)
        if rows
        else policy.budget_seconds
    )
    return rows, budget, req, opt


def _run_step(
    argv: Sequence[str],
    *,
    step_name: str,
    check_id: str,
    timeout: float,
    clean_env: Mapping[str, str],
    cwd: Path | None,
    runner: Callable[..., subprocess.CompletedProcess[str]],
    bad: Callable[[str, str, float], None],
    flush: Callable[[], None],
    timeout_label: str,
) -> tuple[bool, int, str, str, float]:
    t0 = time.monotonic()
    try:
        proc = runner(
            list(argv),
            env=clean_env,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        msg = f"{step_name} timed out {timeout_label}"
        bad(check_id, msg, t0)
        return (
            False,
            124,
            "timeout",
            _format_hook_output_tail(exc.stdout, exc.stderr, msg),
            time.monotonic() - t0,
        )
    except BaseException as exc:
        bad(check_id, redact_text(str(exc), clean_env, _SECRET_ENV_NAME), t0)
        flush()
        raise
    dur = time.monotonic() - t0
    if proc.returncode != 0:
        msg = f"{step_name} exited with code {proc.returncode}"
        tail = _format_hook_output_tail(proc.stdout, proc.stderr, msg)
        diag = _first_diagnostic_line(proc.stderr, proc.stdout, msg)
        bad(
            check_id,
            redact_text(diag, clean_env, _SECRET_ENV_NAME),
            t0,
        )
        return False, proc.returncode, step_name, tail, dur
    return True, 0, "", "", dur


def _execute_cell(
    manifest: RuntimeHooksManifest,
    cell_id: str,
    *,
    slot_label: str,
    app_url: str,
    environment: str,
    version_ref: str,
    cwd: Path | None,
    ambient_env: Mapping[str, str] | None,
    result_path: Path | None,
    runner: Callable[..., subprocess.CompletedProcess[str]],
) -> HookRunResult:
    policy = CELL_POLICIES[cell_id]
    effective_stop = policy.stop_on_fail
    if "running_service" in policy.capabilities and (
        not app_url or not app_url.strip()
    ):
        raise ValueError("app_url must be a non-empty string")
    src_env = os.environ if ambient_env is None else ambient_env
    url, start = app_url.strip(), time.monotonic()
    active, budget, req_env, opt_env = _cell_rows_and_spec(manifest, cell_id)
    checks_file = (cwd or Path()) / HOOK_ARTIFACTS_DIR / HOOK_CHECKS_FILE
    checks_file.unlink(missing_ok=True)
    done: list[CheckVerdict] = []
    boot_seconds = 0.0

    def flush() -> None:
        checks_file.parent.mkdir(parents=True, exist_ok=True)
        record_verdicts(checks_file, source=slot_label, checks=done)

    def bad(check_id: str, detail: str, t0: float) -> None:
        done.append(
            CheckVerdict(
                check_id or slot_label, False, detail, seconds=time.monotonic() - t0
            )
        )

    def finish(
        st: str, code: int, fstep: str, summary: str, tail: str = ""
    ) -> HookRunResult:
        if st == "fail" and not done:
            bad(slot_label, summary, start)
        res = HookRunResult(
            manifest.service,
            slot_label,
            environment,
            url,
            version_ref,
            st,
            code,
            time.monotonic() - start,
            fstep,
            summary,
            tail,
            checks=tuple(done),
            bootstrap_seconds=boot_seconds,
            cell=cell_id,
            policy_version=POLICY_VERSION,
        )
        flush()
        if result_path is not None:
            result_path.parent.mkdir(parents=True, exist_ok=True)
            result_path.write_text(
                json.dumps(res.to_dict(), indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
        return res

    if not active:
        for c in manifest.checks:
            if (ov := _excluded_override(c, cell_id)) is not None:
                done.append(
                    CheckVerdict(
                        c.id,
                        False,
                        f"skipped: {ov.reason} ({ov.issue})",
                        skipped=True,
                    )
                )
        return finish("fail", 1, "run", f"cell {cell_id} ran 0 active checks")

    extra: dict[str, str] = {
        "HOOK_CELL": cell_id,
        "HOOK_POLICY_VERSION": str(POLICY_VERSION),
        **(
            {"HOOK_SCALE": policy.scale} if any(r.accepts_scale for r in active) else {}
        ),
    }
    try:
        clean_env = build_hook_env(
            required_env=req_env,
            optional_env=opt_env,
            slot=slot_label,
            app_url=url,
            environment=environment,
            version_ref=version_ref,
            artifacts_dir=HOOK_ARTIFACTS_DIR,
            ambient_env=src_env,
            extra_env=extra,
        )
    except ValueError as err:
        return finish("fail", 2, "env", str(err), str(err))

    boot_cmds = list(
        dict.fromkeys(r.bootstrap.strip() for r in active if r.bootstrap.strip())
    )
    boot_start = time.monotonic()
    boot_end = boot_start + BOOTSTRAP_CAP_SECONDS
    boot_label = (
        f"after {BOOTSTRAP_CAP_SECONDS}s (bootstrap cap {BOOTSTRAP_CAP_SECONDS}s)"
    )
    for cmd in boot_cmds:
        ok, code, fstep, tail, _ = _run_step(
            ["sh", "-lc", cmd],
            step_name="bootstrap",
            check_id="",
            timeout=max(boot_end - time.monotonic(), 0.001),
            clean_env=clean_env,
            cwd=cwd,
            runner=runner,
            bad=bad,
            flush=flush,
            timeout_label=boot_label,
        )
        boot_seconds = time.monotonic() - boot_start
        if not ok:
            msg = (
                f"bootstrap timed out {boot_label}"
                if fstep == "timeout"
                else f"bootstrap exited with code {code}"
            )
            return finish("fail", code, fstep, msg, tail)
    end = time.monotonic() + budget

    first_fail: tuple[int, str, str, str] | None = None
    for r in active:
        rem = max(end - time.monotonic(), 0.001)
        if r.timeout_seconds < budget and r.timeout_seconds <= rem:
            t_label = f"after {r.timeout_seconds}s (check timeout {r.timeout_seconds}s)"
        else:
            t_label = f"after {budget}s (cell budget {budget}s)"
        ok, code, fstep, tail, dur = _run_step(
            r.argv,
            step_name="run",
            check_id=r.id,
            timeout=min(r.timeout_seconds, rem),
            clean_env=clean_env,
            cwd=cwd,
            runner=runner,
            bad=bad,
            flush=flush,
            timeout_label=t_label,
        )
        if not ok:
            msg = (
                f"run timed out {t_label}"
                if fstep == "timeout"
                else f"run exited with code {code}"
            )
            if first_fail is None:
                first_fail = (code, fstep, msg, tail)
            if effective_stop or (fstep == "timeout" and end - time.monotonic() <= 0):
                return finish("fail", *first_fail)
        else:
            done.append(CheckVerdict(r.id, True, seconds=dur))
    if first_fail is not None:
        return finish("fail", *first_fail)
    return finish("pass", 0, "", f"{slot_label} passed")


def run_cell(
    manifest: RuntimeHooksManifest,
    stage_or_cell: str,
    phase: str | None = None,
    *,
    app_url: str = "",
    environment: str = "local_dev",
    version_ref: str = "local",
    cwd: Path | None = None,
    ambient_env: Mapping[str, str] | None = None,
    result_path: Path | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> HookRunResult:
    """Execute checks of ``manifest`` for ``(stage, phase)`` or ``scheduled_audit``."""
    cell_id = _resolve_cell_id(stage_or_cell, phase, environment=environment)
    p = CELL_POLICIES[cell_id]
    slot_label = p.check_list if isinstance(p.check_list, str) else cell_id
    return _execute_cell(
        manifest,
        cell_id,
        slot_label=slot_label,
        app_url=app_url,
        environment=environment,
        version_ref=version_ref,
        cwd=cwd,
        ambient_env=ambient_env,
        result_path=result_path,
        runner=runner,
    )


__all__ = [
    "HOOK_ARTIFACTS_DIR",
    "HOOK_CHECKS_FILE",
    "HookRunResult",
    "STAGE_HOOK_PLANS",
    "StageHookPlan",
    "build_hook_env",
    "load",
    "load_hooks_manifest",
    "parse_hooks_manifest",
    "run_cell",
    "stage_hook_plan",
]
