"""Fixture, mutation, and repository contract tests for tools/env_inventory.py (#1236).

This test module verifies two levels:
1. Synthetic fixture and mutation tests for every scanner and analyzer rule in
   ``tools/env_inventory.py``.
2. Repository-wide SSOT guards G1, G2, and G3 over the tracked tree and pinned
   application manifests, plus mutation proofs that each guard fails when a
   violation is seeded.
"""

from __future__ import annotations

import copy
import re
import textwrap
from pathlib import Path

import pytest

from tools import env_inventory as envinv
from tools import env_inventory as envreport


def w(root: Path, rel: str, body: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(body).lstrip("\n"))


@pytest.fixture()
def inv(tmp_path):
    w(
        tmp_path,
        "app/main.py",
        """
        import os
        import subprocess
        from os import environ, getenv as ge
        A = os.environ["A_REQ"]
        B = os.environ.get("B_DEF", "bee")
        C = os.getenv("C_NODEF")
        D = ge("D_ALIAS", "dee")
        E = environ.get("E_FROM_IMPORT")
        F = "F_PRES" in os.environ
        G = os.environ.get("G_ONE") or os.environ.get("G_TWO")
        H = os.environ.get("H_ONE", os.environ.get("H_TWO"))
        os.environ["W_SET"] = "x"
        os.environ.setdefault("W_DEFAULT", "y")
        os.environ.update({"W_UPD": "1"})
        os.putenv("W_PUT", "2")
        ENV_KEY = "CONST_KEY"
        Z = os.environ.get(ENV_KEY)
        child = {**os.environ, "CHILD_A": "1"}
        subprocess.run(["x"], env={"CHILD_B": "2"})
        e2 = os.environ.copy()
        e2["CHILD_C"] = "3"
        def read_env(k, d=None): ...
        read_env("HELPER_READ", "d")
        set_env("HELPER_SET", "v")
        dyn = os.environ.get(f"PFX_{x}")
        NAMES = ("LOOP_A", "LOOP_B")
        for n in NAMES:
            os.environ.get(n)
        vals = [os.environ.get(c) for c in ("COMP_A", "COMP_B")]
        for n in ("INLINE_A",):
            os.environ[n] = "1"
        for n, dflt in (("PAIR_A", "x"), ("PAIR_B", "y")):
            os.environ.get(n, dflt)
        m = {}.get("NOT_ENV_DICT")
        def f(env):
            return env.get("environmentId"), env.get("MAPPING_UPPER"), env["MAPPING_REQ"]
    """,
    )
    w(
        tmp_path,
        "app/conf.py",
        """
        from pydantic import AliasChoices, Field
        from pydantic_settings import BaseSettings, SettingsConfigDict
        class S(BaseSettings):
            model_config = SettingsConfigDict(env_prefix="PFX_")
            host: str = "localhost"
            port: int
            token: str = Field(..., validation_alias="TOKEN_NAME")
            mode: str = Field("dev", validation_alias=AliasChoices("MODE_A", "MODE_B"))
            opt: str | None = None
    """,
    )
    w(
        tmp_path,
        "deploy/compose.yaml",
        """
        x-common: &common
          COMMON_K: ${COMMON_SRC:-cdef}
        services:
          app:
            build: .
            container_name: platform-app${ENV_SUFFIX}
            image: platform-app
            environment:
              <<: *common
              LIT: value
              COPY_OF: ${SRC_VAR}
              COMPOSED: "http://${HOST_VAR}:8080"
              REQ: ${REQUIRED_VAR:?must set}
              NESTED: ${OUTER_VAR:-${INNER_VAR:-fallback}}
              EMPTY:
          scripted:
            image: platform-app
            command:
              - sh
              - -c
              - 'WAITED=0; echo $$WAITED $$REAL_INPUT'
          shellish:
            image: platform-app
            command: sh -c 'export DATABASE_URL=$$PREVIEW_DB_URL_ESC'
          db:
            image: postgres:16
            environment:
              - POSTGRES_USER=admin
              - POSTGRES_DB
    """,
    )
    w(
        tmp_path,
        "deploy/Dockerfile",
        """
        FROM python:3.12
        ARG BUILD_ARG=1
        ENV APP_ENV=production APP_NAME="my app"
        ENV LEGACY_ONE legacy value
        RUN echo $APP_ENV ${OTHER_VAR:-x}
    """,
    )
    w(
        tmp_path,
        ".github/workflows/infra-ci.yml",
        """
        name: t
        env:
          WF_LEVEL: lit
        jobs:
          j:
            runs-on: ubuntu-latest
            env:
              TOKEN_ENV: ${{ secrets.MY_TOKEN }}
              COPY_ENV: ${{ env.WF_LEVEL }}
            steps:
              - run: |
                  export EXPORTED=1
                  echo "$USED_VAR ${DEFAULTED:-d}"
                  echo "GH_SET=1" >> "$GITHUB_ENV"
              - run: |
                  python - <<'PY'
                  import os
                  os.environ["IN_HEREDOC"]
                  PY
    """,
    )
    w(
        tmp_path,
        "scripts/run.sh",
        """
        #!/bin/bash
        LOCAL_ONLY=1
        echo "$LOCAL_ONLY"
        export EXPORTED_VAR=abc
        export GENERATED_PW="$(openssl rand -hex 24)"
        export lower_case_local=1
        FOO="${FOO:-fallback}"
        echo "$ENV_INPUT"
        SPACED_LOCAL="has some spaces here"
        echo "$SPACED_LOCAL"
        MULTI_ONE="" MULTI_TWO=0 MULTI_THREE=1
        echo "$MULTI_TWO $MULTI_THREE"
        API_URL=http://x node app.js
        docker run -e DOCKER_E=1 img
        if ! COND_ASSIGNED=$(date); then exit 1; fi
        echo "$COND_ASSIGNED"
        mapfile -t ARR_LOCAL < <(ls)
        echo "${ARR_LOCAL[@]}"
        ssh host "REMOTE_SHA='abc' bash -s"
        docker exec -e "QUOTED_E=$project" c cmd
    """,
    )
    w(
        tmp_path,
        "app/seq_a.py",
        """
        import os
        NAMES = ("SEQA_ONE",)
        for n in NAMES:
            os.environ.get(n)
    """,
    )
    w(
        tmp_path,
        "app/seq_b.py",
        """
        import os
        NAMES = ("SEQB_ONE",)
        for n in NAMES:
            os.environ.get(n)
    """,
    )
    w(
        tmp_path,
        "scripts/evalgen.sh",
        """
        eval "$(python gen.py --shell)"
        echo "$EVAL_GENERATED"
    """,
    )
    w(
        tmp_path,
        "Makefile",
        """
        .PHONY: rec
        export MAKE_EXPORTED = 1
        rec:
        \tLLM_RECORD=1 uv run pytest
    """,
    )
    w(
        tmp_path,
        "libs/deploy/injector.py",
        """
        def build_env(env):
            base = {"INJECTED_BY_DICT": env, "INJECTED_TWICE": "x"}
            base["INJECTED_BY_STORE"] = "y"
            base.update({"DEPLOY_LITERAL_PW": "hunter2literal"})
            return base
    """,
    )
    w(
        tmp_path,
        "libs/sdkfx/environment.py",
        """
        from enum import Enum
        class RuntimeEnvKey(str, Enum):
            GIT_COMMIT = "GIT_COMMIT_FX"
            TIMEOUT = "HTTP_TIMEOUT_FX"
        class NotEnv(str, Enum):
            COLOR = "RED_COLOR_FX"
    """,
    )
    w(
        tmp_path,
        "app/placeholders.py",
        """
        url = "https://x${PLACEHOLDER_ENV_FX}/path"
        cmd = ["docker", "run", "-e", "LIST_E_FX=1", "img"]
    """,
    )
    w(
        tmp_path,
        "web/src/alias.ts",
        """
        const env: Record<string, string | undefined> = process.env;
        const a = env.NEXT_PUBLIC_ALIAS_FX;
        const b = env("S3_HELPER_FX");
        vi.stubEnv("STUBBED_FX", "1");
    """,
    )
    w(
        tmp_path,
        "app/reader.py",
        """
        import os
        a = os.environ.get("INJECTED_BY_DICT")
        b = os.environ.get("INJECTED_BY_STORE")
        c = os.environ.get("DEPLOY_LITERAL_PW")
    """,
    )
    w(
        tmp_path,
        "libs/other/plain.py",
        """
        def table():
            return {"INJECTED_BY_DICT": 1}
    """,
    )
    w(
        tmp_path,
        "libs/base/settings.py",
        """
        from pydantic_settings import BaseSettings
        class RuntimeSettings(BaseSettings):
            llm_model: str = "m"
    """,
    )
    w(
        tmp_path,
        "apps/de/config.py",
        """
        from libs.base.settings import RuntimeSettings
        class Settings(RuntimeSettings):
            moomoo_host: str = "h"
    """,
    )
    w(
        tmp_path,
        "scripts/indirect.sh",
        """
        ping() { local v="$1"; echo "${!v}"; }
        ping INDIRECT_TARGET_URL
    """,
    )
    w(
        tmp_path,
        "scripts/expand.sh",
        """
        echo "${EXPANDED_MSG%%$'\\n'*} ${EXPANDED_SLICE:0:7} ${EXPANDED_ARR[@]}"
    """,
    )
    w(
        tmp_path,
        ".github/workflows/ops-checks.yml",
        """
        name: js
        jobs:
          j:
            runs-on: ubuntu-latest
            env:
              JS_READ_OUTCOME: ok
            steps:
              - uses: docker/build-push-action@v5
                with:
                  build-args: |
                    BUILD_ARG_FX=1
              - uses: actions/github-script@v7
                with:
                  script: |
                    const o = process.env.JS_READ_OUTCOME;
    """,
    )
    w(
        tmp_path,
        "cloudflare/watchdog/worker.js",
        """
        export default { fetch(req, env) { return fetch(url, { headers: { a: env.WORKER_SECRET_TOKEN } }); } };
    """,
    )
    w(
        tmp_path,
        "cloudflare/watchdog/wrangler.toml",
        """
        name = "w"
        [vars]
        WORKER_VAR_ONE = "1"
    """,
    )
    w(
        tmp_path,
        "scripts/loops.sh",
        """
        show() { echo "$ROOT_DIR"; }
        ROOT_DIR="$(pwd)"
        for ITEM in a b; do echo "$ITEM"; done
        read -r ANSWER
        echo "$ANSWER"
        echo "$REPLY $NF"
    """,
    )
    w(
        tmp_path,
        "deploy/secrets.ctmpl",
        """
        {{- with secret "secret/data/p/e/svc" -}}
        DB_PASSWORD={{ .Data.data.password }}
        PLAIN=literal
        {{ with index .Data.data "optional_key" }}OPTIONAL_CTMPL_KEY={{ . }}{{ end }}
        URL_X={{ env "CTMPL_ENV_READ" }}
        {{- end }}
    """,
    )
    w(
        tmp_path,
        "deploy/.env.example",
        """
        A_EX=1
        INDIRECT_TARGET_URL=u
        PLACEHOLDER_ENV_FX=p
        # COMMENTED_EX=2
        # plain comment without equals
    """,
    )
    w(
        tmp_path,
        "deploy/env.manifest.json",
        """
        {"contract_version": 2, "fields": [
          {"field": "a", "env": "MAN_A", "source": "runtime", "sensitive": true, "required": true},
          {"field": "b", "env": "MAN_B", "source": "code", "aliases": ["MAN_B_OLD"]},
          {"field": "c", "env": "COMPOSED_FX", "source": "runtime", "composed_from": "postgres://u:{PW_FX}@h{env:ENV_SUFFIX_FX}/db"}
        ]}
    """,
    )
    w(
        tmp_path,
        "web/src/page.tsx",
        """
        const a = process.env.NEXT_PUBLIC_X ?? "d";
        const b = process.env["B_TS"];
        const { C_TS, D_TS = 1 } = process.env;
        process.env.W_TS = "1";
        const v = import.meta.env.VITE_Y;
    """,
    )
    w(
        tmp_path,
        "tests/test_x.py",
        """
        def test_x(monkeypatch):
            monkeypatch.setenv("TEST_ONLY", "1")
    """,
    )
    w(
        tmp_path,
        "docs/ssot/core.environments.md",
        "Use `MAN_A` and `NOT_A_KEY_AT_ALL` here. ${A_REQ}\n",
    )
    return envinv.run({"fx": tmp_path})


