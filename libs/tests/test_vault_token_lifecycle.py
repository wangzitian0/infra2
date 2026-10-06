"""Tests for Vault app-token lifecycle ownership."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import types


from libs.vault_tokens import policy_name


ROOT = Path(__file__).resolve().parents[2]


def _install_task_stubs(monkeypatch):
    invoke_module = types.ModuleType("invoke")
    exceptions_module = types.ModuleType("invoke.exceptions")
    deployer_module = types.ModuleType("libs.deploy.deployer")
    console_module = types.ModuleType("libs.console")

    class Exit(Exception):
        def __init__(self, message="", code=1):
            super().__init__(message)
            self.message = message
            self.code = code

    def task(*args, **_kwargs):
        if args and callable(args[0]):
            return SimpleNamespace(body=args[0])
        return lambda fn: SimpleNamespace(body=fn)

    def emit(*args, **_kwargs):
        if args:
            print(" ".join(str(arg) for arg in args))

    invoke_module.task = task
    exceptions_module.Exit = Exit
    deployer_module.Deployer = object
    console_module.header = emit
    console_module.success = emit
    console_module.error = emit
    console_module.warning = emit
    console_module.info = emit
    console_module.prompt_action = emit
    console_module.run_with_status = lambda *_args, **_kwargs: None

    monkeypatch.setitem(sys.modules, "invoke", invoke_module)
    monkeypatch.setitem(sys.modules, "invoke.exceptions", exceptions_module)
    monkeypatch.setitem(sys.modules, "libs.deploy.deployer", deployer_module)
    monkeypatch.setitem(sys.modules, "libs.console", console_module)
    return Exit


def _load_vault_tasks(monkeypatch):
    exit_cls = _install_task_stubs(monkeypatch)
    path = ROOT / "bootstrap/05.vault/tasks.py"
    spec = importlib.util.spec_from_file_location("vault_tasks_under_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module, exit_cls


class FakeContext:
    def __init__(self, tracked_accessor: str | None = "old-accessor") -> None:
        self.commands: list[str] = []
        self.tracked_accessor = tracked_accessor

    def run(self, cmd, **kwargs):
        self.commands.append(cmd)
        env = kwargs.get("env") or {}
        if cmd == "vault token lookup -format=json":
            if env.get("VAULT_TOKEN") == "old-dokploy-token":
                return SimpleNamespace(
                    ok=True,
                    stdout=json.dumps({"data": {"accessor": "old-dokploy-accessor"}}),
                    stderr="",
                )
            return SimpleNamespace(
                ok=True, stdout=json.dumps({"data": {"ttl": 3600}}), stderr=""
            )
        if cmd.startswith("vault kv get"):
            if not self.tracked_accessor:
                return SimpleNamespace(ok=False, stdout="", stderr="not found")
            return SimpleNamespace(
                ok=True,
                stdout=json.dumps(
                    {"data": {"data": {"accessor": self.tracked_accessor}}}
                ),
                stderr="",
            )
        if cmd.startswith("vault token create"):
            return SimpleNamespace(
                ok=True,
                stdout=json.dumps(
                    {
                        "auth": {
                            "client_token": "hvs.new-secret-token-value",
                            "accessor": "new-accessor",
                        }
                    }
                ),
                stderr="",
            )
        return SimpleNamespace(ok=True, stdout="", stderr="")


class FakeDokployTokenDeploy:
    def __init__(self, deployment_snapshots: list[list[dict]]) -> None:
        self.compose_id = "compose-iac-runner"
        self.deployment_snapshots = list(deployment_snapshots)
        self.deploy_calls = 0
        self.redeploy_calls = 0
        self.updated_env: dict[str, str] = {}

    def find_compose_by_name(self, *_args, **_kwargs) -> dict:
        return {
            "composeId": self.compose_id,
            "env": "VAULT_APP_TOKEN=hvs.old-token\nENV=production\n",
        }

    def update_compose_env(self, compose_id, *, env_vars=None, env_str=None):
        assert compose_id == self.compose_id
        assert env_str is None
        self.updated_env.update(env_vars or {})
        return {"ok": True}

    def deploy_compose(self, compose_id):
        assert compose_id == self.compose_id
        self.deploy_calls += 1
        return {"ok": True}

    def redeploy_compose(self, compose_id):
        assert compose_id == self.compose_id
        self.redeploy_calls += 1
        return {"ok": True}

    def get_compose(self, compose_id):
        assert compose_id == self.compose_id
        if self.deployment_snapshots:
            return {"deployments": self.deployment_snapshots.pop(0)}
        return {"deployments": []}


def _install_fake_dokploy(monkeypatch, client: FakeDokployTokenDeploy) -> None:
    dokploy_module = types.ModuleType("libs.dokploy")
    dokploy_module.get_dokploy = lambda *_, **__: client
    monkeypatch.setitem(sys.modules, "libs.dokploy", dokploy_module)


def test_policy_names_are_env_scoped() -> None:
    """AC7.5.5: the per-service AppRole policy name includes project, env, and service."""
    assert (
        policy_name("finance_report", "staging", "app") == "finance_report-staging-app"
    )


def test_finance_report_vault_policies_are_env_scoped() -> None:
    """AC7.5.5: finance_report app tokens must not read every environment."""
    for policy_path in [
        ROOT / "finance_report/finance_report/01.postgres/vault-policy.hcl",
        ROOT / "finance_report/finance_report/02.redis/vault-policy.hcl",
        ROOT / "finance_report/finance_report/10.app/vault-policy.hcl",
    ]:
        policy = policy_path.read_text(encoding="utf-8")
        assert "/+/" not in policy, policy_path
        assert "{{env}}" in policy, policy_path


def test_vault_token_targets_includes_alerting_bridge(monkeypatch) -> None:
    """Infra-007 alerting: AppRole target discovery includes the alerting bridge."""
    tasks, _exit_cls = _load_vault_tasks(monkeypatch)
    targets = tasks._vault_token_targets(str(ROOT))

    assert any(
        target.project == "platform"
        and target.service == "alerting"
        and target.service_dir == "12.alerting"
        for target in targets
    )


def test_vault_token_targets_includes_the_canary_todo(monkeypatch) -> None:
    """#991: the canary renders its secrets through a vault-agent, so `setup-approle`
    must find it. A service missing from this map answers "No matching AppRole targets"
    (data_engine hit exactly that)."""
    tasks, _exit_cls = _load_vault_tasks(monkeypatch)
    targets = tasks._vault_token_targets(str(ROOT))

    (todo,) = [
        target
        for target in targets
        if target.project == "platform" and target.service == "todo"
    ]
    assert todo.service_dir == "30.todo"
    assert todo.dokploy_project == "platform"
    selected = tasks._select_token_targets(targets, "platform", "todo")
    assert selected == [todo]
    assert (ROOT / "platform" / todo.service_dir / "vault-policy.hcl").exists()


def test_configure_dokploy_approle_injects_creds_and_redeploys(monkeypatch) -> None:
    """#257: AppRole injection (VAULT_ROLE_ID/VAULT_SECRET_ID) succeeds only after a runtime
    apply proof — same shared spine (`_redeploy_with_vault_creds`) and record-wait as the
    token path. This path previously had no direct unit test."""
    tasks, _exit_cls = _load_vault_tasks(monkeypatch)
    client = FakeDokployTokenDeploy(
        [
            [{"deploymentId": "old", "status": "done"}],
            [{"deploymentId": "old", "status": "done"}],
            [{"deploymentId": "old", "status": "done"}],
            [
                {"deploymentId": "old", "status": "done"},
                {"deploymentId": "new", "status": "done"},
            ],
        ]
    )
    _install_fake_dokploy(monkeypatch, client)
    monkeypatch.setattr(
        "libs.common.get_env",
        lambda: {"ENV": "production", "INTERNAL_DOMAIN": "zitian.party"},
    )
    monkeypatch.setenv("DOKPLOY_DEPLOYMENT_RECORD_TIMEOUT_SECONDS", "0")
    monkeypatch.setattr(tasks.time, "sleep", lambda _seconds: None)

    ok = tasks._configure_dokploy_approle(
        FakeContext(), "redis", "role-123", "secret-456", "platform"
    )

    assert ok is True
    assert client.updated_env == {
        "VAULT_ROLE_ID": "role-123",
        "VAULT_SECRET_ID": "secret-456",
    }
    assert client.deploy_calls == 1
    assert client.redeploy_calls == 1


def test_configure_dokploy_approle_returns_false_when_service_absent(
    monkeypatch,
) -> None:
    """#257: a service not registered in Dokploy fails closed (False), not a crash."""
    tasks, _exit_cls = _load_vault_tasks(monkeypatch)

    class _NoCompose:
        def find_compose_by_name(self, *_a, **_k):
            return None

    _install_fake_dokploy(monkeypatch, _NoCompose())
    monkeypatch.setattr(
        "libs.common.get_env",
        lambda: {"ENV": "production", "INTERNAL_DOMAIN": "zitian.party"},
    )

    ok = tasks._configure_dokploy_approle(FakeContext(), "ghost", "r", "s", "platform")
    assert ok is False


