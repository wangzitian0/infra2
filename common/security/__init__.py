"""Infra2 security domain package.

Import the submodule that defines a name, for example
`from common.security.supply import apply_secret_supply`. The package itself
imports nothing, so `import common.security` and `common.security.registry`
need no infra2-sdk (#847; `common/tests/test_sdk_free_import_surface.py`).
"""
