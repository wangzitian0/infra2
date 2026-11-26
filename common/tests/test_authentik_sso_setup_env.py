"""Authentik SSO setup tasks are environment-specific and fail closed (#1807)."""

from __future__ import annotations

import importlib.util
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from invoke.exceptions import Exit

REPO = Path(__file__).resolve().parents[2]
SECRET = "tok-SECRET-0123456789-abcdefghijklmnop"
WD_FORMAT = '{{index .Config.Labels "com.docker.compose.project.working_dir"}}'
PROJECT_FORMAT = '{{index .Config.Labels "com.docker.compose.project"}}'


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, REPO / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def st():
    return _load("authentik_shared_under_test", "platform/10.authentik/shared_tasks.py")


def _env(suffix="-staging", name="staging"):
    return {
        "ENV": name,
        "ENV_SUFFIX": suffix,
        "ENV_DOMAIN_SUFFIX": suffix,
        "INTERNAL_DOMAIN": "example.test",
        "VPS_HOST": "203.0.113.1",
    }


NAMED = {"staging": _env("-staging", "staging"), "production": _env("", "production")}


def _patch(st, monkeypatch, secrets, vault="vault-admin", remote=None, process=None):
    """Patch the Vault and SSH seams. `process` is the process environment."""
    import common.security.store as store

    process_env = process if process is not None else NAMED["staging"]
    monkeypatch.setattr(
        st, "get_env", lambda name=None: process_env if name is None else NAMED[name]
    )
    if callable(secrets):
        monkeypatch.setattr(store, "get_secrets", secrets)
    else:
        monkeypatch.setattr(store, "get_secrets", lambda *a, **k: secrets)
    monkeypatch.setattr(st, "vault_token", lambda: vault)
    calls = []

    def fake_remote(*a, **k):
        calls.append(a)
        return remote if remote is not None else (0, "", "")

    monkeypatch.setattr(st, "_run_remote_script", fake_remote)
    return calls


@pytest.mark.parametrize("key", ["root_token", "api_token"])
def test_existing_token_returns_early_without_ssh(st, monkeypatch, capsys, key):
    calls = _patch(st, monkeypatch, {key: SECRET})
    assert st.create_root_token.body(SimpleNamespace()) is True
    assert calls == []
    out = capsys.readouterr()
    assert SECRET not in out.out + out.err


def test_force_runs_the_script_although_vault_holds_a_token(st, monkeypatch):
    calls = _patch(st, monkeypatch, {"root_token": SECRET})
    assert st.create_root_token.body(SimpleNamespace(), force=True) is True
    assert len(calls) == 1


def test_missing_vault_path_does_not_stop_a_first_run(st, monkeypatch):
    from common.security.store import VaultSecrets

    def missing(key):
        raise VaultSecrets.VaultSecretNotFoundError("missing")

    # First object serves the skip check; the second serves the check after the script.
    answers = iter([SimpleNamespace(get=missing), {"root_token": SECRET}])
    calls = _patch(st, monkeypatch, lambda *a, **k: next(answers))
    assert st.create_root_token.body(SimpleNamespace()) is True
    assert len(calls) == 1


def test_unreadable_vault_fails_closed_without_ssh(st, monkeypatch):
    from common.security.store import VaultSecrets

    def denied(key):
        raise VaultSecrets.VaultAuthError("denied")

    calls = _patch(st, monkeypatch, lambda *a, **k: SimpleNamespace(get=denied))
    with pytest.raises(Exit) as exc:
        st.create_root_token.body(SimpleNamespace())
    assert exc.value.code == 1
    assert calls == []


def test_missing_vault_token_fails_closed_without_ssh(st, monkeypatch):
    calls = _patch(st, monkeypatch, {}, vault=None)
    with pytest.raises(Exit) as exc:
        st.create_root_token.body(SimpleNamespace())
    assert exc.value.code == 1
    assert calls == []


@pytest.mark.parametrize("missing", ["INTERNAL_DOMAIN", "VPS_HOST"])
def test_missing_host_or_domain_fails_closed_without_ssh(st, monkeypatch, missing):
    process = {**NAMED["staging"], missing: None}
    calls = _patch(st, monkeypatch, {}, process=process)
    with pytest.raises(Exit) as exc:
        st.create_root_token.body(SimpleNamespace())
    assert exc.value.code == 1
    assert calls == []


