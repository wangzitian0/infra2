"""Unit tests for libs.deploy_phase_log's timestamped phase markers."""

from __future__ import annotations

import io

from libs.deploy_phase_log import phase, reset_phase_clock


def test_phase_prints_one_flushed_line_with_elapsed_and_message() -> None:
    clock = iter([100.0, 100.0])
    reset_phase_clock(100.0)
    buf = io.StringIO()

    phase("dokploy-trigger: start", _now=lambda: next(clock), _stream=buf)

    line = buf.getvalue()
    assert line.endswith("\n")
    assert line.count("\n") == 1
    assert "dokploy-trigger: start" in line
    assert line.startswith("[+")
    assert "0.00s" in line


def test_phase_elapsed_uses_the_reset_clock_zero_point() -> None:
    reset_phase_clock(50.0)
    buf = io.StringIO()

    phase("rollout-wait: start", _now=lambda: 62.5, _stream=buf)

    assert "+  12.50s" in buf.getvalue() or "+12.50s" in buf.getvalue().replace(" ", "")


def test_phase_calls_are_logged_in_order_with_a_mocked_clock() -> None:
    """The core guarantee this module exists for: with PYTHONUNBUFFERED=1, each
    phase() call is one line, printed at the moment it happens — so a sequence of
    calls appears in the log in the SAME order they were made, each carrying a
    timestamp no earlier than the one before it."""
    reset_phase_clock(0.0)
    ticks = iter([1.0, 2.5, 2.5, 9.0, 9.0, 20.0])
    buf = io.StringIO()
    names = [
        "secrets-supply: start",
        "secrets-supply: done",
        "dokploy-trigger: start",
        "dokploy-trigger: accepted",
        "rollout-wait: start",
        "rollout-wait: done",
    ]

    for name in names:
        phase(name, _now=lambda: next(ticks), _stream=buf)

    lines = buf.getvalue().splitlines()
    assert len(lines) == len(names)
    for name, line in zip(names, lines):
        assert name in line
    # Elapsed timestamps never go backwards across the sequence.
    elapsed = [float(line.split("s]")[0].removeprefix("[+")) for line in lines]
    assert elapsed == sorted(elapsed)


def test_reset_phase_clock_defaults_to_the_current_monotonic_time() -> None:
    calls: list[None] = []

    def fake_monotonic() -> float:
        calls.append(None)
        return 7.0

    reset_phase_clock(_monotonic=fake_monotonic)

    assert calls  # the default path reads the injected clock, not a hardcoded value
