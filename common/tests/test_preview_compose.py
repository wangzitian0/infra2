"""The Preview compose that the runner uploads to Dokploy (#1647): allow list and wiring."""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

from common.deploy import preview as pl
from common.deploy import v2 as dv2
from common.deploy.preview_compose import (
    INFRA2_GIT_URL,
    reserved_service_names,
    PreviewComposeRefused,
    assert_runner_unit,
    render_runner_compose,
)
from common.tests.test_preview_lifecycle import FakeDokploy

ROOT = Path(__file__).resolve().parents[2]
SHA = "a" * 40
PR = 7
UNIT = "platform/todo"
TODO_COMPOSE = (ROOT / "platform/30.todo/preview.compose.yaml").read_text()


def _doc() -> dict:
    return yaml.safe_load(TODO_COMPOSE)


def _render(doc: dict) -> str:
    return render_runner_compose(UNIT, yaml.safe_dump(doc), SHA, PR)


def test_the_real_todo_preview_compose_passes_and_builds_from_the_pr_commit():
    out = yaml.safe_load(render_runner_compose(UNIT, TODO_COMPOSE, SHA, PR))
    services = out["services"]
    assert set(services) == {f"{n}-pr-{PR}" for n in _doc()["services"]}
    built = {n: s for n, s in services.items() if "build" in s}
    assert set(built) == {f"s3-pr-{PR}", f"s3-init-pr-{PR}", f"todo-pr-{PR}"}
    for name, svc in built.items():
        assert svc["build"]["context"] == f"{INFRA2_GIT_URL}#{SHA}"
        assert (
            svc["image"]
            == f"platform-todo-{name.removesuffix(f'-pr-{PR}')}-preview-pr-{PR}:{SHA[:12]}"
        )
    assert out["services"][f"todo-pr-{PR}"]["depends_on"].keys() == {
        f"redis-pr-{PR}",
        f"postgres-pr-{PR}",
        f"s3-init-pr-{PR}",
    }