class FakeApproleVault:
    """A `c` for setup_approle. It records each Vault step in a shared event list."""

    ROLE = "platform-production-todo"

    def __init__(
        self,
        events: list[str],
        *,
        prior: str = '["acc-old-1", "acc-old-2"]',
        prior_ok: bool = True,
        destroy_ok: bool = True,
    ) -> None:
        self.events = events
        self.prior = prior
        self.prior_ok = prior_ok
        self.destroy_ok = destroy_ok

    def run(self, cmd, **_kwargs):
        def res(ok=True, stdout=""):
            return SimpleNamespace(ok=ok, stdout=stdout, stderr="")

        if cmd == "vault auth list -format=json":
            return res(stdout=json.dumps({"approle/": {}}))
        if cmd == f"vault list -format=json auth/approle/role/{self.ROLE}/secret-id":
            self.events.append("list")
            return res(ok=self.prior_ok, stdout=self.prior)
        if cmd == f"vault read -format=json auth/approle/role/{self.ROLE}/role-id":
            return res(stdout=json.dumps({"data": {"role_id": "role-1"}}))
        if (
            cmd
            == f"vault write -f -format=json auth/approle/role/{self.ROLE}/secret-id"
        ):
            self.events.append("issue")
            return res(
                stdout=json.dumps(
                    {
                        "data": {
                            "secret_id": "secret-new",
                            "secret_id_accessor": "acc-new",
                        }
                    }
                )
            )
        prefix = (
            f"vault write auth/approle/role/{self.ROLE}/secret-id-accessor/destroy "
        )
        if cmd.startswith(prefix):
            self.events.append("destroy:" + cmd.removeprefix(prefix))
            return res(ok=self.destroy_ok)
        return res()


