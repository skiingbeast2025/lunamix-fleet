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
    """Main interactive control center."""
    while True:
        clear_screen()
        cfg = load_config()
        pid = get_daemon_pid()
        telem = get_telemetry()
        autostart_active = is_systemd_enabled()

        if pid:
            if telem.get("paused"):
                status_str = f"{Color.YELLOW}{Color.BOLD}⏸ PAUSED{Color.RESET} (PID: {pid})"
            else:
                status_str = f"{Color.GREEN}{Color.BOLD}● RUNNING{Color.RESET} (PID: {pid})"
        else:
            status_str = f"{Color.RED}{Color.BOLD}○ STOPPED{Color.RESET}"

        autostart_str = (
            f"{Color.GREEN}{Color.BOLD}[● ENABLED]{Color.RESET} - Starts automatically on system boot"
            if autostart_active
            else f"{Color.DIM}[○ DISABLED] - Manual start only{Color.RESET}"
        )

        bot_count_cfg = cfg.get("bot_count", "all")
        bot_str = "ALL" if str(bot_count_cfg).lower() == "all" else str(bot_count_cfg)
        min_delay = cfg.get("min_delay_per_bot", 5.1)
        sent = telem.get("messages_sent", 0)
        msgs_min = telem.get("messages_per_minute", 0.0)
        uptime = telem.get("uptime_str", "00:00:00")
        dash_port = cfg.get("web_dashboard_port", 8088)

        print(f"{Color.BLUE}{Color.BOLD}╔══════════════════════════════════════════════════════════════════════╗")
        print(f"║              HBU2 24/7 DAEMON CONTROLLER & SETTINGS                  ║")
        print(f"╚══════════════════════════════════════════════════════════════════════╝{Color.RESET}")
        print(f" Status       : {status_str}  |  Uptime: {uptime}")
        print(f" Bots Loaded  : {Color.BOLD}{bot_str}{Color.RESET} (from {cfg.get('csv_file', 'simulated_accounts.csv')})")
        print(f" Pacing Speed : {Color.BOLD}{min_delay:.2f}s{Color.RESET} per bot (Max Anti-Mute Safe Speed)")
        print(f" Startup Mode : {autostart_str}")
        print(f" Progress     : {Color.GREEN}{sent:,}{Color.RESET} messages sent  |  {msgs_min:.1f} msgs/min")
        print(f" Web Dashboard: {Color.CYAN}http://localhost:{dash_port}{Color.RESET}")
        print("─" * 70)
        print(f"  {Color.BOLD}[1]{Color.RESET} {Color.GREEN}▶ Start Program Now{Color.RESET} (Detached session: safe to close terminal)")
        print(f"  {Color.BOLD}[2]{Color.RESET} {Color.YELLOW}⏸ Pause / Resume Sending{Color.RESET} (Instant toggle)")
        print(f"  {Color.BOLD}[3]{Color.RESET} {Color.RED}⏹ Stop Service{Color.RESET} (Clean shutdown)")
        print(f"  {Color.BOLD}[4]{Color.RESET} 🤖 Set Bot Count (Current: {Color.BOLD}{bot_str}{Color.RESET})")
        print(f"  {Color.BOLD}[5]{Color.RESET} ⚡ Configure Speed / Delay (Current: {min_delay:.2f}s)")
        print(f"  {Color.BOLD}[T]{Color.RESET} 🔄 {Color.CYAN}Toggle Startup on Boot{Color.RESET} [Press T to flip ON/OFF]")
        print(f"  {Color.BOLD}[P]{Color.RESET} 📊 {Color.WHITE}Check Script Progress{Color.RESET} (Live Terminal Monitor)")
        print(f"  {Color.BOLD}[W]{Color.RESET} 🌐 Open Web Dashboard in Browser")
        print(f"  {Color.BOLD}[L]{Color.RESET} 📋 View Rolling Logs")
        print(f"  {Color.BOLD}[0]{Color.RESET} 🚪 Exit TUI (Service continues in background!)\n")

        choice = input(f"{Color.BOLD}Choose an action [0-9, T, P, W, L]: {Color.RESET}").strip().lower()

        if choice in ("0", "q", "quit", "exit"):
            clear_screen()
            print(f"{Color.GREEN}Exiting TUI controller. If the service is running, it will continue in the background!{Color.RESET}")
            sys.exit(0)

        elif choice == "1":
            # Start Program Now
            clear_screen()
            print(f"{Color.CYAN}[*] Starting 24/7 background service...{Color.RESET}")
            ok, msg = start_service()
            if ok:
                print(f"\n{Color.GREEN}{Color.BOLD}[✓] SUCCESS:{Color.RESET} {msg}")
                print(f"{Color.WHITE}You can now safely close this terminal window, and the bots will continue running 24/7!{Color.RESET}")
            else:
                print(f"\n{Color.YELLOW}[!] {msg}{Color.RESET}")
            input(f"\n{Color.DIM}Press Enter to return to menu...{Color.RESET}")

        elif choice == "2":
            # Pause / Resume
            ok, msg = pause_service()
            print(f"\n{Color.CYAN}[*] {msg}{Color.RESET}")
            time.sleep(1.0)

        elif choice == "3":
            # Stop Service
            print(f"\n{Color.YELLOW}[*] Stopping service...{Color.RESET}")
            ok, msg = stop_service()
            print(f"{Color.GREEN if ok else Color.RED}[*] {msg}{Color.RESET}")
            time.sleep(1.2)

        elif choice == "4":
            # Set Bot Count
            clear_screen()
            print(f"{Color.CYAN}{Color.BOLD}=== CONFIGURE BOT COUNT ==={Color.RESET}\n")
            print("Options:")
            print("  • Type 'A' or 'all' to use ALL accounts from the CSV")
            print("  • Type a number (e.g. 5, 20, 50) for a custom count\n")
            val = input(f"Enter choice [current: {bot_str}]: ").strip()
            if val:
                if val.lower() in ("a", "all"):
                    cfg["bot_count"] = "all"
                    print(f"\n{Color.GREEN}✓ Bot count set to ALL accounts.{Color.RESET}")
                else:
                    try:
                        n = int(val)
                        if n > 0:
                            cfg["bot_count"] = n
                            print(f"\n{Color.GREEN}✓ Bot count set to {n} bots.{Color.RESET}")
                        else:
                            print(f"{Color.RED}[!] Please enter a positive integer.{Color.RESET}")
                    except ValueError:
                        print(f"{Color.RED}[!] Invalid entry.{Color.RESET}")
                save_config(cfg)
                if pid:
                    print(f"{Color.YELLOW}[!] Note: Restart the service ([3] then [1]) for new bot count to take effect.{Color.RESET}")
                time.sleep(1.8)

        elif choice == "5":
            # Speed configuration
            clear_screen()
            print(f"{Color.CYAN}{Color.BOLD}=== CONFIGURE ANTI-MUTE PACING SPEED ==={Color.RESET}\n")
            print(f"The HBU2 server enforces a 5.0s per-user rate limit (SPAM_FAST_MS).")
            print(f"The default of {Color.GREEN}5.10s{Color.RESET} is the guaranteed maximum speed without being paused/muted.\n")
            val = input(f"Enter minimum delay in seconds (>= 5.1) [current: {min_delay:.2f}s]: ").strip()
            if val:
                try:
                    delay = float(val)
                    if delay >= 5.1:
                        cfg["min_delay_per_bot"] = delay
                        cfg["max_delay_per_bot"] = delay + 0.15
                        save_config(cfg)
                        print(f"\n{Color.GREEN}✓ Delay updated to {delay:.2f}s per bot.{Color.RESET}")
                    else:
                        print(f"\n{Color.RED}[!] Value must be >= 5.1s to prevent server auto-mutes.{Color.RESET}")
                except ValueError:
                    print(f"\n{Color.RED}[!] Invalid number.{Color.RESET}")
                time.sleep(1.8)

        elif choice in ("t", "6"):
            # Toggle Startup on Boot
            new_state = toggle_autostart()
            state_text = f"{Color.GREEN}ENABLED (Starts automatically on system boot){Color.RESET}" if new_state else f"{Color.YELLOW}DISABLED{Color.RESET}"
            print(f"\n{Color.CYAN}[*] Startup on boot is now: {state_text}")
            time.sleep(1.5)

        elif choice in ("p", "7"):
            # Check Script Progress
            progress_screen()

        elif choice in ("w", "8"):
            # Open Web Dashboard
            url = f"http://localhost:{dash_port}"
            print(f"\n{Color.CYAN}[*] Opening {url} in your default browser...{Color.RESET}")
            try:
                webbrowser.open(url)
            except Exception as e:
                print(f"[!] Could not open browser: {e}")
            time.sleep(1.2)

        elif choice in ("l", "9"):
            # View Logs
            clear_screen()
            print(f"{Color.CYAN}{Color.BOLD}=== RECENT LOGS ({LOG_FILE}) ==={Color.RESET}\n")
            if os.path.exists(LOG_FILE):
                try:
                    res = subprocess.run(["tail", "-n", "25", LOG_FILE], capture_output=True, text=True, check=False)
                    print(res.stdout)
                except Exception:
                    with open(LOG_FILE, "r", errors="replace") as f:
                        lines = f.readlines()
                        print("".join(lines[-25:]))
            else:
                print(f"{Color.DIM}No log file created yet.{Color.RESET}")
            input(f"\n{Color.DIM}Press Enter to return to menu...{Color.RESET}")

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

if __name__ == "__main__":
    main()
