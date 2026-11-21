"""The paths that handle an ``x-infra2`` stack (infra2#1822 Slice 1, part 2).

No stack uses ``x-infra2`` yet. Each test writes a synthetic ``x-infra2`` file. The
realistic fixture is the real ``platform/30.todo/compose.yaml`` written as ``x-infra2``.
No test calls Dokploy, SSH or the network: each client is a fake that records calls.
"""

from __future__ import annotations

import dataclasses
import hashlib
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

import common.deploy.compose_compile as cc
from common.deploy.compose_compile import (
    ComposeSourceError,
    compile_stack,
    load_stack,
    vault_template_for,
    vault_template_label,
)
from common.deploy.config_hash import config_hash_from_items
from common.tests.conftest import load_repo_module
from common.tests.test_compose_compile import (
    TODO_HAND_WRITTEN,
    _todo_source,
    _x_stack,
)

ROOT = Path(__file__).resolve().parents[2]
TODO_DIR = "platform/30.todo"
TODO = f"{TODO_DIR}/compose.yaml"
TODO_MANIFEST = f"{TODO_DIR}/env.manifest.json"
FULL_SHA = "1af32e6daf17e2c58383dd2c0bfaea13bc11e517"


def _todo_with_preview() -> str:
    """The x-infra2 todo with an ephemeral preview database (an ``x-preview`` block)."""
    doc = yaml.safe_load(_todo_source())
    doc["x-infra2"]["preview"] = {"db_container": "todo-preview-db${ENV_SUFFIX}"}
    doc["services"]["todo"]["x-preview"] = {"database_url": "postgresql"}
    return yaml.safe_dump(doc, sort_keys=False, width=10**6)


def _repo_with(monkeypatch, tmp_path: Path, rel: str, text: str) -> Path:
    """Write ``text`` at ``rel`` under a fake checkout root that the compiler reads."""
    target = tmp_path / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    monkeypatch.setattr(cc, "REPO_ROOT", tmp_path)
    return target


def _template() -> str:
    """The Vault template of the todo stack, by the renderer that the deploy uses."""
    return vault_template_for(TODO, _todo_source())


# --- 1. preview ------------------------------------------------------------------------


class _PreviewClient:
    """Records the preview calls. It has no GitHub provider: a raw slot needs none."""

    def __init__(self, github_id=None):
        self.github_id = github_id
        self.github_reads = 0
        self.created: list[dict] = []
        self.updated: list[dict] = []
        self.deployed: list[str] = []

    def get_github_provider_id(self):
        self.github_reads += 1
        return self.github_id

    def ensure_environment(self, project, env_name, description=""):
        return {"environmentId": "env-preview"}, False

    def find_compose_by_name(self, name, project_name=None, env_name=None):
        if name == "app":
            return {"composeId": "cmp-source-app"}
        return None

    def get_compose_env(self, compose_id):
        return "VAULT_ADDR=https://vault.test\nVAULT_ROLE_ID=r\nVAULT_SECRET_ID=s\n"

    def create_compose(self, environment_id, name, **kwargs):
        self.created.append({"name": name, **kwargs})
        return {"composeId": "cmp-new"}

    def update_compose(self, compose_id, **kwargs):
        self.updated.append({"composeId": compose_id, **kwargs})
        return {}

    def update_compose_env(self, compose_id, env_vars=None, *, replace=False):
        return {}

    def deploy_compose(self, compose_id):
        self.deployed.append(compose_id)
        return {}


def _preview_up(monkeypatch, service: str, client, compose_path: str | None = None):
    from common.deploy import preview as pl

    monkeypatch.setattr(pl, "resolve_to_sha", lambda code, repo=None: FULL_SHA)
    if compose_path is not None:
        config = dataclasses.replace(
            pl.preview_service_config(service), compose_path=compose_path
        )
        monkeypatch.setattr(pl, "preview_service_config", lambda s: config)
    return pl.up(
        "pr",
        5,
        code="main",
        options=pl.PreviewOptions(service=service, wait=False),
        deps=pl.PreviewDeps(
            client=client, domain="example.test", repo="https://x/r.git"
        ),
    )


