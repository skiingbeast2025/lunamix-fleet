#!/usr/bin/env python3
"""
==============================================================================
HBU2 24/7 Simulator Controller & Interactive TUI (service.py)
==============================================================================
Central management interface providing:
  - Interactive TUI settings, controller, and live progress monitor
  - Start Program Now (completely detached background session - safe to close terminal)
  - One-key Startup on Boot Toggle ([T]) backed by systemd user service
  - Customizable Bot Fleet (N or ALL accounts)
  - Maximum Anti-Mute Safe Speed (5.1s staggered pacing)
  - Live Progress Monitor ([P]) and Web Dashboard integration
==============================================================================
"""

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import webbrowser
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "hbu_service_config.json")
LOGS_DIR = os.path.join(BASE_DIR, "logs")
PID_FILE = os.path.join(LOGS_DIR, "daemon.pid")
LOG_FILE = os.path.join(LOGS_DIR, "simulator.log")
STATE_FILE = os.path.join(LOGS_DIR, "telemetry_state.json")
PAUSE_TRIGGER_FILE = os.path.join(LOGS_DIR, ".pause_toggle")
SYSTEMD_USER_DIR = os.path.expanduser("~/.config/systemd/user")
SYSTEMD_SERVICE_FILE = os.path.join(SYSTEMD_USER_DIR, "hbu-simulator.service")

# ==============================================================================
# Styling
# ==============================================================================

class Color:
    RESET   = "\033[0m"
    BOLD    = "\033[1m"
    DIM     = "\033[2m"
    RED     = "\033[91m"
    GREEN   = "\033[92m"
    YELLOW  = "\033[93m"
    BLUE    = "\033[94m"
    MAGENTA = "\033[95m"
    CYAN    = "\033[96m"
    WHITE   = "\033[97m"

def clear_screen():
    os.system("cls" if os.name == "nt" else "clear")

# ==============================================================================
# Config Management
# ==============================================================================

def load_config():
    default = {
        "supabase_rest_url": "https://xvkjvuxhawlljakfkllu.supabase.co/rest/v1",
        "supabase_functions_url": "https://xvkjvuxhawlljakfkllu.supabase.co/functions/v1",
        "supabase_anon_key": "sb_publishable_uyxd5lS77wdXpD56QF_lgg_zaujLjZZ",
        "bot_count": "all",
        "min_delay_per_bot": 5.1,
        "max_delay_per_bot": 5.25,
        "autostart_on_boot": False,
        "web_dashboard_port": 8088,
        "web_dashboard_enabled": True,
        "csv_file": "simulated_accounts.csv",
        "banned_csv_file": "banned_accounts.csv",
        "paused": False
    }
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
                default.update(loaded)
        except Exception:
            pass
    return default

def save_config(cfg):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except Exception as e:
        print(f"{Color.RED}[!] Error saving config: {e}{Color.RESET}")

# ==============================================================================
# Daemon Process Inspection
# ==============================================================================

def get_daemon_pid():
    if os.path.exists(PID_FILE):
        try:
            with open(PID_FILE, "r") as f:
                pid = int(f.read().strip())
            # Check if alive
            os.kill(pid, 0)
            return pid
        except (ValueError, OSError):
            return None
    return None

def get_telemetry():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "status": "STOPPED",
        "uptime_str": "00:00:00",
        "active_bots": 0,
        "messages_sent": 0,
        "messages_failed": 0,
        "messages_per_minute": 0.0,
        "banned_count": 0,
        "last_latency_ms": 0,
        "paused": False,
        "bot_states": [],
        "recent_messages": []
    }

# ==============================================================================
# Systemd Autostart Integration
# ==============================================================================

SYSTEMD_UNIT_CONTENT = f"""[Unit]
Description=HBU2 Simulation 24/7 Background Service
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory={BASE_DIR}
ExecStart={sys.executable} {os.path.join(BASE_DIR, 'daemon_hbu_sim.py')}
Restart=always
RestartSec=5
MemoryMax=250M
CPUQuota=50%
StandardOutput=append:{LOG_FILE}
StandardError=append:{LOG_FILE}

[Install]
WantedBy=default.target
"""

def install_systemd_unit():
    """Installs the systemd user service file if not already installed."""
    try:
        os.makedirs(SYSTEMD_USER_DIR, exist_ok=True)
        with open(SYSTEMD_SERVICE_FILE, "w") as f:
            f.write(SYSTEMD_UNIT_CONTENT)
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception as e:
        print(f"[!] Warning: Could not create systemd unit: {e}")
        return False