def _mutations():
    def socket(d):
        d["services"]["todo"]["volumes"] = ["/var/run/docker.sock:/var/run/docker.sock"]

    def host_root(d):
        d["services"]["todo"]["volumes"] = ["/:/host"]

    def undeclared_volume(d):
        d["services"]["todo"]["volumes"] = ["stolen:/data"]

    def privileged(d):
        d["services"]["todo"]["privileged"] = True

    def cap_add(d):
        d["services"]["todo"]["cap_add"] = ["SYS_ADMIN"]

    def devices(d):
        d["services"]["todo"]["devices"] = ["/dev/sda:/dev/sda"]

    def host_net(d):
        d["services"]["todo"]["network_mode"] = "host"

    def pid_host(d):
        d["services"]["todo"]["pid"] = "host"

    def ports(d):
        d["services"]["postgres"]["ports"] = ["5432:5432"]

    def other_network(d):
        d["services"]["todo"]["networks"] = ["slot", "bootstrap-net"]
        d["networks"]["bootstrap-net"] = {"external": True}

    def top_level_key(d):
        d["configs"] = {"x": {"file": "/etc/shadow"}}

    def foreign_host(d):
        labels = d["services"]["todo"]["labels"]
        labels[3] = (
            "traefik.http.routers.platform-todo-public${ENV_DOMAIN_SUFFIX}.rule="
            "Host(`vault.${INTERNAL_DOMAIN}`)"
        )

    def unscoped_router(d):
        d["services"]["todo"]["labels"].append(
            "traefik.http.routers.platform-todo-public.rule=Host(`x`)"
        )

    def middleware_label(d):
        d["services"]["todo"]["labels"].append(
            "traefik.http.middlewares.x${ENV_DOMAIN_SUFFIX}.basicauth.users=a:b"
        )

    def remote_context(d):
        d["services"]["todo"]["build"]["context"] = "https://evil.example/x.git"

    def escaping_dockerfile(d):
        d["services"]["todo"]["build"]["dockerfile"] = "../../etc/Dockerfile"

    def prod_container_name(d):
        d["services"]["todo"]["container_name"] = "platform-todo"

    def syslog_driver(d):
        d["services"]["todo"]["logging"] = {"driver": "syslog"}

    def image_beside_build(d):
        d["services"]["todo"]["image"] = "hashicorp/vault:1.15"

    def pull_policy_never(d):
        d["services"]["redis"]["pull_policy"] = "never"

    def aliases(d):
        d["services"]["todo"]["networks"] = {
            "slot": None,
            "dokploy-network": {"aliases": ["vault", "op-connect-api"]},
        }

    def static_address(d):
        d["services"]["todo"]["networks"] = {
            "slot": None,
            "dokploy-network": {"ipv4_address": "172.18.0.2"},
        }

    def uppercase_service_name(d):
        d["services"]["Vault"] = d["services"]["redis"]

    def foreign_prefix_container(d):
        d["services"]["todo"]["container_name"] = "vault${ENV_SUFFIX}"

    def no_mem_limit(d):
        del d["services"]["redis"]["mem_limit"]

    def big_mem_limit(d):
        d["services"]["redis"]["mem_limit"] = "2g"

    def big_cpu_shares(d):
        d["services"]["redis"]["cpu_shares"] = 262144

    def no_cpu_limit(d):
        del d["services"]["redis"]["cpu_shares"]

    def tmpfs_without_size(d):
        d["services"]["postgres"]["tmpfs"] = ["/var/lib/postgresql/data"]

    def tmpfs_too_big(d):
        d["services"]["postgres"]["tmpfs"] = ["/fill:size=200g"]

    def too_many_services(d):
        for n in range(3):
            d["services"][f"extra{n}"] = copy.deepcopy(d["services"]["redis"])

    def too_many_builds(d):
        d["services"]["redis"].pop("image")
        d["services"]["redis"]["build"] = copy.deepcopy(d["services"]["s3"]["build"])
        d["services"]["postgres"].pop("image")
        d["services"]["postgres"]["build"] = copy.deepcopy(d["services"]["s3"]["build"])

    def all_entrypoints(d):
        d["services"]["todo"]["labels"][4] = (
            "traefik.http.routers.platform-todo-public${ENV_DOMAIN_SUFFIX}.entrypoints=traefik,web"
        )

    def other_resolver(d):
        labels = d["services"]["todo"]["labels"]
        labels[6] = (
            "traefik.http.routers.platform-todo-public${ENV_DOMAIN_SUFFIX}"
            ".tls.certresolver=attacker"
        )

    def bad_port(d):
        labels = d["services"]["todo"]["labels"]
        labels[2] = (
            "traefik.http.services.platform-todo${ENV_DOMAIN_SUFFIX}"
            ".loadbalancer.server.port=99999"
        )

    def enabled_without_router(d):
        d["services"]["redis"]["labels"] = ["traefik.enable=true"]

    def depends_on_unknown(d):
        d["services"]["todo"]["depends_on"] = ["vault"]

    return [
        image_beside_build,
        pull_policy_never,
        aliases,
        static_address,
        uppercase_service_name,
        foreign_prefix_container,
        no_mem_limit,
        big_mem_limit,
        big_cpu_shares,
        no_cpu_limit,
        tmpfs_without_size,
        tmpfs_too_big,
        too_many_services,
        too_many_builds,
        all_entrypoints,
        other_resolver,
        bad_port,
        enabled_without_router,
        depends_on_unknown,
        socket,
        host_root,
        undeclared_volume,
        privileged,
        cap_add,
        devices,
        host_net,
        pid_host,
        ports,
        other_network,
        top_level_key,
        foreign_host,
        unscoped_router,
        middleware_label,
        remote_context,
        escaping_dockerfile,
        prod_container_name,
        syslog_driver,
    ]


@pytest.mark.parametrize("mutate", _mutations(), ids=lambda f: f.__name__)
def test_a_malicious_preview_compose_is_refused(mutate):
    doc = copy.deepcopy(_doc())
    mutate(doc)
    with pytest.raises(PreviewComposeRefused):
        _render(doc)


@pytest.mark.parametrize("unit", ["finance_report/app", "truealpha/app", "platform/x"])
def test_app_and_unknown_units_are_refused(unit):
    with pytest.raises(ValueError):
        assert_runner_unit(unit)


def test_the_runner_refuses_a_socket_mount_before_any_dokploy_call(monkeypatch):
    doc = _doc()
    doc["services"]["todo"]["volumes"] = ["/var/run/docker.sock:/var/run/docker.sock"]
    monkeypatch.setattr(
        dv2, "read_compose_at", lambda service, sha: yaml.safe_dump(doc)
    )

    def no_dokploy(host=None):
        raise AssertionError("Dokploy must not be reached")

    import common.deploy.dokploy_client as dokploy_client

    monkeypatch.setattr(dokploy_client, "get_dokploy", no_dokploy)
    monkeypatch.setattr(dv2, "_preview_up", lambda *a, **k: pytest.fail("up reached"))
    code = dv2.main(
        [
            "--service", UNIT, "--type", "preview/pr", "--version-ref", "7",
            "--iac-ref", SHA, "--domain", "example.test", "--raw-compose",
        ]
    )  # fmt: skip
    assert code == 1


