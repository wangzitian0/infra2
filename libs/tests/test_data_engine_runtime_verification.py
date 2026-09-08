"""#595: a data-engine deploy is verified against the RUNNING containers, and the first
pull of a new digest is waited for instead of false-failing the sync.

2026-07-27 and 2026-09-04: the sync reported `promoted image digest was not applied`
with the OLD digest while the ~1 GB pull was still in flight; the containers came up on
the new digest seconds after the 90 s window. The verification now pulls the promoted
digest itself (Docker shares the layers with the pull compose already started, and the
call returns when the image is local), then expects the containers to switch, then waits
for their healthchecks."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from invoke.exceptions import CommandTimedOut

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "truealpha/truealpha/20.data_engine/deploy.py"
DIGEST = "sha256:" + "f" * 64
IMAGE = f"ghcr.io/wangzitian0/truealpha-data-engine@{DIGEST}"
OLD = "ghcr.io/wangzitian0/truealpha-data-engine@sha256:" + "0" * 64


def _load():
    spec = importlib.util.spec_from_file_location(
        "truealpha_data_engine_deploy_595", DEPLOY
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Result:
    def __init__(self, stdout: str = "", ok: bool = True, stderr: str = ""):
        self.stdout, self.ok, self.stderr = stdout, ok, stderr


class _Context:
    """Scripts the host: `pull` answers, then per-container image answers in order, then
    per-container health answers in order. Every command is recorded with its timeout."""

    def __init__(
        self, *, pull: _Result, images: list[list[str]], health: list[list[str]]
    ):
        self.pull, self.images, self.health = pull, images, health
        self.calls: list[tuple[str, int | None]] = []
        self._image_round, self._health_round = 0, 0
        self._image_index = self._health_index = 0

    def run(
        self,
        command: str,
        warn: bool = False,
        hide: bool = False,
        timeout: int | None = None,
    ):
        self.calls.append((command, timeout))
        if "docker pull" in command:
            return self.pull
        if ".Config.Image" in command:
            answers = self.images[min(self._image_round, len(self.images) - 1)]
            answer = answers[self._image_index]
            self._image_index += 1
            if self._image_index == 3:
                self._image_index, self._image_round = 0, self._image_round + 1
            return _Result(answer)
        if ".State.Health" in command:
            answers = self.health[min(self._health_round, len(self.health) - 1)]
            answer = answers[self._health_index]
            self._health_index += 1
            if self._health_index == 3:
                self._health_index, self._health_round = 0, self._health_round + 1
            return _Result(answer)
        raise AssertionError(command)


@pytest.fixture
def deployer(monkeypatch):
    module = _load()
    cls = module.DataEngineDeployer
    monkeypatch.setattr(
        cls, "env", classmethod(lambda c: {"VPS_HOST": "vps", "ENV_SUFFIX": "-staging"})
    )
    monkeypatch.setattr(module.time, "sleep", lambda _s: None)
    clock = {"now": 0.0}

    def monotonic():
        clock["now"] += 20.0
        return clock["now"]

    monkeypatch.setattr(module.time, "monotonic", monotonic)
    return cls


def test_the_promoted_digest_is_pulled_before_the_containers_are_inspected(
    deployer,
) -> None:
    context = _Context(
        pull=_Result(DIGEST),
        images=[[OLD, OLD, OLD], [IMAGE, IMAGE, IMAGE]],
        health=[["healthy", "healthy", "starting"], ["healthy", "healthy", "healthy"]],
    )
    assert (
        deployer.verify_runtime_applied(context, {"DATA_ENGINE_IMAGE_DIGEST": DIGEST})
        is None
    )
    first, timeout = context.calls[0]
    assert (
        f"docker pull -q {IMAGE}" in first and timeout == deployer.PULL_DEADLINE_SECONDS
    )
    assert "ssh root@vps" in first
    names = [c for c, _ in context.calls if ".Config.Image" in c]
    assert any("truealpha-dagster-code-server-staging" in c for c in names)
    # the old digest on the first round was waited out, not reported
    assert sum(".State.Health" in c for c, _ in context.calls) == 6


def test_a_pull_that_does_not_finish_names_the_digest_and_the_deadline(
    deployer,
) -> None:
    context = _Context(
        pull=_Result(
            "", ok=False, stderr="Error response from daemon: manifest unknown"
        ),
        images=[[IMAGE, IMAGE, IMAGE]],
        health=[["healthy"] * 3],
    )
    error = deployer.verify_runtime_applied(
        context, {"DATA_ENGINE_IMAGE_DIGEST": DIGEST}
    )
    assert error and "could not be pulled" in error and "manifest unknown" in error
    assert DIGEST[:19] in error and str(deployer.PULL_DEADLINE_SECONDS) in error
    assert len(context.calls) == 1


def test_containers_that_never_switch_still_fail_with_the_stale_image(deployer) -> None:
    context = _Context(
        pull=_Result(DIGEST), images=[[OLD, IMAGE, IMAGE]], health=[["healthy"] * 3]
    )
    error = deployer.verify_runtime_applied(
        context, {"DATA_ENGINE_IMAGE_DIGEST": DIGEST}
    )
    assert error and error.startswith("promoted image digest was not applied")
    assert "truealpha-dagster-webserver-staging=" + OLD in error
    assert not any(".State.Health" in c for c, _ in context.calls)


def test_a_running_but_unhealthy_promoted_build_is_not_applied(deployer) -> None:
    context = _Context(
        pull=_Result(DIGEST),
        images=[[IMAGE, IMAGE, IMAGE]],
        health=[["healthy", "unhealthy", "healthy"]],
    )
    error = deployer.verify_runtime_applied(
        context, {"DATA_ENGINE_IMAGE_DIGEST": DIGEST}
    )
    assert error and "running but not healthy" in error
    assert "truealpha-dagster-daemon-staging=unhealthy" in error


def test_a_container_without_a_healthcheck_counts_as_healthy_when_running(
    deployer,
) -> None:
    context = _Context(
        pull=_Result(DIGEST),
        images=[[IMAGE] * 3],
        health=[["running", "healthy", "healthy"]],
    )
    assert (
        deployer.verify_runtime_applied(context, {"DATA_ENGINE_IMAGE_DIGEST": DIGEST})
        is None
    )


def test_a_pull_that_times_out_fails_closed_with_the_same_message(deployer) -> None:
    """invoke raises CommandTimedOut when `timeout=` elapses (review on #645); the
    verification must not let that exception abort the deploy path."""

    class _TimingOut(_Context):
        def run(self, command, warn=False, hide=False, timeout=None):
            if "docker pull" in command:
                raise CommandTimedOut(_Result("", ok=False), timeout)
            return super().run(command, warn=warn, hide=hide, timeout=timeout)

    context = _TimingOut(
        pull=_Result(DIGEST), images=[[IMAGE] * 3], health=[["healthy"] * 3]
    )
    error = deployer.verify_runtime_applied(
        context, {"DATA_ENGINE_IMAGE_DIGEST": DIGEST}
    )
    assert error and "could not be pulled" in error
    assert f"timed out after {deployer.PULL_DEADLINE_SECONDS}s" in error
