"""The paths that handle an ``x-infra2`` stack (infra2#1822 Slice 1, part 2).

No stack uses ``x-infra2`` yet. Each test writes a synthetic ``x-infra2`` file. The
realistic fixture is the real ``platform/30.todo/compose.yaml`` written as ``x-infra2``.
No test calls Dokploy, SSH or the network: each client is a fake that records calls.
"""

from __future__ import annotations

import dataclasses
import hashlib
import sys
import os
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
            "composePath": "./docker-compose.yml",
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
    """Each ref pins the SDK of this checkout unless its files give another uv.lock."""
    import common.deploy.config_drift as drift

    lock = (ROOT / "uv.lock").read_bytes()

    def contents_at_ref(ref, paths):
        files = {"uv.lock": lock, **files_by_ref[ref]}
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


#: A version that no release pins; built so no file names a real SDK pin.
OTHER_SDK = ".".join(["0", "0", "1"])
_WHEEL_STEM = "infra2" + "_sdk-"


def _lock_for(version: str) -> bytes:
    url = f"https://example.test/download/{_WHEEL_STEM}{version}-py3-none-any.whl"
    return (
        '[[package]]\nname = "infra2-sdk"\n'
        f'version = "{version}"\nsource = {{ url = "{url}" }}\n'
        "dependencies = []\nwheels = [\n"
        f'    {{ url = "{url}", hash = "sha256:{"a" * 64}" }},\n]\n'
    ).encode()


def test_sdk_at_ref_reads_the_infra2_sdk_pin_of_uv_lock() -> None:
    import common.deploy.config_drift as drift

    version, url, sha256 = drift.sdk_at_ref("HEAD")
    assert version == drift.installed_sdk_version()
    assert url.endswith(f"/v{version}/infra2_sdk-{version}-py3-none-any.whl")
    lock = (ROOT / "uv.lock").read_text(encoding="utf-8")
    assert f'{{ url = "{url}", hash = "sha256:{sha256}" }}' in lock


def test_sdk_at_ref_reads_the_wheel_hash_of_a_lock_block(monkeypatch) -> None:
    import common.deploy.config_drift as drift

    monkeypatch.setattr(
        drift, "contents_at_ref", lambda ref, paths: {"uv.lock": _lock_for(OTHER_SDK)}
    )
    assert drift.sdk_at_ref("v") == (
        OTHER_SDK,
        f"https://example.test/download/{_WHEEL_STEM}{OTHER_SDK}-py3-none-any.whl",
        "a" * 64,
    )
    # A lock block without the wheel hash is not usable: None, so the guard fails closed.
    no_hash = _lock_for(OTHER_SDK).split(b"wheels")[0]
    monkeypatch.setattr(
        drift, "contents_at_ref", lambda ref, paths: {"uv.lock": no_hash}
    )
    assert drift.sdk_at_ref("v") is None


def test_config_drift_does_not_compile_an_x_infra2_ref_with_another_sdk(
    monkeypatch,
) -> None:
    """Another SDK can compile other bytes: a false DRIFT. The compare stops instead."""
    source = _todo_source()
    manifest = (ROOT / TODO_MANIFEST).read_bytes()
    drift = _drift_at(
        monkeypatch,
        {
            "v-other-sdk": {
                TODO: source.encode(),
                TODO_MANIFEST: manifest,
                "uv.lock": _lock_for(OTHER_SDK),
            },
            "v-no-lock": {TODO: source.encode(), TODO_MANIFEST: manifest},
        },
    )
    with pytest.raises(ComposeSourceError, match=f"pins infra2-sdk {OTHER_SDK}"):
        drift.expected_hash_at(object, MagicMock(), "v-other-sdk", {})
    # A plain compose does not use the compiler: the SDK does not matter.
    plain = "services:\n  a:\n    image: x\n"
    assert drift.compiled_at_ref(TODO, plain, "v-other-sdk") == plain


