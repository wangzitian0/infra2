"""
Shared console utilities using Rich
"""
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

console = Console()


def header(title: str, subtitle: str = None):
    """Display a task header"""
    text = f"[bold cyan]{title}[/]"
    if subtitle:
        text += f"\n[dim]{subtitle}[/]"
    console.print(Panel(text, border_style="cyan"))


def success(msg: str):
    console.print(f"[green]✅ {msg}[/]")


def error(msg: str, details: str = None):
    console.print(f"[red]❌ {msg}[/]")
    if details:
        console.print(f"[dim red]   {details}[/]")


def warning(msg: str):
    console.print(f"[yellow]⚠️  {msg}[/]")


def info(msg: str):
    console.print(f"[blue]ℹ️  {msg}[/]")


def prompt_action(title: str, instructions: list[str]):
    """Display an action prompt for manual steps"""
    table = Table(show_header=False, box=None, padding=(0, 2))
    for i, instruction in enumerate(instructions, 1):
        table.add_row(f"[yellow]{i}.[/]", instruction)
    console.print(Panel(table, title=f"[bold yellow]⏸️  {title}[/]", border_style="yellow"))
    console.input("[dim]Press Enter when complete...[/]")


def env_vars(title: str, vars: dict):
    """Display environment variables"""
    table = Table(show_header=False, box=None)
    for key, value in vars.items():
        table.add_row(f"[cyan]{key}[/]", f"[green]{value}[/]")
    console.print(Panel(table, title=f"[bold]📋 {title}[/]", border_style="green"))


def run_with_status(c, cmd: str, desc: str, hide: bool = True):
    """Run a command with status indicator"""
    with console.status(f"[cyan]{desc}...[/]"):
        result = c.run(cmd, warn=True, hide=hide)
    if result.ok:
        success(desc)
    else:
        error(desc, result.stderr if result.stderr else "Command failed")
    return result
