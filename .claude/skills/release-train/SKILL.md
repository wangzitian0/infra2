---
name: release-train
description: Infra2 Staging release train and open production decisions. Use after a merge to main, at a loop exit, and when a session starts.
---

# release-train: Staging train and production decisions (infra2 only)

Only the infra2 repository has a release train. Other repositories skip this skill.
The common skills `init`, `smoke`, `z` and `close` point here.

## 1. Read the state

Run this command in an infra2 checkout. It is read-only.

```bash
uv run python -m tools.harness status --fetch --online
```

- A release tag that the production marker has not reached waits for a production decision.
- A `hold` in the handover notes (`recall` skill) answers that decision.
- The `train_gate:` summary names `total_unpromoted_prs`, `oldest_unpromoted_age` and `prod_gatekeeper`.
- `docs/ssot/ops.pipeline.md` defines the production release process.

## 2. Staging train batching (after each merge)

- Batch at most 4 PRs on `main` ahead of Staging. This keeps fault localization within 4 PRs.
- With fewer than 4 pending PRs and another sub-goal, return to `init`.
- With 4 pending PRs, cut the latest `vX.Y.Z` tag on `origin/main`, then dispatch the train:
  `gh workflow run reconcile-iac-inputs.yml -f sync_app_releases=true`.
- Wait for that run and its health and log checks before you start a fifth PR.
- Before `close`, always dispatch the train for 1 to 4 unpromoted PRs.
- Verify Staging logs, `output_invariants`, alert and report completeness, and VPS resource health.

## 3. Failure

- A failed Preview or Staging train gets one forward-fix round of 15 minutes through `init`.
- After that round, revert the poison-pill PR on `main` and reopen its issue.

## 4. Preview teardown

At `close`, tear down the preview slot of the PR with `deploy_v2 --down`.

## 5. Production decision

- The `close` skill section 3 owns the thresholds and the question to the owner.
- The `deploy-evidence` skill owns the evidence chain and the production dispatch.
- Never dispatch production without the owner's live reply in the session (Root section `Production reservation`).
