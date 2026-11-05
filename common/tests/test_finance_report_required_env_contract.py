"""Every env name that finance_report declares must reach its backend container (#1466).

finance_report refuses to start when its environment lacks a value for a name that its
env manifest declares as injected (`Bootloader._check_static_config`, finance_report
`apps/backend/src/boot.py`, commit 79138919). The preview compose gave
`S3_PUBLIC_ENDPOINT` an empty value and gave no `PREFECT_API_URL`. The canary of every
infra2 PR failed on that.

The declared names come from the stack manifest `10.app/env.manifest.json`. That file is
infra2's copy of the finance_report manifest `common/runtime/required-env.generated.json`.
The deploy preflight (`common.deploy.app_contract.unmet`) refuses a release that declares
a field this file lacks. This test then holds the other half: each compose file must
supply each declared field with a non-empty value.

Refresh: when the preflight names a new field, add it to `env.manifest.json`. This test
then fails until the compose files supply it. No test here reads the network.

The check below is the finance_report check:
- An empty or blank value counts as absent.
- A field with `empty_ok` is exempt only when the variable is unset. A variable that is set
  to an empty string fails (finance_report uses `setdefault`, which keeps the empty string).
- A field with source `code` has a default in the image. The test cannot read that default.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import pytest
from infra2_sdk.runtime.config_schema import EnvironmentManifest, assert_environment

from common.deploy import preview as preview_deploy
from common.deploy import promote as promote_deploy
from common.deploy.env_config import app_compose_env_config, preview_alias
from common.tests.compose_env import container_env, resolve

ROOT = Path(__file__).resolve().parents[2]
APP_DIR = ROOT / "finance_report/finance_report/10.app"
PREVIEW_DIR = ROOT / "finance_report/finance_report/preview"
STACK_MANIFEST = APP_DIR / "env.manifest.json"
SERVICE = "finance_report/app"
DOMAIN = "zitian.party"
IMAGE_TAG = "7913891"
IAC_REF = "a" * 40
#: Hosts of the platform Prefect servers that run real flows (production and staging).
REAL_PREFECT_HOSTS = frozenset(
    {"platform-prefect-server", "platform-prefect-server-staging"}
)


def _unconditional_vault_names(template: Path) -> set[str]:
    """Names that the Vault Agent template renders on a line of their own."""
    return set(re.findall(r"^([A-Z][A-Z0-9_]*)=", template.read_text(), re.MULTILINE))


def _container_environ(
    compose: Path, service: str, template: Path, variables: dict[str, str]
) -> dict[str, str]:
    """The environment of one container: compose values, then Vault values (they win)."""
    environ = container_env(compose, service, variables)
    # A Vault value is never empty: the template skips an empty value.
    environ.update({name: "<vault>" for name in _unconditional_vault_names(template)})
    return environ


def _manifest_dict(manifest_path: Path = STACK_MANIFEST) -> dict:
    return json.loads(manifest_path.read_text(encoding="utf-8"))


def _boot_environ(
    environ: dict[str, str], manifest_path: Path = STACK_MANIFEST
) -> dict[str, str]:
    """Apply the finance_report defaults before its check (`_assert_sdk_environment`)."""
    manifest = EnvironmentManifest.from_dict(_manifest_dict(manifest_path))
    booted = dict(environ)
    for field in manifest.fields:
        if field.injected and (field.empty_ok or field.source == "code"):
            booted.setdefault(field.env, "<default>")
    return booted


def missing_names(
    environ: dict[str, str], manifest_path: Path = STACK_MANIFEST
) -> list[str]:
    """Names that finance_report would refuse to start without, for this environment."""
    try:
        assert_environment(
            manifest_path,
            environ=_boot_environ(environ, manifest_path),
            require_injected=True,
        )
    except RuntimeError as refusal:
        found = re.search(r"declares: (.+?) -- names only", str(refusal))
        assert found, str(refusal)
        return found.group(1).split(", ")
    return []


def _fixed_variables(env: str) -> dict[str, str]:
    deploy_environment = {"prod": "production"}.get(env, env)
    variables, _ = promote_deploy._build_deploy_env_vars(
        service=SERVICE,
        env=env,
        domain=DOMAIN,
        deploy_environment=deploy_environment,
        cfg=app_compose_env_config(SERVICE, env),
        image_tag=IMAGE_TAG,
        iac_ref=IAC_REF,
        config_hash="deploy-contract-test",
    )
    return variables


def _preview_variables(kind: str, value: str | int) -> dict[str, str]:
    return preview_deploy._preview_env_vars(
        preview_alias(kind, value),
        service=SERVICE,
        sha=IMAGE_TAG + "0" * 33,
        domain=DOMAIN,
        iac_ref=IAC_REF,
    )


@dataclass(frozen=True)
class BootPath:
    """One way that the finance_report backend container gets its environment."""

    name: str
    compose: Path
    template: Path
    variables: Callable[[], dict[str, str]]

    def environ(self) -> dict[str, str]:
        return _container_environ(
            self.compose, "backend", self.template, self.variables()
        )


#: Every infra2 deploy path that starts the finance_report backend, which runs the check.
#: The `prefect-worker` service runs no check: only `src/main.py` calls the Bootloader.
BOOT_PATHS = (
    BootPath(
        "staging",
        APP_DIR / "compose.yaml",
        APP_DIR / "secrets.ctmpl",
        lambda: _fixed_variables("staging"),
    ),
    BootPath(
        "production",
        APP_DIR / "compose.yaml",
        APP_DIR / "secrets.ctmpl",
        lambda: _fixed_variables("prod"),
    ),
    BootPath(
        "canary",
        PREVIEW_DIR / "compose.yaml",
        PREVIEW_DIR / "secrets.ctmpl",
        lambda: _preview_variables("canary", "preview"),
    ),
    BootPath(
        "preview-pr",
        PREVIEW_DIR / "compose.yaml",
        PREVIEW_DIR / "secrets.ctmpl",
        lambda: _preview_variables("pr", 5),
    ),
    BootPath(
        "preview-main",
        PREVIEW_DIR / "compose.yaml",
        PREVIEW_DIR / "secrets.ctmpl",
        lambda: _preview_variables("branch", "main"),
    ),
)


@pytest.mark.parametrize("path", BOOT_PATHS, ids=lambda path: path.name)
def test_every_declared_name_reaches_the_backend_container(path: BootPath) -> None:
    missing = missing_names(path.environ())
    assert not missing, (
        f"{path.name}: finance_report refuses to start; the environment lacks "
        f"{', '.join(missing)}. Supply a non-empty value in {path.compose.name}."
    )


def test_the_resolved_values_are_the_expected_ones() -> None:
    """Pin the values, not only "not empty": a wrong resolution must not pass."""
    by_name = {path.name: path.environ() for path in BOOT_PATHS}
    assert by_name["canary"]["S3_PUBLIC_ENDPOINT"] == "https://s3.zitian.party"
    assert by_name["canary"]["API_RATE_LIMIT_REQUESTS"] == "300"
    assert by_name["canary"]["PREFECT_API_URL"] == (
        "http://platform-prefect-server-canary-preview:4200/api"
    )
    assert by_name["production"]["PREFECT_API_URL"] == (
        "http://platform-prefect-server:4200/api"
    )
    assert by_name["production"]["S3_PUBLIC_BUCKET"] == "statements"
    assert by_name["staging"]["S3_PUBLIC_BUCKET"] == "finance-report-staging"
    assert by_name["staging"]["PREFECT_API_URL"] == (
        "http://platform-prefect-server-staging:4200/api"
    )


def test_the_contract_covers_the_names_that_broke_the_canary() -> None:
    """Guard against a vacuous pass: the manifest must declare the names that failed."""
    manifest = EnvironmentManifest.from_dict(_manifest_dict())
    injected = [field for field in manifest.fields if field.injected]
    required = {f.env for f in injected if not f.empty_ok and f.source != "code"}
    empty_ok = {f.env for f in injected if f.empty_ok}
    assert {"PREFECT_API_URL", "S3_PUBLIC_ENDPOINT"} <= required
    assert "S3_PUBLIC_BUCKET" in empty_ok
    assert len(required) >= 15


def _complete_environ() -> dict[str, str]:
    """An environment with a value for every injected name, independent of any compose."""
    manifest = EnvironmentManifest.from_dict(_manifest_dict())
    return {field.env: "value" for field in manifest.fields if field.injected}


def test_the_check_treats_an_empty_value_as_absent() -> None:
    environ = _complete_environ()
    assert missing_names(environ) == []
    assert missing_names({**environ, "PREFECT_API_URL": ""}) == ["PREFECT_API_URL"]
    assert missing_names({**environ, "PREFECT_API_URL": "  "}) == ["PREFECT_API_URL"]
    environ.pop("PREFECT_API_URL")
    assert missing_names(environ) == ["PREFECT_API_URL"]


def test_an_empty_ok_name_may_be_unset_but_not_set_to_an_empty_string() -> None:
    """finance_report keeps a set-but-empty value, so `empty_ok` does not cover it."""
    environ = _complete_environ()
    assert missing_names({**environ, "S3_PUBLIC_BUCKET": ""}) == ["S3_PUBLIC_BUCKET"]
    environ.pop("S3_PUBLIC_BUCKET")
    assert missing_names(environ) == []


def test_a_code_sourced_name_may_be_unset() -> None:
    environ = _complete_environ()
    environ.pop("AI_PROVIDER")
    assert missing_names(environ) == []


def test_a_new_declared_name_fails_every_boot_path(tmp_path: Path) -> None:
    """A name that the manifest adds and no compose supplies makes each path fail."""
    manifest = _manifest_dict()
    manifest["fields"].append(
        {
            **manifest["fields"][0],
            "field": "new_name",
            "env": "NEW_NAME",
            "aliases": [],
            "source": "decision",
            "injected": True,
            "empty_ok": False,
            "store_key": "",
        }
    )
    changed = tmp_path / "env.manifest.json"
    changed.write_text(json.dumps(manifest), encoding="utf-8")
    for path in BOOT_PATHS:
        assert missing_names(path.environ(), changed) == ["NEW_NAME"], path.name


@pytest.mark.parametrize(
    "text, variables, expected",
    [
        ("${A}", {"A": "x"}, "x"),
        ("${A:-d}", {}, "d"),
        ("${A:-d}", {"A": ""}, "d"),
        ("${A:-}", {}, ""),
        ("${A:-https://s3.${B}}", {"B": "zitian.party"}, "https://s3.zitian.party"),
        ("${A:-${B:-z}}", {}, "z"),
    ],
)
def test_the_shared_resolver_matches_docker_compose(
    text: str, variables: dict[str, str], expected: str
) -> None:
    assert resolve(text, variables) == expected


@pytest.mark.parametrize(
    "variables",
    [
        lambda: _preview_variables("canary", "preview"),
        lambda: _preview_variables("pr", 5),
        lambda: _preview_variables("branch", "main"),
        lambda: _preview_variables("branch", "staging"),
        lambda: _preview_variables("tag", "v1.2.3"),
    ],
)
def test_a_preview_never_names_a_real_platform_prefect_server(
    variables: Callable[[], dict[str, str]],
) -> None:
    """Setting PREFECT_API_URL turns on Prefect dispatch in finance_report.

    A preview has no Prefect server and no worker. A host that does not exist makes the
    dispatch fail at once; finance_report then parses in-process. A real server would
    take the flow run of a preview upload and run it against another database.
    """
    environ = _container_environ(
        PREVIEW_DIR / "compose.yaml",
        "backend",
        PREVIEW_DIR / "secrets.ctmpl",
        variables(),
    )
    url = environ["PREFECT_API_URL"]
    host = re.fullmatch(r"http://([a-z0-9-]+):4200/api", url)
    assert host, f"unexpected PREFECT_API_URL shape: {url}"
    assert host.group(1) not in REAL_PREFECT_HOSTS, url
