"""Platform images that CI builds from this repository and publishes to GHCR (#1822).

A raw Dokploy compose (``sourceType="raw"``) has no checkout, so a stack cannot `build:`
its image at deploy time. CI builds each image in ``PLATFORM_IMAGES`` from its Dockerfile
and pushes ``<repository>:<tag>`` (``.github/workflows/platform-images.yml``).

The tag is the first ``TAG_LENGTH`` hex characters of the input hash: the sha256 over
``<repo-relative path>:<sha256 of bytes>`` of the Dockerfile and of each file that it
copies (the file set of the config hash, ``config_hash._dockerfile_copy_sources``).

An image has one of two tag kinds (``PlatformImage.tag_kind``):

- ``inputs``: the compose pins ``<repository>:<tag>@sha256:<digest>``. ``pin_problems`` fails
  when the pinned tag is not the tag of the files in this checkout, so a Dockerfile edit
  without a new pin cannot merge. The CI job compares the pinned digest with the GHCR digest
  of the tag. Use it for an image whose inputs seldom change.
- ``commit``: the tag is the full infra2 commit SHA. The compose names
  ``<repository>:${INFRA_IAC_REF:?}``; the deploy issues ``INFRA_IAC_REF``, the commit that it
  deploys (``common/deploy/sync_pipeline.py``). CI builds the tag on each push to ``main``.
  No PR edits a pin. Use it for an image that copies files that most PRs change.

CI pushes a tag only when GHCR does not have it, so a tag holds one image.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

from common.deploy.yaml_text import safe_load_text

REPO_ROOT = Path(__file__).resolve().parents[2]
REGISTRY = "ghcr.io/wangzitian0"
TAG_LENGTH = 32
TAG_KINDS = ("inputs", "commit")
# The compose tag of a ``commit`` image. ``:?`` stops `docker compose` when the deploy
# issues no INFRA_IAC_REF, so the stack never pulls another tag.
COMMIT_TAG = "${INFRA_IAC_REF:?}"
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True)
class PlatformImage:
    """One image that CI builds from this repository, and the compose that pins it."""

    key: str
    repository: str
    dockerfile: str  # repo-relative path
    context: str  # repo-relative build context
    compose: str  # repo-relative compose file that pins the image
    services: tuple[str, ...]  # compose services that run the image
    tag_kind: str = "inputs"  # one of TAG_KINDS

    def __post_init__(self) -> None:
        if self.tag_kind not in TAG_KINDS:
            raise ValueError(
                f"{self.key}: tag_kind {self.tag_kind!r} is not in {TAG_KINDS}"
            )

    @property
    def commit_ref(self) -> str:
        """The exact compose image value of a ``commit`` image."""
        return f"{self.repository}:{COMMIT_TAG}"

    @property
    def pin_re(self) -> re.Pattern[str]:
        return re.compile(
            rf"^{re.escape(self.repository)}:(?P<tag>[0-9a-f]{{{TAG_LENGTH}}})"
            r"@(?P<digest>sha256:[0-9a-f]{64})$"
        )


PLATFORM_IMAGES: dict[str, PlatformImage] = {
    image.key: image
    for image in (
        PlatformImage(
            key="s3",
            repository=f"{REGISTRY}/infra2-s3",
            dockerfile="platform/03.s3/Dockerfile",
            context=".",
            compose="platform/03.s3/compose.yaml",
            services=("s3",),
        ),
        # The image copies all of common/ and tools/, so its input tag changes on most PRs.
        # The commit tag needs no pin edit (#1822; the input-tag pin of #2055 was dropped).
        PlatformImage(
            key="alerting",
            repository=f"{REGISTRY}/infra2-alerting",
            dockerfile="platform/12.alerting/Dockerfile",
            context=".",
            compose="platform/12.alerting/compose.yaml",
            services=("feishu-alert-bridge", "infra-probe-runner"),
            tag_kind="commit",
        ),
    )
}


def commit_image_repositories(service_id: str) -> tuple[str, ...]:
    """GHCR repositories of the ``commit`` images that the stack of ``service_id`` runs.

    The deploy waits until each repository has the tag of the deploy commit
    (``ServiceSpec.image_repositories``, ``preflight._wait_for_image_dependencies``).
    """
    from common.deploy.dependencies import service_key_from_path

    return tuple(
        image.repository
        for image in PLATFORM_IMAGES.values()
        if image.tag_kind == "commit"
        and service_key_from_path(image.compose) == service_id
    )


def input_files(image: PlatformImage, root: Path = REPO_ROOT) -> list[Path]:
    """The Dockerfile and each file that it copies, sorted. Raises when a file is missing."""
    from common.deploy.config_hash import _dockerfile_copy_sources

    root = root.resolve()
    dockerfile = (root / image.dockerfile).resolve()
    if not dockerfile.is_file():
        raise FileNotFoundError(
            f"{image.key}: Dockerfile {image.dockerfile} does not exist"
        )
    context = (root / image.context).resolve()
    return sorted({dockerfile, *_dockerfile_copy_sources(dockerfile, context)})


def input_hash(image: PlatformImage, root: Path = REPO_ROOT) -> str:
    """sha256 over ``<repo-relative path>:<sha256 of bytes>`` of each input file."""
    root = root.resolve()
    lines = [
        f"{path.relative_to(root).as_posix()}:{hashlib.sha256(path.read_bytes()).hexdigest()}"
        for path in input_files(image, root)
    ]
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def expected_tag(image: PlatformImage, root: Path = REPO_ROOT) -> str:
    return input_hash(image, root)[:TAG_LENGTH]


def pinned_refs(image: PlatformImage, root: Path = REPO_ROOT) -> dict[str, str]:
    """``{service: image value}`` of each service that runs the image. Raises when unreadable."""
    text = (root / image.compose).read_text(encoding="utf-8")
    services = (safe_load_text(yaml, text) or {}).get("services") or {}
    return {
        name: str((services.get(name) or {}).get("image") or "")
        for name in image.services
    }


def pin_problems(root: Path = REPO_ROOT) -> list[str]:
    """Each pin that is malformed, builds, or names a tag that is not its tag kind."""
    problems: list[str] = []
    for image in PLATFORM_IMAGES.values():
        tag = expected_tag(image, root) if image.tag_kind == "inputs" else ""
        text = (root / image.compose).read_text(encoding="utf-8")
        services = (safe_load_text(yaml, text) or {}).get("services") or {}
        for name in image.services:
            where = f"{image.compose} service {name!r}"
            spec = services.get(name)
            if not isinstance(spec, dict):
                problems.append(f"{where} does not exist")
                continue
            if "build" in spec:
                problems.append(
                    f"{where} has `build:`; CI publishes {image.repository}"
                )
            ref = str(spec.get("image") or "")
            if image.tag_kind == "commit":
                if ref != image.commit_ref:
                    problems.append(f"{where} image {ref!r} is not {image.commit_ref}")
                continue
            match = image.pin_re.match(ref)
            if not match:
                problems.append(
                    f"{where} image {ref!r} is not {image.repository}:<tag>@sha256:<digest>"
                )
            elif match["tag"] != tag:
                problems.append(
                    f"{where} pins tag {match['tag']}; the input files give {tag}. "
                    f"Push the branch; the platform-images job prints the new pin."
                )
    return problems


def build_matrix(root: Path = REPO_ROOT, commit: str = "") -> list[dict[str, str]]:
    """One row per image for the CI publish job.

    ``commit`` is the full SHA of the checkout. Without it (a pull request), the matrix has
    no ``commit`` image: only a push to ``main`` or a manual run publishes a commit tag.
    """
    commit = commit.strip().lower()
    if commit and not _COMMIT_RE.match(commit):
        raise ValueError(f"commit must be a full 40-hex SHA, got {commit!r}")
    rows = []
    for image in PLATFORM_IMAGES.values():
        if image.tag_kind == "commit":
            if commit:
                rows.append(
                    {
                        "key": image.key,
                        "kind": "commit",
                        "image": f"{image.repository}:{commit}",
                        "dockerfile": image.dockerfile,
                        "context": image.context,
                        "compose": image.compose,
                        "pinned_digest": "",
                    }
                )
            continue
        digests = sorted(
            {
                m["digest"]
                for ref in pinned_refs(image, root).values()
                if (m := image.pin_re.match(ref))
            }
        )
        rows.append(
            {
                "key": image.key,
                "kind": "inputs",
                "image": f"{image.repository}:{expected_tag(image, root)}",
                "dockerfile": image.dockerfile,
                "context": image.context,
                "compose": image.compose,
                "pinned_digest": digests[0] if len(digests) == 1 else "",
            }
        )
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    matrix = sub.add_parser(
        "matrix", help="print the CI publish matrix as one JSON line"
    )
    matrix.add_argument(
        "--commit",
        default="",
        help="full SHA of the checkout; empty gives no commit-tagged image",
    )
    sub.add_parser(
        "check", help="exit 1 when a compose pin does not match its input files"
    )
    args = parser.parse_args(argv)
    if args.command == "matrix":
        print(json.dumps(build_matrix(commit=args.commit), separators=(",", ":")))
        return 0
    problems = pin_problems()
    for problem in problems:
        print(f"platform image pin: {problem}", file=sys.stderr)
    print(f"platform images: {len(PLATFORM_IMAGES)}; problems: {len(problems)}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