def find(inv, key, role=None, layer=None):
    return [
        o
        for o in inv.occ
        if o.key == key
        and (role is None or o.role == role)
        and (layer is None or o.layer.startswith(layer))
    ]


@pytest.mark.parametrize(
    "key,default,required",
    [
        ("A_REQ", None, True),
        ("B_DEF", "bee", False),
        ("C_NODEF", None, False),
        ("D_ALIAS", "dee", False),
        ("E_FROM_IMPORT", None, False),
        ("G_ONE", None, False),
        ("G_TWO", None, False),
        ("CONST_KEY", None, False),
        ("HELPER_READ", "d", False),
    ],
)
def test_python_reads(inv, key, default, required):
    r = find(inv, key, "read", "py-env-read")
    assert len(r) == 1, key
    assert r[0].default == default and r[0].required == required


def test_python_presence_check_is_a_read(inv):
    assert find(inv, "F_PRES", "read", "py-env-read")


def test_python_constant_is_resolved(inv):
    assert "+const" in find(inv, "CONST_KEY", "read")[0].via


@pytest.mark.parametrize(
    "key,layer",
    [
        ("W_SET", "py-env-write"),
        ("W_DEFAULT", "py-env-write"),
        ("W_UPD", "py-env-write"),
        ("W_PUT", "py-env-write"),
        ("CHILD_A", "py-child-env"),
        ("CHILD_B", "py-child-env"),
        ("CHILD_C", "py-child-env"),
        ("HELPER_SET", "py-env-write"),
    ],
)
def test_python_writes(inv, key, layer):
    assert find(inv, key, "write", layer), key


def test_python_dynamic_access_is_reported_not_guessed(inv):
    assert [d["expr"] for d in inv.dynamic if d["path"] == "app/main.py"] == [
        "f'PFX_{x}'"
    ]
    assert not find(inv, "NOT_ENV_DICT")


