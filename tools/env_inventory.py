#!/usr/bin/env python3
"""Environment-variable inventory, SDK classification, and SSOT contract guards.

Every place that WRITES, READS or DECLARES an environment variable becomes one ``Occ``
record. ``analyze()`` joins the records by key, classifies each key against
``infra2_sdk.runtime.environment.runtime_env_contract()`` (classes ``C1`` to ``C6``), and
evaluates the service manifest contract (``libs.security.registry.SERVICES``).

Outputs are written to ``.agents/runs/env-inventory`` by default and are never committed.
"""

from __future__ import annotations

import argparse
import ast
import collections
import csv
import json
import os
import pathlib
import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

UPPER_KEY = re.compile(r"^[A-Z][A-Z0-9_]*$")
ANY_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
STRICT_KEY = re.compile(r"^[A-Z][A-Z0-9_]{2,}$")

SKIP_PARTS = {
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    ".next",
    ".git",
    "dist",
    "build",
    ".mypy_cache",
    ".cache",
    ".agents",
}
SKIP_PREFIXES = ("docs/project/archive/",)
SKIP_NAMES = {
    "uv.lock",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "bun.lock",
    "bun.lockb",
}
MAX_BYTES = 1_500_000

ENV_MAP_NAMES = {
    "env",
    "environ",
    "environment",
    "env_vars",
    "envvars",
    "os_environ",
    "process_env",
    "base_env",
    "child_env",
    "merged_env",
    "env_map",
    "envmap",
    "runtime_env",
    "host_env",
    "source_env",
}
WRITE_WORDS = {
    "set",
    "put",
    "export",
    "inject",
    "write",
    "define",
    "override",
    "patch",
    "update",
    "add",
}
CHILD_ENV_KWARGS = {"env", "environment", "env_vars", "extra_env"}
TESTER_NAMES = {"monkeypatch", "mp", "monkey", "mpatch"}


@dataclass
class Occ:
    key: str
    role: str  # write | read | declare | ref | unset
    layer: str
    repo: str
    path: str
    line: int
    scope: str = "prod"  # prod | test | doc
    ns: str = "env"  # env | gh-secret | gh-var
    default: str | None = None
    has_default: bool | None = None
    required: bool | None = None
    value: str | None = None
    shape: str | None = None  # literal | empty | ref | composed | secret-ref | dynamic
    refs: tuple = ()
    identity: bool = False
    via: str = ""
    unit: str = ""
    conf: str = "high"
    extra: dict = field(default_factory=dict)


class Inventory:
    def __init__(self) -> None:
        self.occ: list[Occ] = []
        self.edges: list[tuple] = []  # (a, b, kind, repo, path, line)
        self.dynamic: list[dict] = []
        self.errors: list[str] = []
        self.counts: collections.Counter = collections.Counter()
        self.strings: dict[tuple, list] = collections.defaultdict(
            list
        )  # (repo, path, kind) -> [(token, line)]

    def add(self, **kw) -> Occ:
        o = Occ(**kw)
        self.occ.append(o)
        return o

    def edge(self, a, b, kind, repo, path, line) -> None:
        if a and b and a != b:
            self.edges.append((a, b, kind, repo, path, line))


# ---------------------------------------------------------------------------------------------
# file selection and routing
# ---------------------------------------------------------------------------------------------
def list_files(root: Path) -> list[str]:
    if (root / ".git").exists():
        out = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "ls-files",
                "--cached",
                "-z",
            ],
            capture_output=True,
            check=True,
        ).stdout
        files = sorted({f for f in out.decode("utf-8", "replace").split("\0") if f})
    else:
        files = [str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()]
    keep = []
    for f in files:
        if not (root / f).is_file():
            continue
        parts = f.split("/")
        if any(p in SKIP_PARTS for p in parts):
            continue
        if f.startswith(SKIP_PREFIXES) or parts[-1] in SKIP_NAMES:
            continue
        keep.append(f)
    return keep


def route(path: str) -> str | None:
    name = os.path.basename(path)
    low = name.lower()
    if re.match(r"^(docker-)?compose([._-].*)?\.ya?ml$", low):
        return "compose"
    if (
        low == "dockerfile"
        or low.startswith("dockerfile.")
        or low.endswith(".dockerfile")
    ):
        return "dockerfile"
    if (path.startswith(".github/workflows/") and low.endswith((".yml", ".yaml"))) or (
        path.startswith(".github/actions/") and low in ("action.yml", "action.yaml")
    ):
        return "workflow"
    if low.endswith(".ctmpl"):
        return "ctmpl"
    if low == ".envrc":
        return "shell"
    if (
        low == ".env"
        or low.startswith(".env.")
        or low.endswith((".env.example", ".env", ".env.tpl"))
    ):
        return "env-example"
    if low.endswith(".json") and ("env.manifest" in low or "required-env" in low):
        return "manifest"
    if low == "package.json":
        return "package-json"
    if low.endswith(".py"):
        return "python"
    if low.endswith((".sh", ".bash")):
        return "shell"
    if low.endswith((".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")):
        return "ts"
    if low in ("wrangler.toml",):
        return "wrangler"
    if low.endswith(".md"):
        return "docs"
    if low in ("makefile", "gnumakefile") or low.endswith(".mk"):
        return "make"
    return None


def scope_of(path: str) -> str:
    norm = path.replace("\\", "/").lower()
    if norm.endswith("apps/app-web/scripts/seed-e2e-fixture.ts") or norm.endswith(
        "libs/runtime/src/truealpha_runtime/testing.py"
    ):
        return "test"
    parts = norm.split("/")
    name = parts[-1]
    if any(
        p in ("tests", "test", "__tests__", "e2e", "e2e_regressions", "fixtures")
        for p in parts[:-1]
    ):
        return "test"
    if name.startswith("test_") or name.endswith("_test.py") or name == "conftest.py":
        return "test"
    if re.search(r"\.(test|spec)\.[jt]sx?$", name):
        return "test"
    return "prod"


DEPLOY_LAYERS = {"compose", "dockerfile", "ctmpl", "env-example", "manifest"}


def unit_of(repo: str, path: str, layer: str) -> str:
    """The deployment unit a file belongs to: one stack directory, or one app."""
    parts = path.split("/")
    if len(parts) >= 3 and parts[0] in ("platform", "bootstrap"):
        return f"{repo}:{parts[0]}/{parts[1]}"
    if (
        len(parts) >= 4
        and parts[0] == parts[1]
        and parts[0] in ("truealpha", "finance_report")
    ):
        return f"{repo}:{parts[0]}/{parts[1]}/{parts[2]}"
    if layer in DEPLOY_LAYERS or os.path.basename(path) in (
        "deploy.py",
        "shared_tasks.py",
    ):
        return f"{repo}:{os.path.dirname(path) or '.'}"
    if parts[0] == "apps" and len(parts) > 2:
        return f"{repo}:apps/{parts[1]}"
    return f"{repo}:{parts[0]}" if len(parts) > 1 else f"{repo}:."


def short(s, n: int = 90) -> str:
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"


# ---------------------------------------------------------------------------------------------
# interpolation helpers (compose, shell, workflow)
# ---------------------------------------------------------------------------------------------
_OP = re.compile(r"^(:?)([-+?=])(.*)$", re.S)


def _braced(text: str):
    """Yield (start, end, inner) for each ${...} in text; braces may nest."""
    i = 0
    while True:
        i = text.find("${", i)
        if i < 0:
            return
        depth, j = 1, i + 2
        while j < len(text) and depth:
            if text[j] == "{":
                depth += 1
            elif text[j] == "}":
                depth -= 1
            j += 1
        if depth:
            return
        yield i, j, text[i + 2 : j - 1]
        i = i + 2  # continue inside the expression to find nested ones


def interp_refs(text: str) -> list[tuple[str, str | None, bool, bool]]:
    """Return (name, default, required, is_braced) for every ${NAME...} or $NAME in text."""
    text = text.replace("$$", "\x00\x00")
    out = []
    spans = []
    for a, b, inner in _braced(text):
        m = re.match(r"^!?([A-Za-z_][A-Za-z0-9_]*)(.*)$", inner, re.S)
        if not m:
            continue
        spans.append((a, b))
        default, required = None, False
        op = _OP.match(m.group(2) or "")
        if op:
            default = op.group(3) if op.group(2) in ("-", "=") else None
            required = op.group(2) == "?"
        out.append((m.group(1), default, required, True))
    for m in re.finditer(r"\$([A-Za-z_][A-Za-z0-9_]*)", text):
        if not any(a <= m.start() < b for a, b in spans):
            out.append((m.group(1), None, False, False))
    return out


def classify_value(key: str, value: str | None) -> tuple[str, tuple, bool]:
    """(shape, refs, identity) for a value string that may hold interpolations."""
    if value is None:
        return "ref", (key,), False
    if value == "":
        return "empty", (), False
    refs = [r[0] for r in interp_refs(value)]
    if "$(" in value or "`" in value:
        return "dynamic", tuple(dict.fromkeys(refs)), False
    if not refs:
        return "literal", (), False
    flat = value.replace("$$", "\x00\x00")
    braces = list(_braced(flat))
    if len(braces) >= 1 and braces[0][0] == 0 and braces[0][1] == len(flat):
        m = re.match(r"^!?([A-Za-z_][A-Za-z0-9_]*)", braces[0][2])
        if m:
            return "ref", (m.group(1),), m.group(1) != key
    whole = re.fullmatch(r"\$([A-Za-z_][A-Za-z0-9_]*)", flat)
    if whole:
        return "ref", (whole.group(1),), whole.group(1) != key
    return "composed", tuple(dict.fromkeys(refs)), False


# ---------------------------------------------------------------------------------------------
# Python (AST)
# ---------------------------------------------------------------------------------------------
class ConstTable:
    """Module-level UPPER string constants, used to resolve os.environ[NAME]."""

    def __init__(self) -> None:
        self.local: dict[str, dict[str, str]] = collections.defaultdict(dict)
        self.glob: dict[str, set] = collections.defaultdict(set)
        self.seqs: dict[str, dict[str, list]] = collections.defaultdict(dict)
        self.gseqs: dict[str, list] = {}
        self.class_bases: dict[str, set] = collections.defaultdict(set)
        self.settings_names: set[str] = set()

    def finish(self) -> None:
        """Settings classes are BaseSettings subclasses across modules (class names, fixpoint)."""
        names = {c for c, bases in self.class_bases.items() if "BaseSettings" in bases}
        changed = True
        while changed:
            changed = False
            for c, bases in self.class_bases.items():
                if c not in names and bases & names:
                    names.add(c)
                    changed = True
        self.settings_names = names

    def collect(self, path: str, tree: ast.AST) -> None:
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                for b in node.bases:
                    last = (dotted(b) or "").split(".")[-1]
                    if last:
                        self.class_bases[node.name].add(last)
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = (
                    node.targets if isinstance(node, ast.Assign) else [node.target]
                )
                val = node.value
                if (
                    isinstance(val, ast.Constant)
                    and isinstance(val.value, str)
                    and ANY_KEY.match(val.value)
                ):
                    for t in targets:
                        if isinstance(t, ast.Name) and UPPER_KEY.match(t.id):
                            self.local[path][t.id] = val.value
                            self.glob[t.id].add(val.value)
                seq = literal_str_seq(val)
                if seq:
                    for t in targets:
                        if isinstance(t, ast.Name) and UPPER_KEY.match(t.id):
                            self.seqs[path][t.id] = seq
                            self.gseqs[t.id] = (
                                seq
                                if t.id not in self.gseqs or self.gseqs[t.id] == seq
                                else []
                            )

    def resolve(self, path: str, name: str) -> str | None:
        if name in self.local.get(path, {}):
            return self.local[path][name]
        vals = self.glob.get(name)
        if vals and len(vals) == 1:
            return next(iter(vals))
        return None


def literal_str_seq(n) -> list[str] | None:
    """The strings of a literal tuple/list/set/dict, or of tuple()/list()/set()/frozenset() around one."""
    if (
        isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id in ("tuple", "list", "set", "frozenset", "sorted")
        and len(n.args) == 1
    ):
        return literal_str_seq(n.args[0])
    if isinstance(n, (ast.Tuple, ast.List, ast.Set)):
        out = []
        for e in n.elts:
            if (
                isinstance(e, ast.Constant)
                and isinstance(e.value, str)
                and ANY_KEY.match(e.value)
            ):
                out.append(e.value)
            elif (
                isinstance(e, ast.Tuple)
                and e.elts
                and isinstance(e.elts[0], ast.Constant)
                and isinstance(e.elts[0].value, str)
                and ANY_KEY.match(e.elts[0].value)
            ):
                out.append(e.elts[0].value)  # ("NAME", default) pairs
            else:
                return None
        return out or None
    if (
        isinstance(n, ast.Dict)
        and n.keys
        and all(
            isinstance(k, ast.Constant)
            and isinstance(k.value, str)
            and ANY_KEY.match(k.value)
            for k in n.keys
            if k is not None
        )
        and None not in n.keys
    ):
        return [k.value for k in n.keys]
    return None


def dotted(n) -> str | None:
    parts = []
    while isinstance(n, ast.Attribute):
        parts.append(n.attr)
        n = n.value
    if isinstance(n, ast.Name):
        parts.append(n.id)
        return ".".join(reversed(parts))
    return None


def val_repr(node) -> str | None:
    if node is None:
        return None
    if isinstance(node, ast.Constant):
        v = node.value
        return "..." if v is Ellipsis else (v if isinstance(v, str) else str(v))
    try:
        return "expr:" + short(ast.unparse(node), 70)
    except Exception:  # noqa: BLE001
        return "expr:?"


