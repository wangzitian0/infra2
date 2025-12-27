import os
import time
import httpx
import sys

# Configuration from environment
VAULT_ADDR = os.environ.get("VAULT_ADDR", "http://vault:8200")
OP_CONNECT_HOST = os.environ.get("OP_CONNECT_HOST", "http://op-connect-api:8080")
OP_CONNECT_TOKEN = os.environ.get("OP_CONNECT_TOKEN")
# 1Password identifiers from environment
OP_VAULT_ID = os.environ.get("OP_VAULT_ID") 
OP_ITEM_ID = os.environ.get("OP_ITEM_ID")

CHECK_INTERVAL = 30 # seconds

print(f"[{time.ctime()}] Script loaded. Checking environment...", flush=True)

def unseal():
    print(f"[{time.ctime()}] Checking Vault health at {VAULT_ADDR}...", flush=True)
    try:
        # Check health
        # 200 = unsealed, 429 = unsealed (standby), 503 = sealed, 501 = not initialized
        with httpx.Client(verify=False) as client:
            resp = client.get(f"{VAULT_ADDR}/v1/sys/health")
            data = resp.json()
            
            if not data.get("sealed"):
                print(f"[{time.ctime()}] Vault is already unsealed.", flush=True)
                return

            print(f"[{time.ctime()}] Vault is SEALED. Starting unseal process...", flush=True)

            # Fetch keys from 1Password
            headers = {"Authorization": f"Bearer {OP_CONNECT_TOKEN}"}
            api_url = f"{OP_CONNECT_HOST}/v1/vaults/{OP_VAULT_ID}/items/{OP_ITEM_ID}"
            
            op_resp = client.get(api_url, headers=headers)
            op_resp.raise_for_status()
            item = op_resp.json()
            
            keys = [f["value"] for f in item.get("fields", []) if f.get("label", "").startswith("Unseal Key")]
            
            if len(keys) < 3:
                print(f"[{time.ctime()}] ERROR: Not enough unseal keys found in 1Password (found {len(keys)})", flush=True)
                return

            # Submit keys
            for i, key in enumerate(keys[:3]):
                print(f"[{time.ctime()}] Submitting key {i+1}/3...", flush=True)
                unseal_resp = client.post(f"{VAULT_ADDR}/v1/sys/unseal", json={"key": key})
                unseal_resp.raise_for_status()
                status = unseal_resp.json()
                
                if not status.get("sealed"):
                    print(f"[{time.ctime()}] ✅ Vault successfully unsealed!", flush=True)
                    return
                print(f"[{time.ctime()}] Progress: {status.get('progress')}/3", flush=True)

    except Exception as e:
        print(f"[{time.ctime()}] ERROR during unseal: {str(e)}", flush=True)

if __name__ == "__main__":
    if not all([OP_CONNECT_TOKEN, OP_VAULT_ID, OP_ITEM_ID]):
        print("ERROR: OP_CONNECT_TOKEN, OP_VAULT_ID, and OP_ITEM_ID environment variables are required.", flush=True)
        sys.exit(1)
        
    print(f"Starting Vault Unsealer Sidecar (Checking every {CHECK_INTERVAL}s)...", flush=True)
    while True:
        try:
            unseal()
        except Exception as e:
            print(f"[{time.ctime()}] CRITICAL: Unexpected error in main loop: {e}", flush=True)
        time.sleep(CHECK_INTERVAL)
