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

## 2. Staging train (after each merge)

- Cut a release tag only with `uv run python tools/release_tag.py --push -m "<message>"`. It tags the head of `origin/main`.
- The tool prints `wait` while a tag train runs or waits, and in the 30 minutes after the start of the newest tag train.
- On `wait`, do other work and run the tool again. The pull requests that merge in that time ride the next tag.
- A dispatch with `after=vX.Y.Z` only replays the same tag.
- After a red train, read its log before the next tag. Name each cause that the log shows, and fix each one in the same tag.
- Verify Staging logs, `output_invariants`, alert and report completeness, and VPS resource health.

## 3. Failure

- A failed Preview or Staging train gets one forward-fix round through `init`. The round ends with the next free tag of `tools/release_tag.py`.
- After that round, revert the poison-pill PR on `main` and reopen its issue.

## 4. Preview teardown

At `close`, tear down the preview slot of the PR with `deploy_v2 --down`.

## 5. Production decision

- The `close` skill section 3 owns the thresholds and the question to the owner.
- The `deploy-evidence` skill owns the evidence chain and the production dispatch.
- Never dispatch production without the owner's live reply in the session (Root section `Production reservation`).
