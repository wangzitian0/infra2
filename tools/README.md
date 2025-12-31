# Infra2 CLI Tools

Standalone `invoke` namespaces loaded by `tools/loader.py`.

## Runner

- Use `invoke` inside an activated venv, or prefix with `uv run` when using uv.
- List all tasks: `invoke --list` (未激活虚拟环境时用 `uv run invoke --list`).

## Namespaces

| Namespace | Entry | Purpose |
|-----------|-------|---------|
| `env` | `tools/env_tool.py` | Remote env/secret SSOT operations |
| `local` | `tools/local_init.py` | Local CLI checks and bootstrap helpers |

## Common Conventions

- Pattern: `invoke <namespace>.<task>`
- `env` defaults: `--env=production`, `--service` optional (required for `list-all`)
- Write operations use `KEY=VALUE` (quote values with spaces)
- Output uses `libs.console` helpers; avoid raw `print` in new tasks.
- Omit `--service` for environment-level (`{project}/{env}`) values.

## env (remote secrets)

Remote-first secrets operations (1Password/Vault). No local `.env` sync.

```bash
# Read secret
invoke env.get KEY --project=platform --service=postgres

# Write secret
invoke env.set KEY=VALUE --project=platform --service=postgres

# List secrets (masked)
invoke env.list-all --project=platform --service=postgres

# Show init/env_vars from 1Password
invoke env.init-status
```

## local (local readiness + bootstrap)

- 输出统一使用 `libs.console`（状态行 + 命令块），不直接 `print`。

```bash
# Check CLI dependencies
invoke local.check

# Guide local setup (prints install instructions)
invoke local.init

# Show installed CLI versions
invoke local.version

# Validate init/env_vars in 1Password (no local .env)
invoke local.bootstrap

# Detect current bootstrap phase
invoke local.phase
```

## References

- [文档索引](../docs/README.md)
- [SSOT Index](../docs/ssot/README.md)
- [Project Portfolio](../docs/project/README.md)
- [AI 行为准则](../AGENTS.md)
