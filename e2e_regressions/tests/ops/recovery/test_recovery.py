"""
Disaster recovery tests.

Tests backup, restore, and disaster recovery procedures.

See README.md for SSOT documentation on recovery procedures.
"""
import pytest


@pytest.mark.ops
async def test_recovery_storage_policy():
    """Verify presence of 'local-path-retain' storage class for persistence."""
    # This is a structural test checking the terraform definition and SSOT
    import pathlib
    storage_tf = pathlib.Path(__file__).parent.parent.parent.parent.parent / "1.bootstrap" / "4.storage.tf"
    
    assert storage_tf.exists(), "Storage configuration should exist"
    content = storage_tf.read_text()
    assert "local-path-retain" in content, "Should have a 'retain' storage class for recovery"
    assert "reclaim_policy         = \"Retain\"" in content, "Storage policy must be 'Retain' for recovery safety"


@pytest.mark.ops
async def test_recovery_docs_exist():
    """Verify disaster recovery procedures are documented."""
    import pathlib
    recovery_doc = pathlib.Path(__file__).parent.parent.parent.parent.parent / "docs" / "ssot" / "ops.recovery.md"
    
    assert recovery_doc.exists(), "Disaster recovery documentation should exist"
    content = recovery_doc.read_text()
    assert "Restore" in content or "Recovery" in content, "Documentation should contain recovery steps"