def _run_setup_approle(
    monkeypatch,
    vault: FakeApproleVault,
    *,
    configure_ok: bool = True,
    refresh_ok: bool = True,
    deploy: bool = True,
):
    """Run setup_approle for platform/todo against the fakes. Returns the Exit or None."""
    tasks, exit_cls = _load_vault_tasks(monkeypatch)
    monkeypatch.setenv("VAULT_ROOT_TOKEN", "root-token")
    monkeypatch.setattr(
        tasks,
        "get_env",
        lambda: {"ENV": "production", "INTERNAL_DOMAIN": "zitian.party"},
    )

    def configure(_c, service, role_id, secret_id, project):
        vault.events.append(f"configure:{service}:{role_id}:{secret_id}:{project}")
        return configure_ok

    def refresh(role_id, secret_id):
        vault.events.append(f"refresh:{role_id}:{secret_id}")
        return refresh_ok

    monkeypatch.setattr(tasks, "_configure_dokploy_approle", configure)
    monkeypatch.setattr(tasks, "_refresh_role_consumers", refresh)
    try:
        tasks.setup_approle.body(
            vault, project="platform", service="todo", deploy=deploy
        )
    except exit_cls as exc:
        return exc
    return None


def test_reissue_destroys_earlier_secret_ids_after_every_consumer_runs_the_new_one(
    monkeypatch,
) -> None:
    """#1070: a re-issue ends the earlier secret ids. The destroy comes only after the
    target compose and every other consumer of the role run the new secret id, and
    it never touches the new one."""
    events: list[str] = []
    failure = _run_setup_approle(monkeypatch, FakeApproleVault(events))

    assert failure is None
    assert events == [
        "list",
        "issue",
        "configure:todo:role-1:secret-new:platform",
        "refresh:role-1:secret-new",
        "destroy:secret_id_accessor=acc-old-1",
        "destroy:secret_id_accessor=acc-old-2",
    ]


