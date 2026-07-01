# 单测覆盖率不退化 SSOT

> **SSOT Key**: `ops.test_coverage`
> **核心定义**: `libs`/`tools` 单元测试行覆盖率的 no-regression 门禁；与 `ops.e2e`（活环境验证）是两个不同的层。

---

## 1. 真理来源 (The Source)

| 维度 | 物理位置 (SSOT) | 说明 |
|------|----------------|------|
| **覆盖率地板** | [`docs/ssot/coverage-baseline.json`](./coverage-baseline.json) | 提交在仓库里的 no-regression 基线 |
| **门禁逻辑** | [`libs/coverage_regression.py`](https://github.com/wangzitian0/infra2/blob/main/libs/coverage_regression.py) | 解析 Cobertura XML、加载/写入基线、判定是否退化 |
| **CLI 入口** | [`tools/coverage_regression_audit.py`](https://github.com/wangzitian0/infra2/blob/main/tools/coverage_regression_audit.py) | PR 门禁调用的命令 |
| **生成命令** | `infra-ci.yml`「Run infra unit tests」步骤 | `pytest --cov=libs --cov=tools --cov-report=xml:coverage/infra2-coverage.xml` |

---

## 2. 设计约束 (Dos & Don'ts)

### ✅ 推荐模式 (Whitelist)

- **模式 A**: 只做 no-regression（当前覆盖率不能低于基线），不设固定最低阈值——和 app 仓库 `docs/ssot/coverage.md` 的哲学一致，也是 `--cov-fail-under=0` 的原因：infra2 没有复杂业务逻辑要求的高覆盖率地板，重点是防止"悄悄退步"，不是追一个绝对数字。
- **模式 B**: 覆盖率提升后用 `--update-baseline` 显式提交新地板（review 过再涨，不是自动棘轮）。
- **模式 C**: Coveralls 只做 `main` 分支徽章/趋势展示（`continue-on-error: true`，只在 `push main` 时跑），**不参与卡 PR**——真正卡 PR 的是本仓库自己的脚本。这是刻意决定：外部服务的可用性/延迟不该进 merge 关键路径。

### ⛔ 禁止模式 (Blacklist)

- **反模式 A**: **禁止** 把 `e2e_regressions/`（打真实部署环境）的测试计入这个覆盖率分母——进程内行覆盖率测不到它，也不该测；那一层的"没覆盖"由 marker 分级（smoke/critical/full）+ smoke-guard 类机制单独把关。
- **反模式 B**: **禁止** 在覆盖率下降时用 `--update-baseline` 掩盖——脚本本身会拒绝（baseline 只在未下降或首次运行时可写）。

---

## 3. 标准操作程序 (Playbooks)

### SOP-001: 本地验证覆盖率门禁

```bash
uv run python -P -m pytest libs/tests -q --cov=libs --cov=tools \
  --cov-report=xml:coverage/infra2-coverage.xml --cov-fail-under=0
uv run python tools/coverage_regression_audit.py
```

### SOP-002: 提交一次覆盖率提升

```bash
uv run python tools/coverage_regression_audit.py --update-baseline
git add docs/ssot/coverage-baseline.json
```

---

## 4. 验证与测试 (The Proof)

| 行为描述 | 测试文件 | 状态 |
|----------|----------|------|
| 覆盖率比对逻辑（解析/加载/写入/判定退化） | `libs/tests/test_coverage_regression.py` | ✅ Implemented |
| CLI 接线（缺基线即失败、`--update-baseline` 复用同一判定） | `libs/tests/test_coverage_regression_audit_cli.py` | ✅ Implemented |
| CI 门禁接线 | `.github/workflows/infra-ci.yml`「Gate coverage regression」 | ✅ Implemented |

---

## Used by

- [docs/ssot/README.md](./README.md)
- [docs/ssot/ops.pipeline.md](./ops.pipeline.md)
