"""
Vault deployment automation tasks
Uses libs/ system for consistent environment and console utilities.
"""
from invoke import task
from libs.deployer import Deployer
from libs.common import get_env
from libs.console import header, success, error, warning, info, prompt_action, run_with_status, console
from typing import Any


class VaultDeployer(Deployer):
    """Vault deployer using libs/ system"""
    
    service = "vault"
    project = "bootstrap"
    compose_path = "bootstrap/05.vault/compose.yaml"
    data_path = "/data/bootstrap/vault"
    uid = "100"   # Vault official image runs as uid 100
    gid = "1000"
    chmod = "755"
    
    # Domain configuration via Dokploy API
    subdomain = "vault"
    service_port = 8200
    service_name = "vault"

    @classmethod
    def pre_compose(cls, c) -> dict | None:
        """Prepare data directory, upload config, and fetch secrets."""
        # 1. Prepare directories
        if not cls._prepare_dirs(c):
            return None
            
        e = cls.env()
        ssh_user = e.get("VPS_SSH_USER") or "root"
        header("Vault pre_compose", "Preparing resources")

        # 2. Upload config
        if not cls.upload_config(c):
            return None
            
        # 3. Create subdirectories with strict permissions
        # vault user (100) needs write access to file/ logs/
        run_with_status(
            c,
            f"ssh {ssh_user}@{e['VPS_HOST']} 'mkdir -p {cls.data_path}/{{file,logs,config}} && chown -R {cls.uid}:{cls.gid} {cls.data_path}'",
            "Set directory structure and permissions"
        )

        # 4. Fetch 1Password Secrets for Unsealer
        info("Fetching secrets from 1Password...")
        env_vars = {
            "INTERNAL_DOMAIN": e.get("INTERNAL_DOMAIN"),
            "OP_VAULT_ID": "Infra2",  # Default vault
        }
        
        try:
            from libs.env import OpSecrets
            
            # OP_CONNECT_TOKEN (from 1Password Connect service account)
            # Item: "bootstrap/1password/VPS-01 Access Token: own_service"
            token_item = OpSecrets(item="bootstrap/1password/VPS-01 Access Token: own_service")
            token = token_item.get("credential")
            if token:
                env_vars["OP_CONNECT_TOKEN"] = token
            else:
                warning("Could not finding OP_CONNECT_TOKEN in 1Password")
                
            # OP_ITEM_ID (Item "bootstrap/vault/Unseal Keys" where unseal keys are stored)
            try:
                # We need the Item ID, not content. Use CLI wrapper or name
                # If item doesn't exist yet (pre-init), we might skip or leave empty.
                # Here we assume it might exist or will be created. 
                # passing name might work if unsealer supports it, but compose expects ID usually.
                # Let's try to look it up.
                cmd = "op item get 'bootstrap/vault/Unseal Keys' --vault Infra2 --format json"
                res = c.run(cmd, hide=True, warn=True)
                if res.ok:
                    import json
                    item = json.loads(res.stdout)
                    env_vars["OP_ITEM_ID"] = item["id"]
                else:
                    info("Vault item 'bootstrap/vault/Unseal Keys' not found (normal if first run)")
                    env_vars["OP_ITEM_ID"] = "" 
            except Exception as ex:
                warning(f"Failed to lookup Vault item ID: {ex}")
                env_vars["OP_ITEM_ID"] = ""

        except ImportError:
            error("Missing libs.env dependencies")
            return None
        except Exception as ex:
            error(f"Failed to fetch secrets: {ex}")
            return None
            
        success("pre_compose complete")
        return env_vars

    @classmethod
    def upload_config(cls, c) -> bool:
        """Upload Vault config file."""
        e = cls.env()
        ssh_user = e.get("VPS_SSH_USER") or "root"
        # Ensure config dir exists first
        c.run(f"ssh {ssh_user}@{e['VPS_HOST']} 'mkdir -p {cls.data_path}/config'")
        
        result = run_with_status(
            c,
            f"scp bootstrap/05.vault/vault.hcl {ssh_user}@{e['VPS_HOST']}:{cls.data_path}/config/",
            "Upload config file",
        )
        return result.ok
    
    @classmethod
    def composing(cls, c, env_vars: dict) -> str:
        """Deploy Vault via Dokploy API (using GitHub provider)"""
        from libs.dokploy import ensure_project, get_dokploy
        from libs.const import GITHUB_OWNER, GITHUB_REPO, GITHUB_BRANCH
        
        e = cls.env()
        header(f"{cls.service} composing", f"Deploying via Dokploy API")
        
        # Ensure project exists
        domain = e.get('INTERNAL_DOMAIN')
        host = f"cloud.{domain}" if domain else None
        
        # Priority: Hardcoded "bootstrap" for this module
        project_name = cls.project
        
        project_id, env_id = ensure_project(project_name, f"Bootstrap services: {project_name}", host=host)
        if not env_id:
            from invoke.exceptions import Exit
            error("Failed to get environment ID")
            raise Exit("Failed to get environment ID", code=1)
            
        # Deploy compose using GitHub provider
        client = get_dokploy(host=host)
        
        # Get GitHub provider ID
        github_id = client.get_github_provider_id()
        if not github_id:
             from invoke.exceptions import Exit
             error("No GitHub provider configured in Dokploy. Please add one in Settings -> Git Providers.")
             raise Exit("No GitHub provider found", code=1)
             
        info(f"Using GitHub provider: {github_id}")
        
        # Check if exists
        existing = client.find_compose_by_name(cls.service, project_name)
        
        if existing:
            compose_id = existing["composeId"]
            info("Updating existing compose service")
            client.update_compose(
                compose_id, 
                source_type="github",
                githubId=github_id,
                repository=GITHUB_REPO,
                owner=GITHUB_OWNER,
                branch=GITHUB_BRANCH,
                composePath=cls.compose_path,
            )
        else:
            info("Creating new compose service with GitHub provider")
            result = client.create_compose(
                environment_id=env_id,
                name=cls.service,
                app_name=f"bootstrap-{cls.service}",
                source_type="github",
                githubId=github_id,
                repository=GITHUB_REPO,
                owner=GITHUB_OWNER,
                branch=GITHUB_BRANCH,
                composePath=cls.compose_path,
            )
            compose_id = result["composeId"]
        
        # Update environment variables
        info("Updating environment variables (from libs)")
        # Filter out internal keys or empty values
        env_content = "\n".join([f"{k}={v}" for k, v in env_vars.items() if v is not None])
        client.update_compose(compose_id, env=env_content)
        
        info(f"Deploying compose {compose_id}...")
        client.deploy_compose(compose_id)
        
        # Configure domain via Dokploy API
        if cls.subdomain and cls.service_port:
            domain_host = f"{cls.subdomain}.{domain}"
            info(f"Configuring domain: {domain_host}")
            try:
                client.create_domain(
                    compose_id=compose_id,
                    host=domain_host,
                    port=cls.service_port,
                    https=True,
                    service_name=cls.service_name,
                )
                success(f"Domain configured: https://{domain_host}")
                # Redeploy to apply domain labels
                client.deploy_compose(compose_id)
            except Exception as exc:
                if "409" in str(exc) or "already" in str(exc).lower():
                    info(f"Domain already exists: {domain_host}")
                else:
                    warning(f"Domain configuration skipped: {exc}")
        
        success(f"Deployed {cls.service} (composeId: {compose_id})")
        return compose_id

    @classmethod
    def post_compose(cls, c, shared_tasks: Any) -> bool:
        """Verify deployment"""
        e = cls.env()
        header("Vault post_compose", "Verifying")
        if cls.check_status(c, shared_tasks):
            success("Vault is reachable")
            return True
        warning("Vault may need initialization")
        return False
        
    @classmethod
    def check_status(cls, c, shared_tasks: Any) -> bool:
        """Custom status check for Vault"""
        e = cls.env()
        result = c.run(
            f"curl -s -o /dev/null -w '%{{http_code}}' https://vault.{e['INTERNAL_DOMAIN']}/v1/sys/health",
            warn=True,
            hide=True,
        )
        if not result.ok:
            error("Vault status check failed: curl command did not complete successfully.")
            stderr = getattr(result, "stderr", "") or ""
            if stderr.strip():
                error(stderr.strip())
            return False
        status_code = (result.stdout or "").strip()
        if not status_code:
            error("Vault status check failed: no HTTP status code returned by curl.")
            return False
        if status_code in {"200", "429", "472", "473"}:
            return True
        warning(f"Vault health endpoint returned unexpected status code: {status_code}")
        return False

    @classmethod
    def is_reachable(cls, c) -> bool:
        """Check if Vault is reachable (any valid response from health endpoint)"""
        e = cls.env()
        result = c.run(
            f"curl -s -o /dev/null -w '%{{http_code}}' https://vault.{e['INTERNAL_DOMAIN']}/v1/sys/health",
            warn=True,
            hide=True,
        )
        if not result.ok:
            return False
        status_code = (result.stdout or "").strip()
        # 501=not initialized, 503=sealed - both mean Vault is reachable
        return status_code in {"200", "429", "472", "473", "501", "503"}


