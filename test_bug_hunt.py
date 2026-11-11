#!/usr/bin/env python3
"""
test_bug_hunt.py - Comprehensive Isolated Fault-Injection & Audit Test Suite

Covers:
  - Phase 1: hbu_auth_client.py, proxy_rotator.py, account_vault.py
  - Corpus & Simulator: download_messages.py, daemon_hbu_sim.py, simulate_hbu_chat.py
  - Proxy & Economy: download_all_proxies.py, test_proxies.py,
    custom_account_coins.py, give_coins.py, filter_banned_accounts.py

100% isolated via tempfile.TemporaryDirectory and unittest.mock; never mutates
live CSVs, proxies.json, chat_messages.json, logs/, or sends live traffic.
"""

from __future__ import annotations

import concurrent.futures
import csv
import http.client
import io
import json
import os
import signal
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import account_vault
import custom_account_coins
import daemon_hbu_sim
import download_all_proxies
import download_messages
import filter_banned_accounts
import give_coins
import hbu_auth_client
import proxy_rotator
import simulate_hbu_chat
import status_server
import test_proxies


# ==============================================================================
# 1. hbu_auth_client.py
# ==============================================================================


class TestHbuAuthClient(unittest.TestCase):
    def test_format_proxies_normalization(self):
        self.assertIsNone(hbu_auth_client._format_proxies(None))
        self.assertIsNone(hbu_auth_client._format_proxies("   "))
        self.assertIsNone(hbu_auth_client._format_proxies(123))  # type: ignore[arg-type]
        self.assertEqual(
            hbu_auth_client._format_proxies("1.2.3.4:8080"),
            {"http": "http://1.2.3.4:8080", "https": "http://1.2.3.4:8080"},
        )
        self.assertEqual(
            hbu_auth_client._format_proxies("socks5://1.2.3.4:1080"),
            {"http": "socks5h://1.2.3.4:1080", "https": "socks5h://1.2.3.4:1080"},
        )
        self.assertEqual(
            hbu_auth_client._format_proxies("socks5h://1.2.3.4:1080"),
            {"http": "socks5h://1.2.3.4:1080", "https": "socks5h://1.2.3.4:1080"},
        )

    def test_session_cleanup_on_success_and_error(self):
        fake_session = mock.MagicMock()
        fake_session.__enter__.return_value = fake_session
        fake_session.request.return_value = mock.Mock(status_code=200, json=lambda: {"id": "c1", "image": "svg"})

        with mock.patch("hbu_auth_client.requests.Session", return_value=fake_session):
            data = hbu_auth_client.fetch_image_challenge(proxy_url="socks5://1.2.3.4:1080")
        self.assertEqual(data["id"], "c1")
        fake_session.__exit__.assert_called_once()

        fake_session.reset_mock()
        fake_session.request.side_effect = requests.exceptions.ProxyError("dead")
        with mock.patch("hbu_auth_client.requests.Session", return_value=fake_session):
            with self.assertRaises(hbu_auth_client.ProxyDeadException):
                hbu_auth_client.fetch_image_challenge(proxy_url="socks5://1.2.3.4:1080")
        fake_session.__exit__.assert_called_once()

    def test_malformed_proxy_url_raises_proxy_dead(self):
        with self.assertRaises(hbu_auth_client.ProxyDeadException):
            hbu_auth_client.fetch_image_challenge(proxy_url="badscheme://1.2.3.4:8080", timeout=0.2)

    def test_fetch_challenge_errors_and_rate_limit(self):
        resp_429 = mock.Mock(status_code=429, text="slow down", headers={"Retry-After": "30"})
        with mock.patch("hbu_auth_client._perform_request", return_value=resp_429):
            with self.assertRaises(hbu_auth_client.RateLimitException) as ctx:
                hbu_auth_client.fetch_image_challenge()
            self.assertEqual(ctx.exception.retry_after, "30")

        resp_bad_json = mock.Mock(status_code=200)
        resp_bad_json.json.side_effect = ValueError("bad json")
        with mock.patch("hbu_auth_client._perform_request", return_value=resp_bad_json):
            with self.assertRaises(ValueError):
                hbu_auth_client.fetch_image_challenge()

        resp_incomplete = mock.Mock(status_code=200, json=lambda: {"id": "only_id"})
        with mock.patch("hbu_auth_client._perform_request", return_value=resp_incomplete):
            with self.assertRaises(ValueError):
                hbu_auth_client.fetch_image_challenge()

        resp_500 = mock.Mock(status_code=500, text="server error")
        with mock.patch("hbu_auth_client._perform_request", return_value=resp_500):
            with self.assertRaises(requests.exceptions.HTTPError):
                hbu_auth_client.fetch_image_challenge()

    def test_submit_signup_payload_and_non_json_error(self):
        resp_ok = mock.Mock(status_code=200, json=lambda: {"user": "Alice"})
        with mock.patch("hbu_auth_client._perform_request", return_value=resp_ok) as m_req:
            res = hbu_auth_client.submit_signup("  Alice ", "pw", "cid", " ab12 ", student_id="  ")
        self.assertTrue(res["ok"])
        sent_json = m_req.call_args.kwargs["json"]
        self.assertEqual(sent_json["username"], "Alice")
        self.assertEqual(sent_json["challenge_answer"], "AB12")
        self.assertNotIn("student_id", sent_json)

        resp_err = mock.Mock(status_code=502, text="Bad Gateway")
        resp_err.json.side_effect = ValueError("not json")
        with mock.patch("hbu_auth_client._perform_request", return_value=resp_err):
            res2 = hbu_auth_client.submit_signup("Alice", "pw", "cid", "A")
        self.assertFalse(res2["ok"])
        self.assertEqual(res2["status"], 502)
        self.assertEqual(res2["error"], "Bad Gateway")


