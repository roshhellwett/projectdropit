"""Rich-styled menu-driven CLI for projectdropit."""
from __future__ import annotations

import os
import shlex
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from rich.align import Align
from rich.box import ROUNDED
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table
from rich.text import Text
from tqdm import tqdm

from . import __version__, updater
from .branding import APP_NAME, AUTHOR, COPYRIGHT, CREDIT, ORG, TAGLINE
from .config import Config
from .discovery import DiscoveryService, Peer, _primary_ipv4
from .transfer import (
    STATUS_ACTIVE,
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_REJECTED,
    IncomingTransfer,
    ReceiverServer,
    human_size,
    send_file,
)

console = Console()


# =============================================================================
# Argument parser
# =============================================================================

def _print_help() -> None:
    console.print(
        f"\n[bold cyan]{APP_NAME}[/bold cyan]  [dim]v{__version__}[/dim]\n"
        f"{TAGLINE}\n\n"
        "[bold]Usage:[/bold]\n"
        "  [cyan]projectdropit[/cyan]            launch the interactive menu\n"
        "  [cyan]projectdropit -start[/cyan]     same as above\n"
        "  [cyan]projectdropit --version[/cyan]  print version and exit\n"
        "  [cyan]projectdropit --no-update[/cyan]  skip update check on launch\n"
        "  [cyan]projectdropit -h[/cyan]         show this help\n"
    )


_RUN_ALIASES = {"start", "-start", "--start", "run", "-run", "--run"}
_HELP_ALIASES = {"-h", "--help", "help"}
_VER_ALIASES = {"-v", "--version", "version"}


def _parse_args(argv: Optional[List[str]] = None) -> dict:
    """Returns flags dict. Exits for help/version."""
    args = list(argv) if argv is not None else sys.argv[1:]
    flags = {"check_updates": True}
    i = 0
    while i < len(args):
        a = args[i].lower()
        if a in _RUN_ALIASES:
            pass
        elif a in _HELP_ALIASES:
            _print_help()
            sys.exit(0)
        elif a in _VER_ALIASES:
            print(f"{APP_NAME} {__version__}")
            sys.exit(0)
        elif a in ("--no-update", "-no-update"):
            flags["check_updates"] = False
        else:
            console.print(f"[red]Unknown argument:[/red] {args[i]}   (try [cyan]projectdropit -h[/cyan])")
            sys.exit(2)
        i += 1
    return flags


# =============================================================================
# Welcome + onboarding
# =============================================================================

def _welcome_panel() -> Panel:
    body = Text.from_markup(
        f"[bold cyan]{APP_NAME}[/bold cyan]  [dim]v{__version__}[/dim]\n"
        f"{TAGLINE}\n"
        f"[green]🔒 End-to-end encrypted[/green] · "
        f"[magenta]🛰  LAN auto-discovery[/magenta] · "
        f"[yellow]⚡ no setup[/yellow]\n"
        f"[dim]{CREDIT}[/dim]"
    )
    return Panel(Align.center(body), border_style="cyan", box=ROUNDED, padding=(1, 2))


def _onboard(cfg: Config) -> None:
    console.print()
    console.print(_welcome_panel())
    if cfg.has_device_name:
        return
    console.print(
        "\n[bold]Set a device name to continue.[/bold]\n"
        "[dim]Other devices on your LAN will see this name when discovering you.[/dim]"
    )
    while True:
        try:
            name = console.input("[bold cyan]›[/bold cyan] [bold]Device name:[/bold] ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[red]A device name is required. Exiting.[/red]")
            sys.exit(1)
        if not name:
            console.print("[red]A device name is required to continue.[/red]")
            continue
        if len(name) > 48:
            console.print("[red]Please keep the name under 48 characters.[/red]")
            continue
        cfg.device_name = name
        if not cfg.save():
            console.print("[yellow]⚠  Settings could not be saved to disk.[/yellow]")
        console.print(
            f"[green]✓ You will appear on the LAN as[/green] "
            f"[bold cyan]{cfg.device_name}[/bold cyan]"
        )
        return


