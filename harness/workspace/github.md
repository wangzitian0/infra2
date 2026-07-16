# GitHub Delivery Preferences

These preferences apply to root workspace work unless a target repository defines a
stricter local rule.

## Branch And PR Lifecycle

1. Do not commit directly to the default branch. Use one branch and PR per coherent
   work key.
2. Keep the PR scope within declared writable paths and explain non-goals.
3. Include a checklist covering code, tests, documentation, compatibility, and rollout
   when applicable.
4. Never let an AI agent merge a PR. Merge authority remains human-owned.
5. Continue monitoring an open PR for late checks, review comments, and conflicting
   base changes until the human merge or close decision.

## Ready-To-Merge Evidence

Treat `mergeable` as ready and authorized now, not merely conflict-free:

- Required checks pass for the exact current head SHA; stale, skipped, cancelled, or
  superseded runs are not evidence.
- Required reviews cover the material diff, requested changes are addressed, and review
  threads are resolved with a reply that records the disposition.
- Base drift is assessed against affected paths and contracts. Rebase when local policy
  requires it or drift invalidates evidence, then rerun the affected proof.
- The PR is not draft, preserves a deployable default branch, and has a rollback path
  for operational changes.
- Staging or Production claims name the exact commit, tag, image ref, or digest actually
  exercised. A healthy older deployment is not proof for a newer change.

Before reporting a PR as ready, query fresh GitHub state and report the URL, branch, head
SHA, draft state, merge state, required checks, and unresolved review count.

## GitHub Tooling

- Prefer semantic GitHub integrations for issue and PR metadata.
- Use `gh` for Actions logs and GraphQL review-thread state when higher-level tooling
  cannot expose exact-head or resolution details.
- Keep mutations explicit: inspect first, then comment, resolve, label, push, or close
  only within the current work key.