def test_remote_failure_raises_exit_and_prints_no_token(st, monkeypatch, capsys):
    _patch(st, monkeypatch, {}, vault=SECRET, remote=(12, "", "FAIL: x"))
    with pytest.raises(Exit) as exc:
        st.create_root_token.body(SimpleNamespace())
    assert exc.value.code == 1
    text = "".join(capsys.readouterr())
    assert SECRET not in text
    assert "FAIL: x" in text and "exit 12" in text


def test_remote_stderr_shows_only_fail_lines(st, monkeypatch, capsys):
    """The token-init script prints the generated token to stderr when the Vault write fails."""
    remote_token = "AUTHENTIK-ROOT-TOKEN-" + "z" * 40
    stderr = (
        "Token was generated but not persisted:\n"
        f"  {remote_token}\n"
        "FAIL: 0 compose directories match platform-authentik-server\n"
    )
    _patch(st, monkeypatch, {}, remote=(1, "", stderr))
    with pytest.raises(Exit):
        st.create_root_token.body(SimpleNamespace())
    text = "".join(capsys.readouterr())
    assert remote_token not in text and "not persisted" not in text
    assert "0 compose directories match" in text


def test_fail_line_with_rich_markup_prints_literally(st, monkeypatch, capsys):
    _patch(st, monkeypatch, {}, remote=(12, "", "FAIL: odd [/bold] path\n"))
    with pytest.raises(Exit) as exc:
        st.create_root_token.body(SimpleNamespace())
    assert exc.value.code == 1
    assert "[/bold]" in "".join(capsys.readouterr())


def test_fail_text_is_cut_to_500_characters(st, monkeypatch, capsys):
    _patch(st, monkeypatch, {}, remote=(12, "", "FAIL: " + "A" * 2000 + "\n"))
    with pytest.raises(Exit):
        st.create_root_token.body(SimpleNamespace())
    text = "".join("".join(capsys.readouterr()).split())
    assert "A" * 400 in text and "A" * 600 not in text


@pytest.mark.parametrize("rc", [124, 255])
def test_ssh_level_failure_shows_a_fixed_reason_not_stderr(st, monkeypatch, capsys, rc):
    stderr = "Host key verification failed.\n" + SECRET + "\n"
    _patch(st, monkeypatch, {}, remote=(rc, "", stderr))
    with pytest.raises(Exit):
        st.create_root_token.body(SimpleNamespace())
    text = "".join(capsys.readouterr())
    assert f"exit {rc}" in text and st.SSH_FAILURES[rc] in text
    assert SECRET not in text and "Host key verification" not in text


def test_remote_user_comes_from_vps_ssh_user(st, monkeypatch):
    process = {**NAMED["staging"], "VPS_SSH_USER": "deployer"}
    seen = {}

    def fake_remote(*a, **k):
        seen.update(k)
        return (0, "", "")

    _patch(st, monkeypatch, {"root_token": SECRET}, process=process)
    monkeypatch.setattr(st, "_run_remote_script", fake_remote)
    assert st.create_root_token.body(SimpleNamespace(), force=True) is True
    assert seen == {"user": "deployer"}


def test_fail_marker_inside_a_line_is_not_shown(st, monkeypatch, capsys):
    stderr = f"  note FAIL: {SECRET}\nFAIL: real reason\n"
    _patch(st, monkeypatch, {}, remote=(12, "", stderr))
    with pytest.raises(Exit):
        st.create_root_token.body(SimpleNamespace())
    text = "".join(capsys.readouterr())
    assert "real reason" in text and SECRET not in text


def test_success_output_has_no_token_text(st, monkeypatch, capsys):
    answers = iter([{}, {"root_token": SECRET}])
    calls = _patch(st, monkeypatch, lambda *a, **k: next(answers))
    assert st.create_root_token.body(SimpleNamespace()) is True
    assert len(calls) == 1
    out = capsys.readouterr().out
    assert SECRET not in out and SECRET[:20] not in out


