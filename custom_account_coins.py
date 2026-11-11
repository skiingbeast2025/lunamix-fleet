#!/usr/bin/env python3
"""
HBU2 Custom Account Coin Manager
Standalone utility to assign custom coin amounts to specific accounts.
Supports custom username & password, optional registration, CSV synchronization,
and direct Supabase REST PATCH / SQL fallback.
"""

import os
import sys
import json
import time
import ssl
import csv
import argparse
import tempfile
import urllib.request
import urllib.error

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SETTINGS_FILE = os.path.join(BASE_DIR, "hbu_sim_settings.json")
DEFAULT_CSV = os.path.join(BASE_DIR, "simulated_accounts.csv")

# ==============================================================================
# Terminal Color & Styling Utilities
# ==============================================================================

class Style:
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
    os.system("clear" if os.name == "posix" else "cls")

def safe_print(msg=""):
    try:
        print(msg)
    except UnicodeEncodeError:
        print(msg.encode("ascii", "ignore").decode("ascii"))

import builtins

def safe_input(prompt=""):
    """Crash-proof user input handling EOF and interrupts."""
    try:
        return builtins.input(prompt)
    except EOFError:
        return ""
    except KeyboardInterrupt:
        safe_print(f"\n{Style.YELLOW}[!] Process cancelled by user. Exiting...{Style.RESET}")
        sys.exit(0)

input = safe_input

# ==============================================================================
# Configuration Management
# ==============================================================================

DEFAULT_SETTINGS = {
    "supabase_rest_url": "https://xvkjvuxhawlljakfkllu.supabase.co/rest/v1",
    "supabase_functions_url": "https://xvkjvuxhawlljakfkllu.supabase.co/functions/v1",
    "supabase_anon_key": "sb_publishable_uE8sH_g3U4uI4K9L708b7w_1vjB5uFw",
    "supabase_service_role_key": "",
    "csv_filename": "simulated_accounts.csv"
}

def load_settings():
    settings = DEFAULT_SETTINGS.copy()
    if os.path.isfile(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    settings.update(data)
        except Exception:
            pass
    return settings

def save_settings(settings):
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2)
    except Exception as e:
        safe_print(f"{Style.RED}[!] Could not save settings: {e}{Style.RESET}")

# ==============================================================================
# Network & Supabase Request Helpers
# ==============================================================================

def get_ssl_context():
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx

def make_http_request(url, method="GET", payload=None, api_key=None, timeout=12):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
        "Origin": "https://rockystudios.github.io",
        "Referer": "https://rockystudios.github.io/HBU2/"
    }
    if api_key:
        headers["apikey"] = api_key
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    ctx = get_ssl_context()
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        return resp.status, resp.read().decode("utf-8", errors="replace")

def register_account(settings, username, password, student_id="CUSTOM"):
    """Optionally registers the account via hbu_signup RPC if it doesn't exist."""
    url = f"{settings['supabase_rest_url'].rstrip('/')}/rpc/hbu_signup"
    payload = {
        "p_username": username,
        "p_password": password,
        "p_student_id": student_id
    }
    key = settings.get("supabase_anon_key") or settings.get("supabase_service_role_key")
    try:
        status, _ = make_http_request(url, method="POST", payload=payload, api_key=key)
        return status in (200, 204), "Registration successful."
    except urllib.error.HTTPError as err:
        body = err.read().decode("utf-8", errors="replace")
        return False, f"HTTP {err.code}: {body}"
    except Exception as e:
        return False, str(e)

def spin_wheel_for_account(rest_url, anon_key, username, password):
    """Spins the daily wheel via /rpc/hbu_spin to claim real database coins with anon key."""
    url = f"{rest_url.rstrip('/')}/rpc/hbu_spin"
    payload = {"p_username": username, "p_password": password}
    try:
        status, resp = make_http_request(url, method="POST", payload=payload, api_key=anon_key)
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

# ==============================================================================
# Local CSV Helpers
# ==============================================================================