def test_reissue_lists_the_earlier_secret_ids_before_it_issues_the_new_one(
    monkeypatch,
) -> None:
    """#1070: a list taken after the issue would contain the new accessor. The list of
    accessors to destroy must exclude the new one even then."""
    events: list[str] = []
    vault = FakeApproleVault(events, prior='["acc-old-1", "acc-new"]')
    failure = _run_setup_approle(monkeypatch, vault)

    assert failure is None
    assert events.index("list") < events.index("issue")
    assert "destroy:secret_id_accessor=acc-new" not in events
    assert "destroy:secret_id_accessor=acc-old-1" in events


def test_reissue_of_a_role_with_no_secret_id_destroys_nothing(monkeypatch) -> None:
    """#1070: `vault list -format=json` prints `{}` and exits 2 for a role with no
    secret id. That is an empty list, not a failure."""
    events: list[str] = []
    vault = FakeApproleVault(events, prior="{}", prior_ok=False)
    failure = _run_setup_approle(monkeypatch, vault)

    assert failure is None
    assert events[:2] == ["list", "issue"]
    assert not [e for e in events if e.startswith("destroy:")]


def test_a_failed_target_deploy_destroys_nothing(monkeypatch) -> None:
    """#1070: when the target compose does not run the new secret id, its containers
    still log in with an earlier one. The task destroys nothing and exits 1."""
    events: list[str] = []
    failure = _run_setup_approle(
        monkeypatch, FakeApproleVault(events), configure_ok=False
    )

    assert failure is not None and failure.code == 1
    assert not [e for e in events if e.startswith(("destroy:", "refresh:"))]


def test_a_failed_consumer_refresh_destroys_nothing(monkeypatch) -> None:
    """#1070: a PR preview copies the AppRole credentials of its source environment.
    When one consumer keeps an earlier secret id, the task destroys nothing."""
    events: list[str] = []
    failure = _run_setup_approle(
        monkeypatch, FakeApproleVault(events), refresh_ok=False
    )

    assert failure is not None and failure.code == 1
    assert not [e for e in events if e.startswith("destroy:")]


def test_a_failed_destroy_exits_one_and_names_the_role(monkeypatch, capsys) -> None:
    """#1070: an earlier secret id that stays valid is a failed re-issue, not a warning."""
    events: list[str] = []
    failure = _run_setup_approle(
        monkeypatch, FakeApproleVault(events, destroy_ok=False)
    )

    assert failure is not None and failure.code == 1
    assert "destroy:secret_id_accessor=acc-old-1" in events
    out = capsys.readouterr().out
    assert "platform/todo: secret_id_destroy_failed" in out
    assert (
        f"{FakeApproleVault.ROLE}: failed to destroy secret id accessor acc-old-1"
        in out
    )


def test_an_unreadable_secret_id_list_issues_nothing(monkeypatch) -> None:
    """#1070: without the earlier accessors the task cannot end them later, so it
    issues no new secret id and exits 1."""
    events: list[str] = []
    vault = FakeApproleVault(events, prior="", prior_ok=False)
    failure = _run_setup_approle(monkeypatch, vault)

    assert failure is not None and failure.code == 1
    assert events == ["list"]