def test_the_release_run_imports_the_sdk_that_the_release_pins(
    monkeypatch, tmp_path: Path
) -> None:
    import common.deploy.config_drift as drift

    seen: dict = {}

    def run_at_ref(ref, args, *, root, timeout, env):
        seen["env"] = env
        return types.SimpleNamespace(
            returncode=0,
            stdout='REF_HASHES {"version": "v1", "services": {}}',
            stderr="",
        )

    monkeypatch.setenv("PYTHONPATH", "/inherited")
    monkeypatch.setattr(drift, "_on_main", lambda commit, root: True)
    monkeypatch.setattr(drift, "run_at_ref", run_at_ref)
    monkeypatch.setattr(drift, "sdk_at_ref", lambda ref: (OTHER_SDK, "u", "h"))
    monkeypatch.setattr(
        drift, "_sdk_dir_for", lambda version, url, sha256: tmp_path / version
    )
    drift.hashes_with_ref_code("abc", ["platform/todo"])
    # The release SDK goes first; an inherited path stays after it.
    assert seen["env"]["PYTHONPATH"] == f"{tmp_path / OTHER_SDK}{os.pathsep}/inherited"
    # The same SDK as this process: nothing to put first.
    monkeypatch.setattr(
        drift, "sdk_at_ref", lambda ref: (drift.installed_sdk_version(), "u", "h")
    )
    drift.hashes_with_ref_code("abc", ["platform/todo"])
    assert seen["env"]["PYTHONPATH"] == "/inherited"


def test_the_release_run_goes_on_when_the_release_sdk_is_unavailable(
    monkeypatch, capsys
) -> None:
    """A plain stack does not need the SDK: a failed download must not stop its run."""
    import common.deploy.config_drift as drift

    seen: dict = {}

    def run_at_ref(ref, args, *, root, timeout, env):
        seen["env"] = env
        return types.SimpleNamespace(
            returncode=0,
            stdout='REF_HASHES {"version": "v1", "services": {"platform/redis": {"hash": "h1", "missing": []}}}',
            stderr="",
        )

    def unavailable(version, url, sha256):
        raise OSError("network is unreachable")

    monkeypatch.delenv("PYTHONPATH", raising=False)
    monkeypatch.setattr(drift, "_on_main", lambda commit, root: True)
    monkeypatch.setattr(drift, "run_at_ref", run_at_ref)
    monkeypatch.setattr(drift, "sdk_at_ref", lambda ref: (OTHER_SDK, "u", "h"))
    monkeypatch.setattr(drift, "_sdk_dir_for", unavailable)
    result = drift.hashes_with_ref_code("abc", ["platform/redis"])
    assert result["services"]["platform/redis"]["hash"] == "h1"
    assert "PYTHONPATH" not in seen["env"]
    assert "network is unreachable" in capsys.readouterr().err


def test_sdk_dir_for_unpacks_a_pure_python_wheel_once(monkeypatch) -> None:
    import io
    import zipfile

    import common.deploy.config_drift as drift

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as wheel:
        wheel.writestr("infra2_sdk/__init__.py", "MARK = 1\n")
        wheel.writestr(f"{_WHEEL_STEM}{OTHER_SDK}.dist-info/METADATA", "Name: x\n")
    fetched: list[str] = []
    monkeypatch.setattr(drift, "_SDK_DIRS", {})
    monkeypatch.setattr(
        drift, "_fetch", lambda url: fetched.append(url) or buf.getvalue()
    )
    url = f"https://x/{_WHEEL_STEM}{OTHER_SDK}-py3-none-any.whl"
    good = hashlib.sha256(buf.getvalue()).hexdigest()
    with pytest.raises(RuntimeError, match="uv.lock records"):
        drift._sdk_dir_for(OTHER_SDK, url, "0" * 64)
    assert drift._SDK_DIRS == {}
    target = drift._sdk_dir_for(OTHER_SDK, url, good)
    assert (target / "infra2_sdk/__init__.py").read_text() == "MARK = 1\n"
    assert drift._sdk_dir_for(OTHER_SDK, url, good) == target
    assert fetched == [url, url]
    with pytest.raises(RuntimeError, match="not a pure-Python wheel"):
        drift._sdk_dir_for(
            "other", f"https://x/{_WHEEL_STEM}other-cp312-macosx.whl", good
        )


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

    # A plain stack, with a template that the registry renderer cannot give.
    plain = "services:\n  vault-agent:\n    image: hashicorp/vault:1.15\n"
    _repo_with(monkeypatch, tmp_path, TODO, plain)
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


