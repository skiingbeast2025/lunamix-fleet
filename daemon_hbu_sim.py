#!/usr/bin/env python3
"""
==============================================================================
HBU2 24/7 Simulator Daemon Engine (daemon_hbu_sim.py)
==============================================================================
Headless, crash-proof 24/7 background service for HBU2 traffic simulation.
Features:
  - Scalable bot fleet (custom number or ALL accounts from CSV)
  - Anti-Mute Maximum Speed (5.1s staggered pacing per bot)
  - Live Pause / Resume state management
  - Circuit Breaker auto-recovery on Supabase timeouts / 500s
  - Automatic isolation of banned/muted accounts to banned_accounts.csv
  - Shared telemetry state for TUI & Web Dashboard
  - Rotating file logging (10MB x 3)
  - Embedded micro-HTTP status server (:8088)
==============================================================================
"""

import argparse
import csv
import json
import logging
import os
import random
import signal
import ssl
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "hbu_service_config.json")
LOGS_DIR = os.path.join(BASE_DIR, "logs")
LOG_FILE = os.path.join(LOGS_DIR, "simulator.log")
PID_FILE = os.path.join(LOGS_DIR, "daemon.pid")
STATE_FILE = os.path.join(LOGS_DIR, "telemetry_state.json")
PAUSE_TRIGGER_FILE = os.path.join(LOGS_DIR, ".pause_toggle")
DEFAULT_CSV = os.path.join(BASE_DIR, "simulated_accounts.csv")
DEFAULT_BANNED_CSV = os.path.join(BASE_DIR, "banned_accounts.csv")

SAMPLE_MESSAGES = [
    "Hey everyone! How's the arcade going?",
    "Anyone want to play Subway Surfers?",
    "Has anyone beaten the high score in Moto X3M yet?",
    "The new UI updates are looking pretty slick :rocket:",
    "Testing out the chat layout from a simulated session.",
    "Just logged on, nice work on the home screen layout!",
    "Does anyone have coin tips for the casino mini-game?",
    "Checking in! Hope everyone's having a great day.",
    "The dark theme styling looks very clean.",
    "GG to whoever was on the leaderboard earlier!",
    "Testing message bubble word-wrap and character spacing.",
    "Is anyone working on new game requests?",
    "Check out the link I just posted in the Links tab :star:",
    "Nice daily streak everyone!",
    "What game are you all playing today?",
    "Speedrun time on Level 3!",
    "Just unlocked a new avatar badge!",
    "Chat is moving fast today :fire:",
    "Who is in the voice room right now?",
    "Great work on the site updates team!"
]

try:
    from download_messages import load_message_pool
except ImportError:
    load_message_pool = None

CHAT_MESSAGES = (
    load_message_pool(SAMPLE_MESSAGES)
    if load_message_pool is not None
    else list(SAMPLE_MESSAGES)
)

# ==============================================================================
# Logging Configuration
# ==============================================================================

os.makedirs(LOGS_DIR, exist_ok=True)
logger = logging.getLogger("HBU_Daemon")
logger.setLevel(logging.INFO)

file_handler = RotatingFileHandler(
    LOG_FILE, maxBytes=10 * 1024 * 1024, backupCount=3, encoding="utf-8"
)
formatter = logging.Formatter("[%(asctime)s] [%(levelname)s] [%(threadName)s] %(message)s")
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)

# Console handler if running interactively
if sys.stdout and sys.stdout.isatty():
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

# ==============================================================================
# Global Daemon State
# ==============================================================================

stop_event = threading.Event()
pause_event = threading.Event()
pause_event.set()  # set = RUNNING; clear = PAUSED

# RLock prevents self-deadlock if helper setters are invoked while updating telemetry.
telemetry_lock = threading.RLock()
banned_csv_lock = threading.Lock()
_snapshot_lock = threading.Lock()
telemetry = {
    "status": "STOPPED",
    "start_time": None,
    "uptime_seconds": 0,
    "uptime_str": "00:00:00",
    "active_bots": 0,
    "total_bots_configured": "ALL",
    "messages_sent": 0,
    "messages_failed": 0,
    "messages_per_minute": 0.0,
    "banned_count": 0,
    "last_latency_ms": 0,
    "paused": False,
    "bot_states": [],
    "recent_messages": deque(maxlen=30)
}

