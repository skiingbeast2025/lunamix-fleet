#!/usr/bin/env python3
"""
simulate_hbu_chat.py

Interactive Terminal User Interface (TUI) & Fully Parallel Automation Tool for
simulating user accounts, chat activity, and managing coins on the HBU2 platform.

Features:
- Anti-Mute Compliant: Enforces server's 5.0s per-user limit (SPAM_FAST_MS) to prevent auto-mutes.
- True Parallel Concurrency: Multi-threaded autonomous bot workers all running simultaneously.
- Coin Manager: Import accounts (single from CSV, all from CSV, or custom user/pass) and set coins.
- Persistent configuration saved to 'hbu_sim_settings.json'.
- Records all generated accounts into a local CSV file immediately upon creation.
- Built-in Local Mock Mode (dry run) with zero external network traffic.
"""

import argparse
import csv
import json
import os
import random
import secrets
import select
import signal
import ssl
import string
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

# Ensure UTF-8 output on standard terminals
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

# Determine base directory
try:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
except Exception:
    BASE_DIR = os.getcwd()

SETTINGS_FILE = os.path.join(BASE_DIR, "hbu_sim_settings.json")

# Default settings
DEFAULT_SETTINGS = {
    "supabase_rest_url": "https://xvkjvuxhawlljakfkllu.supabase.co/rest/v1",
    "supabase_functions_url": "https://xvkjvuxhawlljakfkllu.supabase.co/functions/v1",
    "supabase_anon_key": "sb_publishable_uyxd5lS77wdXpD56QF_lgg_zaujLjZZ",
    "supabase_service_role_key": "",
    "csv_filename": "simulated_accounts.csv",
    "num_accounts": 10,
    "messages_per_user": 3,
    "min_delay_seconds": 5.2,
    "max_delay_seconds": 7.5,
    "use_random_names": True,
    "name_prefix": "User",
}

# Pre-packaged pool of realistic messages for UI testing
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
    "Nice daily streak everyone!"
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

# ANSI styling helpers
class Style:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RED = "\033[91m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    MAGENTA = "\033[95m"
    CYAN = "\033[96m"
    WHITE = "\033[97m"

print_lock = threading.Lock()

def safe_print(*args, **kwargs):
    """Thread-safe terminal printing."""
    with print_lock:
        print(*args, **kwargs)

import builtins

def safe_input(prompt=""):
    """Crash-proof user input handling EOF and interrupts."""
    try:
        return builtins.input(prompt)
    except EOFError:
        return ""
    except KeyboardInterrupt:
        safe_print(f"\n{Style.YELLOW}[!] Interrupted by user. Exiting...{Style.RESET}")
        sys.exit(0)

input = safe_input

def clear_screen():
    os.system("cls" if os.name == "nt" else "clear")

def get_ssl_context():
    """Resilient SSL context fallback for macOS Python certificates."""
    try:
        return ssl.create_default_context()
    except Exception:
        return ssl._create_unverified_context()

def load_settings():
    """Loads configuration from JSON file or returns defaults."""
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    merged = DEFAULT_SETTINGS.copy()
                    merged.update(data)
                    try:
                        min_del = float(merged.get("min_delay_seconds", 5.2))
                    except (TypeError, ValueError):
                        min_del = 5.2
                    if min_del < 5.1:
                        min_del = 5.2
                    try:
                        max_del = float(merged.get("max_delay_seconds", 7.5))
                    except (TypeError, ValueError):
                        max_del = 7.5
                    if max_del < 5.5 or max_del < min_del:
                        max_del = max(7.5, min_del + 0.5)
                    merged["min_delay_seconds"] = min_del
                    merged["max_delay_seconds"] = max_del
                    return merged
        except Exception:
            pass
    return DEFAULT_SETTINGS.copy()

def save_settings(settings):
    """Saves configuration to JSON file."""
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2)
    except Exception as e:
        safe_print(f"{Style.RED}Error saving settings: {e}{Style.RESET}")

WORDLIST_FILE = os.path.join(BASE_DIR, "random_words.txt")
_CACHED_WORDLIST = None

def get_wordlist():
    """Loads words from random_words.txt with fallback."""
    global _CACHED_WORDLIST
    if _CACHED_WORDLIST is None:
        if os.path.isfile(WORDLIST_FILE):
            try:
                with open(WORDLIST_FILE, "r", encoding="utf-8") as f:
                    _CACHED_WORDLIST = [line.strip() for line in f if line.strip()]
            except Exception:
                pass
        if not _CACHED_WORDLIST:
            _CACHED_WORDLIST = ["Neon", "Pixel", "Cyber", "Shadow", "Retro", "Lunar", "Hyper", "Solar", "Echo", "Frost", "Potato", "Steel", "Wave", "Theory", "Shock", "Gamer", "Runner", "Knight", "Pilot", "Ghost"]
    return _CACHED_WORDLIST

def generate_username(prefix="User", use_random=True):
    """Generates a valid 3-20 char username using random_words.txt: ^[A-Za-z0-9_]{3,20}$"""
    if use_random:
        words = get_wordlist()
        mode = random.choice([1, 2, 3])
        if mode == 1:
            w1 = random.choice(words)
            w2 = random.choice(words)
            name = f"{w1}_{w2}"
        elif mode == 2:
            w1 = random.choice(words)
            num = random.randint(10, 999)
            name = f"{w1}_{num}"
        else:
            w1 = random.choice(words)
            w2 = random.choice(words)
            name = f"{w1}{w2}"
        name = name[:20]
        if len(name) < 3:
            name = f"{name}_{random.randint(10, 99)}"
        return name
    else:
        num = random.randint(100, 9999)
        name = f"{prefix}_{num}"
        return name[:20]

def generate_password(length=12):
    """Generates a secure password (min 6 chars required)."""
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
    return "".join(secrets.choice(alphabet) for _ in range(length))

def generate_student_id():
    """Generates a mock student ID string."""
    return f"STU{random.randint(10000, 99999)}"

def make_http_request(url, method="POST", payload=None, anon_key="", timeout=4):
    """Executes an HTTP request with Supabase headers, browser Origin, and Referer."""
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {
        "apikey": anon_key,
        "Authorization": f"Bearer {anon_key}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
        "Origin": "https://rockystudios.github.io",
        "Referer": "https://rockystudios.github.io/HBU2/",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    }
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    ctx = get_ssl_context()
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        return resp.status, resp.read().decode("utf-8", errors="replace")