def test_remote_template_identity_reads_a_compiled_sidecar_and_drops_the_printf_newline(
    monkeypatch,
) -> None:
    template = _template()
    # The sidecar writes the file with `printf '%s\n' "$VAULT_SECRETS_TEMPLATE"`.
    audit, commands = _fake_ssh(monkeypatch, "compiled\n" + template + "\n")
    got = audit._remote_template_identity("vps", "c-1")
    assert got == ("compiled", hashlib.sha256(template.encode()).hexdigest())
    assert len(commands) == 1 and commands[0].startswith("sh -c ")
    assert "infra2.vault.template.sha256" in commands[0]
    # A file without the printf newline is not what the sidecar wrote: unreadable.
    audit, _ = _fake_ssh(monkeypatch, "compiled\n" + template.rstrip("\n"))
    assert audit._remote_template_identity("vps", "c-1") == ("compiled", "")


def test_remote_template_identity_reads_a_hand_written_sidecar(monkeypatch) -> None:
    audit, _ = _fake_ssh(monkeypatch, "hand-written\nabc123\n")
    assert audit._remote_template_identity("vps", "c-1") == ("hand-written", "abc123")
    # No template file, or an SSH failure: the form is unknown.
    audit, _ = _fake_ssh(monkeypatch, "")
    assert audit._remote_template_identity("vps", "c-1") == ("", "")


_FAKE_DOCKER = """#!/bin/sh
# inspect prints the label; exec runs the rest of the command on this host.
[ -n "$FAKE_MISSING" ] && exit 1
if [ "$1" = inspect ]; then
  printf '%s\\n' "$FAKE_LABEL"; exit 0
fi
shift 2
exec "$@"
"""


@pytest.mark.parametrize(
    ("label", "files", "expected_form"),
    [
        # A compiled sidecar: the label is set.
        ("a" * 64, ("compiled",), "compiled"),
        # A hand-written sidecar that writes /vault/secrets.ctmpl from
        # VAULT_SECRETS_TEMPLATE and also mounts /etc/vault/secrets.ctmpl: the path
        # does not make it compiled; the missing label makes it hand-written.
        ("", ("compiled", "mounted"), "hand-written"),
        ("<no value>", ("mounted",), "hand-written"),
    ],
)
def test_remote_template_identity_script_takes_the_form_from_the_label(
    tmp_path: Path, monkeypatch, label: str, files: tuple[str, ...], expected_form: str
) -> None:
    """Run the real shell script with a fake docker: the label gives the form."""
    import subprocess

    import common.security.vault_self_refresh_audit as audit

    template = _template()
    paths = {
        "compiled": tmp_path / "vault/secrets.ctmpl",
        "mounted": tmp_path / "etc/vault/secrets.ctmpl",
    }
    for name in files:
        paths[name].parent.mkdir(parents=True, exist_ok=True)
        paths[name].write_text(template + "\n", encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(_FAKE_DOCKER, encoding="utf-8")
    docker.chmod(0o755)
    monkeypatch.setattr(audit, "COMPILED_TEMPLATE_PATH", str(paths["compiled"]))
    monkeypatch.setattr(audit, "HAND_WRITTEN_TEMPLATE_PATH", str(paths["mounted"]))
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "FAKE_LABEL": label}

    def ssh(host, command):
        return subprocess.run(
            command, shell=True, capture_output=True, text=True, env=env
        )

    monkeypatch.setattr(audit, "_ssh", ssh)
    form, sha = audit._remote_template_identity("vps", "c-1")
    assert form == expected_form
    if expected_form == "compiled":
        assert sha == hashlib.sha256(template.encode()).hexdigest()
    else:
        assert sha == hashlib.sha256((template + "\n").encode()).hexdigest()


