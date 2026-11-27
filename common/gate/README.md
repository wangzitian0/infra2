# Governance & Merge Gate (`common/gate/`)

> **Purpose**: Deterministic PR merge-gate evaluator, GitHub `production` environment lock verification, workflow contract tables, and replay checks invoked by `python -m tools.pr_merge_gate`.

## Modules

| Module | Role |
|---|---|
| `types.py` | Shared constants, dataclasses (`HeadFacts`, `Verdict`), exit codes (`0` ready, `1` wait, `2` owner, `3` action/`review_required`, `4` unevaluable), and `gh` runner helpers |
| `production_contract.py` | Authoritative `production` environment selectors, ungated job secret allowlists, and unclassified scheduled job guards |
| `self_governance.py` | Self-governing review-set dependency closure (`self_governing_files()`) and direction proofs |
| `inventory.py` | Required-check workflow derivation from `docs/ssot/ci-gate-inventory.yaml` |
| `client.py` | Live PR, check-run, thread, and environment lock fact collection from GitHub |
| `review.py` | Weighted review-thread severity scoring (`>= 1.0` blocks) and independent verification comment parsing |
| `evaluator.py` | Pure verdict evaluation (`evaluate()`) and human/JSON rendering |
| `replay.py` | Replays recent merged PRs between `HEAD` and `origin/main` gates (`harness-replay`) |
| `pr_merge_gate.py` | CLI coordinator backing `tools/pr_merge_gate.py` (`docs/ssot/ops.merge-gate.md`) |

## Verification

```bash
.venv/bin/python -m pytest common/tests/test_gate_self_governance.py common/tests/test_pr_merge_gate.py common/tests/test_production_environment_gate.py common/tests/test_gate_replay.py -x
```
