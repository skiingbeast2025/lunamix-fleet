#!/usr/bin/env python3
"""
account_vault.py - Credential Generator & Atomic CSV Fleet Engine

Handles organic username generation (with collision checks across active and
banned fleets), high-entropy password creation, atomic CSV persistence into
simulated_accounts.csv, and initial daily wheel spin execution with optional
proxy routing and safe fallback.
"""

import csv
import os
import random
import re
import secrets
import shutil
import string
import sys
import tempfile
import threading
import time
import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SIMULATED_CSV = os.path.join(BASE_DIR, "simulated_accounts.csv")
BANNED_CSV = os.path.join(BASE_DIR, "banned_accounts.csv")
RANDOM_WORDS_FILE = os.path.join(BASE_DIR, "random_words.txt")

REST_URL = "https://xvkjvuxhawlljakfkllu.supabase.co/rest/v1"
ANON_KEY = "sb_publishable_uyxd5lS77wdXpD56QF_lgg_zaujLjZZ"

DEFAULT_FIELDNAMES = ["username", "password", "student_id", "coins", "created_at"]
USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9_]{3,20}$")

DEFAULT_HEADERS = {
    "apikey": ANON_KEY,
    "Authorization": f"Bearer {ANON_KEY}",
    "Content-Type": "application/json",
    "Origin": "https://rockystudios.github.io",
    "Referer": "https://rockystudios.github.io/HBU2/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
}

PREFIXES = [
    "Nova", "Apex", "Cyber", "Echo", "Shadow", "Vortex", "Frost", "Solar",
    "Titan", "Ghost", "Blaze", "Storm", "Drift", "Pulse", "Rogue", "Hyper",
]
SUFFIXES = [
    "Wolf", "Pilot", "Runner", "Strike", "Knight", "Fox", "Hawk", "Byte",
    "Spark", "Viper", "Ranger", "Core", "Blade", "Lunar", "Flux", "Zero",
]

csv_lock = threading.Lock()


def load_wordlist(filepath: str = None) -> list:
    """Loads clean alphabetic words (3-10 chars) from random_words.txt if present."""
    path = filepath or RANDOM_WORDS_FILE
    words = []
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    w = line.strip()
                    if w and 3 <= len(w) <= 10 and w.isascii() and w.isalpha():
                        words.append(w.capitalize())
        except (OSError, UnicodeDecodeError) as err:
            print(f"[!] Warning: unable to read wordlist '{path}': {err}", file=sys.stderr)
    return words


WORDLIST = load_wordlist()


def _read_usernames_from_csv(csv_path: str) -> set:
    """Reads lowercase usernames from a CSV file if it exists."""
    names = set()
    if not csv_path or not os.path.isfile(csv_path):
        return names
    try:
        with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            if not reader.fieldnames:
                return names
            username_col = next(
                (col for col in reader.fieldnames if col and col.strip().lower() == "username"),
                None,
            )
            if not username_col:
                return names
            for row in reader:
                val = row.get(username_col)
                if val and val.strip():
                    names.add(val.strip().lower())
    except (OSError, csv.Error, UnicodeDecodeError) as err:
        print(f"[!] Warning: unable to read usernames from '{csv_path}': {err}", file=sys.stderr)
    return names


def get_existing_usernames(include_banned: bool = True) -> set:
    """
    Collects existing lowercase usernames from simulated_accounts.csv
    and (when present) banned_accounts.csv to avoid fleet collisions.
    """
    paths = [SIMULATED_CSV, BANNED_CSV] if include_banned else [SIMULATED_CSV]
    existing = set()
    for path in paths:
        existing.update(_read_usernames_from_csv(path))
    return existing


