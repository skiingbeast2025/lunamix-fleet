#!/usr/bin/env python3
"""
==============================================================================
HBU2 Account Status Tester & Filter (filter_banned_accounts.py)
==============================================================================
Tests every account in simulated_accounts.csv (or any specified CSV) against
the live HBU2 backend. Categorizes each account as:
  - ACTIVE: Login succeeds, no ban or mute active.
  - BANNED: Explicit ban on account (HTTP 403).
  - MUTED: Main chat mute active (HTTP 403).
  - INVALID / PURGED: Credentials failed / account wiped by admin.

Separates unusable accounts into a separate CSV (default: banned_accounts.csv)
and updates simulated_accounts.csv with only verified active accounts.
Always creates a backup (simulated_accounts.csv.bak) before modifying.

Usage:
  python3 filter_banned_accounts.py
  python3 filter_banned_accounts.py --workers 10
  python3 filter_banned_accounts.py --dry-run
  python3 filter_banned_accounts.py --csv custom_accounts.csv --banned-csv banned.csv
==============================================================================
"""

import os
import sys
import csv
import json
import time
import ssl
import shutil
import argparse
import tempfile
import threading
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SETTINGS_FILE = os.path.join(BASE_DIR, "hbu_sim_settings.json")
DEFAULT_INPUT_CSV = os.path.join(BASE_DIR, "simulated_accounts.csv")
DEFAULT_BANNED_CSV = os.path.join(BASE_DIR, "banned_accounts.csv")

DEFAULT_SETTINGS = {
    "supabase_rest_url": "https://xvkjvuxhawlljakfkllu.supabase.co/rest/v1",
    "supabase_functions_url": "https://xvkjvuxhawlljakfkllu.supabase.co/functions/v1",
    "supabase_anon_key": "sb_publishable_uyxd5lS77wdXpD56QF_lgg_zaujLjZZ",
}

# ==============================================================================
# Terminal Colors & Styling
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
    banner = f"""{Color.CYAN}{Color.BOLD}
╔══════════════════════════════════════════════════════════════════════╗
║              HBU2 ACCOUNT VERIFIER & BAN FILTER                      ║
║     Test Accounts, Detect Bans & Purges, Move to Banned CSV          ║
╚══════════════════════════════════════════════════════════════════════╝{Color.RESET}
"""
    try:
        print(banner)
    except Exception:
        print("\n=== HBU2 ACCOUNT VERIFIER & BAN FILTER ===\n")

print_lock = threading.Lock()

def safe_print(msg=""):
    with print_lock:
        try:
            print(msg)
        except UnicodeEncodeError:
            print(msg.encode("ascii", "ignore").decode("ascii"))
        sys.stdout.flush()

# ==============================================================================
# Settings & Network Helpers
# ==============================================================================

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
            safe_print(f"{Color.YELLOW}[!] Warning reading settings: {e}{Color.RESET}")
    return settings

def get_ssl_context():
    """Resilient SSL context fallback."""
    try:
        return ssl.create_default_context()
    except Exception:
        return ssl._create_unverified_context()

