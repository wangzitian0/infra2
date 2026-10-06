"""``SyncAction.FAILED`` is a literal so the module imports without infra2-sdk (#1016).

The literal must still equal the SDK's ``DeployState.FAILED`` value: a deploy result
that reports ``failed`` is read by consumers that compare it to the SDK enum.
"""

from __future__ import annotations

from infra2_sdk.deploy import DeployState

from libs.deploy.sync_pipeline import SyncAction


def test_failed_literal_matches_the_sdk_deploy_state() -> None:
    assert SyncAction.FAILED == DeployState.FAILED.value
