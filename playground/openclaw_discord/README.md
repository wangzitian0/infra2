# OpenClaw Discord Deployment

This directory contains the Docker Compose configuration for deploying [OpenClaw](https://github.com/openclaw/openclaw) with Discord integration.

## Architecture

- **Service**: OpenClaw Gateway
- **Version**: Pinned to stable release `ghcr.io/openclaw/openclaw:2026.3.13-1`
- **Network**: Connected to Dokploy's shared Docker network (`dokploy-network`); container listens on `0.0.0.0:${OPENCLAW_GATEWAY_PORT}` internally and is exposed externally via Traefik HTTP routing.
- **Storage**: Named Docker volume `openclaw-discord-data` for persistence across redeploys.
- **Configuration**: **100% environment-driven** — no hardcoded secrets.

## Prerequisites

1.  **Dokploy**: Deployment target.
2.  **Discord Bot**: Configured in Discord Developer Portal.
    - Enable **Message Content Intent** in the Bot tab.
    - Enable **Server Members Intent**.
3.  **LLM Provider**: OpenAI-compatible endpoint (e.g., Zhipu AI, OpenAI).

## Configuration (Environment Variables)

All configuration is driven by environment variables. The `init-config` container generates `openclaw.json` at startup.

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
| `LLM_PROVIDER` | `coding` | Provider name for primary models |
| `LLM_BASE_URL` | `https://api.z.ai/api/coding/paas/v4` | Primary API endpoint |
| `LLM_MODEL_ID` | `glm-5` | Primary Model ID |
| `LLM_MODEL_NAME` | `GLM-5` | Primary Model display name |
| `TIANCLAW_MODEL` | `openai-codex/gpt-5.4` | Model override for the `tianclaw` agent |
| `OPENCLAW_GATEWAY_CHANNEL_HEALTH_CHECK_MINUTES` | `0` | Framework channel health-monitor interval; `0` disables health-monitor restarts entirely |
| `OPENCLAW_AGENTS_MAX_CONCURRENT` | `4` | Maximum concurrent tasks per agent |
| `DISCORD_NATIVE_SKILL_COMMANDS` | `false` | Publish per-skill Discord slash commands in addition to core native commands |
| `DISCORD_ENABLED` | `true` | Enable discord channel |
| `DISCORD_DM_ENABLED` | `true` | Enable Discord DM handling |
| `DISCORD_DM_POLICY` | `open` | DM policy (`open` / `pairing` / `disabled`) |
| `DISCORD_GROUP_POLICY` | `open` | Guild policy (`open` / `allowlist` / `disabled`) |
| `DISCORD_GROUP_REQUIRE_MENTION` | `false` | Require `@bot` mention in guild channels |
| `OPENCLAW_GATEWAY_BIND` | `lan` | Gateway bind mode |
| `OPENCLAW_GATEWAY_PORT` | `18789` | Internal gateway port |
| `OPENCLAW_LOG_LEVEL` | `info` | Framework-recognized log level override for file + console logs |
| `OPENCLAW_DIAGNOSTICS` | _empty_ | Optional targeted diagnostics flags, passed through to OpenClaw unchanged |

`LOG_LEVEL` is kept as a backward-compatible fallback in `compose.yaml`, but OpenClaw itself reads `OPENCLAW_LOG_LEVEL`.

## Config Persistence

The `init-config` container generates `openclaw.json` on first deploy. On subsequent redeploys it preserves the existing file, but still applies selected declarative overrides from environment variables, including `OPENCLAW_GATEWAY_BIND`, `TIANCLAW_MODEL`, `OPENCLAW_GATEWAY_CHANNEL_HEALTH_CHECK_MINUTES`, `DISCORD_NATIVE_SKILL_COMMANDS`, and `OPENCLAW_AGENTS_MAX_CONCURRENT`. This lets operator-managed settings survive while still enforcing critical network, model routing, and runtime behavior.

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
- Set `TIANCLAW_MODEL=openai-codex/gpt-5.4` in Dokploy.
- Redeploy the compose application.
- If the agent entry was manually removed from `openclaw.json`, restore it or reset the config file before redeploying.