# ==============================================================================
# 2. proxy_rotator.py
# ==============================================================================


class TestProxyRotatorAudit(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.base = self.tmpdir.name
        self.quota_file = os.path.join(self.base, "logs", "quota.json")
        self.quarantine_file = os.path.join(self.base, "logs", "quarantine.json")
        self.working_txt = os.path.join(self.base, "working_proxies.txt")
        self.proxies_txt = os.path.join(self.base, "proxies.txt")

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_corrupt_state_files_recover_cleanly(self):
        os.makedirs(os.path.dirname(self.quota_file), exist_ok=True)
        with open(self.quota_file, "wb") as f:
            f.write(b"\xff\xfe{corrupt")
        with open(self.quarantine_file, "w", encoding="utf-8") as f:
            f.write('{"wrong_root": "dict_instead_of_list"}')
        with open(self.working_txt, "w", encoding="utf-8") as f:
            f.write("socks5://198.51.100.1:1080\n")

        rot = proxy_rotator.ProxyRotator(
            include_backups=False,
            quota_file=self.quota_file,
            quarantine_file=self.quarantine_file,
            working_txt=self.working_txt,
            proxies_txt=self.proxies_txt,
        )
        self.assertEqual(rot.proxy_pool, ["socks5://198.51.100.1:1080"])
        self.assertEqual(rot.rate_records, {})
        self.assertEqual(rot.quarantined, set())

    def test_nan_bool_timestamps_rejected_and_protocol_prioritization(self):
        os.makedirs(os.path.dirname(self.quota_file), exist_ok=True)
        now = time.time()
        with open(self.quota_file, "w", encoding="utf-8") as f:
            json.dump({"198.51.100.1:1080": [True, False, float("nan"), float("inf"), now - 10]}, f)
        with open(self.proxies_txt, "w", encoding="utf-8") as f:
            f.write(
                "http://198.51.100.10:8080\n"
                "socks4://198.51.100.11:1080\n"
                "https://198.51.100.12:8443\n"
                "socks5://198.51.100.13:1080\n"
                "http://bad:99999\n"
            )

        rot = proxy_rotator.ProxyRotator(
            include_backups=True,
            backup_limit=2,
            max_signups_per_hour=3,
            quota_file=self.quota_file,
            quarantine_file=self.quarantine_file,
            working_txt=self.working_txt,
            proxies_txt=self.proxies_txt,
            seed=42,
        )
        # socks5 and https should precede socks4 and http
        self.assertEqual(rot.proxy_pool, ["socks5://198.51.100.13:1080", "https://198.51.100.12:8443"])

        # Boolean/NaN/Inf filtered out leaving only 1 valid timestamp
        rot2 = proxy_rotator.ProxyRotator(
            include_backups=False,
            quota_file=self.quota_file,
            quarantine_file=self.quarantine_file,
            working_txt=self.working_txt,
            proxies_txt=self.proxies_txt,
        )
        self.assertEqual(len(rot2.rate_records.get("198.51.100.1:1080", [])), 1)

    def test_disabled_proxies_and_invalid_urls(self):
        rot = proxy_rotator.ProxyRotator(
            use_proxies=False,
            quota_file=self.quota_file,
            quarantine_file=self.quarantine_file,
            working_txt=self.working_txt,
            proxies_txt=self.proxies_txt,
        )
        self.assertIsNone(rot.get_proxy())
        self.assertEqual(rot.get_next_available_wait_seconds(), 0.0)
        self.assertEqual(proxy_rotator.extract_ip_port_key("http://1.2.3.4:99999"), "")
        self.assertFalse(proxy_rotator.probe_proxy("invalid_no_port", timeout=0.1, tcp_only=True))


# ==============================================================================
# 3. account_vault.py
# ==============================================================================


class TestAccountVaultAudit(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.base = self.tmpdir.name

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_load_wordlist_skips_non_ascii_and_bad_lengths(self):
        wl_path = os.path.join(self.base, "words.txt")
        with open(wl_path, "w", encoding="utf-8") as f:
            f.write("ab\ncafé\nvalidword\ntoolongwordhere\n12345\nAlpha\n")
        words = account_vault.load_wordlist(wl_path)
        self.assertEqual(words, ["Validword", "Alpha"])

    def test_generate_username_and_password_guards(self):
        existing = {"nova_wolf", "apex_pilot"}
        for _ in range(15):
            u = account_vault.generate_username(existing=existing)
            self.assertRegex(u, r"^[A-Za-z0-9_]{3,20}$")
            self.assertNotIn(u.lower(), existing)

        with self.assertRaises(ValueError):
            account_vault.generate_password(length=5)
        pwd = account_vault.generate_password(length=14)
        self.assertEqual(len(pwd), 14)

    def test_atomic_csv_corrupt_and_concurrent_safety(self):
        csv_path = os.path.join(self.base, "accounts.csv")
        bak_path = f"{csv_path}.bak"

        # 0-byte CSV must not create .bak
        open(csv_path, "w", encoding="utf-8").close()
        account_vault.append_account_atomically("UserOne", "Pass1234", coins=50, csv_path=csv_path)
        self.assertFalse(os.path.exists(bak_path))

        # Corrupt CSV must not overwrite valid .bak
        with open(bak_path, "w", encoding="utf-8") as f:
            f.write("username,password,student_id,coins,created_at\nSafeUser,SafePass,FLEET,10,2026-01-01\n")
        with open(csv_path, "w", encoding="utf-8") as f:
            f.write("bad_col1,bad_col2\n1,2\n")
        with self.assertRaises(ValueError):
            account_vault.append_account_atomically("UserTwo", "Pass1234", csv_path=csv_path)
        with open(bak_path, "r", encoding="utf-8") as f:
            self.assertIn("SafeUser", f.read())

        # Concurrent appends
        conc_csv = os.path.join(self.base, "concurrent.csv")
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            futs = [
                pool.submit(account_vault.append_account_atomically, f"Bot_{i}", f"Secret{i}Ab", i * 10, "FLEET", None, conc_csv)
                for i in range(12)
            ]
            for fut in futs:
                fut.result(timeout=5)
        with open(conc_csv, "r", encoding="utf-8", newline="") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(len(rows), 12)

    def test_claim_welcome_spin_fallbacks(self):
        # HTTP 502 via proxy -> fallback to direct 200
        resp_502 = mock.Mock(status_code=502)
        resp_200 = mock.Mock(status_code=200, json=lambda: {"prize": 25, "coins": 125})
        with mock.patch("account_vault.requests.Session.post", side_effect=[resp_502, resp_200]) as m_post:
            ok, prize, coins = account_vault.claim_welcome_spin("u", "p", proxy_url="http://1.2.3.4:8080")
        self.assertEqual((ok, prize, coins), (True, 25, 125))
        self.assertEqual(m_post.call_count, 2)

        # Malformed JSON / error payload -> clean (False, 0, 0)
        resp_err = mock.Mock(status_code=200, json=lambda: {"success": False, "error": "already spun"})
        with mock.patch("account_vault.requests.Session.post", return_value=resp_err):
            self.assertEqual(account_vault.claim_welcome_spin("u", "p"), (False, 0, 0))


# ==============================================================================
# 4. download_messages.py
# ==============================================================================


class TestDownloadMessagesAudit(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.base = self.tmpdir.name

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_corpus_sources_and_sanitization_bounds(self):
        distinct_repos = {s.repo for s in download_messages.CORPUS_SOURCES}
        self.assertGreaterEqual(len(distinct_repos), 5)

        self.assertIsNone(download_messages.sanitize_message(""))
        self.assertIsNone(download_messages.sanitize_message("   \t\n  "))
        self.assertIsNone(download_messages.sanitize_message("https://example.com/only-url"))
        self.assertIsNone(download_messages.sanitize_message("<img src='x'>"))
        self.assertEqual(
            download_messages.sanitize_message("<b>Hello</b> check [this](https://a.b) https://spam.io right now!"),
            "Hello check this right now!",
        )
        self.assertIsNone(download_messages.sanitize_message("A " * 260))  # > 500 chars
        self.assertIsNone(download_messages.sanitize_message("THIS IS ALL CAPS SHOUTING HERE"))
        valid = download_messages.sanitize_message("Hey everyone, nice game today!")
        self.assertIsNotNone(valid)
        self.assertTrue(1 <= len(valid) <= 500)

    def test_load_message_pool_fallbacks_on_missing_and_corrupt_json(self):
        fallback = ["Fallback hello!", "Fallback world!"]
        missing_path = os.path.join(self.base, "missing.json")
        self.assertEqual(download_messages.load_message_pool(fallback, path=missing_path), fallback)

        # Invalid JSON
        bad_json = os.path.join(self.base, "bad.json")
        with open(bad_json, "w", encoding="utf-8") as f:
            f.write("{broken json")
        self.assertEqual(download_messages.load_message_pool(fallback, path=bad_json), fallback)

        # Invalid UTF-8 bytes
        bad_utf8 = os.path.join(self.base, "bad_utf8.json")
        with open(bad_utf8, "wb") as f:
            f.write(b"\xff\xfe\x00\x00invalid")
        self.assertEqual(download_messages.load_message_pool(fallback, path=bad_utf8), fallback)

        # Whitespace-only + over-500-char messages filtered out -> falls back
        bounds_json = os.path.join(self.base, "bounds.json")
        with open(bounds_json, "w", encoding="utf-8") as f:
            json.dump({"messages": ["   ", "", "x" * 501]}, f)
        self.assertEqual(download_messages.load_message_pool(fallback, path=bounds_json), fallback)

    def test_verify_corpus_and_mock_harvest_resilience(self):
        out_path = os.path.join(self.base, "chat_messages.json")

        def fake_fetch(url, timeout=20):
            if "chatterbot" in url:
                raise http.client.IncompleteRead(b"partial")
            if "Topical-Chat" in url:
                return json.dumps({"c1": {"content": [{"message": "Topical chat turns work great!"}]}})
            if "icebreakers" in url:
                return "  * What is your favorite retro arcade game?\n"
            if "official_joke_api" in url:
                return json.dumps([{"setup": "Why did the dev quit?", "punchline": "They did not get arrays."}])
            if "dadjokes" in url:
                return "I used to play piano by ear.<>Now I use my hands.\n"
            if "programming-quotes-api" in url:
                return json.dumps([{"en": "Simplicity is prerequisite for reliability."}])
            return "[]"

        with mock.patch("download_messages._fetch_url", side_effect=fake_fetch):
            payload = download_messages.harvest_corpus(output_path=out_path)
        self.assertGreaterEqual(payload["distinct_repo_count"], 5)
        self.assertTrue(download_messages.verify_corpus(out_path))

        # Corrupt corpus fails verify_corpus cleanly without crashing
        with open(out_path, "wb") as f:
            f.write(b"\xff\xfe")
        self.assertFalse(download_messages.verify_corpus(out_path))


# ==============================================================================
# 5. daemon_hbu_sim.py
# ==============================================================================


class TestDaemonHbuSimAudit(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.base = self.tmpdir.name

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_load_config_corrupt_and_invalid_delays(self):
        cfg_path = os.path.join(self.base, "cfg.json")
        with mock.patch.object(daemon_hbu_sim, "CONFIG_FILE", cfg_path):
            # Corrupt JSON
            with open(cfg_path, "w", encoding="utf-8") as f:
                f.write("{corrupt")
            cfg1 = daemon_hbu_sim.load_config()
            self.assertGreaterEqual(cfg1["min_delay_per_bot"], 5.1)

            # Malformed types (string/null delays, list root)
            with open(cfg_path, "w", encoding="utf-8") as f:
                json.dump({"min_delay_per_bot": "fast", "max_delay_per_bot": None}, f)
            cfg2 = daemon_hbu_sim.load_config()
            self.assertEqual(cfg2["min_delay_per_bot"], 5.1)
            self.assertGreaterEqual(cfg2["max_delay_per_bot"], cfg2["min_delay_per_bot"])

            with open(cfg_path, "w", encoding="utf-8") as f:
                json.dump([["min_delay_per_bot", 1.0]], f)
            cfg3 = daemon_hbu_sim.load_config()
            self.assertEqual(cfg3["min_delay_per_bot"], 5.1)

    def test_load_accounts_corrupt_csv_and_telemetry_recovery(self):
        bad_csv = os.path.join(self.base, "bad.csv")
        with open(bad_csv, "wb") as f:
            f.write(b"\xff\xfe\x00bad")
        self.assertEqual(daemon_hbu_sim.load_accounts(bad_csv), [])

        state_file = os.path.join(self.base, "logs", "telemetry_state.json")
        with mock.patch.object(status_server, "STATE_FILE", state_file):
            os.makedirs(os.path.dirname(state_file), exist_ok=True)
            with open(state_file, "w", encoding="utf-8") as f:
                f.write("{corrupt telemetry json")
            recovered = status_server.get_live_state()
            self.assertEqual(recovered["status"], "STOPPED")
            self.assertEqual(recovered["bot_states"], [])

    def test_bot_worker_error_path_does_not_deadlock(self):
        # Fault injection: previously bot_worker acquired telemetry_lock (Lock) then called set_bot_status() -> DEADLOCK
        daemon_hbu_sim.stop_event.clear()
        daemon_hbu_sim.pause_event.set()
        daemon_hbu_sim.reset_circuit_breaker()

        banned_csv = os.path.join(self.base, "banned.csv")
        acc = {"username": "DeadlockCheckBot", "password": "pw", "student_id": "S1", "coins": "10", "created_at": ""}
        cfg = {
            "supabase_functions_url": "http://127.0.0.1:1/functions/v1",
            "supabase_rest_url": "http://127.0.0.1:1/rest/v1",
            "supabase_anon_key": "k",
            "min_delay_per_bot": 5.1,
            "max_delay_per_bot": 5.2,
            "banned_csv_file": banned_csv,
        }
        with daemon_hbu_sim.telemetry_lock:
            daemon_hbu_sim.telemetry["bot_states"] = [
                {"bot": "DeadlockCheckBot", "sent": 0, "last_seen": "queued", "status": "QUEUED"}
            ]
            daemon_hbu_sim.telemetry["banned_count"] = 0
            daemon_hbu_sim.telemetry["messages_failed"] = 0

        with mock.patch("daemon_hbu_sim.send_chat_message", return_value=(False, 12, "HTTP 403: Banned")):
            t = threading.Thread(target=daemon_hbu_sim.bot_worker, args=(acc, 0, 0.0, cfg, False), daemon=True)
            t.start()
            t.join(timeout=3.0)
            self.assertFalse(t.is_alive(), "bot_worker deadlocked on telemetry_lock during error handling!")

        self.assertTrue(os.path.isfile(banned_csv))
        with daemon_hbu_sim.telemetry_lock:
            self.assertEqual(daemon_hbu_sim.telemetry["bot_states"][0]["status"], "BANNED")

    def test_run_daemon_isolated_bot_state_init_and_snapshot_atomicity(self):
        cfg_file = os.path.join(self.base, "hbu_service_config.json")
        csv_file = os.path.join(self.base, "accounts.csv")
        banned_file = os.path.join(self.base, "banned.csv")
        pid_file = os.path.join(self.base, "logs", "daemon.pid")
        state_file = os.path.join(self.base, "logs", "telemetry_state.json")
        pause_file = os.path.join(self.base, "logs", ".pause_toggle")

        with open(csv_file, "w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["username", "password", "student_id", "coins", "created_at"])
            writer.writerow(["SimBot_1", "Pass1", "STU1", "100", "2026-01-01"])
            writer.writerow(["SimBot_2", "Pass2", "STU2", "200", "2026-01-01"])

        with open(cfg_file, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "csv_file": csv_file,
                    "banned_csv_file": banned_file,
                    "bot_count": 2,
                    "min_delay_per_bot": 5.1,
                    "max_delay_per_bot": 5.15,
                    "web_dashboard_enabled": False,
                },
                f,
            )

        with (
            mock.patch.object(daemon_hbu_sim, "CONFIG_FILE", cfg_file),
            mock.patch.object(daemon_hbu_sim, "PID_FILE", pid_file),
            mock.patch.object(daemon_hbu_sim, "STATE_FILE", state_file),
            mock.patch.object(daemon_hbu_sim, "PAUSE_TRIGGER_FILE", pause_file),
        ):
            daemon_hbu_sim.run_daemon(mock=True, test_duration=1)

        self.assertTrue(os.path.isfile(state_file))
        with open(state_file, "r", encoding="utf-8") as f:
            snap = json.load(f)
        self.assertEqual(snap["status"], "STOPPED")
        self.assertEqual(len(snap["bot_states"]), 2)
        self.assertEqual({b["bot"] for b in snap["bot_states"]}, {"SimBot_1", "SimBot_2"})
        self.assertFalse(os.path.exists(pid_file))


# ==============================================================================
# 6. simulate_hbu_chat.py
# ==============================================================================


class TestSimulateHbuChatAudit(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.base = self.tmpdir.name

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_anti_mute_settings_and_message_pool_fallback(self):
        settings_path = os.path.join(self.base, "settings.json")
        with mock.patch.object(simulate_hbu_chat, "SETTINGS_FILE", settings_path):
            # Unsafe min/max and inverted max < min
            with open(settings_path, "w", encoding="utf-8") as f:
                json.dump({"min_delay_seconds": 1.0, "max_delay_seconds": 2.0}, f)
            s1 = simulate_hbu_chat.load_settings()
            self.assertGreaterEqual(s1["min_delay_seconds"], 5.2)
            self.assertGreaterEqual(s1["max_delay_seconds"], s1["min_delay_seconds"])

            with open(settings_path, "w", encoding="utf-8") as f:
                json.dump({"min_delay_seconds": 9.0, "max_delay_seconds": 6.0}, f)
            s2 = simulate_hbu_chat.load_settings()
            self.assertGreaterEqual(s2["max_delay_seconds"], s2["min_delay_seconds"])

            with open(settings_path, "w", encoding="utf-8") as f:
                f.write("{corrupt")
            s3 = simulate_hbu_chat.load_settings()
            self.assertEqual(s3["min_delay_seconds"], 5.2)

        pool = download_messages.load_message_pool(
            simulate_hbu_chat.SAMPLE_MESSAGES,
            path=os.path.join(self.base, "missing.json"),
        )
        self.assertEqual(pool, simulate_hbu_chat.SAMPLE_MESSAGES)

    def test_csv_zero_byte_header_and_corrupt_utf8(self):
        zero_csv = os.path.join(self.base, "zero.csv")
        open(zero_csv, "w", encoding="utf-8").close()
        simulate_hbu_chat.append_single_account_to_csv(
            zero_csv, {"username": "ZeroUser", "password": "pw", "student_id": "S1", "coins": 5}
        )
        loaded = simulate_hbu_chat.load_accounts_from_csv(zero_csv)
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0]["username"], "ZeroUser")

        corrupt_csv = os.path.join(self.base, "corrupt.csv")
        with open(corrupt_csv, "wb") as f:
            f.write(b"\xff\xfeinvalid")
        self.assertEqual(simulate_hbu_chat.load_accounts_from_csv(corrupt_csv), [])

        # Calling execute_simulation from a non-main thread must not crash on signal.signal()
        sim_settings = simulate_hbu_chat.DEFAULT_SETTINGS.copy()
        sim_settings["num_accounts"] = 0
        sim_settings["messages_per_user"] = 0
        err_holder = []

        def run_in_thread():
            try:
                with mock.patch.object(simulate_hbu_chat, "input", return_value=""), mock.patch("builtins.print"):
                    simulate_hbu_chat.execute_simulation(sim_settings, create_new=True, send_messages=False)
            except Exception as exc:
                err_holder.append(exc)

        t = threading.Thread(target=run_in_thread, daemon=True)
        t.start()
        t.join(timeout=3.0)
        self.assertFalse(t.is_alive())
        self.assertEqual(err_holder, [])


# ==============================================================================
# 7. download_all_proxies.py
# ==============================================================================


class TestDownloadAllProxiesAudit(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.base = self.tmpdir.name

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_bogon_and_multicast_filtering(self):
        bogons = [
            "127.0.0.1:8080",
            "10.0.0.1:8080",
            "172.16.0.1:8080",
            "192.168.1.1:8080",
            "169.254.1.1:8080",
            "100.64.0.1:8080",
            "0.0.0.0:8080",
            "224.0.0.1:8080",       # multicast (is_global=True in Python ipaddress!)
            "239.255.255.250:1900", # multicast
            "240.0.0.1:8080",
            "255.255.255.255:8080",
            "192.0.2.1:8080",       # TEST-NET-1
            "999.1.1.1:8080",
            "8.8.8.8:0",
            "8.8.8.8:70000",
            "8.8.8.8:808012",       # 6-digit port must not truncate to 80801
        ]
        valid = [
            "8.8.8.8:8080",
            "socks5h://1.1.1.1:1080",
        ]
        body = "\n".join(bogons + valid).encode("utf-8")

        fake_resp = mock.MagicMock()
        fake_resp.__enter__.return_value = fake_resp
        fake_resp.read.return_value = body

        with mock.patch("download_all_proxies.urllib.request.urlopen", return_value=fake_resp):
            ok, _repo, _proto, _url, count, found, _info = download_all_proxies.fetch_source(
                ("test/repo", "http", "https://example.com/list.txt")
            )

        self.assertTrue(ok)
        self.assertEqual(count, 2)
        self.assertEqual([(p, ip, port) for p, ip, port, _ in found], [("http", "8.8.8.8", 8080), ("socks5", "1.1.1.1", 1080)])

    def test_deduplication_across_sources(self):
        out_json = os.path.join(self.base, "proxies.json")
        out_txt = os.path.join(self.base, "proxies.txt")

        fake_sources = [
            ("repoA", "http", "https://example.com/a.txt"),
            ("repoB", "socks5", "https://example.com/b.txt"),
        ]

        def fake_fetch(item):
            repo, proto, _ = item
            if repo == "repoA":
                return True, repo, proto, "u1", 2, [("http", "8.8.8.8", 8080, repo), ("http", "1.1.1.1", 3128, repo)], "0.1s"
            return True, repo, proto, "u2", 1, [("socks5", "8.8.8.8", 8080, repo)], "0.1s"

        with (
            mock.patch.object(download_all_proxies, "SOURCES", fake_sources),
            mock.patch.object(download_all_proxies, "OUTPUT_JSON", out_json),
            mock.patch.object(download_all_proxies, "OUTPUT_TXT", out_txt),
            mock.patch("download_all_proxies.fetch_source", side_effect=fake_fetch),
        ):
            download_all_proxies.main()

        with open(out_json, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["metadata"]["total_unique_proxies"], 2)
        entry_8 = next(p for p in data["proxies"] if p["ip"] == "8.8.8.8")
        self.assertEqual(entry_8["protocols"], ["http", "socks5"])
        self.assertEqual(entry_8["sources"], ["repoA", "repoB"])


# ==============================================================================
# 8. test_proxies.py
# ==============================================================================


class TestTestProxiesAudit(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.base = self.tmpdir.name

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_corrupted_json_handling(self):
        missing = os.path.join(self.base, "missing.json")
        with self.assertRaises(ValueError):
            test_proxies.load_proxies_json(missing)

        bad_json = os.path.join(self.base, "bad.json")
        with open(bad_json, "w", encoding="utf-8") as f:
            f.write("{corrupt")
        with self.assertRaises(ValueError):
            test_proxies.load_proxies_json(bad_json)

        bad_utf8 = os.path.join(self.base, "bad_utf8.json")
        with open(bad_utf8, "wb") as f:
            f.write(b"\xff\xfe")
        with self.assertRaises(ValueError):
            test_proxies.load_proxies_json(bad_utf8)

        wrong_root = os.path.join(self.base, "list_root.json")
        with open(wrong_root, "w", encoding="utf-8") as f:
            json.dump([1, 2, 3], f)
        with self.assertRaises(ValueError):
            test_proxies.load_proxies_json(wrong_root)

    def test_session_cleanup_and_checkpoint_atomicity(self):
        fake_session = mock.MagicMock()
        fake_session.__enter__.return_value = fake_session
        fake_session.get.return_value = mock.Mock(status_code=200)
        entry = {"ip": "8.8.8.8", "port": 8080, "primary_protocol": "http"}

        with mock.patch("test_proxies.requests.Session", return_value=fake_session):
            res = test_proxies.test_single_proxy(entry, "https://example.com", {}, 1.0)
        self.assertEqual(res["status"], "working")
        fake_session.__exit__.assert_called_once()

        json_path = os.path.join(self.base, "proxies.json")
        txt_path = os.path.join(self.base, "working_proxies.txt")
        payload = {
            "proxies": [
                {"ip": "8.8.8.8", "port": 8080, "primary_protocol": "http", "status": "working", "url": "http://8.8.8.8:8080"},
                {"ip": "1.1.1.1", "port": 1080, "primary_protocol": "socks5", "status": "dead"},
                "malformed_non_dict_entry",
            ]
        }
        test_proxies.save_checkpoint(json_path, payload, txt_path)
        leftover_tmps = [f for f in os.listdir(self.base) if f.endswith(".tmp")]
        self.assertEqual(leftover_tmps, [])
        with open(txt_path, "r", encoding="utf-8") as f:
            self.assertEqual(f.read().strip(), "http://8.8.8.8:8080")

    def test_checkpoint_interval_no_deadlock_and_signal_helper(self):
        # Fault injection: previously main() held state_lock and called save_checkpoint() -> DEADLOCK
        json_path = os.path.join(self.base, "proxies.json")
        txt_path = os.path.join(self.base, "working_proxies.txt")
        proxies = [
            {"ip": f"8.8.8.{i}", "port": 8080, "primary_protocol": "http", "status": None}
            for i in range(1, 6)
        ]
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump({"proxies": proxies}, f)

        def fake_probe(p, target_url, headers, timeout):
            return {"status": "working", "latency_ms": 12.3, "last_checked": "now", "error": None}

        argv = [
            "test_proxies.py",
            "--file", json_path,
            "--checkpoint", "2",
            "--threads", "2",
            "--output-txt", txt_path,
        ]
        with (
            mock.patch.object(sys, "argv", argv),
            mock.patch("test_proxies.test_single_proxy", side_effect=fake_probe),
            mock.patch("test_proxies.install_signal_handlers"),
        ):
            test_proxies.main()

        with open(json_path, "r", encoding="utf-8") as f:
            saved = json.load(f)
        self.assertEqual(saved["metadata"]["tested_summary"]["working"], 5)

        # Verify signal helper sets flag cleanly
        test_proxies.is_interrupted = False
        test_proxies.request_shutdown(signal.SIGINT, None)
        self.assertTrue(test_proxies.is_interrupted)
        test_proxies.is_interrupted = False


# ==============================================================================
# 9. Economy & Filter Modules (custom_account_coins, give_coins, filter_banned_accounts)
# ==============================================================================


class TestEconomyAndFilterAudit(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.base = self.tmpdir.name

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_custom_account_coins_csv_edge_cases(self):
        csv_path = os.path.join(self.base, "accounts.csv")
        with open(csv_path, "w", encoding="utf-8", newline="") as f:
            f.write("username,password,student_id,coins,created_at,extra\n")
            f.write("Alice,pw1,S1,100,2026-01-01,keepme\n")
            f.write(",pw_empty,S2,50\n")  # short row -> None values in later cols
            f.write("Bob,pw2,S3,200,2026-01-01,keepbob,overflow_col\n")

        ok = custom_account_coins.update_or_append_csv(csv_path, "Bob", "newpw", 9999)
        self.assertTrue(ok)
        with open(csv_path, "r", encoding="utf-8", newline="") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(len(rows), 3)
        bob = next(r for r in rows if r["username"] == "Bob")
        self.assertEqual(bob["coins"], "9999")
        self.assertEqual(bob["extra"], "keepbob")

        # Corrupt CSV read failure must NOT wipe the file
        corrupt_csv = os.path.join(self.base, "corrupt.csv")
        raw_corrupt = b"\xff\xfeAlice,pw1\n"
        with open(corrupt_csv, "wb") as f:
            f.write(raw_corrupt)
        self.assertFalse(custom_account_coins.update_or_append_csv(corrupt_csv, "NewUser", "pw", 100))
        with open(corrupt_csv, "rb") as f:
            self.assertEqual(f.read(), raw_corrupt)

    def test_give_coins_csv_edge_cases(self):
        csv_path = os.path.join(self.base, "give.csv")
        # Include UTF-8 BOM + extra overflow column + short row
        with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
            f.write("username,password,student_id,coins,status\n")
            f.write("Carol,pw1,S1,10,Active,overflow1\n")
            f.write("\n")  # blank line

        loaded = give_coins.load_accounts_from_csv(csv_path)
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0]["username"], "Carol")

        self.assertTrue(give_coins.update_csv_account(csv_path, "carol", "", 5000))
        loaded2 = give_coins.load_accounts_from_csv(csv_path)
        self.assertEqual(loaded2[0]["coins"], "5000")

        # Corrupt UTF-8 must not wipe existing CSV
        corrupt_csv = os.path.join(self.base, "give_corrupt.csv")
        raw_corrupt = b"\xff\xfeCarol,pw\n"
        with open(corrupt_csv, "wb") as f:
            f.write(raw_corrupt)
        self.assertFalse(give_coins.update_csv_account(corrupt_csv, "NewUser", "pw", 10))
        with open(corrupt_csv, "rb") as f:
            self.assertEqual(f.read(), raw_corrupt)

    def test_filter_banned_accounts_edge_cases_and_isolation(self):
        # None fields in account dict must not crash test_account
        res_missing = filter_banned_accounts.test_account(
            {"username": None, "password": None, "student_id": None, "coins": None, "created_at": None},
            "http://127.0.0.1:1/rest/v1",
            "http://127.0.0.1:1/functions/v1",
            "k",
        )
        self.assertEqual(res_missing["status"], "INVALID")

        # Corrupt CSV returns [] safely
        corrupt_csv = os.path.join(self.base, "corrupt_filter.csv")
        with open(corrupt_csv, "wb") as f:
            f.write(b"\xff\xfebad")
        self.assertEqual(filter_banned_accounts.load_accounts_from_csv(corrupt_csv), [])

        # Full isolated run_filter with mocked test_account
        in_csv = os.path.join(self.base, "input.csv")
        banned_csv = os.path.join(self.base, "banned.csv")
        with open(in_csv, "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(["username", "password", "student_id", "coins", "created_at"])
            w.writerow(["ActiveUser", "p1", "S1", "100", "2026-01-01"])
            w.writerow(["BannedUser", "p2", "S2", "200", "2026-01-01"])
            w.writerow(["ErrorUser", "p3", "S3", "300", "2026-01-01"])

        def fake_test(acc, *_args):
            u = acc["username"]
            base = {
                "account": acc,
                "username": u,
                "password": acc["password"],
                "student_id": acc["student_id"],
                "coins": acc["coins"],
                "created_at": acc["created_at"],
                "tested_at": "2026-01-01 00:00:00 UTC",
            }
            if u == "BannedUser":
                return {**base, "status": "BANNED", "reason": "Ban reason"}
            if u == "ErrorUser":
                raise RuntimeError("Simulated worker crash")
            return {**base, "status": "ACTIVE", "reason": "OK"}

        with mock.patch("filter_banned_accounts.test_account", side_effect=fake_test):
            filter_banned_accounts.run_filter(in_csv, banned_csv, workers=2, dry_run=False)

        # ActiveUser + ErrorUser retained, BannedUser moved to banned.csv, .bak created
        self.assertTrue(os.path.isfile(f"{in_csv}.bak"))
        retained = filter_banned_accounts.load_accounts_from_csv(in_csv)
        self.assertEqual({a["username"] for a in retained}, {"ActiveUser", "ErrorUser"})
        with open(banned_csv, "r", encoding="utf-8", newline="") as f:
            banned_rows = list(csv.DictReader(f))
        self.assertEqual([r["username"] for r in banned_rows], ["BannedUser"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
