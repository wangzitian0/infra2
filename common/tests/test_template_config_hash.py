"""A template rendered to a `${DATA_PATH}` host mount must change the config hash (#1438).

Before the fix, `sync` printed "config unchanged, skipping deploy" after a change to
`platform/11.signoz/otel-collector-config.yaml`, and the host kept the old file.
"""

from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path

import pytest

from common.deploy import deploy_guard_audit
from common.deploy.config_hash import (
    _artifact_items_from_disk,
    _compose_artifact_files,
    compose_mount_fingerprint_gaps,
    config_hash_from_items,
    missing_mount_fingerprints,
    mount_fingerprint_env,
    mount_fingerprint_var,
)

ROOT = Path(__file__).resolve().parents[2]

# service directory -> template file that deploy.py renders to a ${DATA_PATH} host file
TEMPLATE_SERVICES = {
    "platform/03.clickhouse": "config.xml",
    "platform/11.signoz": "otel-collector-config.yaml",
    "platform/21.portal": "config.yml.tmpl",
    "platform/25.free": "config.json.tmpl",
}

COMPOSE = (
    "services:\n"
    "  app:\n"
    "    image: x\n"
    "    labels:\n"
    '      - "iac.mount-sha256.app.cfg=${IAC_MOUNT_SHA256_APP_CFG:-}"\n'
    "    volumes:\n"
    "      - ${DATA_PATH}/app.cfg:/etc/app.cfg:ro\n"
    "      - ${DATA_PATH}/data:/var/lib/app\n"
)


def _hash(compose_path: Path) -> str:
    text = compose_path.read_text(encoding="utf-8")
    return config_hash_from_items(
        text, {"A": "1"}, _artifact_items_from_disk(str(compose_path), text), []
    )


def _service(tmp_path: Path, template: str = "app.cfg.tmpl") -> Path:
    service_dir = tmp_path / "platform" / "99.ghost"
    service_dir.mkdir(parents=True)
    compose = service_dir / "compose.yaml"
    compose.write_text(COMPOSE, encoding="utf-8")
    if template:
        (service_dir / template).write_text("listen: {{PORT}}\n", encoding="utf-8")
    return compose


@pytest.mark.parametrize("template_name", ["app.cfg", "app.cfg.tmpl"])
def test_template_edit_changes_config_hash(tmp_path: Path, template_name: str) -> None:
    compose = _service(tmp_path, template_name)
    before = _hash(compose)
    (compose.parent / template_name).write_text("listen: {{PORT}}\nnew: 1\n")
    assert _hash(compose) != before


def test_data_directory_mount_is_not_a_hash_input(tmp_path: Path) -> None:
    compose = _service(tmp_path)
    (compose.parent / "data").mkdir()
    (compose.parent / "data" / "runtime.db").write_text("x")
    names = {p.name for p in _compose_artifact_files(str(compose), COMPOSE)}
    assert names == {"app.cfg.tmpl"}


@pytest.mark.parametrize("service_dir,template", sorted(TEMPLATE_SERVICES.items()))
def test_real_service_hashes_its_template(service_dir: str, template: str) -> None:
    compose = ROOT / service_dir / "compose.yaml"
    files = _compose_artifact_files(str(compose), compose.read_text(encoding="utf-8"))
    assert (ROOT / service_dir / template).resolve() in files


def test_guard_passes_on_real_tree() -> None:
    composes = deploy_guard_audit.find_service_composes()
    assert deploy_guard_audit.host_mount_violations(composes) == []
    assert deploy_guard_audit.audit() == []


def test_guard_names_service_that_has_no_template(tmp_path: Path) -> None:
    compose = _service(tmp_path, template="")
    violations = deploy_guard_audit.host_mount_violations([compose], root=tmp_path)
    assert len(violations) == 1
    assert "platform/99.ghost/compose.yaml" in violations[0]
    assert "'app.cfg'" in violations[0]


def test_guard_ignores_directory_mounts(tmp_path: Path) -> None:
    compose = _service(tmp_path)
    assert deploy_guard_audit.host_mount_violations([compose], root=tmp_path) == []