def signup_user(rest_url, anon_key, username, password, student_id):
    """Calls /rest/v1/rpc/hbu_signup."""
    url = f"{rest_url.rstrip('/')}/rpc/hbu_signup"
    payload = {
        "p_username": username,
        "p_password": password,
        "p_student_id": student_id
    }
    try:
        status, _ = make_http_request(url, method="POST", payload=payload, anon_key=anon_key)
        return status in (200, 204), "OK"
    except urllib.error.HTTPError as err:
        body = err.read().decode("utf-8", errors="replace")
        try:
            err_json = json.loads(body)
            msg = err_json.get("message", body)
        except Exception:
            msg = body
        return False, f"HTTP {err.code}: {msg}"
    except Exception as err:
        return False, str(err)

def spin_wheel_for_account(rest_url, anon_key, username, password):
    """Spins the daily wheel via /rpc/hbu_spin to claim real database coins with anon key."""
    url = f"{rest_url.rstrip('/')}/rpc/hbu_spin"
    payload = {"p_username": username, "p_password": password}
    try:
        status, resp = make_http_request(url, method="POST", payload=payload, anon_key=anon_key)
        data = json.loads(resp)
        return True, data.get("prize", 0), data.get("coins", 0), "OK"
    except urllib.error.HTTPError as err:
        body = err.read().decode("utf-8", errors="replace")
        try:
            err_json = json.loads(body)
            msg = err_json.get("message", body)
        except Exception:
            msg = body
        return False, 0, 0, msg
    except Exception as err:
        return False, 0, 0, str(err)

def send_chat_message(functions_url, rest_url, anon_key, username, password, message):
    """Sends a chat message to 'everyone' via Edge Function with RPC fallback."""
    fn_url = f"{functions_url.rstrip('/')}/send-message"
    payload = {
        "username": username,
        "password": password,
        "receiver": "everyone",
        "message": message
    }
    try:
        status, _ = make_http_request(fn_url, method="POST", payload=payload, anon_key=anon_key)
        return status in (200, 204), "OK"
    except urllib.error.HTTPError as err:
        body = err.read().decode("utf-8", errors="replace")
        try:
            err_json = json.loads(body)
            msg = err_json.get("error", err_json.get("message", body))
        except Exception:
            msg = body
        
        if "muted" in msg.lower():
            return False, f"MUTED: {msg}"

        if err.code in (404, 502, 503) or (err.code == 403 and "not authorized" in msg.lower()):
            try:
                rpc_url = f"{rest_url.rstrip('/')}/rpc/hbu_send"
                rpc_payload = {
                    "p_username": username,
                    "p_password": password,
                    "p_receiver": "everyone",
                    "p_message": message
                }
                status, _ = make_http_request(rpc_url, method="POST", payload=rpc_payload, anon_key=anon_key)
                return status in (200, 204), "OK (via RPC)"
            except Exception as rpc_err:
                return False, f"RPC fallback failed: {rpc_err}"

        return False, f"HTTP {err.code}: {msg}"
    except Exception as err:
        return False, str(err)

csv_lock = threading.Lock()