class PyScan(ast.NodeVisitor):
    def __init__(
        self,
        inv: Inventory,
        repo: str,
        path: str,
        consts: ConstTable,
        force_scope: str | None = None,
        line_offset: int = 0,
        layer_tag: str = "",
    ) -> None:
        self.inv, self.repo, self.path, self.consts = inv, repo, path, consts
        self.scope = force_scope or scope_of(path)
        self.off = line_offset
        self.tag = layer_tag
        self.os_names = {"os"}
        self.environ_names: set[str] = set()
        self.getenv_names: set[str] = set()
        self.putenv_names: set[str] = set()
        self.env_alias: set[str] = set()
        self.settings_classes: dict[str, str] = {}
        self.module_has_pydantic_settings = False
        self.class_prefix: dict[str, str] = {}
        self.ctxmap: dict[int, str] = {}
        self.dictvals: dict[int, str | None] = {}
        self.loop_vars: dict[str, list] = {}
        self.discarded_calls: set[int] = set()

    # ----- helpers
    def line(self, node) -> int:
        return getattr(node, "lineno", 0) + self.off

    def rec(self, node, key, role, layer, **kw) -> None:
        if key is None:
            return
        self.inv.add(
            key=key,
            role=role,
            layer=layer + self.tag,
            repo=self.repo,
            path=self.path,
            line=self.line(node),
            scope=self.scope,
            unit=unit_of(self.repo, self.path, layer),
            **kw,
        )

    def key_of(self, n) -> tuple[str | None, bool]:
        if isinstance(n, ast.Constant) and isinstance(n.value, str):
            return (n.value, False) if ANY_KEY.match(n.value) else (None, False)
        if isinstance(n, ast.Name):
            v = self.consts.resolve(self.path, n.id)
            return (v, True) if v else (None, False)
        if isinstance(n, ast.Attribute):
            vals = self.consts.glob.get(n.attr)
            if vals and len(vals) == 1:
                return next(iter(vals)), True
            return None, False
        if isinstance(n, ast.JoinedStr) and all(
            isinstance(v, ast.Constant) for v in n.values
        ):
            s = "".join(v.value for v in n.values)
            return (s, False) if ANY_KEY.match(s) else (None, False)
        return None, False

    def resolve_seq(self, n) -> list[str] | None:
        seq = literal_str_seq(n)
        if seq:
            return seq
        if isinstance(n, ast.Name):
            if n.id in self.loop_vars:
                return self.loop_vars[n.id]
            if n.id in self.consts.seqs.get(self.path, {}):
                return self.consts.seqs[self.path][n.id]
            g = self.consts.gseqs.get(n.id)
            return g or None
        if (
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "keys"
            and not n.args
        ):
            return self.resolve_seq(n.func.value)
        return None

    def multi_keys(self, n) -> list[tuple[str, bool]]:
        """All keys a key expression can take: one for a constant, several for a loop variable over a literal sequence."""
        if isinstance(n, ast.Name) and n.id in self.loop_vars:
            return [(k, True) for k in self.loop_vars[n.id]]
        k, via = self.key_of(n)
        return [(k, via)] if k else []

    def _loop(self, target, iterable, body_visit) -> None:
        pushed = []
        seq = self.resolve_seq(iterable)
        names = (
            [target]
            if isinstance(target, ast.Name)
            else (list(target.elts)[:1] if isinstance(target, ast.Tuple) else [])
        )
        if seq and names and isinstance(names[0], ast.Name):
            pushed.append((names[0].id, self.loop_vars.get(names[0].id)))
            self.loop_vars[names[0].id] = seq
        body_visit()
        for name, old in pushed:
            if old is None:
                self.loop_vars.pop(name, None)
            else:
                self.loop_vars[name] = old

    def visit_For(self, node: ast.For) -> None:
        self.visit(node.iter)
        self._loop(
            node.target,
            node.iter,
            lambda: [self.visit(c) for c in node.body + node.orelse],
        )

    def _comp(self, node, elts) -> None:
        gens = list(node.generators)

        def run(i=0):
            if i == len(gens):
                for e in elts:
                    self.visit(e)
                return
            g = gens[i]
            self.visit(g.iter)
            self._loop(
                g.target, g.iter, lambda: ([self.visit(c) for c in g.ifs], run(i + 1))
            )

        run()

    def visit_ListComp(self, node):
        self._comp(node, [node.elt])

    def visit_SetComp(self, node):
        self._comp(node, [node.elt])

    def visit_GeneratorExp(self, node):
        self._comp(node, [node.elt])

    def visit_DictComp(self, node):
        self._comp(node, [node.key, node.value])

    def env_kind(self, n) -> str | None:
        """'os' for os.environ itself, 'mapping' for an env-like mapping, else None."""
        if (
            isinstance(n, ast.Attribute)
            and n.attr == "environ"
            and isinstance(n.value, ast.Name)
            and n.value.id in self.os_names
        ):
            return "os"
        if isinstance(n, ast.Name):
            if n.id in self.environ_names:
                return "os"
            if n.id in self.env_alias or n.id in ENV_MAP_NAMES:
                return "mapping"
        if isinstance(n, ast.Attribute) and n.attr in ENV_MAP_NAMES:
            return "mapping"
        return None

    def is_env_mapping_expr(self, v) -> bool:
        if self.env_kind(v):
            return True
        if isinstance(v, ast.Call):
            d = dotted(v.func) or ""
            if (
                d.endswith(".copy")
                and isinstance(v.func, ast.Attribute)
                and self.env_kind(v.func.value)
            ):
                return True
            if (
                d in ("dict", "copy.copy", "copy.deepcopy")
                and v.args
                and self.env_kind(v.args[0])
            ):
                return True
        if isinstance(v, ast.Dict):
            return any(
                k is None and self.env_kind(val) for k, val in zip(v.keys, v.values)
            )
        return False

    def getenv_func(self, f) -> bool:
        d = dotted(f)
        if not d:
            return False
        if "." in d:
            base, _, last = d.rpartition(".")
            return last == "getenv" and base in self.os_names
        return d in self.getenv_names

    # ----- prescan
    def prescan(self, tree: ast.AST) -> None:
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    if a.name == "os":
                        self.os_names.add(a.asname or "os")
            elif isinstance(node, ast.ImportFrom) and node.module == "os":
                for a in node.names:
                    n = a.asname or a.name
                    if a.name == "environ":
                        self.environ_names.add(n)
                    elif a.name == "getenv":
                        self.getenv_names.add(n)
                    elif a.name == "putenv":
                        self.putenv_names.add(n)
            elif (
                isinstance(node, ast.ImportFrom)
                and node.module
                and node.module.startswith("pydantic_settings")
            ):
                self.module_has_pydantic_settings = True
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and self.is_env_mapping_expr(node.value):
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        self.env_alias.add(t.id)

    # ----- reads
    def read(
        self,
        node,
        key_node,
        default_node,
        via,
        conf="high",
        required_hint=None,
        mapping=False,
    ) -> None:
        keys = self.multi_keys(key_node) if key_node is not None else []
        if not keys:
            if key_node is not None and via.startswith(("os.", "getenv")):
                self.inv.dynamic.append(
                    {
                        "repo": self.repo,
                        "path": self.path,
                        "line": self.line(node),
                        "expr": short(ast.unparse(key_node), 80),
                        "via": via,
                        "scope": self.scope,
                    }
                )
            return
        for key, via_const in keys:
            if mapping and not (UPPER_KEY.match(key) and len(key) >= 3):
                continue
            has_def = default_node is not None
            self.rec(
                node,
                key,
                "read",
                "py-env-read",
                default=val_repr(default_node),
                has_default=has_def,
                required=bool(required_hint),
                via=via + ("+const" if via_const else ""),
                conf=conf,
            )
            if default_node is not None:
                other = self.read_key(default_node)
                if other:
                    self.inv.edge(
                        key, other, "fallback", self.repo, self.path, self.line(node)
                    )

    def read_key(self, n) -> str | None:
        if isinstance(n, ast.Call):
            f = n.func
            if self.getenv_func(f) and n.args:
                return self.key_of(n.args[0])[0]
            if (
                isinstance(f, ast.Attribute)
                and f.attr in ("get", "pop")
                and self.env_kind(f.value)
                and n.args
            ):
                return self.key_of(n.args[0])[0]
        if isinstance(n, ast.Subscript) and self.env_kind(n.value):
            return self.key_of(n.slice)[0]
        return None

    def write(
        self, node, key_node, value_node, via, layer="py-env-write", conf="high"
    ) -> None:
        v = val_repr(value_node)
        refs = ()
        if value_node is not None:
            rk = self.read_key(value_node)
            if rk:
                refs = (rk,)
        for key, _ in self.multi_keys(key_node):
            self.rec(
                node,
                key,
                "write",
                layer,
                value=v,
                shape="literal" if isinstance(value_node, ast.Constant) else "dynamic",
                refs=refs,
                identity=bool(refs) and not isinstance(value_node, ast.Constant),
                via=via,
                conf=conf,
            )
            if refs:
                self.inv.edge(
                    refs[0], key, "copy", self.repo, self.path, self.line(node)
                )

    def dict_writes(self, node, dnode, via, layer="py-child-env", conf="high") -> None:
        if not isinstance(dnode, ast.Dict):
            return
        for k, v in zip(dnode.keys, dnode.values):
            if k is not None:
                self.write(node, k, v, via, layer, conf)

    # ----- visitors
    def visit_Expr(self, node: ast.Expr) -> None:
        if (
            isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Attribute)
            and node.value.func.attr == "pop"
        ):
            self.discarded_calls.add(id(node.value))
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr in ("get", "pop", "setdefault", "getenv")
            and node.args
            and isinstance(node.args[0], ast.Constant)
        ):
            self.ctxmap[id(node.args[0])] = "get"
        if (dotted(node.func) or "").split(".")[-1] == "env_vars":
            for arg in node.args:
                if isinstance(arg, ast.Dict):
                    for k in arg.keys:
                        if isinstance(k, ast.Constant):
                            self.ctxmap[id(k)] = "console-table"
        self.call(node)
        self.generic_visit(node)

    def call(self, node: ast.Call) -> None:
        f = node.func
        d = dotted(f)
        args = node.args
        kws = {k.arg: k.value for k in node.keywords if k.arg}
        # os.getenv(K[, default])
        if self.getenv_func(f):
            if args:
                self.read(
                    node,
                    args[0],
                    args[1] if len(args) > 1 else kws.get("default"),
                    "os.getenv",
                )
            return
        if isinstance(f, ast.Attribute):
            kind = self.env_kind(f.value)
            attr = f.attr
            if kind and attr == "get":
                if args:
                    self.read(
                        node,
                        args[0],
                        args[1] if len(args) > 1 else kws.get("default"),
                        "os.environ.get" if kind == "os" else "mapping.get",
                        conf="high" if kind == "os" else "med",
                        mapping=(kind != "os"),
                    )
                return
            if kind and attr == "pop":
                if id(node) in self.discarded_calls:
                    return
                if args:
                    self.read(
                        node,
                        args[0],
                        args[1] if len(args) > 1 else None,
                        "os.environ.pop" if kind == "os" else "mapping.pop",
                        conf="high" if kind == "os" else "med",
                        required_hint=False,
                        mapping=(kind != "os"),
                    )
                return
            if kind and attr == "setdefault":
                if args:
                    self.write(
                        node,
                        args[0],
                        args[1] if len(args) > 1 else None,
                        "os.environ.setdefault",
                        conf="high" if kind == "os" else "med",
                    )
                    self.read(
                        node,
                        args[0],
                        args[1] if len(args) > 1 else None,
                        "os.environ.setdefault",
                        conf="med",
                        required_hint=False,
                    )
                return
            if kind and attr == "update":
                if args:
                    self.dict_writes(
                        node,
                        args[0],
                        "os.environ.update",
                        "py-env-write",
                        "high" if kind == "os" else "med",
                    )
                for k, v in kws.items():
                    self.write(
                        node,
                        ast.Constant(value=k),
                        v,
                        "os.environ.update",
                        "py-env-write",
                    )
                return
            # os.putenv
            if (
                attr == "putenv"
                and isinstance(f.value, ast.Name)
                and f.value.id in self.os_names
                and len(args) >= 2
            ):
                self.write(node, args[0], args[1], "os.putenv")
                return
            # monkeypatch.setenv / delenv
            if attr in ("setenv", "delenv") and isinstance(
                f.value, (ast.Name, ast.Attribute)
            ):
                base = f.value.id if isinstance(f.value, ast.Name) else f.value.attr
                if base in TESTER_NAMES and args:
                    if attr == "setenv":
                        self.write(
                            node,
                            args[0],
                            args[1] if len(args) > 1 else None,
                            "monkeypatch.setenv",
                        )
                    else:
                        key, _ = self.key_of(args[0])
                        self.rec(
                            node,
                            key,
                            "unset",
                            "py-env-write",
                            via="monkeypatch.delenv",
                        )
                    return
        if isinstance(f, ast.Name) and f.id in self.putenv_names and len(args) >= 2:
            self.write(node, args[0], args[1], "os.putenv")
            return
        # patch.dict(os.environ, {...}) / mock.patch.dict("os.environ", {...})
        if d and d.endswith("patch.dict") and args:
            tgt = args[0]
            is_env = bool(self.env_kind(tgt)) or (
                isinstance(tgt, ast.Constant) and tgt.value == "os.environ"
            )
            if is_env:
                if len(args) > 1:
                    self.dict_writes(node, args[1], "patch.dict", "py-env-write")
                for k, v in kws.items():
                    self.write(node, ast.Constant(value=k), v, "patch.dict")
            return
        # dict(os.environ, K=v)
        if d == "dict" and args and self.env_kind(args[0]):
            for k, v in kws.items():
                self.write(
                    node,
                    ast.Constant(value=k),
                    v,
                    "dict(os.environ, ...)",
                    "py-child-env",
                )
        # Dagster EnvVar("K")
        if d and (d.split(".")[0] == "EnvVar" or d.split(".")[-1] == "EnvVar") and args:
            self.read(node, args[0], None, "dagster.EnvVar", required_hint=True)
            return
        # click/typer envvar=
        for name in ("envvar", "env_var"):
            if name in kws:
                v = kws[name]
                nodes = v.elts if isinstance(v, (ast.List, ast.Tuple)) else [v]
                for e in nodes:
                    self.read(node, e, None, "cli.envvar", required_hint=False)
        # subprocess-style env={...}
        for name in CHILD_ENV_KWARGS:
            if name in kws and isinstance(kws[name], ast.Dict):
                self.dict_writes(
                    node,
                    kws[name],
                    f"{name}=",
                    "py-child-env",
                    "high" if name == "env" else "med",
                )
        # helper functions: read_env("K", default) / set_env("K", v)
        last = (d or (f.attr if isinstance(f, ast.Attribute) else "")).split(".")[-1]
        if (
            last
            and args
            and isinstance(args[0], (ast.Constant, ast.Name, ast.Attribute))
        ):
            toks = [t for t in last.lower().strip("_").split("_") if t]
            if ({"env", "environ", "getenv", "envvar"} & set(toks)) and last not in (
                "getenv",
                "environ",
            ):
                key, _ = self.key_of(args[0])
                if key and UPPER_KEY.match(key) and len(key) >= 3:
                    if set(toks) & WRITE_WORDS:
                        self.write(
                            node,
                            args[0],
                            args[1] if len(args) > 1 else None,
                            f"helper:{last}",
                            conf="med",
                        )
                    else:
                        self.read(
                            node,
                            args[0],
                            args[1] if len(args) > 1 else kws.get("default"),
                            f"helper:{last}",
                            conf="med",
                            required_hint=bool(
                                {"require", "required", "need", "must"} & set(toks)
                            )
                            and len(args) < 2,
                        )

    def visit_Subscript(self, node: ast.Subscript) -> None:
        if isinstance(node.slice, ast.Constant):
            self.ctxmap[id(node.slice)] = (
                "sub-store" if isinstance(node.ctx, ast.Store) else "sub-load"
            )
        kind = self.env_kind(node.value)
        if kind and isinstance(node.ctx, ast.Load):
            self.read(
                node,
                node.slice,
                None,
                "os.environ[]" if kind == "os" else "mapping[]",
                conf="high" if kind == "os" else "med",
                required_hint=True,
                mapping=(kind != "os"),
            )
        elif kind and isinstance(node.ctx, ast.Del):
            key, _ = self.key_of(node.slice)
            self.rec(node, key, "unset", "py-env-write", via="del os.environ[]")
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        for t in node.targets:
            if isinstance(t, ast.Subscript):
                kind = self.env_kind(t.value)
                if kind:
                    self.write(
                        node,
                        t.slice,
                        node.value,
                        "os.environ[]=" if kind == "os" else "mapping[]=",
                        "py-env-write" if kind == "os" else "py-child-env",
                        "high" if kind == "os" else "med",
                    )
        self.generic_visit(node)

    def visit_Compare(self, node: ast.Compare) -> None:
        if (
            len(node.ops) == 1
            and isinstance(node.ops[0], (ast.In, ast.NotIn))
            and isinstance(node.left, ast.Constant)
        ):
            self.ctxmap[id(node.left)] = "in"
        if (
            len(node.ops) == 1
            and isinstance(node.ops[0], (ast.In, ast.NotIn))
            and self.env_kind(node.comparators[0])
        ):
            key, _ = self.key_of(node.left)
            self.rec(
                node,
                key,
                "read",
                "py-env-read",
                has_default=True,
                required=False,
                via="in os.environ",
                conf="med",
            )
        self.generic_visit(node)

    def visit_List(self, node: ast.List) -> None:
        elts = node.elts
        for i, e in enumerate(elts[:-1]):
            nxt = elts[i + 1]
            if (
                isinstance(e, ast.Constant)
                and e.value in ("-e", "--env")
                and isinstance(nxt, ast.Constant)
                and isinstance(nxt.value, str)
            ):
                m = re.match(r"^([A-Z][A-Z0-9_]*)=(.*)$", nxt.value, re.S)
                if m:
                    self.rec(
                        nxt,
                        m.group(1),
                        "write",
                        "py-docker-env",
                        value=m.group(2),
                        shape="literal",
                        via="docker -e",
                        conf="med",
                    )
        self.generic_visit(node)

    def visit_Dict(self, node: ast.Dict) -> None:
        for k, val in zip(node.keys, node.values):
            if isinstance(k, ast.Constant):
                self.ctxmap.setdefault(id(k), "dict-key")
                if isinstance(val, ast.Constant) and isinstance(val.value, str):
                    self.dictvals[id(k)] = val.value
        if any(k is None and self.env_kind(v) for k, v in zip(node.keys, node.values)):
            self.dict_writes(node, node, "{**os.environ, ...}", "py-child-env")
        self.generic_visit(node)

    def visit_BoolOp(self, node: ast.BoolOp) -> None:
        if isinstance(node.op, ast.Or):
            ks = [k for k in (self.read_key(v) for v in node.values) if k]
            for a, b in zip(ks, ks[1:]):
                self.inv.edge(a, b, "fallback", self.repo, self.path, self.line(node))
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        v = node.value
        if isinstance(v, str) and "${" in v and len(v) < 400:
            for m in re.finditer(r"\$\{([A-Z][A-Z0-9_]{2,})\}", v):
                self.inv.strings[(self.repo, self.path, "py")].append(
                    (m.group(1), self.line(node), "placeholder", None)
                )
        if isinstance(v, str) and STRICT_KEY.match(v):
            self.inv.strings[(self.repo, self.path, "py")].append(
                (
                    v,
                    self.line(node),
                    self.ctxmap.get(id(node), "plain"),
                    self.dictvals.get(id(node)),
                )
            )

    # ----- pydantic settings
    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        if self.is_settings(node):
            self.settings(node)
        if re.search(r"Env(Key|Var|Name)s?$", node.name) and any(
            "Enum" in (dotted(b) or "") for b in node.bases
        ):
            for st in node.body:
                val = st.value if isinstance(st, (ast.Assign, ast.AnnAssign)) else None
                if (
                    isinstance(val, ast.Constant)
                    and isinstance(val.value, str)
                    and UPPER_KEY.match(val.value)
                    and len(val.value) >= 3
                ):
                    self.rec(
                        st,
                        val.value,
                        "read",
                        "py-enum-read",
                        has_default=True,
                        required=False,
                        via=f"enum {node.name}",
                        conf="med",
                    )
        self.generic_visit(node)

    def is_settings(self, node: ast.ClassDef) -> bool:
        for b in node.bases:
            d = dotted(b) or ""
            last = d.split(".")[-1]
            if (
                last == "BaseSettings"
                or last in self.settings_classes
                or last in self.consts.settings_names
            ):
                self.settings_classes[node.name] = ""
                return True
            if last.endswith("Settings") and self.module_has_pydantic_settings:
                self.settings_classes[node.name] = ""
                return True
        return False

    def prefix_of(self, node: ast.ClassDef) -> str:
        prefix = ""
        for b in node.bases:
            d = (dotted(b) or "").split(".")[-1]
            if d in self.class_prefix:
                prefix = self.class_prefix[d]
        for st in node.body:
            val = None
            if isinstance(st, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "model_config" for t in st.targets
            ):
                val = st.value
            elif (
                isinstance(st, ast.AnnAssign)
                and isinstance(st.target, ast.Name)
                and st.target.id == "model_config"
            ):
                val = st.value
            elif isinstance(st, ast.ClassDef) and st.name == "Config":
                for s2 in st.body:
                    if isinstance(s2, ast.Assign) and any(
                        isinstance(t, ast.Name) and t.id == "env_prefix"
                        for t in s2.targets
                    ):
                        if isinstance(s2.value, ast.Constant):
                            prefix = str(s2.value.value)
            if isinstance(val, ast.Call):
                for k in val.keywords:
                    if k.arg == "env_prefix" and isinstance(k.value, ast.Constant):
                        prefix = str(k.value.value)
            elif isinstance(val, ast.Dict):
                for k, v in zip(val.keys, val.values):
                    if (
                        isinstance(k, ast.Constant)
                        and k.value == "env_prefix"
                        and isinstance(v, ast.Constant)
                    ):
                        prefix = str(v.value)
        self.class_prefix[node.name] = prefix
        return prefix

    @staticmethod
    def alias_strings(v) -> list[str]:
        out = []
        if isinstance(v, ast.Constant) and isinstance(v.value, str):
            out.append(v.value)
        elif isinstance(v, ast.Attribute):
            d = dotted(v) or ""
            if "RuntimeEnvKey." in d:
                parts = d.split(".")
                idx = parts.index("RuntimeEnvKey")
                if len(parts) > idx + 1:
                    out.append(parts[idx + 1])
        elif isinstance(v, ast.Call):
            d = (dotted(v.func) or "").split(".")[-1]
            if d == "AliasChoices":
                for a in v.args:
                    out.extend(PyScan.alias_strings(a))
        return out

    def settings(self, node: ast.ClassDef) -> None:
        prefix = self.prefix_of(node)
        for st in node.body:
            if not (isinstance(st, ast.AnnAssign) and isinstance(st.target, ast.Name)):
                continue
            fname = st.target.id
            if fname.startswith("_") or fname == "model_config":
                continue
            ann = ast.unparse(st.annotation) if st.annotation is not None else ""
            if "ClassVar" in ann:
                continue
            call = (
                st.value
                if isinstance(st.value, ast.Call)
                and (dotted(st.value.func) or "").split(".")[-1] == "Field"
                else None
            )
            if call is None:
                for sub in ast.walk(st.annotation) if st.annotation is not None else []:
                    if (
                        isinstance(sub, ast.Call)
                        and (dotted(sub.func) or "").split(".")[-1] == "Field"
                    ):
                        call = sub
                        break
            has_default, default, required = True, None, False
            names: list[str] = []
            if call is not None:
                kw = {k.arg: k.value for k in call.keywords if k.arg}
                if "validation_alias" in kw:
                    names = self.alias_strings(kw["validation_alias"])
                elif "alias" in kw:
                    names = self.alias_strings(kw["alias"])
                elif "env" in kw:
                    names = self.alias_strings(kw["env"])
                dflt = None
                if call.args:
                    dflt = call.args[0]
                if "default" in kw:
                    dflt = kw["default"]
                if "default_factory" in kw:
                    has_default, default = True, "factory"
                elif dflt is None or (
                    isinstance(dflt, ast.Constant) and dflt.value is Ellipsis
                ):
                    has_default, required = False, True
                    if st.value is not call:  # Annotated[...] without default value
                        has_default, required = (
                            (st.value is not None),
                            (st.value is None),
                        )
                        default = val_repr(st.value)
                else:
                    default = val_repr(dflt)
            else:
                if st.value is None:
                    has_default, required = False, True
                else:
                    default = val_repr(st.value)
            if not names:
                names = [(prefix + fname)]
            names = [n.upper() for n in names]
            for i, n in enumerate(names):
                self.rec(
                    st,
                    n,
                    "read",
                    "py-settings",
                    default=default,
                    has_default=has_default,
                    required=required,
                    via="pydantic-settings",
                    conf="high",
                    extra={
                        "class": node.name,
                        "field": fname,
                        "secret": "SecretStr" in ann,
                        "alias_index": i,
                        "aliases": names[1:] if i == 0 else [names[0]],
                    },
                )
            for a, b in zip(names, names[1:]):
                self.inv.edge(a, b, "alias", self.repo, self.path, self.line(st))


