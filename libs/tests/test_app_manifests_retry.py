"""libs.security.app_manifests: the manifest prefetch survives a transport blip (#824).

PR #818 failed a required check 1.3 s in, before pytest started:
``urllib.error.URLError: <urlopen error [Errno 104] Connection reset by peer>``.
The prefetch had no retry, so one reset on raw.githubusercontent.com turned the
merge authority red with zero test signal. Same failure class as #810 / #813.
"""

from __future__ import annotations

import http.client
import ssl
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from libs.security import app_manifests
from tools import fetch_app_manifests

SHA = "0123456789abcdef0123456789abcdef01234567"
PATH = "repos/truealpha/apps/x/required-env.generated.json"
URL = f"https://raw.githubusercontent.com/wangzitian0/truealpha/{SHA}/apps/x/required-env.generated.json"
BODY = b'{"contract_version": 2}'


class _Response:
    """What ``urllib.request.urlopen`` returns: a context manager with ``read()``."""

    def __init__(self, body: bytes = BODY, read_error: BaseException | None = None):
        self._body = body
        self._read_error = read_error

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False

    def read(self) -> bytes:
        if self._read_error is not None:
            raise self._read_error
        return self._body


def _reset() -> urllib.error.URLError:
    """The exact exception of the #818 failure: urlopen wraps the errno 104 reset."""
    return urllib.error.URLError(ConnectionResetError(104, "Connection reset by peer"))


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(URL, code, f"status {code}", {}, None)  # type: ignore[arg-type]


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch):
    """One pinned submodule, a scripted ``urlopen``, and a recorded (never slept) backoff."""
    monkeypatch.setattr(app_manifests, "submodule_commit", lambda root, sub: SHA)
    sleeps: list[float] = []
    monkeypatch.setattr(time, "sleep", sleeps.append)
    script: list[object] = []  # one entry per urlopen call: an exception, or a response
    urls: list[str] = []

    def fake_urlopen(url: str, timeout: float | None = None) -> _Response:
        urls.append(url)
        step = script[len(urls) - 1] if len(urls) <= len(script) else script[-1]
        if isinstance(step, BaseException):
            raise step
        assert isinstance(step, _Response)
        return step

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    class World:
        pass

    state = World()
    state.script = script  # type: ignore[attr-defined]
    state.urls = urls  # type: ignore[attr-defined]
    state.sleeps = sleeps  # type: ignore[attr-defined]
    return state


def _target(tmp_path: Path) -> Path:
    return tmp_path / ".cache/app-manifests" / PATH


def test_the_818_reset_is_retried_once_and_the_fetch_succeeds(
    world, tmp_path: Path
) -> None:
    """Red->green: the #818 traceback (URLError wrapping errno 104) on the first call,
    the real body on the second. The tool's entry point must fetch, not die."""
    world.script[:] = [_reset(), _Response()]

    fetched = fetch_app_manifests.fetch_missing([PATH], root=tmp_path)

    assert fetched == [f"{PATH} @ {SHA[:7]}"]
    assert _target(tmp_path).read_bytes() == BODY
    assert world.urls == [URL, URL]  # 1 failed attempt + 1 retry that succeeded
    assert world.sleeps == [2]  # one backoff, #813's 2**attempt shape


@pytest.mark.parametrize(
    "transient",
    [
        pytest.param(ConnectionResetError(104, "reset"), id="bare-connection-reset"),
        pytest.param(TimeoutError("timed out"), id="read-timeout"),
        pytest.param(
            urllib.error.URLError(TimeoutError("timed out")), id="connect-timeout"
        ),
        pytest.param(
            urllib.error.URLError(
                ssl.SSLError("[SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred")
            ),
            id="tls-eof",
        ),
        pytest.param(_http_error(500), id="http-500"),
        pytest.param(_http_error(502), id="http-502"),
        pytest.param(_http_error(503), id="http-503"),
        pytest.param(_http_error(429), id="http-429-rate-limit"),
        pytest.param(
            http.client.RemoteDisconnected("Remote end closed connection"),
            id="remote-disconnected",
        ),
    ],
)
def test_every_transient_error_is_retried_then_succeeds(
    world, tmp_path: Path, transient: BaseException
) -> None:
    world.script[:] = [transient, _Response()]

    assert app_manifests.fetch_missing([PATH], root=tmp_path) == [f"{PATH} @ {SHA[:7]}"]
    assert len(world.urls) == 2
    assert _target(tmp_path).read_bytes() == BODY


