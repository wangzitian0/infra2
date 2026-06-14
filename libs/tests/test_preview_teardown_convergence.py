"""Infra-009 / finance_report#921 (D8): teardown-convergence invariant.

The orphan-proof property the unified primitive must hold: a preview alias is
**created under, and pruned by, the same key**. Root incident infra2#310 leaked
containers precisely because the two diverged — the stack came up under a docker
project name (``finance_report_pr_<n>``) while ``compose.delete`` tore down with
``docker compose -p <appName> down`` keyed off a *different* ``appName``, so the
Dokploy record vanished while the containers (and their volumes) survived as
untraceable orphans the API reconcile could no longer see.

These tests lock the invariant against regression by modelling BOTH layers Dokploy
touches — the compose *record* (keyed by ``name``, what ``down`` searches) and the
docker *project* teardown (keyed by ``appName``) — and asserting that one alias key
addresses both, so ``up`` then ``down`` leaves zero survivors at either layer.

No live Dokploy/HTTP: the stateful fake records calls, the http getter is faked.
"""

from __future__ import annotations

import pytest

import tools.preview_lifecycle as pl


class ConvergenceFakeDokploy:
    """A stateful Dokploy double that models the two teardown layers of infra2#310.

    - ``records``: Dokploy compose records keyed by ``name`` — what ``down`` searches
      via ``find_compose_by_name``.
    - ``docker_projects``: the docker project a deployed compose runs under, keyed by
      the ``appName`` Dokploy assigns at create. ``delete_compose`` models
      ``docker compose -p <appName> down`` — it can only prune the project under the
      appName bound to that compose at create time.

    A leak (#310) appears as a ``docker_projects`` entry that survives ``down`` because
    the create-time project key and the prune-time key diverged.
    """

    def __init__(self, *, environment_id="env-preview", github_id="gh-1"):
        self._environment_id = environment_id
        self._github_id = github_id
        self.records: dict[str, dict] = {}  # name -> {composeId, appName}
        self.docker_projects: dict[str, str] = {}  # appName -> composeId (running)
        self._seq = 0

    # --- read side ---------------------------------------------------------
    def get_github_provider_id(self):
        return self._github_id

    def get_environment_id(self, project, env_name=None):
        return self._environment_id

    def ensure_environment(self, project, env_name, description=""):
        return {"environmentId": self._environment_id, "name": env_name}, False

    def find_compose_by_name(self, name, project_name=None, env_name=None):
        # The source env's app compose supplies the AppRole creds previews reuse.
        if name == "app" and env_name == "staging":
            return {"composeId": "cmp-source-app"}
        return self.records.get(name)

    def get_compose_env(self, compose_id):
        if compose_id == "cmp-source-app":
            return "VAULT_ADDR=https://v\nVAULT_ROLE_ID=r\nVAULT_SECRET_ID=s\n"
        return ""

    # --- write side --------------------------------------------------------
    def create_compose(self, environment_id, name, *, app_name, **kwargs):
        self._seq += 1
        compose_id = f"cmp-{self._seq}"
        # Dokploy stores the record under `name` and runs the stack's docker project
        # under `app_name`. The convergence invariant is name == app_name.
        self.records[name] = {"composeId": compose_id, "appName": app_name}
        self.docker_projects[app_name] = compose_id
        return {"composeId": compose_id}

    def update_compose(self, compose_id, **kwargs):
        return {}

    def update_compose_env(self, compose_id, env_vars=None, env_str=None):
        return {}

    def deploy_compose(self, compose_id):
        return {}

    def delete_compose(self, compose_id, *, delete_volumes=False):
        # Remove the Dokploy record, then `docker compose -p <appName> down`: prune the
        # docker project keyed by the appName bound to this compose at create time.
        app_name = None
        for name, rec in list(self.records.items()):
            if rec["composeId"] == compose_id:
                app_name = rec["appName"]
                del self.records[name]
                break
        if delete_volumes and app_name is not None:
            self.docker_projects.pop(app_name, None)

    # --- assertions --------------------------------------------------------
    def survivors(self) -> dict[str, str]:
        """Docker projects still running — non-empty means a leaked orphan."""
        return dict(self.docker_projects)


def _ok_get(url, timeout):
    return 200, "ok"


_ALIASES = [("main", None), ("pr", 7), ("commit", "1ab32d5")]


@pytest.mark.parametrize("kind,value", _ALIASES)
def test_up_creates_record_and_docker_project_under_one_key(kind, value):
    # AC #921.1: create-under key — name, appName, and the docker project are the SAME
    # alias key. Divergence here is exactly the infra2#310 root.
    client = ConvergenceFakeDokploy()
    pl.up(
        kind, value, code="main", domain="zitian.party", client=client, http_get=_ok_get
    )

    alias = pl.preview_alias(kind, value)
    assert alias.compose_name in client.records
    rec = client.records[alias.compose_name]
    assert rec["appName"] == alias.compose_name  # name == appName (the invariant)
    assert client.docker_projects == {alias.compose_name: rec["composeId"]}


@pytest.mark.parametrize("kind,value", _ALIASES)
def test_down_prunes_by_the_same_key_leaving_zero_orphans(kind, value):
    # AC #921.2: prune-by key — down finds-and-deletes by the same alias key, so after
    # teardown there is no surviving Dokploy record AND no surviving docker project.
    client = ConvergenceFakeDokploy()
    pl.up(
        kind, value, code="main", domain="zitian.party", client=client, http_get=_ok_get
    )
    pl.down(kind, value, domain="zitian.party", client=client)

    assert client.records == {}  # Dokploy record gone
    assert client.survivors() == {}  # no orphaned docker project — the #310 guarantee


def test_divergent_appname_would_orphan_proving_the_test_has_teeth():
    # Sanity that the model can detect #310: if a stack ran under a docker project name
    # different from the appName that delete prunes by, down() leaves an orphan. The
    # real lifecycle never does this (name == app_name above) — this asserts the harness
    # would fail loudly if a future change reintroduced the divergence.
    client = ConvergenceFakeDokploy()
    pl.up("pr", 7, code="main", domain="zitian.party", client=client, http_get=_ok_get)
    # Simulate the #310 divergence: a stray project under a non-appName key.
    client.docker_projects["finance_report_pr_7"] = "cmp-stray"
    pl.down("pr", 7, domain="zitian.party", client=client)

    assert client.survivors() == {"finance_report_pr_7": "cmp-stray"}  # orphan detected
