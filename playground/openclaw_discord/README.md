# OpenClaw Discord Deployment

This directory contains the Docker Compose configuration for deploying [OpenClaw](https://github.com/openclaw/openclaw) with Discord integration.

## Architecture

- **Service**: OpenClaw Gateway
- **Network**: Connected to Dokploy's shared Docker network (`dokploy-network`); container listens according to `OPENCLAW_GATEWAY_BIND` (default `lan`) on `${OPENCLAW_GATEWAY_PORT}` internally and is exposed externally via Traefik HTTP routing.
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
| `LLM_PROVIDER` | `coding` | Provider name in config |
| `LLM_BASE_URL` | `https://api.z.ai/api/coding/paas/v4` | API endpoint |
| `LLM_MODEL_ID` | `glm-5` | Model ID |
| `LLM_MODEL_NAME` | `GLM-5` | Model display name |
| `DISCORD_ENABLED` | `true` | Enable discord channel |
| `DISCORD_DM_ENABLED` | `true` | Enable Discord DM handling |
| `DISCORD_DM_POLICY` | `open` | DM policy (`open` / `pairing` / `disabled`) |
| `DISCORD_GROUP_POLICY` | `open` | Guild policy (`open` / `allowlist` / `disabled`) |
| `DISCORD_GROUP_REQUIRE_MENTION` | `false` | Require `@bot` mention in guild channels |
| `OPENCLAW_GATEWAY_BIND` | `lan` | Gateway bind mode |
| `OPENCLAW_GATEWAY_PORT` | `18789` | Internal gateway port |

## Deployment Guide (Dokploy)

1.  **Git Provider Deployment**:
    - Connect this repository to Dokploy.
    - Point "Compose Path" to `playground/openclaw_discord/compose.yaml`.
    - Set the Environment Variables in Dokploy UI.

2.  **Verify**:
    - Open `https://openclaw-discord.your-domain.com/?token=<YOUR_TOKEN>`.
    - Check logs for `[discord] discord channel starting`.

## Troubleshooting

### Discord Channel Not Starting

**Symptom**: No `[discord] starting` in logs

**Cause**: Missing or invalid `DISCORD_TOKEN`, or Message Content Intent not enabled in Discord Developer Portal.

**Solution**:
- Verify `DISCORD_TOKEN` in Dokploy.
- Ensure **Message Content Intent** is toggled ON in the [Discord Developer Portal](https://discord.com/developers/applications).

### Bot Online But No Reply

**Symptom**: Bot is logged in, but DM or guild messages get no response.

**Cause**:
- DM policy is `pairing` and sender is not approved yet.
- Guild `requireMention` is `true` and message does not mention bot.

**Solution**:
- For "reply to any DM", set `DISCORD_DM_POLICY=open`.
- For "reply in guild without mention", set `DISCORD_GROUP_REQUIRE_MENTION=false`.
