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
    vault_template_for,
)
from common.deploy.compose_equivalence import (
    check,
    equivalence_problems,
    tracked_compose_files,
)
from common.deploy.config_hash import config_hash_from_items

ROOT = Path(__file__).resolve().parents[2]
TODO = "platform/30.todo/compose.yaml"
# The stacks on x-infra2. A migration adds its file here and proves its equivalence.
X_INFRA2_FILES = {
    TODO,
    "finance_report/finance_report/01.postgres/compose.yaml",
    "finance_report/finance_report/02.redis/compose.yaml",
    "truealpha/truealpha/01.postgres/compose.yaml",
    "finance_report/finance_report/10.app/compose.yaml",
    "truealpha/truealpha/10.app/compose.yaml",
}


# The Canary image has no default hosts: /api/canary/status turns red when the compiled
# todo service lacks one of these.
CANARY_HOSTS = {
    "REDIS_HOST": "platform-redis${ENV_SUFFIX}",
    "S3_ENDPOINT": "http://platform-s3${ENV_SUFFIX}:9000",
    "SIGNOZ_OTEL_COLLECTOR": "http://platform-signoz-otel-collector:13133",
    "OPENPANEL_API_URL": "http://platform-openpanel-api:3000/healthcheck",
    "AUTHENTIK_ENDPOINT": "http://platform-authentik-server${ENV_SUFFIX}:9000/-/health/live/",
}


def _x_stack(**service) -> str:
    spec = {"image": "alpine:3.20", "mem_limit": "64m", **service}
    return yaml.safe_dump({"x-infra2": {}, "services": {"app": spec}}, sort_keys=False)


# --- pass-through: a plain file deploys the same bytes -----------------------------------


def test_every_tracked_compose_file_compiles_to_its_own_bytes() -> None:
    files = tracked_compose_files()
    # 26 files on 2026-11-21; a scan that finds few files proves nothing.
    assert len(files) >= 20, files
    migrated = {
        p for p in files if has_x_infra2((ROOT / p).read_text(encoding="utf-8"))
    }
    assert migrated == X_INFRA2_FILES, (
        "a stack moved to x-infra2; update X_INFRA2_FILES"
    )
    for path in sorted(set(files) - migrated):
        text = (ROOT / path).read_text(encoding="utf-8")
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


@pytest.mark.parametrize(
    ("spec", "reason"),
    [
        ("    env_file: ./app.env\n", "services.a reads env_file './app.env'"),
        (
            "    env_file:\n      - path: ./a.env\n",
            "services.a reads env_file './a.env'",
        ),
        (
            "    extends:\n      file: ./base.yaml\n      service: b\n",
            "services.a extends './base.yaml'",
        ),
    ],
)
def test_raw_ineligibility_names_each_file_that_the_service_reads(spec, reason) -> None:
    text = "services:\n  a:\n    image: x\n" + spec
    assert [r for r in raw_ineligibility(text) if r.startswith(reason)], (
        raw_ineligibility(text)
    )
    # A host path outside the checkout is still readable by a raw record.
    absolute = text.replace("./", "/etc/")
    assert raw_ineligibility(absolute) == []


def test_raw_ineligibility_names_a_top_level_include() -> None:
    text = "include:\n  - ./other.yaml\nservices:\n  a:\n    image: x\n"
    assert raw_ineligibility(text) == [
        "top-level `include:` reads files; raw has no checkout"
    ]


def test_unreadable_compiled_text_is_never_raw_eligible() -> None:
    assert raw_ineligibility(": [")
    assert raw_ineligibility("services: {}\n")


def test_real_stacks_classify_as_measured() -> None:
    """A vault mount and a build block raw; portal has neither (#1822)."""
    mounted = (
        "services:\n  vault-agent:\n    image: hashicorp/vault:1.15\n    volumes:\n"
        "      - ./vault-agent.hcl:/etc/vault/vault-agent.hcl:ro\n"
        "      - ./secrets.ctmpl:/etc/vault/secrets.ctmpl:ro\n"
    )
    assert raw_ineligibility(mounted) == [
        "services.vault-agent mounts './vault-agent.hcl' from the checkout; raw has no checkout",
        "services.vault-agent mounts './secrets.ctmpl' from the checkout; raw has no checkout",
    ]
    # Slice 2: the x-infra2 todo has the template inline, so it is raw-eligible.
    todo = load_stack(TODO)
    assert raw_ineligibility(todo.compiled) == []
    assert todo.source_type == "raw"
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
    # A record switched from github keeps its old composePath unless the update resets
    # it; Dokploy then writes its env file into a directory that a raw deploy wiped (#1889).
    assert kwargs["composePath"] == "./docker-compose.yml"


