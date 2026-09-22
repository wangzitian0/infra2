"""`--strict` has to turn a missing credential into exit 2, and nothing asserted it.

#776 removed a shell branch that printed SKIP and exited 0 ahead of
``pi_chain_smoke.py --strict``, on the premise that ``--strict`` would then
make a missing credential an infra error. The wiring test proved the step
reaches the tool; it substitutes a stub, so it never runs ``preflight`` at all.
An audit put it plainly: the whole argument rested on a function that no
executed assertion touched. One real CI run has since shown exit 2, which is evidence
for that day's code and no guard against the next edit — flipping the ternary
in ``missing()`` would restore the silence #776 was written to end.

Every case here is about that ternary and the ways a credential can be absent
without being missing.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools import pi_chain_smoke as smoke


@pytest.fixture(autouse=True)
def _pi_on_path(monkeypatch):
    """preflight refuses before anything else when `pi` is absent, and the
    machine running the suite may not have it. Pretend it is there so the
    credential branches are what gets measured."""
    monkeypatch.setattr(smoke.shutil, "which", lambda name: f"/usr/bin/{name}")


@pytest.fixture(autouse=True)
def _no_ambient_credential(monkeypatch, tmp_path):
    """A developer with a real auth.json would otherwise see these pass for the
    wrong reason."""
    monkeypatch.delenv("ZAI_CODING_CN_API_KEY", raising=False)
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "agent"))


def test_no_pi_binary_is_infra_whatever_strict_says(monkeypatch) -> None:
    monkeypatch.setattr(smoke.shutil, "which", lambda name: None)
    for strict in (False, True):
        status, detail = smoke.preflight(strict)
        assert status == "infra" and "pi binary" in detail


def test_the_env_credential_is_enough() -> None:
    import os

    os.environ["ZAI_CODING_CN_API_KEY"] = "k"
    try:
        assert smoke.preflight(True)[0] == "ok"
    finally:
        del os.environ["ZAI_CODING_CN_API_KEY"]


def _write_auth(tmp_path: Path, payload: object) -> None:
    agent = tmp_path / "agent"
    agent.mkdir(parents=True, exist_ok=True)
    (agent / "auth.json").write_text(
        payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8"
    )


@pytest.mark.parametrize(
    ("payload", "why"),
    (
        (None, "no auth.json at all"),
        ("{not json", "auth.json is not JSON"),
        ([], "auth.json is a list, not an object"),
        ({"other-provider": {}}, "auth.json has no zai-coding-cn entry"),
    ),
)
def test_strict_turns_every_absence_into_an_infra_error(
    payload: object, why: str, tmp_path: Path
) -> None:
    """This is the contract #776 rests on: with --strict there is no SKIP.

    Exit 2 is what the caller reads, and `main` maps "infra" to it — so a
    ternary flipped the other way would put the daily proof back to reporting
    success while launching nothing.
    """
    if payload is not None:
        _write_auth(tmp_path, payload)
    status, detail = smoke.preflight(True)
    assert status == "infra", f"{why} was not an infra error under --strict"
    assert detail, "an infra verdict with no reason sends the reader nowhere"


@pytest.mark.parametrize("payload", (None, "{not json", [], {"other-provider": {}}))
def test_without_strict_the_same_absences_are_a_skip(
    payload: object, tmp_path: Path
) -> None:
    """The local half of the contract: running it by hand must not be an error."""
    if payload is not None:
        _write_auth(tmp_path, payload)
    assert smoke.preflight(False)[0] == "skip"


def test_a_usable_auth_file_passes(tmp_path: Path) -> None:
    """The control. Without it, `preflight` could return infra unconditionally
    and every assertion above would still hold."""
    _write_auth(tmp_path, {smoke.PROVIDER: {"api_key": "k"}})
    status, detail = smoke.preflight(True)
    assert status == "ok" and "auth.json" in detail


def test_main_maps_infra_to_exit_two(monkeypatch, capsys) -> None:
    """The exit code is the whole interface; the status string is internal."""
    monkeypatch.setattr(smoke, "preflight", lambda strict: ("infra", "no credential"))
    monkeypatch.setattr("sys.argv", ["pi_chain_smoke.py", "--strict"])
    assert smoke.main() == 2
    assert json.loads(capsys.readouterr().out)["verdict"] == "INFRA"


def test_main_maps_skip_to_exit_zero(monkeypatch, capsys) -> None:
    monkeypatch.setattr(smoke, "preflight", lambda strict: ("skip", "no credential"))
    monkeypatch.setattr("sys.argv", ["pi_chain_smoke.py"])
    assert smoke.main() == 0
    assert json.loads(capsys.readouterr().out)["verdict"] == "SKIP"


_PASSING_CHECKS = {
    "exit0": True, "agent_end": True, "route_ok": True, "stop_ok": True,
    "text_ok": True, "tokens_ok": True, "totalTokens": 42,
}


@pytest.mark.parametrize(
    "detail",
    (
        "credential from ZAI_CODING_CN_API_KEY env",
        "credential from /fake/agent/auth.json",
    ),
)
def test_main_names_the_credential_source_it_actually_used(
    monkeypatch, capsys, detail: str
) -> None:
    """dev_env#48: a passing preflight used to discard `detail` -- the run
    picked env or auth.json, and nothing downstream could tell which. Both
    must now be visible: printed (stderr, so the one-line stdout verdict
    stays machine-parseable) and carried into the verdict JSON, regardless
    of which of the two lines actually supplied the credential.
    """
    monkeypatch.setattr(smoke, "preflight", lambda strict: ("ok", detail))
    monkeypatch.setattr(smoke, "run_once", lambda: (dict(_PASSING_CHECKS), ""))
    monkeypatch.setattr("sys.argv", ["pi_chain_smoke.py"])
    assert smoke.main() == 0
    captured = capsys.readouterr()
    assert detail in captured.err
    verdict = json.loads(captured.out)
    assert verdict["verdict"] == "PASS"
    assert verdict["credential_source"] == detail


def test_main_names_the_credential_source_on_a_failed_run_too(monkeypatch, capsys) -> None:
    """The source name must survive a FAIL verdict as well -- diagnosing a
    remote failure benefits from knowing which credential line was live,
    not just that the run failed."""
    detail = "credential from ZAI_CODING_CN_API_KEY env"
    monkeypatch.setattr(smoke, "preflight", lambda strict: ("ok", detail))
    failing = dict(_PASSING_CHECKS, route_ok=False)
    monkeypatch.setattr(smoke, "run_once", lambda: (failing, ""))
    monkeypatch.setattr("sys.argv", ["pi_chain_smoke.py"])
    assert smoke.main() == 1
    verdict = json.loads(capsys.readouterr().out)
    assert verdict["verdict"] == "FAIL"
    assert verdict["credential_source"] == detail