def _firewall_warning() -> None:
    if os.name == "nt":
        console.print(
            "[yellow]Heads-up:[/yellow] Windows may show a one-time "
            "[bold]'Allow an app to communicate'[/bold] dialog. "
            "Click [bold]Allow[/bold] on private networks. "
            "[dim](no admin password needed)[/dim]"
        )
    else:
        console.print("[dim]Opening LAN socket… (no admin/root required)[/dim]")


# =============================================================================
# Auto-update
# =============================================================================

def _maybe_offer_update() -> None:
    """Wait for the background update check to finish, then offer if newer."""
    # Give the background thread up to 5 s to complete. This is called after
    # the 0.4 s startup sleep, so we have ~4.6 s of budget left.
    for _ in range(46):
        if updater.has_checked():
            break
        time.sleep(0.1)

    latest = updater.latest_if_newer()
    if not latest:
        return
    _show_update_banner(latest)
    try:
        if Confirm.ask("[bold]Update now?[/bold]", default=False):
            _run_pip_upgrade()
    except (EOFError, KeyboardInterrupt):
        console.print("\n[dim]Skipped update.[/dim]")


def _show_update_banner(latest: str) -> None:
    console.print()
    console.print(Panel(
        Text.from_markup(
            f"[bold green]Update available[/bold green]: "
            f"[red]{__version__}[/red] → [bold green]{latest}[/bold green]\n"
            f"[dim]Run:[/dim] [cyan]pip install -U projectdropit[/cyan]"
        ),
        border_style="green",
        box=ROUNDED,
        title="✨ projectdropit",
        title_align="left",
    ))


def _run_pip_upgrade() -> None:
    cmd = [sys.executable, "-m", "pip", "install", "-U", "projectdropit"]
    console.print(f"[dim]$ {' '.join(shlex.quote(c) for c in cmd)}[/dim]")
    try:
        result = subprocess.run(cmd, timeout=120)
        rc = result.returncode
    except subprocess.TimeoutExpired:
        console.print("[red]Update timed out after 2 minutes.[/red] Try manually:")
        console.print("[cyan]pip install -U projectdropit[/cyan]")
        return
    except Exception as e:
        console.print(f"[red]Update failed:[/red] {e}")
        return
    if rc == 0:
        console.print(
            "[green]✓ Updated successfully.[/green] "
            "Restart the app to use the new version: "
            "[cyan]projectdropit -start[/cyan]"
        )
        # Raise a dedicated exception so main()'s finally block can suppress
        # the generic "Shutting down…" message that would otherwise appear.
        raise _UpdatedAndExit()
    console.print(f"[red]Update exited with code {rc}.[/red] Try manually: [cyan]pip install -U projectdropit[/cyan]")


class _UpdatedAndExit(SystemExit):
    """Raised after a successful pip upgrade to exit cleanly without the
    generic 'Shutting down…' message."""
    def __init__(self) -> None:
        super().__init__(0)


# =============================================================================
# UI helpers
# =============================================================================

def _format_bps(bps: float) -> str:
    if bps <= 0:
        return "—"
    units = [("B/s", 1), ("KB/s", 1024), ("MB/s", 1024 ** 2), ("GB/s", 1024 ** 3), ("TB/s", 1024 ** 4)]
    label, scale = units[0]
    for lbl, sc in units:
        if bps >= sc:
            label, scale = lbl, sc
    return f"{bps / scale:.1f} {label}"


def _fmt_time(ts: Optional[float]) -> str:
    if not ts:
        return "—"
    return datetime.fromtimestamp(ts).strftime("%H:%M:%S")


def _status_pill(status: str) -> str:
    return {
        STATUS_PENDING: "[black on yellow] pending [/black on yellow]",
        STATUS_ACTIVE: "[white on cyan] receiving [/white on cyan]",
        STATUS_DONE: "[white on green] done [/white on green]",
        STATUS_REJECTED: "[white on red] rejected [/white on red]",
        STATUS_FAILED: "[white on red] failed [/white on red]",
    }.get(status, status)


