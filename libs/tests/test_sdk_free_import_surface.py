"""哪些模块必须在**没有 infra2-sdk** 的情况下可导入（审计 A1/A2，2026-09-23）。

**这条守卫的缺席造成过一次静默回归。** `libs/observability/issue_trail.py` 搬进 domain
package 之后，从纯 stdlib 变成了需要 SDK——因为它改为经 `libs.observability` 取符号，
而那个包当时 eager 导入 `probes.py`，后者无条件 `from infra2_sdk.runtime import ...`。

`ops-checks.yml` 的 watchdog job 只装 `httpx python-dotenv rich`。这条链的终点是
**out-of-band watchdog——别的都挂了之后来叫人的那个**，而且它 schedule-only，
没有任何 PR 会触发它。所以回归会在下一次 cron 才发作，表现是「告警系统自己崩了」。

当时两条 guard 全绿：`test_frozen_shims` 看的是 shim 形状，`test_workflow_runtime_deps`
只收集 `python -m <包>` 形式而 watchdog job 用的是路径形式与 heredoc。

**判据不是「哪些包不该 import SDK」，而是「哪些 job 不装 SDK，它们跑的模块就必须
不需要 SDK」。** 前者会随重构漂移，后者钉在 CI 的实际安装清单上。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent

# 这些模块被**不装 infra2-sdk 的 CI job** 直接执行或导入。
# 增删此表前先去 .github/workflows/ 核对那个 job 的 pip install 清单。
MUST_IMPORT_WITHOUT_SDK = [
    "libs.observability.issue_trail",
    "libs.observability.page_dedup",
    "tools.out_of_band_watchdog",
    "libs.observability.breakdown",
    "libs.security.registry",
    # #847：最小 GitHub Actions job 用 `verify_vault_token` / `generate_password` /
    # `VaultSecrets`，所以 `libs.env` 与 `libs.security`（它的 domain 归宿）都必须无 SDK 可导入。
    # `libs.security.__init__` 对 `supply` / `prune` 做惰性导出，才让后者成立。
    "libs.env",
    "libs.security",
]

# 这个包里**确实需要 SDK** 的公开名字（`supply.py` / `prune.py` 无条件 import infra2_sdk）。
# 它们被取用时必须以 ImportError 失败，而不是返回 None 或桩——静默的假值比崩溃更糟。
SDK_BACKED_SECURITY_NAMES = [
    "SupplyReport",
    "apply_secret_supply",
    "create_secrets_resolver",
    "prune_orphan_secrets",
]
# 不需要 SDK、必须在屏蔽 SDK 时仍可取用的名字（`store.py`，经 `libs.env` 的受保护导入）。
SDK_FREE_SECURITY_NAMES = [
    "VaultSecrets",
    "generate_secret_token",
    "resolve_vault_token",
]

_BLOCKER = """
import sys
# 子进程不继承本进程的 sys.path（尤其在 PYTHONSAFEPATH=1 下），仓库根必须显式加上。
# 第一版漏了这行，于是每条都以 "No module named 'libs'" 变红——**失败原因是环境，
# 不是被检查对象**。照着调名单让它变绿，就是拿假红换假绿。
sys.path.insert(0, {root!r})
from importlib.abc import MetaPathFinder