def _compose_candidate(use_wordlist: bool) -> str:
    """Builds a single organic gamer tag candidate from WORDLIST or fallback wordlists."""
    pool_a = WORDLIST if use_wordlist else PREFIXES
    pool_b = WORDLIST if use_wordlist else SUFFIXES
    style = random.randint(0, 4)

    w1 = random.choice(pool_a)
    w2 = random.choice(pool_b)
    num = random.randint(10, 999)

    if style == 0:
        return f"{w1}_{w2}"
    if style == 1:
        return f"{w1}_{w2}{num}"
    if style == 2:
        return f"{w1}{w2}"
    if style == 3:
        return f"{w1}{w2}{num}"
    return f"{w1}_{num}"


def generate_username(existing: set = None) -> str:
    """
    Generates an organic 3-20 char username matching ^[A-Za-z0-9_]{3,20}$
    that does not collide with simulated_accounts.csv or banned_accounts.csv.
    """
    if existing is None:
        existing_lower = get_existing_usernames(include_banned=True)
    else:
        snapshot = existing.copy() if isinstance(existing, set) else list(existing)
        existing_lower = {u.strip().lower() for u in snapshot if isinstance(u, str) and u.strip()}

    for _ in range(500):
        use_wordlist = bool(WORDLIST) and random.random() < 0.8
        tag = _compose_candidate(use_wordlist)
        if USERNAME_PATTERN.match(tag) and tag.lower() not in existing_lower:
            return tag

    # High-entropy fallback guaranteed to match ^[A-Za-z0-9_]{3,20}$
    for _ in range(100):
        tag = f"User_{secrets.token_hex(4)}"
        if tag.lower() not in existing_lower:
            return tag

    raise RuntimeError("Exhausted attempts to generate a non-colliding username")


def generate_password(length: int = 14) -> str:
    """Generates a high-entropy alphanumeric password containing upper, lower, and digits."""
    if length < 6:
        raise ValueError("Password length must be at least 6 characters")

    chars = string.ascii_letters + string.digits
    required = [
        secrets.choice(string.ascii_uppercase),
        secrets.choice(string.ascii_lowercase),
        secrets.choice(string.digits),
    ]
    rest = [secrets.choice(chars) for _ in range(length - len(required))]
    combined = required + rest
    secrets.SystemRandom().shuffle(combined)
    return "".join(combined)


def _format_proxies(proxy_url: str):
    """Normalizes a proxy URL into a requests-compatible proxies mapping."""
    if not proxy_url or not isinstance(proxy_url, str):
        return None
    cleaned = proxy_url.strip()
    if not cleaned:
        return None

    lower = cleaned.lower()
    if lower.startswith("socks5://"):
        cleaned = "socks5h://" + cleaned[len("socks5://"):]
    elif not any(lower.startswith(scheme) for scheme in ("socks5h://", "socks4://", "http://", "https://")):
        cleaned = f"http://{cleaned}"

    return {"http": cleaned, "https": cleaned}


def claim_welcome_spin(
    username: str,
    password: str,
    proxy_url: str = None,
    timeout: float = 8.0,
) -> tuple:
    """
    Spins the daily wheel via /rpc/hbu_spin to grant starting coins.

    Supports optional proxy forwarding (proxy_url=None) and safely falls back to
    a direct request if the proxy transport fails or returns a transient gateway/rate
    error. Handles HTTP errors cleanly and returns (ok, prize, coins).
    """
    url = f"{REST_URL.rstrip('/')}/rpc/hbu_spin"
    payload = {"p_username": username, "p_password": password}
    routes = [proxy_url, None] if (proxy_url and proxy_url.strip()) else [None]

    with requests.Session() as session:
        session.trust_env = False
        for route_proxy in routes:
            proxies = _format_proxies(route_proxy)
            try:
                resp = session.post(
                    url,
                    headers=DEFAULT_HEADERS,
                    json=payload,
                    proxies=proxies,
                    timeout=timeout,
                )
            except requests.RequestException:
                # Proxy transport/timeout failure: fall back to direct if this was a proxy hop
                if route_proxy is not None:
                    continue
                return False, 0, 0

            if resp.status_code == 200:
                try:
                    data = resp.json()
                except ValueError:
                    return False, 0, 0

                if not isinstance(data, dict) or data.get("success") is False or "error" in data:
                    return False, 0, 0

                try:
                    coins = int(data.get("coins", 0) or 0)
                    prize = int(data.get("prize", 0) or 0)
                except (TypeError, ValueError):
                    return False, 0, 0
                return True, prize, coins

            # Fall back to direct on proxy-side rate limit / gateway errors
            if route_proxy is not None and resp.status_code in (407, 429, 502, 503, 504):
                continue

            # Cleanly handle non-retryable HTTP errors (e.g., 400, 401, 403, 404, 500)
            return False, 0, 0

    return False, 0, 0


