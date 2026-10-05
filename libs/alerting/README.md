# libs/alerting

Domain package for SigNoz alert processing, card rendering, and Feishu message delivery.

## Architecture

This package decomposes the monolithic alerting module into four focused modules:

- `types.py`:
  - Contains domain data models (`PagerItem`, `PagerMessage`, `BasicAuth`).
  - Contains alerting exception types (`AlertingError`, `InvalidWebhookUrl`, `InvalidFeishuAppConfig`, `FeishuDeliveryError`).
  - Contains level mappings, layout constants, and redaction helpers.

- `card.py`:
  - Parses SigNoz and Alertmanager payloads into `PagerMessage` structures.
  - Renders interactive Feishu card payloads and plain-text fallbacks.
  - Implements adaptive layout budget shrinking plans.

- `signoz.py`:
  - Builds SigNoz notification channel payloads.
  - Builds SigNoz log-based and metric-based alert rule definitions.
  - Provides channel and rule discovery helpers.

- `delivery.py`:
  - Validates Feishu webhook URLs and OpenAPI base URLs.
  - Checks TCP reachability to Feishu endpoints.
  - Delivers cards and text messages to Feishu webhooks and internal app bots.
  - Handles daily report delivery routing.

- `__init__.py`:
  - Exports the unified package facade for complete backward compatibility.