def build_consts(repo: str, root: Path, files: list[str]) -> ConstTable:
    table = ConstTable()
    for f in files:
        if route(f) != "python":
            continue
        try:
            tree = ast.parse((root / f).read_text("utf-8", "replace"))
        except (SyntaxError, ValueError, OSError):
            continue
        table.collect(f, tree)
    table.finish()
    return table


def scan_python_text(
    inv: Inventory, repo: str, path: str, text: str, consts: ConstTable, **kw
) -> None:
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError) as exc:
        inv.errors.append(f"python parse: {repo}:{path}: {exc.__class__.__name__}")
        return
    sc = PyScan(inv, repo, path, consts, **kw)
    sc.prescan(tree)
    sc.visit(tree)


# ---------------------------------------------------------------------------------------------
# shell
# ---------------------------------------------------------------------------------------------
HEREDOC = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")
EXPORT_LINE = re.compile(r"^\s*(?:export|declare\s+-x|typeset\s+-x)\s+(.+)$")
ASSIGN_LINE = re.compile(
    r"^\s*(?:(local|readonly|declare)\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$"
)
INLINE_ASSIGN = re.compile(
    r"^\s*((?:[A-Za-z_][A-Za-z0-9_]*=(?:\"[^\"]*\"|'[^']*'|(?![\"'])[^\s;&|]*)\s+)+)(?![A-Za-z_][A-Za-z0-9_]*=)(?=[A-Za-z./$\"'])"
)
ENV_CMD = re.compile(
    r"\benv\s+((?:[A-Za-z_][A-Za-z0-9_]*=(?:\"[^\"]*\"|'[^']*'|\S*)\s+)+)"
)
DOCKER_E = re.compile(
    r"(?:^|\s)(?:-e|--env)[ =][\"']?([A-Za-z_][A-Za-z0-9_]*)(?:=(\"[^\"]*\"|'[^']*'|[^\"'\s]*))?"
)
REMOTE_INLINE = re.compile(
    r"(?<![A-Za-z0-9_])([A-Z][A-Z0-9_]{2,})=((?:'[^']*')|(?:\"[^\"]*\")|[^\s'\"]+)\s+(?:bash|sh|zsh|python3?|node|env|docker|ssh|sudo)\b"
)
ASSIGN_ANY = re.compile(
    r"(?:^|[;&|!({]|\b(?:if|then|do|while|until|elif)\b)\s*!?\s*([A-Za-z_][A-Za-z0-9_]*)="
)
LOCAL_BIND = [
    re.compile(
        r"\b(?:mapfile|readarray)\s+(?:-[A-Za-z]+\s+(?:[0-9]+\s+)?)*([A-Za-z_][A-Za-z0-9_]*)"
    ),
    re.compile(r"\bfor\s+([A-Za-z_][A-Za-z0-9_]*)\s+in\b"),
    re.compile(r"\bread\s+(?:-[A-Za-z]+\s+)*((?:[A-Za-z_][A-Za-z0-9_]*\s*)+)"),
    re.compile(r"\bgetopts\s+\S+\s+([A-Za-z_][A-Za-z0-9_]*)"),
    re.compile(
        r"\b(?:local|declare|typeset)\s+(?:-[A-Za-z]+\s+)*([A-Za-z_][A-Za-z0-9_]*)"
    ),
    re.compile(r"\bselect\s+([A-Za-z_][A-Za-z0-9_]*)\s+in\b"),
]
SHELL_BUILTINS = {
    "REPLY",
    "OPTARG",
    "OPTIND",
    "OPTERR",
    "IFS",
    "NF",
    "NR",
    "FS",
    "OFS",
    "RS",
    "ORS",
    "FNR",
    "RSTART",
    "RLENGTH",
    "BASH_SOURCE",
    "LINENO",
    "RANDOM",
    "SECONDS",
    "PIPESTATUS",
}
GITHUB_ENV_WRITE = re.compile(
    r"[\"']?([A-Za-z_][A-Za-z0-9_]*)=([^\"'\n]*)[\"']?\s*>>\s*[\"']?\$\{?GITHUB_ENV\}?"
)
PYTHON_DASH_C = re.compile(r"\bpython[0-9.]*\s+-c\s+'([^']*)'")


def _split_assign_pairs(s: str) -> list[tuple[str, str]]:
    pairs = []
    try:
        toks = shlex.split(s, posix=True)
    except ValueError:
        toks = s.split()
    for t in toks:
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", t, re.S)
        if m:
            pairs.append((m.group(1), m.group(2)))
        elif re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", t):
            pairs.append((t, None))
    return pairs