def test_the_raw_compose_path_is_where_dokploy_writes_the_raw_file() -> None:
    """Dokploy writes a raw compose to code/docker-compose.yml and its env file to
    dirname(code/<composePath>) with no mkdir. Only this path puts both in code/."""
    import posixpath

    from common.deploy.dokploy_adapter import RAW_COMPOSE_PATH, raw_source_fields

    assert raw_source_fields("x")["composePath"] == RAW_COMPOSE_PATH
    code = "/etc/dokploy/compose/app/code"
    env_dir = posixpath.dirname(
        posixpath.normpath(posixpath.join(code, RAW_COMPOSE_PATH))
    )
    assert env_dir == code


def test_the_dokploy_client_sends_raw_source_and_compose_file() -> None:
    from common.deploy.dokploy_client import DokployClient

    client = DokployClient.__new__(DokployClient)
    sent: list[dict] = []
    client._request = lambda method, endpoint, **kw: sent.append(kw["json"]) or {}
    client.update_compose("c-1", compose_file="services: {}\n", source_type="raw")
    assert sent[0]["sourceType"] == "raw" and sent[0]["composeFile"] == "services: {}\n"


# --- the real todo source -----------------------------------------------------------------


def _todo_source() -> str:
    """The real 30.todo compose: an x-infra2 source since #1822 Slice 2."""
    return (ROOT / TODO).read_text(encoding="utf-8")


def _todo_template() -> str:
    """The Vault template that the deploy renders for the todo stack (no file in git)."""
    template = vault_template_for(TODO, _todo_source())
    assert template and template.startswith("{{- /* Generated from"), template
    return template


def _todo_compiled(source: str | None = None) -> str:
    source = _todo_source() if source is None else source
    return compile_stack(source, vault_template=_todo_template())


def _todo_without(source: str, name: str) -> str:
    """The todo source without the one line or capability that sets ``name``."""
    if name == "S3_ENDPOINT":
        old = "x-capabilities: [s3, otel]"
        assert source.count(old) == 1
        return source.replace(old, "x-capabilities: [otel]")
    lines = [
        line for line in source.splitlines(True) if line.strip().startswith(f"{name}:")
    ]
    assert len(lines) == 1, (name, lines)
    return source.replace(lines[0], "")


def test_the_compiled_todo_sets_each_canary_host() -> None:
    """The Canary image at 4.3.0 has no default hosts (#1822 Slice 2)."""
    env = yaml.safe_load(_todo_compiled())["services"]["todo"]["environment"]
    assert {name: env.get(name) for name in CANARY_HOSTS} == CANARY_HOSTS


@pytest.mark.parametrize("name", sorted(CANARY_HOSTS))
def test_a_todo_source_that_drops_a_canary_host_compiles_without_it(name: str) -> None:
    source = _todo_without(_todo_source(), name)
    env = yaml.safe_load(_todo_compiled(source))["services"]["todo"]["environment"]
    assert name not in env
    assert {n: env.get(n) for n in CANARY_HOSTS} != CANARY_HOSTS


def test_the_compiled_sidecar_forwards_each_env_that_the_template_reads() -> None:
    """The todo template builds DATABASE_URL with `env "ENV_SUFFIX"` (#1864)."""
    import re

    template = _todo_template()
    reads = set(re.findall(r'env "([A-Z_]+)"', template))
    assert reads == {"ENV", "ENV_SUFFIX"}
    sidecar = yaml.safe_load(_todo_compiled())["services"]["vault-agent"]
    assert sidecar["container_name"] == "platform-todo-vault-agent${ENV_SUFFIX}"
    env = sidecar["environment"]
    assert {name: env.get(name) for name in reads} == {
        "ENV": "${ENV}",
        "ENV_SUFFIX": "${ENV_SUFFIX:-}",
    }
    assert env["VAULT_SECRETS_TEMPLATE"].replace("$$", "$") == template