# Standard tasks
# We don't use make_tasks fully because Vault requires extra steps (init, unseal)
prepare = task(lambda c: VaultDeployer.pre_compose(c), name="prepare")
upload_config = task(lambda c: VaultDeployer.upload_config(c), name="upload-config")
@task(name="deploy")
def deploy(c):
    """Deploy Vault (prepares, injects vars, and composes)"""
    # Fetch env vars (includes secrets and domain)
    env_vars = VaultDeployer.pre_compose(c)
    if env_vars is None:
        error("Deployment failed: pre_compose returned No config")
        from invoke.exceptions import Exit
        raise Exit("pre_compose failed", code=1)
        
    VaultDeployer.composing(c, env_vars)


@task(pre=[deploy])
def init(c):
    """Initialize Vault (checks reachability first)"""
    e = get_env()
    header("Vault init", "Checking reachability")
    
    # Pre-check: Vault must be reachable before init (501/503 are OK - means service is up)
    if not VaultDeployer.is_reachable(c):
        error("Vault is not reachable. Deployment may have failed.")
        info("Check logs: ssh root@<host> 'docker logs vault'")
        from invoke.exceptions import Exit
        raise Exit("Vault not reachable", code=1)
    
    success("Vault is reachable, ready for initialization")
    print(f"export VAULT_ADDR=https://vault.{e['INTERNAL_DOMAIN']}")
    print("vault operator init")
    prompt_action("Initialize Vault", [
        "Run the commands above",
        "Save keys to 1Password"
    ])