def _peers_table(peers: List[Peer], self_name: str) -> Table:
    table = Table(
        box=ROUNDED,
        border_style="cyan",
        title=f"[bold]Peers on LAN[/bold]   [dim]you: [/dim][bold cyan]{self_name}[/bold cyan]",
        title_justify="left",
        expand=False,
    )
    table.add_column("#", justify="right", style="dim", width=3)
    table.add_column("Name", style="bold")
    table.add_column("Address", style="cyan")
    table.add_column("Port", justify="right", style="dim")
    if not peers:
        table.add_row("-", "[dim]scanning… no peers found yet[/dim]", "", "")
    for i, p in enumerate(peers, 1):
        table.add_row(str(i), p.name, p.address, str(p.port))
    return table


def _transfers_table(transfers: List[IncomingTransfer]) -> Table:
    table = Table(
        box=ROUNDED,
        border_style="magenta",
        title="[bold]Incoming Transfers[/bold]",
        title_justify="left",
        expand=False,
    )
    table.add_column("#", justify="right", width=3, style="dim")
    table.add_column("From", style="bold")
    table.add_column("File")
    table.add_column("Size", justify="right", style="cyan")
    table.add_column("Progress", justify="right")
    table.add_column("Speed", justify="right", style="dim")
    table.add_column("Status")
    table.add_column("Time", style="dim")
    if not transfers:
        table.add_row("-", "[dim]nothing yet[/dim]", "", "", "", "", "", "")
    for i, t in enumerate(transfers, 1):
        if t.size > 0:
            pct = min(100, int(t.received * 100 / t.size))
            prog = f"{pct}%"
        else:
            prog = "—"
        verified = " [green]✓[/green]" if t.verified else ""
        status = _status_pill(t.status) + (verified if t.status == STATUS_DONE else "")
        speed = _format_bps(t.speed_bps) if t.status in (STATUS_ACTIVE, STATUS_DONE) else ""
        table.add_row(
            str(i),
            t.sender_name,
            t.filename,
            human_size(t.size),
            prog,
            speed,
            status,
            _fmt_time(t.started_at),
        )
    return table


def _press_enter() -> None:
    try:
        console.input("\n[dim]Press Enter to return to the menu…[/dim]")
    except (EOFError, KeyboardInterrupt):
        pass


def _clean_path(raw: str) -> Path:
    s = raw.strip().strip('"').strip("'")
    return Path(s).expanduser()


# =============================================================================
# Main menu (header)
# =============================================================================

def _header_panel(cfg: Config, ip: Optional[str], port: int, pending_count: int) -> Panel:
    badge_pending = (
        f"  [black on yellow] {pending_count} pending [/black on yellow]"
        if pending_count else ""
    )
    body = Text.from_markup(
        f"[bold cyan]{cfg.device_name}[/bold cyan] [dim](you)[/dim]"
        f"  ·  [cyan]{ip or '—'}:{port}[/cyan]"
        f"  ·  [green]🔒 Encrypted[/green]{badge_pending}\n"
        f"[dim]download → {cfg.download_dir}[/dim]"
    )
    return Panel(
        body,
        title=f"[bold]{APP_NAME}[/bold] [dim]v{__version__}[/dim]",
        title_align="left",
        subtitle=f"[dim]{CREDIT}[/dim]",
        subtitle_align="right",
        border_style="cyan",
        box=ROUNDED,
        padding=(0, 2),
    )


def _menu_panel() -> Panel:
    body = Text.from_markup(
        "  [bold cyan]1[/bold cyan]  🛰   Discover Devices\n"
        "  [bold cyan]2[/bold cyan]  📤  Send a File\n"
        "  [bold cyan]3[/bold cyan]  📥  Incoming Transfers\n"
        "  [bold cyan]4[/bold cyan]  ⚙   Settings\n"
        "  [bold cyan]5[/bold cyan]  ⏻   Quit"
    )
    return Panel(body, border_style="cyan", box=ROUNDED, padding=(1, 2))


