"""Compile, hash source and raw eligibility of stack compose files (infra2#1822 Slice 1)."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

from common.deploy.compose_compile import (
    ComposeSourceError,
    StackCompose,
    compile_stack,
    has_x_infra2,
    load_stack,
    raw_ineligibility,
)
from common.deploy.compose_equivalence import (
    check,
    equivalence_problems,
    tracked_compose_files,
)
from common.deploy.config_hash import config_hash_from_items

ROOT = Path(__file__).resolve().parents[2]
TODO = "platform/30.todo/compose.yaml"


def _x_stack(**service) -> str:
    spec = {"image": "alpine:3.20", "mem_limit": "64m", **service}
    return yaml.safe_dump({"x-infra2": {}, "services": {"app": spec}}, sort_keys=False)


# --- pass-through: a plain file deploys the same bytes -----------------------------------


def test_every_tracked_compose_file_compiles_to_its_own_bytes() -> None:
    files = tracked_compose_files()
    # 26 files on 2026-11-21; a scan that finds few files proves nothing.
    assert len(files) >= 20, files
    for path in files:
        text = (ROOT / path).read_text(encoding="utf-8")
        assert not has_x_infra2(text), f"{path} uses x-infra2; update this proof"
        compiled = compile_stack(text)
        assert compiled.encode() == text.encode(), path
        env = {"ENV": "staging"}
        assert config_hash_from_items(compiled, env, [], []) == config_hash_from_items(
            text, env, [], []
        ), path
        stack = load_stack(path)
        assert stack.compiled == stack.source and stack.source_type == "github", path


def test_an_x_infra2_file_is_compiled_and_not_passed_through() -> None:
    source = _x_stack()
    compiled = compile_stack(source)
    assert has_x_infra2(source)
    assert "x-infra2" not in yaml.safe_load(compiled)
    assert yaml.safe_load(compiled)["services"]["app"]["restart"] == "always"


def test_a_comment_or_value_that_names_x_infra2_is_not_a_source() -> None:
    text = "# x-infra2 comes later\nservices:\n  app:\n    image: x-infra2:1\n"
    assert not has_x_infra2(text)
    assert compile_stack(text) is text


def test_a_bad_x_infra2_source_fails_closed() -> None:
    with pytest.raises(ComposeSourceError, match="x-infra2 compile failed"):
        compile_stack(_x_stack(**{"x-vault": {}}))
    with pytest.raises(ComposeSourceError, match="mode"):
        compile_stack("services: {}\n", mode="prod")


# --- hash source -------------------------------------------------------------------------


def _deployer(tmp_path: Path, text: str):
    from common.deploy.deployer import Deployer

    compose = tmp_path / "compose.yaml"
    compose.write_text(text, encoding="utf-8")

    class _Stack(Deployer):
        service = "stack"
        compose_path = str(compose)

    return _Stack


def test_configuration_sha256_comes_from_the_compiled_bytes(tmp_path: Path) -> None:
    source = _x_stack()
    dep = _deployer(tmp_path, source)
    env = {"ENV": "staging"}
    got = dep.compute_local_config_hash(MagicMock(), env)
    assert got == config_hash_from_items(compile_stack(source), env, [], [])
    assert got != config_hash_from_items(source, env, [], [])


def test_a_plain_file_keeps_the_hash_of_its_source_bytes(tmp_path: Path) -> None:
    source = (ROOT / "platform/25.free/compose.yaml").read_text(encoding="utf-8")
    dep = _deployer(tmp_path, source)
    env = {"ENV": "staging"}
    assert dep.compute_local_config_hash(MagicMock(), env) == config_hash_from_items(
        source, env, [], []
    )


# --- raw eligibility ---------------------------------------------------------------------


def test_build_makes_a_stack_not_raw_eligible() -> None:
    compiled = compile_stack(_x_stack(build={"context": "."}))
    assert any("`build:`" in r for r in raw_ineligibility(compiled))
    stack = StackCompose(source=_x_stack(), compiled=compiled, x_infra2=True)
    with pytest.raises(ComposeSourceError, match="raw-eligible"):
        stack.source_type


@pytest.mark.parametrize(
    "volume",
    [
        "./users.xml:/etc/users.xml:ro",
        "../shared:/shared",
        "conf/app.ini:/app.ini",
        "${CONF_DIR:-./conf}:/conf",
        {"type": "bind", "source": "./init.sh", "target": "/init.sh"},
    ],
)
def test_a_bind_mount_from_the_checkout_makes_a_stack_not_raw_eligible(volume) -> None:
    compiled = compile_stack(_x_stack(volumes=[volume]))
    assert any("from the checkout" in r for r in raw_ineligibility(compiled))


@pytest.mark.parametrize(
    "volume",
    [
        "data:/var/lib/data",
        "/var/run/docker.sock:/var/run/docker.sock:ro",
        "${DATA_PATH}/app.conf:/app.conf:ro",
        {"type": "volume", "source": "data", "target": "/data"},
    ],
)
def test_a_named_volume_or_host_path_keeps_a_stack_raw_eligible(volume) -> None:
    source = _x_stack(volumes=[volume])
    compiled = compile_stack(source)
    assert raw_ineligibility(compiled) == []
    assert StackCompose(source, compiled, x_infra2=True).source_type == "raw"


def test_a_relative_top_level_config_file_is_not_raw_eligible() -> None:
    text = "services:\n  app:\n    image: a:1\nconfigs:\n  c:\n    file: ./c.conf\n"
    assert raw_ineligibility(text) == ["configs.c reads './c.conf' from the checkout"]


def test_unreadable_compiled_text_is_never_raw_eligible() -> None:
    assert raw_ineligibility(": [")
    assert raw_ineligibility("services: {}\n")


def test_real_stacks_classify_as_measured() -> None:
    """Real files: a vault mount and a build block raw; portal has neither (#1822)."""
    todo = raw_ineligibility(load_stack(TODO).compiled)
    assert todo == [
        "services.vault-agent mounts './vault-agent.hcl' from the checkout; raw has no checkout",
        "services.vault-agent mounts './secrets.ctmpl' from the checkout; raw has no checkout",
    ]
    assert any(
        "`build:`" in r
        for r in raw_ineligibility(load_stack("platform/03.s3/compose.yaml").compiled)
    )
    assert (
        raw_ineligibility(load_stack("platform/21.portal/compose.yaml").compiled) == []
    )


def test_a_raw_eligible_plain_file_still_uses_github() -> None:
    stack = load_stack("platform/21.portal/compose.yaml")
    assert raw_ineligibility(stack.compiled) == []
    assert stack.source_type == "github"


# --- deploy: Dokploy source type ---------------------------------------------------------


def _composing(monkeypatch, tmp_path: Path, text: str):
    import common.deploy.dokploy_adapter as adapter

    dep = _deployer(tmp_path, text)
    calls: list[tuple[str, dict]] = []
    monkeypatch.setattr(dep, "env", classmethod(lambda cls: {"ENV": "staging"}))
    monkeypatch.setattr(
        dep, "_assert_approle_creds_present", classmethod(lambda cls, e: None)
    )
    monkeypatch.setattr(
        dep, "_deploy_compose_with_record_check", classmethod(lambda cls, c, i: None)
    )
    monkeypatch.setattr(
        dep, "ensure_compose_domains", classmethod(lambda cls, c, i, e: {"errors": []})
    )
    client = MagicMock()
    client.find_compose_by_name.return_value = None
    client.get_github_provider_id.return_value = "gh-1"
    monkeypatch.setattr("common.deploy.dokploy_client.get_dokploy", lambda **k: client)
    monkeypatch.setattr(
        "common.deploy.dokploy_client.ensure_project", lambda *a, **k: ("p-1", "e-1")
    )
    monkeypatch.setattr(
        adapter, "upsert_raw_compose", lambda *a, **k: calls.append(("raw", k)) or "c-1"
    )
    monkeypatch.setattr(
        adapter,
        "upsert_github_compose",
        lambda *a, **k: calls.append(("github", k)) or "c-1",
    )
    dep.composing(MagicMock(), {})
    return calls, client


def test_composing_uploads_a_raw_eligible_x_infra2_stack_as_raw(
    monkeypatch, tmp_path: Path
) -> None:
    source = _x_stack()
    calls, client = _composing(monkeypatch, tmp_path, source)
    assert [kind for kind, _ in calls] == ["raw"]
    assert calls[0][1]["compose_file"] == compile_stack(source)
    client.get_github_provider_id.assert_not_called()


def test_composing_keeps_github_for_a_plain_file(monkeypatch, tmp_path: Path) -> None:
    calls, _ = _composing(monkeypatch, tmp_path, "services:\n  app:\n    image: a:1\n")
    assert [kind for kind, _ in calls] == ["github"]


def test_composing_stops_an_x_infra2_stack_with_build_before_any_api_call(
    monkeypatch, tmp_path: Path
) -> None:
    with pytest.raises(ComposeSourceError, match="raw-eligible"):
        _composing(monkeypatch, tmp_path, _x_stack(build={"context": "."}))


# --- Dokploy adapter ---------------------------------------------------------------------


def test_upsert_raw_compose_creates_then_sets_the_compiled_file_again() -> None:
    from common.deploy.dokploy_adapter import upsert_raw_compose

    client = MagicMock()
    client.create_compose.return_value = {"composeId": "c-9"}
    got = upsert_raw_compose(
        client,
        service_name="todo",
        project_name="platform",
        env_id="e-1",
        compose_file="services: {}\n",
        effective_env="A=1",
        existing=None,
        raw_env_str="A=1",
    )
    assert got == "c-9"
    create = client.create_compose.call_args.kwargs
    assert create["source_type"] == "raw" and create["compose_file"] == "services: {}\n"
    update = client.update_compose.call_args
    assert update.args == ("c-9",)
    assert update.kwargs["source_type"] == "raw"
    assert update.kwargs["compose_file"] == "services: {}\n"
    assert update.kwargs["env"] == "A=1" and update.kwargs["autoDeploy"] is False


def test_upsert_raw_compose_updates_an_existing_record_to_raw() -> None:
    from common.deploy.dokploy_adapter import upsert_raw_compose

    client = MagicMock()
    got = upsert_raw_compose(
        client,
        service_name="todo",
        project_name="platform",
        env_id="e-1",
        compose_file="services: {}\n",
        effective_env="A=1\nVAULT_ROLE_ID=r",
        existing={"composeId": "c-1"},
        raw_env_str="A=1",
    )
    assert got == "c-1"
    client.create_compose.assert_not_called()
    kwargs = client.update_compose.call_args.kwargs
    assert kwargs["source_type"] == "raw" and kwargs["env"] == "A=1\nVAULT_ROLE_ID=r"


def test_the_dokploy_client_sends_raw_source_and_compose_file() -> None:
    from common.deploy.dokploy_client import DokployClient

    client = DokployClient.__new__(DokployClient)
    sent: list[dict] = []
    client._request = lambda method, endpoint, **kw: sent.append(kw["json"]) or {}
    client.update_compose("c-1", compose_file="services: {}\n", source_type="raw")
    assert sent[0]["sourceType"] == "raw" and sent[0]["composeFile"] == "services: {}\n"


# --- equivalence with the real hand-written file -----------------------------------------


def _todo_as_x_infra2() -> str:
    """The real 30.todo compose written as x-infra2 (the Slice 2 shape), built from the file."""
    hand = yaml.safe_load((ROOT / TODO).read_text(encoding="utf-8"))["services"]["todo"]
    keep = ("image", "container_name", "mem_limit", "cpu_shares", "healthcheck")
    env = {k: v for k, v in hand["environment"].items() if k != "CONFIGURATION_SHA256"}
    rule = "Host(`todo${ENV_DOMAIN_SUFFIX}.${INTERNAL_DOMAIN}`)"
    service = {k: hand[k] for k in keep}
    service.update(
        command=["python", "/app/app.py"],
        environment=env,
        **{
            "x-vault": {},
            "x-routes": {
                "service": "platform-todo",
                "port": 8000,
                "routes": [
                    {
                        "name": "platform-todo-public",
                        "rule": rule + " && Path(`/api/health`)",
                        "tls": True,
                        "priority": 100,
                    },
                    {
                        "name": "platform-todo",
                        "rule": rule,
                        "tls": True,
                        "priority": 10,
                        "sso": "todo-auth",
                    },
                ],
            },
        },
    )
    doc = {
        "x-infra2": {"vault": {"name": "platform-todo"}},
        "services": {"todo": service},
    }
    return yaml.safe_dump(doc, sort_keys=False, width=10**6)


def _todo_base_files() -> dict[str, str]:
    d = ROOT / "platform/30.todo"
    return {
        n: (d / n).read_text(encoding="utf-8")
        for n in ("secrets.ctmpl", "vault-agent.hcl")
    }


def test_equivalence_finds_the_sidecar_env_suffix_gap_in_the_real_todo_file() -> None:
    """The SDK sidecar has no ENV_SUFFIX; 30.todo/secrets.ctmpl reads it (#1864)."""
    source = _todo_as_x_infra2()
    files = _todo_base_files()
    compiled = compile_stack(source, vault_template=files["secrets.ctmpl"])
    base = (ROOT / TODO).read_text(encoding="utf-8")
    assert equivalence_problems(source, compiled, base, files) == [
        "services.vault-agent.environment.ENV_SUFFIX: only in the base file"
    ]
    without = base.replace(
        "      # secrets.ctmpl reads it to compose DATABASE_URL.\n      ENV_SUFFIX: ${ENV_SUFFIX:-}\n",
        "",
    )
    assert without != base
    assert equivalence_problems(source, compiled, without, files) == []


def test_equivalence_reports_a_changed_label_template_or_command() -> None:
    source = _todo_as_x_infra2()
    files = _todo_base_files()
    compiled = compile_stack(source, vault_template=files["secrets.ctmpl"])
    base = (
        (ROOT / TODO)
        .read_text(encoding="utf-8")
        .replace(
            "      ENV_SUFFIX: ${ENV_SUFFIX:-}\n      CONFIGURATION_SHA256",
            "      CONFIGURATION_SHA256",
        )
    )
    assert equivalence_problems(source, compiled, base, files) == []
    label = base.replace("priority=100", "priority=99")
    assert any(
        "labels" in p for p in equivalence_problems(source, compiled, label, files)
    )
    other = dict(files, **{"secrets.ctmpl": files["secrets.ctmpl"] + "X=1\n"})
    assert "template differs" in " ".join(
        equivalence_problems(source, compiled, base, other)
    )
    cmd = base.replace("exec python /app/app.py", "exec python /app/other.py")
    assert "does not end in" in " ".join(
        equivalence_problems(source, compiled, cmd, files)
    )


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, timeout=60)


