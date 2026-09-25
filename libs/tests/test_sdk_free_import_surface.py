"""哪些模块必须在**没有 infra2-sdk** 的情况下可导入（审计 A1/A2，2026-09-23）。

**这条守卫的缺席造成过一次静默回归。** `libs/watchdog_issue_trail.py` 搬进 domain
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
    "libs.watchdog_issue_trail",
    "tools.out_of_band_watchdog",
    "libs.container_breakdown",
    "libs.secrets_registry",
    "libs.image_pins",
]

# **实测既有缺陷，不在本表** —— 记在这里而不是静默删掉，因为「名单里没有」和
# 「测过了确实不行」是两回事，后者需要有人去修。
#
# `libs.env`：搬迁前后**都**无法在屏蔽 SDK 时导入（实测 2026-09-23，两个 worktree
# 对照）。这直接打脸 infra2#847 的裁决依据——那里写着「`libs/env.py` 故意守着自己的
# SDK 导入，好让最小 CI job 不装 wheel 也能用 `verify_vault_token`」。它守不住。
# #847 的结论（libs.security 必须能无 SDK 导入）仍然成立，但**理由要换**：不是
# 「env.py 已经做到了」，而是「有 job 需要它做到，而它现在做不到」。已在 #847 补记。
KNOWN_BROKEN_NOT_A_REGRESSION = ("libs.env",)

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
        cwd=ROOT, capture_output=True, text=True, timeout=120,
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
        [sys.executable, "-c", _BLOCKER.format(module="libs.security", root=str(ROOT))],
        cwd=ROOT, capture_output=True, text=True, timeout=120,
        stdin=subprocess.DEVNULL,
    )
    assert proc.returncode != 0, (
        "libs.security 在屏蔽 SDK 后竟然导入成功——屏蔽器失效了，"
        "本文件其余断言的绿都不作数"
    )