# =============================================================================
# Actions
# =============================================================================

def _action_discover(disc: DiscoveryService, cfg: Config) -> None:
    if os.name == "nt":
        hint = "press [bold]Ctrl+C[/bold] to return"
    else:
        hint = "press [bold]Ctrl+C[/bold] or [bold]q[/bold]+Enter to return"
    console.print(f"\n[dim]Live discovery — {hint}.[/dim]\n")
    stop = threading.Event()

    def _watcher() -> None:
        try:
            while not stop.is_set():
                ch = sys.stdin.readline()
                if ch and ch.strip().lower() in ("q", "quit", "exit"):
                    stop.set()
                    return
        except Exception:
            return

    # Note: stdin readline blocks on Windows, so we rely on Ctrl+C primarily.
    # The 'q' watcher is best-effort on POSIX terminals.
    if os.name != "nt":
        threading.Thread(target=_watcher, daemon=True).start()

    try:
        with Live(_peers_table(disc.peers(), cfg.device_name),
                  refresh_per_second=2, console=console) as live:
            while not stop.is_set():
                time.sleep(0.4)
                live.update(_peers_table(disc.peers(), cfg.device_name))
    except KeyboardInterrupt:
        console.print()
    finally:
        stop.set()


def _pick_peer(disc: DiscoveryService, cfg: Config) -> Optional[Peer]:
    # Brief active scan so the user sees results quickly
    deadline = time.time() + 3.0
    while time.time() < deadline and not disc.peers():
        time.sleep(0.3)
    peers = disc.peers()
    console.print()
    console.print(_peers_table(peers, cfg.device_name))
    if not peers:
        console.print(
            "[yellow]No peers found yet.[/yellow] "
            "[dim]Make sure the other device is running [/dim]"
            "[cyan]projectdropit -start[/cyan][dim] on the same Wi-Fi/LAN.[/dim]"
        )
        return None
    raw = Prompt.ask("\n[bold]Pick a peer[/bold] [#] [dim](blank to cancel)[/dim]", default="")
    if not raw.strip():
        return None
    try:
        idx = int(raw)
        if not (1 <= idx <= len(peers)):
            raise IndexError
        return peers[idx - 1]
    except (ValueError, IndexError):
        console.print("[red]Invalid selection.[/red]")
        return None