@task
def unseal(c):
    """(Manual trigger) Restart unsealer container"""
    e = get_env()
    ssh_user = e.get("VPS_SSH_USER") or "root"
    header("Vault unseal", "Triggering unsealer")
    c.run(f"ssh {ssh_user}@{e['VPS_HOST']} 'docker logs --tail 20 vault-unsealer'", warn=True)
    c.run(f"ssh {ssh_user}@{e['VPS_HOST']} 'docker restart vault-unsealer'")
    success("Unsealer restarted")


@task
def status(c):
    """Check Vault status"""
    e = get_env()
    ssh_user = e.get("VPS_SSH_USER") or "root"
    header("Vault status", "Checking")
    c.run(f"curl -s https://vault.{e['INTERNAL_DOMAIN']}/v1/sys/health", warn=True)
    c.run(f"ssh {ssh_user}@{e['VPS_HOST']} 'docker ps | grep vault'", warn=True)


@task
def setup_tokens(c):
    """Generate read-only tokens for platform services"""
    import os
    import json

    header("Vault Token Setup", "Generating service tokens")
    
    # Check VAULT_ROOT_TOKEN
    root_token = os.getenv("VAULT_ROOT_TOKEN")
    if not root_token:
        error("VAULT_ROOT_TOKEN not set")
        info("Get from: op read 'op://Infra2/dexluuvzg5paff3cltmtnlnosm/Root Token'")
        info("Then run: export VAULT_ROOT_TOKEN=<token>")
        return
    
    e = get_env()
    vault_addr = e.get("VAULT_ADDR", f"https://vault.{e['INTERNAL_DOMAIN']}")
    
    success(f"Using Vault: {vault_addr}")
    print("")
    
    # Automatically discover policies from platform directories
    current_dir = os.path.dirname(os.path.abspath(__file__))  # bootstrap/05.vault
    root_dir = os.path.dirname(os.path.dirname(current_dir))
    platform_dir = os.path.join(root_dir, "platform")
    
    # Map service name to its directory name
    service_map = {
        "postgres": "01.postgres",
        "redis": "02.redis",
        "minio": "03.minio",
        "authentik": "10.authentik",
    }
    
    env_name = e.get("ENV", "production")
    
    for service, dir_name in service_map.items():
        policy_name = f"platform-{service}-app"
        policy_path = os.path.join(platform_dir, dir_name, "vault-policy.hcl")
        
        if os.path.exists(policy_path):
            with open(policy_path, "r") as f:
                policy_rules = f.read().replace("{{env}}", env_name)
            info(f"Loaded tailored policy from {dir_name}/vault-policy.hcl")
        else:
            # Fallback policy
            policy_rules = f'path "secret/data/platform/{env_name}/{service}" {{ capabilities = ["read", "list"] }}'
            warning(f"No policy file found for {service}, using default read-only")
        
        # Write policy via vault CLI using stdin
        import io
        policy_io = io.StringIO(policy_rules)
        
        info(f"   Writing policy: {policy_name}...")
        result = c.run(
            f"vault policy write {policy_name} -",
            env={"VAULT_ADDR": vault_addr, "VAULT_TOKEN": root_token},
            in_stream=policy_io,
            hide=True,
            warn=True,
        )
        if not result.ok:
            stderr_msg = getattr(result, "stderr", "") or ""
            if stderr_msg.strip():
                error(f"Failed to create policy '{policy_name}' for service '{service}'", stderr_msg.strip())
            else:
                error(f"Failed to create policy '{policy_name}' for service '{service}'")
            continue
        success(f"   ✅ Policy {policy_name} created")

        # Generate token (permanent, orphan, no default policy)
        cmd = (
            f"vault token create "
            f"-orphan "
            f"-policy={policy_name} "
            f"-no-default-policy "
            f"-display-name=platform-{service} "
            f"-format=json"
        )
        result = c.run(
            cmd,
            env={"VAULT_ADDR": vault_addr, "VAULT_TOKEN": root_token},
            hide=True,
        )
        
        if result.ok:
            token_data = json.loads(result.stdout)
            token = token_data["auth"]["client_token"]
            success(f"✅ Token for {service}:")
            print(f"   {token}")
            _configure_dokploy_token(c, service, token)
            print()
        else:
            error(f"Failed to create token for {service}")

    success("All tokens generated!")
    info("Next steps:")
    info("1. Store tokens in 1Password (optional but recommended)")
    info("2. Set VAULT_APP_TOKEN in Dokploy for each service")
    info("3. Run: invoke <service>.setup")


@task
def setup(c):
    """Complete Vault setup flow"""
    # Check if already running
    if VaultDeployer.check_status(c, None):
        success("Vault already healthy - skipping setup")
        return

    deploy(c)
    init(c)
    unseal(c)
    success("Vault setup complete!")


def _configure_dokploy_token(_c, service: str, token: str):
    """Auto-configure VAULT_APP_TOKEN in Dokploy"""
    try:
        from libs.dokploy import get_dokploy
        from libs.common import get_env
        
        e = get_env()
        domain = e.get('INTERNAL_DOMAIN')
        host = f"cloud.{domain}" if domain else None
        client = get_dokploy(host=host)
        
        # Find compose service
        compose = client.find_compose_by_name(service, "platform")
        if compose:
            compose_id = compose["composeId"]
            info("   Configuring in Dokploy...")
            
            client.update_compose_env(
                compose_id,
                env_vars={"VAULT_APP_TOKEN": token}
            )
            success("   ✅ Auto-configured in Dokploy")
        else:
            warning(f"   Service '{service}' not found in Dokploy, manual setup required")
    except Exception as exc:
        warning(f"   Auto-config failed: {exc}")
        info("   Manual setup: Add VAULT_APP_TOKEN in Dokploy UI")