def test_preview_uploads_an_x_infra2_slot_compiled_in_preview_mode_as_raw(
    monkeypatch, tmp_path: Path
) -> None:
    source = _todo_with_preview()
    _repo_with(monkeypatch, tmp_path, TODO, source)
    client = _PreviewClient(github_id=None)
    result = _preview_up(monkeypatch, "platform/todo", client, compose_path=TODO)

    preview = compile_stack(source, vault_template=_template(), mode="preview")
    deploy = compile_stack(source, vault_template=_template(), mode="deploy")
    assert preview != deploy
    assert "db" in yaml.safe_load(preview)["services"]
    assert "db" not in yaml.safe_load(deploy)["services"]
    assert client.created == [
        {
            "name": "platform-todo-preview-pr-5",
            "app_name": "platform-todo-preview-pr-5",
            "source_type": "raw",
        }
    ]
    assert client.updated == [
        {
            "composeId": "cmp-new",
            "compose_file": preview,
            "source_type": "raw",
            "autoDeploy": False,
        }
    ]
    # A raw slot needs no GitHub provider; this client has none.
    assert client.github_reads == 0
    assert client.deployed == ["cmp-new"] and result.compose_id == "cmp-new"


def test_preview_stops_an_x_infra2_slot_that_is_not_raw_eligible_before_any_write(
    monkeypatch, tmp_path: Path
) -> None:
    _repo_with(monkeypatch, tmp_path, "s/compose.yaml", _x_stack(build="."))
    client = _PreviewClient(github_id="gh-1")
    with pytest.raises(ComposeSourceError, match="raw-eligible"):
        _preview_up(monkeypatch, "platform/todo", client, compose_path="s/compose.yaml")
    assert client.created == [] and client.updated == [] and client.deployed == []


@pytest.mark.parametrize(
    "service", ["finance_report/app", "truealpha/app", "platform/todo"]
)
def test_preview_keeps_the_github_source_and_the_bytes_of_every_plain_slot(
    monkeypatch, service: str
) -> None:
    from common.deploy.env_config import preview_service_config

    path = preview_service_config(service).compose_path
    text = (ROOT / path).read_text(encoding="utf-8")
    stack = load_stack(path, mode="preview")
    assert stack.compiled.encode() == text.encode() and stack.source_type == "github"

    client = _PreviewClient(github_id="gh-1")
    _preview_up(monkeypatch, service, client)
    assert client.created[0]["source_type"] == "github"
    assert client.updated == [
        {
            "composeId": "cmp-new",
            "source_type": "github",
            "githubId": "gh-1",
            "repository": "infra2",
            "owner": "wangzitian0",
            "branch": "main",
            "composePath": path,
            "autoDeploy": False,
        }
    ]
    assert client.github_reads == 1


# --- 2. config drift -------------------------------------------------------------------


def _old_manifest() -> bytes:
    """The todo manifest with another DATABASE_URL port: a release with another template."""
    text = (ROOT / TODO_MANIFEST).read_text(encoding="utf-8")
    assert text.count(":5432/postgres") == 1
    return text.replace(":5432/postgres", ":6543/postgres").encode()


def _drift_at(monkeypatch, files_by_ref: dict[str, dict[str, bytes]]):
    import common.deploy.config_drift as drift

    def contents_at_ref(ref, paths):
        files = files_by_ref[ref]
        return {p: files[p] for p in paths if p in files}

    monkeypatch.setattr(drift, "contents_at_ref", contents_at_ref)
    monkeypatch.setattr(drift, "_hash_input_paths", lambda dep, c: (TODO, [], []))
    return drift


def test_config_drift_compiles_an_x_infra2_stack_with_the_template_at_the_ref(
    monkeypatch,
) -> None:
    source = _todo_source()
    head = (ROOT / TODO_MANIFEST).read_bytes()
    drift = _drift_at(
        monkeypatch,
        {
            "v-head": {TODO: source.encode(), TODO_MANIFEST: head},
            "v-old": {TODO: source.encode(), TODO_MANIFEST: _old_manifest()},
        },
    )
    env = {"ENV": "production"}

    # At a ref with the manifest of this checkout: the hash of the deploy.
    got_head, missing = drift.expected_hash_at(object, MagicMock(), "v-head", env)
    deploy_compiled = load_stack_text(source)
    assert missing == []
    assert got_head == config_hash_from_items(deploy_compiled, env, [], [])

    # At a ref with another manifest: the template of that ref, not of this checkout.
    # The old manifest moves DATABASE_URL to port 6543; nothing else in the template moves.
    old_template = _template().replace(":5432/postgres", ":6543/postgres")
    assert old_template != _template()
    got_old, _ = drift.expected_hash_at(object, MagicMock(), "v-old", env)
    assert got_old == config_hash_from_items(
        compile_stack(source, vault_template=old_template), env, [], []
    )
    assert got_old != got_head


