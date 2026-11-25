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
    assert pi.pin_problems(tmp_path) == ["svc/compose.yaml service 'gone' does not exist"]


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


# --- the CI matrix and workflow ---------------------------------------------------------


def test_the_matrix_has_one_row_per_image() -> None:
    rows = pi.build_matrix()
    assert [r["key"] for r in rows] == list(pi.PLATFORM_IMAGES)
    for row in rows:
        image = pi.PLATFORM_IMAGES[row["key"]]
        [ref] = set(pi.pinned_refs(image).values())
        assert row == {
            "key": image.key,
            "image": f"{image.repository}:{pi.expected_tag(image)}",
            "dockerfile": image.dockerfile,
            "context": image.context,
            "compose": image.compose,
            "pinned_digest": image.pin_re.match(ref)["digest"],
        }


def test_two_different_digests_give_no_pinned_digest(tmp_path: Path, monkeypatch) -> None:
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
    # PyYAML reads the `on:` key as True.
    assert set(workflow[True]) == {"pull_request", "push", "workflow_dispatch"}
