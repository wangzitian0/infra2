# Workspace Coordination Preferences

These are harness defaults, not a replacement for repository-local contributor rules.

## Work Identity

1. Derive workspace identity from the actual checkout root:

   ```bash
   workspace_root="$(git rev-parse --show-toplevel)"
   workspace_name="$(basename "$workspace_root")"
   ```

2. Give each change one stable work key: an issue ID, project ID, or explicit
   standalone key.
3. Search open and recently closed issues, PRs, and branches before creating another
   owner for the same work key.
4. Allow parallel work only when both work keys and writable paths are disjoint. Keep
   one writer for shared manifests, lockfiles, migrations, registries, and authority
   documents.
5. Collapse duplicate work immediately and cross-link the surviving line of work.

## Work Order

1. State a verifiable outcome and explicit non-goals.
2. Read the target repository's local authority before editing.
3. Run the repository-local preflight; do not invent a workspace command that bypasses
   local gates.
4. Separate contract changes from repository-specific instances. Version shared
   contracts before consumers adopt them.
5. Record evidence against the exact source revision and artifact under test.
6. Hand off unresolved decisions and external blockers without overstating completion.

## Repository Autonomy

- A submodule pointer is a tested workspace snapshot, not policy ownership or a live
  release identity.
- Never edit an autonomous App merely to make it conform to a harness preference.
- When working inside an App, its local `AGENTS.md`, architecture, commands, and release
  process override this guide.
- Cross-repository changes use separate PRs and independently reviewable commits.