def update_or_append_csv(csv_filename, username, password, coins, student_id="CUSTOM"):
    """Updates an existing account row or appends a new row atomically in the CSV file."""
    full_path = os.path.join(BASE_DIR, csv_filename) if not os.path.isabs(csv_filename) else csv_filename
    accounts = []
    found = False
    fieldnames = ["username", "password", "student_id", "coins", "created_at"]

    if os.path.isfile(full_path) and os.path.getsize(full_path) > 0:
        try:
            with open(full_path, "r", encoding="utf-8-sig", newline="") as f:
                reader = csv.DictReader(f)
                if reader.fieldnames:
                    for col in reader.fieldnames:
                        if col and col not in fieldnames:
                            fieldnames.append(col)
                for row in reader:
                    row.pop(None, None)
                    row_user = (row.get("username") or "").strip()
                    if row_user == username.strip():
                        row["coins"] = str(coins)
                        if password:
                            row["password"] = password
                        found = True
                    accounts.append(row)
        except Exception as e:
            safe_print(f"{Style.RED}[!] Error reading CSV: {e}{Style.RESET}")
            return False

    if not found:
        accounts.append({
            "username": username,
            "password": password,
            "student_id": student_id,
            "coins": str(coins),
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S")
        })

    parent_dir = os.path.dirname(os.path.abspath(full_path))
    os.makedirs(parent_dir, exist_ok=True)
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(prefix=f".{os.path.basename(full_path)}.", suffix=".tmp", dir=parent_dir)
        file_obj = None
        try:
            file_obj = os.fdopen(fd, "w", newline="", encoding="utf-8")
            with file_obj as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
                writer.writeheader()
                for acc in accounts:
                    acc.pop(None, None)
                    row = {col: acc.get(col, "") for col in fieldnames}
                    if not row.get("student_id"):
                        row["student_id"] = "CUSTOM"
                    if row.get("coins", "") == "":
                        row["coins"] = "0"
                    writer.writerow(row)
                f.flush()
                os.fsync(f.fileno())
        except Exception:
            if file_obj is None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            raise
        os.replace(tmp_path, full_path)
        return True
    except Exception as e:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        safe_print(f"{Style.RED}[!] Error writing CSV: {e}{Style.RESET}")
        return False

# ==============================================================================
# Core Coin Granting Engine
# ==============================================================================

def assign_custom_coins(settings, username, password, coins_amount, service_role_key=None, save_csv=True):
    """
    Assigns coins to the custom user account.
    Checks custom RPC hbu_set_coins, REST PATCH, and provides helpful bypass guidance.
    """
    safe_print(f"\n{Style.BOLD}Target User    :{Style.RESET} {Style.CYAN}{username}{Style.RESET}")
    safe_print(f"{Style.BOLD}Target Coins   :{Style.RESET} {Style.YELLOW}{coins_amount:,}{Style.RESET}")

    active_key = service_role_key or settings.get("supabase_service_role_key") or settings.get("supabase_anon_key")
    rest_url = settings["supabase_rest_url"].rstrip('/')

    # 1. Attempt custom RPC hbu_set_coins (if setup_coin_api.sql was installed)
    rpc_url = f"{rest_url}/rpc/hbu_set_coins"
    rpc_payload = {"p_username": username, "p_coins": coins_amount}
    try:
        status, _ = make_http_request(rpc_url, method="POST", payload=rpc_payload, api_key=active_key)
        if status in (200, 204):
            safe_print(f"{Style.GREEN}{Style.BOLD}✓ SUCCESS:{Style.RESET} {Style.GREEN}Coins updated to {coins_amount:,} for '{username}'! (via hbu_set_coins API){Style.RESET}")
            if save_csv:
                update_or_append_csv(settings["csv_filename"], username, password, coins_amount)
                safe_print(f"{Style.GREEN}✓ Synchronized balance to local '{settings['csv_filename']}'{Style.RESET}")
            return True
    except urllib.error.HTTPError:
        pass
    except Exception:
        pass

    # 2. Fall back to direct REST PATCH
    url = f"{rest_url}/accounts?username=eq.{urllib.request.quote(username)}"
    payload = {"coins": coins_amount}

    success = False
    rls_blocked = False

    safe_print(f"{Style.DIM}[*] Sending PATCH request to Supabase accounts table...{Style.RESET}")

    try:
        status, _ = make_http_request(url, method="PATCH", payload=payload, api_key=active_key)
        if status in (200, 204):
            safe_print(f"{Style.GREEN}{Style.BOLD}✓ SUCCESS:{Style.RESET} {Style.GREEN}Coins updated to {coins_amount:,} for '{username}'!{Style.RESET}")
            success = True
        else:
            safe_print(f"{Style.RED}✗ Server returned HTTP {status}{Style.RESET}")
    except urllib.error.HTTPError as err:
        err_body = err.read().decode("utf-8", errors="replace")
        if err.code in (401, 403) or "42501" in err_body or "violates row-level security policy" in err_body.lower():
            rls_blocked = True
        else:
            safe_print(f"{Style.RED}✗ HTTP {err.code}: {err_body}{Style.RESET}")
    except Exception as err:
        safe_print(f"{Style.RED}✗ Connection Error: {err}{Style.RESET}")

    # Synchronize to CSV if requested
    if save_csv:
        csv_ok = update_or_append_csv(settings["csv_filename"], username, password, coins_amount)
        if csv_ok:
            safe_print(f"{Style.GREEN}✓ Synchronized balance to local '{settings['csv_filename']}'{Style.RESET}")

    # If blocked by Supabase Row Level Security (RLS)
    if rls_blocked:
        safe_print(f"\n{Style.YELLOW}==================================================================={Style.RESET}")
        safe_print(f"{Style.BOLD}{Style.YELLOW}           NOTICE: Supabase Row Level Security (RLS)              {Style.RESET}")
        safe_print(f"{Style.YELLOW}==================================================================={Style.RESET}")
        safe_print("Supabase default security rules block anonymous (`anon`) publishable keys")
        safe_print("from directly editing the `coins` column in `public.accounts`.")
        safe_print(f"\n{Style.BOLD}Ways around this:{Style.RESET}")
        safe_print(f"  {Style.BOLD}1. 🎰 Use Daily Wheel Spins (No SQL setup needed!){Style.RESET}")
        safe_print(f"     Run with {Style.CYAN}--spin{Style.RESET} to spin the wheel via {Style.BOLD}/rpc/hbu_spin{Style.RESET}.")
        safe_print(f"     Every spin awards 10 - 250 real coins directly to Supabase with the anon key!")
        safe_print(f"\n  {Style.BOLD}2. ⚡ Permanently Unlock Direct API Coin Grants (setup_coin_api.sql){Style.RESET}")
        safe_print(f"     Run {Style.CYAN}setup_coin_api.sql{Style.RESET} once in your Supabase SQL Editor.")
        safe_print(f"     This unlocks the script to set any coin balance directly over the API forever!")
        safe_print(f"\n  {Style.BOLD}3. 📋 Direct SQL Update:{Style.RESET}")
        safe_print(f"     {Style.BOLD}{Style.CYAN}UPDATE public.accounts SET coins = {coins_amount} WHERE username = '{username}';{Style.RESET}\n")
        safe_print(f"{Style.YELLOW}==================================================================={Style.RESET}")

    return success or rls_blocked

