# OpenClaw Discord Switch Runbook

This runbook describes how to fully switch models and display names for the live OpenClaw Discord deployment.

It exists because editing only `README.md`, `.env.example`, or Dokploy env vars is not enough after the first deploy.

## Source Of Truth Order

OpenClaw Discord has multiple configuration layers. When they disagree, the live behavior follows this order:

1. Persisted live config: `/home/node/.openclaw/openclaw.json`
2. Cron-level overrides: `cron.payload.model`
3. Agent-level defaults: `agents.list[].model`
4. Provider model catalogs: `models.providers.*.models[]`
5. Repo defaults: `compose.yaml`, `.env.example`, `README.md`

Discord-visible names have an extra split:

1. OpenClaw account label: `channels.discord.accounts.<accountId>.name`
2. Discord platform username / guild nickname

Changing only one layer leads to drift.

## What Must Be Updated For A Model Switch

For an existing deployment, update all applicable layers:

1. Provider model catalog
   - Add the target model under `models.providers.<provider>.models[]` if it does not already exist.
   - Example: adding `glm-5.1`, `glm-5v-turbo`, or `gemini-3.1-pro-preview`.

2. Agent default model
   - Update `agents.list[].model`.
   - Example format: `coding/glm-5.1`, `openai-codex/gpt-5.5`, `github-copilot/gemini-3.1-pro-preview`.

3. Cron overrides
   - Inspect all cron jobs for explicit `payload.model`.
   - Any cron with a hardcoded model will ignore the agent default.

4. Verification
   - Read back `config.get`.
   - Read back `cron.list`.
   - Check recent logs for `model_not_found`, `requested model is not supported`, or auth failures.

## What Must Be Updated For A Name Switch

There are two separate name layers:

1. OpenClaw-side label
   - Update `channels.discord.accounts.<accountId>.name`.
   - This affects OpenClaw control UI and some internal labeling.

2. Discord-visible name
   - Update the real bot username in Discord Developer Portal or via Discord API.
   - If server-specific display is needed, also update the guild nickname.

Do not assume changing OpenClaw account labels will update Discord's visible bot name.

## Why Dokploy Env Changes Often Do Nothing

`repo/playground/openclaw_discord/compose.yaml` generates `openclaw.json` only when the file does not already exist. On later redeploys it preserves the file and applies only selected declarative overrides:

```sh
if [ -f /data/openclaw.json ]; then
  # Patches network/runtime guardrails while preserving dashboard-managed config.
  echo "openclaw.json already exists, patched declarative overrides"
  exit 0
fi
```

That means:

- First deploy: env vars generate the initial config
- Later deploys: the Docker volume remains the live source of truth, except for declared guardrail overrides
- Dashboard edits persist across redeploys
- Compatibility patches may normalize old schema fields, such as Discord `streaming` scalar values or stale explicit web-search provider ids, without removing account tokens or cron jobs

If you want env vars to become authoritative again, you must delete the persisted config and regenerate it, or explicitly edit the live config.

## Where Discord Tokens Usually Come From

Do not assume Discord tokens are still sourced from 1Password or Vault.

For this deployment shape, there are three common token locations:

1. Dokploy env on the first deploy
   - Example: `DISCORD_TOKEN`
   - Used only to seed the initial config

2. Persisted live config
   - `channels.discord.token`
   - `channels.discord.accounts.<accountId>.token`
   - This is often the real runtime source after the dashboard UI adds or edits accounts

3. External secret stores
   - 1Password / Vault may still hold original bootstrap secrets
   - But they are not automatically the runtime source of truth

If OP/Vault does not show a Discord token, that does not mean the running gateway lacks one. The token may already be stored in the persisted OpenClaw config and redacted by the control API.

## Recommended Live Switch Procedure

1. Read the live config from the running gateway.
2. Confirm the current provider catalogs, agent models, Discord account labels, and cron overrides.
3. Upsert any new model IDs into the relevant provider catalog.
4. Update agent models.
5. Update cron `payload.model` where present.
6. Update OpenClaw-side Discord account labels.
7. Update Discord platform usernames / guild nicknames separately.
8. Re-read config and cron state.
9. Tail logs after the next manual message or scheduled run.

## Verification Checklist

- `config.get` shows the expected agent models
- `config.get` shows the expected account labels
- `cron.list` shows the expected `payload.model` overrides
- `health` probes succeed for the relevant Discord accounts
- Recent logs do not show:
  - `400 The requested model is not supported`
  - `400 Unknown Model`
  - `model_not_found`
  - provider auth refresh failures
- Discord-visible names were separately validated in the client

## Current Runtime Notes

The live deployment investigated in April 2026 had all of the following at different times:

- Repo defaults pointing to `glm-5`
- Agent configs pointing elsewhere in persisted `openclaw.json`
- Cron jobs hardcoding their own models
- OpenClaw account labels differing from Discord probe usernames

That drift is expected unless all layers above are switched together.