worker_threads = []

# ==============================================================================
# Configuration Loader
# ==============================================================================

def load_config():
    """Loads configuration from JSON or returns robust defaults."""
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
                data = json.load(f)
            if isinstance(data, dict):
                default.update(data)
            else:
                logger.warning("Config file root is not a JSON object; using defaults.")
        except Exception as e:
            logger.warning(f"Could not load config file: {e}")
    # Enforce minimum safe delay even if config has malformed delay values
    try:
        min_delay = float(default.get("min_delay_per_bot", 5.1))
    except (TypeError, ValueError):
        min_delay = 5.1
    try:
        max_delay = float(default.get("max_delay_per_bot", 5.25))
    except (TypeError, ValueError):
        max_delay = 5.25
    if min_delay < 5.1:
        min_delay = 5.1
    if max_delay < min_delay:
        max_delay = min_delay + 0.15
    default["min_delay_per_bot"] = min_delay
    default["max_delay_per_bot"] = max_delay
    return default

# ==============================================================================
# Network Helper
# ==============================================================================

def get_ssl_context():
    try:
        return ssl.create_default_context()
    except Exception:
        return ssl._create_unverified_context()

def send_chat_message(functions_url, anon_key, username, password, message, timeout=4, rest_url=""):
    """Sends a chat message to 'everyone' via the Edge Function with RPC fallback."""
    url = f"{functions_url.rstrip('/')}/send-message"
    payload = json.dumps({
        "username": username,
        "password": password,
        "receiver": "everyone",
        "message": message
    }).encode("utf-8")

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "apikey": anon_key,
        "Authorization": f"Bearer {anon_key}",
        "Origin": "https://rockystudios.github.io",
        "Referer": "https://rockystudios.github.io/HBU2/"
    }

    ctx = get_ssl_context()
    req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            latency = int((time.time() - t0) * 1000)
            return resp.status in (200, 204), latency, "OK"
    except urllib.error.HTTPError as err:
        latency = int((time.time() - t0) * 1000)
        body = err.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(body)
            msg = parsed.get("error") or parsed.get("message") or body
        except Exception:
            msg = body

        # Fallback to direct RPC /rpc/hbu_send if edge function is unreachable or gives 403 authorization error
        if rest_url and (err.code in (404, 502, 503) or (err.code == 403 and "not authorized" in msg.lower())):
            try:
                rpc_url = f"{rest_url.rstrip('/')}/rpc/hbu_send"
                rpc_payload = json.dumps({
                    "p_username": username,
                    "p_password": password,
                    "p_receiver": "everyone",
                    "p_message": message
                }).encode("utf-8")
                rpc_req = urllib.request.Request(rpc_url, data=rpc_payload, headers=headers, method="POST")
                with urllib.request.urlopen(rpc_req, timeout=timeout, context=ctx) as rpc_resp:
                    latency = int((time.time() - t0) * 1000)
                    return rpc_resp.status in (200, 204), latency, "OK (via RPC)"
            except Exception:
                pass

        return False, latency, f"HTTP {err.code}: {msg}"
    except Exception as exc:
        latency = int((time.time() - t0) * 1000)
        return False, latency, str(exc)

# ==============================================================================
# CSV Operations & Banned Account Isolation
# ==============================================================================

def load_accounts(csv_path):
    """Loads accounts from CSV."""
    if not os.path.isabs(csv_path):
        csv_path = os.path.join(BASE_DIR, csv_path)
    if not os.path.isfile(csv_path):
        return []

    accounts = []
    try:
        with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                uname = (row.get("username") or row.get("Username") or "").strip()
                pwd = (row.get("password") or row.get("Password") or "").strip()
                if uname and pwd:
                    accounts.append({
                        "username": uname,
                        "password": pwd,
                        "student_id": (row.get("student_id") or row.get("ID") or "").strip(),
                        "coins": (row.get("coins") or row.get("Coins") or "0").strip(),
                        "created_at": (row.get("created_at") or row.get("CreatedAt") or "").strip()
                    })
    except (OSError, csv.Error, UnicodeDecodeError) as e:
        logger.warning(f"Could not read accounts CSV '{csv_path}': {e}")
        return []
    return accounts