def test_no_deploy_issues_no_secret_id(monkeypatch) -> None:
    """#1070: with deploy=False nothing stores or prints a secret id, so issuing one
    only adds a valid credential that no service uses."""
    events: list[str] = []
    failure = _run_setup_approle(monkeypatch, FakeApproleVault(events), deploy=False)

    assert failure is None
    assert events == []


def test_secret_id_accessors_rejects_a_value_unsafe_for_the_shell(monkeypatch) -> None:
    """#1070: an accessor goes into a shell command, so only the UUID shape passes."""
    tasks, _exit_cls = _load_vault_tasks(monkeypatch)
    vault = FakeApproleVault([], prior='["acc-1; rm -rf /"]')

    assert tasks._secret_id_accessors(vault, {}, FakeApproleVault.ROLE) is None
    vault.prior = '["2c9c-41f7-a1d0"]'
    assert tasks._secret_id_accessors(vault, {}, FakeApproleVault.ROLE) == [
        "2c9c-41f7-a1d0"
    ]


class FakeDokployConsumers:
    """Dokploy with one target compose, one preview of the same role, one other role."""

    def __init__(self, composes: dict[str, str]) -> None:
        self.envs = dict(composes)
        self.updated: dict[str, dict[str, str]] = {}
        self.deployed: list[str] = []

    def list_projects(self) -> list[dict]:
        return [
            {
                "name": "finance_report",
                "environments": [
                    {
                        "name": "staging",
                        "compose": [{"composeId": "app", "name": "app"}],
                    },
                    {
                        "name": "preview",
                        "compose": [
                            {"composeId": cid, "name": cid}
                            for cid in self.envs
                            if cid != "app"
                        ],
                    },
                ],
            }
        ]

    def get_compose_env(self, compose_id: str) -> str:
        return self.envs[compose_id]

    def update_compose_env(self, compose_id, *, env_vars=None, env_str=None):
        self.updated[compose_id] = dict(env_vars or {})
        lines = [
            line
            for line in self.envs[compose_id].splitlines()
            if line.partition("=")[0] not in (env_vars or {})
        ]
        lines += [f"{k}={v}" for k, v in (env_vars or {}).items()]
        self.envs[compose_id] = "\n".join(lines) + "\n"


def test_refresh_role_consumers_redeploys_each_compose_on_an_earlier_secret_id(
    monkeypatch,
) -> None:
    """#1070: every compose that logs in as the role and holds another secret id gets
    the new one and a redeploy. Composes of other roles stay untouched."""
    tasks, _exit_cls = _load_vault_tasks(monkeypatch)
    client = FakeDokployConsumers(
        {
            "app": "VAULT_ROLE_ID=role-1\nVAULT_SECRET_ID=secret-new\n",
            "report-pr-7": "ENV=pr-7\nVAULT_ROLE_ID=role-1\nVAULT_SECRET_ID=secret-old\n",
            "other": "VAULT_ROLE_ID=role-2\nVAULT_SECRET_ID=secret-old\n",
        }
    )
    monkeypatch.setattr(tasks, "_dokploy_client", lambda: (client, "staging"))
    monkeypatch.setattr(
        tasks,
        "_deploy_compose_with_record_check",
        lambda _client, compose_id: client.deployed.append(compose_id),
    )

    assert tasks._refresh_role_consumers("role-1", "secret-new") is True
    assert client.updated == {"report-pr-7": {"VAULT_SECRET_ID": "secret-new"}}
    assert client.deployed == ["report-pr-7"]


def test_refresh_role_consumers_fails_when_a_redeploy_fails(monkeypatch) -> None:
    """#1070: a consumer without a new `done` deployment record still runs the earlier
    secret id, so the refresh reports failure."""
    tasks, _exit_cls = _load_vault_tasks(monkeypatch)
    client = FakeDokployConsumers(
        {
            "app": "VAULT_ROLE_ID=role-1\nVAULT_SECRET_ID=secret-new\n",
            "report-pr-7": "VAULT_ROLE_ID=role-1\nVAULT_SECRET_ID=secret-old\n",
        }
    )
    monkeypatch.setattr(tasks, "_dokploy_client", lambda: (client, "staging"))

    def stale(_client, _compose_id):
        raise RuntimeError("no new deployment record")

    monkeypatch.setattr(tasks, "_deploy_compose_with_record_check", stale)

    assert tasks._refresh_role_consumers("role-1", "secret-new") is False