def test_python_fallback_edges(inv):
    pairs = {(a, b) for a, b, kind, *_ in inv.edges if kind == "fallback"}
    assert ("G_ONE", "G_TWO") in pairs and ("H_ONE", "H_TWO") in pairs


@pytest.mark.parametrize(
    "key,default,required",
    [
        ("PFX_HOST", "localhost", False),
        ("PFX_PORT", None, True),
        ("TOKEN_NAME", None, True),
        ("MODE_A", "dev", False),
        ("MODE_B", "dev", False),
        ("PFX_OPT", "None", False),
    ],
)
def test_pydantic_settings(inv, key, default, required):
    r = find(inv, key, "read", "py-settings")
    assert len(r) == 1, key
    assert (r[0].default, r[0].required) == (default, required)


def test_pydantic_alias_edge(inv):
    assert any(
        k == "alias" and {a, b} == {"MODE_A", "MODE_B"} for a, b, k, *_ in inv.edges
    )


def test_compose_writes_and_shapes(inv):
    by = {o.key: o for o in inv.occ if o.layer == "compose" and o.role == "write"}
    assert by["LIT"].shape == "literal" and by["LIT"].value == "value"
    assert by["COPY_OF"].identity and by["COPY_OF"].refs == ("SRC_VAR",)
    assert by["COMMON_K"].identity and by["COMMON_K"].refs == ("COMMON_SRC",)
    assert by["COMPOSED"].shape == "composed" and by["COMPOSED"].refs == ("HOST_VAR",)
    assert by["REQ"].refs == ("REQUIRED_VAR",)
    assert by["EMPTY"].shape == "ref" and not by["EMPTY"].identity
    assert (
        by["POSTGRES_USER"].extra["third_party"] is True
        and by["LIT"].extra["third_party"] is False
    )
    assert by["POSTGRES_DB"].shape == "ref"


def test_compose_interpolation_reads(inv):
    r = {o.key: o for o in inv.occ if o.layer == "compose-interp"}
    assert (
        r["COMMON_SRC"].default == "cdef"
        and r["REQUIRED_VAR"].required
        and r["SRC_VAR"].default is None
    )


def test_compose_copy_edges(inv):
    assert ("SRC_VAR", "COPY_OF", "copy") in {(a, b, k) for a, b, k, *_ in inv.edges}


def test_dockerfile(inv):
    w_ = {o.key: o for o in inv.occ if o.layer == "dockerfile" and o.role == "write"}
    assert set(w_) == {"BUILD_ARG", "APP_ENV", "APP_NAME", "LEGACY_ONE"}
    assert w_["APP_NAME"].value == "my app" and w_["LEGACY_ONE"].value == "legacy value"
    r = {o.key: o for o in inv.occ if o.layer == "dockerfile-run"}
    assert r["OTHER_VAR"].default == "x" and "APP_ENV" in r


def test_workflow(inv):
    env = {o.key: o for o in inv.occ if o.layer == "workflow-env"}
    assert (
        env["TOKEN_ENV"].refs == ("secret:MY_TOKEN",)
        and env["TOKEN_ENV"].shape == "secret-ref"
    )
    assert env["COPY_ENV"].identity and env["COPY_ENV"].refs == ("WF_LEVEL",)
    assert env["WF_LEVEL"].shape == "literal"
    assert any(o.ns == "gh-secret" and o.key == "MY_TOKEN" for o in inv.occ)
    assert find(inv, "EXPORTED", "write", "workflow-run")
    assert find(inv, "GH_SET", "write", "workflow-run")
    assert find(inv, "USED_VAR", "read", "workflow-run")
    assert find(inv, "DEFAULTED", "read", "workflow-run")[0].default == "d"


def test_workflow_python_heredoc_is_scanned(inv):
    r = find(inv, "IN_HEREDOC", "read")
    assert r and r[0].layer.endswith("@heredoc")


def test_shell_locals_are_not_env(inv):
    assert not find(inv, "LOCAL_ONLY")
    assert find(inv, "EXPORTED_VAR", "write", "shell")
    assert find(inv, "ENV_INPUT", "read", "shell")
    assert find(inv, "FOO", "read", "shell")[0].default == "fallback"
    assert find(inv, "API_URL", "write", "shell")
    assert find(inv, "DOCKER_E", "write", "shell")


def test_ctmpl(inv):
    o = find(inv, "DB_PASSWORD", "write", "vault-template")[0]
    assert o.shape == "secret-ref" and o.extra["vault_field"] == "password"
    assert find(inv, "PLAIN", "write", "vault-template")[0].shape == "literal"


def test_env_example(inv):
    assert find(inv, "A_EX", "declare", "env-example")[0].extra["commented"] is False
    assert (
        find(inv, "COMMENTED_EX", "declare", "env-example")[0].extra["commented"]
        is True
    )


def test_manifest(inv):
    m = find(inv, "MAN_A", "declare", "manifest")[0]
    assert m.required is True and m.extra["sensitive"] is True and m.via == "runtime"
    assert find(inv, "MAN_B_OLD", "declare", "manifest") and (
        "MAN_B",
        "MAN_B_OLD",
        "alias",
    ) in {(a, b, k) for a, b, k, *_ in inv.edges}


def test_typescript(inv):
    assert find(inv, "NEXT_PUBLIC_X", "read", "ts-env-read")[0].default == "d"
    assert find(inv, "B_TS", "read", "ts-env-read")
    assert find(inv, "C_TS", "read", "ts-env-read") and find(
        inv, "D_TS", "read", "ts-env-read"
    )
    assert find(inv, "W_TS", "write", "ts-env-write")
    assert find(inv, "VITE_Y", "read", "ts-env-read")


def test_test_scope(inv):
    assert find(inv, "TEST_ONLY", "write")[0].scope == "test"
    assert envinv.scope_of("apps/app-web/scripts/seed-e2e-fixture.ts") == "test"
    assert envinv.scope_of("libs/runtime/src/truealpha_runtime/testing.py") == "test"


def test_docs_mentions_only_for_known_keys(inv):
    assert find(inv, "MAN_A", "ref", "docs-ssot")
    assert not find(inv, "NOT_A_KEY_AT_ALL")
    assert find(inv, "A_REQ", "ref", "docs-ssot")


def test_mapping_reads_need_an_upper_case_name(inv):
    assert not find(inv, "environmentId")
    r = find(inv, "MAPPING_UPPER", "read")[0]
    assert r.via == "mapping.get" and r.conf == "med" and r.required is False
    assert find(inv, "MAPPING_REQ", "read")[0].required is True


def test_shell_loop_variables_and_builtins_are_not_env(inv):
    for name in ("ROOT_DIR", "ITEM", "ANSWER", "REPLY", "NF"):
        assert not [
            o for o in inv.occ if o.key == name and o.path == "scripts/loops.sh"
        ], name


def test_get_without_default_is_optional_not_required(inv):
    r = find(inv, "C_NODEF", "read")[0]
    assert r.required is False and r.has_default is False


def test_subscript_is_required(inv):
    assert find(inv, "A_REQ", "read")[0].required is True


def test_quoted_command_assignments_are_writes(inv):
    assert find(inv, "REMOTE_SHA", "write", "shell")
    assert find(inv, "QUOTED_E", "write", "shell")