def execute_spin_action(settings, username, password, save_csv=True):
    """Spins the daily wheel for an account and prints results."""
    safe_print(f"\n{Style.BOLD}Target User    :{Style.RESET} {Style.CYAN}{username}{Style.RESET}")
    safe_print(f"{Style.DIM}[*] Requesting wheel spin from /rpc/hbu_spin...{Style.RESET}")
    rest_url = settings["supabase_rest_url"]
    anon_key = settings.get("supabase_anon_key") or settings.get("supabase_service_role_key")
    ok, prize, total_coins, msg = spin_wheel_for_account(rest_url, anon_key, username, password)
    if ok:
        safe_print(f"{Style.GREEN}{Style.BOLD}✓ WHEEL SPIN SUCCESS!{Style.RESET}")
        safe_print(f"  {Style.BOLD}Prize Won    :{Style.RESET} {Style.YELLOW}+{prize} coins{Style.RESET}")
        safe_print(f"  {Style.BOLD}Total Coins  :{Style.RESET} {Style.CYAN}{total_coins:,}{Style.RESET}")
        safe_print(f"  {Style.DIM}(These coins are live in Supabase and visible on the website leaderboard!){Style.RESET}")
        if save_csv:
            update_or_append_csv(settings["csv_filename"], username, password, total_coins)
            safe_print(f"{Style.GREEN}✓ Synchronized balance to local '{settings['csv_filename']}'{Style.RESET}")
        return True
    else:
        safe_print(f"{Style.YELLOW}[!] Wheel spin response: {msg}{Style.RESET}")
        return False

# ==============================================================================
# Interactive Screen
# ==============================================================================