def _action_send(disc: DiscoveryService, cfg: Config) -> None:
    peer = _pick_peer(disc, cfg)
    if not peer:
        return
    raw_path = Prompt.ask("\n[bold]File path to send[/bold]")
    if not raw_path.strip():
        return
    path = _clean_path(raw_path)
    if not path.exists():
        console.print(f"[red]Not found:[/red] {path}")
        return
    if path.is_dir():
        console.print(f"[red]Directories aren't supported yet:[/red] {path}")
        return
    if not path.is_file():
        console.print(f"[red]Not a regular file:[/red] {path}")
        return
    size = path.stat().st_size

    console.print()
    console.print(Panel(
        Text.from_markup(
            f"[bold]{path.name}[/bold]  [dim]({human_size(size)})[/dim]\n"
            f"to [bold cyan]{peer.name}[/bold cyan] [dim]@ {peer.endpoint}[/dim]\n"
            f"[green]🔒 AES-256-GCM[/green] · [green]ECDH per session[/green]"
        ),
        title="📤 Sending",
        border_style="green",
        box=ROUNDED,
    ))

    # Defer the progress bar until the peer actually accepts and the first
    # chunk arrives. Otherwise users would see a stuck-at-0% bar while we are
    # still waiting for the prompt on the receiver's side.
    console.print("[dim]Waiting for peer to accept… (up to 2 minutes)[/dim]")
    bar_holder: List = [None]
    last = [0]

    def progress(sent: int, total: int) -> None:
        if bar_holder[0] is None:
            bar_holder[0] = tqdm(
                total=total, unit="B", unit_scale=True, unit_divisor=1024,
                desc=path.name[:24], leave=False, dynamic_ncols=True,
            )
        bar_holder[0].update(sent - last[0])
        last[0] = sent

    result_holder: dict = {}

    def runner() -> None:
        result_holder["r"] = send_file(
            peer.address, peer.port, path, cfg.device_name, progress=progress
        )

    th = threading.Thread(target=runner, daemon=True)
    th.start()
    try:
        while th.is_alive():
            th.join(timeout=0.25)
    except KeyboardInterrupt:
        if bar_holder[0] is not None:
            bar_holder[0].close()
        console.print("\n[yellow]Cancelled by user. Connection will time out on the peer.[/yellow]")
        return
    if bar_holder[0] is not None:
        bar_holder[0].close()

    res = result_holder.get("r") or {"ok": False, "reason": "unknown error"}
    if res.get("ok"):
        sha = (res.get("sha256") or "")[:12]
        console.print(
            f"[green]✓ Sent[/green] {human_size(res.get('bytes_sent', 0))}"
            + (f"  [dim]sha256:{sha}…[/dim]" if sha else "")
            + "  [green]🔒 verified[/green]"
        )
    else:
        reason = (res.get("reason") or "failed").lower()
        if "reject" in reason:
            console.print("[red]✗ Peer rejected the transfer.[/red]")
        elif "timeout" in reason or "timed out" in reason:
            console.print("[red]✗ Peer did not respond in time.[/red]")
        elif "connect failed" in reason or "refused" in reason:
            console.print(
                f"[red]✗ Couldn't connect to {peer.endpoint}.[/red] "
                "[dim]Peer may have left the network or its firewall blocked the connection.[/dim]"
            )
        else:
            console.print(f"[red]✗ Transfer failed:[/red] {res.get('reason')}")


def _action_incoming(rx: ReceiverServer) -> None:
    while True:
        transfers = rx.history()
        console.print()
        console.print(_transfers_table(transfers))
        pending = [t for t in transfers if t.status == STATUS_PENDING]
        if not pending:
            _press_enter()
            return
        console.print(
            "\n[bold]Pending offers:[/bold] "
            "[cyan]a <#>[/cyan] accept · [cyan]r <#>[/cyan] reject · "
            "[cyan]aa[/cyan] accept all · [cyan]rr[/cyan] reject all · "
            "[dim]blank to return[/dim]"
        )
        cmd = Prompt.ask("›", default="").strip().lower()
        if not cmd:
            return
        if cmd == "aa":
            for t in pending:
                rx.decide(t.id, True)
            console.print("[green]All pending offers accepted.[/green]")
            continue
        if cmd == "rr":
            for t in pending:
                rx.decide(t.id, False)
            console.print("[red]All pending offers rejected.[/red]")
            continue
        parts = cmd.split()
        if len(parts) != 2 or parts[0] not in ("a", "r"):
            console.print("[red]Use 'a <#>' or 'r <#>' (or aa/rr).[/red]")
            continue
        try:
            idx = int(parts[1]) - 1
            t = transfers[idx]
        except (ValueError, IndexError):
            console.print("[red]Invalid number.[/red]")
            continue
        if t.status != STATUS_PENDING:
            console.print("[yellow]That transfer is no longer pending.[/yellow]")
            continue
        accept = parts[0] == "a"
        rx.decide(t.id, accept)
        if accept:
            console.print(
                f"[green]✓ Accepted.[/green] Saving to "
                f"[bold]{Path(rx.download_dir()) / t.filename}[/bold]  "
                "[green]🔒 Encrypted[/green]"
            )
        else:
            console.print("[red]✗ Rejected.[/red]")


