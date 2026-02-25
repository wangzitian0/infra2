# OpenClaw Deployment

This directory contains the Docker Compose configuration for deploying [OpenClaw](https://github.com/openclaw/openclaw).

## Architecture

- **Service**: OpenClaw Gateway
- **Network**: Connected to Dokploy's shared Docker network (`dokploy-network`); container listens on `0.0.0.0:18789` internally and is exposed externally via Traefik HTTP routing.
- **Storage**: Named Docker volume `openclaw-data` for persistence across redeploys.
- **Configuration**: **100% environment-driven** — no hardcoded secrets, supports multiple bot instances.

## Prerequisites

1.  **Dokploy**: Deployment target.
2.  **Feishu App**: Configured in Feishu Developer Console.
3.  **LLM Provider**: OpenAI-compatible endpoint (e.g., Zhipu AI, OpenAI).

## Configuration (Environment Variables)

All configuration is driven by environment variables. The `init-config` container generates `openclaw.json` at startup.

### Required Variables

| Variable | Description | Example |
|----------|-------------|--------|
| `OPENCLAW_GATEWAY_TOKEN` | Dashboard access token | `your_secure_token` |
| `GOG_KEYRING_PASSWORD` | Keyring password | `random_string` |
| `LLM_API_KEY` | LLM provider API key | `sk-xxx` |
| `FEISHU_APP_ID` | Feishu App ID | `cli_xxx` |
| `FEISHU_APP_SECRET` | Feishu App Secret | `xxx` |
| `FEISHU_VERIFICATION_TOKEN` | Feishu verification token | `xxx` |
| `FEISHU_ENCRYPT_KEY` | Feishu encrypt key | `xxx` |

### Optional Variables (with defaults)

| Variable | Default | Description |
|----------|---------|-------------|
| `LLM_PROVIDER` | `coding` | Provider name in config |
| `LLM_BASE_URL` | `https://api.z.ai/api/coding/paas/v4` | API endpoint |
| `LLM_MODEL_ID` | `glm-5` | Model ID |
| `LLM_MODEL_NAME` | `GLM-5` | Model display name |
| `FEISHU_ENABLED` | `true` | Enable feishu channel |
| `FEISHU_DOMAIN` | `feishu` | Feishu domain |

### Multiple Bot Instances

To run multiple bots, create separate Dokploy deployments with different environment variables:

```bash
# Bot 1 (production)
FEISHU_APP_ID=cli_xxx1
FEISHU_APP_SECRET=secret1
...

# Bot 2 (staging)
FEISHU_APP_ID=cli_xxx2
FEISHU_APP_SECRET=secret2
...
```

Each deployment will have its own volume and configuration.

## Deployment Guide (Dokploy)

1.  **Git Provider Deployment**:
    - Connect this repository to Dokploy.
    - Point "Compose Path" to `playground/openclaw/compose.yaml`.
    - Set the Environment Variables in Dokploy UI.

2.  **Verify**:
    - Open `https://openclaw.your-domain.com/?token=<YOUR_TOKEN>`.
    - Check logs for `[feishu] starting feishu[main] (mode: websocket)`.

## How It Works

1. **init-config** container runs first:
   - Reads environment variables
   - Generates `/data/openclaw.json` using jq
   - Sets permissions

2. **openclaw** container starts:
   - Reads generated config from volume
   - Starts gateway with feishu channel

## Troubleshooting

### Health Check Failures

**Symptom**: Container shows as `unhealthy` in `docker ps`

**Cause**: OpenClaw runs as a WebSocket service (`ws://`), not HTTP.

**Solution**: Health check uses process monitoring:
```yaml
healthcheck:
  test: [ "CMD-SHELL", "pgrep -f 'openclaw-gateway' > /dev/null || exit 1" ]
```

### Feishu Channel Not Starting

**Symptom**: No `[feishu] starting feishu[main]` in logs

**Cause**: Missing feishu environment variables

**Solution**: Verify all `FEISHU_*` variables are set in Dokploy:
- `FEISHU_APP_ID`
- `FEISHU_APP_SECRET`
- `FEISHU_VERIFICATION_TOKEN`
- `FEISHU_ENCRYPT_KEY`

### Config Not Generated

**Symptom**: `init-config` fails or config is empty

**Cause**: Missing required environment variables

**Solution**: Check init-config logs:
```bash
docker logs <init-config-container>
```

### Image Version Breakage

**Symptom**: Feishu worked before but stopped after container recreation

**Cause**: Using `:latest` tag pulled a newer version with incompatible plugin SDK

**Solution**: Image is pinned to v2026.2.6 digest. Do NOT change to `:latest` without testing.

### Data Lost After Redeploy

**Symptom**: Config disappears after Dokploy redeploy

**Solution**: Using named Docker volume `openclaw-data` which persists. If still seeing issues, check volume exists:
```bash
docker volume ls | grep openclaw-data
```