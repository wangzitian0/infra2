"""Logging order test for platform/30.todo (#1213, #1209).

`init_service(..., enable_otel=True)` runs at import time. With a real OTLP endpoint it adds
an OpenTelemetry `LoggingHandler` to the root logger. `logging.basicConfig` does nothing when
the root logger already has a handler. `init_service` calls `basicConfig` at INFO before it
attaches that handler, so the app holds no `basicConfig` call of its own. If the order broke,
the root level would stay WARNING and every app INFO line would be lost.

Root logger state is global, so each case loads the app in a fresh subprocess.
This test needs the real OpenTelemetry packages from the `dev` dependency group.
A missing package makes the subprocess exit with an error. The test then fails.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
TODO_APP = REPO_ROOT / "platform/30.todo/app.py"

# The script imports the app, stubs the call that starts the real server, and runs main().
# It prints one JSON line with the root logger state, then exits without atexit hooks.
# The OTLP exporter points at a closed port, so a normal exit would wait on the flush.
_PROBE_SCRIPT = textwrap.dedent(
    """
    import importlib.util
    import json
    import logging
    import os
    import sys
    import types

    uvicorn_stub = types.ModuleType("uvicorn")
    uvicorn_stub.run = lambda *args, **kwargs: None
    sys.modules["uvicorn"] = uvicorn_stub

    spec = importlib.util.spec_from_file_location("todo_app_logging_order", sys.argv[1])
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.main()

    root = logging.getLogger()
    state = {
        "level": root.level,
        "handlers": [
            type(h).__module__ + "." + type(h).__qualname__ for h in root.handlers
        ],
    }
    sys.stdout.write(json.dumps(state) + "\\n")
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)
    """
)


def _run_probe(app_path: Path) -> tuple[dict, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("OTEL_")}
    env["OTEL_EXPORTER_OTLP_ENDPOINT"] = "http://127.0.0.1:1"
    env["OTEL_SERVICE_NAME"] = "platform-todo"
    env["OTEL_RESOURCE_ATTRIBUTES"] = (
        "deployment.environment.name=staging,infra.service.id=platform/todo,"
        f"service.version={'a' * 40},infra.iac.ref={'a' * 40}"
    )
    env["ENV"] = "staging"
    env["PORT"] = "8000"
    result = subprocess.run(
        [sys.executable, "-c", _PROBE_SCRIPT, str(app_path)],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, (
        f"probe exit code {result.returncode}\nstdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )
    json_lines = [ln for ln in result.stdout.splitlines() if ln.startswith("{")]
    assert len(json_lines) == 1, f"expected one JSON line, got stdout:\n{result.stdout}"
    return json.loads(json_lines[0]), result.stderr


def test_todo_main_leaves_root_logger_at_info_with_otel_handler() -> None:
    """After `main()`, the root level is INFO and both handlers are present."""
    state, stderr = _run_probe(TODO_APP)

    assert state["level"] == logging.INFO, (
        f"root level is {state['level']}, expected {logging.INFO}; handlers: "
        f"{state['handlers']}"
    )
    assert state["handlers"].count("logging.StreamHandler") == 1, state["handlers"]
    otel_handlers = [
        h
        for h in state["handlers"]
        if h.startswith("opentelemetry.") and h.endswith(".LoggingHandler")
    ]
    assert len(otel_handlers) == 1, state["handlers"]
    # The stream handler writes the app INFO line from main(). A dropped INFO line fails here.
    assert "INFO platform-todo Canary Todo service starting on port 8000" in stderr, (
        stderr
    )
