---
name: deploy-evidence
description: Infra2 Platform Deploy Evidence & Reality Guard. Automates Touch Reality verification (Docker container digest, Ledger JSON, Cloudflare Watchdog) before any service-touching issue can be closed.
---

# Infra2 Platform Deploy Evidence & Reality Guard

> **Core Axiom (AGENTS.md Section 5)**:
> *"Closed means deployed, real, and physically verified. Code merge is not deployment. Staging deploy is tag-driven soak. Production deploy requires explicit owner authorization and Touch Reality physical proof."*

## 1. When to Activate This Skill
- Before marking any Infra2 platform or service-touching issue as `Closed` or `Complete`.
- After merging a PR that touches `platform/`, `bootstrap/`, or deployment configuration.
- When verifying staging soak (10min) or production promote reality.

## 2. Four-Phase Verification Protocol

### Phase 1: Staging Soak Observability
Confirm release tag triggered staging reconcile and passed soak:
```bash
# Verify reconcile run for tag
gh run list --workflow reconcile-iac-inputs.yml --branch vX.Y.Z --limit 1
# Check soak status and health (must be >= 10m without crashloop)
```

### Phase 2: Prod Gatekeeper Checkpoint
If owner has not provided explicit prod disposition, format the 3-stage report and ask:
```text
[PROD GATEKEEPER CHECKPOINT]
- Stage 1 (Merge): Release tag vX.Y.Z = commit SHA <sha>
- Stage 2 (Staging Soak): Run URL <url>, Soak Duration <duration>, Health: PASS
- Stage 3 (Prod Baseline): Image <digest>, Watchdog: OK

Authorize production deployment of vX.Y.Z? (Reply "deploy" to authorize, or "hold" to keep unpromoted)
```

### Phase 3: Prod Promote Dispatch
A "deploy" covers only the tag it names. Immediately before dispatch, get a live reply from the owner in this session; an earlier or asynchronous answer is not enough. Then trigger the official promote:
```bash
gh workflow run reconcile-iac-inputs.yml -f promote_prod=true -f after=vX.Y.Z
gh run watch <run-id>
```

### Phase 4: Touch Reality Physical Probes (Read-Only via Existing Tools)
Never declare success based only on GitHub green! Verify the physical system using existing tools:

#### 1. Availability Ledger & Watchdog Outages Verification
Verify VPS availability ledger and Cloudflare Watchdog blackbox outages (dry-run mode without Lark side-effects):
```bash
INFRA2_STABILITY_REPORT_DRY_RUN=1 python -m tools.stability_report
```

#### 2. Physical Image, Container & Route Health Probes
Verify container health, network reachability, and alerting bridge status using the probe runner (dry-run one-shot probe):
```bash
INFRA_PROBE_DRY_RUN=1 python -m tools.infra_probe_runner --once
```

#### 3. Canary Todo 4-Pillar Physical Verification
Verify Canary Todo runtime across all four observability pillars (HTTP route, SigNoz logs, ClickHouse traces, and availability ledger):
```bash
# Verify staging environment
python -m tools.canary_verify --env staging

# Verify production environment
python -m tools.canary_verify --env prod
```

## 3. Red Lines (Instant Rejection)
- [REJECT] Merge commit on main without tag or promote: **DO NOT CLOSE ISSUE**.
- [REJECT] GitHub Action green but VPS container digest unchanged: **REJECT EVIDENCE (GREEN-WHILE-STALE)**.
- [REJECT] Ledger JSON missing or not cycling: **REJECT EVIDENCE**.
- [REJECT] Watchdog reporting outages: **REJECT EVIDENCE**.
