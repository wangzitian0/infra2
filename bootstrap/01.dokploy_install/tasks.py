"""
Dokploy installation automation tasks.
Bootstrap layer - manual server provisioning, no Vault dependency.
"""
from invoke import task
from libs.common import get_env
from libs.console import header, success, error, warning, info, prompt_action, run_with_status


DOKPLOY_VERSION = "v0.25.11"  # ⚠️ v0.26.2 has critical deployment issues


@task
def install(c, version=DOKPLOY_VERSION):
    """Install Dokploy on VPS with specified version.
    
    Args:
        version: Dokploy version to install (default: v0.25.11)
    
    ⚠️ WARNING: v0.26.2 has known deployment failures. Use v0.25.11.
    """
    header("Dokploy Installation", f"Installing Dokploy {version}")
    
    e = get_env()
    vps_host = e.get("VPS_HOST")
    vps_user = e.get("VPS_SSH_USER") or "root"
    
    if not vps_host:
        error("Missing VPS_HOST", "Set in 1Password item init/env_vars")
        return False
    
    info(f"Target: {vps_user}@{vps_host}")
    info(f"Version: {version}")
    
    if version == "v0.26.2":
        warning("⚠️  v0.26.2 has known deployment issues!")
        warning("Consider using v0.25.11 instead")
        prompt_action("Confirm installation", [
            f"You are about to install {version}",
            "This version is known to have issues",
            "Press Ctrl+C to cancel, or Enter to proceed anyway"
        ])
    
    # Install Dokploy on VPS
    install_cmd = (
        f"curl -sSL https://dokploy.com/install.sh | "
        f"DOKPLOY_VERSION={version} sh"
    )
    
    result = run_with_status(
        c,
        f"ssh {vps_user}@{vps_host} '{install_cmd}'",
        f"Installing Dokploy {version}",
        hide=False
    )
    
    if not result.ok:
        error("Installation failed", "Check SSH connection and server logs")
        return False
    
    success(f"Dokploy {version} installed successfully")
    return True


@task
def verify(c):
    """Verify Dokploy installation and service health."""
    header("Dokploy Verification", "Checking installation")
    
    e = get_env()
    vps_host = e.get("VPS_HOST")
    vps_user = e.get("VPS_SSH_USER") or "root"
    
    if not vps_host:
        error("Missing VPS_HOST", "Set in 1Password item init/env_vars")
        return False
    
    # Check Docker containers
    docker_check = run_with_status(
        c,
        f"ssh {vps_user}@{vps_host} 'docker ps | grep dokploy'",
        "Checking Dokploy containers"
    )
    
    if not docker_check.ok:
        error("Dokploy containers not running")
        return False
    
    # Check HTTP endpoint
    http_check = run_with_status(
        c,
        f"ssh {vps_user}@{vps_host} 'curl -I http://localhost:3000'",
        "Checking HTTP endpoint"
    )
    
    if not http_check.ok:
        error("Dokploy HTTP endpoint not responding")
        return False
    
    success("Dokploy installation verified")
    info(f"Access at: http://{vps_host}:3000")
    return True


@task
def version_check(c):
    """Check currently installed Dokploy version."""
    header("Dokploy Version", "Checking installed version")
    
    e = get_env()
    vps_host = e.get("VPS_HOST")
    vps_user = e.get("VPS_SSH_USER") or "root"
    
    if not vps_host:
        error("Missing VPS_HOST", "Set in 1Password item init/env_vars")
        return
    
    result = c.run(
        f"ssh {vps_user}@{vps_host} 'docker inspect dokploy --format \"{{{{.Config.Image}}}}\"'",
        warn=True,
        hide=True
    )
    
    if result.ok:
        image = result.stdout.strip()
        info(f"Dokploy image: {image}")
        if "0.26.2" in image:
            warning("⚠️  Running v0.26.2 which has known issues")
            warning("Consider reinstalling with v0.25.11")
    else:
        error("Unable to determine version", "Dokploy may not be installed")


@task
def setup(c, version=DOKPLOY_VERSION):
    """Full setup: install + verify + next steps guidance.
    
    Args:
        version: Dokploy version to install (default: v0.25.11)
    """
    header("Dokploy Setup", "Complete installation workflow")
    
    # Step 1: Install
    if not install(c, version=version):
        return
    
    # Step 2: Verify
    if not verify(c):
        return
    
    # Step 3: Next steps
    e = get_env()
    vps_host = e.get("VPS_HOST")
    internal_domain = e.get("INTERNAL_DOMAIN")
    
    prompt_action("Complete initial setup", [
        f"1. Visit http://{vps_host}:3000 in your browser",
        "2. Create administrator account",
        "3. Note down credentials in 1Password (bootstrap-dokploy)",
        "4. Return here when complete"
    ])
    
    success("Dokploy setup complete!")
    info("Next steps:")
    info("  1. Run: invoke dns_and_cert.setup")
    info(f"  2. Configure Dokploy domain: cloud.{internal_domain}")


@task
def uninstall(c):
    """Uninstall Dokploy from VPS (use with caution)."""
    header("Dokploy Uninstall", "⚠️  DESTRUCTIVE OPERATION")
    
    e = get_env()
    vps_host = e.get("VPS_HOST")
    vps_user = e.get("VPS_SSH_USER") or "root"
    
    if not vps_host:
        error("Missing VPS_HOST")
        return
    
    warning("This will remove Dokploy and all its data!")
    prompt_action("Confirm uninstall", [
        "⚠️  All projects, apps, and data will be deleted",
        "Press Ctrl+C to cancel, or Enter to proceed"
    ])
    
    uninstall_cmd = "docker stop dokploy && docker rm dokploy"
    
    result = run_with_status(
        c,
        f"ssh {vps_user}@{vps_host} '{uninstall_cmd}'",
        "Uninstalling Dokploy"
    )
    
    if result.ok:
        success("Dokploy uninstalled")
    else:
        error("Uninstall failed", "Container may not exist")


@task
def logs(c, lines="100"):
    """View Dokploy container logs.
    
    Args:
        lines: Number of log lines to display (default: 100)
    """
    header("Dokploy Logs", f"Last {lines} lines")
    
    e = get_env()
    vps_host = e.get("VPS_HOST")
    vps_user = e.get("VPS_SSH_USER") or "root"
    
    if not vps_host:
        error("Missing VPS_HOST")
        return
    
    c.run(
        f"ssh {vps_user}@{vps_host} 'docker logs --tail {lines} dokploy'",
        pty=True
    )