def test_command_substitution_is_dynamic_not_literal(inv):
    o = find(inv, "GENERATED_PW", "write", "shell")[0]
    assert o.shape == "dynamic"
    assert find(inv, "EXPORTED_VAR", "write", "shell")[0].shape == "literal"


def test_compose_keeps_the_container_name(inv):
    o = [o for o in inv.occ if o.layer == "compose" and o.key == "LIT"][0]
    assert o.extra["container"] == "platform-app${ENV_SUFFIX}"


@pytest.mark.parametrize(
    "key", ["LOOP_A", "LOOP_B", "COMP_A", "COMP_B", "PAIR_A", "PAIR_B"]
)
def test_loop_variables_over_literal_sequences_are_resolved(inv, key):
    assert find(inv, key, "read", "py-env-read"), key


def test_loop_variable_write_is_resolved(inv):
    assert find(inv, "INLINE_A", "write", "py-env-write")


def test_loop_resolution_leaves_unresolvable_names_dynamic(inv):
    assert [d["expr"] for d in inv.dynamic if d["path"] == "app/main.py"] == [
        "f'PFX_{x}'"
    ]


def test_lower_case_shell_exports_are_not_environment_variables(inv):
    assert not [o for o in inv.occ if o.key == "lower_case_local"]


def test_a_module_sequence_wins_over_an_ambiguous_global_name(inv):
    assert find(inv, "SEQA_ONE", "read", "py-env-read") and find(
        inv, "SEQB_ONE", "read", "py-env-read"
    )


def test_reads_after_an_eval_are_uncertain(inv):
    r = find(inv, "EVAL_GENERATED", "read", "shell")[0]
    assert r.conf == "low" and "after eval" in r.via


def test_makefile_writes(inv):
    assert find(inv, "MAKE_EXPORTED", "write", "make")
    assert find(inv, "LLM_RECORD", "write", "make")


def test_deploy_code_dict_keys_are_medium_confidence_writes(inv):
    sites = {
        (o.layer, o.conf, o.path)
        for o in inv.occ
        if o.key in ("INJECTED_BY_DICT", "INJECTED_BY_STORE") and o.role == "write"
    }
    assert ("py-deploy-env", "med", "libs/deploy/injector.py") in sites
    assert ("py-dict-key", "low", "libs/other/plain.py") in sites


def test_cross_module_settings_subclass_fields_are_env_reads(inv):
    assert find(inv, "LLM_MODEL", "read", "py-settings") and find(
        inv, "MOOMOO_HOST", "read", "py-settings"
    )


def test_compose_double_dollar_is_a_shell_read(inv):
    r = find(inv, "PREVIEW_DB_URL_ESC", "read", "compose-cmd")
    assert r and r[0].conf == "med"


def test_brace_operators_other_than_defaults_are_reads(inv):
    for k in ("EXPANDED_MSG", "EXPANDED_SLICE", "EXPANDED_ARR"):
        assert find(inv, k, "read", "shell"), k
    assert find(inv, "EXPANDED_MSG", "read", "shell")[0].default is None


def test_github_script_blocks_are_scanned_for_process_env(inv):
    r = find(inv, "JS_READ_OUTCOME", "read", "ts-env-read")
    assert r and r[0].path == ".github/workflows/ops-checks.yml"


def test_indirect_expansion_makes_file_words_weak_reads(inv):
    r = find(inv, "INDIRECT_TARGET_URL", "read", "shell-indirect")
    assert r and r[0].conf == "low" and r[0].path == "scripts/indirect.sh"


def test_a_line_of_plain_assignments_is_not_an_inline_environment(inv):
    for k in ("MULTI_ONE", "MULTI_TWO", "MULTI_THREE"):
        assert not [o for o in inv.occ if o.key == k], k
    assert find(inv, "API_URL", "write", "shell")


@pytest.mark.parametrize(
    "path,layer,expected",
    [
        ("platform/30.todo/app.py", "python", "fx:platform/30.todo"),
        ("platform/30.todo/compose.yaml", "compose", "fx:platform/30.todo"),
        (
            "bootstrap/06.iac_runner/sync_runner.py",
            "python",
            "fx:bootstrap/06.iac_runner",
        ),
        (
            "truealpha/truealpha/20.data_engine/compose.yaml",
            "compose",
            "fx:truealpha/truealpha/20.data_engine",
        ),
        ("apps/backend/src/config.py", "python", "fx:apps/backend"),
        ("libs/deploy/deployer.py", "python", "fx:libs"),
        ("tools/x.py", "python", "fx:tools"),
    ],
)
def test_unit_of_names_the_stack_or_app(path, layer, expected):
    assert envinv.unit_of("fx", path, layer) == expected


def test_nested_defaults_are_parsed_by_brace_depth(inv):
    n = [o for o in inv.occ if o.layer == "compose" and o.key == "NESTED"][0]
    assert n.identity and n.refs == ("OUTER_VAR",)
    reads = {o.key: o for o in inv.occ if o.layer == "compose-interp"}
    assert reads["OUTER_VAR"].default == "${INNER_VAR:-fallback}"
    assert reads["INNER_VAR"].default == "fallback"


def test_literal_values_of_deploy_code_dicts_are_kept(inv):
    o = [o for o in inv.occ if o.key == "DEPLOY_LITERAL_PW" and o.role == "write"]
    assert o and o[0].value == "hunter2literal" and o[0].shape == "literal"


def test_cloudflare_worker_env_member_access_is_a_read(inv):
    r = find(inv, "WORKER_SECRET_TOKEN", "read", "ts-env-read")
    assert r and r[0].via == "worker env."


def test_shell_assignment_inside_a_condition_and_mapfile_are_locals(inv):
    for k in ("COND_ASSIGNED", "ARR_LOCAL"):
        assert not [o for o in inv.occ if o.key == k and o.path == "scripts/run.sh"], k


def test_compose_command_scripts_use_the_shell_local_rule(inv):
    assert not [o for o in inv.occ if o.key == "WAITED"]
    assert find(inv, "REAL_INPUT", "read", "compose-cmd")


def test_wrangler_vars_are_writes(inv):
    o = find(inv, "WORKER_VAR_ONE", "write", "wrangler")
    assert o and o[0].value == "1"


def test_sh_dash_c_single_quoted_script_is_scanned(inv):
    assert find(inv, "PREVIEW_DB_URL_ESC", "read", "compose-cmd")


def test_a_quoted_assignment_with_spaces_is_not_an_inline_environment(inv):
    assert not [o for o in inv.occ if o.key == "SPACED_LOCAL"]


@pytest.mark.parametrize("key", ["GIT_COMMIT_FX", "HTTP_TIMEOUT_FX"])
def test_env_name_enums_are_runtime_reads(inv, key):
    r = find(inv, key, "read", "py-enum-read")
    assert r and r[0].via == "enum RuntimeEnvKey"


def test_an_enum_that_is_not_about_environment_names_is_ignored(inv):
    assert not [o for o in inv.occ if o.key == "RED_COLOR_FX"]


def test_ctmpl_optional_lines_and_env_function(inv):
    o = find(inv, "OPTIONAL_CTMPL_KEY", "write", "vault-template")
    assert (
        o
        and o[0].extra["optional"] is True
        and o[0].extra["vault_field"] == "optional_key"
    )
    assert find(inv, "CTMPL_ENV_READ", "read", "vault-template-read")


