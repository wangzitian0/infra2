#!/usr/bin/env python3
"""Cross-check finance_report's required-env manifest against infra2's secrets.ctmpl (#482).

The app (wangzitian0/finance_report#1828) generates a manifest of every ``Settings``
field it reads, each declaring whether infra2 must inject it via Vault (``vault: true``).
infra2 owns ``secrets.ctmpl``, the Go-template that renders those values at deploy time.
Nothing today keeps the two in sync — a var can be added on the app side and never wired
into the template (silently falls back to the app's code default in production), or a
line can go stale in the template after the app stops reading it.

Ratchet: report-only until ``--enforce`` (Phase 2) — there is known drift to clean up
first, see #482.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = "repos/finance_report/common/runtime/required-env.generated.json"
TEMPLATE = "finance_report/finance_report/10.app/secrets.ctmpl"
RENDERED_VAR_RE = re.compile(r"^([A-Z][A-Z0-9_]*)=", re.MULTILINE)

# Escape hatch (#523): names that must never be reported as drift, regardless of what
# the regex above finds in secrets.ctmpl or what finance_report's manifest says. The
# regex is comment-blind (it scans raw text, not a parsed Go template), so a future
# doc-comment example written flush against column 0 could be misread as a "rendered"
# variable. Add an entry here — with a comment explaining why — instead of reshaping
# the template just to dodge a false positive.
DRIFT_ALLOWLIST: frozenset[str] = frozenset()

REQUIRED_FIELD_KEYS = ("env", "aliases", "vault")


class ManifestShapeError(ValueError):
    """Raised when required-env.generated.json doesn't match the expected shape."""


def _manifest_fields(root: Path) -> list[dict]:
    """Parse and shape-validate the finance_report-generated manifest.

    finance_report is a foreign repo (#523): infra2 doesn't control the shape of its
    generated artifact. Mirror libs/harness_manifest.py's style — explicit isinstance
    checks with clear messages — rather than letting a malformed/reshaped manifest
    crash with a raw traceback or (worse) silently produce an empty field list that
    reads as mass drift.
    """
    manifest_path = root / MANIFEST
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ManifestShapeError(f"cannot read {MANIFEST}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ManifestShapeError(f"{MANIFEST} is not valid JSON: {exc}") from exc

    if not isinstance(raw, dict):
        raise ManifestShapeError(f"{MANIFEST} must contain a JSON object")

    if "fields" not in raw:
        raise ManifestShapeError(f"{MANIFEST} is missing the 'fields' key")

    fields = raw["fields"]
    if not isinstance(fields, list):
        raise ManifestShapeError(f"{MANIFEST}['fields'] must be a list, got {type(fields).__name__}")

    validated: list[dict] = []
    for index, field in enumerate(fields):
        label = f"{MANIFEST}['fields'][{index}]"
        if not isinstance(field, dict):
            raise ManifestShapeError(f"{label} must be an object, got {type(field).__name__}")

        missing = [key for key in REQUIRED_FIELD_KEYS if key not in field]
        if missing:
            raise ManifestShapeError(f"{label} is missing key(s): {', '.join(missing)}")

        if not isinstance(field["env"], str) or not field["env"]:
            raise ManifestShapeError(f"{label}['env'] must be a non-empty string")
        if not isinstance(field["aliases"], list) or not all(isinstance(item, str) for item in field["aliases"]):
            raise ManifestShapeError(f"{label}['aliases'] must be a list of strings")
        if not isinstance(field["vault"], bool):
            raise ManifestShapeError(f"{label}['vault'] must be a bool")

        validated.append(field)

    return validated


def _rendered_names(root: Path) -> set[str]:
    return set(RENDERED_VAR_RE.findall((root / TEMPLATE).read_text(encoding="utf-8"))) - DRIFT_ALLOWLIST


def audit(root: Path = ROOT) -> dict:
    fields = _manifest_fields(root)
    rendered = _rendered_names(root)
    manifest_names: set[str] = set()
    for field in fields:
        manifest_names.add(field["env"])
        manifest_names.update(field["aliases"])

    unbacked_vault_fields = sorted(
        field["env"]
        for field in fields
        if field["vault"]
        and field["env"] not in DRIFT_ALLOWLIST
        and not (({field["env"]} | set(field["aliases"])) & rendered)
    )
    stale_template_entries = sorted(rendered - manifest_names)

    return {
        "unbacked_vault_fields": unbacked_vault_fields,
        "stale_template_entries": stale_template_entries,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="finance_report env-manifest vs secrets.ctmpl drift audit")
    ap.add_argument("--enforce", action="store_true", help="exit non-zero on any drift (Phase 2)")
    args = ap.parse_args(argv)

    try:
        result = audit()
    except ManifestShapeError as exc:
        # The manifest is a foreign artifact (finance_report#1828) that infra2 doesn't
        # control the shape of. A schema change there must not crash or false-fail
        # this gate for an unrelated PR (#523) — report and degrade to a no-op instead.
        print(
            f"::warning::{MANIFEST} does not have the expected shape, skipping "
            f"required-env drift audit (report-only): {exc}",
            file=sys.stderr,
        )
        return 0

    print(json.dumps(result, indent=2, ensure_ascii=False))

    drift = bool(result["unbacked_vault_fields"] or result["stale_template_entries"])
    if drift:
        for env in result["unbacked_vault_fields"]:
            print(f"::warning::{env} is vault:true in the manifest but not rendered by {TEMPLATE}", file=sys.stderr)
        for env in result["stale_template_entries"]:
            print(f"::warning::{env} is rendered by {TEMPLATE} but not in the manifest", file=sys.stderr)
    if args.enforce and drift:
        print("::error::required-env manifest and secrets.ctmpl have drifted", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
