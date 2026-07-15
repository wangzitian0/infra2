"""Compatibility imports for the SDK-owned CI gate schema."""

from infra2_sdk.ci import (
    GATE_ID_RE,
    REQUIRED_GATE_FIELDS,
    load_delivery_stages,
    validate_gate,
    validate_inventory,
)

__all__ = [
    "GATE_ID_RE",
    "REQUIRED_GATE_FIELDS",
    "load_delivery_stages",
    "validate_gate",
    "validate_inventory",
]