# --- equivalence with a hand-written file (synthetic stack) ------------------------------

_EQ_TEMPLATE = 'KEY={{ env "ENV_SUFFIX" }}\n'
_EQ_SOURCE = """\
x-infra2:
  vault:
    name: svc
services:
  app:
    image: a:1
    container_name: app${ENV_SUFFIX}
    command: ["run", "--now"]
    x-vault: {}
    environment:
      A: "1"
      B: "2"
    labels:
      - traefik.http.routers.app.priority=100
"""


def _eq_case() -> tuple[str, str, dict[str, str | None]]:
    """The compiled synthetic stack, its hand-written base, and the base files.

    The base is the compiled text in the hand-written form: the sidecar mounts its
    files (no inline env) and the app entrypoint ends in `exec <command>`.
    """
    compiled = compile_stack(_EQ_SOURCE, vault_template=_EQ_TEMPLATE)
    doc = yaml.safe_load(compiled)
    env = doc["services"]["vault-agent"]["environment"]
    hcl = env.pop("VAULT_AGENT_HCL").replace("$$", "$")
    assert env.pop("VAULT_SECRETS_TEMPLATE").replace("$$", "$") == _EQ_TEMPLATE
    app = doc["services"]["app"]
    app["entrypoint"] = ["sh", "-c", "set -e\n. /secrets/.env\nexec run --now\n"]
    del app["command"]
    base = yaml.safe_dump(doc, sort_keys=False, width=10**6)
    files: dict[str, str | None] = {
        "secrets.ctmpl": _EQ_TEMPLATE,
        "vault-agent.hcl": hcl.replace(
            "/vault/secrets.ctmpl", "/etc/vault/secrets.ctmpl"
        ),
    }
    return compiled, base, files


def test_an_equivalent_hand_written_file_has_no_problem() -> None:
    compiled, base, files = _eq_case()
    assert equivalence_problems(_EQ_SOURCE, compiled, base, files) == []


def test_equivalence_finds_a_sidecar_env_that_the_base_lacks() -> None:
    compiled, base, files = _eq_case()
    without = base.replace("      ENV_SUFFIX: ${ENV_SUFFIX:-}\n", "", 1)
    assert without != base
    assert equivalence_problems(_EQ_SOURCE, compiled, without, files) == [
        "services.vault-agent.environment.ENV_SUFFIX: only in the compiled file"
    ]


def test_equivalence_allows_only_the_release_template_injection_in_the_base() -> None:
    """An app sidecar takes the release template as `${VAULT_SECRETS_TEMPLATE:-}` (#1822
    Slice 3). Only that exact form is dropped; any other base value is a difference."""
    compiled, base, files = _eq_case()
    marker = "      ENV_SUFFIX: ${ENV_SUFFIX:-}\n"
    assert marker in base
    injected = base.replace(
        marker, marker + "      VAULT_SECRETS_TEMPLATE: ${VAULT_SECRETS_TEMPLATE:-}\n", 1
    )
    assert equivalence_problems(_EQ_SOURCE, compiled, injected, files) == []
    fixed = base.replace(marker, marker + "      VAULT_SECRETS_TEMPLATE: abc\n", 1)
    assert equivalence_problems(_EQ_SOURCE, compiled, fixed, files) == [
        "services.vault-agent.environment.VAULT_SECRETS_TEMPLATE: only in the base file"
    ]


def test_equivalence_finds_an_env_that_the_source_drops() -> None:
    compiled, base, files = _eq_case()
    source = _EQ_SOURCE.replace('      A: "1"\n', "")
    dropped = compile_stack(source, vault_template=_EQ_TEMPLATE)
    assert equivalence_problems(source, dropped, base, files) == [
        "services.app.environment.A: only in the base file"
    ]


