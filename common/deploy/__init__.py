"""Centralized deploy_v2 backends.

The ``tools/`` package keeps only the CLI front doors (``deploy_v2.py`` /
``deploy_v2_canary.py``); their backends live here as pure importable libraries:

- :mod:`common.deploy.deployer` — the Invoke-task platform/app Deployer.
- :mod:`common.deploy.preview`  — the multi-alias preview lifecycle (``up`` / ``down``).
- :mod:`common.deploy.promote`  — the fixed-compose staging/prod promote backend.
- :mod:`common.deploy.failure_snapshot` — Dokploy deploy failure diagnosis snapshot.
"""

from __future__ import annotations
