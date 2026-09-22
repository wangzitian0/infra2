# Workspace Coordination Preferences

These are harness defaults, not a replacement for repository-local contributor rules.

## Work Identity

1. Derive workspace identity from the actual checkout root:

   ```bash
   workspace_root="$(git rev-parse --show-toplevel)"
   workspace_name="$(basename "$workspace_root")"
   ```

2. Give each change one stable work key: an issue ID, project ID, or explicit
   standalone key. The physical form of that claim is the Root-level worktree rule (one worktree
   per issue, issue prefix in the directory name; see the workspace root `AGENTS.md`,
   铁则 6); this guide does not restate it.
3. Search open and recently closed issues, PRs, and branches before creating another
   owner for the same work key.
4. Allow parallel work only when both work keys and writable paths are disjoint. Keep
   one writer for shared manifests, lockfiles, migrations, registries, and authority
   documents -- disjoint worktrees (iron rule 6) stop two agents sharing a `.git/index`,
   but they do not stop two issues each legitimately touching the same migration
   chain; that is what this single-writer rule is for.
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

## Orchestrator Liveness

The orchestrator is the main conversation that starts subagents, watches PRs, releases
and CI, and merges. Background-job exits, `Monitor` lines and subagent completions reach
it only between tool calls, never during one. On 2026-09-16 it waited silently for more
than ten minutes on a PR because a shell loop grepped a merge gate's text for expected
outcomes and never matched "1 unresolved review thread(s)"
([truealpha#876](https://github.com/wangzitian0/truealpha/issues/876)).

1. No foreground call in the main conversation blocks longer than 4 minutes. Longer
   work runs in the background or under `Monitor`. Subagents are exempt: they finish
   their deliverable, including long verification, before ending their turn.
2. Keep one watch list and one clock. Every in-flight agent, PR, release and
   default-branch CI run is on the watch list. While the list is non-empty, a persistent
   `Monitor` runs the sweep; it prints a heartbeat after 4 quiet minutes.
3. Wait only on known waiting states. Judge gates by exit code and never grep gate
   text. Exit 1 alone is not "keep waiting": `tools/pr_merge_gate.py` returns 1 for
   pending checks, but also for red checks, unresolved threads, drafts and merged PRs.
4. Every wake-up sweeps everything, tells the user one line per change, and acts on
   every item that is not waiting before re-arming the watch.
5. Read agent progress without reading transcripts. The signal is tool-call activity,
   not output: on 2026-09-22 a native Agent's task output file sat at 152 bytes for 45
   minutes while the agent committed and pushed four branches, and the orchestrator
   stopped it as dead. So: the agent's worktree (branch head, dirty files, unpushed
   commits) or PR head is evidence; the output file's mtime and size are not evidence, for
   native Agents. Look in the sandbox the artifact lives in, not the main checkout.
6. Background scripts always append a verdict line, so a dead process is
   distinguishable from a finished one:

   ```bash
   cmd > "$log" 2>&1; echo "exit=$?" >> "$log"
   ```

### Stall Thresholds

| Item | Stalled when | Response |
|---|---|---|
| Agent | No tool call for ~2 min (owner's criterion, 2026-09-22). A tool call still in flight -- a `Monitor`, a foreground `wait`, a long test run -- counts as alive for its whole duration; the criterion is *no call started and none running*, not *no call returned*. `harness sweep` cannot see tool calls yet and stats the output files instead -- treat its agent STALL as a proxy, confirm in the worktree before acting | Look in the agent's worktree; `SendMessage` only if the worktree is also idle; tell the user if 10 more minutes pass |
| PR checks | Checks pending and none finished for 30 min | Inspect the run; re-run or report |
| Release log | No write for 25 min, or the process died without a verdict line | Read the log tail; report the last stage |
| Workflow run | Queued or running for more than 45 min | Inspect or cancel the run and report |
| Branch head | No CI run 10 min after its commit | Check triggers and path filters |

### Sweep And Watch

`python -m tools.harness sweep <watch.json>` (run from the infra2 root) prints one
state per item: `WAITING`, `DONE`, `ACTION`, `STALL` or `UNKNOWN`. Only `WAITING` is a
reason to keep waiting, and it is decided from an allow-list of GitHub facts. A PR
gate's exit code decides ready (0) and owner (2); its text is discarded, and a gate
command carrying `--merge`, `--request-review`, `--admin` or `--auto` (or an abbreviation
of one) is refused. With `--watch` the sweep prints only transitions and heartbeats and
exits as soon as any item leaves `WAITING`:

| Exit | Meaning |
|---|---|
| 0 | Nothing needs you (one-shot: all waiting or done; watch: all done) |
| 1 | An item needs action |
| 2 | An item finished while others still wait (watch only) |
| 3 | An item stalled or its process died without a verdict |
| 4 | A probe failed, a value was unrecognised, or the input was invalid |
| 5 | The watch budget (`--max-minutes`) ran out |

Keep the watch list at `<scratchpad>/sweep/watch.json` and arm it with an absolute path:

```bash
uv run python -m tools.harness sweep /abs/path/to/scratchpad/sweep/watch.json --watch
```

Item kinds and fields are listed in [`tools/README.md`](../../tools/README.md#harnesspy).

### Guard Hook (Owner-Wired)

`tools/orchestrator_guard_hook.py` enforces rules 1 and 2 in the main conversation. It
denies a foreground Bash call with a timeout above 4 minutes or a foreground wait loop
(`until`/`while` with `sleep`, `gh run watch`, `gh pr checks --watch`), and blocks the
turn's end once when `<scratchpad>/sweep/watch.json` lists items and no
`sweep ... --watch` process is running. Hook input carrying `agent_id` (a subagent) is
always allowed. The repository does not wire it: `.claude/settings.json` needs owner
review. To enable it, the owner adds:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          {
            "type": "command",
            "command": "python3 \"$CLAUDE_PROJECT_DIR/tools/orchestrator_guard_hook.py\""
          }
        ]
      }
    ],
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python3 \"$CLAUDE_PROJECT_DIR/tools/orchestrator_guard_hook.py\""
          }
        ]
      }
    ]
  }
}
```
