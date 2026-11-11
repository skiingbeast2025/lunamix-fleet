#!/usr/bin/env python3
"""
sync_to_github.py - Automated Deployment & Continuous Sync Engine for lunamix-fleet

Maintains the private GitHub repository 'lunamix-fleet'
(https://github.com/skiingbeast2025/lunamix-fleet).
Reads GitHub credentials dynamically from the local workspace .git/config,
stages tracked project files, compresses proxies.json -> proxies.json.gz to stay
well under GitHub's 100 MB limit, commits changes, and pushes to GitHub without
ever logging secrets.
"""

import argparse
import configparser
import gzip
import os
import re
import shutil
import subprocess
from typing import Optional
from urllib.parse import quote, urlparse

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_OWNER = "skiingbeast2025"
REPO_NAME = "lunamix-fleet"
PUBLIC_REPO_URL = f"https://github.com/{REPO_OWNER}/{REPO_NAME}"
STAGING_DIR = os.path.expanduser(f"~/.local/share/{REPO_NAME}-git")
GIT_CONFIG_PATH = os.path.join(BASE_DIR, ".git", "config")
MAX_GITHUB_FILE_BYTES = 100 * 1024 * 1024

_CREDENTIAL_URL_RE = re.compile(r"(https?://[^:@\s]+:)[^@\s]+(@)")
_GITHUB_TOKEN_RE = re.compile(r"\bgh[pousr]_[A-Za-z0-9_]+\b")

# Complete inventory of Phase 1 project files and optional Phase 2 TUI modules.
TRACKED_FILES = [
    # 24/7 Simulator Suite
    "service.py",
    "service.sh",
    "daemon_hbu_sim.py",
    "simulate_hbu_chat.py",
    "status_server.py",
    "stop.sh",
    "test_complete_suite.py",
    "hbu_service_config.json",
    "hbu_sim_settings.json",
    # Conversational Message Corpus
    "download_messages.py",
    "chat_messages.json",
    # Proxy Harvester & Testing Engine
    "download_all_proxies.py",
    "test_proxies.py",
    "working_proxies.txt",
    "proxies.txt",
    # Account Creator Engine & QA Suites
    "hbu_auth_client.py",
    "hbu_challenge_ui.py",
    "proxy_rotator.py",
    "account_vault.py",
    "create_hbu_accounts.py",
    "test_account_creator.py",
    "test_bug_hunt.py",
    "test_proxy_rotator.py",
    # Economy & Account Data
    "simulated_accounts.csv",
    "banned_accounts.csv",
    "random_words.txt",
    "custom_account_coins.py",
    "give_coins.py",
    "filter_banned_accounts.py",
    "anonkey.txt",
    "setup_coin_api.sql",
    "update_coins.sql",
    # Automation Engine
    "sync_to_github.py",
    # Phase 2 TUI Modules (skipped gracefully until created)
    "tui_engine.py",
    "tui_views.py",
    "test_tui_visual.py",
]

OPTIONAL_PHASE2_FILES = {
    "tui_engine.py",
    "tui_views.py",
    "test_tui_visual.py",
}

README_CONTENT = """# Lunamix Fleet & HBU2 Bot Simulator Suite

A multi-module automated bot fleet engine, human-verified account creator, proxy harvester/validator, conversational corpus builder, and 24/7 autonomous simulation system for HBU2.

## Key Capabilities

1. **Human-Verified Account Creator (`create_hbu_accounts.py` & `hbu_challenge_ui.py`):**
   - Implements the Edge Function Challenge Protocol (`hbu_auth_client.py`) with isolated proxy-aware requests.
   - Uses `hbu_challenge_ui.py` for manual browser verification: opens base64 SVG verification challenges in a sandboxed local browser preview page and collects manual 6-character operator input (no automated captcha bypass).
   - Enforces per-IP rate limits (3 signups/hour) and dead-proxy quarantine via `proxy_rotator.py`.
   - Manages thread-safe CSV persistence and welcome wheel spin coin claims via `account_vault.py`.

2. **Conversational Message Corpus (`download_messages.py` & `chat_messages.json`):**
   - Harvests, sanitizes, deduplicates, and verifies a **13,090-message corpus** aggregated from **10 GitHub repositories**.
   - Enforces strict HBU2 length bounds and provides `load_message_pool()` for fleet simulators.

3. **290k Proxy Collector & Concurrent Validator (`download_all_proxies.py` & `test_proxies.py`):**
   - Aggregates **290,356 unique public proxies** (SOCKS5, SOCKS4, HTTP, HTTPS) into `proxies.json` and `proxies.txt`.
   - Validates proxies concurrently with latency tracking and atomic checkpointing (`working_proxies.txt`).
   - Syncs `proxies.json` compressed as `proxies.json.gz` to stay well under GitHub's 100 MB file limit.

4. **24/7 Autonomous Simulator & Economy Tools (`service.py`, `service.sh`, `daemon_hbu_sim.py`):**
   - Background bot fleet daemon with staggered 5.1s+ anti-mute pacing, atomic telemetry state, and live HTTP dashboard (`status_server.py`).
   - Account economy and moderation utilities (`custom_account_coins.py`, `give_coins.py`, `filter_banned_accounts.py`, and SQL helpers).

5. **Bug-Hunt & QA Test Suites:**
   - `test_bug_hunt.py`: Isolated fault-injection and audit suite covering auth, rotation, vault, corpus, proxy, simulator, and economy modules.
   - `test_account_creator.py`, `test_proxy_rotator.py`, and `test_complete_suite.py`: Isolated unit and integration suites using mocks and temporary directories.

## Quick Start

```bash
# Launch interactive fleet controller
./service.sh

# Create accounts with manual browser verification and proxy rotation
python3 create_hbu_accounts.py --count 5

# Verify the 13,090-message chat corpus
python3 download_messages.py --verify

# Validate proxies concurrently
python3 test_proxies.py --protocol socks5 --threads 50

# Run isolated QA and bug-hunt suites
python3 test_account_creator.py
python3 test_bug_hunt.py

# Sync updates to private GitHub repo
python3 sync_to_github.py "chore: sync fleet updates"
```
"""

