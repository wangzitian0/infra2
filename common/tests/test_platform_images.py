"""Platform images that CI builds from this repository and the compose pins (#1822)."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
import yaml

from common.deploy import platform_images as pi

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "platform-images.yml"
DIGEST = "sha256:" + "a" * 64


def _image(services: tuple[str, ...] = ("app",)) -> pi.PlatformImage:
    return pi.PlatformImage(
        "t", "ghcr.io/x/t", "svc/Dockerfile", ".", "svc/compose.yaml", services
    )


def _tree(tmp_path: Path, dockerfile: str, files: dict[str, str]) -> pi.PlatformImage:
    for rel, text in {"svc/Dockerfile": dockerfile, **files}.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return _image()


def _pinned(tmp_path: Path, monkeypatch, service: str) -> pi.PlatformImage:
    """A tree whose compose holds `service` (YAML text), with this table as the only image."""
    image = _tree(tmp_path, "FROM a\nCOPY lib /app/lib\n", {"lib/m.py": "x = 1\n"})
    (tmp_path / "svc" / "compose.yaml").write_text(
        "services:\n  app:\n" + service, encoding="utf-8"
    )
    monkeypatch.setattr(pi, "PLATFORM_IMAGES", {"t": image})
    return image


def _good_pin(image: pi.PlatformImage, root: Path) -> str:
    return f"    image: {image.repository}:{pi.expected_tag(image, root)}@{DIGEST}\n"


# --- the input hash ---------------------------------------------------------------------


def test_hash_is_the_documented_formula(tmp_path: Path) -> None:
    image = _tree(tmp_path, "FROM a\nCOPY lib /app/lib\n", {"lib/m.py": "x = 1\n"})
    lines = [
        "lib/m.py:" + hashlib.sha256(b"x = 1\n").hexdigest(),
        "svc/Dockerfile:" + hashlib.sha256(b"FROM a\nCOPY lib /app/lib\n").hexdigest(),
    ]
    expected = hashlib.sha256("\n".join(lines).encode()).hexdigest()
    assert pi.input_hash(image, tmp_path) == expected
    assert pi.expected_tag(image, tmp_path) == expected[:32]


def test_a_copied_file_change_changes_the_tag(tmp_path: Path) -> None:
    image = _tree(tmp_path, "FROM a\nCOPY lib /app/lib\n", {"lib/m.py": "x = 1\n"})
    before = pi.input_hash(image, tmp_path)
    (tmp_path / "lib" / "m.py").write_text("x = 2\n", encoding="utf-8")
    assert pi.input_hash(image, tmp_path) != before


def test_a_file_that_is_not_copied_does_not_change_the_tag(tmp_path: Path) -> None:
    image = _tree(tmp_path, "FROM a\nCOPY lib /app/lib\n", {"lib/m.py": "x = 1\n"})
    before = pi.input_hash(image, tmp_path)
    (tmp_path / "other.txt").write_text("y\n", encoding="utf-8")
    (tmp_path / "lib" / "__pycache__").mkdir()
    (tmp_path / "lib" / "__pycache__" / "m.cpython-312.pyc").write_bytes(b"\0")
    assert pi.input_hash(image, tmp_path) == before


def test_a_missing_dockerfile_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="svc/Dockerfile"):
        pi.input_hash(_image(), tmp_path)


def test_a_copy_glob_with_no_match_raises(tmp_path: Path) -> None:
    image = _tree(tmp_path, "FROM a\nCOPY lib/*.py /app/\n", {})
    with pytest.raises(FileNotFoundError, match="matched no files"):
        pi.input_hash(image, tmp_path)


# --- the compose pin --------------------------------------------------------------------


def test_the_real_pins_match_their_input_files() -> None:
    assert pi.pin_problems() == []


def test_a_correct_pin_has_no_problem(tmp_path: Path, monkeypatch) -> None:
    image = _pinned(tmp_path, monkeypatch, "")
    (tmp_path / "svc" / "compose.yaml").write_text(
        "services:\n  app:\n" + _good_pin(image, tmp_path), encoding="utf-8"
    )
    assert pi.pin_problems(tmp_path) == []


def test_a_dockerfile_edit_without_a_new_pin_is_a_problem(
    tmp_path: Path, monkeypatch
) -> None:
    image = _pinned(tmp_path, monkeypatch, "")
    old_tag = pi.expected_tag(image, tmp_path)
    (tmp_path / "svc" / "compose.yaml").write_text(
        "services:\n  app:\n" + _good_pin(image, tmp_path), encoding="utf-8"
    )
    (tmp_path / "lib" / "m.py").write_text("x = 2\n", encoding="utf-8")
    new_tag = pi.expected_tag(image, tmp_path)
    assert pi.pin_problems(tmp_path) == [
        f"svc/compose.yaml service 'app' pins tag {old_tag}; the input files give "
        f"{new_tag}. Push the branch; the platform-images job prints the new pin."
    ]


@pytest.mark.parametrize(
    "image_line",
    [
        "    image: ghcr.io/x/t:{tag}\n",  # no digest
        "    image: ghcr.io/x/other:{tag}@" + DIGEST + "\n",  # another repository
        "    image: ghcr.io/x/t:v1.0.0@" + DIGEST + "\n",  # a tag that is not a hash
        "    image: ${{T_IMAGE}}\n",  # a deploy variable, not a pin
    ],
)
def test_a_pin_that_is_not_repository_tag_digest_is_a_problem(
    tmp_path: Path, monkeypatch, image_line: str
) -> None:
    image = _pinned(tmp_path, monkeypatch, "")
    line = image_line.format(tag=pi.expected_tag(image, tmp_path))
    (tmp_path / "svc" / "compose.yaml").write_text(
        "services:\n  app:\n" + line, encoding="utf-8"
    )
    [problem] = pi.pin_problems(tmp_path)
    assert "is not ghcr.io/x/t:<tag>@sha256:<digest>" in problem


def test_a_service_that_builds_is_a_problem(tmp_path: Path, monkeypatch) -> None:
    image = _pinned(tmp_path, monkeypatch, "")
    (tmp_path / "svc" / "compose.yaml").write_text(
        "services:\n  app:\n    build: .\n" + _good_pin(image, tmp_path),
        encoding="utf-8",
    )
    assert pi.pin_problems(tmp_path) == [
        "svc/compose.yaml service 'app' has `build:`; CI publishes ghcr.io/x/t"
    ]


def test_a_missing_service_is_a_problem(tmp_path: Path, monkeypatch) -> None:
    _pinned(tmp_path, monkeypatch, "    image: x\n")
    monkeypatch.setattr(pi, "PLATFORM_IMAGES", {"t": _image(("gone",))})
    assert pi.pin_problems(tmp_path) == [
        "svc/compose.yaml service 'gone' does not exist"
    ]


def test_the_check_command_exits_1_on_a_problem(tmp_path: Path, monkeypatch) -> None:
    _pinned(tmp_path, monkeypatch, "    image: x\n")
    monkeypatch.setattr(pi, "pin_problems", lambda: ["p"])
    assert pi.main(["check"]) == 1
    monkeypatch.setattr(pi, "pin_problems", lambda: [])
    assert pi.main(["check"]) == 0


def test_no_stack_compose_builds_a_published_dockerfile() -> None:
    """A stack `compose.yaml` that builds a published Dockerfile cannot deploy raw.

    Scope: each stack `compose.yaml`. A preview file (`preview.compose.yaml`) has a
    checkout (`sourceType=github`), so its `build:` does not block a raw deploy.
    """
    published = {(ROOT / i.dockerfile).resolve() for i in pi.PLATFORM_IMAGES.values()}
    found = []
    for compose in sorted(ROOT.glob("platform/*/compose.yaml")):
        services = yaml.safe_load(compose.read_text()).get("services") or {}
        for name, spec in services.items():
            build = spec.get("build") if isinstance(spec, dict) else None
            if isinstance(build, dict):
                context = (compose.parent / str(build.get("context") or ".")).resolve()
                dockerfile = context / str(build.get("dockerfile") or "Dockerfile")
                if dockerfile.resolve() in published:
                    found.append(f"{compose.relative_to(ROOT)}:{name}")
    assert found == []


# --- the commit-tagged image ------------------------------------------------------------


def _commit_tree(tmp_path: Path, monkeypatch, service: str) -> pi.PlatformImage:
    image = pi.PlatformImage(
        "t",
        "ghcr.io/x/t",
        "svc/Dockerfile",
        ".",
        "svc/compose.yaml",
        ("app",),
        tag_kind="commit",
    )
    _tree(tmp_path, "FROM a\nCOPY lib /app/lib\n", {"lib/m.py": "x = 1\n"})
    (tmp_path / "svc" / "compose.yaml").write_text(
        "services:\n  app:\n" + service, encoding="utf-8"
    )
    monkeypatch.setattr(pi, "PLATFORM_IMAGES", {"t": image})
    return image


def test_the_commit_tag_is_the_deploy_ref_and_stops_compose_without_it() -> None:
    assert pi.COMMIT_TAG == "${INFRA_IAC_REF:?}"
    image = pi.PLATFORM_IMAGES["alerting"]
    assert image.tag_kind == "commit"
    assert image.commit_ref == "ghcr.io/wangzitian0/infra2-alerting:${INFRA_IAC_REF:?}"
    assert set(pi.pinned_refs(image).values()) == {image.commit_ref}


def test_a_copied_file_change_needs_no_commit_pin_edit(
    tmp_path: Path, monkeypatch
) -> None:
    """The defect of an input tag (#2055): a copied file edit failed the check."""
    _commit_tree(tmp_path, monkeypatch, '    image: "ghcr.io/x/t:${INFRA_IAC_REF:?}"\n')
    assert pi.pin_problems(tmp_path) == []
    (tmp_path / "lib" / "m.py").write_text("x = 2\n", encoding="utf-8")
    assert pi.pin_problems(tmp_path) == []


@pytest.mark.parametrize(
    "image_line",
    [
        "    image: ghcr.io/x/t:latest\n",
        "    image: ghcr.io/x/t:${INFRA_IAC_REF:-latest}\n",  # a fallback tag
        "    image: ghcr.io/x/t:${INFRA_IAC_REF}\n",  # an empty ref pulls `t:`
        "    image: ghcr.io/x/t:${INFRA_IAC_REF:?}@" + DIGEST + "\n",
        "    image: ghcr.io/x/other:${INFRA_IAC_REF:?}\n",
    ],
)
def test_a_commit_image_that_is_not_the_commit_ref_is_a_problem(
    tmp_path: Path, monkeypatch, image_line: str
) -> None:
    _commit_tree(tmp_path, monkeypatch, image_line)
    [problem] = pi.pin_problems(tmp_path)
    assert problem.endswith("is not ghcr.io/x/t:${INFRA_IAC_REF:?}")


def test_a_commit_image_service_that_builds_is_a_problem(
    tmp_path: Path, monkeypatch
) -> None:
    _commit_tree(
        tmp_path,
        monkeypatch,
        '    build: .\n    image: "ghcr.io/x/t:${INFRA_IAC_REF:?}"\n',
    )
    assert pi.pin_problems(tmp_path) == [
        "svc/compose.yaml service 'app' has `build:`; CI publishes ghcr.io/x/t"
    ]


def test_an_unknown_tag_kind_raises() -> None:
    with pytest.raises(ValueError, match="tag_kind 'latest'"):
        pi.PlatformImage("t", "r", "d", ".", "c", ("a",), tag_kind="latest")


def test_the_deploy_waits_for_the_commit_image_of_alerting_only() -> None:
    """deploy_v2 waits for these repositories at the deploy commit (ServiceSpec)."""
    from common.deploy.contract import all_service_keys, service_spec

    waits = {
        key: service_spec(key).image_repositories
        for key in all_service_keys()
        if service_spec(key).iac_pinned and service_spec(key).image_repositories
    }
    assert waits == {"platform/alerting": ("ghcr.io/wangzitian0/infra2-alerting",)}
    assert pi.commit_image_repositories("platform/s3") == ()


def test_the_ghcr_retention_never_deletes_a_platform_image() -> None:
    """A rollback to an older release pulls the commit tag of that release.

    The retention keeps a main commit tag only in the `<7 hex>` form, so a platform
    package in its list would lose each 40-hex tag after the window.
    """
    from common.deploy.ghcr_retention import PACKAGES

    packages = {i.repository.rsplit("/", 1)[1] for i in pi.PLATFORM_IMAGES.values()}
    assert packages == {"infra2-s3", "infra2-alerting"}
    assert packages.isdisjoint(PACKAGES)


# --- the config hash reads the image input files ---------------------------------------


def test_the_config_hash_reads_the_inputs_of_a_pinned_platform_image(
    tmp_path: Path, monkeypatch
) -> None:
    """The service has no `build:`; the hash reads the Dockerfile and its copied files."""
    from common.deploy import config_hash

    image = _pinned(tmp_path, monkeypatch, "")
    compose = tmp_path / "svc" / "compose.yaml"
    text = "services:\n  app:\n" + _good_pin(image, tmp_path)
    compose.write_text(text, encoding="utf-8")
    assert config_hash._compose_artifact_files(str(compose), text) == [
        tmp_path.resolve() / "lib" / "m.py",
        tmp_path.resolve() / "svc" / "Dockerfile",
    ]


def test_the_config_hash_reads_no_platform_inputs_for_another_image(
    tmp_path: Path, monkeypatch
) -> None:
    from common.deploy import config_hash

    _pinned(tmp_path, monkeypatch, "")
    compose = tmp_path / "svc" / "compose.yaml"
    text = "services:\n  app:\n    image: ghcr.io/x/other:1@" + DIGEST + "\n"
    compose.write_text(text, encoding="utf-8")
    assert config_hash._compose_artifact_files(str(compose), text) == []


def test_the_real_s3_hash_reads_the_dockerfile_without_the_vault_mounts() -> None:
    """Measured on this tree: the s3 service alone gives the Dockerfile as its one input."""
    from common.deploy import config_hash

    image = pi.PLATFORM_IMAGES["s3"]
    compose = ROOT / image.compose
    services = yaml.safe_load(compose.read_text(encoding="utf-8"))["services"]
    text = yaml.safe_dump({"services": {"s3": services["s3"]}})
    assert config_hash._compose_artifact_files(str(compose), text) == [
        (ROOT / image.dockerfile).resolve()
    ]


def test_the_real_alerting_hash_reads_the_image_inputs_without_the_vault_mounts() -> (
    None
):
    """Measured on this tree: both alerting services give the image input files only.

    An edit of a copied file changes the config hash, so the deploy recreates the stack
    with the new commit tag. The tag is not in the hash: the compose text is constant.
    """
    from common.deploy import config_hash

    image = pi.PLATFORM_IMAGES["alerting"]
    compose = ROOT / image.compose
    services = yaml.safe_load(compose.read_text(encoding="utf-8"))["services"]
    text = yaml.safe_dump({"services": {n: services[n] for n in image.services}})
    files = config_hash._compose_artifact_files(str(compose), text)
    assert files == pi.input_files(image)
    assert len(files) > 400


def test_the_alerting_hash_covers_each_tracked_file_that_the_dockerfile_copies() -> (
    None
):
    """The Dockerfile copies uv.lock, tools/sdk_requirement.py, app.py, common and tools.

    The repository has no .dockerignore, so the build copies each tracked file below
    these paths. git (not the hash code) lists them.
    """
    import subprocess

    image = pi.PLATFORM_IMAGES["alerting"]
    copied = ["uv.lock", "tools/sdk_requirement.py", "platform/12.alerting/app.py"]
    copied += ["common", "tools"]
    assert not (ROOT / ".dockerignore").exists()
    tracked = subprocess.run(
        ["git", "ls-files", "--", *copied],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    ).stdout.split()
    inputs = {p.relative_to(ROOT).as_posix() for p in pi.input_files(image)}
    assert len(tracked) > 400
    assert set(tracked) <= inputs
    assert "platform/12.alerting/Dockerfile" in inputs
    outside = {
        p
        for p in inputs - {image.dockerfile}
        if not any(p == c or p.startswith(c + "/") for c in copied)
    }
    assert outside == set()


# --- the CI matrix and workflow ---------------------------------------------------------


def test_the_matrix_has_one_row_per_image() -> None:
    commit = "e" * 40
    rows = pi.build_matrix(commit=commit)
    assert [r["key"] for r in rows] == list(pi.PLATFORM_IMAGES)
    for row in rows:
        image = pi.PLATFORM_IMAGES[row["key"]]
        common = {
            "key": image.key,
            "dockerfile": image.dockerfile,
            "context": image.context,
            "compose": image.compose,
        }
        if image.tag_kind == "commit":
            assert row == {
                **common,
                "kind": "commit",
                "image": f"{image.repository}:{commit}",
                "pinned_digest": "",
            }
            continue
        [ref] = set(pi.pinned_refs(image).values())
        assert row == {
            **common,
            "kind": "inputs",
            "image": f"{image.repository}:{pi.expected_tag(image)}",
            "pinned_digest": image.pin_re.match(ref)["digest"],
        }


def test_a_matrix_without_a_commit_has_no_commit_image() -> None:
    """A pull request passes no commit: its merge ref is never a deploy commit."""
    rows = pi.build_matrix()
    assert [r["key"] for r in rows] == ["s3"]
    assert [r["key"] for r in pi.build_matrix(commit="e" * 40)] == ["s3", "alerting"]


@pytest.mark.parametrize("commit", ["e" * 39, "v3.2.34", "main", "E" * 39 + "g"])
def test_a_matrix_commit_that_is_not_a_full_sha_raises(commit: str) -> None:
    with pytest.raises(ValueError, match="full 40-hex SHA"):
        pi.build_matrix(commit=commit)


def test_two_different_digests_give_no_pinned_digest(
    tmp_path: Path, monkeypatch
) -> None:
    image = _pinned(tmp_path, monkeypatch, "")
    tag = pi.expected_tag(image, tmp_path)
    monkeypatch.setattr(pi, "PLATFORM_IMAGES", {"t": _image(("a", "b"))})
    (tmp_path / "svc" / "compose.yaml").write_text(
        "services:\n"
        f"  a:\n    image: ghcr.io/x/t:{tag}@{DIGEST}\n"
        f"  b:\n    image: ghcr.io/x/t:{tag}@sha256:{'b' * 64}\n",
        encoding="utf-8",
    )
    [row] = pi.build_matrix(tmp_path)
    assert row["pinned_digest"] == ""


def test_the_workflow_publishes_the_matrix_rows() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text())
    runs = "\n".join(s.get("run", "") for s in workflow["jobs"]["matrix"]["steps"])
    assert "python -m common.deploy.platform_images matrix" in runs
    publish = workflow["jobs"]["publish"]
    assert publish["strategy"]["matrix"]["include"] == (
        "${{ fromJSON(needs.matrix.outputs.images) }}"
    )
    assert publish["permissions"] == {"contents": "read", "packages": "write"}
    assert publish["env"]["PINNED_DIGEST"] == "${{ matrix.pinned_digest }}"
    assert publish["env"]["KIND"] == "${{ matrix.kind }}"
    # PyYAML reads the `on:` key as True.
    assert set(workflow[True]) == {"pull_request", "push", "workflow_dispatch"}


def test_the_workflow_gives_each_commit_its_own_concurrency_group() -> None:
    """GitHub cancels a pending run when a newer run joins its group.

    A group per ref left a main commit without its commit tag when three pushes came
    close together; the deploy of that commit then had no image.
    """
    workflow = yaml.safe_load(WORKFLOW.read_text())
    group = workflow["concurrency"]["group"]
    assert "github.sha" in group and "inputs.ref" in group
    assert workflow["concurrency"]["cancel-in-progress"] is False


def test_the_workflow_passes_no_commit_on_a_pull_request() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text())
    [step] = [s for s in workflow["jobs"]["matrix"]["steps"] if s.get("id") == "matrix"]
    run = step["run"]
    assert 'matrix --commit "$commit"' in run
    assert 'if [ "$EVENT_NAME" = "pull_request" ]; then commit=""; fi' in run
    # The publish job builds the same commit that the matrix read.
    publish = workflow["jobs"]["publish"]
    [checkout] = [s for s in publish["steps"] if "checkout" in s.get("uses", "")]
    assert checkout["with"]["ref"] == "${{ needs.matrix.outputs.sha }}"
    assert publish["env"]["SHA"] == "${{ needs.matrix.outputs.sha }}"
