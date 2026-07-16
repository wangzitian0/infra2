# Workspace Harness

`harness/` describes how this checkout coordinates repositories without merging
their ownership models.

## Scope

- `infra2` is the infrastructure implementation and deployment control plane.
- `infra2-sdk` is the versioned, side-effect-free cross-repository contract.
- `oh-my-code-agent` is the independently versioned workspace tooling repository for
  managing TUI integrations over time.
- Workspace guides record shared preferences for GitHub, coordination, and software
  design.
- `finance_report` and `truealpha` are autonomous application repositories. Their
  local agent, architecture, CI, and release rules remain authoritative.

The machine-readable inventory is [`repos.yaml`](./repos.yaml). Validate it with:

```bash
uv run python -m tools.harness check
```

The command is read-only. It validates inventory structure and referenced authority
files; it does not update submodules, copy policy, publish packages, or deploy services.

## Workspace Guides

| Guide | Purpose |
|---|---|
| [coordination.md](./workspace/coordination.md) | Work identity, ownership, preflight, and evidence handoff |
| [github.md](./workspace/github.md) | Branch, PR, review, and exact-head delivery preferences |
| [software-design.md](./workspace/software-design.md) | Dependency, contract, compatibility, and test preferences |

These guides are defaults for root workspace work. Inside a nested repository, its local
`AGENTS.md`, architecture documents, and contributor guides take precedence. Adoption by
an autonomous App is an App decision, not a harness synchronization task.

## Ownership Boundary

The harness owns coordination metadata, not cross-repository source coupling. Production
identities remain independent: infra2 release tag, infra2-sdk SemVer, and application
image ref or digest. Workspace tooling uses its own release or pinned commit. See
[Harness Control Plane SSOT](../docs/ssot/core.harness.md).
