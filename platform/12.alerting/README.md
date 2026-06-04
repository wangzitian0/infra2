# Alerting Bridge

Internal bridge that receives SigNoz Alertmanager webhook payloads and sends
Feishu alert messages.

## Why This Exists

SigNoz webhook channels send Alertmanager-style JSON. Feishu has two supported
delivery modes:

- `feishu_webhook`: Feishu custom bot webhook.
- `feishu_app`: Feishu Open Platform app bot via `/open-apis/im/v1/messages`.

This service keeps Feishu secrets in Vault and exposes only an internal
Docker-network endpoint to SigNoz.

## Deployment

```bash
uv run invoke env.set FEISHU_WEBHOOK_URL=https://open.feishu.cn/open-apis/bot/v2/hook/<token> --project=platform --env=production --service=alerting
uv run invoke vault.setup-tokens --project=platform --service=alerting
uv run invoke alerting.setup
uv run invoke alerting.status
```

For Feishu Open Platform app bot mode:

```bash
uv run invoke env.set ALERT_DELIVERY_MODE=feishu_app --project=platform --env=production --service=alerting
uv run invoke env.set FEISHU_APP_ID=cli_xxx --project=platform --env=production --service=alerting
uv run invoke env.set FEISHU_APP_SECRET=<secret> --project=platform --env=production --service=alerting
uv run invoke env.set FEISHU_CHAT_ID=<chat_id> --project=platform --env=production --service=alerting
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
| `ALERT_DELIVERY_MODE` | no | `feishu_webhook` (default) or `feishu_app` |
| `FEISHU_WEBHOOK_URL` | webhook mode | Feishu custom bot webhook URL |
| `FEISHU_APP_ID` | app mode | Feishu Open Platform app ID |
| `FEISHU_APP_SECRET` | app mode | Feishu Open Platform app secret |
| `FEISHU_CHAT_ID` | app mode | Target chat ID for app bot messages |
| `FEISHU_API_BASE` | no | Defaults to `https://open.feishu.cn` |
| `BRIDGE_BASIC_AUTH_USERNAME` | no | Optional SigNoz webhook basic auth username |
| `BRIDGE_BASIC_AUTH_PASSWORD` | no | Optional SigNoz webhook basic auth password |

## Verification

```bash
uv run invoke alerting.status
uv run invoke alerting.test-feishu --message="Infra2 alert test"
```

`test-feishu` sends a synthetic SigNoz-style alert through the bridge and should
result in a Feishu group message.
