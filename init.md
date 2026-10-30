# infra2 Project Charter (init.md)

## 0. Dual Identity: SDK Canary Consumer and IaC Control Plane

`infra2-sdk` and `infra2` are two distinct repositories with orthogonal responsibilities:

- **`infra2-sdk`** is a standalone repository that owns pure data contracts, offline validators, and thin open-protocol adapters (`boto3`, `psycopg`, `httpx`, `opentelemetry`).
- **`infra2`** serves two architectural roles:
  1. **First consumer and Canary proving ground for released `infra2-sdk` SemVer packages.**
  2. **L1/L2 infrastructure implementation, L4 application deployer control plane, and multi-repository development workspace harness.**

**Consumption boundary:** `infra2` pins an immutable released wheel of `infra2-sdk` in `pyproject.toml` and `uv.lock` (with `sha256` extracted by `tools/sdk_requirement.py` for image and workflow installs, `#1115`). `infra2` never imports `repos/infra2-sdk` via `sys.path` or relative paths.

## 1. Core Architectural Principles

1. **Canary Proving Ground for `infra2-sdk`:** `infra2` exercises `infra2-sdk` contracts (`capacity`, `ci`, `delivery`, `deploy`, `fastapi`, `manifests`, `observability`, `refs`, `routing`, `rules`, `runtime`, `scaffold`, `secrets`, `transport`) in control-plane tools and platform services (`tools/deploy_v2.py`, `tools/deploy_v2_canary.py`, `tools/reconcile_iac_inputs.py`, `tools/sdk_usage_census.py`, `platform/30.todo/app.py`) before or alongside autonomous application adoption.
2. **4-Layer Infrastructure Topology (`docs/ssot/core.md`):**
   - **L1 Bootstrap (`bootstrap/`):** Global trust anchor (Dokploy, 1Password Connect, HashiCorp Vault, IaC Runner).
   - **L2 Platform (`platform/`):** Shared per-environment services (PostgreSQL, Redis, Authentik SSO, RustFS S3, SigNoz, OpenPanel).
   - **L3 Data:** Stateful data storage and object buckets.
   - **L4 Application Deployers (`finance_report/`, `truealpha/`):** Compose templates, Vault Agent rendering, and Dokploy orchestration for autonomous applications.
3. **Remote-First Secrets and Manifest v2 Supply Chain (`docs/ssot/bootstrap.vars_and_secrets.md`):**
   - 1Password is the sole authority for static bootstrap secrets. Vault is the runtime secret store for L2/L4 services. Dokploy holds non-secret environment variables.
   - Do not store plaintext `.env` files in git. Application manifests (`EnvironmentManifest`) drive Vault policy and template generation through `infra2_sdk.secrets`.
4. **Four-Axis Deployment Coordinate and Three-Stage Lifecycle (`docs/ssot/core.environments.md`, `docs/ssot/ops.pipeline.md`):**
   - Every deployment binds four explicit coordinates: `--service`, `--type` (`staging`, `prod`, `canary`, `preview`), `--iac-ref`, and `--domain` (`tools/deploy_v2.py`).
   - Changes progress through three isolated stages:
     - **Stage 1:** Code merges to `main` with zero container impact.
     - **Stage 2:** Release tag `vX.Y.Z` auto-deploys to `staging` and runs Canary verification.
     - **Stage 3:** Owner-authorized production deploy or promote runs, followed by physical Touch Reality verification.
5. **Workspace Harness Without Ownership Merger (`docs/ssot/core.harness.md`, `harness/repos.yaml`):**
   - `repos/finance_report` and `repos/truealpha` are autonomous application checkouts (`contract: snapshot`).
   - `oh-my-code-agent/` is independent workspace tooling (`contract: snapshot`).
   - `repos/infra2-sdk` is a coordinated contract submodule (`contract: pinned`).
   - Each repository owns its own `init.md` (charter), `AGENTS.md` (projected Repo-tier operational rules), CI gates, and release identity.

## 2. Document Map (Where Truth Lives)

| Document | Role |
|---|---|
| `init.md` | Architectural charter, SDK Canary relationship, and layer boundaries |
| `AGENTS.md` | Always-loaded operational decision invariants and merge gate rules (projected from `dev_env`) |
| `docs/ssot/MANIFEST.yaml` & `docs/ssot/*.md` | Authoritative technical specifications and proof anchors |
| `harness/repos.yaml` & `harness/README.md` | Multi-repository workspace inventory and autonomy contracts |
| `README.md` | Quick start and operator CLI reference |
