# Infra2 Backup Domain Package (`libs/backup`)

> **SSOT Domain**: Disaster recovery inventory verification, rehearsal specification, plan generation, and restore execution.

## Overview

`libs/backup` governs data protection and disaster recovery across the `infra2` estate:
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
from libs.backup import load_backup_inventory

inventory = load_backup_inventory()
for entry in inventory:
    print(f"Service: {entry.service_id}, Paths: {entry.paths}, Retention: {entry.retention}")
```

### Building and Executing a Restore Rehearsal
```python
from libs.backup import RehearsalSpecification, create_rehearsal_plan, execute_rehearsal

# Define a guarded rehearsal specification targeting a disposable container
spec = RehearsalSpecification(
    service_id="platform/postgres",
    database_name="postgres",
    target_container="test-postgres-rehearsal",
    target_port=15432,
)

plan = create_rehearsal_plan(spec, snapshot_date="latest")
result = execute_rehearsal(plan)
assert result.is_successful
print(f"Restored {result.tables_verified} tables in {result.elapsed_seconds:.2f}s")
```

## Safety Invariants

- **Sandbox Immunity**: Rehearsals MUST NOT target production ports (e.g., 5432) or live volume mounts; `assert_rehearsal_target()` enforces strict ephemeral host separation.
- **Fail-Closed Verification**: A missing or stale manifest triggers an immediate P1 alert payload (`build_backup_alert_payload()`).
- **Guards & Tests**: Covered by `libs/tests/test_backup_verification.py`.
