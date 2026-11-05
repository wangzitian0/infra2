"""``ParseOnce`` returns the result of the parser, once per text, without shared state (#1453)."""

from __future__ import annotations

import io

import pytest
import yaml

from common.tests.yaml_cache import ParseOnce


def test_a_repeated_text_is_parsed_once_and_gives_the_same_data() -> None:
    parse_once = ParseOnce(yaml.safe_load)

    first = parse_once("a: [1, 2]\nb: {c: d}\n")
    second = parse_once("a: [1, 2]\nb: {c: d}\n")

    assert first == second == {"a": [1, 2], "b": {"c": "d"}}
    assert parse_once.parses == 1


def test_a_different_text_is_parsed_again() -> None:
    parse_once = ParseOnce(yaml.safe_load)

    assert parse_once("a: 1") == {"a": 1}
    assert parse_once("a: 2") == {"a": 2}
    assert parse_once.parses == 2


def test_a_caller_that_changes_its_result_does_not_change_the_next_result() -> None:
    parse_once = ParseOnce(yaml.safe_load)

    first = parse_once("a: [1]\n")
    first["a"].append(2)
    first["new"] = True

    assert parse_once("a: [1]\n") == {"a": [1]}


def test_a_parse_error_is_raised_each_time_and_is_not_stored() -> None:
    parse_once = ParseOnce(yaml.safe_load)

    for _ in range(2):
        with pytest.raises(yaml.YAMLError):
            parse_once("a: [unclosed\n")
    assert parse_once.parses == 0


def test_bytes_and_str_of_the_same_document_give_the_same_data() -> None:
    parse_once = ParseOnce(yaml.safe_load)

    assert parse_once(b"a: 1\n") == parse_once("a: 1\n") == {"a": 1}


def test_a_stream_goes_to_the_parser_each_time() -> None:
    parse_once = ParseOnce(yaml.safe_load)

    assert parse_once(io.StringIO("a: 1")) == {"a": 1}
    assert parse_once(io.StringIO("a: 2")) == {"a": 2}
    assert parse_once.parses == 0


def test_the_session_parses_yaml_through_the_cache() -> None:
    """The conftest fixture puts the cache on ``yaml.safe_load`` for every test."""
    assert isinstance(yaml.safe_load, ParseOnce)