def scan_shell_text(
    inv: Inventory,
    repo: str,
    path: str,
    text: str,
    layer_prefix: str,
    scope: str,
    line0: int,
    consts: ConstTable,
    unit_layer: str = "shell",
) -> None:
    lines = text.split("\n")
    assigned_local: dict[str, int] = {}
    exported: set[str] = set()
    eval_line: list[int] = []
    reads: list[tuple[str, int, str | None, bool]] = []
    writes: list[Occ] = []
    unit = unit_of(repo, path, unit_layer)

    def mk_write(key, value, ln, via, conf="high"):
        if not UPPER_KEY.match(key):
            return  # lower-case names are step-local shell variables, not environment variables
        shape, refs, identity = classify_value(key, value)
        o = Occ(
            key=key,
            role="write",
            layer=f"{layer_prefix}-export",
            repo=repo,
            path=path,
            line=ln,
            scope=scope,
            value=value,
            shape=shape,
            refs=refs,
            identity=identity,
            via=via,
            unit=unit,
            conf=conf,
        )
        writes.append(o)
        for r in refs:
            inv.edge(r, key, "copy" if identity else "derive", repo, path, ln)

    i = 0
    n = len(lines)
    while i < n:
        raw = lines[i]
        ln = line0 + i
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            i += 1
            continue
        # heredoc
        hm = HEREDOC.search(raw)
        body_lines: list[str] = []
        if hm:
            delim, quoted = hm.group(2), bool(hm.group(1))
            j = i + 1
            while j < n and lines[j].strip() != delim:
                body_lines.append(lines[j])
                j += 1
            head = raw[: hm.start()]
            is_py = bool(re.search(r"\bpython[0-9.]*\b", head))
            if is_py:
                scan_python_text(
                    inv,
                    repo,
                    path,
                    "\n".join(body_lines),
                    consts,
                    force_scope=scope,
                    line_offset=ln,
                    layer_tag="@heredoc",
                )
            elif not quoted:
                for k, bl in enumerate(body_lines):
                    for name, default, required, _ in interp_refs(bl):
                        if UPPER_KEY.match(name) and len(name) >= 2:
                            reads.append((name, ln + 1 + k, default, required))
            raw = head
            stripped = raw.strip()
            skip_to = j + 1
        else:
            skip_to = i + 1
        if re.search(r"(^|[;&|\s])eval\s+[\"']?\$\(", raw) or re.search(
            r"(^|[;&|\s])eval\s+\$", raw
        ):
            eval_line.append(ln)
        for pm in PYTHON_DASH_C.finditer(raw):
            scan_python_text(
                inv,
                repo,
                path,
                pm.group(1),
                consts,
                force_scope=scope,
                line_offset=ln - 1,
                layer_tag="@inline",
            )
        raw = re.sub(
            r"\b(?:ba|z)?sh\s+-[a-zA-Z]*c\s+'([^']*)'",
            lambda m: " ; " + m.group(1),
            raw,
        )  # sh -c '<script>' runs the script
        # line-level processing (single-quoted segments are literal)
        scan_text = re.sub(r"'[^']*'", "''", raw)
        for mb in ASSIGN_ANY.finditer(raw):
            if UPPER_KEY.match(mb.group(1)) and not EXPORT_LINE.match(raw):
                assigned_local.setdefault(mb.group(1), ln)
        for pat in LOCAL_BIND:
            for mb in pat.finditer(raw):
                for nm in mb.group(1).split():
                    if UPPER_KEY.match(nm):
                        assigned_local.setdefault(nm, ln)
        m = GITHUB_ENV_WRITE.search(raw)
        if m:
            mk_write(m.group(1), m.group(2), ln, "GITHUB_ENV")
        inline_pairs: list = []
        em = EXPORT_LINE.match(raw)
        if em:
            for k, v in _split_assign_pairs(em.group(1)):
                if v is None:
                    exported.add(k)
                    if k in assigned_local:
                        pass
                else:
                    exported.add(k)
                    mk_write(k, v, ln, "export")
        else:
            im = INLINE_ASSIGN.match(raw)
            inline_pairs = _split_assign_pairs(im.group(1)) if im else []
            is_inline = bool(im) and not any(
                v is not None and ("$(" in v or "`" in v) for _, v in inline_pairs
            )
            if is_inline:
                for k, v in inline_pairs:
                    if v is not None:
                        mk_write(k, v, ln, "inline-env", "med")
            else:
                am = ASSIGN_LINE.match(raw)
                if am and not am.group(1) == "local":
                    key, val = am.group(2), am.group(3)
                    assigned_local.setdefault(key, ln)
                    multi = _split_assign_pairs(raw.split(" #", 1)[0])
                    if len(multi) > 1 and all(v is not None for _, v in multi):
                        for k2, _ in multi:
                            assigned_local.setdefault(k2, ln)
                    writes.append(
                        Occ(
                            key=key,
                            role="write",
                            layer=f"{layer_prefix}-local",
                            repo=repo,
                            path=path,
                            line=ln,
                            scope=scope,
                            value=val,
                            shape=classify_value(key, val)[0],
                            refs=classify_value(key, val)[1],
                            via="assign",
                            unit=unit,
                            conf="low",
                        )
                    )
        for mm in ENV_CMD.finditer(raw):
            for k, v in _split_assign_pairs(mm.group(1)):
                if v is not None:
                    mk_write(k, v, ln, "env-cmd", "med")
        inline_seen = {k for k, _ in inline_pairs}
        for mm in REMOTE_INLINE.finditer(raw):
            if mm.group(1) not in inline_seen:
                mk_write(
                    mm.group(1),
                    mm.group(2).strip("\"'"),
                    ln,
                    "inline-env (quoted command)",
                    "med",
                )
        for mm in DOCKER_E.finditer(raw):
            k, v = mm.group(1), mm.group(2)
            if UPPER_KEY.match(k):
                if v is None:
                    reads.append((k, ln, None, False))
                else:
                    mk_write(k, v.strip("\"'"), ln, "docker -e", "med")
        for name, default, required, _ in interp_refs(scan_text):
            if UPPER_KEY.match(name) and len(name) >= 2:
                reads.append((name, ln, default, required))
        i = skip_to
    if "${!" in text:
        for i2, raw2 in enumerate(lines):
            for tok in re.findall(
                r"(?<![A-Za-z0-9_$])([A-Z][A-Z0-9_]{3,})(?![A-Za-z0-9_])", raw2
            ):
                inv.strings[(repo, path, "sh-indirect")].append(
                    (tok, line0 + i2, "plain", None)
                )
    # emit
    for o in writes:
        if o.layer.endswith("-local"):
            if o.key in exported:
                o.layer = f"{layer_prefix}-export"
                o.conf = "med"
                inv.occ.append(o)
            continue
        inv.occ.append(o)
    for name, ln, default, required in reads:
        if name in SHELL_BUILTINS:
            continue
        if name in assigned_local and name not in exported:
            explicit_input = (default is not None or required) and ln <= assigned_local[
                name
            ]
            if not explicit_input:
                continue
        shape_defaults = {"default": default}
        after_eval = any(e < ln for e in eval_line)
        inv.add(
            key=name,
            role="read",
            layer=f"{layer_prefix}-read",
            repo=repo,
            path=path,
            line=ln,
            scope=scope,
            default=default,
            has_default=default is not None,
            required=required,
            via="$" + (" after eval" if after_eval else ""),
            unit=unit,
            conf="low" if (name in assigned_local or after_eval) else "med",
            extra=shape_defaults,
        )


# ---------------------------------------------------------------------------------------------
# YAML helpers
# ---------------------------------------------------------------------------------------------
def y_items(node):
    """Yield (key_node, value_node) pairs of a mapping node with merge keys expanded."""
    if not isinstance(node, yaml.MappingNode):
        return
    for k, v in node.value:
        if getattr(k, "tag", "") == "tag:yaml.org,2002:merge":
            subs = v.value if isinstance(v, yaml.SequenceNode) else [v]
            for s in subs:
                yield from y_items(s)
        else:
            yield k, v


def y_get(node, name):
    found = None
    for k, v in y_items(node):
        if isinstance(k, yaml.ScalarNode) and k.value == name:
            found = v
    return found


def is_third_party_image(image: str | None, has_build: bool) -> bool:
    if has_build:
        return False
    if not image:
        return True
    low = image.lower()
    mine = (
        "wangzitian0",
        "platform-",
        "truealpha",
        "finance",
        "report-",
        "${image_tag",
        "infra2",
    )
    return not any(m in low for m in mine)


def scan_compose(inv: Inventory, repo: str, path: str, text: str) -> None:
    try:
        root = yaml.compose(text, Loader=yaml.SafeLoader)
    except yaml.YAMLError as exc:
        inv.errors.append(f"compose yaml: {repo}:{path}: {exc.__class__.__name__}")
        return
    if root is None:
        return
    unit = unit_of(repo, path, "compose")
    scope = "prod"
    seen_lines: set[tuple] = set()

    def emit_env(service, third, key, value, ln, container=None):
        shape, refs, identity = classify_value(key, value)
        inv.add(
            key=key,
            role="write",
            layer="compose",
            repo=repo,
            path=path,
            line=ln,
            scope=scope,
            value=value,
            shape=shape,
            refs=refs,
            identity=identity,
            via="environment",
            unit=unit,
            extra={"service": service, "third_party": third, "container": container},
        )
        if refs and (identity or shape == "composed"):
            for r in refs:
                inv.edge(r, key, "copy" if identity else "derive", repo, path, ln)

    def do_mapping(service, third, node, container=None):
        if isinstance(node, yaml.MappingNode):
            for k, v in y_items(node):
                if isinstance(k, yaml.ScalarNode):
                    val = (
                        v.value
                        if isinstance(v, yaml.ScalarNode)
                        and v.tag != "tag:yaml.org,2002:null"
                        else (None if isinstance(v, yaml.ScalarNode) else "")
                    )
                    if (
                        isinstance(v, yaml.ScalarNode)
                        and v.tag == "tag:yaml.org,2002:null"
                    ):
                        val = None if v.value in ("", "~", "null") else v.value
                    emit_env(
                        service,
                        third,
                        k.value,
                        val,
                        k.start_mark.line + 1,
                        container,
                    )
        elif isinstance(node, yaml.SequenceNode):
            for it in node.value:
                if isinstance(it, yaml.ScalarNode):
                    s = it.value
                    if "=" in s:
                        k, _, v = s.partition("=")
                        emit_env(
                            service,
                            third,
                            k.strip(),
                            v,
                            it.start_mark.line + 1,
                            container,
                        )
                    else:
                        emit_env(
                            service,
                            third,
                            s.strip(),
                            None,
                            it.start_mark.line + 1,
                            container,
                        )

    def shell_blobs(node, key=None):
        """Scalars that a container shell runs: command/entrypoint values and anything holding $$."""
        if isinstance(node, yaml.ScalarNode):
            if key in ("command", "entrypoint") or "$$" in node.value:
                yield node
        elif isinstance(node, yaml.SequenceNode):
            if key in ("command", "entrypoint") and all(
                isinstance(x, yaml.ScalarNode) for x in node.value
            ):
                joined = yaml.ScalarNode(
                    "tag:yaml.org,2002:str",
                    "\n".join(x.value for x in node.value),
                    node.start_mark,
                    node.end_mark,
                )
                yield joined
            else:
                for x in node.value:
                    yield from shell_blobs(x, key)
        elif isinstance(node, yaml.MappingNode):
            for k, v in y_items(node):
                if isinstance(k, yaml.ScalarNode):
                    yield from shell_blobs(v, k.value)

    done_blobs = set()
    for blob in shell_blobs(root):
        sig = (blob.start_mark.line, blob.value)
        if sig in done_blobs:
            continue
        done_blobs.add(sig)
        line0 = blob.start_mark.line + (
            2 if getattr(blob, "style", None) in ("|", ">") else 1
        )
        scan_shell_text(
            inv,
            repo,
            path,
            blob.value.replace("$$", "$"),
            "compose-cmd",
            scope,
            line0,
            ConstTable(),
            unit_layer="compose",
        )

    services = y_get(root, "services")
    if isinstance(services, yaml.MappingNode):
        for sk, sv in y_items(services):
            service = sk.value
            build = y_get(sv, "build")
            image_node = y_get(sv, "image")
            image = (
                image_node.value if isinstance(image_node, yaml.ScalarNode) else None
            )
            third = is_third_party_image(image, build is not None)
            cn_node = y_get(sv, "container_name")
            container = cn_node.value if isinstance(cn_node, yaml.ScalarNode) else None
            do_mapping(service, third, y_get(sv, "environment"), container)
            if isinstance(build, yaml.MappingNode):
                do_mapping(service, False, y_get(build, "args"), container)
            ef = y_get(sv, "env_file")
            if ef is not None:
                vals = (
                    [ef.value]
                    if isinstance(ef, yaml.ScalarNode)
                    else [x.value for x in ef.value if isinstance(x, yaml.ScalarNode)]
                )
                for v in vals:
                    inv.add(
                        key="<env_file>",
                        role="read",
                        layer="compose",
                        repo=repo,
                        path=path,
                        line=ef.start_mark.line + 1,
                        scope=scope,
                        value=v,
                        via="env_file",
                        unit=unit,
                        extra={"service": service},
                    )
    # every interpolation in the file is a read of the deploy environment
    for idx, raw in enumerate(text.split("\n")):
        s = raw.strip()
        if s.startswith("#"):
            continue
        body = (
            raw.split(" #", 1)[0]
            if '"' not in raw.split(" #", 1)[-1] and "'" not in raw.split(" #", 1)[-1]
            else raw
        )
        for name, default, required, braced in interp_refs(body):
            if UPPER_KEY.match(name) or ANY_KEY.match(name):
                sig = (name, idx)
                if sig in seen_lines:
                    continue
                seen_lines.add(sig)
                inv.add(
                    key=name,
                    role="read",
                    layer="compose-interp",
                    repo=repo,
                    path=path,
                    line=idx + 1,
                    scope=scope,
                    default=default,
                    has_default=default is not None,
                    required=required,
                    via="${}" if braced else "$",
                    unit=unit,
                )


# ---------------------------------------------------------------------------------------------
# Dockerfile
# ---------------------------------------------------------------------------------------------
def scan_dockerfile(inv: Inventory, repo: str, path: str, text: str) -> None:
    unit = unit_of(repo, path, "dockerfile")
    lines = text.split("\n")
    i = 0
    defined: set[str] = set()
    pending_reads: list[tuple[str, int, str | None]] = []
    while i < len(lines):
        ln = i + 1
        line = lines[i]
        i += 1
        if not line.strip() or line.strip().startswith("#"):
            continue
        full = line
        while full.rstrip().endswith("\\") and i < len(lines):
            full = full.rstrip()[:-1] + " " + lines[i].strip()
            i += 1
        parts = full.strip().split(None, 1)
        instr = parts[0].upper()
        rest = parts[1] if len(parts) > 1 else ""
        if instr == "ENV":
            if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", rest):
                for k, v in _split_assign_pairs(rest):
                    shape, refs, identity = classify_value(
                        k, v if v is not None else ""
                    )
                    defined.add(k)
                    inv.add(
                        key=k,
                        role="write",
                        layer="dockerfile",
                        repo=repo,
                        path=path,
                        line=ln,
                        value=v,
                        shape=shape,
                        refs=refs,
                        identity=identity,
                        via="ENV",
                        unit=unit,
                    )
                    for r in refs:
                        inv.edge(r, k, "copy" if identity else "derive", repo, path, ln)
            else:
                k, _, v = rest.partition(" ")
                if k:
                    shape, refs, identity = classify_value(k, v.strip())
                    defined.add(k)
                    inv.add(
                        key=k,
                        role="write",
                        layer="dockerfile",
                        repo=repo,
                        path=path,
                        line=ln,
                        value=v.strip(),
                        shape=shape,
                        refs=refs,
                        identity=identity,
                        via="ENV",
                        unit=unit,
                    )
        elif instr == "ARG":
            k, eq, v = rest.partition("=")
            k = k.strip()
            if k:
                defined.add(k)
                inv.add(
                    key=k,
                    role="write",
                    layer="dockerfile",
                    repo=repo,
                    path=path,
                    line=ln,
                    value=v if eq else None,
                    shape="literal" if eq and v else ("ref" if not eq else "empty"),
                    via="ARG",
                    unit=unit,
                    extra={"arg": True},
                )
        elif instr in (
            "RUN",
            "CMD",
            "ENTRYPOINT",
            "COPY",
            "ADD",
            "WORKDIR",
            "USER",
            "EXPOSE",
            "HEALTHCHECK",
        ):
            for name, default, _required, _ in interp_refs(rest):
                if UPPER_KEY.match(name) and len(name) >= 2:
                    pending_reads.append((name, ln, default))
    for name, ln, default in pending_reads:
        inv.add(
            key=name,
            role="read",
            layer="dockerfile-run",
            repo=repo,
            path=path,
            line=ln,
            default=default,
            has_default=default is not None,
            via="$",
            unit=unit,
            conf="med" if name in defined else "low",
        )


# ---------------------------------------------------------------------------------------------
# GitHub workflows
# ---------------------------------------------------------------------------------------------
GH_EXPR = re.compile(r"\$\{\{(.*?)\}\}", re.S)
GH_CTX = re.compile(r"\b(secrets|vars|env)\.([A-Za-z_][A-Za-z0-9_]*)")