def _policy_paths() -> set[str]:
    """Extract the `path "..."` globs granted by the IaC Runner Vault policy."""
    import re

    policy = (ROOT / "bootstrap" / "06.iac_runner" / "vault-policy.hcl").read_text(
        encoding="utf-8"
    )
    return set(re.findall(r'path\s+"([^"]+)"', policy))


def _policy_matches(glob: str, path: str) -> bool:
    """Minimal Vault ACL match: `+` = exactly one segment, `*` = trailing segments."""
    g, p = glob.split("/"), path.split("/")
    for i, seg in enumerate(g):
        if seg == "*":
            return True
        if i >= len(p):
            return False
        if seg != "+" and seg != p[i]:
            return False
    return len(g) == len(p)


def test_policy_grants_metadata_list_for_service_secrets():
    """KV v2 LIST resolves to secret/metadata/, so a `list` on secret/data/ is a no-op;
    the policy must grant metadata for the service trees the runner enumerates."""
    paths = _policy_paths()
    for svc in (
        "secret/metadata/finance_report/production/app",
        "secret/metadata/platform/production/alerting",
    ):
        assert any(_policy_matches(g, svc) for g in paths), (
            f"no metadata list grant for {svc}"
        )


def _consumers_fixture(monkeypatch, envs: dict[str, str], deploy=None):
    tasks, _exit_cls = _load_vault_tasks(monkeypatch)
    client = FakeDokployConsumers(envs)
    monkeypatch.setattr(tasks, "_dokploy_client", lambda: (client, "staging"))
    monkeypatch.setattr(
        tasks,
        "_deploy_compose_with_record_check",
        deploy or (lambda _client, compose_id: client.deployed.append(compose_id)),
    )
    return tasks, client


def test_refresh_role_consumers_reports_every_blocked_consumer(
    monkeypatch, capsys
) -> None:
    """#1070: one failed preview must not hide the others. The task tries every
    consumer and names each one that still holds an earlier secret id."""
    attempted: list[str] = []

    def deploy(_client, compose_id):
        attempted.append(compose_id)
        raise RuntimeError("no new deployment record")

    tasks, _client = _consumers_fixture(
        monkeypatch,
        {
            "app": "VAULT_ROLE_ID=role-1\nVAULT_SECRET_ID=secret-new\n",
            "report-pr-7": "VAULT_ROLE_ID=role-1\nVAULT_SECRET_ID=secret-old\n",
            "report-pr-8": "VAULT_ROLE_ID=role-1\nVAULT_SECRET_ID=secret-old\n",
        },
        deploy,
    )

    assert tasks._refresh_role_consumers("role-1", "secret-new") is False
    assert attempted == ["report-pr-7", "report-pr-8"]
    out = capsys.readouterr().out
    assert (
        "finance_report/preview/report-pr-7, finance_report/preview/report-pr-8" in out
    )


def test_refresh_role_consumers_fails_when_an_earlier_secret_id_comes_back(
    monkeypatch,
) -> None:
    """#1070: a deploy that read the earlier env can write it back after the scan.
    The second read before the destroy must catch it."""

    def deploy(client, compose_id):
        client.deployed.append(compose_id)
        client.envs[compose_id] = "VAULT_ROLE_ID=role-1\nVAULT_SECRET_ID=secret-old\n"

    tasks, client = _consumers_fixture(
        monkeypatch,
        {
            "app": "VAULT_ROLE_ID=role-1\nVAULT_SECRET_ID=secret-new\n",
            "report-pr-7": "VAULT_ROLE_ID=role-1\nVAULT_SECRET_ID=secret-old\n",
        },
    )
    monkeypatch.setattr(tasks, "_deploy_compose_with_record_check", deploy)

    assert tasks._refresh_role_consumers("role-1", "secret-new") is False
    assert client.deployed == ["report-pr-7"]


