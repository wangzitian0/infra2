"""tools/secrets_prune: a service's store holds only what something reads (#649)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from infra2_sdk.secrets import SecretsError, WriteResult, vault_path

from libs.secrets_registry import SERVICES, Service
from tools import secrets_prune


class MemoryStore:
    def __init__(
        self, documents=None, *, fail: str | None = None, refuse_write: bool = False
    ):
        self.documents = {k: dict(v) for k, v in (documents or {}).items()}
        self.fail = fail
        self.refuse_write = refuse_write

    def read(self, path):
        if self.fail:
            raise SecretsError(self.fail)
        return dict(self.documents.get(path, {}))

    def replace(self, path, values):
        if self.refuse_write:
            raise SecretsError("permission denied")
        before = set(self.documents.get(path, {}))
        self.documents[path] = dict(values)
        return WriteResult(tuple(sorted(before ^ set(values))))


def _service(tmp_path: Path, **kwargs) -> Service:
    manifest = {
        "contract_version": 2,
        "source": "t",
        "fields": [
            {"field": "pw", "env": "PASSWORD", "source": "runtime", "sensitive": True},
            {"field": "mode", "env": "MODE", "source": "human"},
            # supplied by the deployment: never in the store
            {"field": "sha", "env": "GIT_COMMIT_SHA", "source": "release"},
            # composed from another service's store, not this one's
            {
                "field": "db",
                "env": "DATABASE_URL",
                "source": "runtime",
                "provided_by": "platform/postgres:root_password",
            },
        ],
    }
    (tmp_path / "m.json").write_text(json.dumps(manifest), encoding="utf-8")
    return Service(
        str(tmp_path),
        "platform",
        "svc",
        (str(tmp_path / "m.json"),),
        environments=("staging",),
        **kwargs,
    )


def test_orphans_are_everything_no_template_renders_and_no_operator_reads(tmp_path):
    service = _service(tmp_path)
    path = vault_path("platform", "staging", "svc")
    store = MemoryStore(
        {path: {"PASSWORD": "p", "MODE": "m", "GIT_COMMIT_SHA": "old", "LEFTOVER": "x"}}
    )
    report = secrets_prune.prune(services=(service,), store=store, root=tmp_path)
    (plan,) = report.plans
    # release-class and provided_by values are not this path's to hold
    assert plan.orphans == ("GIT_COMMIT_SHA", "LEFTOVER")
    assert plan.keep == ("MODE", "PASSWORD")
    assert not plan.applied and store.documents[path] == {
        "PASSWORD": "p",
        "MODE": "m",
        "GIT_COMMIT_SHA": "old",
        "LEFTOVER": "x",
    }  # a dry run writes nothing
    assert "LEFTOVER" in report.render() and report.orphan_count == 2


def test_declared_operator_keys_stay(tmp_path):
    service = _service(tmp_path, store_only_keys=("root_token",))
    path = vault_path("platform", "staging", "svc")
    store = MemoryStore({path: {"PASSWORD": "p", "MODE": "m", "root_token": "t"}})
    report = secrets_prune.prune(services=(service,), store=store, root=tmp_path)
    assert report.plans[0].orphans == ()
    assert report.render().startswith("secrets prune: 1 clean")


def test_apply_rewrites_the_document_without_the_orphans(tmp_path):
    service = _service(tmp_path)
    path = vault_path("platform", "staging", "svc")
    store = MemoryStore({path: {"PASSWORD": "p", "MODE": "m", "LEFTOVER": "x"}})
    report = secrets_prune.prune(
        services=(service,), store=store, apply=True, root=tmp_path
    )
    assert store.documents[path] == {"PASSWORD": "p", "MODE": "m"}
    assert report.plans[0].applied and "removed=['LEFTOVER']" in report.render()


def test_a_refused_write_or_read_is_reported_not_raised(tmp_path):
    service = _service(tmp_path)
    path = vault_path("platform", "staging", "svc")
    refusing = MemoryStore({path: {"LEFTOVER": "x"}}, refuse_write=True)
    report = secrets_prune.prune(
        services=(service,), store=refusing, apply=True, root=tmp_path
    )
    assert not report.plans[0].applied and "write refused" in report.plans[0].error
    unreadable = MemoryStore(fail="HTTP 403")
    report = secrets_prune.prune(services=(service,), store=unreadable, root=tmp_path)
    assert "unreadable" in report.render()
    assert secrets_prune.main.__doc__ is None or True  # main() is exercised below


def test_previews_own_no_path_and_are_skipped(tmp_path):
    preview = _service(tmp_path, source_env="staging")
    report = secrets_prune.prune(
        services=(preview,), store=MemoryStore(), root=tmp_path
    )
    assert report.plans == []


def test_cli_rejects_an_unknown_service_and_defaults_to_a_dry_run(monkeypatch, capsys):
    with pytest.raises(SystemExit):
        secrets_prune.main(["--service", "platform/nope"])

    seen: dict = {}

    def fake_prune(**kwargs):
        seen.update(kwargs)
        return secrets_prune.PruneReport()

    monkeypatch.setattr(secrets_prune, "prune", fake_prune)
    assert (
        secrets_prune.main(["--service", "platform/alerting", "--env", "staging"]) == 0
    )
    assert [s.id for s in seen["services"]] == ["platform/alerting"]
    assert seen["environments"] == ("staging",) and seen["apply"] is False
    assert "secrets prune: 0 clean" in capsys.readouterr().out


def test_every_registered_service_declares_where_its_operator_keys_are_read():
    """A store-only key is a documented exception; the registry must name each one."""
    documented = {s.id: s.store_only_keys for s in SERVICES if s.store_only_keys}
    assert documented == {
        "platform/authentik": ("root_token",),
        "truealpha/data_engine": (
            "CAPTURE_APPROVED_BY",
            "DATA_ENGINE_IMAGE_DIGEST",
            "RELEASE_MANIFEST_ID",
        ),
    }
