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
    PreviewComposeRefused,
    assert_runner_unit,
    render_runner_compose,
)
from common.tests.test_preview_lifecycle import FakeDokploy

ROOT = Path(__file__).resolve().parents[2]
SHA = "a" * 40
UNIT = "platform/todo"
TODO_COMPOSE = (ROOT / "platform/30.todo/preview.compose.yaml").read_text()


def _doc() -> dict:
    return yaml.safe_load(TODO_COMPOSE)


def _render(doc: dict) -> str:
    return render_runner_compose(UNIT, yaml.safe_dump(doc), SHA)


def test_the_real_todo_preview_compose_passes_and_builds_from_the_pr_commit():
    out = yaml.safe_load(render_runner_compose(UNIT, TODO_COMPOSE, SHA))
    contexts = {
        name: svc["build"]["context"]
        for name, svc in out["services"].items()
        if "build" in svc
    }
    assert contexts == {
        "s3": f"{INFRA2_GIT_URL}#{SHA}",
        "todo": f"{INFRA2_GIT_URL}#{SHA}",
    }
    assert set(out["services"]) == set(_doc()["services"])


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

    return [
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
    compose = render_runner_compose(UNIT, TODO_COMPOSE, SHA)
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