def is_systemd_enabled():
    """Checks if autostart service is enabled."""
    if not os.path.exists(SYSTEMD_SERVICE_FILE):
        return False
    try:
        res = subprocess.run(
            ["systemctl", "--user", "is-enabled", "hbu-simulator"],
            capture_output=True, text=True, check=False
        )
        return "enabled" in res.stdout.strip().lower()
    except Exception:
        cfg = load_config()
        return cfg.get("autostart_on_boot", False)

def toggle_autostart():
    """Toggles startup on boot state."""
    install_systemd_unit()
    currently_enabled = is_systemd_enabled()
    new_state = not currently_enabled

    try:
        if new_state:
            subprocess.run(["systemctl", "--user", "enable", "hbu-simulator"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            # Try enabling user lingering so it runs even before login
            user = os.environ.get("USER", "")
            if user:
                subprocess.run(["loginctl", "enable-linger", user], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            subprocess.run(["systemctl", "--user", "disable", "hbu-simulator"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass

    cfg = load_config()
    cfg["autostart_on_boot"] = new_state
    save_config(cfg)
    return new_state

# ==============================================================================
# Service Lifecycle Controls
# ==============================================================================

def start_service(mock=False):
    """
    Starts the daemon in a completely detached background session.
    Safe to close the terminal immediately after starting!
    """
    pid = get_daemon_pid()
    if pid:
        return False, f"Service is already running (PID: {pid})"

    os.makedirs(LOGS_DIR, exist_ok=True)

    # If systemd service is installed and enabled, manage via systemctl
    if not mock and is_systemd_enabled():
        try:
            subprocess.run(["systemctl", "--user", "start", "hbu-simulator"], capture_output=True, check=False)
            time.sleep(1.0)
            new_pid = get_daemon_pid()
            if new_pid:
                return True, f"Service started via systemd (PID: {new_pid}). Safe to close terminal!"
        except Exception:
            pass

    daemon_script = os.path.join(BASE_DIR, "daemon_hbu_sim.py")

    cmd = [sys.executable, daemon_script]
    if mock:
        cmd.append("--mock")

    try:
        # Start completely detached from this terminal session (start_new_session=True)
        # Redirect standard I/O so SIGHUP and terminal closing never affect it
        with open(os.devnull, "r") as devnull, open(LOG_FILE, "a") as logfile:
            proc = subprocess.Popen(
                cmd,
                stdin=devnull,
                stdout=logfile,
                stderr=logfile,
                start_new_session=True,
                cwd=BASE_DIR
            )
        time.sleep(0.8)
        new_pid = get_daemon_pid()
        if new_pid:
            return True, f"Service started in background (PID: {new_pid}). Safe to close terminal!"
        return True, f"Service launched (PID: {proc.pid}). Safe to close terminal!"
    except Exception as e:
        return False, f"Failed to start service: {e}"

def stop_service():
    """Stops the daemon gracefully."""
    # If managed via systemd user unit, stop it there so Restart=always doesn't trigger
    try:
        subprocess.run(["systemctl", "--user", "stop", "hbu-simulator"], capture_output=True, check=False)
    except Exception:
        pass

    pid = get_daemon_pid()
    if not pid:
        if os.path.exists(PID_FILE):
            os.remove(PID_FILE)
        return True, "Service stopped successfully."

    try:
        os.kill(pid, signal.SIGTERM)
        for _ in range(15):
            time.sleep(0.2)
            if not get_daemon_pid():
                break
        else:
            os.kill(pid, signal.SIGKILL)

        if os.path.exists(PID_FILE):
            os.remove(PID_FILE)
        return True, "Service stopped successfully."
    except Exception as e:
        return False, f"Could not stop service: {e}"

def pause_service(force_state=None):
    """Sets or toggles pause/resume state. force_state: True (pause), False (resume), None (toggle)."""
    pid = get_daemon_pid()
    if not pid:
        return False, "Service is not running."

    try:
        if force_state is True:
            trigger_val = "1"
            status = "PAUSED"
        elif force_state is False:
            trigger_val = "0"
            status = "RUNNING"
        else:
            telem = get_telemetry()
            status = "RUNNING" if telem.get("paused", False) else "PAUSED"
            trigger_val = "0" if status == "RUNNING" else "1"

        with open(PAUSE_TRIGGER_FILE, "w") as f:
            f.write(trigger_val)
        time.sleep(0.3)
        return True, f"Service is now {status}."
    except Exception as e:
        return False, f"Could not change pause state: {e}"

# ==============================================================================
# Live Progress Monitor Screen (Option P)
# ==============================================================================

def progress_screen():
    """Full-screen live telemetry and message feed."""
    while True:
        clear_screen()
        pid = get_daemon_pid()
        telem = get_telemetry()

        status_color = Color.GREEN if pid and not telem.get("paused") else (Color.YELLOW if pid else Color.RED)
        status_label = "● RUNNING" if pid and not telem.get("paused") else ("⏸ PAUSED" if pid else "○ STOPPED")

        sent = telem.get("messages_sent", 0)
        failed = telem.get("messages_failed", 0)
        total_ops = sent + failed
        rate_pct = f"{(sent / total_ops * 100):.1f}%" if total_ops > 0 else "100.0%"
        msgs_min = telem.get("messages_per_minute", 0.0)
        latency = telem.get("last_latency_ms", 0)
        uptime = telem.get("uptime_str", "00:00:00")
        active_bots = telem.get("active_bots", 0)
        mutes = telem.get("banned_count", 0)

        print(f"{Color.CYAN}{Color.BOLD}╔══════════════════════════════════════════════════════════════════════╗")
        print(f"║                      LIVE SCRIPT PROGRESS MONITOR                    ║")
        print(f"╚══════════════════════════════════════════════════════════════════════╝{Color.RESET}")
        print(f" State      : {status_color}{Color.BOLD}{status_label}{Color.RESET} (PID: {pid or 'None'})  |  Uptime: {uptime}")
        print(f" Throughput : {Color.BOLD}{msgs_min:.1f}{Color.RESET} msgs/min  |  Ping Latency: {latency}ms")
        print(f" Totals     : {Color.GREEN}{sent:,}{Color.RESET} sent  |  {rate_pct} success  |  {failed} fails  |  {mutes} mutes")
        print(f" Fleet Size : {Color.BOLD}{active_bots}{Color.RESET} parallel bots active")
        print("─" * 70)

        # Bot table sample
        bots = telem.get("bot_states", [])
        print(f"{Color.BOLD}ACTIVE BOT FLEET ({len(bots)} registered):{Color.RESET}")
        if bots:
            for b in bots[:8]:
                b_status = f"{Color.GREEN}OK{Color.RESET}" if b.get("status") == "OK" else f"{Color.RED}{b.get('status')}{Color.RESET}"
                print(f"  • {Color.CYAN}{b.get('bot'):<22}{Color.RESET}: {b.get('sent', 0):>4} msgs | {b.get('last_seen', 'just now')} [{b_status}]")
            if len(bots) > 8:
                print(f"    {Color.DIM}... and {len(bots) - 8} more bots running concurrently{Color.RESET}")
        else:
            print(f"  {Color.DIM}(No active bot data recorded yet){Color.RESET}")

        print("─" * 70)
        print(f"{Color.BOLD}RECENT CHAT MESSAGES:{Color.RESET}")
        recent = telem.get("recent_messages", [])
        if recent:
            for m in recent[-6:]:
                t = m.get("time", "")
                b = m.get("bot", "")
                txt = m.get("msg", "")
                print(f"  {Color.DIM}[{t}]{Color.RESET} <{Color.GREEN}{b}{Color.RESET}> \"{txt}\"")
        else:
            print(f"  {Color.DIM}(Waiting for messages...){Color.RESET}")

        print("─" * 70)
        print(f"{Color.YELLOW}Hotkeys: [P] Pause/Resume  |  [R] Force Refresh  |  [Q] Return to Menu{Color.RESET}")

        # Non-blocking input check
        try:
            import select
            rlist, _, _ = select.select([sys.stdin], [], [], 2.0)
            if rlist:
                key = sys.stdin.readline().strip().lower()
                if key in ("q", "quit", "exit", "b", "back"):
                    break
                elif key == "p":
                    pause_service()
                elif key == "r":
                    continue
        except Exception:
            time.sleep(2.0)

# ==============================================================================
# Interactive TUI Menu
# ==============================================================================

def main_tui():
    """Main interactive control center powered by the curses TUI."""
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print(
            f"{Color.YELLOW}[!] Interactive TUI requires a TTY terminal. "
            f"Use CLI subcommands (e.g. './service.sh status' or './service.sh --help').{Color.RESET}"
        )
        return

    try:
        import curses
        from tui_views import launch_tui

        launch_tui(sys.modules[__name__], start_tab=0)
    except Exception as exc:
        err_name = type(exc).__name__
        print(
            f"{Color.RED}[!] Failed to launch interactive TUI ({err_name}): {exc}{Color.RESET}\n"
            f"{Color.DIM}Fallback: run './service.sh status' or './service.sh --help' for CLI controls.{Color.RESET}"
        )

# ==============================================================================
# CLI Entry Point
# ==============================================================================

def main():
    parser = argparse.ArgumentParser(description="HBU2 24/7 Simulator Controller")
    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    subparsers.add_parser("start", help="Start service in detached background")
    subparsers.add_parser("stop", help="Stop background service")
    subparsers.add_parser("restart", help="Restart background service")
    subparsers.add_parser("pause", help="Toggle pause/resume state")
    subparsers.add_parser("resume", help="Resume sending messages")
    subparsers.add_parser("status", help="Print current status and metrics")
    subparsers.add_parser("progress", help="Open live terminal progress monitor")

    p_auto = subparsers.add_parser("autostart", help="Manage system startup")
    p_auto.add_argument("action", choices=["on", "off", "toggle", "status"], default="status", nargs="?")

    p_bots = subparsers.add_parser("bots", help="Set bot count")
    p_bots.add_argument("count", help="Number of bots or 'all'")

    p_create = subparsers.add_parser("create", help="Batch create accounts via proxy rotator")
    p_create.add_argument("count", type=int, default=10, nargs="?", help="Number of accounts to create (default: 10)")
    p_create.add_argument("--threads", "-c", type=int, default=1, help="Worker threads (default: 1)")
    p_create.add_argument("--direct", action="store_true", help="Force direct connection")

    p_sync = subparsers.add_parser("sync", help="Sync all project files and code edits to GitHub")
    p_sync.add_argument("message", nargs="?", default="feat: update lunamix fleet codebase and tools", help="Git commit message")

    args = parser.parse_args()

    if not args.command:
        # Launch Interactive TUI by default
        main_tui()
        return

    if args.command == "start":
        ok, msg = start_service()
        print(f"{'[✓]' if ok else '[!]'} {msg}")
    elif args.command == "stop":
        ok, msg = stop_service()
        print(f"{'[✓]' if ok else '[!]'} {msg}")
    elif args.command == "restart":
        stop_service()
        time.sleep(1.0)
        ok, msg = start_service()
        print(f"{'[✓]' if ok else '[!]'} {msg}")
    elif args.command == "pause":
        ok, msg = pause_service(force_state=True)
        print(f"[i] {msg}")
    elif args.command == "resume":
        ok, msg = pause_service(force_state=False)
        print(f"[i] {msg}")
    elif args.command == "status":
        pid = get_daemon_pid()
        telem = get_telemetry()
        autostart = is_systemd_enabled()
        status = "RUNNING" if pid and not telem.get("paused") else ("PAUSED" if pid else "STOPPED")
        print(f"Status       : {status} (PID: {pid or 'None'})")
        print(f"Autostart    : {'ENABLED' if autostart else 'DISABLED'}")
        print(f"Messages Sent: {telem.get('messages_sent', 0):,}")
        print(f"Speed        : {telem.get('messages_per_minute', 0.0):.1f} msgs/min")
        print(f"Uptime       : {telem.get('uptime_str', '00:00:00')}")
        print(f"Active Bots  : {telem.get('active_bots', 0)}")
    elif args.command == "progress":
        if sys.stdin.isatty() and sys.stdout.isatty():
            try:
                from tui_views import launch_tui
                launch_tui(sys.modules[__name__], start_tab=1)
                return
            except Exception:
                pass
        progress_screen()
    elif args.command == "autostart":
        if args.action == "on":
            if not is_systemd_enabled():
                toggle_autostart()
            print("Autostart on boot: ENABLED")
        elif args.action == "off":
            if is_systemd_enabled():
                toggle_autostart()
            print("Autostart on boot: DISABLED")
        elif args.action == "toggle":
            st = toggle_autostart()
            print(f"Autostart on boot: {'ENABLED' if st else 'DISABLED'}")
        else:
            st = is_systemd_enabled()
            print(f"Autostart on boot: {'ENABLED' if st else 'DISABLED'}")
    elif args.command == "bots":
        cfg = load_config()
        if args.count.lower() in ("a", "all"):
            cfg["bot_count"] = "all"
            print("Bot count set to: ALL")
        else:
            try:
                cfg["bot_count"] = int(args.count)
                print(f"Bot count set to: {cfg['bot_count']}")
            except ValueError:
                print(f"[!] Invalid count: {args.count}")
                return
        save_config(cfg)
    elif args.command == "create":
        cmd = [sys.executable, os.path.join(BASE_DIR, "create_hbu_accounts.py"), "--count", str(args.count)]
        if args.threads:
            cmd.extend(["--threads", str(args.threads)])
        if args.direct:
            cmd.append("--direct")
        subprocess.run(cmd)
    elif args.command == "sync":
        cmd = [sys.executable, os.path.join(BASE_DIR, "sync_to_github.py"), args.message]
        subprocess.run(cmd)

if __name__ == "__main__":
    main()
