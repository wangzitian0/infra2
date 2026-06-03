"""Internal SigNoz to Feishu alert bridge."""

from __future__ import annotations

import base64
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from libs.alerting import (
    AlertingError,
    deliver_feishu_text,
    format_signoz_alert,
    redacted_url,
    validate_feishu_webhook_url,
)

HOST = "0.0.0.0"
PORT = int(os.getenv("PORT", "8080"))
MAX_BODY_BYTES = 512 * 1024


class AlertBridgeHandler(BaseHTTPRequestHandler):
    server_version = "Infra2FeishuAlertBridge/1.0"

    def do_GET(self) -> None:
        if self.path != "/health":
            self._json(404, {"status": "not_found"})
            return
        webhook_url = os.getenv("FEISHU_WEBHOOK_URL", "")
        try:
            validate_feishu_webhook_url(webhook_url)
        except AlertingError as exc:
            self._json(503, {"status": "degraded", "error": str(exc)})
            return
        self._json(200, {"status": "ok", "webhook": redacted_url(webhook_url)})

    def do_POST(self) -> None:
        if self.path != "/signoz/webhook":
            self._json(404, {"status": "not_found"})
            return
        if not self._authorized():
            self._json(401, {"status": "unauthorized"})
            return

        content_length = int(self.headers.get("Content-Length", "0") or "0")
        if content_length <= 0 or content_length > MAX_BODY_BYTES:
            self._json(413, {"status": "payload_too_large"})
            return

        try:
            payload = json.loads(self.rfile.read(content_length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._json(400, {"status": "invalid_json"})
            return
        if not isinstance(payload, dict):
            self._json(400, {"status": "invalid_payload"})
            return

        webhook_url = os.getenv("FEISHU_WEBHOOK_URL", "")
        try:
            text = format_signoz_alert(payload)
            response = deliver_feishu_text(webhook_url, text)
        except AlertingError as exc:
            self._json(502, {"status": "delivery_failed", "error": str(exc)})
            return

        self._json(202, {"status": "accepted", "feishu": response})

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"{self.address_string()} - {fmt % args}", flush=True)

    def _authorized(self) -> bool:
        username = os.getenv("BRIDGE_BASIC_AUTH_USERNAME", "")
        password = os.getenv("BRIDGE_BASIC_AUTH_PASSWORD", "")
        if not username and not password:
            return True
        header = self.headers.get("Authorization", "")
        expected = "Basic " + base64.b64encode(
            f"{username}:{password}".encode("utf-8")
        ).decode("ascii")
        return header == expected

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    server = ThreadingHTTPServer((HOST, PORT), AlertBridgeHandler)
    print(f"alert bridge listening on {HOST}:{PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
