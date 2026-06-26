"""Centralized deploy_v2 backends.

The ``tools/`` package keeps only the CLI front doors (``deploy_v2.py`` /
``deploy_v2_canary.py``); their backends live here as pure importable libraries:

- :mod:`libs.deploy.deployer` — the Invoke-task platform/app Deployer.
- :mod:`libs.deploy.preview`  — the multi-alias preview lifecycle (``up`` / ``down``).
- :mod:`libs.deploy.promote`  — the fixed-compose staging/prod promote backend.
"""
