"""Resolve a registry tag to the immutable OCI digest it points at (truealpha#712).

The data-engine stack is digest-pinned by design (``20.data_engine/compose.yaml``): a
release promotes ``truealpha-data-engine:vX.Y.Z`` by writing the tag's digest into the
environment's Vault path. Until 2026-09-07 that write was an operator session
(``docker buildx imagetools inspect`` on the host, then ``vault kv patch``); the promotion
therefore could not run from a release request, and the app shipped one release ahead of
the engine with every gate green (truealpha v0.0.37). This module is the registry half of
the fix: the iac-runner resolves the digest itself, from the same Registry v2 manifest API
``tools.deploy_v2`` already uses for its artifact-readiness check.

The digest returned is the one the ``Docker-Content-Digest`` header names for the tag's
manifest as served with the OCI index/manifest accept set — i.e. the multi-arch index
digest when the image is an index, the manifest digest otherwise — which is exactly what
``docker pull image@sha256:…`` and ``imagetools inspect`` print for the tag.
"""

from __future__ import annotations

import re

import httpx

_DIGEST_RE = re.compile(r"\Asha256:[0-9a-f]{64}\Z")
_MANIFEST_ACCEPT = ", ".join(
    [
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    ]
)


class ImageDigestError(RuntimeError):
    """The registry did not yield a digest for the tag (missing, refused, or malformed)."""


def _registry_parts(image: str) -> tuple[str, str]:
    registry, _, repository = image.partition("/")
    if not registry or not repository:
        raise ValueError(f"image must be registry/repository, got {image!r}")
    return registry, repository


def _parse_authenticate(header: str) -> dict[str, str]:
    scheme, _, params = header.partition(" ")
    if scheme.lower() != "bearer":
        return {}
    return {
        key.strip(): value.strip().strip('"')
        for key, _, value in (part.partition("=") for part in params.split(","))
        if key.strip()
    }


def _bearer_token(client: httpx.Client, authenticate_header: str) -> str:
    params = _parse_authenticate(authenticate_header)
    realm = params.get("realm")
    if not realm:
        raise ImageDigestError("registry challenged without a bearer realm")
    query = {k: v for k, v in params.items() if k in ("service", "scope")}
    response = client.get(realm, params=query)
    response.raise_for_status()
    token = response.json().get("token") or response.json().get("access_token")
    if not token:
        raise ImageDigestError("registry token endpoint returned no token")
    return str(token)


def resolve_image_digest(
    image: str, ref: str, *, client: httpx.Client | None = None
) -> str:
    """Return ``sha256:…`` for ``image:ref`` as the registry serves it, or raise.

    ``ref`` is a tag (``v0.0.46``) or an existing digest (returned unchanged after a
    shape check). Anonymous pull tokens are fetched on a Bearer challenge, as GHCR
    answers for public packages. Fails closed on 404 (no such tag), 401/403 after the
    challenge, and any 5xx/429 — a promotion must never guess a digest.
    """
    if _DIGEST_RE.match(ref):
        return ref
    if not re.match(r"\A[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}\Z", ref):
        raise ValueError(f"ref must be a registry tag or a sha256 digest, got {ref!r}")
    if client is None:
        with httpx.Client(timeout=15.0, follow_redirects=True) as created:
            return resolve_image_digest(image, ref, client=created)
    registry, repository = _registry_parts(image)
    url = f"https://{registry}/v2/{repository}/manifests/{ref}"

    def request(token: str | None = None) -> httpx.Response:
        headers = {"Accept": _MANIFEST_ACCEPT}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return client.head(url, headers=headers)

    response = request()
    if response.status_code == 401:
        response = request(
            _bearer_token(client, response.headers.get("www-authenticate", ""))
        )
    if response.status_code == 404:
        raise ImageDigestError(f"{image}:{ref} does not exist in the registry")
    if response.status_code in (401, 403):
        raise ImageDigestError(
            f"registry refused {image}:{ref} (status {response.status_code})"
        )
    if not 200 <= response.status_code < 300:
        raise ImageDigestError(
            f"registry answered {response.status_code} for {image}:{ref}"
        )
    digest = response.headers.get("docker-content-digest", "")
    if not _DIGEST_RE.match(digest):
        raise ImageDigestError(
            f"registry returned no usable Docker-Content-Digest for {image}:{ref} ({digest!r})"
        )
    return digest