GITIGNORE_CONTENT = """__pycache__/
*.pyc
logs/
*.tmp
*.bak
telemetry_state.json
status_server.log
proxies.json
"""


def redact_secrets(text: str, token: Optional[str] = None) -> str:
    """Scrub GitHub tokens and URL-embedded credentials from logs or error strings."""
    if not text:
        return text
    sanitized = text
    if token:
        sanitized = sanitized.replace(token, "<REDACTED>")
        encoded = quote(token, safe="")
        if encoded:
            sanitized = sanitized.replace(encoded, "<REDACTED>")
    sanitized = _CREDENTIAL_URL_RE.sub(r"\1<REDACTED>\2", sanitized)
    sanitized = _GITHUB_TOKEN_RE.sub("<REDACTED>", sanitized)
    return sanitized


def load_git_identity_and_token(
    git_config_path: str = GIT_CONFIG_PATH,
) -> tuple[str, str, str, str]:
    """
    Read GitHub username, token, and git author identity from .git/config.

    Returns (auth_user, token, git_name, git_email).
    """
    if not os.path.exists(git_config_path):
        raise RuntimeError(f"Git config not found at {git_config_path}")

    # RawConfigParser avoids '%' interpolation errors on URL-encoded values.
    parser = configparser.RawConfigParser()
    parser.read(git_config_path, encoding="utf-8")

    git_name = parser.get("user", "name", fallback="Aidric").strip() or "Aidric"
    git_email = (
        parser.get("user", "email", fallback="aidric@users.noreply.github.com").strip()
        or "aidric@users.noreply.github.com"
    )

    candidate_sections = ['remote "origin"'] + [
        s for s in parser.sections() if s.startswith("remote ") and s != 'remote "origin"'
    ]
    for section in candidate_sections:
        if not parser.has_option(section, "url"):
            continue
        raw_url = parser.get(section, "url").strip()
        parsed = urlparse(raw_url)
        if parsed.scheme in ("http", "https") and parsed.password:
            auth_user = parsed.username or git_name
            return auth_user, parsed.password, git_name, git_email

    raise RuntimeError(
        f"No GitHub credential token found in {git_config_path} remote URLs"
    )


def build_authenticated_remote_url(auth_user: str, token: str) -> str:
    """Build the authenticated HTTPS remote URL for the target private repository."""
    safe_user = quote(auth_user, safe="")
    safe_token = quote(token, safe="")
    return f"https://{safe_user}:{safe_token}@github.com/{REPO_OWNER}/{REPO_NAME}.git"


def run_git(
    args: list[str],
    cwd: str,
    token: Optional[str] = None,
    check: bool = True,
) -> subprocess.CompletedProcess:
    """Execute a git subprocess with captured output and redacted error reporting."""
    # Never use subprocess check=True directly so CalledProcessError never exposes remote URLs.
    res = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    if check and res.returncode != 0:
        safe_cmd = " ".join(redact_secrets(part, token) for part in ["git", *args])
        detail = redact_secrets((res.stderr or res.stdout or "").strip(), token)
        raise RuntimeError(f"Command '{safe_cmd}' failed (exit {res.returncode}): {detail}")
    return res


def ensure_staging_repo(
    auth_user: str,
    token: str,
    git_name: str,
    git_email: str,
) -> None:
    """Initialize or update the staging git repository and its authenticated remote."""
    os.makedirs(STAGING_DIR, exist_ok=True)
    git_dir = os.path.join(STAGING_DIR, ".git")
    remote_url = build_authenticated_remote_url(auth_user, token)

    if not os.path.exists(git_dir):
        print(f"[*] Initializing git tracking in: {STAGING_DIR}...")
        run_git(["init", "-b", "main"], cwd=STAGING_DIR, token=token)
        run_git(["remote", "add", "origin", remote_url], cwd=STAGING_DIR, token=token)
    else:
        remotes = run_git(["remote"], cwd=STAGING_DIR, token=token).stdout.split()
        if "origin" in remotes:
            run_git(["remote", "set-url", "origin", remote_url], cwd=STAGING_DIR, token=token)
        else:
            run_git(["remote", "add", "origin", remote_url], cwd=STAGING_DIR, token=token)

    run_git(["config", "user.name", git_name], cwd=STAGING_DIR, token=token)
    run_git(["config", "user.email", git_email], cwd=STAGING_DIR, token=token)