def scan_workflow(
    inv: Inventory, repo: str, path: str, text: str, consts: ConstTable
) -> None:
    try:
        root = yaml.compose(text, Loader=yaml.SafeLoader)
    except yaml.YAMLError as exc:
        inv.errors.append(f"workflow yaml: {repo}:{path}: {exc.__class__.__name__}")
        return
    unit = unit_of(repo, path, "workflow")
    if root is None:
        return

    def walk(node, depth=0):
        if isinstance(node, yaml.MappingNode):
            for k, v in y_items(node):
                if (
                    isinstance(k, yaml.ScalarNode)
                    and k.value == "env"
                    and isinstance(v, yaml.MappingNode)
                ):
                    for ek, ev in y_items(v):
                        if isinstance(ek, yaml.ScalarNode):
                            raw = ev.value if isinstance(ev, yaml.ScalarNode) else ""
                            refs, identity, shape = [], False, "literal"
                            exprs = GH_EXPR.findall(raw)
                            if exprs:
                                shape = "composed"
                                for e in exprs:
                                    for ctx, name in GH_CTX.findall(e):
                                        refs.append(
                                            (
                                                "secret:"
                                                if ctx == "secrets"
                                                else "var:"
                                                if ctx == "vars"
                                                else ""
                                            )
                                            + name
                                        )
                                whole = GH_EXPR.fullmatch(raw.strip())
                                if whole and len(refs) == 1:
                                    shape, identity = (
                                        (
                                            "secret-ref"
                                            if refs[0].startswith("secret:")
                                            else "ref"
                                        ),
                                        True,
                                    )
                            elif raw == "":
                                shape = "empty"
                            inv.add(
                                key=ek.value,
                                role="write",
                                layer="workflow-env",
                                repo=repo,
                                path=path,
                                line=ek.start_mark.line + 1,
                                value=raw,
                                shape=shape,
                                refs=tuple(refs),
                                identity=identity,
                                via="env:",
                                unit=unit,
                            )
                            for r in refs:
                                if not r.startswith(("secret:", "var:")):
                                    inv.edge(
                                        r,
                                        ek.value,
                                        "copy" if identity else "derive",
                                        repo,
                                        path,
                                        ek.start_mark.line + 1,
                                    )
                elif (
                    isinstance(k, yaml.ScalarNode)
                    and k.value == "build-args"
                    and isinstance(v, yaml.ScalarNode)
                ):
                    for j, bl in enumerate(v.value.split("\n")):
                        bm = re.match(r"^\s*([A-Z][A-Z0-9_]*)=(.*)$", bl)
                        if bm:
                            shape, refs, identity = classify_value(
                                bm.group(1), bm.group(2).strip()
                            )
                            inv.add(
                                key=bm.group(1),
                                role="write",
                                layer="workflow-build-arg",
                                repo=repo,
                                path=path,
                                line=v.start_mark.line
                                + (2 if v.style in ("|", ">") else 1)
                                + j,
                                value=bm.group(2).strip(),
                                shape=shape,
                                refs=refs,
                                identity=identity,
                                via="build-args",
                                unit=unit,
                            )
                elif (
                    isinstance(k, yaml.ScalarNode)
                    and k.value == "script"
                    and isinstance(v, yaml.ScalarNode)
                ):
                    scan_ts(
                        inv,
                        repo,
                        path,
                        v.value,
                        v.start_mark.line + (2 if v.style in ("|", ">") else 1),
                        scope_of(path),
                    )
                elif (
                    isinstance(k, yaml.ScalarNode)
                    and k.value == "run"
                    and isinstance(v, yaml.ScalarNode)
                ):
                    line0 = v.start_mark.line + (2 if v.style in ("|", ">") else 1)
                    scan_shell_text(
                        inv,
                        repo,
                        path,
                        v.value,
                        "workflow-run",
                        scope_of(path),
                        line0,
                        consts,
                        unit_layer="workflow",
                    )
                else:
                    walk(v, depth + 1)
        elif isinstance(node, yaml.SequenceNode):
            for it in node.value:
                walk(it, depth + 1)

    walk(root)
    for idx, raw in enumerate(text.split("\n")):
        for expr in GH_EXPR.findall(raw):
            for ctx, name in GH_CTX.findall(expr):
                if ctx == "env":
                    inv.add(
                        key=name,
                        role="read",
                        layer="workflow-expr",
                        repo=repo,
                        path=path,
                        line=idx + 1,
                        via="${{ env. }}",
                        unit=unit,
                    )
                else:
                    inv.add(
                        key=name,
                        role="read",
                        layer="workflow-expr",
                        repo=repo,
                        path=path,
                        line=idx + 1,
                        ns="gh-secret" if ctx == "secrets" else "gh-var",
                        via=f"${{{{ {ctx}. }}}}",
                        unit=unit,
                    )


# ---------------------------------------------------------------------------------------------
# templates, example files, manifests, TypeScript, docs
# ---------------------------------------------------------------------------------------------
def scan_ctmpl(inv: Inventory, repo: str, path: str, text: str) -> None:
    unit = unit_of(repo, path, "ctmpl")
    for idx, raw in enumerate(text.split("\n")):
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        for em in re.finditer(r'\benv\s+"([A-Z][A-Z0-9_]+)"', s):
            inv.add(
                key=em.group(1),
                role="read",
                layer="vault-template-read",
                repo=repo,
                path=path,
                line=idx + 1,
                via="env func",
                unit=unit,
                required=False,
            )
        m = re.match(r"^((?:\{\{-?[^}]*-?\}\}\s*)*)([A-Za-z_][A-Za-z0-9_]*)=(.*)$", s)
        if not m or s.startswith("{{-") and "=" not in s.split("}}")[-1]:
            continue
        key, expr = m.group(2), m.group(3)
        if m.group(1):
            expr = m.group(1) + expr
        field_ = re.findall(r"\.Data\.data\.([A-Za-z0-9_]+)", expr) or re.findall(
            r'index\s+\.Data\.data\s+"([A-Za-z0-9_]+)"', expr
        )
        secret_path = re.findall(r'secret\s+"([^"]+)"', expr)
        shape = "secret-ref" if ("{{" in expr) else "literal"
        inv.add(
            key=key,
            role="write",
            layer="vault-template",
            repo=repo,
            path=path,
            line=idx + 1,
            shape=shape,
            value=None if shape == "secret-ref" else expr,
            via="ctmpl",
            unit=unit,
            extra={
                "vault_field": field_[0] if field_ else None,
                "vault_path": secret_path[0] if secret_path else None,
                "optional": bool(m.group(1)),
            },
        )


def scan_env_example(inv: Inventory, repo: str, path: str, text: str) -> None:
    unit = unit_of(repo, path, "env-example")
    scope = scope_of(path)
    for idx, raw in enumerate(text.split("\n")):
        m = re.match(r"^\s*(#\s*)?(?:export\s+)?([A-Z][A-Z0-9_]*)=(.*)$", raw)
        if m:
            inv.add(
                key=m.group(2),
                role="declare",
                layer="env-example",
                repo=repo,
                path=path,
                line=idx + 1,
                scope=scope,
                value=m.group(3).strip(),
                shape=classify_value(m.group(2), m.group(3).strip())[0],
                via="commented" if m.group(1) else "example",
                unit=unit,
                conf="low" if m.group(1) else "high",
                extra={"commented": bool(m.group(1))},
            )


def scan_manifest(
    inv: Inventory,
    repo: str,
    path: str,
    text: str,
    *,
    synthetic_code_read: bool = False,
) -> None:
    unit = unit_of(repo, path, "manifest")
    try:
        data = json.loads(text)
    except ValueError as exc:
        inv.errors.append(f"manifest json: {repo}:{path}: {exc.__class__.__name__}")
        return
    lines = text.split("\n")
    for fld in data.get("fields", []) or []:
        key = fld.get("env") or fld.get("name")
        if not key:
            continue
        ln = next((i + 1 for i, line in enumerate(lines) if f'"{key}"' in line), 1)
        extra = {
            k: fld.get(k)
            for k in (
                "source",
                "sensitive",
                "injected",
                "empty_ok",
                "group",
                "provided_by",
                "composed_from",
                "aliases",
                "scope",
                "ci",
                "mirror_to_1password",
            )
            if k in fld
        }
        inv.add(
            key=key,
            role="declare",
            layer="manifest",
            repo=repo,
            path=path,
            line=ln,
            required=fld.get("required"),
            has_default=fld.get("has_default"),
            via=str(fld.get("source") or ""),
            unit=unit,
            extra=extra,
        )
        is_app_stack_code = extra.get("source") == "code" and (
            path.startswith("finance_report/finance_report/10.app")
            or path.startswith("truealpha/truealpha/10.app")
        )
        if synthetic_code_read or is_app_stack_code:
            inv.add(
                key=key,
                role="read",
                layer="py-settings",
                repo=repo,
                path=path,
                line=ln,
                required=bool(fld.get("required")),
                has_default=bool(fld.get("has_default", True)),
                via="app-manifest",
                unit=unit,
                extra={"secret": bool(fld.get("sensitive"))},
            )
        for ph in re.findall(
            r"\{env:([A-Z][A-Z0-9_]+)\}", str(fld.get("composed_from") or "")
        ):
            inv.add(
                key=ph,
                role="read",
                layer="manifest-compose",
                repo=repo,
                path=path,
                line=ln,
                via="{env:}",
                unit=unit,
                required=False,
            )
        for a in fld.get("aliases") or []:
            inv.edge(key, a, "alias", repo, path, ln)
            inv.add(
                key=a,
                role="declare",
                layer="manifest",
                repo=repo,
                path=path,
                line=ln,
                via="alias-of:" + key,
                unit=unit,
                conf="med",
                extra={"alias_of": key},
            )
            if synthetic_code_read:
                inv.add(
                    key=a,
                    role="read",
                    layer="py-settings",
                    repo=repo,
                    path=path,
                    line=ln,
                    required=bool(fld.get("required")),
                    has_default=bool(fld.get("has_default", True)),
                    via="app-manifest-alias",
                    unit=unit,
                )


TS_PATTERNS = [
    (
        re.compile(
            r"process\.env\.([A-Za-z_][A-Za-z0-9_]*)(?:\s*(?:\?\?|\|\|)\s*(\"[^\"]*\"|'[^']*'|`[^`]*`|[0-9]+))?"
        ),
        "process.env.",
    ),
    (
        re.compile(
            r"process\.env\[\s*[\"']([A-Za-z_][A-Za-z0-9_]*)[\"']\s*\](?:\s*(?:\?\?|\|\|)\s*(\"[^\"]*\"|'[^']*'|`[^`]*`|[0-9]+))?"
        ),
        "process.env[]",
    ),
    (
        re.compile(
            r"import\.meta\.env\.([A-Za-z_][A-Za-z0-9_]*)(?:\s*(?:\?\?|\|\|)\s*(\"[^\"]*\"|'[^']*'|`[^`]*`|[0-9]+))?"
        ),
        "import.meta.env.",
    ),
    (
        re.compile(r"Deno\.env\.get\(\s*[\"']([A-Za-z_][A-Za-z0-9_]*)[\"']\s*\)"),
        "Deno.env.get",
    ),
]
TS_DESTRUCT = re.compile(r"(?:const|let|var)\s*\{([^}]*)\}\s*=\s*process\.env")
TS_ASSIGN = re.compile(r"process\.env\.([A-Za-z_][A-Za-z0-9_]*)\s*=(?!=)")


def scan_ts(
    inv: Inventory,
    repo: str,
    path: str,
    text: str,
    line0: int = 1,
    scope: str | None = None,
) -> None:
    scope = scope or scope_of(path)
    unit = unit_of(repo, path, "ts")
    ts_aliases = set(
        re.findall(
            r"\b(?:const|let|var)\s+([A-Za-z_]\w*)\s*(?::[^=\n]+)?=\s*process\.env\b(?!\.)",
            text,
        )
    )
    for idx, raw in enumerate(text.split("\n"), line0 - 1):
        if raw.lstrip().startswith("//"):
            continue
        for pat, via in TS_PATTERNS:
            for m in pat.finditer(raw):
                key = m.group(1)
                dflt = (
                    m.group(2).strip("\"'`") if pat.groups >= 2 and m.group(2) else None
                )
                if (
                    TS_ASSIGN.search(raw)
                    and via == "process.env."
                    and re.search(
                        r"process\.env\." + re.escape(key) + r"\s*=(?!=)", raw
                    )
                ):
                    inv.add(
                        key=key,
                        role="write",
                        layer="ts-env-write",
                        repo=repo,
                        path=path,
                        line=idx + 1,
                        scope=scope,
                        via=via,
                        unit=unit,
                        conf="med",
                    )
                    continue
                inv.add(
                    key=key,
                    role="read",
                    layer="ts-env-read",
                    repo=repo,
                    path=path,
                    line=idx + 1,
                    scope=scope,
                    default=dflt,
                    has_default=dflt is not None,
                    required=False,
                    via=via,
                    unit=unit,
                )
        if re.search(r"(cloudflare|worker)", path, re.I):
            for m in re.finditer(r"(?<![A-Za-z0-9_.])env\.([A-Z][A-Z0-9_]{2,})", raw):
                inv.add(
                    key=m.group(1),
                    role="read",
                    layer="ts-env-read",
                    repo=repo,
                    path=path,
                    line=idx + 1,
                    scope=scope,
                    via="worker env.",
                    unit=unit,
                    conf="med",
                    required=False,
                )
        for alias in ts_aliases:
            for m in re.finditer(
                r"(?<![A-Za-z0-9_.])" + re.escape(alias) + r"\.([A-Z][A-Z0-9_]{2,})",
                raw,
            ):
                inv.add(
                    key=m.group(1),
                    role="read",
                    layer="ts-env-read",
                    repo=repo,
                    path=path,
                    line=idx + 1,
                    scope=scope,
                    via=f"{alias}.",
                    unit=unit,
                    conf="med",
                    required=False,
                )
        for m in re.finditer(
            r"(?<![A-Za-z0-9_.])env\(\s*[\"']([A-Z][A-Z0-9_]{2,})[\"']", raw
        ):
            inv.add(
                key=m.group(1),
                role="read",
                layer="ts-env-read",
                repo=repo,
                path=path,
                line=idx + 1,
                scope=scope,
                via="env()",
                unit=unit,
                conf="med",
                required=False,
            )
        for m in re.finditer(r"vi\.stubEnv\(\s*[\"']([A-Z][A-Z0-9_]{2,})[\"']", raw):
            inv.add(
                key=m.group(1),
                role="write",
                layer="ts-env-write",
                repo=repo,
                path=path,
                line=idx + 1,
                scope="test",
                via="vi.stubEnv",
                unit=unit,
                conf="med",
            )
        for m in TS_DESTRUCT.finditer(raw):
            for part in m.group(1).split(","):
                name = part.split(":")[0].split("=")[0].strip()
                if name and ANY_KEY.match(name):
                    inv.add(
                        key=name,
                        role="read",
                        layer="ts-env-read",
                        repo=repo,
                        path=path,
                        line=idx + 1,
                        scope=scope,
                        via="destructure",
                        unit=unit,
                        conf="med",
                        has_default="=" in part,
                    )
        for tok in re.findall(r"[\"']([A-Z][A-Z0-9_]{2,})[\"']", raw):
            inv.strings[(repo, path, "ts")].append((tok, idx + 1, "plain", None))


def scan_wrangler(inv: Inventory, repo: str, path: str, text: str) -> None:
    import tomllib

    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        inv.errors.append(f"wrangler toml: {repo}:{path}: {exc.__class__.__name__}")
        return
    unit = unit_of(repo, path, "wrangler")
    tables = [("vars", data.get("vars") or {})]
    for envname, body in (data.get("env") or {}).items():
        tables.append((f"env.{envname}.vars", (body or {}).get("vars") or {}))
    lines = text.split("\n")
    for label, tbl in tables:
        for key, val in tbl.items():
            ln = next(
                (
                    i + 1
                    for i, line in enumerate(lines)
                    if re.match(rf"^\s*{re.escape(key)}\s*=", line)
                ),
                1,
            )
            inv.add(
                key=key,
                role="write",
                layer="wrangler",
                repo=repo,
                path=path,
                line=ln,
                value=str(val),
                shape="literal",
                via=label,
                unit=unit,
            )


def scan_make(inv: Inventory, repo: str, path: str, text: str) -> None:
    unit = unit_of(repo, path, "make")
    for idx, raw in enumerate(text.split("\n")):
        if raw.lstrip().startswith("#"):
            continue
        m = re.match(r"^\s*export\s+([A-Z][A-Z0-9_]*)\s*[:?+]?=\s*(.*)$", raw)
        if m:
            inv.add(
                key=m.group(1),
                role="write",
                layer="make",
                repo=repo,
                path=path,
                line=idx + 1,
                value=m.group(2).strip(),
                shape="literal",
                via="export",
                unit=unit,
                conf="med",
            )
            continue
        if raw.startswith(("\t", "  ")):
            lead = re.match(
                r"^\s*@?-?((?:[A-Z][A-Z0-9_]*=(?:\"[^\"]*\"|'[^']*'|\S*)\s+)+)(?=[A-Za-z./$])",
                raw,
            )
            if lead:
                for k, v in _split_assign_pairs(lead.group(1)):
                    if v is not None:
                        inv.add(
                            key=k,
                            role="write",
                            layer="make",
                            repo=repo,
                            path=path,
                            line=idx + 1,
                            value=v,
                            shape="literal",
                            via="inline-env",
                            unit=unit,
                            conf="med",
                        )


