"""Drift audit for finance_report's required-env manifest vs secrets.ctmpl (#482)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.validate_required_env import MANIFEST, TEMPLATE, audit, main

ROOT = Path(__file__).resolve().parents[2]

# infra-ci deliberately never checks out submodules by default (#506) — only the
# dedicated validate-required-env CI job does a scoped `git submodule update --init
# repos/finance_report`. The general `libs/tests` run (every other CI job, and a plain
# local checkout before running `invoke setup`) has no submodule content, so the two
# "real" tests below must skip rather than fail when the manifest isn't there.
_SUBMODULE_CHECKED_OUT = (ROOT / MANIFEST).exists()
_SKIP_REASON = "repos/finance_report submodule not checked out"


def _write(root: Path, fields: list[dict], template_lines: list[str]) -> None:
    manifest_path = root / MANIFEST
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps({"fields": fields}), encoding="utf-8")

    template_path = root / TEMPLATE
    template_path.parent.mkdir(parents=True, exist_ok=True)
    template_path.write_text("\n".join(template_lines) + "\n", encoding="utf-8")


def _field(env: str, *, vault: bool, aliases: list[str] | None = None) -> dict:
    return {"field": env.lower(), "env": env, "aliases": aliases or [], "group": "Test", "vault": vault, "has_default": True}


@pytest.mark.skipif(not _SUBMODULE_CHECKED_OUT, reason=_SKIP_REASON)
def test_real_manifest_and_template_are_in_sync() -> None:
    result = audit()
    assert result["unbacked_vault_fields"] == []
    assert result["stale_template_entries"] == []


@pytest.mark.skipif(not _SUBMODULE_CHECKED_OUT, reason=_SKIP_REASON)
def test_real_enforce_exits_zero() -> None:
    assert main(["--enforce"]) == 0


def test_unbacked_vault_field_is_flagged(tmp_path: Path) -> None:
    _write(
        tmp_path,
        fields=[_field("SECRET_KEY", vault=True)],
        template_lines=['DEBUG={{ with .Data.data.DEBUG }}{{ printf "%q" . }}{{ else }}"false"{{ end }}'],
    )
    result = audit(tmp_path)
    assert result["unbacked_vault_fields"] == ["SECRET_KEY"]
    assert result["stale_template_entries"] == ["DEBUG"]


def test_alias_satisfies_the_vault_requirement(tmp_path: Path) -> None:
    _write(
        tmp_path,
        fields=[_field("ZAI_API_KEY", vault=True, aliases=["AI_API_KEY", "GEMINI_API_KEY"])],
        template_lines=['AI_API_KEY={{ with .Data.data.AI_API_KEY }}{{ printf "%q" . }}{{ else }}""{{ end }}'],
    )
    result = audit(tmp_path)
    assert result["unbacked_vault_fields"] == []
    assert result["stale_template_entries"] == []


def test_non_vault_field_with_no_render_is_not_flagged(tmp_path: Path) -> None:
    _write(
        tmp_path,
        fields=[_field("ACCESS_TOKEN_EXPIRE_MINUTES", vault=False)],
        template_lines=['SECRET_KEY={{ with .Data.data.SECRET_KEY }}{{ printf "%q" . }}{{ else }}""{{ end }}'],
    )
    result = audit(tmp_path)
    assert result["unbacked_vault_fields"] == []
    assert result["stale_template_entries"] == ["SECRET_KEY"]


def test_enforce_fails_closed_on_drift() -> None:
    class _StubArgs:
        enforce = True

    # exercise main()'s drift branch directly against a synthetic root via monkeypatched audit
    import tools.validate_required_env as mod

    original_audit = mod.audit
    try:
        mod.audit = lambda root=ROOT: {"unbacked_vault_fields": ["X"], "stale_template_entries": []}
        assert main(["--enforce"]) == 1
        assert main([]) == 0
    finally:
        mod.audit = original_audit