def test_equivalence_reports_a_changed_label_template_config_or_command() -> None:
    compiled, base, files = _eq_case()
    label = base.replace("priority=100", "priority=99")
    assert label != base
    assert any(
        "labels" in p for p in equivalence_problems(_EQ_SOURCE, compiled, label, files)
    )
    other = dict(files, **{"secrets.ctmpl": _EQ_TEMPLATE + "X=1\n"})
    assert equivalence_problems(_EQ_SOURCE, compiled, base, other) == [
        "vault-agent: the compiled template differs from the base secrets.ctmpl"
    ]
    hcl = dict(files, **{"vault-agent.hcl": str(files["vault-agent.hcl"]) + "x = 1\n"})
    assert equivalence_problems(_EQ_SOURCE, compiled, base, hcl) == [
        "vault-agent: the compiled config differs from the base vault-agent.hcl"
    ]
    missing: dict[str, str | None] = {"secrets.ctmpl": None, "vault-agent.hcl": None}
    assert equivalence_problems(_EQ_SOURCE, compiled, base, missing) == [
        "vault-agent: the base ref has no secrets.ctmpl to compare",
        "vault-agent: the base ref has no vault-agent.hcl to compare",
    ]
    cmd = base.replace("exec run --now", "exec run --later")
    assert cmd != base
    assert "does not end in" in " ".join(
        equivalence_problems(_EQ_SOURCE, compiled, cmd, files)
    )


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, timeout=60)


def _commit(root: Path) -> None:
    _git(root, "add", ".")
    _git(
        root,
        "-c",
        "user.email=t@example.test",
        "-c",
        "user.name=t",
        "commit",
        "-qm",
        "base",
    )


def test_check_compares_a_migrated_file_with_its_base_ref(tmp_path: Path) -> None:
    synth = "platform/99.synth/compose.yaml"
    source = _x_stack(environment={"A": "1", "B": "2"})
    stack = tmp_path / synth
    stack.parent.mkdir(parents=True)
    # The hand-written base is the compiled text, committed as a plain file.
    stack.write_text(compile_stack(source), encoding="utf-8")
    (tmp_path / "platform/21.portal").mkdir(parents=True)
    (tmp_path / "platform/21.portal/compose.yaml").write_bytes(
        (ROOT / "platform/21.portal/compose.yaml").read_bytes()
    )
    _git(tmp_path, "init", "-q")
    _commit(tmp_path)
    base = check("HEAD", root=tmp_path)
    assert (base.files, base.plain_identical, base.x_infra2, base.problems) == (
        2,
        2,
        0,
        [],
    )
    stack.write_text(source, encoding="utf-8")
    report = check("HEAD", root=tmp_path)
    assert (report.files, report.plain_identical, report.x_infra2, report.compared) == (
        2,
        1,
        1,
        1,
    )
    assert report.problems == []
    # A source that drops an env value fails the check.
    stack.write_text(_x_stack(environment={"A": "1"}), encoding="utf-8")
    assert check("HEAD", root=tmp_path).problems == [
        f"{synth}: services.app.environment.B: only in the base file"
    ]
    # A file that is x-infra2 at the base too has no hand-written base to compare.
    _commit(tmp_path)
    again = check("HEAD", root=tmp_path)
    assert (again.x_infra2, again.compared, again.problems) == (1, 0, [])


def test_check_on_this_tree_proves_every_plain_file_and_fails_on_an_empty_scan(
    monkeypatch,
) -> None:
    report = check("HEAD")
    assert report.problems == []
    assert report.files >= 20
    assert report.x_infra2 == len(X_INFRA2_FILES)
    assert report.plain_identical == report.files - report.x_infra2
    import common.deploy.compose_equivalence as eq

    monkeypatch.setattr(eq, "tracked_compose_files", lambda root=ROOT: [])
    assert eq.check("HEAD").problems == [
        "no compose file was checked; the scan is empty"
    ]


def test_check_reports_a_plain_file_that_the_compile_changes(monkeypatch) -> None:
    import common.deploy.compose_equivalence as eq

    monkeypatch.setattr(eq, "compile_stack", lambda text, **k: text + "\n")
    report = eq.check("HEAD")
    plain = [p for p in report.problems if not p.startswith(tuple(X_INFRA2_FILES))]
    assert len(plain) == report.files - len(X_INFRA2_FILES) >= 20
    assert all("a plain file changed in the compile" in p for p in plain)


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
