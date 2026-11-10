"""Unit tests for common.observability.portal_apps YAML quoting (#1474)."""

from __future__ import annotations

import yaml

from common.observability.portal_apps import render_items_yaml


def test_render_items_yaml_quotes_colon_and_hash_in_dashboard_title() -> None:
    """#1474: a dashboard title with `: ` and ` #` must be JSON-quoted so YAML parsing
    round-trips the full string instead of raising ScannerError or truncating at `#`."""
    title = "Alpha: Overview #1"
    rendered = render_items_yaml(
        [
            {
                "name": "Alpha Dashboard",
                "subtitle": title,
                "tag": "dashboard",
                "url": "https://signoz.zitian.party/dashboard?search=Alpha%3A%20Overview%20%231",
            }
        ],
        indent=0,
    )
    parsed = yaml.safe_load(rendered)
    assert parsed == [
        {
            "name": "Alpha Dashboard",
            "subtitle": title,
            "tag": "dashboard",
            "url": "https://signoz.zitian.party/dashboard?search=Alpha%3A%20Overview%20%231",
        }
    ]