def load_stack_text(source: str) -> str:
    """The compiled text that the deploy uploads for the todo source."""
    return compile_stack(source, vault_template=_template())


def test_config_drift_stops_when_the_manifest_is_absent_at_the_ref(monkeypatch) -> None:
    drift = _drift_at(monkeypatch, {"v1": {TODO: _todo_source().encode()}})
    with pytest.raises(ComposeSourceError, match="absent at v1"):
        drift.expected_hash_at(object, MagicMock(), "v1", {"ENV": "production"})


def test_config_drift_hashes_a_plain_file_as_its_own_bytes(monkeypatch) -> None:
    plain = "services:\n  app:\n    image: a:1\n"
    drift = _drift_at(monkeypatch, {"v1": {TODO: plain.encode()}})
    got, missing = drift.expected_hash_at(object, MagicMock(), "v1", {"ENV": "p"})
    assert (got, missing) == (config_hash_from_items(plain, {"ENV": "p"}, [], []), [])
    assert drift.compiled_at_ref(TODO, plain, "v1") is plain


# --- 3. vault self-refresh audit -------------------------------------------------------


def _vault_service(compose_path: str):
    from common.security.vault_audit import VaultService

    directory = compose_path.rsplit("/", 1)[0]
    return VaultService(
        id="platform/todo",
        project="platform",
        dokploy_service="todo",
        compose_path=compose_path,
        vault_agent_config_path=f"{directory}/vault-agent.hcl",
        secret_template_path=f"{directory}/secrets.ctmpl",
        vault_path_template="secret/data/platform/{env}/todo",
        vault_agent_container="platform-todo-vault-agent${ENV_SUFFIX}",
        app_containers=("platform-todo${ENV_SUFFIX}",),
        auth_method="approle",
    )


def test_release_template_sha256_of_an_x_infra2_stack_is_the_label(
    monkeypatch, tmp_path: Path
) -> None:
    import common.security.vault_audit as va

    _repo_with(monkeypatch, tmp_path, TODO, _todo_source())
    # A stale secrets.ctmpl beside the source must not count: the label is the release.
    (tmp_path / TODO_DIR / "secrets.ctmpl").write_text("stale\n", encoding="utf-8")
    monkeypatch.setattr(va, "REPO_ROOT", tmp_path)
    want = hashlib.sha256(_template().encode()).hexdigest()
    assert vault_template_label(load_stack(TODO).compiled) == want
    assert va._release_template_sha256(_vault_service(TODO)) == want


def test_release_template_sha256_of_a_plain_stack_hashes_secrets_ctmpl(
    monkeypatch, tmp_path: Path
) -> None:
    import common.security.vault_audit as va

    # The hand-written todo, with a template that the registry renderer cannot give.
    _repo_with(
        monkeypatch, tmp_path, TODO, TODO_HAND_WRITTEN.read_text(encoding="utf-8")
    )
    (tmp_path / TODO_DIR / "secrets.ctmpl").write_text("plain\n", encoding="utf-8")
    monkeypatch.setattr(va, "REPO_ROOT", tmp_path)
    want = hashlib.sha256(b"plain\n").hexdigest()
    assert va._release_template_sha256(_vault_service(TODO)) == want


def _fake_ssh(monkeypatch, stdout: str):
    import common.security.vault_self_refresh_audit as audit

    commands: list[str] = []

    def fake(host, command):
        commands.append(command)
        return types.SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    monkeypatch.setattr(audit, "_ssh", fake)
    return audit, commands


