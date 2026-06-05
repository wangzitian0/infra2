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
uv run invoke env.set ALERT_DELIVERY_MODE=feishu_app --project=platform --env=production --service=alerting --credential-type=root_vars
uv run invoke env.set FEISHU_APP_ID=cli_xxx --project=platform --env=production --service=alerting --credential-type=root_vars
uv run invoke env.set FEISHU_APP_SECRET=<secret> --project=platform --env=production --service=alerting --credential-type=root_vars
uv run invoke env.set FEISHU_CHAT_ID=<chat_id> --project=platform --env=production --service=alerting --credential-type=root_vars
uv run invoke vault.setup-tokens --project=platform --service=alerting
uv run invoke alerting.setup
uv run invoke alerting.status
```

1Password root vars are the long-lived source of truth. During
`alerting.pre-compose`, the deployer mirrors those fields into
`secret/platform/{env}/alerting` so the vault-agent can render runtime secrets.

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

## SigNoz Rule Automation

`platform/12.alerting` owns shared SigNoz alert automation only. Application
names are parameters; do not add app-specific tasks here.

Inspect the rule payload without mutating SigNoz:

```bash
uv run python -m invoke alerting.shared.print-log-error-rule-payload \
  --alert-name=ExampleBackendErrorLogs \
  --service-name=example-backend
```

Create or verify the internal channel and a reusable OTEL log error rule:

```bash
uv run python -m invoke alerting.shared.ensure-log-error-rule \
  --alert-name=ExampleBackendErrorLogs \
  --service-name=example-backend
```

## Secrets

Long-lived source: 1Password item `platform/{env}/alerting` (`root_vars`).
Runtime mirror: Vault path `secret/platform/{env}/alerting`.

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
uv run python -m invoke alerting.shared.ensure-log-error-rule \
  --alert-name=ExampleBackendErrorLogs \
  --service-name=example-backend \
  --dry-run
```

`test-feishu` sends a synthetic SigNoz-style alert through the bridge and should
result in a Feishu group message.

## Out-of-band Watchdog

The bridge remains internal only. Whole-host and bridge-down detection is handled
by `.github/workflows/out-of-band-watchdog.yml`, which runs every 30 minutes from
GitHub Actions and sends Feishu directly when infra2 or the bridge cannot be
trusted.

The bridge waits up to 300 seconds for `/secrets/.env` at startup, but it does
not require the vault-agent sidecar to stay Docker-healthy after the file is
rendered. This keeps alert delivery available even when vault-agent's own
stale-secret health check needs separate remediation.

Required repository secrets:

- `INFRA2_OUT_OF_BAND_ALERT_DELIVERY_MODE`: `feishu_webhook` or `feishu_app`
- `INFRA2_WATCHDOG_SSH_HOST`
- `INFRA2_WATCHDOG_SSH_USER`
- `INFRA2_WATCHDOG_SSH_PRIVATE_KEY`

For `feishu_webhook` mode:

- `INFRA2_OUT_OF_BAND_FEISHU_WEBHOOK_URL`

For `feishu_app` mode:

- `INFRA2_OUT_OF_BAND_FEISHU_APP_ID`
- `INFRA2_OUT_OF_BAND_FEISHU_APP_SECRET`
- `INFRA2_OUT_OF_BAND_FEISHU_CHAT_ID`
- `INFRA2_OUT_OF_BAND_FEISHU_API_BASE`: optional, defaults to `https://open.feishu.cn`

Optional repository variables:

- `INFRA2_WATCHDOG_HTTP_TARGETS`: newline-separated `name|url|status_csv`
- `INFRA2_WATCHDOG_SSH_TARGETS`: newline-separated `name|command|expected_text`
- `INFRA2_WATCHDOG_SSH_PORT`: defaults to `22`

Default checks cover the public Dokploy entrypoint, SSH reachability, Docker
daemon reachability, and the `platform-alerting` in-container `/health` endpoint
via SSH.
IaC Runner, MinIO, Postgres, Redis, and application dependency health remain
service-level signals handled in-band through SigNoz and this bridge.

## Infra Service Probes

`infra-probe-runner` runs beside the bridge and checks core infra dependencies
from inside the Dokploy network. Failures are converted into SigNoz-compatible
payloads and posted to the bridge.

Default probe coverage:

- Dokploy public entrypoint
- Vault health endpoint
- MinIO live endpoint
- Authentik health endpoint
- SigNoz frontend/query path
- Alert bridge `/health`
- platform Postgres TCP readiness
- platform Redis TCP readiness
- ClickHouse `/ping`

Probe spec format:

```text
name|kind|target|expected|severity|timeout_seconds
```

Dry-run:

```bash
INFRA_PROBE_DRY_RUN=1 uv run python tools/infra_probe_runner.py --once --json
```