@pytest.mark.parametrize(
    "name,empty_suffix", [("staging", "-staging"), ("production", "''")]
)
def test_script_built_for_the_named_environment(st, monkeypatch, name, empty_suffix):
    calls = _patch(st, monkeypatch, {}, process=NAMED[name])
    with pytest.raises(Exit):
        # The check after the SSH call finds no token, so the task fails after the call.
        st.create_root_token.body(SimpleNamespace())
    assert len(calls) == 1
    host, script, token = calls[0]
    assert host == "203.0.113.1" and token == "vault-admin"
    lines = script.splitlines()
    assert f"ENV_NAME={name}" in lines
    assert f"ENV_SUFFIX={empty_suffix}" in lines
    assert 'SERVER="platform-authentik-server$ENV_SUFFIX"' in lines
    assert f"ENV={name} " in script


def test_stale_exported_suffix_fails_closed_before_any_io(st, monkeypatch):
    """ENV=production with an exported ENV_SUFFIX=-staging must not reach staging."""
    process = {**NAMED["production"], "ENV_SUFFIX": "-staging"}
    calls = _patch(st, monkeypatch, {}, process=process)
    with pytest.raises(Exit) as exc:
        st.create_root_token.body(SimpleNamespace())
    assert exc.value.code == 1
    assert calls == []


