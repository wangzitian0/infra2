# OpenClaw Discord Deployment

This directory contains the Docker Compose configuration for deploying [OpenClaw](https://github.com/openclaw/openclaw) with Discord integration.

## Architecture

- **Service**: OpenClaw Gateway
- **Version**: Pinned to stable release `ghcr.io/openclaw/openclaw:2026.5.3`
- **Network**: Connected to Dokploy's shared Docker network (`dokploy-network`); container listens on `0.0.0.0:${OPENCLAW_GATEWAY_PORT}` internally and is exposed externally via Traefik HTTP routing.
- **Storage**: Named Docker volume `openclaw-discord-data` for persistence across redeploys.
- **Configuration**: First deploy is environment-driven, but the live source of truth becomes the persisted `/home/node/.openclaw/openclaw.json`.
- **Plugins**: The gateway bootstraps the official `@openclaw/discord` plugin before startup when the persisted volume does not already have it installed.
- **State Sync**: Optional `tianclaw-git-sync` sidecar can snapshot the sanitized `openclaw-discord-data` volume to `git@github.com:wangzitian-ai/tianclaw.git`.

## Prerequisites

1.  **Dokploy**: Deployment target.
2.  **Discord Bot**: Configured in Discord Developer Portal.
    - Enable **Message Content Intent** in the Bot tab.
    - Enable **Server Members Intent**.
3.  **LLM Provider**: OpenAI-compatible endpoint (e.g., Zhipu AI, OpenAI).

## Configuration (Environment Variables)

All configuration is driven by environment variables. The `init-config` container generates `openclaw.json` at startup.

Important: after the first successful deploy, OpenClaw reads the persisted config file from the Docker volume, not this repo's defaults. If you change a model, Discord account, or cron job in the dashboard UI, those changes survive redeploys until the persisted file is deleted or explicitly updated.

### Required Variables

| Variable | Description | Example |
|----------|-------------|--------|
| `OPENCLAW_GATEWAY_TOKEN` | Dashboard access token | `your_secure_token` |
| `GOG_KEYRING_PASSWORD` | Keyring password | `random_string` |
| `LLM_API_KEY` | LLM provider API key | `sk-xxx` |
| `DISCORD_TOKEN` | Discord Bot Token | `OTI...` |

### Optional Variables (with defaults)

| Variable | Default | Description |
|----------|---------|-------------|
| `LLM_PROVIDER` | `github-copilot` | Provider name for primary models |
| `LLM_BASE_URL` | `https://api.githubcopilot.com` | Primary API endpoint |
| `LLM_MODEL_ID` | `claude-sonnet-4.6` | Primary Model ID |
| `LLM_MODEL_NAME` | `Claude Sonnet 4.6` | Primary Model display name |
| `TIANCLAW_MODEL` | `openai-codex/gpt-5.5` | Model override for the `tianclaw` agent |
| `OPENCLAW_GATEWAY_CHANNEL_HEALTH_CHECK_MINUTES` | `0` | Framework channel health-monitor interval; `0` disables health-monitor restarts entirely |
| `OPENCLAW_AGENTS_MAX_CONCURRENT` | `4` | Maximum concurrent tasks per agent |
| `DISCORD_NATIVE_SKILL_COMMANDS` | `false` | Publish per-skill Discord slash commands in addition to core native commands |
| `OPENCLAW_SKIP_ONBOARDING` | `true` | Skip interactive onboarding in automated Docker deployments |
| `DISCORD_ENABLED` | `true` | Enable discord channel |
| `DISCORD_DM_ENABLED` | `true` | Enable Discord DM handling |
| `DISCORD_DM_POLICY` | `open` | DM policy (`open` / `pairing` / `disabled`) |
| `DISCORD_GROUP_POLICY` | `open` | Guild policy (`open` / `allowlist` / `disabled`) |
| `DISCORD_GROUP_REQUIRE_MENTION` | `false` | Require `@bot` mention in guild channels |
| `OPENCLAW_GATEWAY_BIND` | `lan` | Gateway bind mode |
| `OPENCLAW_GATEWAY_PORT` | `18789` | Internal gateway port |
| `OPENCLAW_LOG_LEVEL` | `info` | Framework-recognized log level override for file + console logs |
| `OPENCLAW_DIAGNOSTICS` | _empty_ | Optional targeted diagnostics flags, passed through to OpenClaw unchanged |
| `TIANCLAW_GIT_SYNC_ENABLED` | `false` | Enable periodic sanitized volume snapshots to the TianClaw private repo |
| `TIANCLAW_GIT_REPO` | `git@github.com:wangzitian-ai/tianclaw.git` | Git repository for OpenClaw volume snapshots |
| `TIANCLAW_GIT_BRANCH` | `main` | Branch used by the sync sidecar |
| `TIANCLAW_GIT_SYNC_INTERVAL_SECONDS` | `300` | Sync loop interval |
| `TIANCLAW_GIT_SSH_KEY_PATH` | `/root/.ssh/id_ed25519` | Host private-key path mounted read-only by the sync sidecar |
| `TIANCLAW_GIT_SSH_KNOWN_HOSTS_PATH` | `/root/.ssh/known_hosts` | Host known-hosts path mounted read-only by the sync sidecar |
| `TIANCLAW_GIT_SSH_PRIVATE_KEY` | _empty_ | Optional private-key contents used instead of `TIANCLAW_GIT_SSH_KEY_PATH` |
| `TIANCLAW_GIT_SSH_KNOWN_HOSTS` | _empty_ | Optional known-hosts contents used instead of `TIANCLAW_GIT_SSH_KNOWN_HOSTS_PATH` |

`LOG_LEVEL` is kept as a backward-compatible fallback in `compose.yaml`, but OpenClaw itself reads `OPENCLAW_LOG_LEVEL`.

## Config Persistence

The `init-config` container generates `openclaw.json` on first deploy. On subsequent redeploys it preserves the existing file, but still applies selected declarative overrides from environment variables, including `OPENCLAW_GATEWAY_BIND`, `TIANCLAW_MODEL`, `OPENCLAW_GATEWAY_CHANNEL_HEALTH_CHECK_MINUTES`, `DISCORD_NATIVE_SKILL_COMMANDS`, and `OPENCLAW_AGENTS_MAX_CONCURRENT`. This lets operator-managed settings survive while still enforcing critical network, model routing, and runtime behavior.

For OpenClaw `2026.5.3`, the same patch step also normalizes legacy Discord streaming fields to the object form required by the current schema and removes an explicit legacy `tools.web.search.provider=brave` value while preserving the existing search API key. This lets OpenClaw use provider auto-detection instead of failing startup on a stale provider registration. This is a compatibility migration for persisted configs, not a config reset.

OpenClaw `2026.5.x` loads Discord as an installable plugin. The `openclaw` service command installs the official `@openclaw/discord` plugin only when it is missing, then starts the gateway. This keeps fresh containers and fresh volumes from booting without the Discord channel.

When enabled, `tianclaw-git-sync` mounts the same Docker volume read-only at `/openclaw`, mounts the configured SSH key material read-only, clones the private repo into an ephemeral container path, rsyncs the sanitized volume root, commits changes, and pushes the configured branch. It intentionally excludes `.git`, `.ssh`, `credentials`, `identity`, `keyrings`, `npm/node_modules`, virtualenvs, `openclaw.json*`, `.env*`, key files, token-like paths, and SQLite WAL/SHM files.

This means there are multiple configuration layers:

1. Repo defaults (`compose.yaml`, `.env.example`, this README)
2. Dokploy environment variables
3. Persisted live config (`/home/node/.openclaw/openclaw.json`)
4. Runtime overrides such as `cron.payload.model`
5. Discord-side profile state (bot username / guild nickname)

For model or nickname migrations, always verify the live layers instead of trusting repo defaults.

To **reset** the config to environment variable defaults, manually delete the file and redeploy:
```bash
docker exec <container> rm /home/node/.openclaw/openclaw.json
# Then redeploy via Dokploy
```

## Deployment Guide (Dokploy)

1.  **Git Provider Deployment**:
    - Connect this repository to Dokploy.
    - Point "Compose Path" to `./playground/openclaw_discord/compose.yaml`.
    - Set the Environment Variables in Dokploy UI.

2.  **Verify**:
    - Open `https://openclaw-discord.your-domain.com/?token=<YOUR_TOKEN>`.
    - Check logs for `[discord] discord channel starting`.
    - Run `docker inspect --format '{{json .State.Health}}' <container>` and confirm the health check stays `healthy`.
    - If you are changing models or names on an existing deployment, follow [SWITCHING.md](./SWITCHING.md) instead of only editing env vars.

## Branch Deploy Practice

When validating changes in Dokploy, prefer a **Git branch deploy** over switching the application to `raw` compose:

1. Push the candidate changes to a dedicated branch, for example `fix/openclaw-discord-stability`.
2. Keep the Dokploy application `sourceType=github`.
3. Temporarily point the Dokploy application branch at the test branch and redeploy.
4. Verify:
   - deployment log shows the expected branch commit
   - `init-config` exits `0`
   - gateway logs show `listening on ws://0.0.0.0:${OPENCLAW_GATEWAY_PORT}`
   - Discord accounts log in cleanly
   - the public URL returns `HTTP 200`
5. After verification, either merge the branch or point Dokploy back to `main`.

Avoid the `raw compose` fallback for this service unless you are also prepared to clean up Dokploy metadata. In practice, the Git deployment path is more reliable because Dokploy continues to clone the repository into its expected working directory and keeps `.env` handling, `composePath`, and deployment logs aligned.

One more operational detail: if the persisted `openclaw.json` was ever generated with `gateway.bind=auto`, redeploys can silently fall back to loopback-only listening and the public route will return `502` even though the container is `healthy`. Keep `OPENCLAW_GATEWAY_BIND=lan` in Dokploy and let `init-config` patch `.gateway.bind` on every redeploy.

## Runtime Policy

This deployment intentionally applies two startup overrides during `init-config`:

- `gateway.channelHealthCheckMinutes=0`
- `channels.discord.commands.nativeSkills=false`
- legacy `channels.discord.*.streaming` scalar values are normalized to `{ mode: "..." }`

The first disables OpenClaw's framework health-monitor, which was batch-restarting all Discord accounts on the same 5-minute tick. The second keeps core slash commands but drops per-skill command fan-out, reducing startup-time Discord API traffic.

## Troubleshooting

### Discord Channel Not Starting

**Symptom**: No `[discord] starting` in logs

**Cause**: Missing or invalid `DISCORD_TOKEN`, or Message Content Intent not enabled in Discord Developer Portal.

**Solution**:
- Verify `DISCORD_TOKEN` in Dokploy.
- Ensure **Message Content Intent** is toggled ON in the [Discord Developer Portal](https://discord.com/developers/applications).

### Bots Reconnect In Bursts Or Spam Discord Startup Calls

**Symptom**: Multiple Discord accounts restart together every few minutes, followed by `deploy-rest:put:error`, `/gateway/bot` fetch failures, or bot identity fetch failures.

**Cause**: OpenClaw's framework health-monitor checks all Discord accounts on the same interval. If several accounts are stuck in `connected=false`, one monitor tick can restart them all together, creating a startup-time Discord API burst.

**Solution**:
- Keep `OPENCLAW_GATEWAY_CHANNEL_HEALTH_CHECK_MINUTES=0` so the framework does not batch-restart Discord accounts.
- Keep `DISCORD_NATIVE_SKILL_COMMANDS=false` to reduce startup slash-command volume.
- Raise `OPENCLAW_LOG_LEVEL=debug` temporarily when investigating reconnect loops.
- Set `OPENCLAW_DIAGNOSTICS` only for targeted troubleshooting; it increases log volume.

### Model Change Did Not Take Effect

**Symptom**: Dashboard, cron runs, or logs still show an old model after changing Dokploy env vars.

**Cause**: The persisted `openclaw.json` still contains old `agents.*.model`, provider catalogs, or cron-level `payload.model` overrides.

**Solution**:
- Inspect the live config, not just this repo.
- Update the provider model catalog if you introduce a new model ID.
- Update agent defaults and any cron jobs with explicit `payload.model`.
- See the full migration checklist in [SWITCHING.md](./SWITCHING.md).

### Discord Name Did Not Change

**Symptom**: OpenClaw account names changed, but Discord still shows the old bot name.

**Cause**: OpenClaw account labels and Discord platform usernames/nicknames are separate layers.

**Solution**:
- Update `channels.discord.accounts.*.name` for OpenClaw-side labels.
- Update the real Discord bot username or guild nickname separately in Discord or via Discord API.
- Verify both layers after the switch.

### Bot Online But No Reply

**Symptom**: Bot is logged in, but DM or guild messages get no response.

**Cause**:
- DM policy is `pairing` and sender is not approved yet.
- Guild `requireMention` is `true` and message does not mention bot.

**Solution**:
- For "reply to any DM", set `DISCORD_DM_POLICY=open`.
- For "reply in guild without mention", set `DISCORD_GROUP_REQUIRE_MENTION=false`.

### Background Tasks/Reasoning Stability

**Symptom**: Complex tasks fail or use the wrong model.

**Cause**: All tasks default to the primary model, which might be too simple or have expired credits.

**Solution**:
- Point the primary `LLM_*` variables at a more robust model if the current provider is underpowered.
- If a dedicated reasoning model is needed later, add it to the compose template and document the schema change here before relying on it in Dokploy.

### Agent Model Override Not Applied

**Symptom**: `tianclaw` still uses an old model after redeploy.

**Cause**: The running config was created before `TIANCLAW_MODEL` was introduced, or the override was not set in Dokploy.

**Solution**:
- Set `TIANCLAW_MODEL=openai-codex/gpt-5.5` in Dokploy.
- Redeploy the compose application.
- If the agent entry was manually removed from `openclaw.json`, restore it or reset the config file before redeploying.