def compress_proxies_json() -> None:
    """Compress proxies.json to proxies.json.gz in staging and enforce GitHub's 100 MB limit."""
    src = os.path.join(BASE_DIR, "proxies.json")
    dst = os.path.join(STAGING_DIR, "proxies.json.gz")
    if not os.path.exists(src):
        print("  [!] Note: proxies.json not found in workspace, skipping compression.")
        return

    src_mb = os.path.getsize(src) / (1024 * 1024)
    print(f"[*] Compressing proxies.json ({src_mb:.2f} MB) -> proxies.json.gz...")
    with open(src, "rb") as f_in, gzip.open(dst, "wb", compresslevel=6) as f_out:
        shutil.copyfileobj(f_in, f_out)

    dst_bytes = os.path.getsize(dst)
    dst_mb = dst_bytes / (1024 * 1024)
    if dst_bytes >= MAX_GITHUB_FILE_BYTES:
        raise RuntimeError(
            f"Compressed proxies.json.gz is {dst_mb:.2f} MB, exceeding GitHub's 100 MB limit"
        )
    print(f"[+] Compressed to: {dst_mb:.2f} MB (well under GitHub's 100 MB limit)")


def sync_files() -> None:
    """Copy tracked project files into staging, generate docs, and compress proxies.json."""
    print(f"[*] Syncing {len(TRACKED_FILES)} tracked entries to staging...")
    for rel_path in TRACKED_FILES:
        src = os.path.join(BASE_DIR, rel_path)
        dst = os.path.join(STAGING_DIR, rel_path)
        if os.path.exists(src):
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
        elif rel_path in OPTIONAL_PHASE2_FILES:
            print(f"  [~] Optional Phase 2 file not yet present, skipping: {rel_path}")
        else:
            print(f"  [!] Warning: tracked file missing in workspace, skipping: {rel_path}")

    with open(os.path.join(STAGING_DIR, "README.md"), "w", encoding="utf-8") as f:
        f.write(README_CONTENT)
    with open(os.path.join(STAGING_DIR, ".gitignore"), "w", encoding="utf-8") as f:
        f.write(GITIGNORE_CONTENT)

    compress_proxies_json()


def commit_and_push(commit_msg: str) -> None:
    """Stage, commit, and push all synced files to the private GitHub repository."""
    auth_user, token, git_name, git_email = load_git_identity_and_token()
    ensure_staging_repo(auth_user, token, git_name, git_email)
    sync_files()

    run_git(["add", "-A"], cwd=STAGING_DIR, token=token)
    status_out = run_git(["status", "--porcelain"], cwd=STAGING_DIR, token=token).stdout.strip()

    if not status_out:
        print("[+] Everything up-to-date! No changes to commit.")
        return

    print(f"[*] Committing changes: '{commit_msg}'...")
    run_git(["commit", "-m", commit_msg], cwd=STAGING_DIR, token=token)

    print(f"[*] Pushing to private repository: {PUBLIC_REPO_URL} (main branch)")
    push_res = run_git(
        ["push", "-u", "origin", "main"],
        cwd=STAGING_DIR,
        token=token,
        check=False,
    )

    if push_res.returncode == 0:
        print("=" * 72)
        print("[+] SUCCESS: All project files successfully pushed to GitHub!")
        print(f"    Repository: {PUBLIC_REPO_URL}")
        print("=" * 72)
        return

    print("[*] Remote diverged, attempting rebase pull...")
    rebase_res = run_git(
        ["pull", "--rebase", "origin", "main"],
        cwd=STAGING_DIR,
        token=token,
        check=False,
    )
    if rebase_res.returncode != 0:
        safe_err = redact_secrets((rebase_res.stderr or rebase_res.stdout or "").strip(), token)
        raise RuntimeError(f"Rebase pull failed: {safe_err}")

    retry_res = run_git(
        ["push", "-u", "origin", "main"],
        cwd=STAGING_DIR,
        token=token,
        check=False,
    )
    if retry_res.returncode == 0:
        print("[+] Pushed successfully after rebase!")
        return

    safe_err = redact_secrets((retry_res.stderr or retry_res.stdout or "").strip(), token)
    raise RuntimeError(f"Push failed after rebase: {safe_err}")


def main() -> None:
    """Parse CLI arguments and execute the GitHub sync workflow."""
    parser = argparse.ArgumentParser(description="Continuous GitHub Sync for lunamix-fleet")
    parser.add_argument(
        "message",
        nargs="?",
        default="feat: update lunamix fleet codebase and tools",
        help="Git commit message",
    )
    args = parser.parse_args()
    commit_and_push(args.message)


if __name__ == "__main__":
    main()