def test_remote_template_sha256_reads_the_compiled_path_and_drops_the_printf_newline(
    monkeypatch,
) -> None:
    template = _template()
    # The sidecar writes the file with `printf '%s\n' "$VAULT_SECRETS_TEMPLATE"`.
    audit, commands = _fake_ssh(monkeypatch, template + "\n")
    got = audit._remote_template_sha256("vps", "c-1", compiled_sidecar=True)
    assert commands == ["docker exec c-1 cat /vault/secrets.ctmpl"]
    assert got == hashlib.sha256(template.encode()).hexdigest()
    # A file without the printf newline is not what the sidecar wrote: unreadable.
    audit, _ = _fake_ssh(monkeypatch, template.rstrip("\n"))
    assert audit._remote_template_sha256("vps", "c-1", compiled_sidecar=True) == ""


def test_remote_template_sha256_keeps_the_mounted_path_for_a_plain_stack(
    monkeypatch,
) -> None:
    audit, commands = _fake_ssh(monkeypatch, "abc123\n")
    assert audit._remote_template_sha256("vps", "c-1") == "abc123"
    assert commands == [
        "docker exec c-1 sh -lc 'sha256sum /etc/vault/secrets.ctmpl 2>/dev/null | cut -d\" \" -f1'"
    ]


def test_the_audit_passes_a_compiled_sidecar_that_runs_the_release_template(
    monkeypatch, tmp_path: Path
) -> None:
    import common.security.vault_audit as va
    import common.security.vault_self_refresh_audit as audit

    _repo_with(monkeypatch, tmp_path, TODO, _todo_source())
    monkeypatch.setattr(va, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(
        "common.core.environ.get_env",
        lambda env_name=None: {"VPS_HOST": "vps", "INTERNAL_DOMAIN": "example.test"},
    )
    client = MagicMock()
    client.find_compose_by_name.return_value = {"env": ""}
    monkeypatch.setattr(
        "common.deploy.dokploy_client.get_dokploy", lambda host=None: client
    )
    for name in (
        "_remote_container_state",
        "_remote_secret_file_state",
        "_remote_container_logs",
    ):
        monkeypatch.setattr(audit, name, lambda *a, **k: {})
    seen: list[str] = []

    def ssh(host, command):
        seen.append(command)
        return types.SimpleNamespace(returncode=0, stdout=_template() + "\n", stderr="")

    monkeypatch.setattr(audit, "_ssh", ssh)
    service = _vault_service(TODO)
    obs = audit.collect_live_observations([service], env="production")
    deployed = obs["services"]["platform/todo"]["deployed_template_sha256"]
    assert seen == ["docker exec platform-todo-vault-agent cat /vault/secrets.ctmpl"]
    result = va.classify_deployed_template(service, deployed)
    assert result.status == "pass", result
    # The message names the compiled compose, not the secrets.ctmpl that is not deployed.
    assert result.summary == (
        "the template in the vault-agent container matches the compiled "
        f"{TODO} template"
    )


# --- 4. the six stacks with their own composing() --------------------------------------


def _base_composing_patches(monkeypatch, dep, client) -> None:
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
    monkeypatch.setattr("common.deploy.dokploy_client.get_dokploy", lambda **k: client)
    monkeypatch.setattr(
        "common.deploy.dokploy_client.ensure_project", lambda *a, **k: ("p-1", "e-1")
    )


# Each override delivers a host file, then calls Deployer.composing (the #1870 path).
_PLATFORM_DELIVERY = {
    "platform/clickhouse": {
        "_deliver_config": lambda cls, c, e: True,
        "_render_config": lambda cls, e: "<x/>",
    },
    "platform/signoz": {
        "_deliver_collector_config": lambda cls, c, e: True,
        "_render_collector_config": lambda cls, e: "a: 1\n",
    },
    "platform/portal": {
        "_deliver_config": lambda cls, c, e: "/data/portal/config.yml",
        "_render_config": lambda cls, e: "a: 1\n",
    },
    "platform/free": {
        "_ensure_remote_config": lambda cls, c: ("uuid-1", "/p"),
        "_render_config": lambda cls, u, p: "{}",
    },
}


@pytest.mark.parametrize("service_id", sorted(_PLATFORM_DELIVERY))
def test_platform_stacks_with_their_own_composing_upload_x_infra2_as_raw(
    monkeypatch, tmp_path: Path, service_id: str
) -> None:
    import common.deploy.dokploy_adapter as adapter
    from common.deploy.deployer import Deployer, load_deployer_class

    dep = load_deployer_class(service_id)
    assert "composing" in vars(dep), f"{service_id} no longer overrides composing()"
    source = _x_stack()
    compose = tmp_path / "compose.yaml"
    compose.write_text(source, encoding="utf-8")
    monkeypatch.setattr(dep, "compose_path", str(compose))
    for name, fn in _PLATFORM_DELIVERY[service_id].items():
        monkeypatch.setattr(dep, name, classmethod(fn))
    module = sys.modules[dep.__module__]
    for helper in ("prepare_delivered_mounts", "mount_fingerprint_env"):
        if hasattr(module, helper):
            monkeypatch.setattr(module, helper, lambda *a, **k: {})
    client = MagicMock()
    client.find_compose_by_name.return_value = None
    _base_composing_patches(monkeypatch, dep, client)
    calls: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        adapter, "upsert_raw_compose", lambda *a, **k: calls.append(("raw", k)) or "c-1"
    )
    monkeypatch.setattr(
        adapter,
        "upsert_github_compose",
        lambda *a, **k: calls.append(("github", k)) or "c-1",
    )
    assert dep.composing(MagicMock(), {}) == "c-1"
    assert [kind for kind, _ in calls] == ["raw"]
    assert calls[0][1]["compose_file"] == compile_stack(source)
    client.get_github_provider_id.assert_not_called()
    assert dep.composing.__func__ is not Deployer.composing.__func__