def _action_settings(cfg: Config, disc: DiscoveryService, ip: Optional[str], port: int) -> None:
    while True:
        console.print()
        console.print(Panel(
            Text.from_markup(
                f"[bold]Device name      [/bold] {cfg.device_name}\n"
                f"[bold]LAN address      [/bold] {ip or '—'}:{port}\n"
                f"[bold]Download folder  [/bold] {cfg.download_dir}\n"
                f"[bold]Encryption       [/bold] [green]ECDH (X25519) + AES-256-GCM[/green]\n"
                f"[bold]Version          [/bold] {__version__}"
            ),
            title="⚙  Settings",
            title_align="left",
            border_style="cyan",
            box=ROUNDED,
        ))
        console.print(
            "  [bold cyan]1[/bold cyan]  Change device name\n"
            "  [bold cyan]2[/bold cyan]  Change download folder\n"
            "  [bold cyan]3[/bold cyan]  Check for updates\n"
            "  [bold cyan]4[/bold cyan]  About\n"
            "  [bold cyan]5[/bold cyan]  Back"
        )
        choice = Prompt.ask("›", choices=["1", "2", "3", "4", "5"], default="5")
        if choice == "1":
            new_name = Prompt.ask("New device name", default=cfg.device_name).strip()
            if new_name and new_name != cfg.device_name:
                cfg.device_name = new_name
                if not cfg.save():
                    console.print("[yellow]⚠  Settings could not be saved to disk.[/yellow]")
                disc.update_device_name(cfg.device_name)
                console.print(
                    f"[green]✓ Device name updated to[/green] [bold]{cfg.device_name}[/bold]."
                )
        elif choice == "2":
            new_dir = Prompt.ask("New download folder", default=str(cfg.download_dir)).strip()
            if new_dir:
                try:
                    cfg.download_dir = new_dir
                    cfg.ensure_download_dir()
                    if not cfg.save():
                        console.print("[yellow]⚠  Settings could not be saved to disk.[/yellow]")
                    console.print(
                        f"[green]✓ Download folder set to[/green] [bold]{cfg.download_dir}[/bold]."
                    )
                except Exception as e:
                    console.print(f"[red]Could not set folder:[/red] {e}")
        elif choice == "3":
            console.print("[dim]Checking PyPI for updates…[/dim]")
            try:
                latest = updater.check_sync()
            except Exception as e:
                console.print(f"[red]Update check failed:[/red] {e}")
                latest = None
            if latest:
                _show_update_banner(latest)
                try:
                    if Confirm.ask("Update now?", default=False):
                        _run_pip_upgrade()
                except (EOFError, KeyboardInterrupt):
                    console.print("\n[dim]Skipped update.[/dim]")
            else:
                err = updater.last_error()
                if err:
                    console.print(
                        f"[yellow]Could not reach PyPI[/yellow] [dim]({err})[/dim]\n"
                        "[dim]Check your internet connection and try again.[/dim]"
                    )
                else:
                    console.print(
                        f"[green]✓ You're on the latest version[/green] "
                        f"[dim](v{__version__})[/dim]."
                    )
        elif choice == "4":
            console.print(Panel(
                Text.from_markup(
                    f"[bold cyan]{APP_NAME}[/bold cyan]  v{__version__}\n"
                    f"{TAGLINE}\n\n"
                    f"[dim]Author:[/dim] {AUTHOR}\n"
                    f"[dim]Org:[/dim] {ORG}\n"
                    f"[dim]License:[/dim] MIT\n"
                    f"[dim]{COPYRIGHT}[/dim]"
                ),
                title="ℹ  About",
                border_style="cyan",
                box=ROUNDED,
            ))
            _press_enter()
        else:
            return


# =============================================================================
# Main loop
# =============================================================================

