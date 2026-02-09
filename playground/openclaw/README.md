# OpenClaw Deployment

This directory contains the Docker Compose configuration for deploying [OpenClaw](https://github.com/openclaw/openclaw).

## Architecture

- **Service**: OpenClaw Gateway
- **Network**: Uses Host networking (via Dokploy/Traefik routing), internally binds to `0.0.0.0` (LAN) on port `18789`.
- **Storage**: Persists configuration and workspace data in `./data` (mounted to `/root/.openclaw`).
- **Configuration**:
    - Secrets via Environment Variables (`.env`).
    - Complex settings (Trusted Proxies) via `data/openclaw.json`.

## Prerequisites

1.  **Dokploy**: Deployment target.
2.  **Feishu App**: Configured in Feishu Developer Console.
3.  **LLM Provider**: Zhipu AI (GLM) via OpenAI-compatible endpoint.

## Configuration Files

### 1. `compose.yml`
Defines the service.
**Critical Settings**:
- `command`: Force binds to `lan` interface to allow Docker networking.
- `environment`: Passes secrets from Dokploy.

### 2. `data/openclaw.json`
Handles configuration that cannot be set via environment variables, specifically **Trusted Proxies**.
This is required for OpenClaw to accept connections forwarded by Traefik/Dokploy.

```json
{
  "gateway": {
    "trustedProxies": ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.1/8"]
  }
}
```

### 3. `.env` (Secrets)
Create this file based on `.env.example`.
**Required Variables**:
- `OPENCLAW_GATEWAY_TOKEN`: Secure token for accessing the dashboard.
- `OPENAI_API_KEY`: API Key for the LLM.
- `FEISHU_*`: Feishu App credentials.

## Deployment Guide (Dokploy)

1.  **Git Provider Deployment**:
    - Connect this repository to Dokploy.
    - Point "Compose Path" to `infra2/playground/openclaw/compose.yml`.
    - Set the Environment Variables in Dokploy UI (copy from your local `.env`).

2.  **Verify**:
    - Open `https://openclaw.your-domain.com/?token=<YOUR_TOKEN>`.
    - If prompted "Pairing Required", check container logs for `REQUEST_ID` and approve via CLI:
      ```bash
      docker exec -it <container_id> node dist/index.js nodes approve <REQUEST_ID>
      ```
