"""libs.image_digest — a tag resolves to the digest the registry serves, and nothing is guessed."""

from __future__ import annotations

import pytest

from libs.image_digest import ImageDigestError, resolve_image_digest

IMAGE = "ghcr.io/wangzitian0/truealpha-data-engine"
DIGEST = "sha256:" + "d" * 64


class _Resp:
    def __init__(
        self, status_code: int, headers: dict | None = None, body: dict | None = None
    ):
        self.status_code = status_code
        self.headers = headers or {}
        self._body = body or {}

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"status {self.status_code}")


class _Client:
    """Scripted registry: a list of HEAD responses, plus one token response."""

    def __init__(self, heads, token=None):
        self._heads = list(heads)
        self._token = token
        self.calls: list[tuple[str, str, dict]] = []

    def head(self, url, headers=None):
        self.calls.append(("HEAD", url, dict(headers or {})))
        return self._heads.pop(0)

    def get(self, url, params=None):
        self.calls.append(("GET", url, dict(params or {})))
        return _Resp(200, body={"token": self._token})


def test_resolves_the_docker_content_digest_of_a_tag() -> None:
    client = _Client([_Resp(200, {"docker-content-digest": DIGEST})])
    assert resolve_image_digest(IMAGE, "v0.0.46", client=client) == DIGEST
    method, url, headers = client.calls[0]
    assert (
        method == "HEAD"
        and url
        == "https://ghcr.io/v2/wangzitian0/truealpha-data-engine/manifests/v0.0.46"
    )
    # the OCI index accept set, so a multi-arch tag yields its index digest — what
    # `docker pull image@digest` and `imagetools inspect` agree on
    assert "application/vnd.oci.image.index.v1+json" in headers["Accept"]


def test_answers_a_bearer_challenge_with_an_anonymous_token() -> None:
    challenge = 'Bearer realm="https://ghcr.io/token",service="ghcr.io",scope="repository:wangzitian0/truealpha-data-engine:pull"'
    client = _Client(
        [
            _Resp(401, {"www-authenticate": challenge}),
            _Resp(200, {"docker-content-digest": DIGEST}),
        ],
        token="anon",
    )
    assert resolve_image_digest(IMAGE, "v0.0.46", client=client) == DIGEST
    assert client.calls[1] == (
        "GET",
        "https://ghcr.io/token",
        {
            "service": "ghcr.io",
            "scope": "repository:wangzitian0/truealpha-data-engine:pull",
        },
    )
    assert client.calls[2][2]["Authorization"] == "Bearer anon"


def test_a_digest_ref_passes_through_unchanged() -> None:
    client = _Client([])
    assert resolve_image_digest(IMAGE, DIGEST, client=client) == DIGEST
    assert client.calls == []


@pytest.mark.parametrize(
    "response, message",
    [
        (_Resp(404), "does not exist"),
        (_Resp(403), "refused"),
        (_Resp(503), "answered 503"),
        (
            _Resp(200, {"docker-content-digest": "sha256:short"}),
            "no usable Docker-Content-Digest",
        ),
        (_Resp(200, {}), "no usable Docker-Content-Digest"),
    ],
)
def test_fails_closed_instead_of_guessing(response, message) -> None:
    with pytest.raises(ImageDigestError, match=message):
        resolve_image_digest(IMAGE, "v0.0.46", client=_Client([response]))


def test_rejects_a_ref_that_is_neither_tag_nor_digest() -> None:
    with pytest.raises(ValueError, match="registry tag or a sha256 digest"):
        resolve_image_digest(IMAGE, "v0.0.46 && rm -rf /", client=_Client([]))