def _load_deploy_module(service_dir: str):
    spec = importlib.util.spec_from_file_location(
        f"deploy_{service_dir.split('.')[-1]}", ROOT / service_dir / "deploy.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# service directory -> (service, host file, deployer class) for the fingerprint tests
FINGERPRINT_SERVICES = {
    "platform/03.clickhouse": ("clickhouse", "config.xml", "ClickHouseDeployer"),
    "platform/11.signoz": (
        "otel-collector",
        "otel-collector-config.yaml",
        "SigNozDeployer",
    ),
    "platform/21.portal": ("portal", "config.yml", "PortalDeployer"),
    "platform/25.free": ("free", "config.json", "FreeDeployer"),
}


def test_template_edit_changes_config_hash_of_real_service(tmp_path: Path) -> None:
    """Hash flips on a template-only change of a real service (49497fcd behaviour)."""
    import shutil

    copy = tmp_path / "platform" / "21.portal"
    shutil.copytree(ROOT / "platform/21.portal", copy)
    compose = copy / "compose.yaml"
    before = _hash(compose)
    template = copy / "config.yml.tmpl"
    template.write_text(template.read_text(encoding="utf-8") + "\n# edit\n")
    assert _hash(compose) != before


def test_fingerprint_var_is_derived_from_file_name() -> None:
    assert mount_fingerprint_var("config.xml") == "IAC_MOUNT_SHA256_CONFIG_XML"
    assert (
        mount_fingerprint_var("otel-collector-config.yaml")
        == "IAC_MOUNT_SHA256_OTEL_COLLECTOR_CONFIG_YAML"
    )


def test_fingerprint_changes_only_with_content() -> None:
    a = mount_fingerprint_env({"a.cfg": "x", "b.cfg": "y"})
    assert a == mount_fingerprint_env({"a.cfg": b"x", "b.cfg": b"y"})
    b = mount_fingerprint_env({"a.cfg": "x2", "b.cfg": "y"})
    assert b["IAC_MOUNT_SHA256_A_CFG"] != a["IAC_MOUNT_SHA256_A_CFG"]
    assert b["IAC_MOUNT_SHA256_B_CFG"] == a["IAC_MOUNT_SHA256_B_CFG"]


def test_guard_names_service_without_fingerprint_label(tmp_path: Path) -> None:
    compose = _service(tmp_path)
    compose.write_text(
        COMPOSE.replace(
            '      - "iac.mount-sha256.app.cfg=${IAC_MOUNT_SHA256_APP_CFG:-}"\n', ""
        ).replace("    labels:\n", ""),
        encoding="utf-8",
    )
    violations = deploy_guard_audit.host_mount_violations([compose], root=tmp_path)
    assert len(violations) == 1
    assert "service 'app'" in violations[0]
    assert "IAC_MOUNT_SHA256_APP_CFG" in violations[0]


def test_guard_accepts_fingerprint_label(tmp_path: Path) -> None:
    compose = _service(tmp_path)
    assert compose_mount_fingerprint_gaps(COMPOSE) == []
    assert deploy_guard_audit.host_mount_violations([compose], root=tmp_path) == []


@pytest.mark.parametrize("service_dir", sorted(FINGERPRINT_SERVICES))
def test_real_service_carries_fingerprint_label(service_dir: str) -> None:
    service, name, _cls = FINGERPRINT_SERVICES[service_dir]
    text = (ROOT / service_dir / "compose.yaml").read_text(encoding="utf-8")
    gaps = compose_mount_fingerprint_gaps(text)
    assert (service, name) not in gaps


@pytest.mark.parametrize("service_dir", sorted(FINGERPRINT_SERVICES))
def test_guard_names_real_service_when_label_removed(
    tmp_path: Path, service_dir: str
) -> None:
    """The unfixed compose (no fingerprint label) fails the guard, naming its service."""
    service, name, _cls = FINGERPRINT_SERVICES[service_dir]
    var = mount_fingerprint_var(name)
    lines = (
        (ROOT / service_dir / "compose.yaml").read_text(encoding="utf-8").splitlines()
    )
    kept = [ln for ln in lines if var not in ln]
    assert len(kept) < len(lines)
    target = tmp_path / service_dir
    target.mkdir(parents=True)
    compose = target / "compose.yaml"
    compose.write_text("\n".join(kept) + "\n", encoding="utf-8")
    (target / "users.xml").write_text("<x/>")
    for tmpl in (name, f"{name}.tmpl"):
        if (ROOT / service_dir / tmpl).is_file():
            (target / tmpl).write_text("x")
    violations = deploy_guard_audit.host_mount_violations([compose], root=tmp_path)
    assert any(f"service {service!r}" in v and var in v for v in violations), violations


def test_missing_fingerprint_is_reported() -> None:
    assert missing_mount_fingerprints("platform/21.portal/compose.yaml", {}) == [
        "IAC_MOUNT_SHA256_CONFIG_YML"
    ]
    assert (
        missing_mount_fingerprints(
            "platform/21.portal/compose.yaml", {"IAC_MOUNT_SHA256_CONFIG_YML": "abc"}
        )
        == []
    )


def test_deployer_composing_refuses_missing_fingerprint(monkeypatch) -> None:
    """Base `Deployer.composing` fails closed before any Dokploy call."""
    from common.deploy.deployer import Deployer

    class Ghost(Deployer):
        service = "ghost"
        compose_path = "platform/21.portal/compose.yaml"

        @classmethod
        def env(cls):
            return {"ENV": "x", "ENV_NAME": "production"}

    with pytest.raises(ValueError, match="IAC_MOUNT_SHA256_CONFIG_YML"):
        Ghost.composing(None, {})


@pytest.mark.parametrize("service_dir", sorted(FINGERPRINT_SERVICES))
def test_composing_delivers_config_and_sets_fingerprint(
    monkeypatch, service_dir: str
) -> None:
    """`sync` runs composing but never pre_compose: composing delivers the file and
    passes the sha256 of the delivered bytes to compose as a variable."""
    from common.deploy.deployer import Deployer

    _svc, name, cls_name = FINGERPRINT_SERVICES[service_dir]
    cls = getattr(_load_deploy_module(service_dir), cls_name)
    seen: dict = {}
    calls: list[str] = []
    monkeypatch.setattr(
        cls,
        "env",
        classmethod(
            lambda _c: {
                "ENV": "x",
                "INTERNAL_DOMAIN": "example.test",
                "VPS_HOST": "h",
            }
        ),
    )

    def fake_super(_c, _ctx, env_vars):
        seen.update(env_vars)
        calls.append("c")
        return "id"

    monkeypatch.setattr(Deployer, "composing", classmethod(fake_super))
    rendered = {"v": "rendered-" + name}
    for attr in ("_deliver_config", "_deliver_collector_config"):
        if hasattr(cls, attr):
            monkeypatch.setattr(
                cls, attr, classmethod(lambda _c, *_a: calls.append("d") or True)
            )
    for attr in ("_render_config", "_render_collector_config"):
        if hasattr(cls, attr):
            monkeypatch.setattr(cls, attr, classmethod(lambda _c, *_a: rendered["v"]))
    if hasattr(cls, "_ensure_remote_config"):
        monkeypatch.setattr(
            cls,
            "_ensure_remote_config",
            classmethod(lambda _c, _ctx: calls.append("d") or ("uuid", "/api/v1/p")),
        )

    assert cls.composing(None, {}) == "id"
    assert calls == ["d", "c"]
    assert seen == {
        **seen,
        **mount_fingerprint_env({name: rendered["v"]}),
    }
    assert seen[mount_fingerprint_var(name)]


FREE_UUID = "123e4567-e89b-12d3-a456-426614174000"
FREE_PATH = "/api/v1/abc123"


class _Result:
    ok = True

    def __init__(self, stdout: str = "") -> None:
        self.stdout = stdout


class _FakeContext:
    def __init__(self, host_text: str, ok: bool = True, raises: bool = False) -> None:
        self.host_text, self.ok, self.raises = host_text, ok, raises
        self.commands: list[str] = []

    def run(self, cmd, *_a, **_k):
        self.commands.append(cmd)
        if self.raises:
            raise OSError("ssh: connection reset")
        result = _Result(self.host_text)
        result.ok = self.ok
        return result


def _free_ensure(monkeypatch, ctx):
    """Run `_ensure_remote_config`; return (result, host writes, logged errors)."""
    module = _load_deploy_module("platform/25.free")
    cls = module.FreeDeployer
    uploads: list[str] = []
    errors: list[str] = []
    monkeypatch.setattr(
        cls,
        "env",
        classmethod(
            lambda _c: {"ENV": "production", "VPS_HOST": "h", "VPS_SSH_USER": "root"}
        ),
    )
    monkeypatch.setenv("FREE_UUID", FREE_UUID)
    monkeypatch.setenv("FREE_PATH", FREE_PATH)
    monkeypatch.setattr(
        module,
        "run_with_status",
        lambda _c, cmd, _d: uploads.append(cmd) or _Result(),
    )
    monkeypatch.setattr(module, "error", lambda msg, *_: errors.append(msg))
    return cls._ensure_remote_config(ctx), uploads, errors


def _free_upload_count(monkeypatch, host_text: str) -> int:
    result, uploads, _ = _free_ensure(monkeypatch, _FakeContext(host_text))
    assert result == (FREE_UUID, FREE_PATH)
    return sum(1 for cmd in uploads if cmd.startswith("scp "))


def test_free_uploads_template_change_when_uuid_and_path_are_stable(
    monkeypatch,
) -> None:
    module = _load_deploy_module("platform/25.free")
    current = module.FreeDeployer._render_config(FREE_UUID, FREE_PATH)
    assert _free_upload_count(monkeypatch, current) == 0
    assert _free_upload_count(monkeypatch, current.replace('"warn"', '"info"')) == 1


# --- #1449: only a confirmed absent host file may create a uuid and path -------------

ABSENT = "__IAC_ABSENT__"  # printed by the remote read command for a missing file
CONFIG_PATH = "/data/platform/free/config.json"


@pytest.mark.parametrize(
    "ctx",
    [
        _FakeContext("", ok=False),
        _FakeContext("", ok=True),
        _FakeContext(ABSENT, ok=False),
        _FakeContext("", raises=True),
    ],
    ids=["nonzero-exit", "empty-output", "failed-with-sentinel", "raises"],
)
def test_free_failed_read_stops_without_new_values_or_write(monkeypatch, ctx) -> None:
    result, writes, errors = _free_ensure(monkeypatch, ctx)
    assert result is None
    assert writes == []
    assert any(CONFIG_PATH in message for message in errors)


def test_free_confirmed_absent_file_creates_values_and_writes(monkeypatch) -> None:
    ctx = _FakeContext(ABSENT + "\n")
    result, writes, _ = _free_ensure(monkeypatch, ctx)
    assert ABSENT in ctx.commands[0] and CONFIG_PATH in ctx.commands[0]
    assert result == (FREE_UUID, FREE_PATH)
    assert [cmd.split()[0] for cmd in writes] == ["scp", "ssh"]


def test_free_existing_file_keeps_its_uuid_and_path(monkeypatch) -> None:
    module = _load_deploy_module("platform/25.free")
    old_uuid, old_path = "223e4567-e89b-12d3-a456-426614174999", "/api/v1/kept"
    host = module.FreeDeployer._render_config(old_uuid, old_path)
    result, writes, _ = _free_ensure(monkeypatch, _FakeContext(host))
    assert result == (old_uuid, old_path)  # the env FREE_UUID/FREE_PATH lose
    assert [cmd.split()[0] for cmd in writes] == ["ssh"]  # permissions only, no scp


@pytest.mark.parametrize("host", ["{not json", '{"inbounds": []}'])
def test_free_malformed_existing_file_is_not_overwritten(monkeypatch, host) -> None:
    result, writes, _ = _free_ensure(monkeypatch, _FakeContext(host))
    assert (result, writes) == (None, [])


def test_free_client_info_reports_a_failed_read(monkeypatch) -> None:
    module = _load_deploy_module("platform/25.free")
    cls, errors = module.FreeDeployer, []
    env = {"ENV": "production", "VPS_HOST": "h", "INTERNAL_DOMAIN": "x.test"}
    monkeypatch.setattr(cls, "env", classmethod(lambda _c: env))
    monkeypatch.setattr(module, "error", lambda msg, *_: errors.append(msg))
    cls.print_client_info(_FakeContext("", ok=False))
    assert any(CONFIG_PATH in message for message in errors)


# --- review findings of PR #1447 (head 7762f85) -------------------------------------


def test_changing_a_dependency_module_flips_the_signoz_hash(tmp_path: Path) -> None:
    from common.deploy.config_hash import (
        _dependency_items_from_globs,
        config_hash_from_items,
    )
    from common.deploy.dependencies import extra_dependency_globs

    compose = ROOT / "platform/11.signoz/compose.yaml"
    text = compose.read_text(encoding="utf-8")
    items = _artifact_items_from_disk(str(compose), text)
    deps = _dependency_items_from_globs(extra_dependency_globs("platform/signoz"))
    base = config_hash_from_items(text, {}, items, deps)
    changed = [
        (label, content + b"\n# edit" if label.endswith("env_config.py") else content)
        for label, content in deps
    ]
    assert any(label.endswith("env_config.py") for label, _ in deps)
    assert config_hash_from_items(text, {}, items, changed) != base


# --- guard hardening ----------------------------------------------------------------


def _compose_with(volume: str, label_var: str = "IAC_MOUNT_SHA256_C_YML") -> str:
    return (
        "services:\n"
        "  app:\n"
        "    image: x\n"
        "    labels:\n"
        f'      - "iac.mount-sha256.c.yml=${{{label_var}:-}}"\n'
        "    volumes:\n" + volume
    )


@pytest.mark.parametrize(
    "volume",
    [
        "      - ${DATA_PATH:-/data/x}/c.yml:/etc/c.yml:ro\n",
        "      - ${CONF_DIR}/c.yml:/etc/c.yml:ro\n",
        "      - type: bind\n        source: ${DATA_PATH}/c.yml\n        target: /etc/c.yml\n",
        "      - type: bind\n        source: ${DATA_PATH:-/d}/c.yml\n        target: /etc/c.yml\n",
    ],
)
def test_mount_forms_are_detected(volume: str) -> None:
    from common.deploy.config_hash import compose_host_mount_files

    assert compose_host_mount_files(_compose_with(volume)) == ["c.yml"]


@pytest.mark.parametrize(
    "volume",
    [
        "      - ${DATA_PATH}/data:/var/lib/app\n",
        "      - ${X:-./conf}/c.yml:/etc/c.yml:ro\n",
        "      - ./c.yml:/etc/c.yml:ro\n",
        "      - ${DATA_PATH}/htpasswd:/etc/htpasswd:ro\n",
    ],
)
def test_non_rendered_mount_forms_are_ignored(volume: str) -> None:
    """Directories, repo-relative paths and suffix-less names are out of scope."""
    from common.deploy.config_hash import compose_host_mount_files

    assert compose_host_mount_files(_compose_with(volume)) == []


@pytest.mark.parametrize(
    "volume",
    [
        "      - ${DATA_PATH:-/data/x}/c.yml:/etc/c.yml:ro\n",
        "      - type: bind\n        source: ${DATA_PATH}/c.yml\n        target: /etc/c.yml\n",
    ],
)
def test_guard_names_mount_form_without_template(tmp_path: Path, volume: str) -> None:
    service_dir = tmp_path / "platform" / "98.forms"
    service_dir.mkdir(parents=True)
    compose = service_dir / "compose.yaml"
    compose.write_text(_compose_with(volume), encoding="utf-8")
    violations = deploy_guard_audit.host_mount_violations([compose], root=tmp_path)
    assert any("'c.yml'" in v and "no config-hash input" in v for v in violations)
    (service_dir / "c.yml.tmpl").write_text("x")
    assert deploy_guard_audit.host_mount_violations([compose], root=tmp_path) == []


def test_label_with_a_longer_variable_name_does_not_count(tmp_path: Path) -> None:
    text = _compose_with(
        "      - ${DATA_PATH}/c.yml:/etc/c.yml:ro\n",
        label_var="IAC_MOUNT_SHA256_C_YML_OLD",
    )
    assert compose_mount_fingerprint_gaps(text) == [("app", "c.yml")]
    ok = _compose_with("      - ${DATA_PATH}/c.yml:/etc/c.yml:ro\n")
    assert compose_mount_fingerprint_gaps(ok) == []


@pytest.mark.parametrize("content", ["services: [", "- not a mapping\n", "x: 1\n"])
def test_guard_fails_on_compose_it_cannot_read(tmp_path: Path, content: str) -> None:
    service_dir = tmp_path / "platform" / "97.broken"
    service_dir.mkdir(parents=True)
    compose = service_dir / "compose.yaml"
    compose.write_text(content, encoding="utf-8")
    violations = deploy_guard_audit.host_mount_violations([compose], root=tmp_path)
    assert len(violations) == 1
    assert "read nothing" in violations[0]


def test_composing_check_raises_on_unparsable_compose(tmp_path: Path) -> None:
    broken = tmp_path / "compose.yaml"
    broken.write_text("services: [", encoding="utf-8")
    with pytest.raises(ValueError):
        missing_mount_fingerprints(str(broken), {})
    assert missing_mount_fingerprints(str(tmp_path / "absent.yaml"), {}) == []


def test_guard_rejects_host_file_under_another_variable(tmp_path: Path) -> None:
    """Only `${DATA_PATH}` names a template; a template beside it does not clear `${CONF_DIR}`."""
    service_dir = tmp_path / "platform" / "96.other"
    service_dir.mkdir(parents=True)
    compose = service_dir / "compose.yaml"
    compose.write_text(
        _compose_with("      - ${CONF_DIR}/c.yml:/etc/c.yml:ro\n"), encoding="utf-8"
    )
    (service_dir / "c.yml.tmpl").write_text("x")
    violations = deploy_guard_audit.host_mount_violations([compose], root=tmp_path)
    assert any("'c.yml'" in v and "no config-hash input" in v for v in violations)


@pytest.mark.parametrize("service_dir", sorted(FINGERPRINT_SERVICES))
def test_composing_names_service_and_template_when_render_fails(
    monkeypatch, service_dir: str
) -> None:
    """A render that returns None (missing template) gives an explicit error."""
    from common.deploy.deployer import Deployer

    service, name, cls_name = FINGERPRINT_SERVICES[service_dir]
    cls = getattr(_load_deploy_module(service_dir), cls_name)
    monkeypatch.setattr(cls, "env", classmethod(lambda _c: {"ENV": "x"}))
    for attr in ("_deliver_config", "_deliver_collector_config"):
        if hasattr(cls, attr):
            monkeypatch.setattr(cls, attr, classmethod(lambda _c, *_a: True))
    for attr in ("_render_config", "_render_collector_config"):
        if hasattr(cls, attr):
            monkeypatch.setattr(cls, attr, classmethod(lambda _c, *_a: None))
    if hasattr(cls, "_ensure_remote_config"):
        monkeypatch.setattr(
            cls,
            "_ensure_remote_config",
            classmethod(lambda _c, _ctx: ("uuid", "/api/v1/p")),
        )
    monkeypatch.setattr(Deployer, "composing", classmethod(lambda *_a: "id"))
    with pytest.raises(RuntimeError) as err:
        cls.composing(None, {})
    message = str(err.value)
    assert cls.service in message
    assert f"{service_dir}/{name}" in message


def test_mount_fingerprint_env_rejects_none_content() -> None:
    with pytest.raises(RuntimeError, match=r"svc: cannot render host file 'a.cfg'"):
        mount_fingerprint_env({"a.cfg": None}, service="svc", template_dir="d")


def test_mount_fingerprint_env_accepts_empty_string_content() -> None:
    assert mount_fingerprint_env({"a.cfg": ""}) == {
        "IAC_MOUNT_SHA256_A_CFG": hashlib.sha256(b"").hexdigest()
    }
