# Alerting Bridge

Internal bridge that receives SigNoz Alertmanager webhook payloads and sends
Feishu custom bot text messages.

## Why This Exists

SigNoz webhook channels send Alertmanager-style JSON. Feishu custom bot
webhooks require `msg_type=text` payloads, so the two endpoints are not directly
compatible. This service keeps the Feishu webhook secret in Vault and exposes
only an internal Docker-network endpoint to SigNoz.

## Deployment

```bash
uv run invoke env.set FEISHU_WEBHOOK_URL=https://open.feishu.cn/open-apis/bot/v2/hook/<token> --project=platform --env=production --service=alerting
uv run invoke vault.setup-tokens --project=platform --service=alerting
uv run invoke alerting.setup
uv run invoke alerting.status
```

The bridge is internal only:

```text
http://platform-alerting${ENV_SUFFIX}:8080/signoz/webhook
```

## SigNoz Channel

Create the SigNoz API key first if needed:

```bash
uv run invoke signoz.shared.create-api-key
```

Then create the notification channel:

```bash
uv run invoke alerting.create-signoz-channel
```

If `BRIDGE_BASIC_AUTH_USERNAME` and `BRIDGE_BASIC_AUTH_PASSWORD` are present in
Vault, `create-signoz-channel` automatically includes them in the SigNoz webhook
channel.

To inspect the payload without mutating SigNoz:

```bash
uv run invoke alerting.print-channel-payload
```

## Secrets

Vault path: `secret/platform/{env}/alerting`

| Key | Required | Purpose |
|---|---:|---|
| `FEISHU_WEBHOOK_URL` | yes | Feishu custom bot webhook URL |
| `BRIDGE_BASIC_AUTH_USERNAME` | no | Optional SigNoz webhook basic auth username |
| `BRIDGE_BASIC_AUTH_PASSWORD` | no | Optional SigNoz webhook basic auth password |

## Verification

```bash
uv run invoke alerting.status
uv run invoke alerting.test-feishu --message="Infra2 alert test"
```

`test-feishu` sends a synthetic SigNoz-style alert through the bridge and should
result in a Feishu group message.