def main() -> None:
    flags = _parse_args()
    cfg = Config.load()

    # Surface any config corruption warning before onboarding.
    if cfg.load_warning:
        console.print(f"[yellow]⚠  {cfg.load_warning}[/yellow]")

    _onboard(cfg)
    cfg.ensure_download_dir()

    if flags["check_updates"]:
        updater.check_async()

    rx = ReceiverServer(
        get_download_dir=lambda: cfg.ensure_download_dir(),
        get_device_name=lambda: cfg.device_name,
    )
    console.print()
    _firewall_warning()
    rx.start()

    disc = DiscoveryService(cfg.device_name, rx.port)
    disc.start()
    ip = disc.local_ip or _primary_ipv4()

    # Friendly LAN diagnostics so users aren't confused when discovery is silent.
    if ip is None:
        console.print(
            "[yellow]⚠  No LAN IPv4 address detected.[/yellow] "
            "[dim]Connect to Wi-Fi/Ethernet to be visible to other devices.[/dim]"
        )
    if not disc.registered:
        console.print(
            f"[yellow]⚠  mDNS service registration failed[/yellow] "
            f"[dim]({disc.last_error or 'unknown'}).[/dim] "
            "[dim]You may not appear in others' peer lists, but you can still receive incoming connections if reachable.[/dim]"
        )

    update_offered = False
    _seen_done: set = set()
    _seen_done_lock = threading.Lock()

    def on_offer(t: IncomingTransfer) -> None:
        # Visible nudge — does not block the menu input.
        console.print(
            f"\n[bold yellow]► Incoming:[/bold yellow] "
            f"[bold]{t.sender_name}[/bold] wants to send "
            f"[bold]{t.filename}[/bold] [dim]({human_size(t.size)})[/dim]. "
            f"Open [cyan]Incoming Transfers[/cyan] to accept or reject."
        )

    def on_update(t: IncomingTransfer) -> None:
        # Surface a one-time visible line when a transfer completes / fails.
        # Guard with a lock since on_update is called from receiver threads.
        with _seen_done_lock:
            if t.id in _seen_done:
                return
            if t.status in (STATUS_DONE, STATUS_FAILED):
                _seen_done.add(t.id)
            else:
                return
        if t.status == STATUS_DONE:
            verified = " [green]✓ verified[/green]" if t.verified else ""
            console.print(
                f"\n[bold green]✓ Received[/bold green] "
                f"[bold]{t.filename}[/bold] [dim]({human_size(t.size)})[/dim] "
                f"from [bold cyan]{t.sender_name}[/bold cyan] → "
                f"[bold]{t.save_path}[/bold]{verified}"
            )
        elif t.status == STATUS_FAILED:
            console.print(
                f"\n[bold red]✗ Receive failed[/bold red] "
                f"[bold]{t.filename}[/bold] from [bold]{t.sender_name}[/bold]: "
                f"{t.error or 'unknown error'}"
            )

    rx.on_offer = on_offer
    rx.on_update = on_update

    try:
        # small grace period for the update check to finish
        time.sleep(0.4)
        if flags["check_updates"] and not update_offered:
            _maybe_offer_update()
            update_offered = True

        while True:
            console.print()
            console.print(_header_panel(cfg, ip, rx.port, len(rx.pending())))
            console.print(_menu_panel())
            try:
                choice = Prompt.ask(
                    "[bold cyan]›[/bold cyan]",
                    choices=["1", "2", "3", "4", "5"],
                    default="1",
                )
            except (EOFError, KeyboardInterrupt):
                break
            if choice == "1":
                _action_discover(disc, cfg)
            elif choice == "2":
                _action_send(disc, cfg)
            elif choice == "3":
                _action_incoming(rx)
            elif choice == "4":
                _action_settings(cfg, disc, ip, rx.port)
            elif choice == "5":
                if Confirm.ask("Quit projectdropit?", default=True):
                    break
    except KeyboardInterrupt:
        console.print()
    except _UpdatedAndExit:
        # Successful pip upgrade — skip the generic shutdown message.
        try:
            disc.stop()
        except Exception:
            pass
        try:
            rx.stop()
        except Exception:
            pass
        return
    finally:
        console.print("\n[dim]Shutting down…[/dim]")
        try:
            disc.stop()
        except Exception:
            pass
        try:
            rx.stop()
        except Exception:
            pass
        console.print(f"[dim]{COPYRIGHT}[/dim]")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # pragma: no cover
        console.print(f"[red]Fatal:[/red] {e}")
        sys.exit(1)