_BOOTSTRAP = {
    "vault": (
        "bootstrap/05.vault/tasks.py",
        "VaultDeployer",
        "vault",
        "bootstrap-vault",
    ),
    "1password": (
        "bootstrap/04.1password/tasks.py",
        "OnePasswordDeployer",
        "1password-connect",
        "bootstrap-1password",
    ),
}


def _bootstrap(monkeypatch, tmp_path: Path, key: str, text: str | None, existing=None):
    """Run the composing() of one bootstrap stack with a recording Dokploy client."""
    rel, cls_name, _, _ = _BOOTSTRAP[key]
    module = load_repo_module(rel, f"_x_infra2_{key}_tasks")
    dep = getattr(module, cls_name)
    if hasattr(module, "prompt_action"):
        # The 1password fallback asks the operator to deploy by hand; tests do not wait.
        monkeypatch.setattr(module, "prompt_action", lambda *a, **k: None)
    if text is not None:
        compose = tmp_path / "compose.yaml"
        compose.write_text(text, encoding="utf-8")
        monkeypatch.setattr(dep, "compose_path", str(compose))
    client = MagicMock()
    client.get_github_provider_id.return_value = "gh-1"
    client.find_compose_by_name.return_value = existing
    client.create_compose.return_value = {"composeId": "c-new"}
    client.ensure_domains.return_value = {"created": 1, "skipped": 0}
    ensured: list[tuple] = []
    monkeypatch.setattr("common.deploy.dokploy_client.get_dokploy", lambda **k: client)
    monkeypatch.setattr(
        "common.deploy.dokploy_client.ensure_project",
        lambda *a, **k: ensured.append(a) or ("p-1", "e-1"),
    )
    monkeypatch.setattr(
        dep, "env", classmethod(lambda cls: {"INTERNAL_DOMAIN": "example.test"})
    )
    try:
        result = dep.composing(
            MagicMock(), {"INTERNAL_DOMAIN": "example.test", "A": "1", "B": None}
        )
    except ComposeSourceError as exc:
        result = exc
    return result, client, ensured, dep


@pytest.mark.parametrize("key", sorted(_BOOTSTRAP))
@pytest.mark.parametrize("existing", [None, {"composeId": "c-old"}])
def test_bootstrap_stacks_upload_x_infra2_as_raw(
    monkeypatch, tmp_path: Path, key: str, existing
) -> None:
    source = _x_stack()
    result, client, _, _ = _bootstrap(monkeypatch, tmp_path, key, source, existing)
    _, _, name, app_name = _BOOTSTRAP[key]
    compose_id = existing["composeId"] if existing else "c-new"
    assert result == (True if key == "1password" else compose_id)
    raw = {
        "compose_file": compile_stack(source),
        "source_type": "raw",
        "autoDeploy": False,
    }
    if existing:
        client.create_compose.assert_not_called()
    else:
        create = client.create_compose.call_args.kwargs
        assert create == {
            "environment_id": "e-1",
            "name": name,
            "app_name": app_name,
            "env": "INTERNAL_DOMAIN=example.test\nA=1",
            **raw,
        }
    update = client.update_compose.call_args
    assert update.args == (compose_id,)
    assert update.kwargs == {"env": "INTERNAL_DOMAIN=example.test\nA=1", **raw}
    client.get_github_provider_id.assert_not_called()
    client.deploy_compose.assert_called_once_with(compose_id)


