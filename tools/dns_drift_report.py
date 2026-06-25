#!/usr/bin/env python3
"""Daily DNS drift report — the first T3 reconciler (intent ↔ external reality).

Compares the DNS the tooling INTENDS (bootstrap/02.dns_and_cert's DEFAULT_RECORDS, normalized)
against what Cloudflare ACTUALLY serves, and posts a daily report to a dedicated Feishu/Lark
group. This is the only way to manage drift whose truth lives outside the repo: a record can be
hand-added/removed in the Cloudflare UI with no PR, so no internal test can catch it (see #280).

Properties (by design):
  * READ-ONLY on Cloudflare — GET only; the single side effect is the Lark message.
  * REPORT, not gate — it gates nothing (no deploy/PR depends on it). Finding DRIFT is reported,
    never an error; it posts daily even when in-sync (delivery self-proves the path, #425). Exit
    semantics, kept honest: no-op exit 0 only when NOT fully configured; a fully-configured run
    that can't read Cloudflare or post to Lark exits non-zero, so a BROKEN reconciler is visible
    (a silently-green broken reconciler would be exactly the lie this is meant to catch).
  * SINGLE-SOURCED intent — reuses DEFAULT_RECORDS + _normalize_record + the CF client from the
    DNS tooling itself, so it checks the same records the tooling manages, not a third copy.

Config (env, wired from GitHub secrets/vars in .github/workflows/dns-drift-report.yml):
  CF_API_TOKEN, CF_ZONE_ID | CF_ZONE_NAME   Cloudflare read access
  INTERNAL_DOMAIN                            e.g. zitian.party
  CF_RECORDS (optional)                      comma list override; else DEFAULT_RECORDS
  Delivery (one of):
    app bot  — DNS_DRIFT_FEISHU_{APP_ID,APP_SECRET,CHAT_ID}[, API_BASE]: send via the existing
               infra2 Feishu app bot to the 'infra2 reports' group it was added to (preferred).
    webhook  — DNS_DRIFT_FEISHU_WEBHOOK_URL: a custom-bot webhook.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from dataclasses import dataclass
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _dns_tasks():
    """Load bootstrap/02.dns_and_cert/tasks.py (dotted dir → importlib by path)."""
    spec = importlib.util.spec_from_file_location(
        "dns_cert_tasks", ROOT / "bootstrap/02.dns_and_cert/tasks.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@dataclass(frozen=True)
class DnsDrift:
    missing: list[str]  # intended (expected) but ABSENT in Cloudflare — real drift
    unmanaged: list[str]  # present in Cloudflare but NOT in intent — informational

    @property
    def in_sync(self) -> bool:
        return not self.missing


def compute_drift(expected: set[str], actual: set[str]) -> DnsDrift:
    """Pure diff. `missing` (intent not realized) is the signal; `unmanaged` is info —
    other mechanisms (platform routing, manual) legitimately create records, so an extra
    record is reported, not treated as a failure."""
    return DnsDrift(
        missing=sorted(expected - actual), unmanaged=sorted(actual - expected)
    )


def format_report(drift: DnsDrift, domain: str, n_expected: int, n_actual: int) -> str:
    lines = [
        f"📋 [Infra2] DNS drift report · {domain}",
        f"intent {n_expected} records · cloudflare {n_actual} A/CNAME records",
    ]
    if drift.missing:
        lines.append(
            f"🔴 MISSING in Cloudflare ({len(drift.missing)}) — intent NOT realized:"
        )
        lines += [f"  · {r}" for r in drift.missing]
    if drift.unmanaged:
        lines.append(
            f"ℹ️ unmanaged ({len(drift.unmanaged)}) — in Cloudflare, not in DNS intent:"
        )
        lines += [f"  · {r}" for r in drift.unmanaged]
    if drift.in_sync and not drift.unmanaged:
        lines.append("✅ in sync — every intended record exists; nothing unmanaged.")
    elif drift.in_sync:
        lines.append(
            "✅ every intended record exists (unmanaged ones are informational)."
        )
    return "\n".join(lines)


def _post_to_webhook(webhook: str, text: str) -> None:
    """Post to a Feishu/Lark custom-bot webhook."""
    from libs.alerting import validate_feishu_webhook_url

    # Validate the URL is an allowed Feishu/Lark host before posting (same SSRF guard the rest
    # of the codebase uses), so an injected/typo'd webhook can't be POSTed to.
    url = validate_feishu_webhook_url(webhook)
    resp = httpx.post(
        url, json={"msg_type": "text", "content": {"text": text}}, timeout=15.0
    )
    resp.raise_for_status()
    body = resp.json()
    # Lark custom bot returns {"code":0} or {"StatusCode":0} on success.
    if body.get("code") not in (0, None) or body.get("StatusCode") not in (0, None):
        raise RuntimeError(f"Lark webhook rejected the message: {body}")


def delivery_mode() -> str | None:
    """Which delivery is configured: 'app' (bot in a named group, by chat_id) preferred over
    'webhook'. App mode reuses the existing infra2 Feishu app bot, so the report lands in the
    'infra2 reports' group it was added to."""
    if (
        os.environ.get("DNS_DRIFT_FEISHU_CHAT_ID")
        and os.environ.get("DNS_DRIFT_FEISHU_APP_ID")
        and os.environ.get("DNS_DRIFT_FEISHU_APP_SECRET")
    ):
        return "app"
    if os.environ.get("DNS_DRIFT_FEISHU_WEBHOOK_URL", "").strip():
        return "webhook"
    return None


def deliver(text: str) -> None:
    """Send the report via whichever Feishu delivery is configured (app bot or webhook)."""
    mode = delivery_mode()
    if mode == "app":
        from libs.alerting import deliver_feishu_app_text

        deliver_feishu_app_text(
            app_id=os.environ["DNS_DRIFT_FEISHU_APP_ID"],
            app_secret=os.environ["DNS_DRIFT_FEISHU_APP_SECRET"],
            chat_id=os.environ["DNS_DRIFT_FEISHU_CHAT_ID"],
            text=text,
            api_base=os.environ.get(
                "DNS_DRIFT_FEISHU_API_BASE", "https://open.feishu.cn"
            ),
        )
    elif mode == "webhook":
        _post_to_webhook(os.environ["DNS_DRIFT_FEISHU_WEBHOOK_URL"].strip(), text)
    else:
        raise RuntimeError("no Feishu delivery configured (app creds or webhook)")


def _expected_records(dns) -> list[str]:
    domain = os.environ["INTERNAL_DOMAIN"]
    # CF_RECORDS may arrive quoted (op stores it as "cloud,op,..."); strip wrapping quotes
    # before splitting, else the first/last name keep a stray quote and falsely look MISSING.
    override = os.environ.get("CF_RECORDS", "").strip().strip('"').strip("'")
    names = (
        [r.strip() for r in override.split(",") if r.strip()]
        if override
        else list(dns.DEFAULT_RECORDS)
    )
    return [dns._normalize_record(name, domain) for name in names]


def _actual_records(dns) -> list[str]:
    # Context-manage the client like the DNS tooling does, so the connection isn't leaked.
    with dns._cloudflare_client(os.environ["CF_API_TOKEN"]) as client:
        zone = dns._resolve_zone_id(
            client, os.environ.get("CF_ZONE_ID"), os.environ.get("CF_ZONE_NAME")
        )
        if not zone:
            raise RuntimeError(
                "could not resolve Cloudflare zone (CF_ZONE_ID / CF_ZONE_NAME)"
            )
        # one page (per_page=100) is enough for this zone; add pagination if it grows.
        result = dns._cf_request(
            client, "GET", f"/zones/{zone}/dns_records", params={"per_page": 100}
        )
    return [r["name"] for r in (result or []) if r.get("type") in ("A", "CNAME")]


def main() -> int:
    have_zone = bool(os.environ.get("CF_ZONE_ID") or os.environ.get("CF_ZONE_NAME"))
    # No-op cleanly ONLY when not fully configured (so the job is never a noisy red before the
    # secrets exist). Validate everything the live path needs up front — including the values
    # read later (INTERNAL_DOMAIN, the zone, a delivery target) — so a partial config can't
    # raise mid-run.
    if not (
        os.environ.get("CF_API_TOKEN")
        and delivery_mode()
        and os.environ.get("INTERNAL_DOMAIN")
        and have_zone
    ):
        print(
            "dns-drift-report: not fully configured (need CF_API_TOKEN, INTERNAL_DOMAIN, "
            "CF_ZONE_ID|CF_ZONE_NAME, and a delivery target — either "
            "DNS_DRIFT_FEISHU_{APP_ID,APP_SECRET,CHAT_ID} or DNS_DRIFT_FEISHU_WEBHOOK_URL) — "
            "skipping.",
            file=sys.stderr,
        )
        return 0  # not-yet-configured is not a failure
    # Fully configured below: any Cloudflare/Feishu error propagates (non-zero) on purpose, so a
    # broken-but-configured reconciler is a visible red rather than a silent green.

    dns = _dns_tasks()
    expected = set(_expected_records(dns))
    actual = set(_actual_records(dns))
    drift = compute_drift(expected, actual)
    report = format_report(
        drift, os.environ.get("INTERNAL_DOMAIN", "?"), len(expected), len(actual)
    )
    print(report)
    deliver(report)  # daily delivery self-proves the path, drift or not
    return 0


if __name__ == "__main__":
    sys.exit(main())
