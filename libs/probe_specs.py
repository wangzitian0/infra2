"""Frozen shim: the implementation lives in ``libs.observability.probe_specs`` (#955).

Import from ``libs.observability.probe_specs`` in new code. This module only re-exports it.
"""

from __future__ import annotations

from libs.observability.probe_specs import (
    ENV_SUFFIX_PLACEHOLDER,
    SPECS_LINE_SEPARATOR,
    encode_specs_env_value,
    missing_probe_names,
    normalize_specs_text,
    normalized_probe_fields,
    parse_probe_names,
    render_probe_spec_text,
    render_public_route_spec_text,
    resolve_env_suffix,
)

__all__ = [
    "ENV_SUFFIX_PLACEHOLDER",
    "SPECS_LINE_SEPARATOR",
    "encode_specs_env_value",
    "missing_probe_names",
    "normalize_specs_text",
    "normalized_probe_fields",
    "parse_probe_names",
    "render_probe_spec_text",
    "render_public_route_spec_text",
    "resolve_env_suffix",
]