def _fake_docker(tmp_path, suffix, wd, project="proj-x", compose_rc=0):
    """Fake `docker`: answers inspect by exact format string, logs compose calls."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    log = tmp_path / "docker.log"
    envfile = tmp_path / "envfile.path"
    envcopy = tmp_path / "envfile.copy"
    (bin_dir / "docker").write_text(
        "#!/bin/sh\n"
        'if [ "$1" = inspect ]; then\n'
        f'  [ "$4" = "platform-authentik-server{suffix}" ] || exit 1\n'
        f"  case \"$3\" in '{WD_FORMAT}') printf %s '{wd}'; exit 0;; "
        f"'{PROJECT_FORMAT}') printf %s '{project}'; exit 0;; esac\n"
        "  exit 1\n"
        "fi\n"
        'prev=""\n'
        'for a in "$@"; do\n'
        f'  if [ "$prev" = --env-file ]; then echo "$a" > {envfile}; cat "$a" > {envcopy}; fi\n'
        '  prev="$a"\n'
        "done\n"
        'echo "$PWD ENV=$ENV ENV_SUFFIX=$ENV_SUFFIX VAULT_ADDR=$VAULT_ADDR $*" '
        f">> {log}\n"
        "cat > /dev/null\n"
        f"exit {compose_rc}\n"
    )
    (bin_dir / "docker").chmod(0o755)
    return bin_dir, log, envfile, envcopy


def _bash(script, bin_dir):
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "VAULT_INIT_TOKEN": "tok-in-envfile",
    }
    return subprocess.run(
        ["bash", "-s"],
        input=script,
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


def _run_script(st, tmp_path, suffix, wd_label, dirs, compose_rc=0, project="proj-x"):
    root = tmp_path / "compose"
    for d in dirs:
        (root / d / "code/platform/10.authentik").mkdir(parents=True)
    wd = wd_label.replace("ROOT", str(root))
    bin_dir, log, envfile, envcopy = _fake_docker(
        tmp_path, suffix, wd, project=project, compose_rc=compose_rc
    )
    script = st.build_remote_script(
        "staging" if suffix else "production", suffix, "example.test", str(root)
    )
    return _bash(script, bin_dir), log, envfile, envcopy


@pytest.mark.parametrize(
    "suffix,chosen",
    [("-staging", "platform-authentik-stg"), ("", "platform-authentik-prd")],
)
def test_script_runs_only_in_this_environments_directory(st, tmp_path, suffix, chosen):
    wd = f"ROOT/{chosen}/code/platform/10.authentik"
    res, log, envfile, envcopy = _run_script(
        st, tmp_path, suffix, wd, ["platform-authentik-stg", "platform-authentik-prd"]
    )
    assert res.returncode == 0, res.stderr
    lines = log.read_text().splitlines()
    assert len(lines) == 1
    line = lines[0]
    assert f"/{chosen}/code/platform/10.authentik " in line
    name = "staging" if suffix else "production"
    assert (
        f"ENV={name} ENV_SUFFIX={suffix} VAULT_ADDR=https://vault.example.test " in line
    )
    # Compose joins the running project, starts no dependency and passes the suffix
    # that the readiness wait of the token-init script needs.
    assert f" -p proj-x run --rm --no-deps -e ENV_SUFFIX={suffix} token-init" in line
    assert "VAULT_INIT_TOKEN=tok-in-envfile" in envcopy.read_text()
    assert not Path(envfile.read_text().strip()).exists()


@pytest.mark.parametrize(
    "suffix,chosen",
    [("-staging", "platform-authentik-stg"), ("", "platform-authentik-prd")],
)
def test_script_runs_in_the_raw_compose_directory(st, tmp_path, suffix, chosen):
    # A raw stack (#1822 step B) runs from `<app>/code`: Dokploy wrote
    # code/docker-compose.yml there and there is no checkout below it. Measured on
    # staging 2026-11-26: platform-s3-staging has working_dir
    # /etc/dokploy/compose/platform-minio-6kne1n/code.
    wd = f"ROOT/{chosen}/code"
    res, log, envfile, envcopy = _run_script(
        st, tmp_path, suffix, wd, ["platform-authentik-stg", "platform-authentik-prd"]
    )
    assert res.returncode == 0, res.stderr
    lines = log.read_text().splitlines()
    assert len(lines) == 1
    assert lines[0].startswith(f"{tmp_path}/compose/{chosen}/code ENV=")
    assert (
        f" -p proj-x run --rm --no-deps -e ENV_SUFFIX={suffix} token-init" in lines[0]
    )


def test_script_gives_compose_no_stdin(st, tmp_path):
    """Compose must not read the rest of the script from the shared stdin."""
    wd = "ROOT/platform-authentik-stg/code/platform/10.authentik"
    root = tmp_path / "compose"
    (root / "platform-authentik-stg/code/platform/10.authentik").mkdir(parents=True)
    bin_dir, log, *_ = _fake_docker(tmp_path, "-staging", wd.replace("ROOT", str(root)))
    script = st.build_remote_script("staging", "-staging", "example.test", str(root))
    res = _bash(script + "echo AFTER-COMPOSE\n", bin_dir)
    assert res.returncode == 0, res.stderr
    assert "AFTER-COMPOSE" in res.stdout


def test_script_stops_when_the_token_file_cannot_be_created(st, tmp_path):
    wd = "ROOT/platform-authentik-stg/code/platform/10.authentik"
    root = tmp_path / "compose"
    (root / "platform-authentik-stg/code/platform/10.authentik").mkdir(parents=True)
    bin_dir, log, *_ = _fake_docker(tmp_path, "-staging", wd.replace("ROOT", str(root)))
    (bin_dir / "mktemp").write_text("#!/bin/sh\nexit 1\n")
    (bin_dir / "mktemp").chmod(0o755)
    script = st.build_remote_script("staging", "-staging", "example.test", str(root))
    res = _bash(script, bin_dir)
    assert res.returncode != 0
    assert not log.exists()


def test_script_removes_the_token_file_when_compose_fails(st, tmp_path):
    wd = "ROOT/platform-authentik-stg/code/platform/10.authentik"
    res, log, envfile, envcopy = _run_script(
        st, tmp_path, "-staging", wd, ["platform-authentik-stg"], compose_rc=5
    )
    assert res.returncode == 5
    assert envcopy.exists()
    assert not Path(envfile.read_text().strip()).exists()


def test_script_fails_closed_on_zero_matching_directories(st, tmp_path):
    res, log, *_ = _run_script(
        st,
        tmp_path,
        "-staging",
        "ROOT/other/code/platform/10.authentik",
        ["platform-authentik-prd", "platform-authentik-stg"],
    )
    assert res.returncode == 12
    assert "expected 1" in res.stderr
    assert not log.exists()


def test_script_fails_closed_when_environment_container_missing(st, tmp_path):
    # The fake answers only for the production container name; staging is asked.
    root = tmp_path / "compose"
    (root / "platform-authentik-x/code/platform/10.authentik").mkdir(parents=True)
    bin_dir, log, *_ = _fake_docker(tmp_path, "", "/nowhere")
    script = st.build_remote_script("staging", "-staging", "example.test", str(root))
    res = _bash(script, bin_dir)
    assert res.returncode == 11
    assert "not found" in res.stderr
    assert not log.exists()


def test_script_fails_closed_when_container_has_no_project_label(st, tmp_path):
    wd = "ROOT/platform-authentik-stg/code/platform/10.authentik"
    res, log, *_ = _run_script(
        st, tmp_path, "-staging", wd, ["platform-authentik-stg"], project=""
    )
    assert res.returncode == 13
    assert "no compose project label" in res.stderr
    assert not log.exists()


def test_script_fails_closed_on_two_matching_directories(st, tmp_path):
    # Two checkouts resolve to one directory when one is a symlink alias of the other.
    root = tmp_path / "compose"
    real = root / "platform-authentik-a/code/platform/10.authentik"
    real.mkdir(parents=True)
    alias_dir = root / "platform-authentik-b/code/platform"
    alias_dir.mkdir(parents=True)
    (alias_dir / "10.authentik").symlink_to(real)
    bin_dir, log, *_ = _fake_docker(tmp_path, "-staging", str(real))
    script = st.build_remote_script("staging", "-staging", "example.test", str(root))
    res = _bash(script, bin_dir)
    assert res.returncode == 12
    assert not log.exists()


def test_ssh_script_is_noninteractive_and_bounded(monkeypatch):
    from common.deploy import vps_ssh

    seen = {}

    def fake_run(args, **kw):
        seen.update(args=args, **kw)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(vps_ssh.subprocess, "run", fake_run)
    vps_ssh.ssh_script("echo hi", "h", "root", "", 22, timeout=7)
    assert "BatchMode=yes" in seen["args"] and "ConnectTimeout=10" in seen["args"]
    assert seen["input"] == "echo hi" and seen["timeout"] == 7
    assert not any("echo hi" in a for a in seen["args"])


def test_ssh_script_checks_the_host_key(monkeypatch):
    """The script carries a Vault token: an unknown or changed host key must fail."""
    from common.deploy import vps_ssh

    seen = {}

    def fake_run(args, **kw):
        seen["args"] = args
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(vps_ssh.subprocess, "run", fake_run)
    vps_ssh.ssh_script("x", "h", "root", "")
    assert "StrictHostKeyChecking=yes" in seen["args"]
    assert "StrictHostKeyChecking=no" not in seen["args"]
    assert "UserKnownHostsFile=/dev/null" not in seen["args"]


def test_ssh_script_timeout_returns_nonzero(monkeypatch):
    from common.deploy import vps_ssh

    def boom(args, **kw):
        raise subprocess.TimeoutExpired(args, 1)

    monkeypatch.setattr(vps_ssh.subprocess, "run", boom)
    rc, _, err = vps_ssh.ssh_script("x", "h", "root", "", timeout=1)
    assert rc == 124 and "timed out" in err


def test_remote_runner_sends_the_token_on_stdin_only(st, monkeypatch):
    from common.deploy import vps_ssh

    seen = {}

    def fake_run(args, **kw):
        seen.update(args=args, **kw)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(vps_ssh.subprocess, "run", fake_run)
    monkeypatch.setattr(st, "_ssh_credentials", lambda: ("h", "root", "", 22))
    assert st._run_remote_script("203.0.113.1", "echo body", SECRET) == (0, "", "")
    assert SECRET in seen["input"] and "echo body" in seen["input"]
    assert SECRET not in " ".join(seen["args"])
    assert "echo body" not in " ".join(seen["args"])
    assert seen["args"][-2:] == ["root@203.0.113.1", "bash -s"]
    assert seen["timeout"] == st.REMOTE_TIMEOUT_SECONDS == 600


def test_remote_runner_passes_its_time_limit_to_ssh(st, monkeypatch):
    from common.deploy import vps_ssh

    seen = {}

    def fake_run(args, **kw):
        seen.update(kw)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(vps_ssh.subprocess, "run", fake_run)
    monkeypatch.setattr(st, "_ssh_credentials", lambda: ("h", "root", "", 22))
    monkeypatch.setattr(st, "REMOTE_TIMEOUT_SECONDS", 7)
    st._run_remote_script("203.0.113.1", "echo body", SECRET)
    assert seen["timeout"] == 7


def test_remote_runner_uses_the_given_user(st, monkeypatch):
    from common.deploy import vps_ssh

    seen = {}

    def fake_run(args, **kw):
        seen["args"] = args
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(vps_ssh.subprocess, "run", fake_run)
    monkeypatch.setattr(st, "_ssh_credentials", lambda: ("h", "root", "", 22))
    st._run_remote_script("203.0.113.1", "echo body", SECRET, user="deployer")
    assert seen["args"][-2] == "deployer@203.0.113.1"


def test_fail_closed_turns_false_into_exit(st):
    @st.fail_closed
    def bad():
        return False

    with pytest.raises(Exit) as exc:
        bad()
    assert exc.value.code == 1


@pytest.mark.parametrize(
    "name,args",
    [
        ("create_root_token", ()),
        ("create_proxy_app", ("Todo", "todo", "https://t.example.test", "todo-svc")),
        ("setup_admin_group", ()),
        ("list_apps", ()),
    ],
)
def test_authentik_task_without_a_root_token_exits_nonzero(st, monkeypatch, name, args):
    """Invoke exits 0 on a False return: each task must turn it into exit code 1."""
    _patch(st, monkeypatch, {}, vault=None)
    with pytest.raises(Exit) as exc:
        getattr(st, name).body(SimpleNamespace(), *args)
    assert exc.value.code == 1


class _Client:
    """Fake HTTP client: `get` and `post` take the answer or a list of answers."""

    def __init__(self, post=None, get=None, patch=None):
        self.posts = []
        self._post, self._get, self._patch = post, get, patch

    @staticmethod
    def _pick(answer, index):
        return answer[index] if isinstance(answer, list) else answer

    def post(self, url, json=None):
        self.posts.append(json)
        return self._pick(self._post, len(self.posts) - 1)

    def get(self, url):
        return self._get

    def patch(self, url, json=None):
        return self._patch


def _resp(status, body=None, text=""):
    return SimpleNamespace(status_code=status, json=lambda: body, text=text)


NO_BINDING = _resp(200, {"results": []})


@pytest.mark.parametrize(
    "post,expected",
    [
        (_resp(201), True),
        (_resp(400, text="Binding already exists"), True),
        (
            _resp(400, text="The fields policy, target, order must make a unique set."),
            True,
        ),
        (_resp(400, text="other"), False),
        (_resp(500), False),
    ],
)
def test_policy_binding_reports_failure(st, post, expected):
    client = _Client(post=post, get=NO_BINDING)
    assert (
        st._bind_policies_to_app(client, "https://x", "app", {"admins": "p"})
        is expected
    )


def test_existing_policy_binding_passes_without_a_post(st):
    client = _Client(post=_resp(500), get=_resp(200, {"results": [{"pk": "b"}]}))
    assert st._bind_policies_to_app(client, "https://x", "app", {"admins": "p"})
    assert client.posts == []


def test_failed_binding_lookup_does_not_count_as_existing(st):
    client = _Client(post=_resp(201), get=_resp(403, {"results": [{"pk": "b"}]}))
    assert st._bind_policies_to_app(client, "https://x", "app", {"admins": "p"})
    assert len(client.posts) == 1


def test_policy_binding_keeps_binding_the_other_groups_after_a_failure(st):
    client = _Client(post=[_resp(500), _resp(201)], get=NO_BINDING)
    ok = st._bind_policies_to_app(
        client, "https://x", "app", {"admins": "p1", "users": "p2"}
    )
    assert ok is False
    assert [x["policy"] for x in client.posts] == ["p1", "p2"]


@pytest.mark.parametrize(
    "get,patch,expected",
    [
        (_resp(200, {"results": [{"pk": "o", "providers": []}]}), _resp(200), True),
        (_resp(200, {"results": [{"pk": "o", "providers": []}]}), _resp(500), False),
        (_resp(200, {"results": [{"pk": "o", "providers": ["9"]}]}), None, True),
        (_resp(200, {"results": []}), None, False),
        (_resp(500), None, False),
        (_resp(403, {"results": [{"pk": "o", "providers": []}]}), _resp(200), False),
    ],
)
def test_outpost_configuration_reports_failure(st, get, patch, expected):
    client = _Client(get=get, patch=patch)
    assert st._configure_embedded_outpost(client, "https://x", "9") is expected


_PROXY_STUBS = {
    "_verify_authentik_auth": True,
    "_ensure_authentik_groups": {"admins": "g"},
    "_get_authentik_flows": ("a", "b"),
    "_ensure_access_policies": {"admins": "p"},
    "_ensure_proxy_provider": "9",
    "_ensure_application": ("todo", "pk"),
    "_bind_policies_to_app": True,
    "_configure_embedded_outpost": True,
}


def _run_proxy_app(st, monkeypatch, **overrides):
    _patch(st, monkeypatch, {"root_token": SECRET})
    for fn, value in {**_PROXY_STUBS, **overrides}.items():
        monkeypatch.setattr(st, fn, lambda *a, _v=value, **k: _v)
    return st.create_proxy_app.body(
        SimpleNamespace(), "Todo", "todo", "https://t.example.test", "todo-svc"
    )


def test_create_proxy_app_succeeds_when_every_step_succeeds(st, monkeypatch):
    assert _run_proxy_app(st, monkeypatch) is True


@pytest.mark.parametrize(
    "failing", ["_bind_policies_to_app", "_configure_embedded_outpost"]
)
def test_create_proxy_app_exits_nonzero_when_a_final_step_fails(
    st, monkeypatch, failing
):
    with pytest.raises(Exit) as exc:
        _run_proxy_app(st, monkeypatch, **{failing: False})
    assert exc.value.code == 1


@pytest.mark.parametrize(
    "rel", ["platform/30.todo/deploy.py", "platform/21.portal/deploy.py"]
)
@pytest.mark.parametrize("failing_step", [0, 1, 2])
def test_sso_setup_stops_and_exits_nonzero_on_failed_step(
    rel, failing_step, monkeypatch
):
    mod = _load("sso_under_test_" + rel.split("/")[1].replace(".", "_"), rel)
    commands = []

    def fake_run(c, cmd, desc, hide=True):
        commands.append(cmd.split()[1])
        return SimpleNamespace(ok=len(commands) - 1 != failing_step)

    monkeypatch.setattr(mod, "run_with_status", fake_run)
    deployer = next(
        v for k, v in vars(mod).items() if k.endswith("Deployer") and k != "Deployer"
    )
    monkeypatch.setattr(
        deployer, "env", classmethod(lambda cls: _env("", "production"))
    )
    with pytest.raises(Exit) as exc:
        mod.sso_setup.body(SimpleNamespace())
    assert exc.value.code == 1
    steps = [
        "authentik.shared.create-root-token",
        "authentik.shared.setup-admin-group",
        "authentik.shared.create-proxy-app",
    ]
    assert commands == steps[: failing_step + 1]


@pytest.mark.parametrize(
    "rel", ["platform/30.todo/deploy.py", "platform/21.portal/deploy.py"]
)
def test_sso_setup_exits_nonzero_without_internal_domain(rel, monkeypatch):
    mod = _load("sso_nodomain_" + rel.split("/")[1].replace(".", "_"), rel)
    commands = []
    monkeypatch.setattr(
        mod, "run_with_status", lambda *a, **k: commands.append(a) or None
    )
    deployer = next(
        v for k, v in vars(mod).items() if k.endswith("Deployer") and k != "Deployer"
    )
    env = {**_env("", "production"), "INTERNAL_DOMAIN": None}
    monkeypatch.setattr(deployer, "env", classmethod(lambda cls: env))
    with pytest.raises(Exit) as exc:
        mod.sso_setup.body(SimpleNamespace())
    assert exc.value.code == 1
    assert commands == []