def scan_package_json(inv: Inventory, repo: str, path: str, text: str) -> None:
    try:
        data = json.loads(text)
    except ValueError:
        return
    unit = unit_of(repo, path, "package-json")
    for name, cmd in (data.get("scripts") or {}).items():
        if not isinstance(cmd, str):
            continue
        for m in re.finditer(
            r"(?:^|[\s&;])([A-Z][A-Z0-9_]+)=(\"[^\"]*\"|'[^']*'|\S*)", cmd
        ):
            inv.add(
                key=m.group(1),
                role="write",
                layer="package-json",
                repo=repo,
                path=path,
                line=1,
                value=m.group(2).strip("\"'"),
                shape="literal",
                via=f"scripts.{name}",
                unit=unit,
                conf="low",
            )


def scan_docs(inv: Inventory, repo: str, path: str, text: str) -> None:
    layer = "docs-ssot" if path.startswith("docs/ssot/") else "docs-other"
    for idx, raw in enumerate(text.split("\n")):
        for tok in re.findall(r"`([A-Z][A-Z0-9_]{2,})(?:=[^`]*)?`", raw):
            inv.strings[(repo, path, layer)].append((tok, idx + 1, "plain", None))
        for tok in re.findall(r"\$\{?([A-Z][A-Z0-9_]{2,})\}?", raw):
            inv.strings[(repo, path, layer)].append((tok, idx + 1, "plain", None))


# ---------------------------------------------------------------------------------------------
# driver
# ---------------------------------------------------------------------------------------------
def scan_repo(inv: Inventory, repo: str, root: Path) -> None:
    files = list_files(root)
    consts = build_consts(repo, root, files)
    for f in files:
        kind = route(f)
        if not kind:
            continue
        p = root / f
        if not p.is_file() or p.stat().st_size > MAX_BYTES:
            continue
        try:
            text = p.read_text("utf-8", "replace")
        except OSError:
            continue
        inv.counts[(repo, kind)] += 1
        if kind == "python":
            scan_python_text(inv, repo, f, text, consts)
        elif kind == "shell":
            scan_shell_text(inv, repo, f, text, "shell", scope_of(f), 1, consts)
        elif kind == "compose":
            scan_compose(inv, repo, f, text)
        elif kind == "dockerfile":
            scan_dockerfile(inv, repo, f, text)
        elif kind == "workflow":
            scan_workflow(inv, repo, f, text, consts)
        elif kind == "ctmpl":
            scan_ctmpl(inv, repo, f, text)
        elif kind == "env-example":
            scan_env_example(inv, repo, f, text)
        elif kind == "manifest":
            scan_manifest(inv, repo, f, text)
        elif kind == "ts":
            scan_ts(inv, repo, f, text)
        elif kind == "package-json":
            scan_package_json(inv, repo, f, text)
        elif kind == "make":
            scan_make(inv, repo, f, text)
        elif kind == "wrangler":
            scan_wrangler(inv, repo, f, text)
        elif kind == "docs":
            scan_docs(inv, repo, f, text)


DEPLOY_CODE = re.compile(
    r"(^|/)(libs/deploy/|tools/_lib/dev/pr_preview_lifecycle/|libs/core/environ\.py|libs/core/service_identity\.py|bootstrap/[^/]+/(sync_runner|tasks)\.py|platform/[^/]+/deploy\.py|"
    r"[^/]*/[^/]*/[^/]+/deploy\.py|shared_tasks\.py|tools/deploy_v2[a-z_]*\.py|libs/security/)"
)


def resolve_weak_refs(inv: Inventory) -> None:
    """A string literal or a doc token equal to a known key becomes a low-confidence record.

    The syntactic context decides the role: a dict key or a subscript store looks like a write
    (a deploy script that builds an env mapping), a subscript load or .get() looks like a read.
    """
    known = {
        o.key
        for o in inv.occ
        if o.ns == "env"
        and o.role in ("write", "read", "declare")
        and o.conf != "low"
        and o.key != "<env_file>"
    }
    for (repo, path, kind), toks in inv.strings.items():
        if kind == "py" and DEPLOY_CODE.search(path) and scope_of(path) == "prod":
            for tok, _ln, ctx, _sval in toks:
                if ctx in ("dict-key", "sub-store") and "_" in tok and len(tok) >= 5:
                    known.add(tok)
    have = {(o.repo, o.path, o.line, o.key) for o in inv.occ}
    ctx_role = {
        "dict-key": ("write", "py-dict-key"),
        "sub-store": ("write", "py-mapping-write"),
        "sub-load": ("read", "py-mapping-read"),
        "get": ("read", "py-mapping-read"),
        "in": ("read", "py-mapping-read"),
        "placeholder": ("read", "py-placeholder"),
    }
    for (repo, path, kind), toks in inv.strings.items():
        layer = {
            "py": "str-ref",
            "ts": "str-ref",
            "sh-indirect": "shell-indirect",
        }.get(kind, kind)
        for tok, ln, ctx, sval in toks:
            if tok in known and (repo, path, ln, tok) not in have:
                have.add((repo, path, ln, tok))
                role, lay = ("ref", layer)
                if kind == "sh-indirect":
                    role = "read"
                conf = "low"
                if kind == "py" and ctx in ctx_role:
                    role, lay = ctx_role[ctx]
                    if role == "write" and DEPLOY_CODE.search(path):
                        lay, conf = "py-deploy-env", "med"
                inv.add(
                    key=tok,
                    role=role,
                    layer=lay,
                    repo=repo,
                    path=path,
                    line=ln,
                    scope="doc" if layer.startswith("docs") else scope_of(path),
                    conf=conf,
                    unit=unit_of(repo, path, layer),
                    value=sval if role == "write" else None,
                    shape="literal" if (role == "write" and sval) else None,
                )


def run(repos: dict[str, Path], *, include_app_manifests: bool = False) -> Inventory:
    inv = Inventory()
    for name, root in repos.items():
        scan_repo(inv, name, root)
    if include_app_manifests and "infra2" in repos:
        root = repos["infra2"]
        from libs.security import registry as sreg

        seen: set[str] = set()
        for s in sreg.SERVICES:
            for mp in s.manifests:
                if not mp.startswith("repos/") or mp in seen:
                    continue
                seen.add(mp)
                _, sub_repo, rest = mp.split("/", 2)
                if sub_repo in repos and (repos[sub_repo] / ".git").exists():
                    continue
                mf = sreg.manifest_file(mp, root=root)
                if not mf.exists():
                    sreg.load_manifest(mp, root=root)
                    mf = sreg.manifest_file(mp, root=root)
                if mf.exists():
                    scan_manifest(
                        inv,
                        sub_repo,
                        rest,
                        mf.read_text("utf-8", "replace"),
                        synthetic_code_read=True,
                    )
    resolve_weak_refs(inv)
    return inv


# ---------------------------------------------------------------------------------------------
# Report, SDK coverage, and contract checks
# ---------------------------------------------------------------------------------------------
STRONG_WRITE = {
    "compose",
    "dockerfile",
    "workflow-env",
    "workflow-run-export",
    "shell-export",
    "vault-template",
    "py-env-write",
    "py-child-env",
    "package-json",
    "ts-env-write",
    "make",
    "py-deploy-env",
    "wrangler",
    "compose-cmd-export",
    "workflow-build-arg",
    "py-docker-env",
}
WEAK_WRITE = {"py-dict-key", "py-mapping-write"}
STRONG_READ = {
    "py-env-read",
    "py-settings",
    "ts-env-read",
    "shell-read",
    "workflow-run-read",
    "compose-interp",
    "compose-cmd-read",
    "workflow-expr",
    "dockerfile-run",
    "py-enum-read",
    "vault-template-read",
    "manifest-compose",
}
WEAK_READ = {"py-mapping-read", "shell-indirect", "py-placeholder"}

PLATFORM = re.compile(
    r"^(PATH|HOME|USER|LOGNAME|SHELL|PWD|OLDPWD|TERM|TMPDIR|TEMP|TMP|LANG|LC_[A-Z_]+|HOSTNAME|HOST|UID|EUID|PPID|SHLVL|IFS|PS[0-9]|DISPLAY|EDITOR|PAGER|TZ|CI|"
    r"GITHUB_[A-Z_]+|RUNNER_[A-Z_]+|ACTIONS_[A-Z_]+|FORCE_JAVASCRIPT_ACTIONS_TO_NODE24|INPUT_[A-Z_]+|NODE_ENV|NODE_OPTIONS|PYTHON[A-Z_]*|VIRTUAL_ENV|CONDA[A-Z_]*|UV_[A-Z_]+|PIP_[A-Z_]+|"
    r"XDG_[A-Z_]+|SSH_[A-Z_]+|BASH[A-Z_]*|OSTYPE|MACHTYPE|HOSTTYPE|WSL_DISTRO_NAME|NO_COLOR|FORCE_COLOR|COLUMNS|LINES|DEBIAN_FRONTEND|PYTEST_[A-Z_]+|PLAYWRIGHT_[A-Z_]+|"
    r"HTTP_PROXY|HTTPS_PROXY|NO_PROXY|ALL_PROXY|SSL_CERT_[A-Z_]+|REQUESTS_CA_BUNDLE|CURL_CA_BUNDLE|DOCKER_[A-Z_]+|COMPOSE_[A-Z_]+|AWS_[A-Z_]+|"
    r"GOOGLE_[A-Z_]+|NEXT_RUNTIME|NEXT_TELEMETRY_DISABLED|VERCEL[A-Z_]*|npm_[a-z_]+|NPM_[A-Z_]+|HF_[A-Z_]+)$"
)
THIRD_PARTY = re.compile(
    r"^(OTEL_[A-Z_]+|POSTGRES_(USER|PASSWORD|DB|HOST_AUTH_METHOD|INITDB_[A-Z_]+)|PG[A-Z]*|PGDATA|MINIO_[A-Z_]+|CLICKHOUSE_[A-Z_]+|SIGNOZ_[A-Z_]+|GF_[A-Z_]+|GRAFANA_[A-Z_]+|"
    r"AUTHENTIK_[A-Z_]+|DAGSTER_[A-Z_]+|PREFECT_[A-Z_]+|HOMER_[A-Z_]+|TRAEFIK_[A-Z_]+|VAULT_[A-Z_]+|OP_[A-Z_]+|SENTRY_[A-Z_]+|DD_[A-Z_]+|GO[A-Z0-9]*|PI_[A-Z_]+|COVERAGE_[A-Z_]+|"
    r"NEXT_PUBLIC_[A-Z_]+|NPM_[A-Z_]+|SKIP_[A-Z_]+|REDIS_(ARGS|USERNAME)|RUSTFS_[A-Z_]+|OPENPANEL_[A-Z_]+|CGO_ENABLED)$"
)
SENSITIVE_RE = re.compile(
    r"(PASSWORD|PASSWD|(^|_)PASS($|_)|SECRET|TOKEN|API_?KEY|PRIVATE|CREDENTIAL|ACCESS_?KEY|SIGNING|ENCRYPTION|COOKIE|SALT|(^|_)DSN$|(^|_)KEY$|_KEY_|DATABASE_URL|CONNECTION_STRING)"
)
NOT_A_SECRET = re.compile(
    r"(_FILE|_PATH|_DIR|_REPORT|_ENABLED|_URL_FILE|^FREE_SHOW_|_SHOW_|MAX_TOKENS|_TOKENS$|_SECONDS$|_SECRETS_WAIT|_MODE$)"
)
PLACEHOLDER = re.compile(
    r"^(<.*>|\$\{.*\}|\*+|xxx+"
    r"|change[_-]?me|replace[_-]?me|your[_-][a-z_-]+|ci-placeholder[-a-z]*"
    r"|(dummy|fake|mock|placeholder|example|sample)(?:[_-][a-z]+)*"
    r"|(password|secret|test|none|null|todo|local|dev|ci|e2e|minioadmin|postgres|admin|root|guest))$",
    re.I,
)


def _is_ephemeral_preview_db_password(key: str, val: str, path: str) -> bool:
    return (
        key in ("PREVIEW_DB_PASSWORD", "POSTGRES_PASSWORD")
        and str(val).strip().lower() in ("preview", "ephemeral")
        and ("/preview/" in path or path.endswith("preview.py"))
    )


DEV_COMPOSE = re.compile(r"(^|/)docker-compose[^/]*\.ya?ml$")
DEPLOYED_LAYERS = {"compose-interp", "dockerfile", "dockerfile-run", "py-settings"}
DEPLOYED_CODE = re.compile(
    r"^(platform|bootstrap|apps|truealpha|finance_report|libs/deploy|libs/observability|libs/core)/"
)
DEV_URL = re.compile(
    r"(localhost|127\.0\.0\.1|@postgres|@db[:/]|@redis|@minio|@host\.docker\.internal)"
)
CI_FILE = re.compile(
    r"(^|/)(?:[a-z0-9_-]*-)?(ci[^/]*|test[^/]*|e2e[^/]*|mirror-[^/]*)\.ya?ml$|(^|/)docker-compose\.(ci|dev|e2e|test)[^/]*\.ya?ml$|dev_backend\.py$"
)
ENV_TIER = re.compile(r"(^|_)(ENV|ENVIRONMENT|TIER)$")
ENV_TIER_NOT = re.compile(r"(FILE|SUFFIX|PATH|DIR|VAR|NAME_ENV)")
RELEASE_ID = re.compile(
    r"(GIT_COMMIT|COMMIT_SHA|GIT_SHA|IAC_REF|IMAGE_TAG|RELEASE_TAG|BUILD_SHA|(^|_)SHA$|(^|_)REF$|APP_VERSION|RELEASE_VERSION|IMAGE_DIGEST|REVISION|CONFIG_HASH)"
)
FAILOPEN_ENV = {"production", "prod"}
FAILOPEN_ID = {
    "unknown",
    "dev",
    "dev-local",
    "latest",
    "none",
    "local",
    "main",
    "master",
    "0",
    "working-tree",
    "local-unpinned",
}

SDK_EXTRA = {
    "GITHUB_ACTIONS": "helper",
    "VAULT_ADDR": "helper",
    "VAULT_TOKEN": "helper",
    "VAULT_ROLE_ID": "helper",
    "VAULT_SECRET_ID": "helper",
    "GITHUB_TOKEN": "tool-token",
    "INFRA2_PAT": "tool-token",
    "ENV_SUFFIX": "template",
}
SDK_IDENTITY_ONLY = {
    "CONFIGURATION_SHA256",
    "GIT_COMMIT_SHA",
    "IMAGE_DIGEST",
    "RELEASE_ID",
}
SDK_READ_VAULT = {"VAULT_ADDR", "VAULT_TOKEN", "VAULT_ROLE_ID", "VAULT_SECRET_ID"}


def sdk_vocabulary() -> dict[str, dict]:
    """Read the SDK vocabulary via ``infra2_sdk.runtime.environment.runtime_env_contract()``."""
    import infra2_sdk
    from infra2_sdk.runtime import environment as sdk_env

    fn = getattr(infra2_sdk, "runtime_env_contract", None) or getattr(
        sdk_env, "runtime_env_contract"
    )
    data = fn()
    entries = data.get("variables") or data.get("keys") or ()
    vocab: dict[str, dict] = {}
    for item in entries:
        canon = item.get("name") or item.get("key")
        if not canon:
            continue
        vocab[canon] = {"kind": "canonical", "canonical": canon}
        for alias in item.get("aliases") or ():
            vocab[alias] = {"kind": "alias", "canonical": canon}
    for name, kind in SDK_EXTRA.items():
        vocab.setdefault(name, {"kind": kind, "canonical": name})
    return vocab


def _sdk_read_otel(vocab: dict[str, dict] | None = None) -> set[str]:
    """Return the SDK vocabulary names that ``OtelSettings.from_env`` looks up."""
    if vocab is None:
        try:
            vocab = sdk_vocabulary()
        except (ImportError, AttributeError):
            vocab = {}
    return {
        name
        for name, meta in vocab.items()
        if meta.get("kind") in ("canonical", "alias")
        and meta.get("canonical") not in SDK_IDENTITY_ONLY
    } | {"GITHUB_ACTIONS"}


