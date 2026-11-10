"""Behavior tests for the pytest durations and faulthandler settings (#1494)."""

import subprocess
import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = REPO_ROOT / "pyproject.toml"


def test_faulthandler_timeout_is_120_seconds():
    config = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    assert config["tool"]["pytest"]["ini_options"]["faulthandler_timeout"] == 120


def test_durations_report_names_slow_test_under_repo_config(tmp_path):
    slow_test = tmp_path / "test_slow_probe.py"
    slow_test.write_text(
        "import time\n\n\ndef test_sleeps_slowly():\n    time.sleep(1.2)\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(slow_test),
            "-c",
            str(PYPROJECT),
            "--rootdir",
            str(tmp_path),
            "-n",
            "0",
            "-p",
            "no:cacheprovider",
        ],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=tmp_path,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "slowest 25 durations" in result.stdout
    assert "test_slow_probe.py::test_sleeps_slowly" in result.stdout