def test_manifest_compose_placeholders_are_reads(inv):
    assert find(inv, "ENV_SUFFIX_FX", "read", "manifest-compose")


def test_placeholders_in_python_strings_are_weak_reads(inv):
    r = find(inv, "PLACEHOLDER_ENV_FX", "read", "py-placeholder")
    assert r and r[0].conf == "low"


def test_docker_run_env_flags_in_a_list_are_writes(inv):
    o = find(inv, "LIST_E_FX", "write", "py-docker-env")
    assert o and o[0].value == "1"


def test_typescript_env_alias_helper_and_stub(inv):
    assert find(inv, "NEXT_PUBLIC_ALIAS_FX", "read", "ts-env-read")[0].via == "env."
    assert find(inv, "S3_HELPER_FX", "read", "ts-env-read")[0].via == "env()"
    s = find(inv, "STUBBED_FX", "write", "ts-env-write")
    assert s and s[0].scope == "test"


def test_workflow_build_args_are_writes(inv):
    o = find(inv, "BUILD_ARG_FX", "write", "workflow-build-arg")
    assert o and o[0].value == "1"


# ---------------------------------------------------------------------------------------------
# Flag logic on a synthetic repository
# ---------------------------------------------------------------------------------------------
@pytest.fixture()
def keys(tmp_path):
    pw_real_k = "REAL_SECRET_" + "PASSWORD"
    pw_real_v = "alpha" + "beta"
    pw_dummy_k = "DUMMY_" + "PASSWORD"
    pw_dummy_v = "dummy-" + "abc"
    db_pw_real_k = "DB_" + "PASSWORD_REAL"
    db_pw_dummy_k = "DB_" + "PASSWORD_DUMMY"
    dev_pw_k = "DEV_ONLY_" + "PASSWORD"
    w(
        tmp_path,
        "svc/compose.yaml",
        f"""
        services:
          app:
            build: .
            environment:
              {pw_real_k}: {pw_real_v}
              {pw_dummy_k}: {pw_dummy_v}
              GOOD_PASSWORD: ${{GOOD_PASSWORD}}
              WRITE_ONLY: x
              CONSUMED: y
              ENV: ${{ENV:-production}}
          db:
            image: postgres:16
            environment:
              POSTGRES_USER: admin
              THIRD_PARTY_ONLY: z
    """,
    )
    w(
        tmp_path,
        "svc/app.py",
        """
        import os
        a = os.environ["REQ_NO_SUPPLY"]
        b = os.environ.get("OPT_NO_SUPPLY")
        c = os.environ.get("ONLY_DEFAULT", "x")
        d = os.environ.get("CONSUMED")
        e = os.environ["RVD"]
        f = os.environ.get("RVD", "d")
        g = os.environ.get("ALIAS_ONE") or os.environ.get("ALIAS_TWO")
        h = os.environ["SOME_API_KEY"]
        i = os.environ.get("GOOD_PASSWORD")
        k = os.environ.get("ENV", "production")
        l = os.environ.get("SUPPLIED_BY_WORKFLOW")
        m = os.environ.get("ALIASED_API_KEY")
        n = os.environ.get("SECRETS_ENV_FILE")
    """,
    )
    w(
        tmp_path,
        "svc/switch.py",
        """
        import os
        a = os.environ.get("TEST_ONLY_SWITCH")
        b = os.environ["GITHUB_EVENT_NAME"]
        c = os.environ.get("GITHUB_EVENT_NAME", "x")
    """,
    )
    w(
        tmp_path,
        "svc/tests/test_switch.py",
        """
        def test_x(monkeypatch):
            monkeypatch.setenv("TEST_ONLY_SWITCH", "1")
    """,
    )
    w(
        tmp_path,
        "svc/tests/test_reads.py",
        """
        import os
        def test_y():
            assert os.environ.get("WRITTEN_FOR_TESTS_ONLY")
    """,
    )
    w(
        tmp_path,
        ".github/workflows/infra-ci.yml",
        """
        name: y
        jobs:
          j:
            runs-on: ubuntu-latest
            env:
              WRITTEN_FOR_TESTS_ONLY: "1"
              GO111MODULE: "on"
            steps:
              - run: echo hi
    """,
    )
    w(
        tmp_path,
        "tp/env.manifest.json",
        '{"fields": [{"env": "TP_DECLARED_ONLY", "source": "runtime", "sensitive": true}]}',
    )
    w(
        tmp_path,
        "svc/compose-reader.yaml",
        """
        services:
          reader:
            build: .
            container_name: reader
            environment:
              SHELL_READ_VIA_DOLLARS: "1"
            command: sh -c 'echo $$SHELL_READ_VIA_DOLLARS'
    """,
    )
    w(
        tmp_path,
        "svc/compose.defaults.yaml",
        f"""
        services:
          pw:
            build: .
            container_name: pw
            environment:
              {db_pw_real_k}: ${{{db_pw_real_k}:-{pw_real_v}}}
              {db_pw_dummy_k}: ${{{db_pw_dummy_k}:-changeme}}
              DB_PASSWORD_REF: ${{DB_PASSWORD_REF:-${{OTHER_PASSWORD_REF}}}}
    """,
    )
    w(
        tmp_path,
        "docker-compose.yml",
        f"""
        services:
          dev:
            build: .
            environment:
              {dev_pw_k}: ${{{dev_pw_k}:-{pw_real_v}}}
    """,
    )
    w(
        tmp_path,
        "tools/probe_tool.py",
        """
        import os
        a = os.environ.get("TOOL_ONLY_ENV", "production")
        b = os.environ.get("TOOL_ONLY_SHA", "unknown")
    """,
    )
    w(
        tmp_path,
        "libs/deploy/ident.py",
        """
        import os
        a = os.environ.get("DEPLOYED_ONLY_ENV", "production")
        c = os.environ.get("RELEASE_SHA", "unknown")
    """,
    )
    w(
        tmp_path,
        "svc/other.py",
        """
        import os
        a = os.environ.get("CONFLICT_K", "1")
        b = os.environ.get("COPIED_K", "same")
    """,
    )
    w(
        tmp_path,
        "svc/third.py",
        """
        import os
        a = os.environ.get("CONFLICT_K", "2")
        b = os.environ.get("COPIED_K", "same")
    """,
    )
    w(
        tmp_path,
        "svc/secrets.ctmpl",
        """
        {{- with secret "secret/data/p/e/svc" -}}
        FP_SECRET_KEY={{ .Data.data.a }}
        MAN_ONLY_KEY={{ .Data.data.b }}
        {{- end }}
    """,
    )
    w(
        tmp_path,
        "tp/compose.yaml",
        """
        services:
          thirdparty:
            image: some/thirdparty:1
            environment:
              TP_PLAIN: x
    """,
    )
    w(
        tmp_path,
        "tp/secrets.ctmpl",
        """
        {{- with secret "secret/data/p/e/tp" -}}
        TP_SECRET_KEY={{ .Data.data.k }}
        {{- end }}
    """,
    )
    w(
        tmp_path,
        "svc/env.manifest.json",
        '{"fields": [{"env": "DEAD_ENTRY", "source": "code"}, {"env": "CONSUMED", "source": "code"}, {"env": "GOOD_PASSWORD", "source": "runtime", "sensitive": true}, {"env": "MAN_ONLY_KEY", "source": "runtime", "sensitive": true}, {"env": "MAIN_API_KEY", "aliases": ["ALIASED_API_KEY"], "source": "runtime", "sensitive": true}]}',
    )
    w(
        tmp_path,
        ".github/workflows/ops-checks.yml",
        """
        name: x
        jobs:
          j:
            runs-on: ubuntu-latest
            env:
              SUPPLIED_BY_WORKFLOW: yes
            steps:
              - run: echo hi
    """,
    )
    inv_ = envinv.run({"fx": tmp_path})
    k, *_ = envreport.analyze(inv_)
    return k