SDK_READ_OTEL = _sdk_read_otel()


def base_layer(layer: str) -> str:
    return layer.split("@")[0]


@dataclass
class K:
    key: str
    occ: list = field(default_factory=list)

    # filled by analyze
    sensitive: bool = False
    flags: list = field(default_factory=list)
    info: dict = field(default_factory=dict)


def sites(occs, n=3):
    out, seen = [], set()
    for o in occs:
        s = f"{o.repo}:{o.path}:{o.line}"
        if s not in seen:
            seen.add(s)
            out.append(s)
        if len(out) >= n:
            break
    return out


def norm_default(v):
    if v is None:
        return None
    return str(v).strip().strip("\"'")


class UF:
    def __init__(self):
        self.p = {}

    def find(self, x):
        self.p.setdefault(x, x)
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[rb] = ra


def analyze(inv, groups=None, vocab=None):
    groups = groups or {}
    if vocab is None:
        try:
            vocab = sdk_vocabulary()
        except Exception:  # noqa: BLE001
            vocab = {}
    sdk_read_otel = _sdk_read_otel(vocab) if vocab else SDK_READ_OTEL
    keys: dict[str, K] = {}
    for o in inv.occ:
        if o.ns != "env" or o.key == "<env_file>":
            continue
        keys.setdefault(o.key, K(o.key)).occ.append(o)

    def prod_path(p):
        return scope_of(p) == "prod"

    keyname = re.compile(r"^[A-Z][A-Z0-9_]{2,}$")
    edges = [
        e
        for e in inv.edges
        if prod_path(e[4]) and keyname.match(str(e[0])) and keyname.match(str(e[1]))
    ]
    uf = UF()
    link_kinds = {"copy", "fallback", "alias"}
    for a, b, kind, _repo, _path, _line in edges:
        if kind in link_kinds:
            uf.union(a, b)
    for a, meta in (vocab or {}).items():
        if meta.get("kind") == "alias" and meta.get("canonical"):
            b = meta["canonical"]
            if keyname.match(a) and keyname.match(b):
                uf.union(a, b)
    comps = collections.defaultdict(set)
    for a, b, kind, *_ in edges:
        if kind in link_kinds:
            comps[uf.find(a)].add(a)
            comps[uf.find(a)].add(b)
    for a, meta in (vocab or {}).items():
        if meta.get("kind") == "alias" and meta.get("canonical"):
            b = meta["canonical"]
            if keyname.match(a) and keyname.match(b):
                comps[uf.find(a)].add(a)
                comps[uf.find(a)].add(b)
    comp_of = {}
    for r, members in comps.items():
        for m in members:
            comp_of[m] = r
    derived_from = collections.defaultdict(set)
    feeds = collections.defaultdict(set)
    for a, b, _kind, *_ in edges:
        derived_from[b].add(a)
        feeds[a].add(b)

    unit_first_party = collections.defaultdict(bool)
    unit_has_compose = collections.defaultdict(bool)
    for o in inv.occ:
        if o.layer == "compose" and o.role == "write":
            unit_has_compose[o.unit] = True
            if not o.extra.get("third_party"):
                unit_first_party[o.unit] = True

    def unit_third(u):
        return unit_has_compose[u] and not unit_first_party[u]

    lower = collections.defaultdict(set)
    for k in keys:
        lower[k.lower()].add(k)

    for k in keys.values():
        occs = k.occ
        prod = [o for o in occs if o.scope == "prod"]
        test = [o for o in occs if o.scope == "test"]
        w_strong = [
            o for o in prod if o.role == "write" and base_layer(o.layer) in STRONG_WRITE
        ]
        w_weak = [
            o for o in prod if o.role == "write" and base_layer(o.layer) in WEAK_WRITE
        ]
        decl = [
            o
            for o in prod
            if o.role == "declare" and not o.extra.get("commented") and o.conf != "low"
        ]
        man = [o for o in prod if o.role == "declare" and o.layer == "manifest"]
        man_primary = [o for o in man if not o.extra.get("alias_of")]
        ctm = [o for o in prod if o.layer == "vault-template"]
        envex = [o for o in prod if o.layer == "env-example"]
        r_strong = [
            o
            for o in prod
            if o.role == "read"
            and base_layer(o.layer) in STRONG_READ
            and o.conf != "low"
        ]
        r_weak = [
            o
            for o in prod
            if o.role == "read"
            and (
                base_layer(o.layer) in WEAK_READ
                or (base_layer(o.layer) in STRONG_READ and o.conf == "low")
            )
        ]
        refs = [o for o in prod if o.role == "ref" and o.layer == "str-ref"]
        docs_ssot = [o for o in occs if o.layer == "docs-ssot"]
        docs_other = [o for o in occs if o.layer == "docs-other"]
        compose_w = [o for o in w_strong if o.layer == "compose"]
        third_only = (
            bool(compose_w)
            and all(o.extra.get("third_party") for o in compose_w)
            and len(compose_w) == len(w_strong)
        )
        if w_strong and all(
            o.layer == "vault-template" and unit_third(o.unit) for o in w_strong
        ):
            third_only = True
        if man_primary and all(unit_third(o.unit) for o in man_primary):
            third_only = True

        name_looks_secret = bool(
            SENSITIVE_RE.search(k.key)
        ) and not NOT_A_SECRET.search(k.key)
        explicit_non_sensitive = (
            bool(man)
            and all(not o.extra.get("sensitive") for o in man)
            and not name_looks_secret
        )
        sensitive = (not explicit_non_sensitive) and (
            name_looks_secret
            or any(o.extra.get("sensitive") for o in man)
            or any(
                o.shape == "secret-ref" and o.layer == "vault-template" for o in prod
            )
            or any(o.extra.get("secret") for o in r_strong)
        )
        k.sensitive = sensitive

        defaults = []
        for o in r_strong:
            if (
                o.has_default
                and o.default is not None
                and not str(o.default).startswith("expr:")
                and o.default != "factory"
            ):
                defaults.append((norm_default(o.default), o))
        for o in w_strong:
            if (
                o.layer == "dockerfile"
                and o.shape == "literal"
                and not o.extra.get("arg")
            ):
                defaults.append((norm_default(o.value), o))

        def dval(v):
            if v is None:
                return None
            v = str(v).strip().strip("\"'").lower()
            if v in ("", "none", "null", "?", "-", "n/a") or re.match(r"^<.*>$", v):
                return None
            return v

        def deployed_site(o):
            return base_layer(o.layer) in DEPLOYED_LAYERS or (
                base_layer(o.layer) == "py-env-read"
                and bool(DEPLOYED_CODE.match(o.path))
            )

        by_group = collections.defaultdict(set)
        group_files = collections.defaultdict(set)
        for v, o in defaults:
            nv = dval(v)
            if nv is None:
                continue
            gid = groups.get(o.unit, o.unit)
            by_group[gid].add(nv)
            group_files[gid].add((o.repo, o.path))
        by_repo = by_group
        files = {f for fs in group_files.values() for f in fs}
        distinct_all = {v for v, _ in defaults}

        platform = bool(PLATFORM.match(k.key))
        third = bool(THIRD_PARTY.match(k.key)) or third_only

        supply_strong = bool(w_strong or decl)
        supply_weak = bool(w_weak)
        any_read_default = any(o.has_default for o in r_strong)

        test_writes = [o for o in test if o.role == "write"]
        test_reads = [o for o in test if o.role == "read" and o.conf != "low"]
        flags = []
        req_reads = [o for o in r_strong if o.required]
        if r_strong and not supply_strong and not platform:
            if supply_weak and not any_read_default:
                flags.append("READ_ONLY_WEAK_SUPPLY")
            elif any_read_default:
                flags.append("DEFAULT_IS_ONLY_SOURCE")
            elif test_writes and not supply_weak:
                flags.append("ONLY_TEST_SUPPLY")
            elif req_reads:
                flags.append("READ_NO_SUPPLY")
            else:
                flags.append("OPTIONAL_READ_NO_SUPPLY")
        if req_reads and any_read_default and not platform:
            flags.append("REQUIRED_VS_DEFAULT")
        if (
            any(not CI_FILE.search(o.path) for o in w_strong)
            and not (r_strong or r_weak or refs or man_primary or test_reads)
            and not platform
            and not third
        ):
            flags.append("WRITE_NO_READER")
        if (
            man_primary
            and not (r_strong or r_weak or refs or compose_w or test_reads)
            and not platform
            and not third
        ):
            flags.append("DEAD_MANIFEST_ENTRY")
        if (
            envex
            and not (r_strong or r_weak or refs or man or w_strong or test_reads)
            and not platform
        ):
            flags.append("STALE_ENV_EXAMPLE")
        if any(len(vs) >= 2 for vs in by_repo.values()) and not platform:
            flags.append("DEFAULT_CONFLICT")
        elif any(
            len(fs) >= 2 and len(by_group[g]) == 1 for g, fs in group_files.items()
        ):
            flags.append("DEFAULT_COPIED")
        deployed_defaults = {dval(v) for v, o in defaults if deployed_site(o)} - {None}
        if platform:
            pass
        elif (
            ENV_TIER.search(k.key)
            and not ENV_TIER_NOT.search(k.key)
            and any(v in FAILOPEN_ENV for v in deployed_defaults)
        ):
            flags.append("FAILOPEN_DEFAULT")
        elif RELEASE_ID.search(k.key) and any(
            v in FAILOPEN_ID for v in deployed_defaults
        ):
            flags.append("FAILOPEN_DEFAULT")
        if sensitive:
            lit = [
                o
                for o in w_strong
                if o.layer
                in (
                    "compose",
                    "dockerfile",
                    "workflow-env",
                    "py-env-write",
                    "package-json",
                    "shell-export",
                    "workflow-run-export",
                    "py-deploy-env",
                )
                and o.shape == "literal"
                and o.value
                and not PLACEHOLDER.match(str(o.value).strip())
                and not _is_ephemeral_preview_db_password(k.key, str(o.value), o.path)
                and not DEV_URL.search(str(o.value))
                and not CI_FILE.search(o.path)
            ]
            if lit:
                flags.append("SECRET_LITERAL")
            if r_strong and not man and not ctm and not platform:
                flags.append("SECRET_OUTSIDE_CONTRACT")
            sdef = [
                o
                for o in r_strong
                if o.has_default
                and o.default
                and not str(o.default).startswith(("expr:", "${"))
                and not PLACEHOLDER.match(str(o.default).strip())
                and not _is_ephemeral_preview_db_password(k.key, str(o.default), o.path)
                and not DEV_URL.search(str(o.default))
                and not CI_FILE.search(o.path)
                and not DEV_COMPOSE.search(o.path)
            ]
            if sdef:
                flags.append("SECRET_DEFAULT")
        if comp_of.get(k.key):
            flags.append("ALIAS_GROUP")
        if len(lower[k.key.lower()]) > 1:
            flags.append("CASE_VARIANT")
        if not (r_strong or w_strong or decl or r_weak or w_weak) and (
            test or docs_ssot or docs_other or refs
        ):
            flags.append("ONLY_TESTS_OR_DOCS")
        if r_strong and not docs_ssot and not docs_other and not platform and not third:
            flags.append("UNDOCUMENTED")

        in_vocab = k.key in vocab
        sdk_called = k.key in sdk_read_otel or k.key in SDK_READ_VAULT
        consumer_reads = [
            o for o in prod if o.role == "read" and o.repo != "infra2-sdk"
        ]
        consumer_writes = [
            o for o in prod if o.role == "write" and o.repo != "infra2-sdk"
        ]
        if in_vocab:
            sdk_class = (
                "C1 SDK looks it up"
                if sdk_called
                else (
                    "C2 SDK names it, consumers handle it"
                    if (consumer_reads or consumer_writes or man_primary)
                    else "C3 SDK names it, nobody uses it"
                )
            )
        elif not consumer_reads:
            sdk_class = "C6 not read by production code"
        elif man_primary or ctm:
            sdk_class = "C4 registered in a manifest, SDK does not name it"
        else:
            sdk_class = "C5 wild"

        k.flags = flags
        k.info = dict(
            sdk_class=sdk_class,
            sdk_kind=vocab[k.key]["kind"] if in_vocab else "",
            sdk_canonical=vocab[k.key]["canonical"] if in_vocab else "",
            repos=sorted({o.repo for o in prod}),
            units=sorted({o.unit for o in prod}),
            w_layers=sorted({base_layer(o.layer) for o in w_strong}),
            w_weak=sorted({base_layer(o.layer) for o in w_weak}),
            decl_layers=sorted({base_layer(o.layer) for o in decl}),
            r_layers=sorted({base_layer(o.layer) for o in r_strong}),
            n_w=len(w_strong),
            n_decl=len(decl),
            n_r=len(r_strong),
            n_r_weak=len(r_weak),
            n_test=len(test),
            n_ref=len(refs),
            docs_ssot=len(docs_ssot),
            docs_other=len(docs_other),
            defaults=sorted({v for v in distinct_all if v is not None}),
            n_default_sites=len(files),
            manifest_sources=sorted({str(o.via) for o in man_primary if o.via}),
            in_manifest=bool(man_primary),
            in_ctmpl=bool(ctm),
            in_compose=bool(compose_w),
            in_env_example=bool(envex),
            comp=comp_of.get(k.key),
            derived_from=sorted(derived_from.get(k.key, ())),
            feeds=sorted(feeds.get(k.key, ())),
            platform=platform,
            third_party=third,
            sample=sites(w_strong + decl)[:2] + sites(r_strong)[:2],
            w_sites=sites(w_strong, 4),
            r_sites=sites(r_strong, 4),
        )
    return keys, comps, edges, comp_of


def red(k: K, v):
    if v in (None, ""):
        return v
    return "<redacted>" if k.sensitive else v


def service_groups(services) -> dict[str, str]:
    """unit -> group id. A stack directory and the app code it runs share one group."""
    g: dict[str, str] = {}
    for s in services:
        gid = f"svc:{s.directory}"
        g[f"infra2:{s.directory}"] = gid
        for repo, prefix in CODE_UNITS.get(s.id, []):
            if not s.preview:
                g[f"{repo}:{prefix}"] = gid
    return g


def load_services():
    from libs.security.registry import SERVICES

    return SERVICES


CODE_UNITS = {
    "finance_report/app": [("finance_report", "apps/backend")],
    "finance_report/preview": [("finance_report", "apps/backend")],
    "truealpha/app": [("truealpha", "apps/app-web"), ("truealpha", "apps/llm-service")],
    "truealpha/preview": [
        ("truealpha", "apps/app-web"),
        ("truealpha", "apps/llm-service"),
    ],
    "truealpha/data_engine": [("truealpha", "apps/data-engine"), ("truealpha", "libs")],
}