def test_refresh_role_consumers_fails_when_the_scan_misses_the_target(
    monkeypatch,
) -> None:
    """#1070: the target already holds the new secret id. A scan that does not see
    it is incomplete, so the refresh fails instead of reporting zero consumers."""
    tasks, client = _consumers_fixture(
        monkeypatch, {"app": "VAULT_ROLE_ID=role-2\nVAULT_SECRET_ID=secret-x\n"}
    )

    assert tasks._refresh_role_consumers("role-1", "secret-new") is False
    assert client.updated == {}


def test_refresh_role_consumers_fails_on_a_compose_without_id(monkeypatch) -> None:
    """#1070: a compose the scan cannot read could be a consumer."""
    tasks, client = _consumers_fixture(
        monkeypatch, {"app": "VAULT_ROLE_ID=role-1\nVAULT_SECRET_ID=secret-new\n"}
    )
    projects = client.list_projects()
    projects[0]["environments"][0]["compose"].append({"name": "no-id"})
    monkeypatch.setattr(client, "list_projects", lambda: projects)

    assert tasks._refresh_role_consumers("role-1", "secret-new") is False


def test_refresh_role_consumers_fails_when_an_env_read_fails(monkeypatch) -> None:
    """#1070: an unreadable env could hide a consumer."""
    tasks, client = _consumers_fixture(
        monkeypatch, {"app": "VAULT_ROLE_ID=role-1\nVAULT_SECRET_ID=secret-new\n"}
    )

    def broken(_compose_id):
        raise RuntimeError("compose.one: 502")

    monkeypatch.setattr(client, "get_compose_env", broken)

    assert tasks._refresh_role_consumers("role-1", "secret-new") is False


def test_env_value_reads_quoted_and_spaced_values(monkeypatch) -> None:
    """#1070: a quoted value must compare equal to the bare role id."""
    tasks, _exit_cls = _load_vault_tasks(monkeypatch)
    env = "A=1\nVAULT_ROLE_ID=\"role-1\"\n VAULT_SECRET_ID = 's-1'\n"

    assert tasks._env_value(env, "VAULT_ROLE_ID") == "role-1"
    assert tasks._env_value(env, "VAULT_SECRET_ID") == "s-1"
    assert tasks._env_value(env, "MISSING") == ""


def test_setup_approle_handles_the_iac_runner_last(monkeypatch) -> None:
    """#1070: a run inside the runner (SOP-007) stops when the runner redeploys, so
    every other role completes first."""
    tasks, exit_cls = _load_vault_tasks(monkeypatch)
    monkeypatch.setenv("VAULT_ROOT_TOKEN", "root-token")
    monkeypatch.setattr(
        tasks,
        "get_env",
        lambda: {"ENV": "production", "INTERNAL_DOMAIN": "zitian.party"},
    )
    order: list[str] = []
    monkeypatch.setattr(
        tasks,
        "_configure_dokploy_approle",
        lambda _c, service, *_a: order.append(service) or False,
    )

    monkeypatch.setattr(tasks, "_secret_id_accessors", lambda *_a: [])

    class AnyRoleVault:
        def run(self, cmd, **_kwargs):
            out = ""
            if cmd == "vault auth list -format=json":
                out = json.dumps({"approle/": {}})
            elif cmd.endswith("/role-id"):
                out = json.dumps({"data": {"role_id": "r"}})
            elif cmd.endswith("/secret-id"):
                out = json.dumps(
                    {"data": {"secret_id": "s", "secret_id_accessor": "a"}}
                )
            return SimpleNamespace(ok=True, stdout=out, stderr="")

    try:
        tasks.setup_approle.body(AnyRoleVault(), deploy=True)
    except exit_cls:
        pass

    assert "iac_runner" in order and len(order) > 1
    assert order[-1] == "iac_runner"