def test_check_compares_a_migrated_file_with_its_base_ref(tmp_path: Path) -> None:
    stack = tmp_path / "platform/30.todo"
    stack.mkdir(parents=True)
    for name in ("compose.yaml", "secrets.ctmpl", "vault-agent.hcl"):
        (stack / name).write_bytes((ROOT / "platform/30.todo" / name).read_bytes())
    (tmp_path / "platform/21.portal").mkdir(parents=True)
    (tmp_path / "platform/21.portal/compose.yaml").write_bytes(
        (ROOT / "platform/21.portal/compose.yaml").read_bytes()
    )
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", ".")
    _git(
        tmp_path,
        "-c",
        "user.email=t@example.test",
        "-c",
        "user.name=t",
        "commit",
        "-qm",
        "base",
    )
    base = check("HEAD", root=tmp_path)
    assert (base.files, base.plain_identical, base.x_infra2, base.problems) == (
        2,
        2,
        0,
        [],
    )
    (stack / "compose.yaml").write_text(_todo_as_x_infra2(), encoding="utf-8")
    report = check("HEAD", root=tmp_path)
    assert (report.files, report.plain_identical, report.x_infra2, report.compared) == (
        2,
        1,
        1,
        1,
    )
    assert report.problems == [
        "platform/30.todo/compose.yaml: services.vault-agent.environment.ENV_SUFFIX: only in the base file"
    ]


