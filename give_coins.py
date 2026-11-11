#!/usr/bin/env python3
"""
==============================================================================
HBU2 Custom Coin Granter (give_coins.py)
==============================================================================
Standalone external script to input a custom username, password, and desired
coin amount to apply coins to an account.

Usage:
  Interactive Mode:
    python3 give_coins.py

  Command Line Mode:
    python3 give_coins.py -u MyPlayer -p MyPassword123! -c 50000
    python3 give_coins.py -u MyPlayer -p MyPassword123! --spin
==============================================================================
"""

import os
import sys
import json
import time
import ssl
import csv
import getpass
import argparse
import tempfile
import urllib.request
import urllib.error
import urllib.parse

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SETTINGS_FILE = os.path.join(BASE_DIR, "hbu_sim_settings.json")
DEFAULT_CSV = os.path.join(BASE_DIR, "simulated_accounts.csv")
SQL_OUTPUT_FILE = os.path.join(BASE_DIR, "update_coins.sql")

DEFAULT_SETTINGS = {
    "supabase_rest_url": "https://xvkjvuxhawlljakfkllu.supabase.co/rest/v1",
    "supabase_functions_url": "https://xvkjvuxhawlljakfkllu.supabase.co/functions/v1",
    "supabase_anon_key": "sb_publishable_uyxd5lS77wdXpD56QF_lgg_zaujLjZZ",
    "supabase_service_role_key": "",
    "csv_filename": "simulated_accounts.csv"
}

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

def print_banner():
    banner = f"""
{Color.CYAN}{Color.BOLD}╔════════════════════════════════════════════════════════════════╗
║                   HBU2 CUSTOM COIN GRANTER                     ║
║         Set Custom Coins for Any Username & Password           ║
╚════════════════════════════════════════════════════════════════╝{Color.RESET}
"""
    try:
        print(banner)
    except Exception:
        print("\n=== HBU2 CUSTOM COIN GRANTER ===\n")

def safe_print(msg=""):
    try:
        print(msg)
    except UnicodeEncodeError:
        print(msg.encode("ascii", "ignore").decode("ascii"))

# ==============================================================================
# Network Helper
# ==============================================================================

