# infra2 Project Charter (init.md)

## 0. Dual Identity: SDK Canary Consumer and IaC Control Plane

`infra2-sdk` and `infra2` are two distinct repositories with orthogonal responsibilities:

- **`infra2-sdk`** is a standalone repository that owns pure data contracts, offline validators, and thin open-protocol adapters (`boto3`, `psycopg`, `httpx`, `opentelemetry`).
- **`infra2`** serves two architectural roles:
  1. **Host of temporary `infra2-sdk` version bridges during rollout and deployer of the Canary Todo application (`platform/30.todo`, source in `infra2-sdk` `canary/todo`).**
  2. **L1/L2 infrastructure implementation, L4 application deployer control plane, and multi-repository development workspace harness.**

**Consumption boundary:** `infra2` pins an immutable released wheel of `infra2-sdk` in `pyproject.toml` and `uv.lock` (with `sha256` extracted by `tools/sdk_requirement.py` for image and workflow installs, `#1115`). `infra2` never imports `repos/infra2-sdk` via `sys.path` or relative paths.

## 1. Core Architectural Principles

1. **Canary Proving Ground for `infra2-sdk`:** `infra2` exercises `infra2-sdk` contracts (`infra2_sdk.config`, `infra2_sdk.telemetry`, `infra2_sdk.service`, `infra2_sdk.storage`, `infra2_sdk.llm`, `infra2_sdk.delivery`) in control-plane tools (`tools/deploy_v2.py`, `tools/reconcile_iac_inputs.py`) and deploys the `canary/todo` app (`platform/30.todo/deploy.py`, source in `infra2-sdk` `canary/todo/app.py`).
2. **4-Layer Infrastructure Topology (`docs/ssot/core.md`):**
   - **L1 Bootstrap (`bootstrap/`):** Global trust anchor (Dokploy, 1Password Connect, HashiCorp Vault, IaC Runner).
   - **L2 Platform (`platform/`):** Shared per-environment services (PostgreSQL, Redis, Authentik SSO, RustFS S3, SigNoz, OpenPanel).
   - **L3 Data:** Stateful data storage and object buckets.
   - **L4 Application Deployers (`finance_report/`, `truealpha/`):** Compose templates, Vault Agent rendering, and Dokploy orchestration for autonomous applications.
3. **Remote-First Secrets and Manifest v2 Supply Chain (`docs/ssot/bootstrap.vars_and_secrets.md`):**
   - 1Password is the sole authority for static bootstrap secrets. Vault is the runtime secret store for L2/L4 services. Dokploy holds non-secret environment variables.
   - Do not store plaintext `.env` files in git. Application manifests (`EnvironmentManifest`) drive Vault policy and template generation through `common.security.backends`.
4. **Four-Axis Deployment Coordinate (`docs/ssot/core.environments.md`, `docs/ssot/ops.pipeline.md`):**
   - Every deployment binds four explicit coordinates: `--service`, `--type` (`staging`, `prod`, `preview`), `--version-ref`, and `--iac-ref` (`tools/deploy_v2.py`); `--domain` is an optional override, not a coordinate axis.
   - The stages, phases and change kinds are defined once in [`docs/ssot/ops.pipeline.md` section 2.0](docs/ssot/ops.pipeline.md#20-delivery-model-five-flows-five-stages-three-phases).
5. **Workspace Harness Without Ownership Merger (`docs/ssot/core.harness.md`, `harness/repos.yaml`):**
   - `repos/finance_report` and `repos/truealpha` are autonomous application checkouts (`contract: snapshot`).
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