def _read_and_validate_csv(target_csv: str) -> tuple:
    """
    Reads and validates an existing CSV file before backup or modification.

    Returns (fieldnames, rows, is_valid_for_backup). Only non-empty CSVs with
    a valid header containing a username column are marked valid for backup.
    """
    if not os.path.isfile(target_csv) or os.path.getsize(target_csv) == 0:
        return list(DEFAULT_FIELDNAMES), [], False

    with open(target_csv, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        raw_headers = [col for col in (reader.fieldnames or []) if col is not None]
        if not raw_headers or any(not col.strip() for col in raw_headers):
            raise ValueError(f"Invalid CSV header in '{target_csv}': {reader.fieldnames}")
        if not any(col.strip().lower() == "username" for col in raw_headers):
            raise ValueError(f"CSV '{target_csv}' is missing required 'username' column")

        rows = []
        for row in reader:
            row.pop(None, None)
            rows.append(row)

    return raw_headers, rows, True


def _write_backup_atomically(src_csv: str, parent_dir: str) -> None:
    """Atomically writes src_csv to src_csv.bak via a temp file + os.replace."""
    bak_path = f"{src_csv}.bak"
    fd, bak_tmp = tempfile.mkstemp(
        prefix=f".{os.path.basename(bak_path)}.",
        suffix=".tmp",
        dir=parent_dir,
    )
    os.close(fd)
    try:
        shutil.copy2(src_csv, bak_tmp)
        os.replace(bak_tmp, bak_path)
    except Exception:
        if os.path.exists(bak_tmp):
            os.unlink(bak_tmp)
        raise


def _write_csv_atomically(target_csv: str, fieldnames: list, rows: list, parent_dir: str) -> None:
    """Writes fieldnames and rows to a temp file, fsyncs, and replaces target_csv."""
    fd, tmp_csv = tempfile.mkstemp(
        prefix=f".{os.path.basename(target_csv)}.",
        suffix=".tmp",
        dir=parent_dir,
    )
    file_obj = None
    try:
        file_obj = os.fdopen(fd, "w", newline="", encoding="utf-8")
        with file_obj as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_csv, target_csv)
    except Exception:
        if file_obj is None:
            try:
                os.close(fd)
            except OSError:
                pass
        if os.path.exists(tmp_csv):
            os.unlink(tmp_csv)
        raise


def append_account_atomically(
    username: str,
    password: str,
    coins: int = 100,
    student_id: str = "FLEET",
    created_at: str = None,
    csv_path: str = None,
) -> str:
    """
    Atomically appends a new account row to simulated_accounts.csv (or csv_path),
    preserving exact existing CSV headers and rows, creating a .bak backup only
    when the existing file is valid, and writing via .tmp + os.replace under csv_lock.
    """
    if not isinstance(username, str) or not username.strip():
        raise ValueError("username must be a non-empty string")
    if not isinstance(password, str) or not password.strip():
        raise ValueError("password must be a non-empty string")

    target_csv = os.path.abspath(csv_path or SIMULATED_CSV)
    parent_dir = os.path.dirname(target_csv)
    os.makedirs(parent_dir, exist_ok=True)

    ts = created_at if created_at is not None else time.strftime("%Y-%m-%d %H:%M:%S")
    canonical_values = {
        "username": username,
        "password": password,
        "student_id": str(student_id),
        "studentid": str(student_id),
        "coins": str(coins),
        "created_at": ts,
        "createdat": ts,
    }

    with csv_lock:
        fieldnames, rows, is_valid_for_backup = _read_and_validate_csv(target_csv)
        if is_valid_for_backup:
            _write_backup_atomically(target_csv, parent_dir)

        new_row = {col: canonical_values.get(col.strip().lower(), "") for col in fieldnames}
        rows.append(new_row)
        _write_csv_atomically(target_csv, fieldnames, rows, parent_dir)

    return target_csv


