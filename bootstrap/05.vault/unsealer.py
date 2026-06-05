import os
import time
import httpx
import sys
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("vault-unsealer")

# Configuration from environment
VAULT_ADDR = os.environ.get("VAULT_ADDR", "http://vault:8200")
OP_CONNECT_HOST = os.environ.get("OP_CONNECT_HOST", "http://op-connect-api:8080")
OP_CONNECT_TOKEN = os.environ.get("OP_CONNECT_TOKEN")
# 1Password identifiers from environment
OP_VAULT_ID = os.environ.get("OP_VAULT_ID")
OP_ITEM_ID = os.environ.get("OP_ITEM_ID")
VERIFY_TLS = os.environ.get("VAULT_VERIFY", "true").lower() not in {"false", "0", "no"}

CHECK_INTERVAL = 30  # seconds

logger.info("Script loaded. Checking environment...")


def connect_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {OP_CONNECT_TOKEN}"}


def connect_item_url() -> str:
    return f"{OP_CONNECT_HOST}/v1/vaults/{OP_VAULT_ID}/items/{OP_ITEM_ID}"


def connect_health_is_active(payload: dict) -> bool:
    statuses = {
        item.get("service"): item.get("status")
        for item in payload.get("dependencies", [])
    }
    return (
        statuses.get("sqlite") == "ACTIVE"
        and statuses.get("sync") == "ACTIVE"
        and statuses.get("1Password") == "ACTIVE"
    )


def health_check() -> int:
    """Return 0 only when Vault is unsealed and 1Password Connect auth works."""
    if not all([OP_CONNECT_TOKEN, OP_VAULT_ID, OP_ITEM_ID]):
        logger.error("Required 1Password Connect environment variables are missing.")
        return 1

    try:
        with httpx.Client(verify=VERIFY_TLS, timeout=5.0) as client:
            op_resp = client.get(f"{OP_CONNECT_HOST}/health")
            if op_resp.status_code != 200:
                logger.error(
                    "1Password Connect health returned HTTP %s", op_resp.status_code
                )
                return 1
            if not connect_health_is_active(op_resp.json()):
                logger.error("1Password Connect dependencies are not active.")
                return 1

            item_resp = client.get(connect_item_url(), headers=connect_headers())
            if item_resp.status_code != 200:
                logger.error(
                    "1Password Connect authenticated item lookup returned HTTP %s",
                    item_resp.status_code,
                )
                return 1

            vault_resp = client.get(f"{VAULT_ADDR}/v1/sys/health")
            if vault_resp.status_code not in {200, 429, 472, 473}:
                logger.error("Vault health returned HTTP %s", vault_resp.status_code)
                return 1

            data = vault_resp.json()
            if data.get("sealed"):
                logger.error("Vault is sealed.")
                return 1
    except Exception as exc:
        logger.exception("Unsealer health check failed: %s", exc)
        return 1

    logger.info("Unsealer dependencies are healthy.")
    return 0


def unseal():
    logger.info("Checking Vault health at %s...", VAULT_ADDR)
    try:
        # Check health
        # 200 = unsealed, 429 = unsealed (standby), 503 = sealed, 501 = not initialized
        if not VERIFY_TLS:
            logger.warning(
                "TLS verification disabled (set VAULT_VERIFY=true to enable)."
            )
        with httpx.Client(verify=VERIFY_TLS) as client:
            resp = client.get(f"{VAULT_ADDR}/v1/sys/health")
            data = resp.json()

            if not data.get("sealed"):
                logger.info("Vault is already unsealed.")
                return

            logger.warning("Vault is SEALED. Starting unseal process...")

            # Fetch keys from 1Password
            op_resp = client.get(connect_item_url(), headers=connect_headers())
            op_resp.raise_for_status()
            item = op_resp.json()

            keys = [
                f["value"]
                for f in item.get("fields", [])
                if f.get("label", "").startswith("Unseal Key")
            ]

            if len(keys) < 3:
                logger.error(
                    "Not enough unseal keys found in 1Password (found %s)", len(keys)
                )
                return

            # Submit keys
            for i, key in enumerate(keys[:3]):
                logger.info("Submitting key %s/3...", i + 1)
                unseal_resp = client.post(
                    f"{VAULT_ADDR}/v1/sys/unseal", json={"key": key}
                )
                unseal_resp.raise_for_status()
                status = unseal_resp.json()

                if not status.get("sealed"):
                    logger.info("Vault successfully unsealed.")
                    return
                logger.info("Progress: %s/3", status.get("progress"))

    except Exception as e:
        logger.exception("Error during unseal: %s", e)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "health":
        sys.exit(health_check())

    if not all([OP_CONNECT_TOKEN, OP_VAULT_ID, OP_ITEM_ID]):
        logger.error(
            "OP_CONNECT_TOKEN, OP_VAULT_ID, and OP_ITEM_ID environment variables are required."
        )
        sys.exit(1)

    logger.info(
        "Starting Vault Unsealer Sidecar (Checking every %ss)...", CHECK_INTERVAL
    )
    while True:
        try:
            unseal()
        except Exception as e:
            logger.exception("Unexpected error in main loop: %s", e)
        time.sleep(CHECK_INTERVAL)