def test_remote_template_identity_script_is_unknown_without_the_file_or_container(
    tmp_path: Path, monkeypatch
) -> None:
    import subprocess

    import common.security.vault_self_refresh_audit as audit

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(_FAKE_DOCKER, encoding="utf-8")
    docker.chmod(0o755)
    monkeypatch.setattr(audit, "COMPILED_TEMPLATE_PATH", str(tmp_path / "none-c"))
    monkeypatch.setattr(audit, "HAND_WRITTEN_TEMPLATE_PATH", str(tmp_path / "none-m"))
    for extra in ({"FAKE_LABEL": ""}, {"FAKE_LABEL": "a" * 64}, {"FAKE_MISSING": "1"}):
        env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", **extra}

        def ssh(host, command, _env=env):
            return subprocess.run(
                command, shell=True, capture_output=True, text=True, env=_env
            )

        monkeypatch.setattr(audit, "_ssh", ssh)
        assert audit._remote_template_identity("vps", "c-1") == ("", ""), extra


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
        return types.SimpleNamespace(
            returncode=0, stdout="compiled\n" + _template() + "\n", stderr=""
        )

    monkeypatch.setattr(audit, "_ssh", ssh)
    service = _vault_service(TODO)
    obs = audit.collect_live_observations([service], env="production")
    observed = obs["services"]["platform/todo"]
    deployed = observed["deployed_template_sha256"]
    assert observed["deployed_template_form"] == "compiled"
    assert len(seen) == 1
    assert seen[0].startswith("sh -c ")
    assert "docker exec platform-todo-vault-agent cat " in seen[0]
    result = va.classify_deployed_template(service, deployed, "compiled")
    assert result.status == "pass", result
    # The message names the compiled compose, not the secrets.ctmpl that is not deployed.
    assert result.summary == (
        "the template in the vault-agent container matches the compiled "
        f"{TODO} template"
    )


def test_the_audit_fails_a_hand_written_sidecar_when_the_release_is_compiled(
    monkeypatch, tmp_path: Path
) -> None:
    """A rollback runs the hand-written form while main ships x-infra2: a fail, not info."""
    import common.security.vault_audit as va

    _repo_with(monkeypatch, tmp_path, TODO, _todo_source())
    monkeypatch.setattr(va, "REPO_ROOT", tmp_path)
    service = _vault_service(TODO)
    result = va.classify_deployed_template(service, "a" * 64, "hand-written")
    assert result.status == "fail" and result.severity == "P1", result
    assert "hand-written template" in result.summary
    assert result.evidence["release_form"] == "compiled"
    # The same container in the release form compares by hash, as before.
    same_form = va.classify_deployed_template(service, "a" * 64, "compiled")
    assert same_form.status == "fail" and "never reached" in same_form.summary
    assert "hand-written" not in same_form.summary


_HAND_WRITTEN_COMPOSE = "services:\n  vault-agent:\n    image: vault\n"
_HAND_WRITTEN_TEMPLATE = '{{ with secret "x" }}A={{ .Data.data.a }}{{ end }}\n'


def _git(root: Path, *args: str) -> None:
    import subprocess

    subprocess.run(
        ["git", "-c", "user.email=t@example.test", "-c", "user.name=t", *args],
        cwd=root,
        check=True,
        capture_output=True,
    )