class _Block(MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name == "infra2_sdk" or name.startswith("infra2_sdk."):
            raise ImportError("No module named %r" % name)
        return None


sys.meta_path.insert(0, _Block())
__import__({module!r})
"""


@pytest.mark.parametrize("module", MUST_IMPORT_WITHOUT_SDK)
def test_module_imports_without_infra2_sdk(module: str) -> None:
    """在**子进程**里屏蔽 SDK 再导入。

    子进程而非 monkeypatch：本进程早已 import 过 SDK，`sys.modules` 里有缓存，
    在本进程内屏蔽只能测到一个已经被污染的状态——那样的绿是假的。
    """
    proc = subprocess.run(
        [sys.executable, "-c", _BLOCKER.format(module=module, root=str(ROOT))],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        stdin=subprocess.DEVNULL,
    )
    assert proc.returncode == 0, (
        f"{module} 在没有 infra2-sdk 时导入失败——而执行它的 CI job 不装 SDK。\n"
        f"{proc.stderr[-800:]}"
    )


def test_the_list_is_not_empty() -> None:
    """空表上参数化会零条通过，那是 GREEN-WHILE-EMPTY 不是通过。"""
    assert len(MUST_IMPORT_WITHOUT_SDK) >= 4


def test_a_module_that_genuinely_needs_the_sdk_still_fails() -> None:
    """反向断言：屏蔽器本身必须有效。

    没有这条，屏蔽器哪天失效（比如又用了废弃的 `find_module` API），上面每条都会
    变成「导入成功」而全绿——检查器坏掉和被检查对象没问题，表现完全一样。
    本会话实测踩过这个坑：第一版屏蔽器用 `find_module`，Python 3.12 不再调用它。
    """
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            _BLOCKER.format(module="libs.security.supply", root=str(ROOT)),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        stdin=subprocess.DEVNULL,
    )
    assert proc.returncode != 0, (
        "libs.security.supply 在屏蔽 SDK 后竟然导入成功——屏蔽器失效了，"
        "本文件其余断言的绿都不作数"
    )


_ACCESS_PROBE = """
import sys
sys.path.insert(0, {root!r})
from importlib.abc import MetaPathFinder


class _Block(MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name == "infra2_sdk" or name.startswith("infra2_sdk."):
            raise ImportError("No module named %r" % name)
        return None


sys.meta_path.insert(0, _Block())
import libs.security as security

for name in {free!r}:
    assert getattr(security, name) is not None, name

outcomes = {{}}
for name in {backed!r}:
    try:
        value = getattr(security, name)
    except ImportError as exc:
        outcomes[name] = "ImportError: %s" % exc
    else:
        outcomes[name] = "RETURNED %r" % (value,)
for name, outcome in outcomes.items():
    print(name, "=>", outcome)
"""


def test_sdk_backed_security_names_fail_loudly_without_the_sdk() -> None:
    """#847：包导入不要求 SDK，但需要 SDK 的**名字**被取用时必须明确报错。

    只断言「`import libs.security` 成功」会放过一种更坏的回归：把惰性导出写成
    `except ImportError: apply_secret_supply = None`——导入变绿，部署时却在 `None()`
    上崩。所以这里逐个取用，并要求每一个都以 ImportError 且点名 infra2_sdk 失败。
    """
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            _ACCESS_PROBE.format(
                root=str(ROOT),
                free=SDK_FREE_SECURITY_NAMES,
                backed=SDK_BACKED_SECURITY_NAMES,
            ),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        stdin=subprocess.DEVNULL,
    )
    assert proc.returncode == 0, (
        "屏蔽 SDK 后 `import libs.security` 或取用 SDK-free 名字失败：\n"
        f"{proc.stderr[-800:]}"
    )
    lines = [line for line in proc.stdout.splitlines() if "=>" in line]
    assert len(lines) == len(SDK_BACKED_SECURITY_NAMES), proc.stdout
    for line in lines:
        assert "=> ImportError:" in line and "infra2_sdk" in line, (
            f"SDK-backed 名字没有以点名 infra2_sdk 的 ImportError 失败：{line}"
        )


def test_security_lazy_exports_match_the_public_surface_with_the_sdk() -> None:
    """有 SDK 时，`__all__` 的每个名字都解析到真实对象，且与子模块是同一个对象。

    惰性导出的另一种坏法：`__all__` 里留着名字，`_SDK_BACKED` 里漏了它——无 SDK 的
    探针看不出来（它们本来就该失败），所以在有 SDK 的进程里再钉一次。
    """
    import libs.security as security
    from libs.security import prune, store, supply

    owners = {
        "SupplyReport": supply,
        "apply_secret_supply": supply,
        "create_secrets_resolver": supply,
        "prune_orphan_secrets": prune,
        "VaultSecrets": store,
        "generate_secret_token": store,
        "resolve_vault_token": store,
    }
    assert sorted(owners) == sorted(security.__all__)
    for name, owner in owners.items():
        assert getattr(security, name) is getattr(owner, name), name
    assert set(SDK_BACKED_SECURITY_NAMES) | set(SDK_FREE_SECURITY_NAMES) == set(
        security.__all__
    )
    # an unknown name is still an AttributeError (not an ImportError, not None) ...
    with pytest.raises(AttributeError, match="no attribute 'not_a_name'"):
        security.not_a_name  # noqa: B018
    # ... and dir() advertises the lazy names before they have been resolved
    assert set(security.__all__) <= set(dir(security))
