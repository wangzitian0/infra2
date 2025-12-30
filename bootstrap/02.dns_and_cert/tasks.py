"""Cloudflare DNS + certificate automation for bootstrap domains."""
from __future__ import annotations

import os
import socket
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Iterable

import httpx
from invoke import task

from libs.common import get_env
from libs.console import header, success, error, warning, info, env_vars
from libs.env import OpSecrets


BASE_URL = "https://api.cloudflare.com/client/v4"
DEFAULT_RECORDS = ("cloud", "op", "vault", "sso", "home")
DEFAULT_TTL = 1
CLOUDFLARE_ITEM = "bootstrap/cloudflare"
RECORDS_KEY = "CF_RECORDS"
DEFAULT_COOLDOWN_SECONDS = 60


def _parse_bool(value: str | bool | None, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.lower()
        if lowered in ("1", "true", "yes", "on"):
            return True
        if lowered in ("0", "false", "no", "off"):
            return False
        warning(f"Invalid boolean value {value!r}; using default={default}")
    return default


def _split_records(records: str | None) -> list[str]:
    if not records:
        return list(DEFAULT_RECORDS)
    return [r.strip() for r in records.split(",") if r.strip()]


def _split_records_or_empty(records: str | None) -> list[str]:
    if not records:
        return []
    return [r.strip() for r in records.split(",") if r.strip()]


def _normalize_record(name: str, internal_domain: str) -> str:
    if name == "@":
        return internal_domain
    if name == internal_domain or name.endswith(f".{internal_domain}"):
        return name
    return f"{name}.{internal_domain}"


def _normalize_record_list(records: list[str], internal_domain: str | None = None) -> list[str] | None:
    if not internal_domain:
        env = get_env()
        internal_domain = env.get("INTERNAL_DOMAIN")
    if not internal_domain:
        error("Missing INTERNAL_DOMAIN", "Set in init/env_vars")
        return None

    normalized: list[str] = []
    for name in records:
        if not name or any(ch.isspace() for ch in name):
            warning(f"Skipping invalid record name: {name!r}")
            continue
        normalized.append(_normalize_record(name, internal_domain))

    if not normalized:
        error("No valid records to process")
        return None
    return normalized


def _load_cloudflare_secrets() -> dict[str, str]:
    secrets = OpSecrets(item=CLOUDFLARE_ITEM)
    result: dict[str, str] = {}
    for key in ("CF_API_TOKEN", "CF_ZONE_ID", "CF_ZONE_NAME"):
        env_val = os.getenv(key) or os.getenv(key.replace("CF_", "CLOUDFLARE_"))
        if env_val:
            result[key] = env_val
            continue
        secret_val = secrets.get(key) or secrets.get(key.replace("CF_", "CLOUDFLARE_"))
        if secret_val:
            result[key] = secret_val
    return result


def _load_record_list(records: str | None) -> list[str]:
    if records:
        return _split_records(records)

    env_val = os.getenv(RECORDS_KEY) or os.getenv("CLOUDFLARE_RECORDS") or os.getenv("DNS_RECORDS")
    if env_val:
        return _split_records(env_val)

    secrets = OpSecrets(item=CLOUDFLARE_ITEM)
    for key in (RECORDS_KEY, "CLOUDFLARE_RECORDS", "DNS_RECORDS"):
        val = secrets.get(key)
        if val:
            return _split_records(val)

    return list(DEFAULT_RECORDS)


def _cloudflare_client(token: str) -> httpx.Client:
    return httpx.Client(
        base_url=BASE_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        timeout=15.0,
    )


def _cf_request(client: httpx.Client, method: str, path: str, **kwargs):
    try:
        resp = client.request(method, path, **kwargs)
    except httpx.RequestError as exc:
        error("Cloudflare API request failed", str(exc))
        return None

    try:
        data = resp.json()
    except ValueError:
        error("Cloudflare API invalid JSON", resp.text[:200])
        return None

    if resp.status_code >= 400 or not data.get("success"):
        err_msg = str(data.get("errors") or data.get("messages") or resp.text[:200])
        error(f"Cloudflare API error ({resp.status_code})", err_msg)
        return None
    return data.get("result")


def _resolve_zone_id(client: httpx.Client, zone_id: str | None, zone_name: str | None) -> str | None:
    if zone_id:
        return zone_id
    if not zone_name:
        error("Missing CF_ZONE_ID", "Provide CF_ZONE_ID or CF_ZONE_NAME in 1Password/bootstrap/cloudflare")
        return None
    result = _cf_request(client, "GET", "/zones", params={"name": zone_name})
    if not result:
        error("Cloudflare zone lookup failed", zone_name)
        return None
    if isinstance(result, list):
        if not result:
            error("Cloudflare zone lookup empty result", zone_name)
            return None
        return result[0].get("id")
    return result.get("id")


def _ensure_record(client: httpx.Client, zone_id: str, name: str, ip: str, proxied: bool, ttl: int) -> bool:
    query = {"type": "A", "name": name}
    existing = _cf_request(client, "GET", f"/zones/{zone_id}/dns_records", params=query)
    payload = {
        "type": "A",
        "name": name,
        "content": ip,
        "ttl": ttl,
        "proxied": proxied,
        "comment": "managed-by-infra2",
    }

    if existing:
        record = existing[0]
        record_id = record.get("id")
        if record.get("content") == ip and record.get("proxied") == proxied:
            info(f"DNS record ok: {name}")
            return True
        result = _cf_request(client, "PUT", f"/zones/{zone_id}/dns_records/{record_id}", json=payload)
        if result is None:
            return False
        success(f"DNS record updated: {name}")
        return True

    result = _cf_request(client, "POST", f"/zones/{zone_id}/dns_records", json=payload)
    if result is None:
        return False
    success(f"DNS record created: {name}")
    return True


def _ensure_zone_setting(client: httpx.Client, zone_id: str, setting: str, value: str) -> bool:
    current = _cf_request(client, "GET", f"/zones/{zone_id}/settings/{setting}")
    if current and current.get("value") == value:
        info(f"Zone setting ok: {setting}={value}")
        return True
    result = _cf_request(client, "PATCH", f"/zones/{zone_id}/settings/{setting}", json={"value": value})
    if result is None:
        return False
    success(f"Zone setting updated: {setting}={value}")
    return True


def _record_urls(records: Iterable[str]) -> list[str]:
    return [f"https://{record}" for record in records]


def _ensure_dns_records(records: list[str], proxied: bool, ttl: int) -> bool:
    env = get_env()
    internal_domain = env.get("INTERNAL_DOMAIN")
    vps_host = env.get("VPS_HOST")

    if not internal_domain or not vps_host:
        error("Missing env vars", "Ensure INTERNAL_DOMAIN and VPS_HOST are set in init/env_vars")
        return False

    secrets = _load_cloudflare_secrets()
    token = secrets.get("CF_API_TOKEN")
    zone_id = secrets.get("CF_ZONE_ID")
    zone_name = secrets.get("CF_ZONE_NAME") or internal_domain

    if not token:
        error("Missing CF_API_TOKEN", "Set in 1Password item bootstrap/cloudflare")
        return False

    normalized = _normalize_record_list(records, internal_domain)
    if not normalized:
        return False
    if not proxied and ttl <= 1:
        warning("TTL too low for non-proxied records; using 300s")
        ttl = 300
    env_vars(
        "DNS TARGETS",
        {
            "ZONE": zone_name,
            "VPS_HOST": vps_host,
            "PROXIED": str(proxied).lower(),
            "RECORDS": ", ".join(normalized),
        },
    )

    with _cloudflare_client(token) as client:
        resolved_zone = _resolve_zone_id(client, zone_id, zone_name)
        if not resolved_zone:
            return False
        for record in normalized:
            if not _ensure_record(client, resolved_zone, record, vps_host, proxied, ttl):
                return False

    return True


def _ensure_ssl_settings(mode: str, always_https: str) -> bool:
    env = get_env()
    internal_domain = env.get("INTERNAL_DOMAIN")
    if not internal_domain:
        error("Missing INTERNAL_DOMAIN", "Set in init/env_vars")
        return False

    secrets = _load_cloudflare_secrets()
    token = secrets.get("CF_API_TOKEN")
    zone_id = secrets.get("CF_ZONE_ID")
    zone_name = secrets.get("CF_ZONE_NAME") or internal_domain

    if not token:
        error("Missing CF_API_TOKEN", "Set in 1Password item bootstrap/cloudflare")
        return False

    with _cloudflare_client(token) as client:
        resolved_zone = _resolve_zone_id(client, zone_id, zone_name)
        if not resolved_zone:
            return False
        if not _ensure_zone_setting(client, resolved_zone, "ssl", mode):
            return False
        if not _ensure_zone_setting(client, resolved_zone, "always_use_https", always_https):
            return False
    return True


def _warm_certs(records: list[str], retries: int, delay: float) -> bool:
    urls = _record_urls(records)
    ok = True

    for url in urls:
        for attempt in range(1, retries + 1):
            try:
                resp = httpx.get(url, timeout=10.0)
                info(f"HTTPS ok: {url} ({resp.status_code})")
                break
            except httpx.SSLError as exc:
                warning(f"TLS not ready: {url} (attempt {attempt}/{retries})")
                try:
                    info(f"Retrying {url} without TLS verification to warm up certificate")
                    httpx.get(url, timeout=10.0, verify=False)
                except Exception:
                    warning(f"Unverified warm-up failed for {url}")
                time.sleep(delay)
            except Exception as exc:
                warning(f"HTTPS error: {url} ({exc})")
                time.sleep(delay)
        else:
            # for-else: executes when all attempts failed without a break.
            error("TLS warm-up failed", url)
            ok = False

    return ok


def _verify_dns(records: list[str]) -> bool:
    failures = []
    max_workers = min(10, len(records)) or 1

    def _resolve(record: str) -> str | None:
        try:
            socket.gethostbyname(record)
            return None
        except socket.gaierror as exc:
            return f"{record}: {exc}"

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        for result in executor.map(_resolve, records):
            if result:
                failures.append(result)
    if failures:
        error("DNS resolution failed", "; ".join(failures))
        return False
    success("DNS resolution ok")
    return True


def _verify_https(records: list[str]) -> bool:
    failures = []
    urls = _record_urls(records)
    max_workers = min(10, len(urls)) or 1

    def _check(url: str) -> str | None:
        try:
            resp = httpx.get(url, timeout=10.0)
            if resp.status_code >= 400:
                return f"{url}: {resp.status_code}"
            return None
        except Exception as exc:
            return f"{url}: {exc}"

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        for result in executor.map(_check, urls):
            if result:
                failures.append(result)
    if failures:
        error("HTTPS verification failed", "; ".join(failures))
        return False
    success("HTTPS verification ok")
    return True


def _write_record_list(records: list[str]) -> bool:
    secrets = OpSecrets(item=CLOUDFLARE_ITEM)
    joined = ",".join(records)
    return secrets.set(RECORDS_KEY, joined)


@task
def apply(c, records="", proxied="true", ttl=str(DEFAULT_TTL)):
    """Ensure Cloudflare DNS records for bootstrap domains"""
    header("DNS apply", "Cloudflare DNS records")
    record_list = _load_record_list(records)
    proxied_flag = _parse_bool(proxied, True)
    try:
        ttl_int = int(ttl)
    except ValueError:
        ttl_int = DEFAULT_TTL
    if _ensure_dns_records(record_list, proxied_flag, ttl_int):
        success("DNS apply complete")


@task
def add(c, records=""):
    """Add DNS records to the 1Password list (CF_RECORDS)"""
    header("DNS add", "Update CF_RECORDS in 1Password")
    new_records = _split_records_or_empty(records)
    if not new_records:
        error("No records provided", "Use --records=sub1,sub2")
        return

    current = _load_record_list("")
    merged = list(dict.fromkeys(current + new_records))
    if not _write_record_list(merged):
        error("Failed to update CF_RECORDS", "Check 1Password CLI auth")
        return

    env_vars("CF_RECORDS", {"CF_RECORDS": ", ".join(merged)})
    success("CF_RECORDS updated")


@task
def ssl(c, mode="full", always_https="on"):
    """Ensure Cloudflare SSL settings for the zone"""
    header("DNS ssl", "Cloudflare SSL settings")
    if _ensure_ssl_settings(mode, always_https):
        success("SSL settings complete")


@task
def warm(c, records="", retries="8", delay="6"):
    """Warm HTTPS endpoints to trigger certificate issuance (retries = max attempts)"""
    header("DNS warm", "HTTPS warm-up")
    record_list = _load_record_list(records)
    normalized = _normalize_record_list(record_list)
    if not normalized:
        return
    try:
        retries_int = int(retries)
        delay_float = float(delay)
    except ValueError:
        retries_int = 8
        delay_float = 6.0
    if _warm_certs(normalized, retries_int, delay_float):
        success("HTTPS warm-up complete")


@task
def verify(c, records=""):
    """Verify DNS resolution and HTTPS connectivity"""
    header("DNS verify", "DNS + HTTPS checks")
    record_list = _load_record_list(records)
    normalized = _normalize_record_list(record_list)
    if not normalized:
        return False
    if not _verify_dns(normalized):
        return False
    return _verify_https(normalized)


@task
def setup(c, records="", proxied="true", ssl_mode="full", always_https="on", cooldown=str(DEFAULT_COOLDOWN_SECONDS)):
    """Full setup: DNS records + SSL settings + HTTPS warm-up.

    Includes a cooldown (default 60s) for DNS/SSL propagation before warm-up.
    Override with --cooldown=N (use --cooldown=0 to skip).
    """
    header("DNS setup", "Cloudflare DNS + SSL automation")
    record_list = _load_record_list(records)
    proxied_flag = _parse_bool(proxied, True)
    if not _ensure_dns_records(record_list, proxied_flag, DEFAULT_TTL):
        return
    if not _ensure_ssl_settings(ssl_mode, always_https):
        return
    normalized = _normalize_record_list(record_list)
    if not normalized:
        return
    try:
        cooldown_seconds = int(cooldown)
    except ValueError:
        cooldown_seconds = DEFAULT_COOLDOWN_SECONDS
    if cooldown_seconds > 0:
        warning(f"Cooldown {cooldown_seconds}s for DNS/SSL propagation")
        time.sleep(cooldown_seconds)
    _warm_certs(normalized, retries=8, delay=6.0)
    success("DNS setup complete")
