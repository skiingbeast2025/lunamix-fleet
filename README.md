# Lunamix Fleet & HBU2 Bot Simulator Suite

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