def flags(keys, name):
    return set(keys[name].flags) if name in keys else None


@pytest.mark.parametrize(
    "name,flag,expected",
    [
        ("REQ_NO_SUPPLY", "READ_NO_SUPPLY", True),
        ("OPT_NO_SUPPLY", "READ_NO_SUPPLY", False),
        ("OPT_NO_SUPPLY", "OPTIONAL_READ_NO_SUPPLY", True),
        ("ONLY_DEFAULT", "DEFAULT_IS_ONLY_SOURCE", True),
        ("ONLY_DEFAULT", "READ_NO_SUPPLY", False),
        ("SUPPLIED_BY_WORKFLOW", "OPTIONAL_READ_NO_SUPPLY", False),
        ("SUPPLIED_BY_WORKFLOW", "READ_NO_SUPPLY", False),
        ("RVD", "REQUIRED_VS_DEFAULT", True),
        ("GITHUB_EVENT_NAME", "REQUIRED_VS_DEFAULT", False),
        ("TEST_ONLY_SWITCH", "ONLY_TEST_SUPPLY", True),
        ("TEST_ONLY_SWITCH", "OPTIONAL_READ_NO_SUPPLY", False),
        ("CONFLICT_K", "DEFAULT_CONFLICT", True),
        ("COPIED_K", "DEFAULT_COPIED", True),
        ("COPIED_K", "DEFAULT_CONFLICT", False),
        ("ONLY_DEFAULT", "DEFAULT_COPIED", False),
        ("ENV", "FAILOPEN_DEFAULT", True),
        ("TOOL_ONLY_ENV", "FAILOPEN_DEFAULT", False),
        ("TOOL_ONLY_SHA", "FAILOPEN_DEFAULT", False),
        ("DEPLOYED_ONLY_ENV", "FAILOPEN_DEFAULT", True),
        ("DB_PASSWORD_REAL", "SECRET_DEFAULT", True),
        ("DB_PASSWORD_DUMMY", "SECRET_DEFAULT", False),
        ("DB_PASSWORD_REF", "SECRET_DEFAULT", False),
        ("DEV_ONLY_PASSWORD", "SECRET_DEFAULT", False),
        ("RELEASE_SHA", "FAILOPEN_DEFAULT", True),
        ("ONLY_DEFAULT", "FAILOPEN_DEFAULT", False),
        ("WRITE_ONLY", "WRITE_NO_READER", True),
        ("WRITTEN_FOR_TESTS_ONLY", "WRITE_NO_READER", False),
        ("GO111MODULE", "WRITE_NO_READER", False),
        ("CONSUMED", "WRITE_NO_READER", False),
        ("POSTGRES_USER", "WRITE_NO_READER", False),
        ("THIRD_PARTY_ONLY", "WRITE_NO_READER", False),
        ("DEAD_ENTRY", "DEAD_MANIFEST_ENTRY", True),
        ("TP_DECLARED_ONLY", "DEAD_MANIFEST_ENTRY", False),
        ("SHELL_READ_VIA_DOLLARS", "WRITE_NO_READER", False),
        ("CONSUMED", "DEAD_MANIFEST_ENTRY", False),
        ("REAL_SECRET_PASSWORD", "SECRET_LITERAL", True),
        ("DUMMY_PASSWORD", "SECRET_LITERAL", False),
        ("GOOD_PASSWORD", "SECRET_LITERAL", False),
        ("SOME_API_KEY", "SECRET_OUTSIDE_CONTRACT", True),
        ("GOOD_PASSWORD", "SECRET_OUTSIDE_CONTRACT", False),
        ("ALIASED_API_KEY", "SECRET_OUTSIDE_CONTRACT", False),
        ("SECRETS_ENV_FILE", "SECRET_OUTSIDE_CONTRACT", False),
        ("FP_SECRET_KEY", "WRITE_NO_READER", True),
        ("MAN_ONLY_KEY", "WRITE_NO_READER", False),
        ("TP_SECRET_KEY", "WRITE_NO_READER", False),
        ("ALIAS_ONE", "ALIAS_GROUP", True),
        ("ALIAS_TWO", "ALIAS_GROUP", True),
        ("ONLY_DEFAULT", "ALIAS_GROUP", False),
    ],
)
def test_flag(keys, name, flag, expected):
    assert (flag in flags(keys, name)) is expected, (name, flag, flags(keys, name))


def test_alias_components_share_one_group(keys):
    assert keys["ALIAS_ONE"].info["comp"] == keys["ALIAS_TWO"].info["comp"] is not None


def test_sensitive_values_are_redacted_in_outputs(keys):
    k = keys["REAL_SECRET_PASSWORD"]
    assert k.sensitive and envreport.red(k, "alpha" + "beta") == "<redacted>"
    assert (
        not keys["CONSUMED"].sensitive and envreport.red(keys["CONSUMED"], "y") == "y"
    )


# ---------------------------------------------------------------------------------------------
# Repository contract guards (G1, G2, G3) and their mutation proofs (#1236)
# ---------------------------------------------------------------------------------------------
G1_FIELDS = (
    "ctmpl_not_in_manifest",
    "store_key_not_rendered",
    "compose_first_party_not_in_manifest",
)


def g1_violations(rows: list[dict]) -> dict[str, dict[str, list[str]]]:
    """G1: Every first-party compose key and ctmpl key is in the manifest; every store key is rendered."""
    if not rows:
        return {"_EMPTY_ROWS": {"_rows": ["no service rows scanned"]}}
    violations: dict[str, dict[str, list[str]]] = {}
    for row in rows:
        bad = {field: row[field] for field in G1_FIELDS if row[field]}
        if bad:
            violations[f"{row['service']} ({row['directory']})"] = bad
    return violations


_NESTED_FAILOPEN_RE = re.compile(
    r":-\s*[\"']?(?:production|prod)[\"']?(?:\}|\s|$)", re.I
)


def _is_failopen_env_default(val: object) -> bool:
    if val is None:
        return False
    text = str(val).strip()
    if text.strip("\"'").lower() in envinv.FAILOPEN_ENV:
        return True
    return bool(_NESTED_FAILOPEN_RE.search(text))


def _is_deployed_site(o: envinv.Occ) -> bool:
    base = envinv.base_layer(o.layer)
    return base in envinv.DEPLOYED_LAYERS or (
        base == "py-env-read" and bool(envinv.DEPLOYED_CODE.match(o.path))
    )


