"""Centralized deploy_v2 backends.

The ``tools/`` package keeps only the CLI front door (``deploy_v2.py``); its
unified backend lives here as pure importable libraries:

- :mod:`common.deploy.deployer` — the Invoke-task platform/app Deployer.
- :mod:`common.deploy.sync_pipeline` — unified SyncPipeline v2 orchestrator.
- :mod:`common.deploy.steps.app_rollout` — fixed-compose app rollout steps.
- :mod:`common.deploy.steps.preview_slot` — dynamic preview slot lifecycle steps.
- :mod:`common.deploy.failure_snapshot` — Dokploy deploy failure diagnosis snapshot.
"""

from __future__ import annotations