def _run_self_test():
    """
    Runs isolated self-verification without modifying live simulated_accounts.csv.
    """
    import unittest.mock as mock

    print("[*] Running account_vault.py self-test (live CSV untouched)...")
    active_names = _read_usernames_from_csv(SIMULATED_CSV)
    banned_names = _read_usernames_from_csv(BANNED_CSV)
    all_existing = get_existing_usernames(include_banned=True)

    print(
        f"[*] Loaded wordlist={len(WORDLIST)} words | "
        f"active={len(active_names)} | banned={len(banned_names)} | combined={len(all_existing)}"
    )

    generated = []
    for _ in range(10):
        u = generate_username(all_existing)
        assert USERNAME_PATTERN.match(u), f"Invalid username format: {u}"
        assert u.lower() not in all_existing, f"Collision detected: {u}"
        generated.append(u)
    p = generate_password()
    assert len(p) == 14 and any(c.isupper() for c in p) and any(c.islower() for c in p) and any(c.isdigit() for c in p)
    print(f"[*] Sample generated credentials: Username={generated[0]}, Password={p}")

    # Verify collision avoidance explicitly against both simulated & banned sets
    if active_names and banned_names:
        sample_active = next(iter(active_names))
        sample_banned = next(iter(banned_names))
        assert sample_active in all_existing and sample_banned in all_existing

    # Verify atomic CSV append preserves exact headers & existing rows in an isolated temp dir
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_csv = os.path.join(tmpdir, "simulated_accounts.csv")
        if os.path.isfile(SIMULATED_CSV):
            shutil.copy2(SIMULATED_CSV, tmp_csv)
            with open(SIMULATED_CSV, "r", encoding="utf-8", newline="") as f:
                orig_reader = csv.DictReader(f)
                orig_headers = list(orig_reader.fieldnames)
                orig_count = sum(1 for _ in orig_reader)
        else:
            orig_headers = list(DEFAULT_FIELDNAMES)
            orig_count = 0

        append_account_atomically(
            username=generated[0],
            password=p,
            coins=250,
            student_id="FLEET",
            csv_path=tmp_csv,
        )

        with open(tmp_csv, "r", encoding="utf-8", newline="") as f:
            new_reader = csv.DictReader(f)
            new_headers = list(new_reader.fieldnames)
            new_rows = list(new_reader)

        assert new_headers == orig_headers, f"Header mismatch: {new_headers} != {orig_headers}"
        assert len(new_rows) == orig_count + 1
        assert new_rows[-1]["username"] == generated[0]
        assert new_rows[-1]["password"] == p
        assert new_rows[-1]["student_id"] == "FLEET"
        assert new_rows[-1]["coins"] == "250"
        assert bool(new_rows[-1]["created_at"])
        if orig_count > 0:
            assert os.path.isfile(f"{tmp_csv}.bak"), "Missing .bak backup"
        leftover_tmps = [fn for fn in os.listdir(tmpdir) if fn.endswith(".tmp")]
        assert not leftover_tmps, f"Leftover temp files: {leftover_tmps}"
        print(
            f"[*] Atomic CSV test passed: headers={new_headers}, "
            f"rows={orig_count}->{len(new_rows)}, backup_created={os.path.isfile(f'{tmp_csv}.bak')}"
        )

        # Verify concurrent thread-safe appends
        fresh_csv = os.path.join(tmpdir, "concurrent.csv")
        threads = [
            threading.Thread(
                target=append_account_atomically,
                args=(f"ThreadUser_{i}", f"Pass{i}Abc", 100 + i, "FLEET", None, fresh_csv),
            )
            for i in range(8)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        with open(fresh_csv, "r", encoding="utf-8", newline="") as f:
            c_rows = list(csv.DictReader(f))
        assert len(c_rows) == 8, f"Expected 8 concurrent rows, got {len(c_rows)}"
        print("[*] Concurrent thread-safe CSV append test passed: 8/8 rows persisted.")

        # Verify .bak is NOT created for 0-byte CSV and NOT overwritten for corrupt CSV
        empty_csv = os.path.join(tmpdir, "empty.csv")
        open(empty_csv, "w", encoding="utf-8").close()
        append_account_atomically("EmptyUser", "Pass1234", 100, "FLEET", None, empty_csv)
        assert not os.path.exists(f"{empty_csv}.bak"), "Should not create .bak for 0-byte CSV"
        with open(empty_csv, "r", encoding="utf-8", newline="") as f:
            assert csv.DictReader(f).fieldnames == DEFAULT_FIELDNAMES

        corrupt_csv = os.path.join(tmpdir, "corrupt.csv")
        corrupt_bak = f"{corrupt_csv}.bak"
        with open(corrupt_bak, "w", encoding="utf-8") as f:
            f.write("username,password,student_id,coins,created_at\nGoodUser,GoodPass,FLEET,100,2026-11-11 00:00:00\n")
        with open(corrupt_csv, "wb") as f:
            f.write(b"\xff\xfeinvalid_utf8_bytes")
        try:
            append_account_atomically("NewUser", "Pass1234", 100, "FLEET", None, corrupt_csv)
            assert False, "Expected corrupt CSV append to fail"
        except (UnicodeDecodeError, ValueError, csv.Error):
            pass
        with open(corrupt_bak, "r", encoding="utf-8") as f:
            assert "GoodUser" in f.read(), "Corrupt CSV overwrote valid .bak"

        # Verify .tmp cleanup when os.replace raises
        fail_csv = os.path.join(tmpdir, "replace_fail.csv")
        with mock.patch("os.replace", side_effect=OSError("Simulated replace failure")):
            try:
                append_account_atomically("FailUser", "Pass1234", 100, "FLEET", None, fail_csv)
                assert False, "Expected os.replace failure"
            except OSError:
                pass
        leftover_tmps = [fn for fn in os.listdir(tmpdir) if fn.endswith(".tmp")]
        assert not leftover_tmps, f"Leftover temp files after failure: {leftover_tmps}"
        print("[*] Backup validation & .tmp error-cleanup tests passed.")

    # Verify proxy formatting, fallback on proxy error, and clean HTTP error handling
    assert _format_proxies("socks5://127.0.0.1:1080") == {
        "http": "socks5h://127.0.0.1:1080",
        "https": "socks5h://127.0.0.1:1080",
    }

    class _FakeResp:
        def __init__(self, status_code, body):
            self.status_code = status_code
            self._body = body

        def json(self):
            return self._body

    calls = []

    def _fake_post_fallback(url, headers=None, json=None, proxies=None, timeout=None):
        calls.append(proxies)
        if proxies is not None:
            raise requests.exceptions.ProxyError("Simulated proxy failure")
        return _FakeResp(200, {"prize": 50, "coins": 150})

    with mock.patch.object(requests.Session, "post", side_effect=_fake_post_fallback):
        ok, prize, coins = claim_welcome_spin("testu", "testp", proxy_url="socks5://127.0.0.1:1080")
        assert (ok, prize, coins) == (True, 50, 150)
        assert len(calls) == 2 and calls[0] is not None and calls[1] is None

    with mock.patch.object(requests.Session, "post", return_value=_FakeResp(401, {"error": "Unauthorized"})):
        ok, prize, coins = claim_welcome_spin("testu", "testp")
        assert (ok, prize, coins) == (False, 0, 0)

    print("[*] Proxy fallback & HTTP error handling tests passed.")
    print("[✓] All account_vault.py self-tests passed.")


if __name__ == "__main__":
    _run_self_test()