def make_request(url, method="GET", payload=None, api_key=None, timeout=10):
    """Executes an HTTP request to Supabase REST / RPC endpoints."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept": "application/json",
        "Origin": "https://rockystudios.github.io",
        "Referer": "https://rockystudios.github.io/HBU2/"
    }
    if api_key:
        headers["apikey"] = api_key
        headers["Authorization"] = f"Bearer {api_key}"

    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload).encode("utf-8")

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        body = resp.read().decode("utf-8", errors="replace")
        return resp.status, body

def load_settings():
    """Loads configuration settings from hbu_sim_settings.json or defaults."""
    settings = DEFAULT_SETTINGS.copy()
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
                if isinstance(loaded, dict):
                    settings.update(loaded)
        except Exception as e:
            safe_print(f"{Color.YELLOW}[!] Warning: Could not read settings file: {e}{Color.RESET}")
    return settings

# ==============================================================================
# Supabase Operations
# ==============================================================================

def fetch_wallet_balance(rest_url, anon_key, username, password):
    """
    Fetches the current live coin balance from Supabase via /rpc/hbu_wallet.
    Returns (success: bool, coins: int, message: str)
    """
    url = f"{rest_url.rstrip('/')}/rpc/hbu_wallet"
    payload = {"p_username": username, "p_password": password}
    try:
        status, body = make_request(url, method="POST", payload=payload, api_key=anon_key, timeout=8)
        data = json.loads(body)
        coins = data.get("coins", 0)
        return True, coins, "Success"
    except urllib.error.HTTPError as err:
        err_body = err.read().decode("utf-8", errors="replace")
        try:
            err_json = json.loads(err_body)
            msg = err_json.get("message") or err_json.get("error") or err_body
        except Exception:
            msg = err_body
        return False, 0, f"HTTP {err.code}: {msg}"
    except Exception as e:
        return False, 0, str(e)

def spin_daily_wheel(rest_url, anon_key, username, password):
    """
    Spins the daily wheel via /rpc/hbu_spin for guaranteed free coins.
    Returns (success: bool, prize: int, total_coins: int, message: str)
    """
    url = f"{rest_url.rstrip('/')}/rpc/hbu_spin"
    payload = {"p_username": username, "p_password": password}
    try:
        status, body = make_request(url, method="POST", payload=payload, api_key=anon_key, timeout=8)
        data = json.loads(body)
        prize = data.get("prize", 0)
        total = data.get("coins", 0)
        return True, prize, total, "Wheel spun successfully!"
    except urllib.error.HTTPError as err:
        err_body = err.read().decode("utf-8", errors="replace")
        try:
            err_json = json.loads(err_body)
            msg = err_json.get("message") or err_json.get("error") or err_body
        except Exception:
            msg = err_body
        return False, 0, 0, f"{msg}"
    except Exception as e:
        return False, 0, 0, str(e)

def load_accounts_from_csv(csv_filepath):
    """Loads all accounts from the CSV file."""
    if not os.path.isabs(csv_filepath):
        csv_filepath = os.path.join(BASE_DIR, csv_filepath)
    if not os.path.isfile(csv_filepath):
        return []
    accounts = []
    try:
        with open(csv_filepath, "r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                uname = (row.get("username") or "").strip()
                pwd = (row.get("password") or "").strip()
                if uname and pwd:
                    accounts.append({
                        "username": uname,
                        "password": pwd,
                        "student_id": (row.get("student_id") or "").strip(),
                        "coins": (row.get("coins") or "0").strip()
                    })
    except (OSError, csv.Error, UnicodeDecodeError):
        return []
    return accounts

def find_account_in_csv(csv_filepath, username):
    """Finds an account by username (case-insensitive) in the CSV."""
    accounts = load_accounts_from_csv(csv_filepath)
    for acc in accounts:
        if acc["username"].lower() == username.strip().lower():
            return acc
    return None

def update_csv_account(csv_filepath, username, password, coins):
    """Synchronizes the account balance into simulated_accounts.csv atomically."""
    if not os.path.isabs(csv_filepath):
        csv_filepath = os.path.join(BASE_DIR, csv_filepath)

    rows = []
    fieldnames = ["username", "password", "student_id", "coins", "status"]
    found = False

    if os.path.isfile(csv_filepath) and os.path.getsize(csv_filepath) > 0:
        try:
            with open(csv_filepath, "r", encoding="utf-8-sig", newline="") as f:
                reader = csv.DictReader(f)
                if reader.fieldnames:
                    fieldnames = [fn for fn in reader.fieldnames if fn]
                    if "coins" not in fieldnames:
                        fieldnames.append("coins")
                for r in reader:
                    r.pop(None, None)
                    row_user = (r.get("username") or "").strip()
                    if row_user.lower() == username.strip().lower():
                        r["coins"] = str(coins)
                        if password:
                            r["password"] = password
                        found = True
                    rows.append(r)
        except Exception:
            return False

    if not found:
        new_row = {
            "username": username,
            "password": password,
            "student_id": "",
            "coins": str(coins),
            "status": "Active"
        }
        for fn in fieldnames:
            if fn not in new_row:
                new_row[fn] = ""
        rows.append(new_row)

    parent_dir = os.path.dirname(os.path.abspath(csv_filepath))
    os.makedirs(parent_dir, exist_ok=True)
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(prefix=f".{os.path.basename(csv_filepath)}.", suffix=".tmp", dir=parent_dir)
        file_obj = None
        try:
            file_obj = os.fdopen(fd, "w", encoding="utf-8", newline="")
            with file_obj as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
                writer.writeheader()
                writer.writerows(rows)
                f.flush()
                os.fsync(f.fileno())
        except Exception:
            if file_obj is None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            raise
        os.replace(tmp_path, csv_filepath)
        return True
    except Exception:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        return False

def apply_coins(settings, username, password, target_coins, service_key=None):
    """
    Applies custom coins to the target user account using:
      1. Direct custom RPC: /rpc/hbu_set_coins (if setup_coin_api.sql installed)
      2. Direct REST PATCH: /accounts?username=eq.<user> (if service key or RLS allows)
      3. Fallback: Generates SQL statement and saves to update_coins.sql
    """
    rest_url = settings["supabase_rest_url"].rstrip('/')
    active_key = service_key or settings.get("supabase_service_role_key") or settings.get("supabase_anon_key")

    safe_print(f"\n{Color.BOLD}[1/3] Checking account credentials & initial balance...{Color.RESET}")
    has_wallet, initial_coins, wallet_msg = fetch_wallet_balance(rest_url, active_key, username, password)

    if has_wallet:
        safe_print(f"  {Color.GREEN}✓ Verified account credentials!{Color.RESET}")
        safe_print(f"  Current Live Balance : {Color.CYAN}{initial_coins:,} coins{Color.RESET}")
    else:
        safe_print(f"  {Color.YELLOW}[!] Note on wallet check: {wallet_msg}{Color.RESET}")
        initial_coins = 0

    safe_print(f"\n{Color.BOLD}[2/3] Applying {target_coins:,} coins to '{username}'...{Color.RESET}")

    # Method 1: Custom RPC hbu_set_coins
    rpc_url = f"{rest_url}/rpc/hbu_set_coins"
    rpc_payload = {"p_username": username, "p_coins": target_coins}
    try:
        status, body = make_request(rpc_url, method="POST", payload=rpc_payload, api_key=active_key, timeout=8)
        if status in (200, 204):
            safe_print(f"  {Color.GREEN}{Color.BOLD}✓ SUCCESS via hbu_set_coins API!{Color.RESET}")
            safe_print(f"  {Color.GREEN}Coins set to {target_coins:,} directly in Supabase!{Color.RESET}")
            update_csv_account(settings["csv_filename"], username, password, target_coins)
            safe_print(f"  {Color.GREEN}✓ Synchronized balance to local '{settings['csv_filename']}'{Color.RESET}")
            return True, target_coins
    except Exception:
        pass

    # Method 2: Direct REST PATCH
    patch_url = f"{rest_url}/accounts?username=eq.{urllib.parse.quote(username)}"
    patch_payload = {"coins": target_coins}
    rls_blocked = False

    try:
        status, body = make_request(patch_url, method="PATCH", payload=patch_payload, api_key=active_key, timeout=8)
        if status in (200, 204):
            safe_print(f"  {Color.GREEN}{Color.BOLD}✓ SUCCESS via REST PATCH!{Color.RESET}")
            safe_print(f"  {Color.GREEN}Coins updated to {target_coins:,} for '{username}'!{Color.RESET}")
            update_csv_account(settings["csv_filename"], username, password, target_coins)
            safe_print(f"  {Color.GREEN}✓ Synchronized balance to local '{settings['csv_filename']}'{Color.RESET}")
            return True, target_coins
    except urllib.error.HTTPError as err:
        err_body = err.read().decode("utf-8", errors="replace")
        if err.code in (401, 403) or "42501" in err_body or "row-level security" in err_body.lower():
            rls_blocked = True
        else:
            safe_print(f"  {Color.RED}✗ PATCH Error HTTP {err.code}: {err_body}{Color.RESET}")
    except Exception as e:
        safe_print(f"  {Color.RED}✗ Connection error during PATCH: {e}{Color.RESET}")

    # Synchronize to CSV locally
    update_csv_account(settings["csv_filename"], username, password, target_coins)
    safe_print(f"  {Color.GREEN}✓ Local balance updated in '{settings['csv_filename']}'{Color.RESET}")

    # Method 3: Supabase RLS instructions & 1-click SQL generation
    safe_print(f"\n{Color.BOLD}[3/3] Supabase Row Level Security (RLS) Status:{Color.RESET}")

    # Save exact SQL command to update_coins.sql
    sql_cmd = f"UPDATE public.accounts SET coins = {target_coins} WHERE username = '{username}';"
    try:
        with open(SQL_OUTPUT_FILE, "w", encoding="utf-8") as f:
            f.write(f"-- Run this in the Supabase SQL Editor to apply coins immediately:\n")
            f.write(f"{sql_cmd}\n")
    except Exception:
        pass

    safe_print(f"\n{Color.YELLOW}──────────────────────────────────────────────────────────────────{Color.RESET}")
    safe_print(f"{Color.BOLD}Supabase RLS is currently protecting the accounts table from direct edit by anon key.{Color.RESET}")
    safe_print(f"\n{Color.BOLD}To apply {target_coins:,} coins to the live server right now:{Color.RESET}")
    safe_print(f"  1. Copy and paste this single SQL line into your Supabase SQL Editor:")
    safe_print(f"     {Color.CYAN}{Color.BOLD}{sql_cmd}{Color.RESET}")
    safe_print(f"     (Saved to: {SQL_OUTPUT_FILE})")
    safe_print(f"\n  2. Or run {Color.CYAN}setup_coin_api.sql{Color.RESET} once in your Supabase SQL Editor.")
    safe_print(f"     This creates the {Color.BOLD}hbu_set_coins{Color.RESET} RPC so this script can set")
    safe_print(f"     any coin amount directly over the API without SQL every time!")
    safe_print(f"{Color.YELLOW}──────────────────────────────────────────────────────────────────{Color.RESET}\n")

    return False, initial_coins

def apply_coins_to_all(settings, target_coins, service_key=None):
    """Applies coins to all accounts in the CSV file."""
    accounts = load_accounts_from_csv(settings["csv_filename"])
    if not accounts:
        safe_print(f"{Color.RED}[!] No accounts found in '{settings['csv_filename']}'.{Color.RESET}")
        return False

    safe_print(f"\n{Color.BOLD}Targeting ALL {len(accounts):,} accounts from '{settings['csv_filename']}'...{Color.RESET}")
    safe_print(f"Target Coins per Account: {Color.YELLOW}{target_coins:,}{Color.RESET}")

    # Synchronize all accounts in local CSV
    for acc in accounts:
        update_csv_account(settings["csv_filename"], acc["username"], acc["password"], target_coins)
    safe_print(f"{Color.GREEN}✓ Updated all {len(accounts):,} balances in '{settings['csv_filename']}'{Color.RESET}")

    # Generate multi-user SQL query
    user_list = ", ".join(f"'{acc['username']}'" for acc in accounts)
    sql_cmd = f"UPDATE public.accounts SET coins = {target_coins} WHERE username IN ({user_list});"

    try:
        with open(SQL_OUTPUT_FILE, "w", encoding="utf-8") as f:
            f.write("-- Run this in the Supabase SQL Editor to update all accounts simultaneously:\n")
            f.write(f"{sql_cmd}\n")
    except Exception:
        pass

    safe_print(f"\n{Color.YELLOW}──────────────────────────────────────────────────────────────────{Color.RESET}")
    safe_print(f"{Color.BOLD}Bulk SQL statement generated for all {len(accounts):,} accounts!{Color.RESET}")
    safe_print(f"  Saved to: {Color.CYAN}{SQL_OUTPUT_FILE}{Color.RESET}")
    safe_print(f"\nTo apply to the live server right now:")
    safe_print(f"  Paste the contents of {Color.CYAN}update_coins.sql{Color.RESET} into your Supabase SQL Editor.")
    safe_print(f"{Color.YELLOW}──────────────────────────────────────────────────────────────────{Color.RESET}\n")
    return True

def pick_account_from_csv(settings):
    """Interactive account selector from the CSV file."""
    csv_file = settings["csv_filename"]
    accounts = load_accounts_from_csv(csv_file)
    if not accounts:
        safe_print(f"{Color.YELLOW}[!] No accounts found in '{csv_file}'. Switching to manual entry.{Color.RESET}")
        return None

    safe_print(f"\n{Color.CYAN}{Color.BOLD}Loaded {len(accounts):,} accounts from '{csv_file}'{Color.RESET}")

    page_size = 10
    page = 0
    total_pages = (len(accounts) + page_size - 1) // page_size

    while True:
        start_idx = page * page_size
        end_idx = min(start_idx + page_size, len(accounts))
        safe_print(f"\n{Color.BOLD}Accounts [{start_idx + 1} - {end_idx} of {len(accounts)}]:{Color.RESET}")
        for i in range(start_idx, end_idx):
            acc = accounts[i]
            coins_str = f"{int(acc.get('coins', 0)):,}" if str(acc.get('coins', '')).isdigit() else str(acc.get('coins', '0'))
            safe_print(f"  {Color.BOLD}[{i + 1:3d}]{Color.RESET} {Color.CYAN}{acc['username']:<22}{Color.RESET} | Coins: {Color.YELLOW}{coins_str:>12}{Color.RESET}")

        prompt_opts = []
        if total_pages > 1:
            if page < total_pages - 1:
                prompt_opts.append("'n' for next page")
            if page > 0:
                prompt_opts.append("'p' for prev page")
        prompt_opts.append("type name to search")
        opts_str = f" ({', '.join(prompt_opts)})" if prompt_opts else ""

        try:
            val = input(f"\nEnter account number [1-{len(accounts)}]{opts_str}: ").strip()
        except (KeyboardInterrupt, EOFError):
            safe_print("\nCancelled.")
            sys.exit(0)

        if not val:
            continue

        if val.lower() == 'n' and page < total_pages - 1:
            page += 1
            continue
        elif val.lower() == 'p' and page > 0:
            page -= 1
            continue
        elif val.isdigit():
            idx = int(val) - 1
            if 0 <= idx < len(accounts):
                chosen = accounts[idx]
                safe_print(f"\n{Color.GREEN}✓ Selected: {Color.BOLD}{chosen['username']}{Color.RESET}")
                return chosen
            else:
                safe_print(f"{Color.RED}[!] Invalid number. Please enter 1 to {len(accounts)}.{Color.RESET}")
        else:
            # Search by username substring
            matches = [acc for acc in accounts if val.lower() in acc["username"].lower()]
            if len(matches) == 1:
                chosen = matches[0]
                safe_print(f"\n{Color.GREEN}✓ Matched: {Color.BOLD}{chosen['username']}{Color.RESET}")
                return chosen
            elif len(matches) > 1:
                safe_print(f"\n{Color.BOLD}Found {len(matches)} matches:{Color.RESET}")
                for mi, m in enumerate(matches[:15], 1):
                    m_coins = f"{int(m.get('coins', 0)):,}" if str(m.get('coins', '')).isdigit() else str(m.get('coins', '0'))
                    safe_print(f"  [{mi}] {Color.CYAN}{m['username']:<20}{Color.RESET} (Coins: {m_coins})")
                try:
                    sub_sel = input(f"Choose match [1-{min(len(matches), 15)}]: ").strip()
                    if sub_sel.isdigit() and 1 <= int(sub_sel) <= min(len(matches), 15):
                        chosen = matches[int(sub_sel) - 1]
                        safe_print(f"\n{Color.GREEN}✓ Selected: {Color.BOLD}{chosen['username']}{Color.RESET}")
                        return chosen
                except (KeyboardInterrupt, EOFError):
                    safe_print("\nCancelled.")
                    sys.exit(0)
            else:
                safe_print(f"{Color.RED}[!] No accounts matching '{val}'.{Color.RESET}")

# ==============================================================================
# Interactive & CLI Entry Points
# ==============================================================================

def parse_arguments():
    parser = argparse.ArgumentParser(
        description="Assign a custom amount of coins to any HBU2 account.",
        formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("-u", "--username", help="Account username (if password omitted, looked up in CSV)")
    parser.add_argument("-p", "--password", help="Account password")
    parser.add_argument("-c", "--coins", type=int, help="Desired coin amount (e.g. 50000)")
    parser.add_argument("-k", "--key", help="Optional Supabase service_role key to bypass RLS")
    parser.add_argument("--spin", action="store_true", help="Spin the daily wheel directly for free live coins")
    parser.add_argument("--csv-index", type=int, help="Select account from CSV by 1-based index (e.g. 1)")
    parser.add_argument("--all", action="store_true", help="Apply coins to ALL accounts in the CSV")
    parser.add_argument("--list", action="store_true", help="List accounts from the CSV and exit")
    return parser.parse_args()

def interactive_prompt():
    print_banner()
    settings = load_settings()

    safe_print(f"{Color.BOLD}How would you like to select the account?{Color.RESET}")
    safe_print(f"  {Color.BOLD}[1]{Color.RESET} 📋 Choose an account from CSV ({settings['csv_filename']})")
    safe_print(f"  {Color.BOLD}[2]{Color.RESET} ✍️  Enter custom username & password manually")
    safe_print(f"  {Color.BOLD}[3]{Color.RESET} 👥 Apply to ALL accounts in CSV")

    try:
        source_choice = input(f"\n{Color.BOLD}Select [1, 2, or 3, default=1]: {Color.RESET}").strip()
    except (KeyboardInterrupt, EOFError):
        safe_print("\nCancelled.")
        sys.exit(0)

    if source_choice == "3":
        # All accounts
        while True:
            try:
                coins_in = input(f"\n{Color.BOLD}Enter Desired Coin Amount for ALL accounts [e.g. 50000]: {Color.RESET}").strip()
            except (KeyboardInterrupt, EOFError):
                safe_print("\nCancelled.")
                sys.exit(0)
            if coins_in.isdigit():
                coins_amount = int(coins_in)
                break
            safe_print(f"{Color.RED}[!] Please enter a valid positive integer.{Color.RESET}")
        apply_coins_to_all(settings, coins_amount)
        return

    username = ""
    password = ""

    if source_choice == "2":
        # Manual entry
        safe_print(f"\n{Color.BOLD}Enter the account details below:{Color.RESET}\n")
        while True:
            try:
                username = input(f"{Color.BOLD}Enter Username: {Color.RESET}").strip()
            except (KeyboardInterrupt, EOFError):
                safe_print("\nCancelled.")
                sys.exit(0)
            if username:
                break
            safe_print(f"{Color.RED}[!] Username cannot be empty.{Color.RESET}")

        if sys.stdin.isatty():
            try:
                password = getpass.getpass(f"{Color.BOLD}Enter Password: {Color.RESET}").strip()
                if not password:
                    password = input(f"{Color.BOLD}Enter Password (visible): {Color.RESET}").strip()
            except Exception:
                password = input(f"{Color.BOLD}Enter Password: {Color.RESET}").strip()
        else:
            password = input().strip()
    else:
        # Pick from CSV (default)
        chosen = pick_account_from_csv(settings)
        if not chosen:
            # Fall back to manual entry
            safe_print(f"\n{Color.BOLD}Enter username & password manually:{Color.RESET}")
            username = input(f"{Color.BOLD}Enter Username: {Color.RESET}").strip()
            password = input(f"{Color.BOLD}Enter Password: {Color.RESET}").strip()
        else:
            username = chosen["username"]
            password = chosen["password"]

    # Action choice
    safe_print(f"\n{Color.BOLD}Choose action for '{username}':{Color.RESET}")
    safe_print(f"  {Color.BOLD}[1]{Color.RESET} 💰 Set custom coin amount (e.g. 50,000)")
    safe_print(f"  {Color.BOLD}[2]{Color.RESET} 🎰 Spin daily wheel (/rpc/hbu_spin - guaranteed free live coins)")

    try:
        choice = input(f"\n{Color.BOLD}Select [1 or 2, default=1]: {Color.RESET}").strip()
    except (KeyboardInterrupt, EOFError):
        safe_print("\nCancelled.")
        sys.exit(0)

    if choice == "2":
        safe_print(f"\n{Color.DIM}[*] Requesting wheel spin from /rpc/hbu_spin...{Color.RESET}")
        ok, prize, total, msg = spin_daily_wheel(
            settings["supabase_rest_url"],
            settings["supabase_anon_key"],
            username,
            password
        )
        if ok:
            safe_print(f"\n{Color.GREEN}{Color.BOLD}✓ WHEEL SPIN SUCCESS!{Color.RESET}")
            safe_print(f"  {Color.BOLD}Prize Won    :{Color.RESET} {Color.YELLOW}+{prize} coins{Color.RESET}")
            safe_print(f"  {Color.BOLD}Total Coins  :{Color.RESET} {Color.CYAN}{total:,}{Color.RESET}")
            update_csv_account(settings["csv_filename"], username, password, total)
        else:
            safe_print(f"\n{Color.YELLOW}[!] Wheel spin response: {msg}{Color.RESET}")
        return

    # Coins amount
    while True:
        try:
            coins_in = input(f"\n{Color.BOLD}Enter Desired Coin Amount [e.g. 50000]: {Color.RESET}").strip()
        except (KeyboardInterrupt, EOFError):
            safe_print("\nCancelled.")
            sys.exit(0)
        if coins_in.isdigit():
            coins_amount = int(coins_in)
            break
        safe_print(f"{Color.RED}[!] Please enter a valid positive integer.{Color.RESET}")

    apply_coins(settings, username, password, coins_amount)

def main():
    args = parse_arguments()
    settings = load_settings()

    # --list flag: list accounts from CSV
    if args.list:
        accounts = load_accounts_from_csv(settings["csv_filename"])
        safe_print(f"\n{Color.CYAN}{Color.BOLD}Accounts in '{settings['csv_filename']}' ({len(accounts):,} total):{Color.RESET}\n")
        for i, acc in enumerate(accounts[:40], 1):
            coins_str = f"{int(acc.get('coins', 0)):,}" if str(acc.get('coins', '')).isdigit() else str(acc.get('coins', '0'))
            safe_print(f"  {Color.BOLD}[{i:3d}]{Color.RESET} {Color.CYAN}{acc['username']:<22}{Color.RESET} | Coins: {Color.YELLOW}{coins_str:>12}{Color.RESET}")
        if len(accounts) > 40:
            safe_print(f"\n  {Color.DIM}... and {len(accounts) - 40} more accounts.{Color.RESET}")
        return

    # --all flag: apply to all accounts
    if args.all:
        coins = args.coins if args.coins is not None else 50000
        apply_coins_to_all(settings, coins, service_key=args.key)
        return

    # --csv-index flag: select by 1-based index from CSV
    if args.csv_index is not None:
        accounts = load_accounts_from_csv(settings["csv_filename"])
        idx = args.csv_index - 1
        if 0 <= idx < len(accounts):
            acc = accounts[idx]
            username = acc["username"]
            password = acc["password"]
            safe_print(f"Selected CSV account #{args.csv_index}: {Color.CYAN}{username}{Color.RESET}")

            if args.spin:
                ok, prize, total, msg = spin_daily_wheel(
                    settings["supabase_rest_url"],
                    args.key or settings["supabase_anon_key"],
                    username,
                    password
                )
                if ok:
                    safe_print(f"✓ Success! Won +{prize} coins. Total balance: {total:,} coins.")
                    update_csv_account(settings["csv_filename"], username, password, total)
                else:
                    safe_print(f"✗ Wheel spin failed: {msg}")
                return

            coins = args.coins if args.coins is not None else 50000
            apply_coins(settings, username, password, coins, service_key=args.key)
            return
        else:
            safe_print(f"{Color.RED}[!] CSV index #{args.csv_index} is out of range (1 - {len(accounts)}).{Color.RESET}")
            return

    # If username is provided via CLI
    if args.username:
        username = args.username.strip()
        password = (args.password or "").strip()

        # If password was not passed, check if account is in CSV to get password automatically!
        if not password:
            found = find_account_in_csv(settings["csv_filename"], username)
            if found and found.get("password"):
                password = found["password"]
                safe_print(f"{Color.DIM}[*] Automatically retrieved password for '{username}' from '{settings['csv_filename']}'.{Color.RESET}")

        if args.spin:
            safe_print(f"Spinning daily wheel for '{username}'...")
            ok, prize, total, msg = spin_daily_wheel(
                settings["supabase_rest_url"],
                args.key or settings["supabase_anon_key"],
                username,
                password
            )
            if ok:
                safe_print(f"✓ Success! Won +{prize} coins. Total balance: {total:,} coins.")
                update_csv_account(settings["csv_filename"], username, password, total)
            else:
                safe_print(f"✗ Wheel spin failed: {msg}")
            return

        coins = args.coins if args.coins is not None else 50000
        apply_coins(settings, username, password, coins, service_key=args.key)
    else:
        # Interactive mode
        interactive_prompt()

if __name__ == "__main__":
    main()