def contract(inv, keys, services, comp_of=None, comps=None):
    comp_of = comp_of or {}
    comps = comps or {}
    rows = []
    for s in services:
        mkeys, msrc = set(), {}
        for mp in s.manifests:
            if mp.startswith("repos/"):
                _, repo, rest = mp.split("/", 2)
            else:
                repo, rest = "infra2", mp
            for o in inv.occ:
                if (
                    o.layer == "manifest"
                    and o.repo == repo
                    and o.path == rest
                    and not o.extra.get("alias_of")
                ):
                    mkeys.add(o.key)
                    msrc[o.key] = o.extra.get("source")
        d = s.directory
        ctm = {
            o.key
            for o in inv.occ
            if o.layer == "vault-template"
            and o.repo == "infra2"
            and o.path == f"{d}/secrets.ctmpl"
        }
        comp = {}
        for o in inv.occ:
            if (
                o.layer == "compose"
                and o.role == "write"
                and o.repo == "infra2"
                and o.path.startswith(d + "/")
                and o.path.count("/") == d.count("/") + 1
            ):
                comp.setdefault(o.key, o)
        first_party = {k for k, o in comp.items() if not o.extra.get("third_party")}
        excl = set(s.exclude_envs) | set(s.store_only_keys)
        code_reads = collections.defaultdict(list)
        units = CODE_UNITS.get(s.id) or [("infra2", d)]
        for o in inv.occ:
            if (
                o.role == "read"
                and o.scope == "prod"
                and base_layer(o.layer) in ("py-env-read", "py-settings", "ts-env-read")
                and o.conf != "low"
            ):
                for repo, prefix in units:
                    if o.repo == repo and (
                        o.path == prefix or o.path.startswith(prefix + "/")
                    ):
                        code_reads[o.key].append(o)
        need_store = {
            k for k in mkeys if msrc.get(k) in ("runtime", "human", "bootstrap")
        }
        rows.append(
            dict(
                service=s.id,
                directory=d,
                manifests=len(s.manifests),
                manifest_keys=len(mkeys),
                ctmpl_keys=len(ctm),
                compose_keys=len(comp),
                ctmpl_not_in_manifest=sorted(ctm - mkeys - excl),
                store_key_not_rendered=sorted(need_store - ctm - excl),
                compose_first_party_not_in_manifest=sorted(
                    first_party - mkeys - ctm - excl
                ),
                code_reads=len(code_reads),
                code_reads_not_declared=sorted(
                    k
                    for k, os_ in code_reads.items()
                    if k not in mkeys
                    and k not in comp
                    and k not in ctm
                    and not any(
                        m in mkeys or m in comp or m in ctm
                        for m in comps.get(comp_of.get(k), ())
                    )
                    and not any(o.has_default for o in os_)
                    and not PLATFORM.match(k)
                    and not THIRD_PARTY.match(k)
                ),
                code_reads_default_only_not_declared=sorted(
                    k
                    for k, os_ in code_reads.items()
                    if k not in mkeys
                    and k not in comp
                    and k not in ctm
                    and not any(
                        m in mkeys or m in comp or m in ctm
                        for m in comps.get(comp_of.get(k), ())
                    )
                    and any(o.has_default for o in os_)
                    and not PLATFORM.match(k)
                    and not THIRD_PARTY.match(k)
                ),
            )
        )
    return rows


SEVERITY = {
    "READ_NO_SUPPLY": 3,
    "SECRET_LITERAL": 3,
    "SECRET_DEFAULT": 3,
    "SECRET_OUTSIDE_CONTRACT": 3,
    "DEFAULT_CONFLICT": 2,
    "FAILOPEN_DEFAULT": 2,
    "WRITE_NO_READER": 2,
    "DEAD_MANIFEST_ENTRY": 2,
    "READ_ONLY_WEAK_SUPPLY": 1,
    "ONLY_TEST_SUPPLY": 0,
    "OPTIONAL_READ_NO_SUPPLY": 1,
    "REQUIRED_VS_DEFAULT": 2,
    "STALE_ENV_EXAMPLE": 1,
    "DEFAULT_COPIED": 1,
    "CASE_VARIANT": 1,
    "DEFAULT_IS_ONLY_SOURCE": 0,
    "ALIAS_GROUP": 1,
    "UNDOCUMENTED": 0,
    "ONLY_TESTS_OR_DOCS": 0,
}
MEANING = {
    "READ_NO_SUPPLY": "read by code, required, and nobody sets it (rule 7: read but never written = defect)",
    "READ_ONLY_WEAK_SUPPLY": "read by code; only a dict key / mapping store (weak) looks like a producer",
    "ONLY_TEST_SUPPLY": "read by production code; only test code sets it (a test-only switch)",
    "OPTIONAL_READ_NO_SUPPLY": "optional read (get without default); nobody sets it (rule 7: a dead switch, or set by hand outside the repos)",
    "REQUIRED_VS_DEFAULT": "one site treats the key as required (subscript / :?), another site gives it a default",
    "DEFAULT_IS_ONLY_SOURCE": "read by code with a default; no producer anywhere (a code-only knob)",
    "WRITE_NO_READER": "set by compose/Dockerfile/workflow/template; no reader or reference in any repo (rule 7: written but never read = waste)",
    "DEAD_MANIFEST_ENTRY": "declared in a manifest; no code, compose or reference uses it",
    "STALE_ENV_EXAMPLE": ".env.example lists it; nothing reads or declares it",
    "DEFAULT_CONFLICT": "two different fallback defaults for the same key inside one repo",
    "DEFAULT_COPIED": "the same fallback default is stated in two or more files (a copy)",
    "FAILOPEN_DEFAULT": "environment tier defaults to production, or an identity value defaults to unknown/dev/latest",
    "SECRET_LITERAL": "secret-like key with a literal non-empty value in compose/Dockerfile/workflow/code",
    "SECRET_DEFAULT": "secret-like key with a literal non-empty fallback default in deployed code or compose",
    "SECRET_OUTSIDE_CONTRACT": "secret-like key read by first-party code with no manifest entry and no Vault template line",
    "ALIAS_GROUP": "key is linked to other names by a copy/fallback/alias edge (one fact, several names)",
    "CASE_VARIANT": "the same name exists with different letter case",
    "UNDOCUMENTED": "read by first-party code; named in no document",
    "ONLY_TESTS_OR_DOCS": "appears only in tests or documents",
}


def doc_only(inv, repos, vocab=None):
    """UPPER_SNAKE names in docs/ssot that appear in no code or config file of the repositories."""
    if vocab is None:
        try:
            vocab = sdk_vocabulary()
        except (ImportError, AttributeError):
            vocab = {}
    tok = re.compile(r"[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+")
    code_tokens = set(vocab)
    for _, root in repos.items():
        for f in list_files(root):
            if route(f) in (None, "docs") or f in (
                "tools/env_inventory.py",
                "libs/tests/test_env_inventory.py",
            ):
                continue
            p = root / f
            if p.is_file() and p.stat().st_size <= MAX_BYTES:
                code_tokens.update(tok.findall(p.read_text("utf-8", "replace")))
    if "infra2" in repos:
        from libs.security import registry as sreg

        infra_root = repos["infra2"]
        for s in sreg.SERVICES:
            for mp in s.manifests:
                mf = sreg.manifest_file(mp, root=infra_root)
                if mf.exists():
                    code_tokens.update(tok.findall(mf.read_text("utf-8", "replace")))
    out = collections.defaultdict(list)
    for (repo, path, layer), toks in inv.strings.items():
        if layer == "docs-ssot":
            for t, ln, _, _ in toks:
                if tok.fullmatch(t) and t not in code_tokens:
                    out[t].append(f"{repo}:{path}:{ln}")
    return dict(out)


def concept_rows(keys, comp_of, regex, not_regex=None):
    rows = []
    for k in sorted(keys.values(), key=lambda k: k.key):
        if (
            not regex.search(k.key)
            or (not_regex and not_regex.search(k.key))
            or k.info["platform"]
        ):
            continue
        i = k.info
        if not (i["n_w"] or i["n_r"] or i["decl_layers"]):
            continue
        rows.append(
            dict(
                key=k.key,
                writes=i["n_w"],
                reads=i["n_r"],
                defaults=i["defaults"][:3],
                comp=comp_of.get(k.key),
                repos=i["repos"],
                flags=k.flags,
            )
        )
    return rows


def write_outputs(
    out: pathlib.Path, inv, keys, comps, edges, comp_of, rows, heads, docs_only=None
):
    out.mkdir(parents=True, exist_ok=True)
    ordered = sorted(keys.values(), key=lambda k: k.key)

    with open(out / "env_inventory.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(
            [
                "key",
                "class",
                "sensitive",
                "repos",
                "writers",
                "n_writes",
                "declared_in",
                "readers",
                "n_reads",
                "n_weak_reads",
                "n_test_refs",
                "fallback_defaults",
                "n_default_files",
                "manifest_source",
                "in_manifest",
                "in_ctmpl",
                "in_compose",
                "in_env_example",
                "docs_ssot",
                "docs_other",
                "alias_group",
                "derived_from",
                "feeds",
                "flags",
                "write_sites",
                "read_sites",
            ]
        )
        for k in ordered:
            i = k.info
            w.writerow(
                [
                    k.key,
                    i.get("sdk_class", ""),
                    int(k.sensitive),
                    ",".join(i["repos"]),
                    ",".join(i["w_layers"] + [f"~{x}" for x in i["w_weak"]]),
                    i["n_w"],
                    ",".join(i["decl_layers"]),
                    ",".join(i["r_layers"]),
                    i["n_r"],
                    i["n_r_weak"],
                    i["n_test"],
                    ";".join(
                        ("<redacted>" if k.sensitive else d) for d in i["defaults"]
                    ),
                    i["n_default_sites"],
                    ",".join(i["manifest_sources"]),
                    int(i["in_manifest"]),
                    int(i["in_ctmpl"]),
                    int(i["in_compose"]),
                    int(i["in_env_example"]),
                    i["docs_ssot"],
                    i["docs_other"],
                    i["comp"] or "",
                    ",".join(i["derived_from"]),
                    ",".join(i["feeds"]),
                    ";".join(k.flags),
                    " | ".join(i["w_sites"]),
                    " | ".join(i["r_sites"]),
                ]
            )

    detail = {}
    for k in ordered:
        detail[k.key] = dict(
            sensitive=k.sensitive,
            flags=k.flags,
            info=k.info,
            occurrences=[
                dict(
                    role=o.role,
                    layer=o.layer,
                    site=f"{o.repo}:{o.path}:{o.line}",
                    scope=o.scope,
                    conf=o.conf,
                    via=o.via,
                    default=red(k, o.default),
                    value=red(k, o.value),
                    shape=o.shape,
                    refs=list(o.refs),
                    required=o.required,
                )
                for o in k.occ
            ],
        )
    with open(out / "env_inventory.json", "w") as fh:
        json.dump(
            {
                "heads": heads,
                "doc_only": docs_only,
                "keys": detail,
                "contract": rows,
                "alias_components": {r: sorted(m) for r, m in comps.items()},
                "dynamic": inv.dynamic,
                "parse_errors": inv.errors,
            },
            fh,
            indent=1,
            default=str,
        )

    flag_counts = collections.Counter(f for k in ordered for f in k.flags)
    class_counts = collections.Counter(
        k.info.get("sdk_class", "") for k in ordered if k.info.get("sdk_class")
    )
    md = []
    a = md.append
    a("# Environment-variable inventory\n")
    a(
        f"Scanned: {time.strftime('%Y-%m-%d %H:%MZ', time.gmtime())}. Heads: "
        + ", ".join(f"{n}@{h}" for n, h in heads.items())
        + "."
    )
    a(
        f"Keys (namespace env): **{len(ordered)}**. Records: {len(inv.occ)}. Parse errors: {len(inv.errors)}. Unresolved dynamic accesses: {len(inv.dynamic)}.\n"
    )
    a("## SDK classes\n")
    a("| Class | Keys |\n|---|---:|")
    for cls_name, cnt in sorted(class_counts.items()):
        a(f"| {cls_name} | {cnt} |")
    a("")
    a("## Flag counts\n")
    a("| Flag | Keys | Meaning |\n|---|---:|---|")
    for f, c in sorted(
        flag_counts.items(), key=lambda kv: (-SEVERITY.get(kv[0], 0), -kv[1])
    ):
        a(f"| {f} | {c} | {MEANING.get(f, '')} |")
    a("")
    for f in sorted(flag_counts, key=lambda x: (-SEVERITY.get(x, 0), x)):
        if f in ("UNDOCUMENTED", "ONLY_TESTS_OR_DOCS", "ALIAS_GROUP"):
            continue
        sel = [k for k in ordered if f in k.flags]
        a(f"## {f} ({len(sel)})\n\n{MEANING.get(f, '')}\n")
        a(
            "| Key | Class | Repos | Written by | Read by | Defaults | Sites |\n|---|---|---|---|---|---|---|"
        )
        for k in sel[:150]:
            i = k.info
            a(
                f"| `{k.key}` | {i.get('sdk_class', '')[:2]} | {','.join(i['repos'])} | {','.join(i['w_layers'] + i['decl_layers'])} | {','.join(i['r_layers'])} | "
                f"{'; '.join(('<redacted>' if k.sensitive else d) for d in i['defaults'])[:60]} | {' ; '.join(i['w_sites'][:1] + i['r_sites'][:1])} |"
            )
        if len(sel) > 150:
            a(f"\n… {len(sel) - 150} more in env_inventory.csv\n")
        a("")
    a("## Service contract check (libs/security/registry.SERVICES)\n")
    a(
        "| Service | manifest keys | ctmpl keys | compose keys | code reads | ctmpl not in manifest | store keys not rendered | first-party compose keys outside contract | code reads (no default) declared nowhere |\n|---|---:|---:|---:|---:|---|---|---|---|"
    )
    for r in rows:
        a(
            f"| {r['service']} ({r['directory']}) | {r['manifest_keys']} | {r['ctmpl_keys']} | {r['compose_keys']} | {r['code_reads']} | "
            f"{', '.join(r['ctmpl_not_in_manifest']) or '–'} | {', '.join(r['store_key_not_rendered']) or '–'} | "
            f"{', '.join(r['compose_first_party_not_in_manifest'][:8]) or '–'}{' …' if len(r['compose_first_party_not_in_manifest']) > 8 else ''} | "
            f"{', '.join(r['code_reads_not_declared'][:8]) or '–'}{' …' if len(r['code_reads_not_declared']) > 8 else ''} |"
        )
    (out / "env_inventory.md").write_text("\n".join(md) + "\n")
    return flag_counts


def default_repos(root: Path = REPO_ROOT) -> dict[str, Path]:
    repos: dict[str, Path] = {"infra2": root}
    for sub in ("finance_report", "truealpha", "infra2-sdk"):
        p = root / "repos" / sub
        if (p / ".git").exists():
            repos[sub] = p
    return repos


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--out",
        type=Path,
        default=REPO_ROOT / ".agents" / "runs" / "env-inventory",
        help="Output directory for CSV/JSON/Markdown reports (never inside tracked files).",
    )
    parser.add_argument(
        "--sdk",
        type=Path,
        default=None,
        help="Path to infra2-sdk repository root.",
    )
    parser.add_argument(
        "--infra2",
        type=Path,
        default=None,
        help="Path to infra2 repository root.",
    )
    parser.add_argument(
        "--finance-report",
        type=Path,
        default=None,
        help="Path to finance_report repository root.",
    )
    parser.add_argument(
        "--truealpha",
        type=Path,
        default=None,
        help="Path to truealpha repository root.",
    )
    args = parser.parse_args(argv)
    repos = default_repos((args.infra2 or REPO_ROOT).resolve())
    if args.infra2:
        repos["infra2"] = args.infra2.resolve()
    if args.sdk:
        repos["infra2-sdk"] = args.sdk.resolve()
    if args.finance_report:
        repos["finance_report"] = args.finance_report.resolve()
    if args.truealpha:
        repos["truealpha"] = args.truealpha.resolve()
    inv = run(repos, include_app_manifests=True)
    heads = {
        n: subprocess.run(
            ["git", "-C", str(p), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
        for n, p in repos.items()
    }
    services = load_services()
    keys, comps, edges, comp_of = analyze(inv, service_groups(services))
    rows = contract(inv, keys, services, comp_of, comps)
    fc = write_outputs(
        args.out, inv, keys, comps, edges, comp_of, rows, heads, doc_only(inv, repos)
    )
    print("keys", len(keys), "flags", dict(fc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
