# Workspace Harness

`harness/` describes how this checkout coordinates repositories without merging
their ownership models.

## Scope

- `infra2` is the infrastructure implementation and deployment control plane.
- `infra2-sdk` owns versioned contracts and explicitly invoked protocol adapters;
  importing it remains free of network and global-provider side effects.
- `oh-my-code-agent` owns coding-agent observation, profiles and isolated runtimes,
  independently of application deployment and task scheduling.
- Workspace guides record shared preferences for GitHub, coordination, and software
  design.
- `finance_report` and `truealpha` are autonomous application repositories. Their
  local agent, architecture, CI, and release rules remain authoritative.

The machine-readable inventory is [`repos.yaml`](./repos.yaml). Validate it with:

```bash
uv run python -m tools.harness check
uv run python -m tools.harness status --fetch
```

The command is read-only. It validates inventory structure and referenced authority
files; it does not update submodules, copy policy, publish packages, or deploy services.
`status` reports parent pins, checkout/remote heads, ahead/behind, dirty paths, and
release identity. Its optional `--fetch` refreshes origin metadata only; it never checks
out or pulls a repository. Add `--require-current` when drift should make CI/scripting fail.

## Repository entry points

The shared structure is an explicit owner, local proof command, and independent release.
Each repository keeps the file layout that expresses its own architecture. These are
navigation pointers to local authority, not a second copy of its policy.

| Repository | Architecture and work entry | Local verification entry |
|---|---|---|
| infra2 | `docs/ssot/` and `docs/project/Infra-019.*` | `uv run python -m tools.harness check`; relevant SSOT's The Proof |
| infra2-sdk | `README.md`, `pyproject.toml`, module contracts | `uv run --extra dev pytest`; `uv run ruff check .` |
| OMCA | `init.md`, `docs/README.md`, `docs/project/roadmap.md` | `make build`, `make test`, `make standalone`; host/version-specific qualification |
| Finance Report | `vision.md`, `common/<pkg>/contract.py`, `common/meta/data/MANIFEST.yaml` | `tools/preflight.py --tier=static` through its documented Python environment |
| TrueAlpha | `vision.md`, `init.md`, issue-owned acceptance checks | `tools/prepush.sh` with Bash 4+; scoped runtime/module tests |

Finance Report's `docs/ssot/` is retired. Its package roadmap owns new ACs; EPIC files
contain shrink-only residue. TrueAlpha's issue and capability model remains independent.
SDK pins may differ between consumers: compatibility, not equal version strings, is
the requirement. Neither App imports SDK source from the workspace checkout.

## Reuse when starting an app

| Need | Owner and reuse path |
|---|---|
| Environment tiers, runtime identity, dependency validation, HTTP/S3/Postgres/OTel adapters | Install a released `infra2-sdk` artifact; start from its standalone readiness example |
| Business dependency names, required tiers, domain rules, routes and models | Define them in the app's own contracts and tests |
| Infrastructure provisioning, secret delivery, deployment and promotion | Use infra2's published deployment interfaces and onboarding paths |
| Coding-agent profiles, Skills/MCP selection and isolated launch | Use the independently built OMCA CLI; follow its host-version qualification procedure |

Reuse a released contract where it removes duplicate semantics. Keep compatibility
wrappers when an app's public API differs, and prove their behavior before replacement.
Do not move app policy into a common package merely to make directory trees look alike.
The SDK's installed-wheel smoke is an executable consumer example; OMCA's safe automatic
qualification does not replace its human TUI/restart/model proof. Its installed
executable embeds reviewed Knowledge Packs and ontology; installation proof must
run outside the build checkout, including after that checkout is removed.

Reviewed snapshot and acceptance evidence live in
[Infra-019 TODOWRITE](../docs/project/Infra-019.TODOWRITE.md).

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
