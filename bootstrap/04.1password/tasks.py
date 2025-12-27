"""
1Password Connect deployment automation tasks
"""
import os
from invoke import task


# Environment variables
VPS_HOST = os.environ.get("VPS_HOST")
INTERNAL_DOMAIN = os.environ.get("INTERNAL_DOMAIN")


@task
def prepare(c):
    """Prepare 1Password data directory"""
    print("\n📁 Preparing 1Password data directory...")
    
    # Create directory
    c.run(f"ssh root@{VPS_HOST} 'mkdir -p /data/bootstrap/1password'")
    
    # Set permissions (allow container to write database files)
    c.run(f"ssh root@{VPS_HOST} 'chown -R 1000:1000 /data/bootstrap/1password'")
    c.run(f"ssh root@{VPS_HOST} 'chmod 777 /data/bootstrap/1password'")
    
    # Verify
    result = c.run(f"ssh root@{VPS_HOST} 'ls -la /data/bootstrap/1password'", hide=True)
    print(result.stdout)
    print("✅ Directory preparation complete")


@task
def upload_credentials(c):
    """Upload 1Password credentials file"""
    print("\n📤 Uploading credentials file...")
    
    # Reading credentials from 1Password Vault...
    print("Reading credentials from 1Password Vault...")
    cmd = f"op document get 'bootstrap/1password/VPS-01 Credentials File' --vault Infra2 | ssh root@{VPS_HOST} 'cat > /data/bootstrap/1password/1password-credentials.json && chown 1000:1000 /data/bootstrap/1password/1password-credentials.json'"
    
    result = c.run(cmd, warn=True)
    if not result.ok:
        print("❌ Upload failed, please ensure:")
        print("  1. 1Password CLI (op) is installed")
        print("  2. Logged in: eval $(op signin)")
        print("  3. 'VPS-01 Credentials File' exists in Vault 'Infra2'")
        raise Exception("Credentials upload failed")
    
    # Verify
    result = c.run(f"ssh root@{VPS_HOST} 'ls -lh /data/bootstrap/1password/1password-credentials.json'")
    print("✅ Credentials uploaded")


@task(pre=[prepare, upload_credentials])
def deploy(c):
    """Deploy 1Password Connect to Dokploy"""
    print("\n🚀 Deploying 1Password Connect...")
    print("\n" + "="*60)
    print("⏸️ Please complete the following in Dokploy UI:")
    print("="*60)
    print(f"1. Access: https://cloud.{INTERNAL_DOMAIN}")
    print("2. Create Project: bootstrap (if not exists)")
    print("3. Create Docker Compose App:")
    print("   - Name: 1password-connect")
    print("   - Repository: GitHub → wangzitian0/infra2")
    print("   - Branch: main")
    print("   - Compose Path: bootstrap/04.1password/compose.yaml")
    print("4. Click Deploy")
    print("5. Wait for deployment to complete (watch logs)")
    print("="*60)
    
    input("\n✋ Press Enter to continue after completion...")
    
    # Verify deployment
    print("\n🔍 Verifying 1Password Connect service...")
    result = c.run(f"curl -s https://op.{INTERNAL_DOMAIN}/health", warn=True)
    if result.ok and "1Password Connect" in result.stdout:
        print("✅ 1Password Connect service is healthy")
        print(result.stdout)
    else:
        print("⚠️ Service temporarily unavailable (may need to wait a few minutes)")


@task(pre=[deploy])
def verify(c):
    """Verify 1Password Connect functionality"""
    print("\n🔍 Verifying 1Password Connect...")
    
    # Health check
    print("1. Health check:")
    result = c.run(f"curl -s https://op.{INTERNAL_DOMAIN}/health", warn=True)
    if result.ok:
        print(result.stdout)
    
    # Test reading secrets (optional)
    print("\n2. Test reading secrets (requires Access Token):")
    print("   Run the following command to test:")
    print(f"   TOKEN=$(op item get 'VPS-01 Access Token: own_service' --vault Infra2 --fields credential --reveal)")
    print(f"   curl -H \"Authorization: Bearer $TOKEN\" https://op.{INTERNAL_DOMAIN}/v1/vaults")


@task
def status(c):
    """Check 1Password Connect status"""
    print(f"\n🔍 Checking 1Password Connect status...")
    
    # Check HTTP
    c.run(f"curl -s https://op.{INTERNAL_DOMAIN}/health", warn=True)
    
    # Check container status
    print(f"\nChecking container status:")
    c.run(f"ssh root@{VPS_HOST} 'docker ps | grep op-connect'", warn=True)
    
    # Check data directory
    print(f"\nChecking data directory:")
    c.run(f"ssh root@{VPS_HOST} 'ls -lh /data/bootstrap/1password/'", warn=True)


@task
def fix_permissions(c):
    """Fix database permission issues"""
    print("\n🔧 Fixing permission issues...")
    c.run(f"ssh root@{VPS_HOST} 'chmod 777 /data/bootstrap/1password'")
    print("✅ Permissions fixed to 777")
    print("Note: Recommend redeploying the app in Dokploy")


@task(pre=[prepare, upload_credentials, deploy, verify])
def setup(c):
    """Complete 1Password Connect setup flow"""
    print("\n✅ 1Password Connect setup complete!")
    print(f"\nAccess URL: https://op.{INTERNAL_DOMAIN}")
    print("\nRemember to update SSOT version tracking table:")
    print("docs/ssot/bootstrap.nodep.md")