def test_a_connection_that_dies_mid_body_is_retried(world, tmp_path: Path) -> None:
    """The reset may land in ``read()``, after ``urlopen`` returned: the retry unit is
    the whole request-and-read, not the connect alone."""
    world.script[:] = [
        _Response(read_error=http.client.IncompleteRead(b'{"contr', 20)),
        _Response(),
    ]

    app_manifests.fetch_missing([PATH], root=tmp_path)

    assert len(world.urls) == 2
    assert _target(tmp_path).read_bytes() == BODY


def test_a_blip_that_never_clears_fails_after_three_attempts(
    world, tmp_path: Path
) -> None:
    """Red->green: the budget is bounded (1 attempt + 2 retries, #813's budget), and the
    error names the URL and the attempt count so the CI log is diagnosable."""
    world.script[:] = [_reset()]  # every call resets

    with pytest.raises(app_manifests.ManifestFetchError, match="3 attempts") as caught:
        app_manifests.fetch_missing([PATH], root=tmp_path)

    assert URL in str(caught.value)
    assert isinstance(caught.value.__cause__, urllib.error.URLError)
    assert len(world.urls) == 3
    assert world.sleeps == [2, 4]  # no sleep after the last attempt
    assert not _target(
        tmp_path
    ).exists()  # a failed fetch leaves no partial cache entry


@pytest.mark.parametrize("code", [400, 401, 403, 404, 410])
def test_a_permanent_http_status_is_not_retried(
    world, tmp_path: Path, code: int
) -> None:
    """A 404 means the pinned commit lacks the manifest: retrying cannot fix it, and the
    required check must say so at once."""
    world.script[:] = [_http_error(code)]

    with pytest.raises(urllib.error.HTTPError) as caught:
        app_manifests.fetch_missing([PATH], root=tmp_path)

    assert caught.value.code == code
    assert len(world.urls) == 1
    assert world.sleeps == []


@pytest.mark.parametrize(
    "permanent",
    [
        pytest.param(
            urllib.error.URLError("unknown url type: htps"), id="url-with-text-reason"
        ),
        pytest.param(
            urllib.error.URLError(
                ssl.SSLCertVerificationError("certificate verify failed")
            ),
            id="tls-certificate-failure",
        ),
        pytest.param(ValueError("not a transport error"), id="programming-error"),
    ],
)
def test_a_permanent_error_is_not_retried(
    world, tmp_path: Path, permanent: BaseException
) -> None:
    world.script[:] = [permanent]

    with pytest.raises(type(permanent)):
        app_manifests.fetch_missing([PATH], root=tmp_path)

    assert len(world.urls) == 1
    assert world.sleeps == []


def test_the_backoff_is_injectable_for_callers_and_tests(world, tmp_path: Path) -> None:
    """``_github_raw`` takes the same ``_retries`` / ``_sleep`` hooks as
    ``supply.retrying_transport``: no test needs to patch the clock to bound its time."""
    world.script[:] = [_reset(), _reset(), _Response()]
    sleeps: list[float] = []

    body = app_manifests._github_raw(URL, _sleep=sleeps.append)

    assert body == BODY
    assert sleeps == [2, 4]
    assert world.sleeps == []  # the injected hook, not time.sleep, did the waiting

    world.urls.clear()
    world.script[:] = [_reset()]
    with pytest.raises(app_manifests.ManifestFetchError, match="1 attempt"):
        app_manifests._github_raw(URL, _retries=0, _sleep=sleeps.append)
    assert len(world.urls) == 1
