"""The Preview compose that the runner uploads to Dokploy (#1647): allow list and wiring."""

from __future__ import annotations

import copy
import re
from pathlib import Path

import pytest
import yaml

from common.deploy import preview as pl
from common.deploy import v2 as dv2
from common.deploy.preview_compose import (
    _MAX_TMPFS_ENTRIES,
    INFRA2_GIT_URL,
    _slot_suffix,
    app_preview_names,
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

    def mem_limit_zero(d):
        d["services"]["redis"]["mem_limit"] = "0"

    def tmpfs_size_zero(d):
        d["services"]["postgres"]["tmpfs"] = ["/var/lib/postgresql/data:size=0"]

    def tmpfs_too_many(d):
        d["services"]["postgres"]["tmpfs"] = [
            f"/t{n}:size=1m" for n in range(_MAX_TMPFS_ENTRIES + 1)
        ]

    def tmpfs_string(d):
        d["services"]["postgres"]["tmpfs"] = "/var/lib/postgresql/data:size=1m"

    def logging_missing(d):
        del d["services"]["redis"]["logging"]

    def logging_no_options(d):
        d["services"]["redis"]["logging"] = {"driver": "json-file"}

    def logging_no_max_size(d):
        del d["services"]["redis"]["logging"]["options"]["max-size"]

    def logging_huge_max_size(d):
        d["services"]["redis"]["logging"]["options"]["max-size"] = "500g"

    def logging_zero_max_size(d):
        d["services"]["redis"]["logging"]["options"]["max-size"] = "0"

    def logging_no_max_file(d):
        del d["services"]["redis"]["logging"]["options"]["max-file"]

    def logging_many_files(d):
        d["services"]["redis"]["logging"]["options"]["max-file"] = "4"

    def logging_not_a_mapping(d):
        d["services"]["redis"]["logging"] = "json-file"

    def no_container_name(d):
        del d["services"]["redis"]["container_name"]

    def dict_labels(d):
        d["services"]["todo"]["labels"] = dict(
            label.split("=", 1) for label in d["services"]["todo"]["labels"]
        )

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
        mem_limit_zero,
        big_mem_limit,
        big_cpu_shares,
        no_cpu_limit,
        tmpfs_without_size,
        tmpfs_too_big,
        tmpfs_size_zero,
        tmpfs_too_many,
        tmpfs_string,
        logging_missing,
        logging_no_options,
        logging_no_max_size,
        logging_huge_max_size,
        logging_zero_max_size,
        logging_no_max_file,
        logging_many_files,
        logging_not_a_mapping,
        no_container_name,
        dict_labels,
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


@pytest.mark.parametrize("value", [0, "0", "0g", "0b", "00", "0m", "5m", "5242879"])
def test_a_memory_limit_below_the_docker_minimum_is_refused(value):
    doc = _doc()
    doc["services"]["redis"]["mem_limit"] = value
    with pytest.raises(PreviewComposeRefused, match="6m-1g"):
        _render(doc)


@pytest.mark.parametrize("value", ["6m", "1g", "1024m", 1024**3, 6 * 1024**2])
def test_the_memory_limit_bounds_are_inclusive(value):
    doc = _doc()
    doc["services"]["redis"]["mem_limit"] = value
    assert _render(doc)


@pytest.mark.parametrize("size", ["0", "0m", "00", "0b"])
def test_a_tmpfs_of_size_zero_is_refused(size):
    doc = _doc()
    doc["services"]["postgres"]["tmpfs"] = [f"/var/lib/postgresql/data:size={size}"]
    with pytest.raises(PreviewComposeRefused, match="above 0"):
        _render(doc)


def test_the_tmpfs_entry_cap_is_inclusive():
    doc = _doc()
    doc["services"]["postgres"]["tmpfs"] = [
        f"/t{n}:size=1m" for n in range(_MAX_TMPFS_ENTRIES)
    ]
    assert _render(doc)


@pytest.mark.parametrize("size", ["10m", "10240k", "1k", 1024])
def test_the_log_size_bounds_are_inclusive(size):
    doc = _doc()
    doc["services"]["redis"]["logging"]["options"].update(
        {"max-size": size, "max-file": 3}
    )
    assert _render(doc)


@pytest.mark.parametrize("size", ["11m", "10241k", "1g", "0b"])
def test_a_log_size_over_the_cap_or_zero_is_refused(size):
    doc = _doc()
    doc["services"]["redis"]["logging"]["options"]["max-size"] = size
    with pytest.raises(PreviewComposeRefused, match="max-size"):
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
    coordinates = pl._preview_coordinates(UNIT, alias, "example.test")
    assert coordinates.env_suffix == _slot_suffix(PR)
    assert coordinates.env_domain_suffix == _slot_suffix(PR)


def test_every_preview_service_has_a_container_name_the_teardown_check_can_see():
    out = yaml.safe_load(render_runner_compose(UNIT, TODO_COMPOSE, SHA, PR))
    suffix = _slot_suffix(PR)
    names = [s["container_name"] for s in out["services"].values()]
    assert len(names) == len(out["services"]) > 0
    assert all(n.startswith("platform-") and n.endswith("${ENV_SUFFIX}") for n in names)
    containers = [{"name": n.replace("${ENV_SUFFIX}", suffix)} for n in names]

    class Inventory:
        def get_containers(self):
            return containers

    absent, survivors = pl.check_containers_absent(Inventory(), UNIT, suffix)
    assert not absent
    assert survivors == sorted(row["name"] for row in containers)


def _app_names() -> frozenset[str]:
    names = app_preview_names()
    # not green-while-empty: both App repositories contribute, and a router name is there
    assert {"truealpha-web", "truealpha-api"} <= names
    assert {"finance_report-backend", "finance-report-web"} <= names
    return names


def _valid_service_names() -> list[str]:
    return sorted(n for n in _app_names() if re.fullmatch(r"[a-z0-9][a-z0-9-]*", n))


def test_an_app_preview_name_is_found_by_its_container_router_and_traefik_service():
    names = _app_names()
    assert "truealpha-app-vault-agent" in names  # a container name
    assert "finance-report-api" in names  # a router and a Traefik service name
    assert not any("${" in n or n.endswith("-pr-") for n in names)


@pytest.mark.parametrize("name", _valid_service_names())
def test_a_service_named_like_an_app_preview_container_is_refused(name):
    # infra2 PR 7 renames service `<name>` to `<name>-pr-7`; App PR 7 runs `<name>-pr-7`.
    doc = _doc()
    doc["services"][name] = copy.deepcopy(doc["services"]["redis"])
    doc["services"][name]["container_name"] = "platform-x${ENV_SUFFIX}"
    with pytest.raises(PreviewComposeRefused, match="App PR Previews"):
        _render(doc)


def test_a_service_with_a_neutral_name_is_renamed_not_refused():
    doc = _doc()
    doc["services"]["neutral-web"] = copy.deepcopy(doc["services"]["redis"])
    doc["services"]["neutral-web"]["container_name"] = "platform-x${ENV_SUFFIX}"
    out = yaml.safe_load(_render(doc))
    assert f"neutral-web-pr-{PR}" in out["services"]


def _router_labels(router: str, service: str | None = None) -> list[str]:
    rule = "Host(`todo${ENV_DOMAIN_SUFFIX}.${INTERNAL_DOMAIN}`)"
    return [
        "traefik.enable=true",
        f"traefik.http.routers.{router}${{ENV_DOMAIN_SUFFIX}}.rule={rule}",
        *(
            [
                f"traefik.http.routers.{router}${{ENV_DOMAIN_SUFFIX}}.service={service}${{ENV_DOMAIN_SUFFIX}}"
            ]
            if service
            else []
        ),
    ]


def _with_labels(labels: list[str]) -> dict:
    doc = _doc()
    doc["services"]["todo"]["labels"] = labels
    return doc


def test_the_neutral_router_labels_pass():
    assert _render(_with_labels(_router_labels("neutral-router")))


@pytest.mark.parametrize("name", sorted(app_preview_names()))
def test_a_router_named_like_an_app_preview_router_is_refused(name):
    with pytest.raises(PreviewComposeRefused, match="App PR Previews"):
        _render(_with_labels(_router_labels(name)))


@pytest.mark.parametrize("name", sorted(app_preview_names()))
def test_a_traefik_service_named_like_an_app_preview_one_is_refused(name):
    labels = _router_labels("neutral-router") + [
        f"traefik.http.services.{name}${{ENV_DOMAIN_SUFFIX}}.loadbalancer.server.port=8000"
    ]
    with pytest.raises(PreviewComposeRefused, match="App PR Previews"):
        _render(_with_labels(labels))


@pytest.mark.parametrize("name", sorted(app_preview_names()))
def test_a_router_cannot_point_at_an_app_preview_traefik_service(name):
    with pytest.raises(PreviewComposeRefused, match="App PR Previews"):
        _render(_with_labels(_router_labels("neutral-router", service=name)))


def test_app_preview_names_fail_closed_when_an_app_has_no_preview_compose(tmp_path):
    (tmp_path / "truealpha").mkdir()
    (tmp_path / "finance_report").mkdir()
    with pytest.raises(PreviewComposeRefused, match="no App Preview names"):
        app_preview_names.__wrapped__(tmp_path)


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
