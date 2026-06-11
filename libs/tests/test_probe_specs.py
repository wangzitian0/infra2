"""Tests for the runtime probe-spec verification helpers."""

from libs.probe_specs import missing_probe_names, parse_probe_names


def test_parse_probe_names_extracts_first_field():
    specs = (
        "openpanel-api-http|http|http://platform-openpanel-api:3000/healthcheck|200|warning|5\n"
        "platform-postgres-tcp|tcp|platform-postgres:5432|connected|critical|5\n"
    )
    assert parse_probe_names(specs) == {"openpanel-api-http", "platform-postgres-tcp"}


def test_parse_probe_names_ignores_blank_and_malformed_lines():
    assert parse_probe_names("\n   \nno-pipe-here\n  \n") == set()


def test_missing_probe_names_reports_source_not_running():
    source = (
        "a-http|http|x|200|critical|5\n"
        "b-http|http|y|200|critical|5\n"
        "c-http|http|z|200|warning|5\n"
    )
    running = "a-http|http|x|200|critical|5\nb-http|http|y|200|critical|5\n"
    assert missing_probe_names(source, running) == ["c-http"]


def test_missing_probe_names_empty_when_running_is_superset():
    source = "a-http|http|x|200|critical|5\n"
    running = "a-http|http|x|200|critical|5\nb-http|http|y|200|warning|5\n"
    assert missing_probe_names(source, running) == []


def test_missing_probe_names_ignores_target_suffix_differences():
    # The name field (first column) is identical across envs even though the
    # target host carries ${ENV_SUFFIX}; verification keys on names, not targets.
    source = "openpanel-ch-http|http|http://platform-openpanel-ch:8123/ping|200|warning|5\n"
    running = (
        "openpanel-ch-http|http|http://platform-openpanel-ch-staging:8123/ping|200|warning|5\n"
    )
    assert missing_probe_names(source, running) == []