def http_post_json(url, payload, api_key, timeout=8):
    """Executes a JSON POST request, returning (status_code, response_data, error_msg)."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "apikey": api_key,
        "Authorization": f"Bearer {api_key}",
        "Origin": "https://rockystudios.github.io",
        "Referer": "https://rockystudios.github.io/HBU2/"
    }
    data = json.dumps(payload).encode("utf-8")
    ctx = get_ssl_context()
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")

    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            try:
                parsed = json.loads(body)
            except Exception:
                parsed = body
            return resp.status, parsed, None
    except urllib.error.HTTPError as err:
        body = err.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(body)
        except Exception:
            parsed = body
        return err.code, parsed, None
    except Exception as exc:
        return None, None, str(exc)

# ==============================================================================
# Account Testing Logic
# ==============================================================================

def test_account(account, rest_url, functions_url, anon_key):
    """
    Tests an account in two phases:
      1. Login test via /rest/v1/rpc/hbu_login.
         - If fails with 'Wrong username or password' -> Status: INVALID (Account wiped or bad credentials).
      2. If login succeeds, probe ban status via /functions/v1/send-message.
         - If 403: Status: BANNED / MUTED with specific server reason.
         - If 400 with 'user does not exist': Status: ACTIVE (Login confirmed, no ban/mute).
    
    Returns a dict with test results.
    """
    username = (account.get("username") or "").strip()
    password = (account.get("password") or "").strip()

    result = {
        "account": account,
        "username": username,
        "password": password,
        "student_id": (account.get("student_id") or "").strip(),
        "coins": (account.get("coins") or "0").strip(),
        "created_at": (account.get("created_at") or "").strip(),
        "status": "UNKNOWN",
        "reason": "",
        "tested_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    }

    if not username or not password:
        result["status"] = "INVALID"
        result["reason"] = "Missing username or password in CSV"
        return result

    # --------------------------------------------------------------------------
    # Phase 1: Test Login via RPC /rpc/hbu_login
    # --------------------------------------------------------------------------
    login_url = f"{rest_url.rstrip('/')}/rpc/hbu_login"
    login_payload = {"p_username": username, "p_password": password}

    retries = 3
    login_status = None
    login_resp = None
    login_err = None

    for attempt in range(retries):
        login_status, login_resp, login_err = http_post_json(
            login_url, login_payload, anon_key, timeout=9
        )
        # If we got a conclusive response (200, 400, 401, 403, 404), exit loop
        if login_status is not None and login_status not in (500, 502, 503, 504):
            break
        # Transient error or statement timeout, wait briefly and retry
        time.sleep(1.0 * (attempt + 1))

    if login_status is None:
        result["status"] = "ERROR"
        result["reason"] = f"Network timeout/error during login test: {login_err}"
        return result

    # Check for invalid credentials / wiped account
    if login_status != 200:
        err_msg = ""
        if isinstance(login_resp, dict):
            err_msg = login_resp.get("message") or login_resp.get("error") or str(login_resp)
        else:
            err_msg = str(login_resp)

        if "Wrong username or password" in err_msg or login_status == 400 or login_status == 401:
            result["status"] = "INVALID"
            result["reason"] = "Wrong username or password (account wiped or credentials reset)"
            return result
        else:
            result["status"] = "ERROR"
            result["reason"] = f"HTTP {login_status}: {err_msg}"
            return result

    # --------------------------------------------------------------------------
    # Phase 2: Login succeeded! Now test ban/mute status via /functions/v1/send-message
    # We probe by attempting to DM a nonexistent dummy user "__ban_check_probe__".
    # If the user is banned, the edge function aborts at line 1546 with HTTP 403 (banned).
    # If the user is clean, it passes the ban check and aborts with HTTP 400 ("user does not exist").
    # This NEVER writes a message to chat or triggers any spam cooldown!
    # --------------------------------------------------------------------------
    probe_url = f"{functions_url.rstrip('/')}/send-message"
    probe_payload = {
        "username": username,
        "password": password,
        "receiver": "__ban_check_probe_user__",
        "message": "ping"
    }

    probe_status = None
    probe_resp = None
    probe_err = None

    for attempt in range(2):
        probe_status, probe_resp, probe_err = http_post_json(
            probe_url, probe_payload, anon_key, timeout=8
        )
        if probe_status is not None and probe_status not in (500, 502, 503, 504):
            break
        time.sleep(1.0)

    if probe_status == 403:
        err_msg = ""
        if isinstance(probe_resp, dict):
            err_msg = probe_resp.get("error") or probe_resp.get("message") or str(probe_resp)
        else:
            err_msg = str(probe_resp)

        if "not authorized to use hbu2 services" in err_msg.lower():
            result["status"] = "ERROR"
            result["reason"] = f"Origin check error: {err_msg}"
            return result
        elif "muted" in err_msg.lower():
            result["status"] = "MUTED"
            result["reason"] = err_msg
        else:
            result["status"] = "BANNED"
            result["reason"] = err_msg
        return result

    elif probe_status == 400:
        err_msg = ""
        if isinstance(probe_resp, dict):
            err_msg = probe_resp.get("error") or probe_resp.get("message") or str(probe_resp)
        else:
            err_msg = str(probe_resp)

        if "does not exist" in err_msg.lower():
            # Passed ban check, stopped at nonexistent DM target
            result["status"] = "ACTIVE"
            result["reason"] = "Account verified active and unbanned"
            return result
        elif "Wrong username or password" in err_msg:
            result["status"] = "INVALID"
            result["reason"] = err_msg
            return result

    # If HTTP 200 or 204 or any other response
    result["status"] = "ACTIVE"
    result["reason"] = "Account verified active and unbanned"
    return result

# ==============================================================================
# CSV File Management
# ==============================================================================

def load_accounts_from_csv(csv_path):
    """Loads accounts from a CSV file into a list of dicts."""
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
                if uname:
                    accounts.append({
                        "username": uname,
                        "password": pwd,
                        "student_id": (row.get("student_id") or row.get("ID") or "").strip(),
                        "coins": (row.get("coins") or row.get("Coins") or "0").strip(),
                        "created_at": (row.get("created_at") or row.get("CreatedAt") or "").strip()
                    })
    except (OSError, csv.Error, UnicodeDecodeError) as exc:
        safe_print(f"{Color.RED}[!] Error reading CSV '{csv_path}': {exc}{Color.RESET}")
        return []
    return accounts

def save_accounts_to_csv(csv_path, accounts):
    """Saves a list of accounts to a CSV file atomically."""
    fieldnames = ["username", "password", "student_id", "coins", "created_at"]
    parent_dir = os.path.dirname(os.path.abspath(csv_path))
    os.makedirs(parent_dir, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=f".{os.path.basename(csv_path)}.", suffix=".tmp", dir=parent_dir)
    file_obj = None
    try:
        file_obj = os.fdopen(fd, "w", encoding="utf-8", newline="")
        with file_obj as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            for acc in accounts:
                writer.writerow({
                    "username": acc.get("username", ""),
                    "password": acc.get("password", ""),
                    "student_id": acc.get("student_id", ""),
                    "coins": acc.get("coins", "0"),
                    "created_at": acc.get("created_at", "")
                })
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, csv_path)
    except Exception:
        if file_obj is None:
            try:
                os.close(fd)
            except OSError:
                pass
        if os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        raise

def append_to_banned_csv(banned_csv_path, results):
    """Appends tested unusable accounts (banned/invalid/muted) to the banned CSV."""
    parent_dir = os.path.dirname(os.path.abspath(banned_csv_path))
    if parent_dir:
        os.makedirs(parent_dir, exist_ok=True)
    file_exists = os.path.isfile(banned_csv_path) and os.path.getsize(banned_csv_path) > 0
    fieldnames = [
        "username", "password", "student_id", "coins",
        "created_at", "status", "reason", "tested_at"
    ]

    existing_usernames = set()
    if file_exists:
        try:
            with open(banned_csv_path, "r", encoding="utf-8-sig", newline="") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    u = (row.get("username") or "").strip().lower()
                    if u:
                        existing_usernames.add(u)
        except Exception:
            pass

    with open(banned_csv_path, "a", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if not file_exists:
            writer.writeheader()

        written_count = 0
        for res in results:
            uname = (res.get("username") or "").strip()
            if not uname:
                continue
            if uname.lower() not in existing_usernames:
                writer.writerow({
                    "username": uname,
                    "password": res.get("password", ""),
                    "student_id": res.get("student_id", ""),
                    "coins": res.get("coins", "0"),
                    "created_at": res.get("created_at", ""),
                    "status": res.get("status", ""),
                    "reason": res.get("reason", ""),
                    "tested_at": res.get("tested_at", "")
                })
                existing_usernames.add(uname.lower())
                written_count += 1

    return written_count

# ==============================================================================
# Main Execution Workflow
# ==============================================================================

def run_filter(input_csv, banned_csv, workers=3, dry_run=False, force_retest=False):
    """Orchestrates testing accounts, skipping already-catalogued ones, and separating banned/invalid ones."""
    print_banner()

    if not os.path.isabs(input_csv):
        input_csv = os.path.join(BASE_DIR, input_csv)
    if not os.path.isabs(banned_csv):
        banned_csv = os.path.join(BASE_DIR, banned_csv)

    settings = load_settings()
    rest_url = settings["supabase_rest_url"]
    functions_url = settings["supabase_functions_url"]
    anon_key = settings["supabase_anon_key"]

    safe_print(f"{Color.CYAN}[i] Loading accounts from: {Color.WHITE}{input_csv}{Color.RESET}")
    accounts = load_accounts_from_csv(input_csv)
    total = len(accounts)

    if total == 0:
        safe_print(f"{Color.RED}[!] No accounts found in {input_csv}. Exiting.{Color.RESET}")
        return

    # Check for already catalogued unusable accounts
    already_banned = set()
    if os.path.isfile(banned_csv) and not force_retest:
        try:
            with open(banned_csv, "r", encoding="utf-8-sig", newline="") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    u = (row.get("username") or "").strip().lower()
                    if u:
                        already_banned.add(u)
        except Exception:
            pass

    to_test = []
    skipped_count = 0
    for acc in accounts:
        if acc["username"].lower() in already_banned:
            skipped_count += 1
        else:
            to_test.append(acc)

    safe_print(f"{Color.CYAN}[i] Total accounts in file : {Color.BOLD}{total}{Color.RESET}")
    if skipped_count > 0:
        safe_print(f"{Color.CYAN}[i] Already verified/banned: {Color.YELLOW}{skipped_count}{Color.RESET}{Color.CYAN} (skipping re-test){Color.RESET}")
        safe_print(f"{Color.CYAN}[i] Remaining to verify    : {Color.BOLD}{len(to_test)}{Color.RESET}")
    safe_print(f"{Color.CYAN}[i] Worker concurrency     : {Color.BOLD}{workers}{Color.RESET} threads")
    safe_print(f"{Color.CYAN}[i] Banned CSV destination : {Color.WHITE}{banned_csv}{Color.RESET}")
    if dry_run:
        safe_print(f"{Color.YELLOW}[!] DRY-RUN MODE: Files will NOT be modified.{Color.RESET}")
    safe_print("-" * 70)

    # Tracking lists
    active_accounts = []
    banned_accounts = []
    invalid_accounts = []
    error_accounts = []

    tested_count = 0
    start_time = time.time()

    if to_test:
        safe_print(f"{Color.CYAN}[*] Starting verification of {len(to_test)} accounts...{Color.RESET}\n")

        # ThreadPool for parallel execution with safe pacing
        def worker_task(idx, acc):
            # Gentle pacing between requests
            time.sleep((idx % workers) * 0.15)
            res = test_account(acc, rest_url, functions_url, anon_key)
            return idx, res

        with ThreadPoolExecutor(max_workers=workers) as executor:
            future_map = {
                executor.submit(worker_task, i, acc): (i, acc)
                for i, acc in enumerate(to_test, 1)
            }

            for future in as_completed(future_map):
                i, acc = future_map[future]
                try:
                    idx, res = future.result()
                except Exception as exc:
                    res = {
                        "account": acc,
                        "username": (acc.get("username") or "").strip(),
                        "password": (acc.get("password") or "").strip(),
                        "student_id": (acc.get("student_id") or "").strip(),
                        "coins": (acc.get("coins") or "0").strip(),
                        "created_at": (acc.get("created_at") or "").strip(),
                        "status": "ERROR",
                        "reason": f"Worker exception: {exc}",
                        "tested_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
                    }
                tested_count += 1
                status = res["status"]
                uname = res["username"]
                reason = res["reason"]

                if status == "ACTIVE":
                    active_accounts.append(res["account"])
                    safe_print(
                        f"[{tested_count}/{len(to_test)}] {Color.GREEN}✓ ACTIVE{Color.RESET}   : "
                        f"{Color.BOLD}{uname:<22}{Color.RESET} | {reason}"
                    )
                elif status == "BANNED":
                    banned_accounts.append(res)
                    safe_print(
                        f"[{tested_count}/{len(to_test)}] {Color.RED}✗ BANNED{Color.RESET}   : "
                        f"{Color.BOLD}{uname:<22}{Color.RESET} | {reason}"
                    )
                elif status == "MUTED":
                    banned_accounts.append(res)
                    safe_print(
                        f"[{tested_count}/{len(to_test)}] {Color.MAGENTA}🔇 MUTED{Color.RESET}    : "
                        f"{Color.BOLD}{uname:<22}{Color.RESET} | {reason}"
                    )
                elif status == "INVALID":
                    invalid_accounts.append(res)
                    safe_print(
                        f"[{tested_count}/{len(to_test)}] {Color.YELLOW}⚠ INVALID{Color.RESET}  : "
                        f"{Color.BOLD}{uname:<22}{Color.RESET} | {reason}"
                    )
                else:
                    error_accounts.append(res)
                    safe_print(
                        f"[{tested_count}/{len(to_test)}] {Color.RED}? ERROR{Color.RESET}    : "
                        f"{Color.BOLD}{uname:<22}{Color.RESET} | {reason}"
                    )

    elapsed = time.time() - start_time
    rate = tested_count / elapsed if elapsed > 0 else 0

    # ==========================================================================
    # Summary Table
    # ==========================================================================
    safe_print("\n" + "=" * 70)
    safe_print(f"{Color.CYAN}{Color.BOLD}                  VERIFICATION COMPLETE SUMMARY                   {Color.RESET}")
    safe_print("=" * 70)
    safe_print(f"  Total Accounts in Input : {Color.BOLD}{total}{Color.RESET}")
    safe_print(f"  Previously Catalogued   : {Color.YELLOW}{skipped_count}{Color.RESET}")
    safe_print(f"  Tested in This Run      : {Color.BOLD}{tested_count}{Color.RESET}")
    safe_print(f"  Active / Usable         : {Color.GREEN}{Color.BOLD}{len(active_accounts)}{Color.RESET}")
    safe_print(f"  Explicitly Banned/Muted : {Color.RED}{Color.BOLD}{len(banned_accounts)}{Color.RESET}")
    safe_print(f"  Wiped / Invalid Pass    : {Color.YELLOW}{Color.BOLD}{len(invalid_accounts)}{Color.RESET}")
    safe_print(f"  Network / Other Errors  : {Color.DIM}{len(error_accounts)}{Color.RESET}")
    if tested_count > 0:
        safe_print(f"  Elapsed Time            : {elapsed:.2f}s ({rate:.1f} accounts/sec)")
    safe_print("=" * 70)

    # Combine unusable accounts to move: banned, muted, and wiped/invalid
    unusable = banned_accounts + invalid_accounts

    if dry_run:
        safe_print(f"\n{Color.YELLOW}[!] Dry-run enabled. No files were modified.{Color.RESET}")
        return

    # ==========================================================================
    # File Updates
    # ==========================================================================
    safe_print(f"\n{Color.CYAN}[*] Processing file operations...{Color.RESET}")

    # 1. Append unusable accounts to banned_accounts.csv
    if unusable:
        added = append_to_banned_csv(banned_csv, unusable)
        safe_print(
            f"{Color.GREEN}[✓] Appended {Color.BOLD}{added}{Color.RESET}{Color.GREEN} "
            f"new unusable accounts to: {Color.WHITE}{banned_csv}{Color.RESET}"
        )
    else:
        safe_print(f"{Color.CYAN}[i] No new banned or invalid accounts to append.{Color.RESET}")

    # 2. Back up input CSV if not already backed up
    backup_file = f"{input_csv}.bak"
    if not os.path.exists(backup_file):
        try:
            shutil.copyfile(input_csv, backup_file)
            safe_print(f"{Color.GREEN}[✓] Saved safe backup of original accounts to: {Color.WHITE}{backup_file}{Color.RESET}")
        except Exception as e:
            safe_print(f"{Color.YELLOW}[!] Warning: Could not create backup file: {e}{Color.RESET}")

    # 3. Update input CSV: keep active accounts AND any unverified accounts (that errored/timed out)
    # This guarantees that accounts are never discarded due to transient server timeouts
    retained_accounts = active_accounts + [e["account"] for e in error_accounts]
    save_accounts_to_csv(input_csv, retained_accounts)
    safe_print(
        f"{Color.GREEN}[✓] Updated {Color.WHITE}{input_csv}{Color.RESET}{Color.GREEN} "
        f"({Color.BOLD}{len(active_accounts)}{Color.RESET}{Color.GREEN} active, "
        f"{Color.YELLOW}{len(error_accounts)}{Color.RESET}{Color.GREEN} pending retry on server recovery).{Color.RESET}"
    )

    if len(retained_accounts) == 0:
        safe_print(
            f"\n{Color.YELLOW}[!] NOTICE: All tested accounts in {input_csv} were banned or wiped by the server administrator.{Color.RESET}\n"
            f"    The original file has been archived to {Color.WHITE}{backup_file}{Color.RESET}\n"
            f"    and all unusable accounts are catalogued in {Color.WHITE}{banned_csv}{Color.RESET}."
        )

# ==============================================================================
# CLI Entry Point
# ==============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="HBU2 Account Tester & Ban Filter: Test accounts and move banned/invalid ones to a separate CSV."
    )
    parser.add_argument(
        "--csv",
        default=DEFAULT_INPUT_CSV,
        help=f"Path to input accounts CSV (default: {DEFAULT_INPUT_CSV})"
    )
    parser.add_argument(
        "--banned-csv",
        default=DEFAULT_BANNED_CSV,
        help=f"Path to output banned accounts CSV (default: {DEFAULT_BANNED_CSV})"
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=3,
        help="Number of concurrent worker threads (default: 3, max: 10)"
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force re-testing accounts even if already in banned_accounts.csv"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Test accounts without writing or modifying any CSV files"
    )

    args = parser.parse_args()

    workers = max(1, min(args.workers, 10))
    run_filter(args.csv, args.banned_csv, workers=workers, dry_run=args.dry_run, force_retest=args.force)

if __name__ == "__main__":
    main()