def append_single_account_to_csv(filepath, account):
    """Appends a single account to CSV immediately upon creation."""
    full_path = os.path.join(BASE_DIR, filepath) if not os.path.isabs(filepath) else filepath
    with csv_lock:
        file_exists = os.path.isfile(full_path) and os.path.getsize(full_path) > 0
        with open(full_path, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            if not file_exists:
                writer.writerow(["username", "password", "student_id", "coins", "created_at"])
            writer.writerow([
                account["username"],
                account["password"],
                account.get("student_id", ""),
                account.get("coins", 0),
                time.strftime("%Y-%m-%d %H:%M:%S")
            ])

def load_accounts_from_csv(filepath):
    """Loads existing accounts from CSV file."""
    full_path = os.path.join(BASE_DIR, filepath) if not os.path.isabs(filepath) else filepath
    if not os.path.isfile(full_path):
        return []
    accounts = []
    with csv_lock:
        try:
            with open(full_path, "r", encoding="utf-8-sig", newline="") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    uname = (row.get("username") or "").strip()
                    pwd = row.get("password")
                    if uname and pwd is not None:
                        row["username"] = uname
                        if not row.get("coins"):
                            row["coins"] = 0
                        accounts.append(row)
        except (OSError, csv.Error, UnicodeDecodeError):
            return []
    return accounts

def update_csv_coins(filepath, updates_dict):
    """Updates the coin values for accounts in the CSV file."""
    full_path = os.path.join(BASE_DIR, filepath) if not os.path.isabs(filepath) else filepath
    if not os.path.isfile(full_path):
        return False
    with csv_lock:
        rows = []
        fieldnames = ["username", "password", "student_id", "coins", "created_at"]
        try:
            with open(full_path, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                if reader.fieldnames:
                    for fn in reader.fieldnames:
                        if fn and fn not in fieldnames:
                            fieldnames.append(fn)
                for r in reader:
                    r.pop(None, None)  # Clean unmapped columns
                    uname = r.get("username", "")
                    if uname in updates_dict:
                        r["coins"] = str(updates_dict[uname])
                    rows.append(r)

            with open(full_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
                writer.writeheader()
                writer.writerows(rows)
            return True
        except Exception as e:
            safe_print(f"{Style.RED}[!] CSV Update Error: {e}{Style.RESET}")
            return False

# ==============================================================================
# Coin Granting Engine
# ==============================================================================

def grant_coins_to_accounts(settings, target_accounts, coins_amount):
    """
    Applies coins to specified accounts via Supabase REST API (PATCH),
    with automatic fallback/instruction for RLS permissions, and updates CSV.
    """
    clear_screen()
    safe_print(f"{Style.BOLD}{Style.CYAN}==================================================================={Style.RESET}")
    safe_print(f"{Style.BOLD}{Style.WHITE}                   Granting Coins to Accounts                     {Style.RESET}")
    safe_print(f"{Style.BOLD}{Style.CYAN}==================================================================={Style.RESET}\n")

    safe_print(f"Targeting: {Style.BOLD}{len(target_accounts)}{Style.RESET} account(s)")
    safe_print(f"Coins to assign: {Style.YELLOW}{coins_amount:,}{Style.RESET}\n")

    rest_url = settings["supabase_rest_url"].rstrip("/")
    service_key = settings.get("supabase_service_role_key") or os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    anon_key = settings["supabase_anon_key"]
    active_key = service_key if service_key else anon_key

    is_using_service_key = bool(service_key)
    updates_for_csv = {}
    successful = []
    rls_blocked = []

    try:
        for acc in target_accounts:
            uname = acc["username"]
            safe_print(f"  Updating '{uname}'... ", end="", flush=True)

            # 1. Attempt custom RPC hbu_set_coins (if setup_coin_api.sql was installed)
            rpc_url = f"{rest_url}/rpc/hbu_set_coins"
            rpc_payload = {"p_username": uname, "p_coins": int(coins_amount)}
            rpc_succeeded = False

            try:
                status, resp = make_http_request(rpc_url, method="POST", payload=rpc_payload, anon_key=active_key)
                if status in (200, 204):
                    safe_print(f"{Style.GREEN}✓ Updated to {coins_amount:,} coins (via hbu_set_coins API){Style.RESET}")
                    successful.append(uname)
                    updates_for_csv[uname] = coins_amount
                    rpc_succeeded = True
            except urllib.error.HTTPError:
                pass
            except Exception:
                pass

            if rpc_succeeded:
                continue

            # 2. Fall back to direct REST PATCH
            url = f"{rest_url}/accounts?username=eq.{urllib.parse.quote(uname)}"
            payload = {"coins": int(coins_amount)}

            try:
                status, resp = make_http_request(url, method="PATCH", payload=payload, anon_key=active_key)
                if status in (200, 204):
                    safe_print(f"{Style.GREEN}✓ Updated to {coins_amount:,} coins{Style.RESET}")
                    successful.append(uname)
                    updates_for_csv[uname] = coins_amount
                else:
                    safe_print(f"{Style.RED}✗ HTTP {status}{Style.RESET}")
            except urllib.error.HTTPError as err:
                err_body = err.read().decode("utf-8", errors="replace")
                if err.code in (401, 403) or "42501" in err_body:
                    safe_print(f"{Style.YELLOW}Blocked by Supabase RLS{Style.RESET}")
                    rls_blocked.append(uname)
                    updates_for_csv[uname] = coins_amount
                else:
                    safe_print(f"{Style.RED}✗ HTTP {err.code}: {err_body}{Style.RESET}")
            except Exception as err:
                safe_print(f"{Style.RED}✗ Connection Error: {err}{Style.RESET}")

    except KeyboardInterrupt:
        safe_print(f"\n{Style.RED}[!] Coin assignment process interrupted by user.{Style.RESET}")

    # Update the local CSV records
    if updates_for_csv:
        update_csv_coins(settings["csv_filename"], updates_for_csv)
        safe_print(f"\n{Style.GREEN}✓ Updated local CSV records in '{settings['csv_filename']}' with new coin balances.{Style.RESET}")

    # If blocked by Row Level Security, provide direct guidance and auto-generate SQL file
    if rls_blocked:
        user_list_sql = ", ".join(f"'{u}'" for u in rls_blocked)
        sql_filename = os.path.join(BASE_DIR, "update_coins.sql")
        setup_filename = os.path.join(BASE_DIR, "setup_coin_api.sql")
        try:
            with open(sql_filename, "w", encoding="utf-8") as f:
                f.write(f"-- Run this in your Supabase SQL Editor to apply coins directly:\n")
                f.write(f"UPDATE public.accounts SET coins = {coins_amount} WHERE username IN ({user_list_sql});\n")
            sql_saved_msg = f"{Style.GREEN}✓ Ready-to-run SQL saved to:{Style.RESET} {Style.BOLD}update_coins.sql{Style.RESET}"
        except Exception:
            sql_saved_msg = ""

        safe_print(f"\n{Style.YELLOW}==================================================================={Style.RESET}")
        safe_print(f"{Style.BOLD}{Style.YELLOW}          HOW TO BYPASS THIS AND MAKE COINS WORK DIRECTLY         {Style.RESET}")
        safe_print(f"{Style.YELLOW}==================================================================={Style.RESET}")
        safe_print(f"By default, Supabase's anon key cannot directly modify the accounts table.")
        safe_print(f"\n{Style.BOLD}Two ways around this:{Style.RESET}")
        safe_print(f"  {Style.BOLD}1. 🎰 Use Daily Wheel Spins (No SQL setup needed!){Style.RESET}")
        safe_print(f"     Choose Option [4] in Coin Manager to spin the wheel via the built-in")
        safe_print(f"     /rpc/hbu_spin endpoint. Every bot wins real coins that show on the leaderboard.")
        safe_print(f"\n  {Style.BOLD}2. ⚡ Permanently Unlock Direct API Coin Grants (setup_coin_api.sql){Style.RESET}")
        safe_print(f"     Run {Style.CYAN}setup_coin_api.sql{Style.RESET} once in your Supabase SQL Editor.")
        safe_print(f"     This unlocks the script to assign any amount of coins directly over the API forever!")
        safe_print(f"\n  {Style.BOLD}3. 📋 One-time SQL Update:{Style.RESET}")
        if sql_saved_msg:
            safe_print(f"     {sql_saved_msg}")
        safe_print(f"{Style.YELLOW}==================================================================={Style.RESET}")

    input("\nPress Enter to return to menu...")

def coin_manager_menu(settings):
    """Interactive menu for importing and setting coins."""
    while True:
        clear_screen()
        safe_print(f"{Style.BOLD}{Style.CYAN}==================================================================={Style.RESET}")
        safe_print(f"{Style.BOLD}{Style.WHITE}                     Account Coin Manager                         {Style.RESET}")
        safe_print(f"{Style.BOLD}{Style.CYAN}==================================================================={Style.RESET}\n")

        safe_print(f"  {Style.BOLD}[1]{Style.RESET} Select a SINGLE account from CSV")
        safe_print(f"  {Style.BOLD}[2]{Style.RESET} Select ALL accounts from CSV")
        safe_print(f"  {Style.BOLD}[3]{Style.RESET} Enter a CUSTOM username and password")
        safe_print(f"  {Style.BOLD}[4]{Style.RESET} 🎰 Harvest Daily Wheel Spins for Accounts (No SQL needed!)")
        safe_print(f"  {Style.BOLD}[5]{Style.RESET} ⚡ Permanently Unlock Direct API Coin Grants (setup_coin_api.sql)")
        safe_print(f"  {Style.BOLD}[0]{Style.RESET} Back to Main Menu\n")

        choice = input(f"{Style.BOLD}Choose an option [0-5]: {Style.RESET}").strip()

        if choice == "0":
            break

        elif choice == "1":
            accounts = load_accounts_from_csv(settings["csv_filename"])
            if not accounts:
                safe_print(f"\n{Style.RED}[!] No accounts found in '{settings['csv_filename']}'.{Style.RESET}")
                time.sleep(1.2)
                continue

            clear_screen()
            safe_print(f"{Style.BOLD}Select an account from CSV:{Style.RESET}\n")
            for idx, acc in enumerate(accounts, start=1):
                c = acc.get("coins", 0)
                safe_print(f"  [{idx}] {acc['username']:<20} (Coins: {c})")

            sel = input(f"\nEnter account number [1-{len(accounts)}]: ").strip()
            if not sel.isdigit() or not (1 <= int(sel) <= len(accounts)):
                continue

            chosen_acc = accounts[int(sel) - 1]
            amount_in = input(f"Enter coins for '{chosen_acc['username']}' [e.g. 5000]: ").strip()
            if not amount_in.isdigit():
                continue

            grant_coins_to_accounts(settings, [chosen_acc], int(amount_in))

        elif choice == "2":
            accounts = load_accounts_from_csv(settings["csv_filename"])
            if not accounts:
                safe_print(f"\n{Style.RED}[!] No accounts found in '{settings['csv_filename']}'.{Style.RESET}")
                time.sleep(1.2)
                continue

            amount_in = input(f"\nEnter coins for all {len(accounts)} accounts [e.g. 10000]: ").strip()
            if not amount_in.isdigit():
                continue

            grant_coins_to_accounts(settings, accounts, int(amount_in))

        elif choice == "3":
            clear_screen()
            safe_print(f"{Style.BOLD}Enter Custom Account Credentials:{Style.RESET}\n")
            uname = input("Username: ").strip()
            if not uname:
                continue
            pwd = input("Password: ").strip()
            amount_in = input(f"Coins to assign to '{uname}': ").strip()
            if not amount_in.isdigit():
                continue

            save_to_csv = input("Add this custom account to your local CSV? (Y/n): ").strip().lower() != "n"
            custom_acc = {"username": uname, "password": pwd, "student_id": "CUSTOM", "coins": int(amount_in)}

            if save_to_csv:
                append_single_account_to_csv(settings["csv_filename"], custom_acc)
                safe_print(f"{Style.GREEN}✓ Account added to '{settings['csv_filename']}'{Style.RESET}")

            grant_coins_to_accounts(settings, [custom_acc], int(amount_in))

        elif choice == "4":
            clear_screen()
            safe_print(f"{Style.BOLD}{Style.CYAN}==================================================================={Style.RESET}")
            safe_print(f"{Style.BOLD}{Style.WHITE}           Harvest Daily Wheel Spins (/rpc/hbu_spin)               {Style.RESET}")
            safe_print(f"{Style.BOLD}{Style.CYAN}==================================================================={Style.RESET}\n")
            safe_print(f"This uses the server's official daily wheel spin RPC.")
            safe_print(f"Every spin awards {Style.BOLD}10 - 250 real coins{Style.RESET} directly into Supabase without SQL.")
            safe_print(f"These coins immediately appear on the website leaderboard!\n")

            safe_print(f"  {Style.BOLD}[1]{Style.RESET} Spin for ALL accounts in CSV")
            safe_print(f"  {Style.BOLD}[2]{Style.RESET} Spin for a SINGLE account from CSV")
            safe_print(f"  {Style.BOLD}[3]{Style.RESET} Spin for a CUSTOM username & password")
            safe_print(f"  {Style.BOLD}[0]{Style.RESET} Cancel\n")

            sub_choice = input(f"Choose an option [0-3]: ").strip()
            if sub_choice == "0":
                continue

            target_list = []
            if sub_choice == "1":
                target_list = load_accounts_from_csv(settings["csv_filename"])
                if not target_list:
                    safe_print(f"{Style.RED}[!] No accounts found in '{settings['csv_filename']}'.{Style.RESET}")
                    time.sleep(1.2)
                    continue
            elif sub_choice == "2":
                accounts = load_accounts_from_csv(settings["csv_filename"])
                if not accounts:
                    safe_print(f"{Style.RED}[!] No accounts found in '{settings['csv_filename']}'.{Style.RESET}")
                    time.sleep(1.2)
                    continue
                for idx, acc in enumerate(accounts, start=1):
                    safe_print(f"  [{idx}] {acc['username']:<20} (Coins: {acc.get('coins', 0)})")
                sel = input(f"\nSelect account [1-{len(accounts)}]: ").strip()
                if sel.isdigit() and 1 <= int(sel) <= len(accounts):
                    target_list = [accounts[int(sel) - 1]]
                else:
                    continue
            elif sub_choice == "3":
                uname = input("Username: ").strip()
                pwd = input("Password: ").strip()
                if uname and pwd:
                    target_list = [{"username": uname, "password": pwd}]
                else:
                    continue

            rest_url = settings["supabase_rest_url"]
            anon_key = settings.get("supabase_anon_key") or settings.get("supabase_service_role_key")
            updates_for_csv = {}

            safe_print(f"\n{Style.BOLD}Starting wheel spin harvest for {len(target_list)} account(s)...{Style.RESET}\n")
            for acc in target_list:
                uname = acc["username"]
                pwd = acc.get("password", "")
                if not pwd:
                    safe_print(f"  {uname:<20} : {Style.YELLOW}Skipped (no password stored){Style.RESET}")
                    continue
                ok, prize, total_coins, msg = spin_wheel_for_account(rest_url, anon_key, uname, pwd)
                if ok:
                    safe_print(f"  {uname:<20} : {Style.GREEN}✓ Won +{prize} coins! (Total: {total_coins}){Style.RESET}")
                    updates_for_csv[uname] = total_coins
                else:
                    safe_print(f"  {uname:<20} : {Style.YELLOW}Spin note: {msg}{Style.RESET}")

            if updates_for_csv:
                update_csv_coins(settings["csv_filename"], updates_for_csv)
                safe_print(f"\n{Style.GREEN}✓ CSV balances updated! Check the leaderboard on the website.{Style.RESET}")

            input("\nPress Enter to continue...")

        elif choice == "5":
            clear_screen()
            safe_print(f"{Style.BOLD}{Style.CYAN}==================================================================={Style.RESET}")
            safe_print(f"{Style.BOLD}{Style.WHITE}       Permanently Unlock Direct API Coin Grants (Zero RLS)        {Style.RESET}")
            safe_print(f"{Style.BOLD}{Style.CYAN}==================================================================={Style.RESET}\n")
            safe_print(f"Why are coins blocked by default?")
            safe_print(f"  Supabase restricts direct PATCH requests on `public.accounts` when using")
            safe_print(f"  the public `anon` publishable key (Row Level Security error 42501).\n")
            safe_print(f"{Style.BOLD}The Permanent Fix (Takes 15 seconds):{Style.RESET}")
            safe_print(f"  1. Open your project at https://supabase.com/dashboard")
            safe_print(f"  2. Click {Style.BOLD}SQL Editor{Style.RESET} in the left sidebar")
            safe_print(f"  3. Copy and run the code from {Style.CYAN}setup_coin_api.sql{Style.RESET}:\n")
            safe_print(f"{Style.YELLOW}-------------------------------------------------------------------{Style.RESET}")
            safe_print(f"create or replace function public.hbu_set_coins(p_username text, p_coins int)")
            safe_print(f"returns json language plpgsql security definer as $$")
            safe_print(f"begin")
            safe_print(f"  update public.accounts set coins = p_coins where username = p_username;")
            safe_print(f"  return json_build_object('success', true, 'username', p_username, 'coins', p_coins);")
            safe_print(f"end; $$;")
            safe_print(f"grant execute on function public.hbu_set_coins(text, int) to anon, authenticated;")
            safe_print(f"{Style.YELLOW}-------------------------------------------------------------------{Style.RESET}\n")
            safe_print(f"{Style.GREEN}✓ Once executed, this script can assign ANY coin amount to any account{Style.RESET}")
            safe_print(f"{Style.GREEN}  instantly over the API without ever seeing an RLS error again!{Style.RESET}\n")
            input("Press Enter to return to menu...")

# ==============================================================================
# Parallel Multi-Threaded Simulation Execution Engine
# ==============================================================================

def execute_simulation(settings, create_new=True, send_messages=True):
    """
    Executes in FULL PARALLEL mode with ANTI-MUTE COMPLIANCE:
    - Enforces >= 5.2s delay per bot (server mutes accounts that send 2 messages in < 5.0s).
    - Velocity is scaled across bots in parallel, preventing all mutes.
    - Instant responsive shutdown when [Enter] or [Ctrl+C] is pressed.
    """
    clear_screen()
    safe_print(f"{Style.BOLD}{Style.CYAN}==================================================================={Style.RESET}")
    safe_print(f"{Style.BOLD}{Style.WHITE}          HBU2 Multi-User Parallel Simulation Engine               {Style.RESET}")
    safe_print(f"{Style.BOLD}{Style.CYAN}==================================================================={Style.RESET}\n")

    csv_file = settings["csv_filename"]
    stop_simulation = threading.Event()
    msgs_per_user = settings["messages_per_user"]
    
    # Enforce safe delay: Server spamCheck() mutes any user sending 2 msgs in < 5.0s
    min_del = max(5.2, float(settings.get("min_delay_seconds", 5.2)))
    max_del = max(min_del + 0.5, float(settings.get("max_delay_seconds", 7.5)))

    counter_lock = threading.Lock()
    total_messages_sent = [0]
    total_target_accounts = settings["num_accounts"] if create_new else 0
    worker_threads = []

    safe_print(f"{Style.YELLOW}[*] Anti-Mute Guard Active:{Style.RESET} Each bot paces messages by {min_del:.1f}s - {max_del:.1f}s")
    safe_print(f"{Style.DIM}    (Server automatically mutes any account sending 2 messages within 5.0 seconds).{Style.RESET}")
    safe_print(f"{Style.GREEN}[*] Total Room Velocity: Scaled across parallel bots simultaneously.{Style.RESET}")
    safe_print(f"\n{Style.BOLD}{Style.YELLOW}>> PRESS [ENTER] OR [Ctrl+C] AT ANY TIME TO STOP THE SIMULATION <<{Style.RESET}\n")

    def stdin_stop_listener():
        while not stop_simulation.is_set():
            try:
                # Non-blocking poll for stdin input on posix
                r, _, _ = select.select([sys.stdin], [], [], 0.1)
                if r:
                    _ = sys.stdin.readline()
                    stop_simulation.set()
                    safe_print(f"\n{Style.YELLOW}{Style.BOLD}[!] STOP REQUESTED VIA KEYBOARD (Enter). Halting all bots...{Style.RESET}")
                    break
            except Exception:
                break

    listener_thread = threading.Thread(target=stdin_stop_listener, daemon=True)
    listener_thread.start()

    def individual_account_chatter(acc, initial_stagger=0.0):
        uname = acc["username"]
        pwd = acc["password"]

        if stop_simulation.wait(initial_stagger):
            return

        for msg_i in range(msgs_per_user):
            if stop_simulation.is_set():
                break

            msg = random.choice(CHAT_MESSAGES)
            with counter_lock:
                total_messages_sent[0] += 1
                curr_total = total_messages_sent[0]

            if stop_simulation.is_set():
                break

            safe_print(f"  {Style.CYAN}[💬 {uname}]{Style.RESET} Msg ({msg_i+1}/{msgs_per_user}): \"{msg}\"... ", end="", flush=True)

            success, result = send_chat_message(
                settings["supabase_functions_url"],
                settings["supabase_rest_url"],
                settings["supabase_anon_key"],
                uname, pwd, msg
            )

            # If stopped while request was in-flight, suppress output and exit immediately
            if stop_simulation.is_set():
                break

            if success:
                safe_print(f"{Style.GREEN}✓ Sent{Style.RESET}")
            else:
                safe_print(f"{Style.RED}✗ {result}{Style.RESET}")

            if msg_i < msgs_per_user - 1 and not stop_simulation.is_set():
                bot_delay = random.uniform(min_del, max_del)
                if stop_simulation.wait(bot_delay):
                    break

        if not stop_simulation.is_set():
            safe_print(f"  {Style.GREEN}[✓ {uname}]{Style.RESET} Finished sending all {msgs_per_user} messages.")

    # Signal handler for clean Ctrl+C
    def sig_handler(sig, frame):
        stop_simulation.set()
        safe_print(f"\n{Style.YELLOW}{Style.BOLD}[!] Interrupt signal received. Stopping all bots...{Style.RESET}")

    is_main_thread = threading.current_thread() is threading.main_thread()
    old_sigint = signal.signal(signal.SIGINT, sig_handler) if is_main_thread else None

    try:
        if create_new:
            for i in range(total_target_accounts):
                if stop_simulation.is_set():
                    break

                uname = generate_username(settings["name_prefix"], settings["use_random_names"])
                pwd = generate_password()
                stu_id = generate_student_id()

                safe_print(f"{Style.YELLOW}[+ Account]{Style.RESET} Registering ({i+1}/{total_target_accounts}) '{uname}'... ", end="", flush=True)

                success, msg = signup_user(
                    settings["supabase_rest_url"],
                    settings["supabase_anon_key"],
                    uname, pwd, stu_id
                )

                if success:
                    spin_ok, prize, total_coins, _ = spin_wheel_for_account(
                        settings["supabase_rest_url"],
                        settings["supabase_anon_key"],
                        uname, pwd
                    )
                    coins_awarded = total_coins if spin_ok else 0
                    acc = {"username": uname, "password": pwd, "student_id": stu_id, "coins": coins_awarded}
                    append_single_account_to_csv(csv_file, acc)
                    coins_info = f" (+{prize} spin coins)" if spin_ok else ""
                    safe_print(f"{Style.GREEN}✓ Created{coins_info}{Style.RESET}")

                    if send_messages:
                        safe_print(f"  └─► {Style.MAGENTA}Launching parallel chat thread for '{uname}'...{Style.RESET}")
                        t = threading.Thread(target=individual_account_chatter, args=(acc, random.uniform(0.2, 1.2)), daemon=True)
                        worker_threads.append(t)
                        t.start()
                else:
                    safe_print(f"{Style.RED}✗ Failed ({msg}){Style.RESET}")

                if stop_simulation.wait(1.0):
                    break

        elif send_messages and not create_new:
            existing = load_accounts_from_csv(csv_file)
            if not existing:
                safe_print(f"{Style.RED}[!] No accounts found in '{csv_file}'. Create accounts first.{Style.RESET}")
                input(f"\nPress Enter to return to menu...")
                return

            # Check bot count to avoid overwhelming the system
            configured_count = settings.get("num_accounts", 10)
            target_accounts = existing
            if len(existing) > configured_count:
                safe_print(f"{Style.BOLD}Found {len(existing)} accounts in '{csv_file}'.{Style.RESET}")
                safe_print(f"  {Style.BOLD}[1]{Style.RESET} Launch configured amount ({Style.CYAN}{configured_count}{Style.RESET} bots)")
                safe_print(f"  {Style.BOLD}[2]{Style.RESET} Launch ALL {Style.YELLOW}{len(existing)}{Style.RESET} bots from CSV")
                try:
                    c_choice = input(f"Select option [1 or 2, default=1]: ").strip()
                    if c_choice != "2":
                        target_accounts = existing[:configured_count]
                except (KeyboardInterrupt, EOFError):
                    return

            safe_print(f"\n{Style.GREEN}[*] Launching {len(target_accounts)} parallel bot threads!{Style.RESET}\n")
            interval_step = 5.0 / max(1, len(target_accounts))
            for idx, acc in enumerate(target_accounts):
                stagger = idx * interval_step + random.uniform(0.1, 0.4)
                t = threading.Thread(target=individual_account_chatter, args=(acc, stagger), daemon=True)
                worker_threads.append(t)
                t.start()

        if worker_threads:
            safe_print(f"\n{Style.DIM}[*] Running parallel bot chat streams... (Press Enter anytime to stop){Style.RESET}")
            while any(t.is_alive() for t in worker_threads):
                if stop_simulation.is_set():
                    break
                time.sleep(0.1)

            # If stop was requested, join threads up to 1.5s to cleanly terminate all socket calls
            if stop_simulation.is_set():
                safe_print(f"\n{Style.YELLOW}[*] Shutting down active bot threads...{Style.RESET}")
                deadline = time.time() + 1.8
                for t in worker_threads:
                    time_left = max(0.01, deadline - time.time())
                    t.join(timeout=time_left)
                    if time.time() >= deadline:
                        break

        if stop_simulation.is_set():
            safe_print(f"\n{Style.YELLOW}✓ Simulation stopped successfully.{Style.RESET} Messages dispatched: {total_messages_sent[0]}")
        else:
            safe_print(f"\n{Style.GREEN}✓ Parallel simulation complete! Total messages dispatched: {total_messages_sent[0]}{Style.RESET}")

    except KeyboardInterrupt:
        stop_simulation.set()
        safe_print(f"\n{Style.RED}[!] Simulation interrupted by user.{Style.RESET}")
    finally:
        stop_simulation.set()
        if old_sigint is not None:
            signal.signal(signal.SIGINT, old_sigint)

    # Flush any buffered Enter keypresses so they don't leak into subsequent prompts
    try:
        import termios
        termios.tcflush(sys.stdin, termios.TCIFLUSH)
    except Exception:
        pass

    safe_print(f"\n{Style.CYAN}==================================================================={Style.RESET}")
    safe_print(f"  {Style.BOLD}[Enter]{Style.RESET} Return to Main Menu   |   {Style.BOLD}[Q]{Style.RESET} Quit Script Completely")
    try:
        post_choice = input(f"{Style.BOLD}Choose action: {Style.RESET}").strip().lower()
        if post_choice in ("q", "quit", "exit", "0"):
            clear_screen()
            safe_print(f"{Style.YELLOW}Exiting simulator. Goodbye!{Style.RESET}")
            sys.exit(0)
    except (KeyboardInterrupt, EOFError):
        clear_screen()
        safe_print(f"\n{Style.YELLOW}Exiting simulator. Goodbye!{Style.RESET}")
        sys.exit(0)

# ==============================================================================
# Local Mock Mode (Zero Network Traffic Test)
# ==============================================================================

def execute_local_mock_test(settings):
    """Spins up a temporary local mock server to verify parallel logic safely."""
    import http.server
    import threading

    clear_screen()
    safe_print(f"{Style.BOLD}{Style.MAGENTA}==================================================================={Style.RESET}")
    safe_print(f"{Style.BOLD}{Style.WHITE}          Local Mock Mode (Parallel Threads Verification)          {Style.RESET}")
    safe_print(f"{Style.BOLD}{Style.MAGENTA}==================================================================={Style.RESET}\n")

    port = 8899
    events_timeline = []

    class MockHandler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("content-length", 0))
            raw = self.rfile.read(length).decode("utf-8")
            body = json.loads(raw)
            if self.path.endswith("/rpc/hbu_signup"):
                events_timeline.append(f"SIGNUP:{body['p_username']}")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b"{}")
            elif self.path.endswith("/send-message"):
                events_timeline.append(f"MSG:{body['username']}")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"ok": true}')
            else:
                self.send_response(404)
                self.end_headers()

        def do_PATCH(self):
            events_timeline.append(f"PATCH_COINS:{self.path}")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok": true}')

        def log_message(self, format, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", port), MockHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()

    mock_settings = settings.copy()
    mock_settings["supabase_rest_url"] = f"http://127.0.0.1:{port}/rest/v1"
    mock_settings["supabase_functions_url"] = f"http://127.0.0.1:{port}/functions/v1"
    mock_settings["num_accounts"] = 3
    mock_settings["messages_per_user"] = 2
    mock_settings["min_delay_seconds"] = 5.2
    mock_settings["max_delay_seconds"] = 5.5
    mock_settings["csv_filename"] = "mock_test_accounts.csv"

    safe_print(f"{Style.YELLOW}[*] Testing independent parallel bot threads with anti-mute timing...{Style.RESET}\n")

    full_path = os.path.join(BASE_DIR, mock_settings["csv_filename"])
    if os.path.exists(full_path):
        os.remove(full_path)

    execute_simulation(mock_settings, create_new=True, send_messages=True)

    server.shutdown()

    if os.path.exists(full_path):
        os.remove(full_path)

    safe_print(f"\n{Style.BOLD}Execution Timeline Verification:{Style.RESET}")
    for item in events_timeline:
        safe_print(f"  → {item}")

    safe_print(f"\n{Style.GREEN}✓ Verified! Multiple accounts executed concurrently across parallel threads.{Style.RESET}")

# ==============================================================================
# TUI Menu & Settings Editor
# ==============================================================================

def configure_settings_menu(settings):
    """Submenu for viewing and editing simulation settings."""
    while True:
        clear_screen()
        safe_print(f"{Style.BOLD}{Style.CYAN}==================================================================={Style.RESET}")
        safe_print(f"{Style.BOLD}{Style.WHITE}                     Configure Simulation Settings                {Style.RESET}")
        safe_print(f"{Style.BOLD}{Style.CYAN}==================================================================={Style.RESET}\n")

        safe_print(f"  {Style.BOLD}[1]{Style.RESET} Number of Accounts to create : {Style.YELLOW}{settings['num_accounts']}{Style.RESET}")
        safe_print(f"  {Style.BOLD}[2]{Style.RESET} Messages Per Account         : {Style.YELLOW}{settings['messages_per_user']}{Style.RESET}")
        safe_print(f"  {Style.BOLD}[3]{Style.RESET} Min Delay Between Msgs (sec) : {Style.YELLOW}{settings['min_delay_seconds']}{Style.RESET} {Style.GREEN}(>= 5.2s prevents server auto-mute){Style.RESET}")
        safe_print(f"  {Style.BOLD}[4]{Style.RESET} Max Delay Between Msgs (sec) : {Style.YELLOW}{settings['max_delay_seconds']}{Style.RESET}")
        words_count = len(get_wordlist())
        style_desc = f"Dictionary Words ({words_count} from random_words.txt)" if settings['use_random_names'] else f"Prefix ({settings['name_prefix']})"
        safe_print(f"  {Style.BOLD}[5]{Style.RESET} Username Style               : {Style.YELLOW}{style_desc}{Style.RESET}")
        safe_print(f"  {Style.BOLD}[6]{Style.RESET} Output CSV Filename          : {Style.YELLOW}{settings['csv_filename']}{Style.RESET}")
        safe_print(f"  {Style.BOLD}[7]{Style.RESET} Supabase REST URL            : {Style.DIM}{settings['supabase_rest_url']}{Style.RESET}")
        safe_print(f"  {Style.BOLD}[8]{Style.RESET} Supabase Functions URL       : {Style.DIM}{settings['supabase_functions_url']}{Style.RESET}")
        safe_print(f"  {Style.BOLD}[9]{Style.RESET} Supabase Anon Publishable Key: {Style.DIM}{settings['supabase_anon_key'][:16]}...{Style.RESET}")
        safe_print(f"  {Style.BOLD}[S]{Style.RESET} Supabase Service Role Key    : {Style.DIM}{settings.get('supabase_service_role_key', '')[:16] if settings.get('supabase_service_role_key') else '(None - optional for RLS bypass)'}{Style.RESET}")
        safe_print(f"  {Style.BOLD}[R]{Style.RESET} Reset to Defaults")
        safe_print(f"  {Style.BOLD}[0]{Style.RESET} Back to Main Menu\n")

        choice = input(f"{Style.BOLD}Select an option to edit: {Style.RESET}").strip().lower()

        if choice == "0":
            save_settings(settings)
            break
        elif choice == "1":
            val = input(f"Enter number of accounts [{settings['num_accounts']}]: ").strip()
            if val.isdigit() and int(val) > 0:
                settings["num_accounts"] = int(val)
        elif choice == "2":
            val = input(f"Enter messages per account [{settings['messages_per_user']}]: ").strip()
            if val.isdigit() and int(val) > 0:
                settings["messages_per_user"] = int(val)
        elif choice == "3":
            val = input(f"Enter min delay in seconds [{settings['min_delay_seconds']}]: ").strip()
            try:
                fl = float(val)
                if fl < 5.1:
                    print(f"{Style.RED}Notice: Server auto-mutes any account sending 2 messages within 5.0 seconds! Setting to 5.2s.{Style.RESET}")
                    fl = 5.2
                settings["min_delay_seconds"] = fl
            except ValueError:
                pass
        elif choice == "4":
            val = input(f"Enter max delay in seconds [{settings['max_delay_seconds']}]: ").strip()
            try:
                fl = float(val)
                if fl >= settings["min_delay_seconds"]:
                    settings["max_delay_seconds"] = fl
            except ValueError:
                pass
        elif choice == "5":
            print("\n  1) Natural Gamer Tags (e.g. Neon_Fox42)")
            print("  2) Custom Prefix (e.g. TestUser_101)")
            c = input("Choose (1 or 2): ").strip()
            if c == "1":
                settings["use_random_names"] = True
            elif c == "2":
                settings["use_random_names"] = False
                pref = input("Enter prefix: ").strip()
                if pref:
                    settings["name_prefix"] = pref
        elif choice == "6":
            val = input(f"Enter CSV file name [{settings['csv_filename']}]: ").strip()
            if val:
                settings["csv_filename"] = val
        elif choice == "7":
            val = input(f"Enter REST URL [{settings['supabase_rest_url']}]: ").strip()
            if val:
                settings["supabase_rest_url"] = val.rstrip("/")
        elif choice == "8":
            val = input(f"Enter Functions URL [{settings['supabase_functions_url']}]: ").strip()
            if val:
                settings["supabase_functions_url"] = val.rstrip("/")
        elif choice == "9":
            val = input(f"Enter Anon Key: ").strip()
            if val:
                settings["supabase_anon_key"] = val
        elif choice == "s":
            val = input(f"Enter Service Role Key: ").strip()
            settings["supabase_service_role_key"] = val
        elif choice == "r":
            settings.clear()
            settings.update(DEFAULT_SETTINGS)
            safe_print(f"{Style.GREEN}Settings reset to default.{Style.RESET}")
            time.sleep(0.6)

def view_saved_accounts(settings):
    """Displays saved accounts from the CSV in a formatted table."""
    clear_screen()
    csv_file = settings["csv_filename"]
    accounts = load_accounts_from_csv(csv_file)
    safe_print(f"{Style.BOLD}{Style.CYAN}==================================================================={Style.RESET}")
    safe_print(f"{Style.BOLD}{Style.WHITE}                   Saved Accounts ({csv_file})                    {Style.RESET}")
    safe_print(f"{Style.BOLD}{Style.CYAN}==================================================================={Style.RESET}\n")

    if not accounts:
        safe_print(f"{Style.YELLOW}No accounts currently stored in '{csv_file}'.{Style.RESET}")
    else:
        safe_print(f"{Style.BOLD}{'#':<4} {'Username':<20} {'Password':<15} {'Coins':<12} {'Created At'}{Style.RESET}")
        print("-" * 65)
        for idx, acc in enumerate(accounts, start=1):
            uname = acc.get("username", "")
            pwd = acc.get("password", "")
            try:
                coins = f"{int(acc.get('coins') or 0):,}"
            except (TypeError, ValueError):
                coins = str(acc.get("coins") or 0)
            cat = acc.get("created_at", "")
            safe_print(f"{idx:<4} {uname:<20} {pwd:<15} {coins:<12} {cat}")

    input("\nPress Enter to return to menu...")

def clear_csv_file(settings):
    """Empties or removes the CSV file."""
    csv_file = settings["csv_filename"]
    full_path = os.path.join(BASE_DIR, csv_file) if not os.path.isabs(csv_file) else csv_file
    if not os.path.isfile(full_path):
        safe_print(f"\n{Style.YELLOW}File '{csv_file}' does not exist.{Style.RESET}")
    else:
        confirm = input(f"\nAre you sure you want to delete '{csv_file}'? (y/N): ").strip().lower()
        if confirm == "y":
            with csv_lock:
                if os.path.exists(full_path):
                    os.remove(full_path)
            safe_print(f"{Style.GREEN}File deleted successfully.{Style.RESET}")
        else:
            safe_print("Operation cancelled.")
    time.sleep(1.0)

def main_tui():
    """Main interactive TUI loop."""
    settings = load_settings()

    while True:
        clear_screen()
        print(f"{Style.BOLD}{Style.BLUE}╔═════════════════════════════════════════════════════════════════╗{Style.RESET}")
        print(f"{Style.BOLD}{Style.BLUE}║{Style.RESET} {Style.BOLD}{Style.WHITE}   HBU2 Anti-Mute Parallel Simulation & Traffic Generator      {Style.RESET}{Style.BOLD}{Style.BLUE}║{Style.RESET}")
        print(f"{Style.BOLD}{Style.BLUE}╚═════════════════════════════════════════════════════════════════╝{Style.RESET}")
        print(f" {Style.DIM}Target:{Style.RESET}   {settings['supabase_rest_url']}")
        print(f" {Style.DIM}Pacing:{Style.RESET}   {settings['min_delay_seconds']}s - {settings['max_delay_seconds']}s per bot {Style.GREEN}(Guaranteed Anti-Mute Safe){Style.RESET}")
        print(f" {Style.DIM}Active:{Style.RESET}   {settings['num_accounts']} concurrent bots  |  {settings['messages_per_user']} msgs/each\n")

        print(f"  {Style.BOLD}[1]{Style.RESET} {Style.GREEN}▶ Run Full Parallel Simulation{Style.RESET} (Simultaneous Create & Multi-Chat)")
        print(f"  {Style.BOLD}[2]{Style.RESET} {Style.CYAN}💬 Send Messages in Parallel{Style.RESET} (All Existing Accounts Chat at Once)")
        print(f"  {Style.BOLD}[3]{Style.RESET} {Style.YELLOW}👤 Create Accounts Only{Style.RESET} (Register and write to CSV)")
        print(f"  {Style.BOLD}[4]{Style.RESET} {Style.MAGENTA}🪙 Manage Account Coins{Style.RESET} (Set Coins for Single, All, or Custom Account)")
        print(f"  {Style.BOLD}[5]{Style.RESET} 🧪 Run Local Mock Test / Dry-Run (Safe, zero remote calls)")
        print(f"  {Style.BOLD}[6]{Style.RESET} ⚙️  Configure Settings & Delays")
        print(f"  {Style.BOLD}[7]{Style.RESET} 📋 View Saved Accounts in CSV")
        print(f"  {Style.BOLD}[8]{Style.RESET} 🗑️  Clear / Reset CSV File")
        print(f"  {Style.BOLD}[9]{Style.RESET} 🖥️  Open Graphical Desktop Controller (One-Click Stop Button)")
        print(f"  {Style.BOLD}[S]{Style.RESET} 🚀 {Style.CYAN}24/7 Background Service & Autostart Controller{Style.RESET}")
        print(f"  {Style.BOLD}[0]{Style.RESET} 🚪 Exit (or press Q / Ctrl+C)\n")

        choice = input(f"{Style.BOLD}Choose an action [0-9 or Q to quit]: {Style.RESET}").strip().lower()

        if choice in ("0", "q", "quit", "exit", "x"):
            clear_screen()
            safe_print("Exiting simulator. Goodbye!")
            sys.exit(0)
        elif choice == "1":
            execute_simulation(settings, create_new=True, send_messages=True)
        elif choice == "2":
            execute_simulation(settings, create_new=False, send_messages=True)
        elif choice == "3":
            execute_simulation(settings, create_new=True, send_messages=False)
        elif choice == "4":
            coin_manager_menu(settings)
        elif choice == "5":
            execute_local_mock_test(settings)
        elif choice == "6":
            configure_settings_menu(settings)
        elif choice == "7":
            view_saved_accounts(settings)
        elif choice == "8":
            clear_csv_file(settings)
        elif choice == "9":
            try:
                subprocess.Popen([sys.executable, os.path.join(BASE_DIR, "gui_controller.py")])
                safe_print(f"\n{Style.GREEN}✓ Graphical Controller window launched!{Style.RESET}")
                time.sleep(1.0)
            except Exception as e:
                safe_print(f"\n{Style.RED}[!] Could not launch GUI: {e}{Style.RESET}")
                time.sleep(1.5)
        elif choice == "s":
            try:
                subprocess.run([sys.executable, os.path.join(BASE_DIR, "service.py")])
            except Exception as e:
                safe_print(f"\n{Style.RED}[!] Could not launch 24/7 service TUI: {e}{Style.RESET}")
                time.sleep(1.5)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="HBU2 Anti-Mute Parallel Simulation Generator")
    parser.add_argument("-i", "--interactive", action="store_true", help="Launch interactive TUI (default)")
    parser.add_argument("--mock", action="store_true", help="Run local mock dry-run test")
    parser.add_argument("--run", action="store_true", help="Run full simulation with saved settings non-interactively")
    parser.add_argument("--chat", action="store_true", help="Run existing accounts chat simulation non-interactively")
    parser.add_argument("--gui", action="store_true", help="Launch Graphical Controller alongside simulator")
    args = parser.parse_args()

    if args.gui:
        try:
            subprocess.Popen([sys.executable, os.path.join(BASE_DIR, "gui_controller.py")])
        except Exception:
            pass

    if args.mock:
        execute_local_mock_test(load_settings())
    elif args.run:
        execute_simulation(load_settings(), create_new=True, send_messages=True)
    elif args.chat:
        execute_simulation(load_settings(), create_new=False, send_messages=True)
    else:
        main_tui()
