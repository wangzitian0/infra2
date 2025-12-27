"""
CI/CD Pipeline tests.

Tests pipeline execution and deployment processes.

See README.md for SSOT documentation on pipeline configuration.
"""
import pytest


@pytest.mark.ops
async def test_github_workflows_exist():
    """Verify GitHub Actions workflows are present."""
    import pathlib
    workflow_path = pathlib.Path(__file__).parent.parent.parent.parent.parent / ".github" / "workflows"
    
    assert workflow_path.exists(), ".github/workflows directory should exist"
    
    # Check for core infra workflows
    workflows = list(workflow_path.glob("*.yml")) + list(workflow_path.glob("*.yaml"))
    assert len(workflows) > 0, "Should have at least one CI workflow"

