"""Local environment initialization and CLI dependency check

Usage:
    invoke local.init       # Check and install all dependencies
    invoke local.check      # Check dependencies only
"""
from __future__ import annotations
from invoke import task
import subprocess
import shutil
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
            cmd, shell=True, capture_output=True, text=True, timeout=10
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


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
    
    all_ok = True
    results = []
    
    for name, dep in CLI_DEPS.items():
        if _check_command(dep['check']):
            results.append((name, True, None))
        else:
            results.append((name, False, dep.get('docs', '')))
            all_ok = False
    
    # Display results
    from rich.table import Table
    table = Table(show_header=True)
    table.add_column("CLI", style="cyan")
    table.add_column("Status")
    table.add_column("Docs")
    
    for name, ok, docs in results:
        status = "[green]✅ Installed[/]" if ok else "[red]❌ Missing[/]"
        table.add_row(name, status, docs or "")
    
    console.print(table)
    
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
    if not check(c):
        console.print()
        info("Installation instructions:")
        platform = _get_platform()
        
        for name, dep in CLI_DEPS.items():
            if not _check_command(dep['check']):
                console.print(f"\n[bold]{name}[/]:")
                if platform == 'mac' and 'install_mac' in dep:
                    console.print(f"  [cyan]{dep['install_mac']}[/]")
                elif platform == 'linux' and 'install_linux' in dep:
                    console.print(f"  [cyan]{dep['install_linux']}[/]")
                elif 'install' in dep:
                    console.print(f"  [cyan]{dep['install']}[/]")
                console.print(f"  [dim]Docs: {dep['docs']}[/]")
    
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
                dep['check'], shell=True, capture_output=True, text=True, timeout=10
            )
            if result.returncode == 0:
                ver = result.stdout.strip().split('\n')[0]
                console.print(f"[cyan]{name}[/]: {ver}")
            else:
                console.print(f"[cyan]{name}[/]: [red]not installed[/]")
        except:
            console.print(f"[cyan]{name}[/]: [red]error[/]")


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
    from libs.env import EnvManager, OP_VAULT, INIT_ITEM, REQUIRED_INIT_FIELDS
    
    header("Bootstrap Check", "Validating 1Password config")
    
    # Check op signin
    if not _check_command('op whoami'):
        error("1Password CLI not signed in")
        console.print("[yellow]Run: op signin[/]")
        return False
    
    # Read init/env_vars using EnvManager
    mgr = EnvManager('init')
    fields = mgr.get_all_env(level='service')
    
    if not fields:
        error(f"Item '{INIT_ITEM}' not found in vault '{OP_VAULT}'")
        console.print("[yellow]Create it with:[/]")
        console.print(f'  op item create --category=login --title="{INIT_ITEM}" --vault="{OP_VAULT}" \\')
        console.print('    "VPS_HOST[text]=<ip>" "INTERNAL_DOMAIN[text]=<domain>"')
        return False
    
    # Validate required fields
    missing = [k for k in REQUIRED_INIT_FIELDS if k not in fields]
    
    if missing:
        error(f"Missing fields in {INIT_ITEM}: {', '.join(missing)}")
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
    console.print("  1. [cyan]invoke 1password.setup[/] - Deploy 1Password Connect")
    console.print("  2. [cyan]invoke vault.setup[/] - Deploy Vault")
    
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
    from libs.env import EnvManager, INIT_ITEM, OP_VAULT
    import json
    
    header("Bootstrap Phase Detection")
    
    def get_field(item_name: str, field_label: str) -> str:
        """Get field from 1Password item via CLI (EnvManager doesn't support arbitrary items easily yet)"""
        # TODO: Extend EnvManager to support arbitrary items or use it if items match standard naming
        # specific items are tricky, sticking to op cli for specific non-standard items
        try:
            result = subprocess.run(
                f'op item get "{item_name}" --vault="{OP_VAULT}" --format=json',
                shell=True, capture_output=True, text=True, check=True
            )
            item = json.loads(result.stdout)
            for f in item.get("fields", []):
                if f.get("label") == field_label:
                    return f.get("value", "")
            return ""
        except (subprocess.CalledProcessError, json.JSONDecodeError, KeyError):
            return ""
    
    # EnvManager can read INIT_ITEM easily
    mgr_init = EnvManager('init')
    init_vars = mgr_init.get_all_env('service')
    
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
    
    table.add_row("Phase 0: Local config", "[green]✅[/]" if phase_0 else "[dim]⏳[/]", f"{INIT_ITEM}")
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
            console.print(f"[yellow]Next: {next_steps[current]}[/]")