def _served_repo(monkeypatch, tmp_path: Path, *, served_compiled: bool) -> str:
    """A git repo whose tag holds the served stack and whose disk holds x-infra2 main."""
    import common.deploy.config_drift as drift
    import common.security.vault_audit as va

    _git(tmp_path, "init", "-q")
    _repo_with(
        monkeypatch,
        tmp_path,
        TODO,
        _todo_source() if served_compiled else _HAND_WRITTEN_COMPOSE,
    )
    (tmp_path / TODO_DIR / "secrets.ctmpl").write_text(
        _HAND_WRITTEN_TEMPLATE, encoding="utf-8"
    )
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "served")
    _git(tmp_path, "tag", "production/v1.0.0")
    # Main moves on: the stack becomes x-infra2 and the template file goes away.
    (tmp_path / TODO_DIR / "secrets.ctmpl").unlink()
    (tmp_path / TODO).write_text(_todo_source(), encoding="utf-8")
    monkeypatch.setattr(drift, "ROOT", tmp_path)
    monkeypatch.setattr(va, "REPO_ROOT", tmp_path)
    return "production/v1.0.0"


def _x_infra2_service():
    """The service as main derives it for an x-infra2 stack: the compose is the source."""
    return dataclasses.replace(
        _vault_service(TODO), secret_template_path=TODO, vault_agent_config_path=TODO
    )


def test_served_template_reads_the_stack_at_the_served_tag(
    monkeypatch, tmp_path: Path
) -> None:
    import common.security.vault_audit as va

    ref = _served_repo(monkeypatch, tmp_path, served_compiled=False)
    service = _x_infra2_service()
    form, sha, source = va._served_template(service, ref)
    assert form == "hand-written"
    assert sha == hashlib.sha256(_HAND_WRITTEN_TEMPLATE.encode()).hexdigest()
    assert source == f"{TODO_DIR}/secrets.ctmpl"
    assert va._served_template(service, "production/v9.9.9") is None


def test_the_audit_compares_a_lagging_container_with_the_served_release(
    monkeypatch, tmp_path: Path
) -> None:
    """Main ships x-infra2; production still serves the hand-written tag."""
    import common.security.vault_audit as va

    ref = _served_repo(monkeypatch, tmp_path, served_compiled=False)
    service = _x_infra2_service()
    served_sha = hashlib.sha256(_HAND_WRITTEN_TEMPLATE.encode()).hexdigest()

    same = va.classify_deployed_template(service, served_sha, "hand-written", ref)
    assert (same.status, same.severity) == ("pass", "P1"), same
    assert ref in same.summary and same.evidence["served_ref"] == ref

    other = va.classify_deployed_template(service, "b" * 64, "hand-written", ref)
    assert (other.status, other.severity) == ("fail", "P1"), other
    assert ref in other.summary

    # An unreadable container hash is not lag: the form mismatch stays a fail.
    unreadable = va.classify_deployed_template(service, "", "hand-written", ref)
    assert (unreadable.status, unreadable.severity) == ("fail", "P1"), unreadable

    # Without a served release, the form mismatch stays a fail.
    unknown = va.classify_deployed_template(service, served_sha, "hand-written", "")
    assert (unknown.status, unknown.severity) == ("fail", "P1"), unknown
    assert "never reached" in unknown.summary


def test_the_audit_fails_a_container_that_matches_neither_main_nor_the_served_release(
    monkeypatch, tmp_path: Path
) -> None:
    """The served tag is x-infra2 too, so a hand-written container is a rollback."""
    import common.security.vault_audit as va

    ref = _served_repo(monkeypatch, tmp_path, served_compiled=True)
    service = _x_infra2_service()
    served_sha = hashlib.sha256(_HAND_WRITTEN_TEMPLATE.encode()).hexdigest()
    result = va.classify_deployed_template(service, served_sha, "hand-written", ref)
    assert (result.status, result.severity) == ("fail", "P1"), result
    assert "never reached" in result.summary


