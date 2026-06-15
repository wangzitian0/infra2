"""Infra-009 / finance_report#921 (D8): teardown-convergence invariant.

The orphan-proof property the unified primitive must hold: a preview alias is
**created under, and pruned by, the same stable key** — the Dokploy compose *record
name* (``compose_name``). Root incident infra2#310 leaked containers because the OLD
path tore a stack down with a direct ``docker compose -p <project> down`` whose project
key DIVERGED from what the stack actually ran under, so the Dokploy record vanished while
the containers (and volumes) survived as untraceable orphans.

REALITY the test must model (observed live): Dokploy does NOT honor the requested
``app_name`` verbatim — it assigns ``appName = <name>-<random-suffix>`` (e.g.
``finance-report-preview-pr-999-02gcji``). So **name != appName**. The teardown is
nonetheless orphan-proof because ``down`` keys off the stable record ``name`` →
``composeId`` and calls Dokploy's ``delete_compose(composeId)``; Dokploy prunes the docker
project under the (suffixed) appName IT assigned. Teardown is therefore appName-agnostic —
nothing keys it off the bare name, which is exactly what avoids the #310 divergence.

An earlier version of this test modelled ``name == appName`` and gave FALSE confidence;
this version models the real suffix so it cannot pass while a real divergence leaks.

No live Dokploy/HTTP: the stateful fake records calls, the http getter is faked.
"""

from __future__ import annotations

import pytest

import tools.preview_lifecycle as pl


class ConvergenceFakeDokploy:
    """A stateful Dokploy double modelling the two layers of the infra2#310 leak.

    - ``records``: Dokploy compose records keyed by ``name`` — the stable key ``down``
      searches via ``find_compose_by_name``.
    - ``docker_projects``: the docker project a deployed compose runs under, keyed by the
      ``appName`` Dokploy ASSIGNS at create — which is ``name`` + a random suffix, NOT the
      requested ``app_name``. ``delete_compose`` (by composeId) prunes the project under
      that assigned appName, mirroring how Dokploy's own teardown works.

    A leak (#310) appears as a ``docker_projects`` entry that survives ``down`` — which
    happens only if teardown keys off something other than the composeId/assigned-appName.
    """

    def __init__(self, *, environment_id="env-preview", github_id="gh-1"):
        self._environment_id = environment_id
        self._github_id = github_id
        self.records: dict[str, dict] = {}  # name -> {composeId, appName(suffixed)}
        self.docker_projects: dict[str, str] = {}  # assigned appName -> composeId
        self.volumes: dict[str, str] = {}  # assigned appName -> composeId (ephemeral DB)
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
    def create_compose(self, environment_id, name, *, app_name=None, **kwargs):
        self._seq += 1
        compose_id = f"cmp-{self._seq}"
        # FAITHFUL to live Dokploy: ignore the requested app_name's exactness and assign
        # appName = name + a suffix. name != appName by construction — the very thing the
        # old test failed to model.
        assigned_app_name = f"{name}-{self._seq:02d}sfx"
        self.records[name] = {"composeId": compose_id, "appName": assigned_app_name}
        self.docker_projects[assigned_app_name] = compose_id
        self.volumes[assigned_app_name] = compose_id  # the alias's ephemeral DB volume
        return {"composeId": compose_id}

    def update_compose(self, compose_id, **kwargs):
        return {}

    def update_compose_env(self, compose_id, env_vars=None, env_str=None):
        return {}

    def deploy_compose(self, compose_id):
        return {}

    def delete_compose(self, compose_id, *, delete_volumes=False):
        # Dokploy deletes by composeId and prunes the docker project under the appName IT
        # assigned — so teardown is agnostic to the name/appName divergence. The
        # project/containers come down ALWAYS; `delete_volumes` only additionally removes
        # the named volume (the ephemeral DB), so model the two layers separately.
        for name, rec in list(self.records.items()):
            if rec["composeId"] == compose_id:
                self.docker_projects.pop(rec["appName"], None)  # containers: always
                if delete_volumes:
                    self.volumes.pop(rec["appName"], None)  # volume: only when asked
                del self.records[name]
                break

    # --- assertions --------------------------------------------------------
    def survivors(self) -> dict[str, str]:
        """Docker projects still running — non-empty means a leaked orphan."""
        return dict(self.docker_projects)


def _ok_get(url, timeout):
    return 200, "ok"


_ALIASES = [("branch", "main"), ("pr", 7), ("commit", "1ab32d5")]


@pytest.mark.parametrize("kind,value", _ALIASES)
def test_record_keyed_by_stable_name_while_dokploy_mangles_appname(kind, value):
    # AC #921.1: the Dokploy record is created under the stable `compose_name`, while the
    # docker project runs under Dokploy's SUFFIXED appName (name != appName, per reality).
    client = ConvergenceFakeDokploy()
    pl.up(
        kind, value, code="main", domain="zitian.party", client=client, http_get=_ok_get
    )

    alias = pl.preview_alias(kind, value)
    assert alias.compose_name in client.records  # record under the stable name
    rec = client.records[alias.compose_name]
    # Dokploy mangled the appName — it is NOT equal to the compose_name (the old test's
    # false assumption), but it IS derived from it (suffix), and there is exactly one
    # running docker project, under that assigned appName.
    assert rec["appName"] != alias.compose_name
    assert rec["appName"].startswith(alias.compose_name + "-")
    assert client.docker_projects == {rec["appName"]: rec["composeId"]}


@pytest.mark.parametrize("kind,value", _ALIASES)
def test_down_leaves_zero_orphans_despite_appname_suffix(kind, value):
    # AC #921.2: down finds by the stable name -> composeId and deletes via Dokploy, which
    # prunes the suffixed docker project. Zero survivors at BOTH layers, even though
    # name != appName — the real orphan-proof guarantee.
    client = ConvergenceFakeDokploy()
    pl.up(
        kind, value, code="main", domain="zitian.party", client=client, http_get=_ok_get
    )
    pl.down(kind, value, domain="zitian.party", client=client)

    assert client.records == {}  # Dokploy record gone
    assert client.survivors() == {}  # no orphaned docker project — the #310 guarantee
    assert client.volumes == {}  # ephemeral DB volume gone (down passes delete_volumes)


def test_teardown_is_composeid_based_not_name_based():
    # The guarantee is that teardown routes through composeId (appName-agnostic), NOT a
    # direct `docker -p <compose_name> down`. Prove the harness has teeth: a stray project
    # under the BARE compose_name (what the #310 direct-docker path would have used) is
    # NOT what the current stack runs under, so it would survive — i.e. keying teardown
    # off the bare name (the #310 bug) leaks. The real lifecycle never does this.
    client = ConvergenceFakeDokploy()
    pl.up("pr", 7, code="main", domain="zitian.party", client=client, http_get=_ok_get)
    bare_name = pl.preview_alias("pr", 7).compose_name
    client.docker_projects[bare_name] = "cmp-stray"  # a #310-style divergent project
    pl.down("pr", 7, domain="zitian.party", client=client)

    # the real (suffixed) project is gone; the bare-name stray survives — proving the
    # model distinguishes the two keys and that composeId-teardown is what saves us.
    assert client.survivors() == {bare_name: "cmp-stray"}