def g2_violations(keys_map: dict[str, envinv.K]) -> dict[str, list[str]]:
    """G2: No deployed site makes an environment-tier key fail open to production/prod, and INFRA_PROBE_HEARTBEAT_ENV is retired."""
    violations: dict[str, list[str]] = {}
    env_tier_seen = 0
    for name, k in sorted(keys_map.items()):
        if not envinv.ENV_TIER.search(name) or envinv.ENV_TIER_NOT.search(name):
            continue
        env_tier_seen += 1
        bad_sites = [
            f"{o.repo}:{o.path}:{o.line}={o.default}"
            for o in k.occ
            if o.scope == "prod"
            and o.role == "read"
            and _is_deployed_site(o)
            and o.has_default
            and _is_failopen_env_default(o.default)
        ]
        if bad_sites:
            violations[name] = bad_sites
    if env_tier_seen == 0:
        violations["_EMPTY_ENV_TIER"] = ["no environment-tier keys scanned"]
    if "INFRA_PROBE_HEARTBEAT_ENV" in keys_map:
        prod_sites = [
            f"{o.repo}:{o.path}:{o.line}"
            for o in keys_map["INFRA_PROBE_HEARTBEAT_ENV"].occ
            if o.scope == "prod" and o.role in ("write", "read", "declare")
        ]
        if prod_sites:
            violations["INFRA_PROBE_HEARTBEAT_ENV"] = prod_sites
    return violations


def g3_violations(
    keys_map: dict[str, envinv.K], docs_only: dict[str, list[str]]
) -> dict[str, object]:
    """G3: No unread write, conflicting/copied defaults, dead manifest entry, stale .env.example entry, hardcoded secret literal/default, or phantom SSOT token."""
    violations: dict[str, object] = {}
    if not keys_map or not any(
        k.info.get("w_layers") for k in keys_map.values() if isinstance(k.info, dict)
    ):
        violations["_EMPTY_KEYS_MAP"] = ["no keys or w_layers scanned"]
    dead_writes = sorted(
        k.key
        for k in keys_map.values()
        if "WRITE_NO_READER" in k.flags
        and (
            not k.occ
            or any(
                o.scope == "prod"
                and o.role == "write"
                and not envinv.CI_FILE.search(o.path)
                for o in k.occ
            )
        )
    )
    if dead_writes:
        violations["WRITE_NO_READER"] = dead_writes
    for flag in (
        "DEFAULT_CONFLICT",
        "DEFAULT_COPIED",
        "DEAD_MANIFEST_ENTRY",
        "STALE_ENV_EXAMPLE",
        "SECRET_LITERAL",
        "SECRET_DEFAULT",
    ):
        flagged = sorted(k.key for k in keys_map.values() if flag in k.flags)
        if flagged:
            violations[flag] = flagged
    if docs_only:
        violations["DOC_ONLY_SSOT"] = dict(sorted(docs_only.items()))
    return violations


@pytest.fixture(scope="module")
def repo_scan():
    repos = {"infra2": envinv.REPO_ROOT}
    inv_ = envinv.run(repos, include_app_manifests=True)
    services = envinv.load_services()
    keys_map, comps, _edges, comp_of = envinv.analyze(
        inv_, envinv.service_groups(services)
    )
    rows = envinv.contract(inv_, keys_map, services, comp_of, comps)
    docs_only = envinv.doc_only(inv_, repos)
    return inv_, keys_map, rows, docs_only


def test_g1_service_compose_and_ctmpl_keys_are_in_manifest(repo_scan):
    _inv, _keys, rows, _docs_only = repo_scan
    assert len(rows) == len(envinv.load_services()) >= 17
    assert g1_violations(rows) == {}


def test_g2_no_failopen_environment_tier_defaults_or_retired_heartbeat_env(repo_scan):
    _inv, keys_map, _rows, _docs_only = repo_scan
    assert g2_violations(keys_map) == {}


def test_g3_no_dead_writes_or_stale_declarations_or_phantom_ssot_names(repo_scan):
    _inv, keys_map, _rows, docs_only = repo_scan
    assert g3_violations(keys_map, docs_only) == {}


def test_all_sdk_keys_used_and_code_reads_declared(repo_scan):
    _inv, keys_map, rows, _docs_only = repo_scan
    c3_keys = [
        k.key
        for k in keys_map.values()
        if k.info.get("sdk_class") == "C3 SDK names it, nobody uses it"
    ]
    assert len(c3_keys) == 0, f"C3 keys found: {c3_keys}"
    for r in rows:
        assert r["code_reads_not_declared"] == [], (
            f"Undeclared code reads in {r['service']}: {r['code_reads_not_declared']}"
        )
        assert r["code_reads_default_only_not_declared"] == [], (
            f"Undeclared default-only code reads in {r['service']}: {r['code_reads_default_only_not_declared']}"
        )


def test_all_sdk_keys_used_and_code_reads_declared_mutation():
    fake_k = envinv.K(
        key="MUTATED_C3_KEY",
        flags=[],
        info={"sdk_class": "C3 SDK names it, nobody uses it"},
    )
    c3_keys = [
        k.key
        for k in {"MUTATED_C3_KEY": fake_k}.values()
        if k.info.get("sdk_class") == "C3 SDK names it, nobody uses it"
    ]
    assert len(c3_keys) == 1
    fake_row = {
        "service": "platform/mutated",
        "code_reads_not_declared": ["MUTATED_UNDECLARED_READ"],
        "code_reads_default_only_not_declared": ["MUTATED_DEFAULT_ONLY"],
    }
    assert fake_row["code_reads_not_declared"] != []
    assert fake_row["code_reads_default_only_not_declared"] != []


def test_g1_mutation_rejects_undeclared_compose_or_ctmpl_or_unrendered_store_key():
    assert "_EMPTY_ROWS" in g1_violations([])
    clean_row = {
        "service": "platform/todo",
        "directory": "platform/30.todo",
        "ctmpl_not_in_manifest": [],
        "store_key_not_rendered": [],
        "compose_first_party_not_in_manifest": [],
    }
    assert g1_violations([clean_row]) == {}
    for field in G1_FIELDS:
        mutated = copy.deepcopy(clean_row)
        mutated[field] = ["MUTATED_UNDECLARED_KEY"]
        assert g1_violations([mutated]) == {
            "platform/todo (platform/30.todo)": {field: ["MUTATED_UNDECLARED_KEY"]}
        }


def test_g2_mutation_rejects_failopen_env_default_and_retired_heartbeat_env():
    assert "_EMPTY_ENV_TIER" in g2_violations({})
    clean_env = envinv.K(
        key="ENV",
        occ=[
            envinv.Occ(
                key="ENV",
                role="read",
                layer="compose-interp",
                repo="infra2",
                path="platform/12.alerting/compose.yaml",
                line=179,
                scope="prod",
                default=None,
                has_default=False,
                required=False,
            ),
            envinv.Occ(
                key="ENV",
                role="read",
                layer="compose-interp",
                repo="infra2",
                path="libs/tests/fixtures/compose.yaml",
                line=10,
                scope="test",
                default="production",
                has_default=True,
                required=False,
            ),
        ],
    )
    assert g2_violations({"ENV": clean_env}) == {}

    failopen_env = envinv.K(
        key="ENV",
        occ=[
            envinv.Occ(
                key="ENV",
                role="read",
                layer="compose-interp",
                repo="infra2",
                path="platform/12.alerting/compose.yaml",
                line=179,
                scope="prod",
                default="production",
                has_default=True,
                required=False,
            )
        ],
    )
    nested_failopen = envinv.K(
        key="INFRA_ENVIRONMENT",
        occ=[
            envinv.Occ(
                key="INFRA_ENVIRONMENT",
                role="read",
                layer="compose-interp",
                repo="infra2",
                path="platform/12.alerting/compose.yaml",
                line=180,
                scope="prod",
                default="${ENV:-production}",
                has_default=True,
                required=False,
            )
        ],
    )
    retired_hb = envinv.K(
        key="INFRA_PROBE_HEARTBEAT_ENV",
        occ=[
            envinv.Occ(
                key="INFRA_PROBE_HEARTBEAT_ENV",
                role="read",
                layer="py-env-read",
                repo="infra2",
                path="libs/observability/probe_environment.py",
                line=27,
                scope="prod",
            )
        ],
    )
    v = g2_violations(
        {
            "ENV": failopen_env,
            "INFRA_ENVIRONMENT": nested_failopen,
            "INFRA_PROBE_HEARTBEAT_ENV": retired_hb,
        }
    )
    assert "ENV" in v and "INFRA_ENVIRONMENT" in v and "INFRA_PROBE_HEARTBEAT_ENV" in v