def test_the_audit_compares_a_hand_written_container_with_the_served_template(
    monkeypatch, tmp_path: Path
) -> None:
    """Both forms are hand-written; main changed the template after the served tag."""
    import common.deploy.config_drift as drift
    import common.security.vault_audit as va

    _git(tmp_path, "init", "-q")
    _repo_with(monkeypatch, tmp_path, TODO, _HAND_WRITTEN_COMPOSE)
    template = tmp_path / TODO_DIR / "secrets.ctmpl"
    template.write_text(_HAND_WRITTEN_TEMPLATE, encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "served")
    _git(tmp_path, "tag", "production/v1.0.0")
    template.write_text(_HAND_WRITTEN_TEMPLATE + "B=1\n", encoding="utf-8")
    monkeypatch.setattr(drift, "ROOT", tmp_path)
    monkeypatch.setattr(va, "REPO_ROOT", tmp_path)
    service = _vault_service(TODO)
    served_sha = hashlib.sha256(_HAND_WRITTEN_TEMPLATE.encode()).hexdigest()
    ref = "production/v1.0.0"

    lagging = va.classify_deployed_template(service, served_sha, "hand-written", ref)
    assert (lagging.status, lagging.severity) == ("pass", "P1"), lagging
    assert ref in lagging.summary

    neither = va.classify_deployed_template(service, "c" * 64, "hand-written", ref)
    assert (neither.status, neither.severity) == ("fail", "P1"), neither

    # Without a served release, main's template stays the only reference.
    unknown = va.classify_deployed_template(service, served_sha, "hand-written", "")
    assert (unknown.status, unknown.severity) == ("fail", "P1"), unknown


def test_a_compiled_served_release_does_not_soften_a_compiled_mismatch(
    monkeypatch, tmp_path: Path
) -> None:
    """The served tag is compiled, so its hash is unknown: a stale container fails."""
    import common.security.vault_audit as va

    ref = _served_repo(monkeypatch, tmp_path, served_compiled=True)
    service = _x_infra2_service()
    result = va.classify_deployed_template(service, "d" * 64, "compiled", ref)
    assert (result.status, result.severity) == ("fail", "P1"), result
    assert "never reached" in result.summary
    assert ref not in result.summary


def test_served_ref_is_the_production_marker_only_for_production(monkeypatch) -> None:
    import common.deploy.config_drift as drift
    import common.security.vault_audit as va

    monkeypatch.setattr(drift, "_production_target_tag", lambda: "production/v1.2.3")
    assert va._served_ref("production") == "production/v1.2.3"
    assert va._served_ref("staging") == ""

    def no_marker():
        raise SystemExit("no production/v* marker found")

    monkeypatch.setattr(drift, "_production_target_tag", no_marker)
    assert va._served_ref("production") == ""


def test_audit_from_observations_passes_the_served_ref(monkeypatch) -> None:
    import common.security.vault_audit as va

    seen: list[str] = []
    monkeypatch.setattr(va, "_served_ref", lambda env: f"served-for-{env}")

    def classify(service, sha, form, served_ref):
        seen.append(served_ref)
        return va._result(service, "deployed-template", "pass", "P1", "ok", {})

    monkeypatch.setattr(va, "classify_deployed_template", classify)
    va.audit_from_observations(
        [_vault_service(TODO)], {"services": {}}, env="production", now=0
    )
    assert seen == ["served-for-production"]


def test_the_audit_fails_a_compiled_release_without_its_template_label(
    monkeypatch, tmp_path: Path
) -> None:
    import common.security.vault_audit as va
    import common.security.vault_self_refresh_audit as audit

    _repo_with(monkeypatch, tmp_path, TODO, _todo_source())
    monkeypatch.setattr(va, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(audit, "_release_template_sha256", lambda _service: "")
    service = _vault_service(TODO)
    result = va.classify_deployed_template(service, "a" * 64, "compiled")
    assert result.status == "fail" and result.severity == "P1", result
    assert "infra2.vault.template.sha256" in result.summary


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
        "composePath": "./docker-compose.yml",
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