def test_check_on_this_tree_proves_every_plain_file_and_fails_on_an_empty_scan(
    monkeypatch,
) -> None:
    report = check("HEAD")
    assert report.problems == []
    assert report.plain_identical == report.files >= 20
    import common.deploy.compose_equivalence as eq

    monkeypatch.setattr(eq, "tracked_compose_files", lambda root=ROOT: [])
    assert eq.check("HEAD").problems == [
        "no compose file was checked; the scan is empty"
    ]


def test_check_reports_a_plain_file_that_the_compile_changes(monkeypatch) -> None:
    import common.deploy.compose_equivalence as eq

    monkeypatch.setattr(eq, "compile_stack", lambda text, **k: text + "\n")
    problems = eq.check("HEAD").problems
    assert problems and all(
        "a plain file changed in the compile" in p for p in problems
    )


def test_the_cli_prints_a_plain_file_unchanged(capsys) -> None:
    from tools.compose_compile import main

    assert main(["print", str(ROOT / "platform/25.free/compose.yaml")]) == 0
    out = capsys.readouterr()
    assert out.out == (ROOT / "platform/25.free/compose.yaml").read_text(
        encoding="utf-8"
    )
    assert "# source type: github" in out.err


# --- base ref reads -----------------------------------------------------------------


def test_text_at_ref_refuses_an_unknown_base_and_returns_none_for_a_missing_file() -> (
    None
):
    """Review of #1870: an unknown base ref returned None, so `check` skipped the comparison
    for a migrated file and exited 0."""
    from common.deploy import compose_equivalence as eq

    with pytest.raises(ValueError, match="is not a commit"):
        eq.text_at_ref("deadbeefnotaref", "platform/30.todo/compose.yaml")
    assert eq.text_at_ref("HEAD", "no/such/compose.yaml") is None
    assert "services:" in (
        eq.text_at_ref("HEAD", "platform/30.todo/compose.yaml") or ""
    )


def test_check_reports_an_unknown_base_before_any_file_migrates() -> None:
    """Review of #1870 round 2: with no x-infra2 file, only check() itself reads the base."""
    from common.deploy import compose_equivalence as eq

    report = eq.check("deadbeefnotaref")
    assert report.problems and "is not a commit" in report.problems[0], report.problems
    assert report.files == 0