def test_a_platform_preview_uploads_the_checked_compose_and_reads_no_vault(monkeypatch):
    monkeypatch.setattr(pl, "resolve_to_sha", lambda code, repo=None: SHA)
    client = FakeDokploy(existing=None)
    compose = render_runner_compose(UNIT, TODO_COMPOSE, SHA, PR)
    pl.up(
        "pr",
        7,
        code=SHA,
        service=UNIT,
        domain="example.test",
        client=client,
        wait=False,
        repo=INFRA2_GIT_URL,
        compose_file=compose,
    )
    source = [u for u in client.updated if "source_type" in u][-1]
    assert source["source_type"] == "raw" and source["compose_file"] == compose
    env = client.env_updates[-1][1]
    assert not {"VAULT_ROLE_ID", "VAULT_SECRET_ID", "VAULT_ADDR"} & set(env)
    assert ("app", "staging") not in [(n, e) for n, _p, e in client.found]


@pytest.mark.parametrize("name", ["vault", "op-connect-api", "platform-postgres"])
def test_a_production_service_name_never_reaches_dokploy_network_as_a_dns_name(name):
    assert name in reserved_service_names()  # read from the repository's compose files
    doc = copy.deepcopy(_doc())
    doc["services"][name] = copy.deepcopy(doc["services"]["redis"])
    doc["services"][name]["container_name"] = "platform-x${ENV_SUFFIX}"
    doc["services"][name]["networks"] = ["slot", "dokploy-network"]
    out = yaml.safe_load(_render(doc))
    assert name not in out["services"]
    assert f"{name}-pr-{PR}" in out["services"]
    assert not {n for n in out["services"]} & reserved_service_names()


def test_a_deeply_nested_document_is_refused_cleanly():
    with pytest.raises(PreviewComposeRefused):
        render_runner_compose(UNIT, "[" * 20000, SHA, PR)


def test_infra2_slot_names_never_equal_an_app_pr_preview_name():
    infra = yaml.safe_load(render_runner_compose(UNIT, TODO_COMPOSE, SHA, PR))
    suffix = f"-pr-{PR}"
    infra_names = set(infra["services"])
    infra_names |= {
        s["container_name"].replace("${ENV_SUFFIX}", suffix)
        for s in infra["services"].values()
        if "container_name" in s
    }
    infra_labels = [
        label.split("=", 1)[0].replace("${ENV_DOMAIN_SUFFIX}", suffix)
        for s in infra["services"].values()
        for label in s.get("labels", [])
    ]
    app_names: set[str] = set()
    app_labels: list[str] = []
    for rel in (
        "finance_report/finance_report/preview/compose.yaml",
        "truealpha/truealpha/preview/compose.yaml",
    ):
        app = yaml.safe_load((ROOT / rel).read_text())
        app_names |= set(app["services"])
        for s in app["services"].values():
            if "container_name" in s:
                app_names.add(s["container_name"].replace("${ENV_SUFFIX}", suffix))
            app_labels += [
                label.split("=", 1)[0].replace("${ENV_DOMAIN_SUFFIX}", suffix)
                for label in s.get("labels", [])
                if label.startswith("traefik.http")
            ]
    assert app_names and app_labels  # not green-while-empty
    assert not infra_names & {n.replace("${ENV_SUFFIX}", suffix) for n in app_names}
    assert not set(infra_labels) & set(app_labels)


def test_preview_alias_suffix_is_the_one_the_renderer_assumes():
    alias = pl.preview_alias("pr", PR, slug_prefix="platform-todo-preview")
    assert (
        pl._preview_coordinates(UNIT, alias, "example.test").env_suffix == f"-pr-{PR}"
    )


def test_down_cancels_a_running_deploy_before_it_deletes_the_compose(monkeypatch):
    monkeypatch.setattr(pl.time, "sleep", lambda _s: None)
    events: list[str] = []

    class Late(FakeDokploy):
        def __init__(self):
            super().__init__(existing={"composeId": "cmp-1"})
            self.status = "running"

        def get_latest_deployment(self, compose_id):
            return {"status": self.status}

        def cancel_compose_deployment(self, compose_id):
            events.append("cancel")
            self.status = "cancelled"

        def delete_compose(self, compose_id, *, delete_volumes=False):
            events.append("delete")
            return super().delete_compose(compose_id, delete_volumes=delete_volumes)

    pl.down("pr", PR, domain="example.test", client=Late(), service=UNIT)
    assert events == ["cancel", "delete"]
