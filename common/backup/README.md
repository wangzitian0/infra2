# Infra2 Backup Domain Package (`common/backup`)

> **SSOT Domain**: Disaster recovery inventory verification, rehearsal specification, plan generation, and restore execution.

## Overview

`common/backup` governs data protection and disaster recovery across the `infra2` estate:
1. **Backup Verification**: Inspects local and off-host backup manifests (Google Drive via rclone crypt) to verify freshness, payload size, and integrity.
2. **Rehearsal Specification**: Defines `RehearsalSpecification` models that enforce sandboxed recovery targets (e.g., ephemeral test containers with dedicated ports) to prevent production database corruption.
3. **Automated Rehearsal Execution**: End-to-end restore rehearsal execution: pulls snapshots from cold storage, unencrypts archives, spins up ephemeral instances, restores tables, and runs smoke assertions.

## Module Map

| Module | Role | Key Exports |
|--------|------|-------------|
| `verification.py` | Backup inventory & manifest validation | `load_backup_inventory()`, `verify_backup_manifest()`, `build_backup_alert_payload()`, `BackupCheck`, `BackupEntry` |
| `rehearsal.py` | Restore rehearsal planning & sandboxed execution | `RehearsalSpecification`, `RestoreRehearsalPlan`, `create_rehearsal_plan()`, `execute_rehearsal()`, `assert_rehearsal_target()` |

## Usage Examples

### Loading the Estate Backup Inventory
```python
from common.backup import load_backup_inventory

inventory = load_backup_inventory()
for entry in inventory:
    print(f"Service: {entry.service_id}, Path: {entry.data_path}, Retention: {entry.retention_days}d")
```

### Building and Executing a Restore Rehearsal
```python
from pathlib import Path
from common.backup import (
    RehearsalSpecification,
    create_rehearsal_plan,
    execute_rehearsal,
    load_backup_inventory,
)

# Define a guarded rehearsal specification targeting a disposable container
entry = next(e for e in load_backup_inventory() if "pg" in e.method)
spec = RehearsalSpecification(
    entry=entry,
    artifact={"remote_uri": "local:/tmp/rehearsal.dump"},
    archive_path=Path("/tmp/rehearsal.dump"),
    target_container="test-postgres-rehearsal",
    pg_user="postgres",
    database="postgres",
    invariant_sql=("SELECT 1;",),
)

plan = create_rehearsal_plan(spec)
result = execute_rehearsal(plan)
assert result["status"] == "pass"
print(f"Verified {result['invariants_checked']} invariants for {result['service_id']}")
```

## Safety Invariants

- **Sandbox Immunity**: Rehearsals MUST NOT target production ports (e.g., 5432) or live volume mounts; `assert_rehearsal_target()` enforces strict ephemeral host separation.
- **Fail-Closed Verification**: A missing or stale manifest triggers an immediate P1 alert payload (`build_backup_alert_payload()`).
- **Guards & Tests**: Covered by `common/tests/test_backup_verification.py`.
