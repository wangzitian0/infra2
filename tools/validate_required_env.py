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


def _manifest_fields(root: Path) -> list[dict]:
    data = json.loads((root / MANIFEST).read_text(encoding="utf-8"))
    return data.get("fields") or []


def _rendered_names(root: Path) -> set[str]:
    return set(RENDERED_VAR_RE.findall((root / TEMPLATE).read_text(encoding="utf-8")))


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
        if field["vault"] and not (({field["env"]} | set(field["aliases"])) & rendered)
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

    result = audit()
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