def interactive_loop():
    settings = load_settings()

    while True:
        clear_screen()
        safe_print(f"{Style.BOLD}{Style.MAGENTA}╔═════════════════════════════════════════════════════════════════╗{Style.RESET}")
        safe_print(f"{Style.BOLD}{Style.MAGENTA}║{Style.RESET} {Style.BOLD}{Style.WHITE}          HBU2 Custom Account Coin Manager (Standalone)          {Style.RESET}{Style.BOLD}{Style.MAGENTA}║{Style.RESET}")
        safe_print(f"{Style.BOLD}{Style.MAGENTA}╚═════════════════════════════════════════════════════════════════╝{Style.RESET}")
        safe_print(f" {Style.DIM}Target:{Style.RESET}  {settings['supabase_rest_url']}")
        key_label = "Service Role Key (Full Bypass)" if settings.get("supabase_service_role_key") else "Anon Key (Publishable)"
        safe_print(f" {Style.DIM}Auth:{Style.RESET}    {key_label}")
        safe_print(f" {Style.DIM}CSV:{Style.RESET}     {settings['csv_filename']}\n")

        safe_print(f"  {Style.BOLD}[1]{Style.RESET} Assign custom coin amount (e.g. 50,000)")
        safe_print(f"  {Style.BOLD}[2]{Style.RESET} 🎰 Spin daily wheel (/rpc/hbu_spin - no SQL needed!)")
        safe_print(f"  {Style.BOLD}[3]{Style.RESET} Register new account (/rpc/hbu_signup)")
        safe_print(f"  {Style.BOLD}[4]{Style.RESET} ⚡ Permanently unlock direct API coin grants (setup_coin_api.sql)")
        safe_print(f"  {Style.BOLD}[0]{Style.RESET} Exit (or press Q / Ctrl+C)\n")

        choice = input(f"{Style.BOLD}Choose an option [0-4 or Q to quit]: {Style.RESET}").strip().lower()

        if choice in ("0", "q", "quit", "exit", "x"):
            safe_print("\nExiting. Goodbye!")
            break

        elif choice == "1":
            safe_print(f"\n{Style.BOLD}Assign Custom Coins:{Style.RESET}")
            uname = input(f" {Style.BOLD}Username{Style.RESET}: ").strip()
            if not uname:
                continue
            pwd = input(f" {Style.BOLD}Password{Style.RESET} (optional): ").strip()
            coins_input = input(f" {Style.BOLD}Coins to Assign{Style.RESET} [e.g. 10000]: ").strip()
            if not coins_input.isdigit():
                safe_print(f"{Style.RED}[!] Invalid coins amount. Must be an integer.{Style.RESET}")
                time.sleep(1.2)
                continue
            coins_val = int(coins_input)
            save_csv = input(f" Save/update in '{settings['csv_filename']}'? (Y/n): ").strip().lower() != "n"
            assign_custom_coins(settings, uname, pwd, coins_val, save_csv=save_csv)
            input("\nPress Enter to continue...")

        elif choice == "2":
            safe_print(f"\n{Style.BOLD}Spin Daily Wheel (/rpc/hbu_spin):{Style.RESET}")
            uname = input(f" {Style.BOLD}Username{Style.RESET}: ").strip()
            if not uname:
                continue
            pwd = input(f" {Style.BOLD}Password{Style.RESET}: ").strip()
            if not pwd:
                safe_print(f"{Style.RED}[!] Password is required for wheel spin.{Style.RESET}")
                time.sleep(1.2)
                continue
            save_csv = input(f" Save/update in '{settings['csv_filename']}'? (Y/n): ").strip().lower() != "n"
            execute_spin_action(settings, uname, pwd, save_csv=save_csv)
            input("\nPress Enter to continue...")

        elif choice == "3":
            safe_print(f"\n{Style.BOLD}Register New Account:{Style.RESET}")
            uname = input(f" {Style.BOLD}Username{Style.RESET}: ").strip()
            if not uname:
                continue
            pwd = input(f" {Style.BOLD}Password{Style.RESET}: ").strip() or "HbuPass123!"
            stu_id = input(f" {Style.BOLD}Student ID{Style.RESET} [e.g. STU12345]: ").strip() or "CUSTOM"
            safe_print(f"{Style.DIM}[*] Attempting signup for '{uname}'...{Style.RESET}")
            reg_ok, reg_msg = register_account(settings, uname, pwd, stu_id)
            if reg_ok:
                safe_print(f"{Style.GREEN}✓ Registered successfully!{Style.RESET}")
                save_csv = input(f" Save to '{settings['csv_filename']}'? (Y/n): ").strip().lower() != "n"
                if save_csv:
                    update_or_append_csv(settings["csv_filename"], uname, pwd, 0, stu_id)
                    safe_print(f"{Style.GREEN}✓ Saved to '{settings['csv_filename']}'{Style.RESET}")
            else:
                safe_print(f"{Style.YELLOW}[i] Registration note: {reg_msg}{Style.RESET}")
            input("\nPress Enter to continue...")

        elif choice == "4":
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
# Local Mock Mode for Testing
# ==============================================================================