@pytest.mark.parametrize("key", sorted(_BOOTSTRAP))
def test_bootstrap_stacks_stop_x_infra2_that_is_not_raw_eligible_before_any_api_call(
    monkeypatch, tmp_path: Path, key: str
) -> None:
    result, client, ensured, _ = _bootstrap(
        monkeypatch, tmp_path, key, _x_stack(build=".")
    )
    # vault raises; 1password reports the failure and falls back to the manual steps.
    if key == "vault":
        assert isinstance(result, ComposeSourceError)
    else:
        assert result is False
    assert ensured == [] and client.mock_calls == []


@pytest.mark.parametrize("key", sorted(_BOOTSTRAP))
def test_bootstrap_stacks_keep_the_github_calls_for_their_plain_file(
    monkeypatch, tmp_path: Path, key: str
) -> None:
    from unittest.mock import call

    result, client, _, dep = _bootstrap(monkeypatch, tmp_path, key, None)
    _, _, name, app_name = _BOOTSTRAP[key]
    assert load_stack(dep.compose_path).source_type == "github"
    github = {
        "source_type": "github",
        "githubId": "gh-1",
        "repository": "infra2",
        "owner": "wangzitian0",
        "branch": "main",
        "composePath": dep.compose_path,
    }
    domain = {
        "compose_id": "c-new",
        "desired_domains": [
            {
                "host": f"{dep.subdomain}.example.test",
                "port": dep.service_port,
                "https": True,
            }
        ],
        "service_name": dep.service_name,
    }
    lookup = call.find_compose_by_name(name, "bootstrap")
    order = (
        [lookup, call.get_github_provider_id()]
        if key == "1password"
        else [call.get_github_provider_id(), lookup]
    )
    assert client.mock_calls == [
        *order,
        call.create_compose(
            environment_id="e-1", name=name, app_name=app_name, **github
        ),
        call.update_compose("c-new", env="INTERNAL_DOMAIN=example.test\nA=1"),
        call.ensure_domains(**domain),
        call.deploy_compose("c-new"),
    ]
    assert result == (True if key == "1password" else "c-new")


# --- deploy: the in-service check reads the compiled services --------------------------


def test_the_in_service_check_expects_the_compiled_vault_sidecar(
    monkeypatch, tmp_path: Path
) -> None:
    from common.deploy.deployer import Deployer

    compose = _repo_with(monkeypatch, tmp_path, TODO, _todo_source())

    class _Todo(Deployer):
        service = "todo"
        compose_path = str(compose)

    seen: list[str] = []
    monkeypatch.setattr(
        "common.deploy.in_service.expected_running_services",
        lambda text: seen.append(text) or [],
    )
    assert _Todo._verify_containers_in_service(MagicMock(), MagicMock()) is None
    assert seen == [load_stack(TODO).compiled]
    assert "vault-agent" in yaml.safe_load(seen[0])["services"]


def test_upsert_raw_compose_names_the_app_when_asked() -> None:
    from common.deploy.dokploy_adapter import raw_source_fields, upsert_raw_compose

    client = MagicMock()
    client.create_compose.return_value = {"composeId": "c-1"}
    upsert_raw_compose(
        client,
        service_name="1password-connect",
        project_name="bootstrap",
        env_id="e-1",
        compose_file="services: {}\n",
        effective_env="A=1",
        existing=None,
        raw_env_str="A=1",
        app_name="bootstrap-1password",
    )
    create = client.create_compose.call_args.kwargs
    assert create["app_name"] == "bootstrap-1password"
    assert {k: create[k] for k in raw_source_fields("x")} == raw_source_fields(
        "services: {}\n"
    )
