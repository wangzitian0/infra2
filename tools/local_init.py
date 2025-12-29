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
    import json
    
    header("Bootstrap Check", "Validating 1Password config")
    
    # Check op signin
    if not _check_command('op whoami'):
        error("1Password CLI not signed in")
        console.print("[yellow]Run: op signin[/]")
        return False
    
    # Read init/env_vars from 1Password
    OP_VAULT = "Infra2"
    OP_ITEM = "init/env_vars"
    
    try:
        result = subprocess.run(
            f'op item get "{OP_ITEM}" --vault="{OP_VAULT}" --format=json',
            shell=True, capture_output=True, text=True, check=True
        )
        item = json.loads(result.stdout)
        fields = {f["label"]: f.get("value", "") for f in item.get("fields", [])
                  if f.get("label") and f.get("value")}
    except subprocess.CalledProcessError:
        error(f"Item '{OP_ITEM}' not found in vault '{OP_VAULT}'")
        console.print("[yellow]Create it with:[/]")
        console.print('  op item create --category=login --title="init/env_vars" --vault="Infra2" \\')
        console.print('    "VPS_HOST[text]=<ip>" "INTERNAL_DOMAIN[text]=<domain>"')
        return False
    except json.JSONDecodeError:
        error("Failed to parse 1Password response")
        return False
    
    # Validate required fields
    required = ["VPS_HOST", "INTERNAL_DOMAIN"]
    missing = [k for k in required if k not in fields]
    
    if missing:
        error(f"Missing fields in {OP_ITEM}: {', '.join(missing)}")
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
    import json
    from rich.table import Table
    
    header("Bootstrap Phase Detection")
    
    OP_VAULT = "Infra2"
    
    def get_field(item_name: str, field_label: str) -> str:
        """Get field from 1Password item"""
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
        except:
            return ""
    
    # Phase checks: (item, field, phase_desc)
    phase_checks = [
        ("init/env_vars", "VPS_HOST", "Phase 0: Local config"),
        ("init/env_vars", "INTERNAL_DOMAIN", "Phase 0: Domain set"),
        ("bootstrap/1password/VPS-01 Access Token: own_service", "credential", "Phase 2: 1Password Connect"),
        ("bootstrap/vault/Unseal Keys", "Root Token", "Phase 3: Vault deployed"),
    ]
    
    table = Table(show_header=True)
    table.add_column("Phase", style="cyan")
    table.add_column("1Password Item")
    table.add_column("Field")
    table.add_column("Status")
    
    phase_num = 0
    for item_name, field_label, desc in phase_checks:
        value = get_field(item_name, field_label)
        if value:
            # Increment phase based on order
            if "Phase 0" in desc:
                phase_num = max(phase_num, 1)
            elif "Phase 2" in desc:
                phase_num = max(phase_num, 2)
            elif "Phase 3" in desc:
                phase_num = max(phase_num, 3)
            status = "[green]✅[/]"
        else:
            status = "[dim]⏳[/]"
        
        # Shorten item name for display
        short_item = item_name.split("/")[-1][:25]
        table.add_row(desc, short_item, field_label, status)
    
    console.print(table)
    
    # Current phase
    console.print()
    if phase_num >= 3:
        success(f"Current Phase: {phase_num} - Ready for platform services!")
    else:
        info(f"Current Phase: {phase_num}")
        next_steps = {
            0: "Run: invoke local.bootstrap",
            1: "Run: invoke 1password.setup",
            2: "Run: invoke vault.setup",
        }
        if phase_num in next_steps:
            console.print(f"[yellow]Next: {next_steps[phase_num]}[/]")
