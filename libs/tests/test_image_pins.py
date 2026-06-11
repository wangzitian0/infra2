"""Tests for the bare-':latest' image-pin lint."""

from libs.image_pins import bare_latest_violations


def test_flags_bare_latest():
    text = "services:\n  portal:\n    image: b4bz/homer:latest\n"
    assert bare_latest_violations(text) == ["b4bz/homer:latest"]


def test_allows_digest_pinned_latest():
    text = "    image: b4bz/homer:latest@sha256:abc123\n"
    assert bare_latest_violations(text) == []


def test_allows_specific_version_and_other_floating_tags():
    # PR-1 scope is bare ':latest' only; other floating tags are a later tightening.
    text = (
        "    image: clickhouse/clickhouse-server:25.10.2.65\n"
        "    image: lindesvard/openpanel-api:2\n"
        "    image: redis:alpine\n"
    )
    assert bare_latest_violations(text) == []


def test_allows_templated_app_image():
    text = "    image: ghcr.io/wangzitian0/finance_report-backend:${IMAGE_TAG:-latest}\n"
    assert bare_latest_violations(text) == []


def test_registry_port_not_confused_for_tag():
    text = "    image: localhost:5000/foo\n    image: localhost:5000/bar:latest\n"
    assert bare_latest_violations(text) == ["localhost:5000/bar:latest"]


def test_multiple_violations_collected():
    text = "    image: a/b:latest\n    image: c/d:1.2.3\n    image: e/f:latest\n"
    assert bare_latest_violations(text) == ["a/b:latest", "e/f:latest"]