def execute_mock_test():
    import http.server
    import threading

    port = 8898
    received_requests = []

    class MockHandler(http.server.BaseHTTPRequestHandler):
        def do_PATCH(self):
            received_requests.append(f"PATCH:{self.path}")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok": true}')

        def do_POST(self):
            received_requests.append(f"POST:{self.path}")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            if "/rpc/hbu_spin" in self.path:
                self.wfile.write(b'{"prize": 75, "coins": 75}')
            elif "/rpc/hbu_set_coins" in self.path:
                self.wfile.write(b'{"success": true}')
            else:
                self.wfile.write(b'{}')

        def log_message(self, format, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", port), MockHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()

    test_settings = {
        "supabase_rest_url": f"http://127.0.0.1:{port}/rest/v1",
        "supabase_functions_url": f"http://127.0.0.1:{port}/functions/v1",
        "supabase_anon_key": "test_anon_key",
        "supabase_service_role_key": "test_service_key",
        "csv_filename": "mock_custom_coins_test.csv"
    }

    test_csv_path = os.path.join(BASE_DIR, test_settings["csv_filename"])
    if os.path.exists(test_csv_path):
        os.remove(test_csv_path)

    safe_print(f"{Style.BOLD}Running Custom Coins Mock Test...{Style.RESET}")
    assign_custom_coins(test_settings, "TestCustomUser", "secret123", 75000, save_csv=True)
    execute_spin_action(test_settings, "TestCustomUser", "secret123", save_csv=True)

    server.shutdown()

    # Verify CSV
    csv_exists = os.path.exists(test_csv_path)
    if csv_exists:
        with open(test_csv_path, "r") as f:
            content = f.read()
        os.remove(test_csv_path)
        safe_print(f"{Style.GREEN}✓ CSV verified:\n{content.strip()}{Style.RESET}")

    safe_print(f"{Style.GREEN}✓ Mock Server Requests: {received_requests}{Style.RESET}")
    safe_print(f"{Style.GREEN}✓ Custom Account Coin Manager Mock Test Passed!{Style.RESET}")

# ==============================================================================
# CLI Entrypoint
# ==============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="HBU2 Standalone Custom Account Coin Manager",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Interactive mode:
  python3 custom_account_coins.py

  # Direct CLI assignment:
  python3 custom_account_coins.py -u MyPlayer -c 50000 -p MyPassword123!

  # Spin daily wheel directly (awards 10-250 coins with anon key):
  python3 custom_account_coins.py -u MyPlayer -p MyPassword123! --spin

  # Direct CLI assignment with service role key:
  python3 custom_account_coins.py -u MyPlayer -c 50000 -k <SERVICE_ROLE_KEY>

  # Local mock test:
  python3 custom_account_coins.py --mock
"""
    )
    parser.add_argument("-u", "--username", help="Target username")
    parser.add_argument("-p", "--password", default="", help="Password (optional, saved to CSV)")
    parser.add_argument("-c", "--coins", type=int, help="Number of coins to assign")
    parser.add_argument("--spin", action="store_true", help="Spin daily wheel for account via /rpc/hbu_spin")
    parser.add_argument("-k", "--service-key", help="Supabase service_role key (bypasses RLS)")
    parser.add_argument("--register", action="store_true", help="Attempt registration via hbu_signup if account is new")
    parser.add_argument("--no-csv", action="store_true", help="Do not update or save to local CSV file")
    parser.add_argument("--mock", action="store_true", help="Run local mock dry-run test")

    args = parser.parse_args()

    if args.mock:
        execute_mock_test()
        return

    settings = load_settings()

    if args.username and (args.coins is not None or args.spin):
        if args.register:
            safe_print(f"[*] Attempting registration for '{args.username}'...")
            reg_ok, msg = register_account(settings, args.username, args.password or "HbuPass123!")
            safe_print(f"[*] Registration result: {msg}")

        if args.spin:
            execute_spin_action(
                settings=settings,
                username=args.username,
                password=args.password,
                save_csv=not args.no_csv
            )
        else:
            assign_custom_coins(
                settings=settings,
                username=args.username,
                password=args.password,
                coins_amount=args.coins,
                service_role_key=args.service_key,
                save_csv=not args.no_csv
            )
    else:
        interactive_loop()

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        safe_print(f"\n{Style.RED}[!] Process cancelled by user.{Style.RESET}")
        sys.exit(0)
