"""The identity map must travel with the image and never drift from the tree (#608).

`platform/12.alerting`'s image COPYs `libs` and `tools` and nothing else, so inside the
running probe container the deploy.py tree is absent and the registry derived from it is
empty. Measured 2026-09-10:

    $ docker exec platform-alerting-probes-staging ls /app
    app.py  libs  tools
    >>> len(service_attrs())                              0
    >>> service_id_for_dokploy("finance_report", "app")   None

Every compose the deploy-queue guard swept resolved to None — 32,236 "unregistered" log
lines in 24 h — and every alert that watcher raises carries `infra/unregistered` instead
of a real service.
"""

from __future__ import annotations

import json


from libs import service_registry
from tools import gen_dokploy_identity_map


def test_the_committed_map_matches_the_deploy_tree():
    """Drift fails here, not at 3am in a container nobody is reading."""
    committed = json.loads(
        service_registry.DOKPLOY_IDENTITY_MAP_PATH.read_text(encoding="utf-8")
    )
    assert committed == gen_dokploy_identity_map.build(), (
        "run `python tools/gen_dokploy_identity_map.py`"
    )


def test_every_registered_service_resolves_from_the_map_alone(monkeypatch):
    """The container's situation: no tree, only what the image carries."""
    from_tree = gen_dokploy_identity_map.build()
    monkeypatch.setattr(service_registry, "service_attrs", dict)

    for coordinate, service_id in from_tree.items():
        project, _, compose = coordinate.partition("/")
        assert service_registry.service_id_for_dokploy(project, compose) == service_id


def test_the_tree_wins_when_it_is_there(monkeypatch, tmp_path):
    """A checkout is always more current than an artifact built from it."""
    stale = tmp_path / "stale.json"
    stale.write_text(json.dumps({"platform/redis": "platform/wrong"}), encoding="utf-8")
    monkeypatch.setattr(service_registry, "DOKPLOY_IDENTITY_MAP_PATH", stale)

    assert (
        service_registry.service_id_for_dokploy("platform", "redis") == "platform/redis"
    )


def test_an_unknown_compose_is_still_unknown(monkeypatch):
    """`None` has to keep meaning 'nobody can account for this', or the whole signal
    goes away — playground/* and finance/appwrite are the real instances."""
    monkeypatch.setattr(service_registry, "service_attrs", dict)

    assert service_registry.service_id_for_dokploy("playground", "TianClaws") is None
    assert service_registry.service_id_for_dokploy("finance", "appwrite") is None


def test_the_generator_check_mode_reports_drift(tmp_path, monkeypatch, capsys):
    drifted = tmp_path / "drifted.json"
    drifted.write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(service_registry, "DOKPLOY_IDENTITY_MAP_PATH", drifted)
    monkeypatch.setattr(gen_dokploy_identity_map, "DOKPLOY_IDENTITY_MAP_PATH", drifted)

    assert gen_dokploy_identity_map.main(["--check"]) == 1
    assert "drifted" in capsys.readouterr().err

    assert gen_dokploy_identity_map.main([]) == 0
    assert gen_dokploy_identity_map.main(["--check"]) == 0