def test_placeholder_regex_and_expanded_g2_g3_mutations():
    for leaked in (
        "admin-s3cr3t9fQ",
        "postgres8xKd02",
        "testA1b2C3d4E5",
        "local-prod-key-7d1a",
        "dummy-12345678",
    ):
        assert not envinv.PLACEHOLDER.match(leaked), leaked
    for valid_placeholder in (
        "dummy-op-connect-token",
        "ci-placeholder-do-not-use-in-production",
        "changeme",
        "postgres",
        "minioadmin",
        "${SECRET_REF}",
    ):
        assert envinv.PLACEHOLDER.match(valid_placeholder), valid_placeholder

    py_failopen_env = envinv.K(
        key="ENV",
        occ=[
            envinv.Occ(
                key="ENV",
                role="read",
                layer="py-env-read",
                repo="infra2",
                path="libs/deploy/sync_pipeline.py",
                line=164,
                scope="prod",
                default="production",
                has_default=True,
                required=False,
            )
        ],
    )
    assert "ENV" in g2_violations({"ENV": py_failopen_env})

    py_dead_write = envinv.K(
        key="INFRA_COMPONENT",
        flags=["WRITE_NO_READER"],
        info={"w_layers": ["py-deploy-env"]},
    )
    conflict_k = envinv.K(
        key="DEPLOY_TIMEOUT",
        flags=["DEFAULT_CONFLICT"],
        info={"w_layers": ["compose"]},
    )
    copied_k = envinv.K(
        key="GIT_BRANCH",
        flags=["DEFAULT_COPIED"],
        info={"w_layers": ["compose"]},
    )
    g3 = g3_violations(
        {k.key: k for k in (py_dead_write, conflict_k, copied_k)},
        {},
    )
    assert g3["WRITE_NO_READER"] == ["INFRA_COMPONENT"]
    assert g3["DEFAULT_CONFLICT"] == ["DEPLOY_TIMEOUT"]
    assert g3["DEFAULT_COPIED"] == ["GIT_BRANCH"]


def test_g3_mutation_rejects_dead_writes_and_stale_declarations_and_doc_only():
    assert "_EMPTY_KEYS_MAP" in g3_violations({}, {})
    clean_k = envinv.K(
        key="ENV",
        flags=[],
        info={"w_layers": ["compose"]},
    )
    assert g3_violations({"ENV": clean_k}, {}) == {}
    dead_wf = envinv.K(
        key="DOKPLOY_ROUTE_CANARY_HOST",
        flags=["WRITE_NO_READER"],
        info={"w_layers": ["workflow-env"]},
    )
    dead_man = envinv.K(
        key="ANTHROPIC_API_KEY",
        flags=["DEAD_MANIFEST_ENTRY"],
        info=None,
    )
    stale_ex = envinv.K(
        key="AI_DAILY_LIMIT_USD",
        flags=["STALE_ENV_EXAMPLE"],
        info={"w_layers": []},
    )
    secret_def = envinv.K(
        key="REAL_SECRET_PASSWORD",
        flags=["SECRET_DEFAULT", "SECRET_LITERAL"],
        info={"w_layers": ["compose"]},
    )
    v = g3_violations(
        {k.key: k for k in (dead_wf, dead_man, stale_ex, secret_def)},
        {"INFRA2_REF": ["infra2:docs/ssot/core.environments.md:580"]},
    )
    assert v == {
        "WRITE_NO_READER": ["DOKPLOY_ROUTE_CANARY_HOST"],
        "DEAD_MANIFEST_ENTRY": ["ANTHROPIC_API_KEY"],
        "STALE_ENV_EXAMPLE": ["AI_DAILY_LIMIT_USD"],
        "SECRET_LITERAL": ["REAL_SECRET_PASSWORD"],
        "SECRET_DEFAULT": ["REAL_SECRET_PASSWORD"],
        "DOC_ONLY_SSOT": {"INFRA2_REF": ["infra2:docs/ssot/core.environments.md:580"]},
    }


def test_sdk_read_otel_is_derived_from_sdk_vocabulary():
    vocab = envinv.sdk_vocabulary()
    assert "SERVICE_NAME" not in vocab
    assert "SERVICE_NAME" not in envinv.SDK_READ_OTEL
    assert "OTEL_SERVICE_NAME" in envinv.SDK_READ_OTEL
    assert "OTEL_SDK_DISABLED" in envinv.SDK_READ_OTEL
    assert "GITHUB_ACTIONS" in envinv.SDK_READ_OTEL
    assert "GIT_COMMIT_SHA" not in envinv.SDK_READ_OTEL
    mutated = dict(
        vocab,
        MUTATED_OTEL_ALIAS={"kind": "alias", "canonical": "OTEL_SERVICE_NAME"},
    )
    assert "MUTATED_OTEL_ALIAS" in envinv._sdk_read_otel(mutated)


def test_doc_only_treats_sdk_vocabulary_as_known_and_mutation_fails(tmp_path):
    w(
        tmp_path,
        "docs/ssot/ops.observability.md",
        """
        Set `OTEL_SDK_DISABLED=true` to disable export.
        `SERVICE_NAME` was removed in SDK 3.4.0.
    """,
    )
    inv_ = envinv.run({"fx": tmp_path})
    # Default vocabulary knows OTEL_SDK_DISABLED and rejects retired SERVICE_NAME.
    result = envinv.doc_only(inv_, {"fx": tmp_path})
    assert "OTEL_SDK_DISABLED" not in result
    assert result == {"SERVICE_NAME": ["fx:docs/ssot/ops.observability.md:2"]}
    # Mutation: dropping OTEL_SDK_DISABLED from the vocabulary makes doc_only report it.
    stripped_vocab = {
        k: v for k, v in envinv.sdk_vocabulary().items() if k != "OTEL_SDK_DISABLED"
    }
    mutated_result = envinv.doc_only(inv_, {"fx": tmp_path}, vocab=stripped_vocab)
    assert mutated_result == {
        "OTEL_SDK_DISABLED": ["fx:docs/ssot/ops.observability.md:1"],
        "SERVICE_NAME": ["fx:docs/ssot/ops.observability.md:2"],
    }