def record_banned_account(banned_csv_path, account, reason):
    """Immediately logs an unusable/banned account to the banned CSV."""
    if not os.path.isabs(banned_csv_path):
        banned_csv_path = os.path.join(BASE_DIR, banned_csv_path)

    fieldnames = [
        "username", "password", "student_id", "coins",
        "created_at", "status", "reason", "tested_at"
    ]
    try:
        parent_dir = os.path.dirname(os.path.abspath(banned_csv_path))
        if parent_dir:
            os.makedirs(parent_dir, exist_ok=True)
        with banned_csv_lock:
            file_exists = os.path.isfile(banned_csv_path) and os.path.getsize(banned_csv_path) > 0
            with open(banned_csv_path, "a", encoding="utf-8", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                if not file_exists:
                    writer.writeheader()
                writer.writerow({
                    "username": account.get("username", ""),
                    "password": account.get("password", ""),
                    "student_id": account.get("student_id", ""),
                    "coins": account.get("coins", "0"),
                    "created_at": account.get("created_at", ""),
                    "status": "BANNED" if "ban" in reason.lower() else "MUTED",
                    "reason": reason,
                    "tested_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
                })
    except Exception as e:
        logger.error(f"Could not record banned account: {e}")

# ==============================================================================
# Circuit Breaker (Auto-Recovery on Remote Server Delays)
# ==============================================================================

circuit_breaker_lock = threading.Lock()
circuit_breaker_until = 0
circuit_breaker_consecutive_failures = 0

def check_circuit_breaker():
    """Checks if the circuit breaker is currently active."""
    global circuit_breaker_until
    with circuit_breaker_lock:
        now = time.time()
        if now < circuit_breaker_until:
            return True, int(circuit_breaker_until - now)
        return False, 0

def trip_circuit_breaker(reason="Server timeout/overload"):
    """Pauses all bots temporarily when Supabase is overloaded."""
    global circuit_breaker_until, circuit_breaker_consecutive_failures
    with circuit_breaker_lock:
        circuit_breaker_consecutive_failures += 1
        # Exponential pause: 15s, 30s, 60s, max 120s
        pause_duration = min(120, 15 * (2 ** min(3, circuit_breaker_consecutive_failures - 1)))
        circuit_breaker_until = time.time() + pause_duration
        logger.warning(
            f"[CIRCUIT BREAKER ACTIVATED] {reason}. Pausing all bots for {pause_duration}s to allow server recovery."
        )

def reset_circuit_breaker():
    """Resets circuit breaker after successful request."""
    global circuit_breaker_consecutive_failures, circuit_breaker_until
    with circuit_breaker_lock:
        circuit_breaker_consecutive_failures = 0
        circuit_breaker_until = 0

# ==============================================================================
# Bot Worker Thread
# ==============================================================================

def bot_worker(acc, bot_idx, initial_stagger, config, mock=False):
    """Individual autonomous bot worker running indefinitely at maximum safe speed."""
    uname = acc["username"]
    pwd = acc["password"]
    fn_url = config["supabase_functions_url"]
    rest_url = config.get("supabase_rest_url", "")
    anon_key = config["supabase_anon_key"]
    min_delay = config["min_delay_per_bot"]
    max_delay = config["max_delay_per_bot"]
    banned_csv = config["banned_csv_file"]

    def set_bot_status(status_str, seen=None):
        with telemetry_lock:
            for b in telemetry["bot_states"]:
                if b["bot"] == uname:
                    b["status"] = status_str
                    if seen:
                        b["last_seen"] = seen
                    break

    # Initial staggered delay to spread traffic evenly across the fleet
    if initial_stagger > 0:
        set_bot_status("STAGGERED", f"stagger {initial_stagger:.1f}s")
        if stop_event.wait(initial_stagger):
            return

    set_bot_status("READY", "ready")
    logger.info(f"Bot '{uname}' initialized and active (cycle: {min_delay:.2f}s).")

    # Local bot stats
    sent_count = 0
    last_sent_time = 0

    while not stop_event.is_set():
        # 1. Respect Pause State
        pause_event.wait()
        if stop_event.is_set():
            break

        # 2. Check Circuit Breaker
        tripped, remaining_sec = check_circuit_breaker()
        if tripped:
            set_bot_status("CIRCUIT_BREAKER", f"pause {remaining_sec}s")
            stop_event.wait(min(2.0, max(0.1, remaining_sec)))
            continue

        # 3. Enforce strictly >= 5.1s since last send for this specific bot
        now = time.time()
        elapsed_since_last = now - last_sent_time
        target_interval = random.uniform(min_delay, max_delay)
        if elapsed_since_last < target_interval:
            sleep_needed = target_interval - elapsed_since_last
            set_bot_status("PACING", f"wait {sleep_needed:.1f}s")
            if stop_event.wait(sleep_needed):
                break

        if stop_event.is_set():
            break

        # 4. Pick message
        msg = random.choice(CHAT_MESSAGES)

        # 5. Send message
        set_bot_status("SENDING", "dispatching")
        if mock:
            # Local mock test (no network calls)
            time.sleep(0.05)
            ok, latency, err = True, 15, "OK (Mock)"
        else:
            ok, latency, err = send_chat_message(fn_url, anon_key, uname, pwd, msg, timeout=12, rest_url=rest_url)

        last_sent_time = time.time()
        timestr = datetime.now().strftime("%H:%M:%S")

        # 6. Update Telemetry
        with telemetry_lock:
            telemetry["last_latency_ms"] = latency
            if ok:
                telemetry["messages_sent"] += 1
                sent_count += 1
                reset_circuit_breaker()
                telemetry["recent_messages"].append({
                    "time": timestr,
                    "bot": uname,
                    "msg": msg,
                    "status": "OK"
                })
                for b in telemetry["bot_states"]:
                    if b["bot"] == uname:
                        b["sent"] = sent_count
                        b["last_seen"] = "just now"
                        b["status"] = "OK"
                        break
            else:
                telemetry["messages_failed"] += 1
                # Origin authorization rejection
                if "not authorized to use hbu2 services" in err.lower():
                    logger.error(f"Bot '{uname}' origin error: {err}")
                    set_bot_status("AUTH_ERROR", "unauthorized origin")
                    trip_circuit_breaker(err)
                # Invalid Credentials (account wiped or password reset)
                elif "401" in err or "wrong username or password" in err.lower():
                    logger.warning(f"Bot '{uname}' invalid credentials (HTTP 401): {err}")
                    record_banned_account(banned_csv, acc, f"INVALID: {err}")
                    set_bot_status("INVALID", "bad password")
                    break
                # Check for Ban or Mute (HTTP 403)
                elif "403" in err or "banned" in err.lower() or "muted" in err.lower():
                    logger.warning(f"Bot '{uname}' received BAN/MUTE: {err}")
                    telemetry["banned_count"] += 1
                    record_banned_account(banned_csv, acc, err)
                    set_bot_status("BANNED", "muted/banned")
                    break
                # Server timeout
                elif "timed out" in err.lower():
                    set_bot_status("TIMEOUT", "server slow")
                    trip_circuit_breaker(err)
                # Check for Server 500 / 502 / 503 / 504 / Timeouts
                elif any(code in err for code in ("500", "502", "503", "504")):
                    set_bot_status("SERVER_ERROR", err[:15])
                    trip_circuit_breaker(err)

        logger.debug(f"Bot '{uname}': sent msg #{sent_count} ({latency}ms) -> {err}")

    logger.info(f"Bot '{uname}' finished execution.")

# ==============================================================================
# Telemetry Flusher & Background Monitor
# ==============================================================================

def save_telemetry_snapshot(snap):
    """Atomically writes telemetry state to disk to prevent half-written/corrupt reads."""
    parent_dir = os.path.dirname(os.path.abspath(STATE_FILE))
    tmp_path = None
    with _snapshot_lock:
        try:
            os.makedirs(parent_dir, exist_ok=True)
            # Convert deque to list if present without mutating caller's dict
            payload = dict(snap)
            if isinstance(payload.get("recent_messages"), deque):
                payload["recent_messages"] = list(payload["recent_messages"])
            fd, tmp_path = tempfile.mkstemp(
                prefix=f".{os.path.basename(STATE_FILE)}.",
                suffix=".tmp",
                dir=parent_dir,
            )
            file_obj = None
            try:
                file_obj = os.fdopen(fd, "w", encoding="utf-8")
                with file_obj as f:
                    json.dump(payload, f, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
            except Exception:
                if file_obj is None:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                raise
            os.replace(tmp_path, STATE_FILE)
        except Exception as e:
            logger.debug(f"Telemetry save error: {e}")
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass

def telemetry_writer_thread(config):
    """Periodically writes telemetry snapshot to disk for TUI / Web / CLI."""
    start_time = time.time()
    recent_sent_counts = deque(maxlen=30)  # for sliding window msgs/min

    while not stop_event.is_set():
        # Check for pause trigger file changes
        if os.path.exists(PAUSE_TRIGGER_FILE):
            try:
                with open(PAUSE_TRIGGER_FILE, "r") as f:
                    val = f.read().strip().lower()
                os.remove(PAUSE_TRIGGER_FILE)
                if (val in ("1", "pause") or (val == "toggle" and pause_event.is_set())) and pause_event.is_set():
                    pause_event.clear()
                    logger.info("Daemon state toggled to PAUSED.")
                elif (val in ("0", "resume") or (val == "toggle" and not pause_event.is_set())) and not pause_event.is_set():
                    pause_event.set()
                    logger.info("Daemon state toggled to RESUMED.")
            except Exception:
                pass

        now = time.time()
        uptime_sec = int(now - start_time)
        hrs = uptime_sec // 3600
        mins = (uptime_sec % 3600) // 60
        secs = uptime_sec % 60
        uptime_str = f"{hrs:02d}:{mins:02d}:{secs:02d}"

        with telemetry_lock:
            telemetry["uptime_seconds"] = uptime_sec
            telemetry["uptime_str"] = uptime_str
            telemetry["status"] = "PAUSED" if not pause_event.is_set() else "RUNNING"
            telemetry["paused"] = not pause_event.is_set()
            if worker_threads:
                telemetry["active_bots"] = sum(1 for t in worker_threads if t.is_alive())

            # Calculate msgs/min
            recent_sent_counts.append((now, telemetry["messages_sent"]))
            if len(recent_sent_counts) >= 2:
                dt = recent_sent_counts[-1][0] - recent_sent_counts[0][0]
                dm = recent_sent_counts[-1][1] - recent_sent_counts[0][1]
                telemetry["messages_per_minute"] = (dm / dt) * 60.0 if dt > 0 else 0.0

            # Write snapshot to disk
            snapshot = telemetry.copy()
            snapshot["recent_messages"] = list(telemetry["recent_messages"])
            snapshot["bot_states"] = list(telemetry["bot_states"])

        save_telemetry_snapshot(snapshot)
        time.sleep(1.0)

# ==============================================================================
# Main Daemon Lifecycle
# ==============================================================================

def write_pid():
    try:
        pid_dir = os.path.dirname(os.path.abspath(PID_FILE))
        if pid_dir:
            os.makedirs(pid_dir, exist_ok=True)
        with open(PID_FILE, "w") as f:
            f.write(str(os.getpid()))
    except Exception as e:
        logger.warning(f"Could not write PID file: {e}")

def remove_pid():
    if os.path.exists(PID_FILE):
        try:
            os.remove(PID_FILE)
        except Exception:
            pass

def signal_handler(signum, frame):
    """Graceful shutdown signal handling (SIGTERM, SIGINT)."""
    logger.info(f"Received signal {signum}. Initiating clean shutdown...")
    stop_event.set()
    pause_event.set()  # Unblock any paused workers so they can exit immediately

def run_daemon(mock=False, test_duration=0):
    """Main daemon entrypoint."""
    stop_event.clear()
    pause_event.set()
    reset_circuit_breaker()

    # Register signals (only valid on main thread)
    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, signal_handler)
        signal.signal(signal.SIGINT, signal_handler)
        # Ignore SIGHUP so closing the terminal does not kill the daemon
        if hasattr(signal, "SIGHUP"):
            signal.signal(signal.SIGHUP, signal.SIG_IGN)

    write_pid()
    logger.info(f"HBU2 Simulator Daemon started (PID: {os.getpid()}).")

    config = load_config()

    # Start Embedded Status Server if enabled
    if config.get("web_dashboard_enabled", True):
        port = config.get("web_dashboard_port", 8088)
        try:
            from status_server import start_status_server
            start_status_server(port)
            logger.info(f"Web Dashboard listening at http://localhost:{port}")
        except Exception as e:
            logger.warning(f"Status server error: {e}")

    # Load Accounts
    csv_file = config.get("csv_file", "simulated_accounts.csv")
    resolved_csv = csv_file if os.path.isabs(csv_file) else os.path.join(BASE_DIR, csv_file)
    accounts = load_accounts(resolved_csv)
    if not accounts:
        # Fallback to bak file if main CSV is temporarily emptied or corrupted
        bak_file = f"{resolved_csv}.bak"
        if os.path.exists(bak_file):
            logger.info(f"Loading accounts from backup: {bak_file}")
            accounts = load_accounts(bak_file)

    if not accounts:
        logger.error(f"No accounts found in {csv_file}. Exiting daemon.")
        remove_pid()
        sys.exit(1)

    # Determine Bot Count
    bot_cfg = config.get("bot_count", "all")
    if str(bot_cfg).lower() == "all":
        selected_accounts = accounts
    else:
        try:
            n = int(bot_cfg)
            selected_accounts = accounts[:max(1, n)]
        except Exception:
            selected_accounts = accounts

    total_bots = len(selected_accounts)
    logger.info(f"Loaded {total_bots} bot accounts for continuous simulation.")

    with telemetry_lock:
        telemetry["active_bots"] = total_bots
        telemetry["total_bots_configured"] = "ALL" if str(bot_cfg).lower() == "all" else total_bots
        telemetry["status"] = "RUNNING"
        telemetry["messages_sent"] = 0
        telemetry["messages_failed"] = 0
        telemetry["banned_count"] = 0
        telemetry["recent_messages"].clear()
        telemetry["bot_states"] = [
            {
                "bot": acc["username"],
                "sent": 0,
                "last_seen": "queued",
                "status": "QUEUED"
            }
            for acc in selected_accounts
        ]

    # Start Telemetry Thread
    t_flusher = threading.Thread(target=telemetry_writer_thread, args=(config,), daemon=True)
    t_flusher.start()

    # Calculate staggered startup offsets across the fleet
    min_delay = config["min_delay_per_bot"]
    threads = []
    for i, acc in enumerate(selected_accounts):
        # Stagger each bot evenly over the 5.1s cycle
        stagger = (i / max(1, total_bots)) * min_delay
        t = threading.Thread(
            target=bot_worker,
            args=(acc, i, stagger, config, mock),
            name=f"Bot-{acc['username']}"
        )
        t.daemon = True
        t.start()
        threads.append(t)

    logger.info(f"All {total_bots} bot threads spawned with staggered anti-mute pacing.")
    global worker_threads
    worker_threads = threads

    # Execution Loop
    start_time = time.time()
    try:
        while not stop_event.is_set():
            time.sleep(0.5)
            if threads and not any(t.is_alive() for t in threads):
                logger.warning("All bot threads have finished or stopped. Halting daemon.")
                break
            if test_duration > 0 and (time.time() - start_time) >= test_duration:
                logger.info(f"Test duration of {test_duration}s reached. Stopping daemon.")
                break
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt received.")
    finally:
        stop_event.set()
        pause_event.set()

        logger.info("Waiting up to 3 seconds for bot threads to stop...")
        for t in threads:
            t.join(timeout=0.1)

        # Mark stopped in state
        with telemetry_lock:
            telemetry["status"] = "STOPPED"
            snapshot = telemetry.copy()
            snapshot["recent_messages"] = list(telemetry["recent_messages"])
            snapshot["bot_states"] = list(telemetry["bot_states"])
        save_telemetry_snapshot(snapshot)

        remove_pid()
        logger.info("HBU2 Simulator Daemon successfully stopped.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="HBU2 24/7 Simulator Daemon")
    parser.add_argument("--mock", action="store_true", help="Run local mock simulation (dry run)")
    parser.add_argument("--test-duration", type=int, default=0, help="Automatically exit after N seconds (for testing)")
    args = parser.parse_args()

    run_daemon(mock=args.mock, test_duration=args.test_duration)
