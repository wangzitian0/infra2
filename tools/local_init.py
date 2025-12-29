"""Local environment initialization and CLI dependency check

Usage:
    invoke local.init       # Check and install all dependencies
    invoke local.check      # Check dependencies only
"""
from __future__ import annotations
from invoke import task
import subprocess
import shutil
import shlex
from libs.console import console, success, error, warning, info, header


CLI_DEPS = {
    'vault': {
        'check': 'vault version',
        'install_mac': 'brew install hashicorp/tap/vault',
        'install_linux': 'sudo apt-get install vault || sudo yum install vault',
        'docs': 'https://developer.hashicorp.com/vault/install',
    },
    'op': {
        'check': 'op --version',
        'install_mac': 'brew install 1password-cli',
        'install_linux': 'See https://developer.1password.com/docs/cli/get-started',
        'docs': 'https://developer.1password.com/docs/cli',
    },
    'dokploy': {
        'check': 'npx @dokploy/cli --version',
        'install': 'npm install -g @dokploy/cli',
        'docs': 'https://docs.dokploy.com/cli',
    },
}


def _check_command(cmd: str) -> bool:
    """Check if a command exists and runs successfully"""
    try:
        result = subprocess.run(
            shlex.split(cmd), capture_output=True, text=True, timeout=10
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False


def _collect_cli_status() -> list[tuple[str, bool, str]]:
    """Collect CLI status for table display."""
    results = []
    for name, dep in CLI_DEPS.items():
        ok = _check_command(dep['check'])
        results.append((name, ok, dep.get('docs', '')))
    return results


def _print_cli_status(results: list[tuple[str, bool, str]]) -> bool:
    """Render CLI status table and return overall OK."""
    from rich.table import Table
    table = Table(show_header=True)
    table.add_column("CLI", style="cyan")
    table.add_column("Status")
    table.add_column("Docs")

    all_ok = True
    for name, ok, docs in results:
        status = "[green]✅ Installed[/]" if ok else "[red]❌ Missing[/]"
        table.add_row(name, status, docs or "")
        if not ok:
            all_ok = False
    console.print(table)
    return all_ok


def _get_platform() -> str:
    """Get current platform"""
    import platform
    system = platform.system().lower()
    if system == 'darwin':
        return 'mac'
    elif system == 'linux':
        return 'linux'
    return 'unknown'


@task
def check(c):
    """Check CLI dependencies"""
    header("Local Environment Check", "Checking CLI dependencies")
    
    results = _collect_cli_status()
    all_ok = _print_cli_status(results)
    if all_ok:
        success("All CLI dependencies installed")
    else:
        warning("Some CLI dependencies are missing")
    
    return all_ok


@task
def init(c):
    """Initialize local environment - check and guide installation"""
    header("Local Environment Init", "Setting up development environment")
    
    # Check Python deps
    info("Python dependencies managed by uv (pyproject.toml)")
    
    # Check CLI deps
    console.print()
    results = _collect_cli_status()
    if not _print_cli_status(results):
        console.print()
        info("Installation instructions:")
        platform = _get_platform()

        missing = {name for name, ok, _ in results if not ok}
        for name, dep in CLI_DEPS.items():
            if name not in missing:
                continue
            console.print()
            info(f"{name}:")
            if platform == 'mac' and 'install_mac' in dep:
                console.print(f"  {dep['install_mac']}")
            elif platform == 'linux' and 'install_linux' in dep:
                console.print(f"  {dep['install_linux']}")
            elif 'install' in dep:
                console.print(f"  {dep['install']}")
            info(f"Docs: {dep['docs']}")
    
    # Check uv
    console.print()
    if shutil.which('uv'):
        success("uv is installed")
    else:
        warning("uv not found - install with: curl -LsSf https://astral.sh/uv/install.sh | sh")
    
    # Check 1Password signin
    console.print()
    if _check_command('op whoami'):
        success("1Password CLI signed in")
    else:
        warning("1Password CLI not signed in - run: op signin")
    
    # Check Vault auth
    console.print()
    if _check_command('vault token lookup'):
        success("Vault authenticated")
    else:
        warning("Vault not authenticated - run: vault login")


@task
def version(c):
    """Show versions of all CLI tools"""
    header("CLI Versions")
    
    for name, dep in CLI_DEPS.items():
        try:
            result = subprocess.run(
                shlex.split(dep['check']), capture_output=True, text=True, timeout=10
            )
            if result.returncode == 0:
                ver = result.stdout.strip().split('\n')[0]
                success(f"{name}: {ver}")
            else:
                warning(f"{name}: not installed")
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
            error(f"{name}: error", str(exc))


@task
def bootstrap(c):
    """Verify 1Password is ready for bootstrap (zero-frame init)
    
    Validates init/env_vars exists in 1Password Infra2 vault.
    No local .env file needed - libs/common.py reads directly from 1Password.
    
    Dependency chain:
    1. local.bootstrap (this) -> validates config
    2. 1password.setup -> deploys 1Password Connect
    3. vault.setup -> deploys Vault
    4. platform services -> uses Vault
    """
    from libs.env import OpSecrets
    
    header("Bootstrap Check", "Validating 1Password config")
    
    # Check op signin
    if not _check_command('op whoami'):
        error("1Password CLI not signed in")
        info("Run: op signin")
        return False
    
    op = OpSecrets()
    fields = op.get_all()
    init_item = OpSecrets.INIT_ITEM
    vault_name = OpSecrets.VAULT
    
    if not fields:
        error(f"Item '{init_item}' not found in vault '{vault_name}'")
        info("Create it with:")
        console.print(f'  op item create --category=login --title="{init_item}" --vault="{vault_name}" \\')
        console.print('    "VPS_HOST[text]=<ip>" "INTERNAL_DOMAIN[text]=<domain>"')
        return False
    
    # Validate required fields
    required_fields = ["VPS_HOST", "INTERNAL_DOMAIN"]
    missing = [k for k in required_fields if not fields.get(k)]
    
    if missing:
        error(f"Missing fields in {init_item}: {', '.join(missing)}")
        return False
    
    # Display config
    from rich.table import Table
    table = Table(show_header=True, title="✅ Config from 1Password")
    table.add_column("Key", style="cyan")
    table.add_column("Value")
    
    for k, v in fields.items():
        display = v if len(v) < 20 else f"{v[:15]}..."
        table.add_row(k, display)
    
    console.print(table)
    
    success("Bootstrap config validated!")
    info("No local .env needed - libs/common.py reads directly from 1Password")
    info("Next steps:")
    console.print("  1. invoke 1password.setup - Deploy 1Password Connect")
    console.print("  2. invoke vault.setup - Deploy Vault")
    
    return True


@task
def phase(c):
    """Detect current bootstrap phase and show status
    
    Phases:
    0 - Pure local (only op CLI)
    1 - Dokploy installed
    2 - 1Password Connect deployed
    3 - Vault deployed
    4 - Ready for platform
    
    Each phase reads from its respective 1Password item.
    """
    from rich.table import Table
    from libs.env import OpSecrets
    
    header("Bootstrap Phase Detection")
    
    def get_field(item_name: str, field_label: str) -> str:
        return OpSecrets(item=item_name).get(field_label) or ""
    
    init_vars = OpSecrets().get_all()
    init_item = OpSecrets.INIT_ITEM
    
    # Phase checks: (check_fn, phase_desc)
    # Phase 0: init vars
    phase_0 = bool(init_vars.get("VPS_HOST") and init_vars.get("INTERNAL_DOMAIN"))
    
    # Phase 2: 1Password Connect (credential)
    # Item: bootstrap/1password/VPS-01 Access Token: own_service
    # This is standard structure? 'bootstrap' project, '1password' service?
    # No, item name is custom.
    phase_2 = bool(get_field("bootstrap/1password/VPS-01 Access Token: own_service", "credential"))
    
    # Phase 3: Vault
    # Item: bootstrap/vault/Unseal Keys
    phase_3 = bool(get_field("bootstrap/vault/Unseal Keys", "Root Token"))
    
    table = Table(show_header=True)
    table.add_column("Phase", style="cyan")
    table.add_column("Status")
    table.add_column("Details")
    
    table.add_row("Phase 0: Local config", "[green]✅[/]" if phase_0 else "[dim]⏳[/]", f"{init_item}")
    table.add_row("Phase 2: 1Password Connect", "[green]✅[/]" if phase_2 else "[dim]⏳[/]", "Connect Token")
    table.add_row("Phase 3: Vault deployed", "[green]✅[/]" if phase_3 else "[dim]⏳[/]", "Root Token")
    
    console.print(table)
    
    # Logic
    current = 0
    if phase_0: current = 1
    if phase_2: current = 2
    if phase_3: current = 3
    
    console.print()
    if current >= 3:
        success(f"Current Phase: {current} - Ready for platform services!")
    else:
        info(f"Current Phase: {current}")
        next_steps = {
            0: "Run: invoke local.bootstrap",
            1: "Run: invoke 1password.setup",
            2: "Run: invoke vault.setup",
        }
        if current in next_steps:
            info(f"Next: {next_steps[current]}")
