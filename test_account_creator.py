#!/usr/bin/env python3
"""
test_account_creator.py - Isolated Unit & Integration Test Suite for HBU2 Fleet Creator

Tests hbu_challenge_ui, hbu_auth_client, proxy_rotator, account_vault, and
create_hbu_accounts using unittest mocks and tempfile.TemporaryDirectory so live
endpoints, browser launchers, and live CSV/log files are never touched.
"""

import base64
import csv
import os
import tempfile
import threading
import unittest
from unittest import mock

import requests

import account_vault
import create_hbu_accounts
import hbu_auth_client
import hbu_challenge_ui
from proxy_rotator import ProxyRotator, extract_ip_port_key


SAMPLE_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="320" height="100">'
    '<text x="20" y="60">ME4A48</text></svg>'
)
SAMPLE_SVG_B64 = base64.b64encode(SAMPLE_SVG.encode("utf-8")).decode("ascii")


def _make_fake_response(
    status_code: int = 200,
    json_data=None,
    text: str = "",
    headers: dict | None = None,
    json_exc: Exception | None = None,
) -> mock.MagicMock:
    """Create a lightweight mock requests.Response object."""
    resp = mock.MagicMock(spec=requests.Response)
    resp.status_code = status_code
    resp.text = text
    resp.headers = headers or {}
    if json_exc is not None:
        resp.json.side_effect = json_exc
    else:
        resp.json.return_value = json_data
    return resp


def _create_isolated_rotator(
    tmpdir: str,
    working_proxies: list[str] | None = None,
    backup_proxies: list[str] | None = None,
    **kwargs,
) -> ProxyRotator:
    """Instantiate a ProxyRotator whose files live strictly inside tmpdir."""
    logs_dir = os.path.join(tmpdir, "logs")
    os.makedirs(logs_dir, exist_ok=True)
    working_txt = os.path.join(tmpdir, "working_proxies.txt")
    proxies_txt = os.path.join(tmpdir, "proxies.txt")

    with open(working_txt, "w", encoding="utf-8") as f:
        for proxy in working_proxies or []:
            f.write(f"{proxy}\n")

    with open(proxies_txt, "w", encoding="utf-8") as f:
        for proxy in backup_proxies or []:
            f.write(f"{proxy}\n")

    defaults = {
        "use_proxies": True,
        "max_signups_per_hour": 3,
        "window_seconds": 3600,
        "quota_file": os.path.join(logs_dir, "proxy_rate_limits.json"),
        "quarantine_file": os.path.join(logs_dir, "quarantined_proxies.json"),
        "working_txt": working_txt,
        "proxies_txt": proxies_txt,
    }
    defaults.update(kwargs)
    return ProxyRotator(**defaults)


class TestHbuChallengeUI(unittest.TestCase):
    """Tests for hbu_challenge_ui data URI validation, answer checks, and temp cleanup."""

    def test_build_svg_data_uri_accepts_raw_and_prefixed_base64(self):
        expected = f"data:image/svg+xml;base64,{SAMPLE_SVG_B64}"
        self.assertEqual(hbu_challenge_ui.build_svg_data_uri(SAMPLE_SVG_B64), expected)

        wrapped = f"  DATA:IMAGE/SVG+XML;BASE64,{SAMPLE_SVG_B64[:12]}\n{SAMPLE_SVG_B64[12:]}  "
        self.assertEqual(hbu_challenge_ui.build_svg_data_uri(wrapped), expected)

    def test_build_svg_data_uri_rejects_invalid_inputs(self):
        for bad in (None, 123, "", "   ", "data:image/svg+xml;base64,", "not!valid!b64"):
            with self.assertRaises(ValueError):
                hbu_challenge_ui.build_svg_data_uri(bad)  # type: ignore[arg-type]

    def test_validate_challenge_answer_alphabet_length_and_skip(self):
        self.assertEqual(hbu_challenge_ui.validate_challenge_answer("me4a48"), "ME4A48")
        self.assertEqual(hbu_challenge_ui.validate_challenge_answer("  k9z2np "), "K9Z2NP")

        # Forbidden lookalike characters: I, O, 0, 1
        for forbidden in ("AB23I9", "AB23O9", "AB2309", "AB2319"):
            self.assertIsNone(hbu_challenge_ui.validate_challenge_answer(forbidden))

        # Short, long, empty, non-string, and explicit skip
        for invalid in (None, 123456, "", "   ", "AB234", "AB23456", "skip", "  SKIP "):
            self.assertIsNone(hbu_challenge_ui.validate_challenge_answer(invalid))  # type: ignore[arg-type]

    def test_present_challenge_to_human_creates_and_cleans_up_temp_file(self):
        observed_paths: list[str] = []

        def fake_open(url: str) -> bool:
            self.assertTrue(url.startswith("file://"))
            path = url[len("file://"):]
            observed_paths.append(path)
            self.assertTrue(os.path.isfile(path))
            with open(path, "r", encoding="utf-8") as f:
                html = f.read()
            self.assertIn(f"data:image/svg+xml;base64,{SAMPLE_SVG_B64}", html)
            return True

        with mock.patch("hbu_challenge_ui.webbrowser.open", side_effect=fake_open):
            result = hbu_challenge_ui.present_challenge_to_human(
                SAMPLE_SVG_B64,
                input_fn=lambda _prompt: "  me4a48 ",
            )

        self.assertEqual(result, "ME4A48")
        self.assertEqual(len(observed_paths), 1)
        self.assertFalse(os.path.exists(observed_paths[0]))

    def test_present_challenge_cleans_up_on_skip_interrupt_and_browser_error(self):
        created_paths: list[str] = []

        def failing_open(url: str) -> bool:
            created_paths.append(url[len("file://"):])
            raise RuntimeError("No GUI browser available")

        with mock.patch("hbu_challenge_ui.webbrowser.open", side_effect=failing_open):
            # Browser failure falls back cleanly to input_fn
            res_valid = hbu_challenge_ui.present_challenge_to_human(
                SAMPLE_SVG_B64,
                input_fn=lambda _prompt: "W7X8Y9",
            )
            self.assertEqual(res_valid, "W7X8Y9")

            # Skip returns None
            res_skip = hbu_challenge_ui.present_challenge_to_human(
                SAMPLE_SVG_B64,
                input_fn=lambda _prompt: "skip",
            )
            self.assertIsNone(res_skip)

            # KeyboardInterrupt / EOFError returns None and still unlinks temp file
            def raise_eof(_prompt: str) -> str:
                raise EOFError("stdin closed")

            res_eof = hbu_challenge_ui.present_challenge_to_human(
                SAMPLE_SVG_B64,
                input_fn=raise_eof,
            )
            self.assertIsNone(res_eof)

        self.assertEqual(len(created_paths), 3)
        for path in created_paths:
            self.assertFalse(os.path.exists(path))


class TestHbuAuthClient(unittest.TestCase):
    """Tests for hbu_auth_client challenge fetch, error mapping, and signup responses."""

    def test_format_proxies_rewrites_socks5_to_socks5h(self):
        self.assertIsNone(hbu_auth_client._format_proxies(None))
        self.assertIsNone(hbu_auth_client._format_proxies("   "))
        self.assertEqual(
            hbu_auth_client._format_proxies("192.0.2.10:8080"),
            {"http": "http://192.0.2.10:8080", "https": "http://192.0.2.10:8080"},
        )
        self.assertEqual(
            hbu_auth_client._format_proxies("socks5://192.0.2.10:1080"),
            {"http": "socks5h://192.0.2.10:1080", "https": "socks5h://192.0.2.10:1080"},
        )

    def test_fetch_image_challenge_success(self):
        fake_resp = _make_fake_response(
            200,
            json_data={"id": "chal_001", "image": SAMPLE_SVG_B64},
        )
        with mock.patch.object(requests.Session, "request", return_value=fake_resp) as req_mock:
            data = hbu_auth_client.fetch_image_challenge(proxy_url="socks5://192.0.2.1:1080")

        self.assertEqual(data, {"id": "chal_001", "image": SAMPLE_SVG_B64})
        req_mock.assert_called_once()
        args, kwargs = req_mock.call_args
        self.assertEqual(args[0], "GET")
        self.assertIn("action=image_challenge", args[1])
        self.assertEqual(kwargs["timeout"], 8.0)

    def test_fetch_image_challenge_invalid_payload_raises_value_error(self):
        bad_json = _make_fake_response(200, json_exc=ValueError("bad json"))
        with mock.patch.object(requests.Session, "request", return_value=bad_json):
            with self.assertRaises(ValueError):
                hbu_auth_client.fetch_image_challenge()

        missing_fields = _make_fake_response(200, json_data={"id": "only_id"})
        with mock.patch.object(requests.Session, "request", return_value=missing_fields):
            with self.assertRaises(ValueError):
                hbu_auth_client.fetch_image_challenge()

    def test_fetch_image_challenge_translates_proxy_and_rate_limit_errors(self):
        proxy = "socks5://192.0.2.2:1080"
        with mock.patch.object(
            requests.Session,
            "request",
            side_effect=requests.exceptions.ProxyError("tunnel refused"),
        ):
            with self.assertRaises(hbu_auth_client.ProxyDeadException) as ctx:
                hbu_auth_client.fetch_image_challenge(proxy_url=proxy)
            self.assertEqual(ctx.exception.proxy_url, proxy)

        rate_resp = _make_fake_response(
            429,
            text="Too Many Requests",
            headers={"Retry-After": "120"},
        )
        with mock.patch.object(requests.Session, "request", return_value=rate_resp):
            with self.assertRaises(hbu_auth_client.RateLimitException) as ctx:
                hbu_auth_client.fetch_image_challenge(proxy_url=proxy)
            self.assertEqual(ctx.exception.status_code, 429)
            self.assertEqual(ctx.exception.retry_after, "120")

    def test_submit_signup_handles_200_400_429_and_network_error(self):
        ok_resp = _make_fake_response(200, json_data={"ok": True, "username": "NovaWolf"})
        with mock.patch.object(requests.Session, "request", return_value=ok_resp) as req_mock:
            res_200 = hbu_auth_client.submit_signup(
                username=" NovaWolf ",
                password="Secret123",
                challenge_id="chal_1",
                challenge_answer=" me4a48 ",
                student_id="FLEET",
                proxy_url="http://192.0.2.3:8080",
            )
        self.assertEqual(
            res_200,
            {"ok": True, "status": 200, "error": None, "data": {"ok": True, "username": "NovaWolf"}},
        )
        sent_json = req_mock.call_args.kwargs["json"]
        self.assertEqual(sent_json["username"], "NovaWolf")
        self.assertEqual(sent_json["challenge_answer"], "ME4A48")
        self.assertEqual(sent_json["student_id"], "FLEET")

        bad_resp = _make_fake_response(400, json_data={"error": "Invalid challenge answer"})
        with mock.patch.object(requests.Session, "request", return_value=bad_resp):
            res_400 = hbu_auth_client.submit_signup("u", "p", "c", "WRONG1")
        self.assertFalse(res_400["ok"])
        self.assertEqual(res_400["status"], 400)
        self.assertEqual(res_400["error"], "Invalid challenge answer")

        limit_resp = _make_fake_response(429, json_data={"message": "Rate limit exceeded"})
        with mock.patch.object(requests.Session, "request", return_value=limit_resp):
            res_429 = hbu_auth_client.submit_signup("u", "p", "c", "ME4A48")
        self.assertFalse(res_429["ok"])
        self.assertEqual(res_429["status"], 429)
        self.assertEqual(res_429["error"], "Rate limit exceeded")

        with mock.patch.object(
            requests.Session,
            "request",
            side_effect=requests.exceptions.ConnectTimeout("timed out"),
        ):
            with self.assertRaises(hbu_auth_client.ProxyDeadException):
                hbu_auth_client.submit_signup("u", "p", "c", "ME4A48", proxy_url="http://192.0.2.3:8080")


class TestProxyRotator(unittest.TestCase):
    """Tests for proxy_rotator 3/hr sliding window, quarantine, and cooldown math."""

    def test_sliding_window_enforces_3_signups_per_hour_and_expires(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            proxy = "socks5://192.0.2.50:1080"
            key = extract_ip_port_key(proxy)
            now = 1_700_000_000.0

            with mock.patch("proxy_rotator.time.time", return_value=now):
                rotator = _create_isolated_rotator(tmpdir, working_proxies=[proxy])
                for _ in range(3):
                    self.assertEqual(rotator.get_proxy(), proxy)
                    rotator.record_signup(proxy)

                self.assertEqual(len(rotator.rate_records[key]), 3)
                self.assertIsNone(rotator.get_proxy())
                self.assertEqual(rotator.get_proxy(allow_rate_limited=True), proxy)
                self.assertEqual(rotator.get_stats()["rate_limited"], 1)

            # Advance just past 3600s window -> timestamps expire and proxy becomes available
            with mock.patch("proxy_rotator.time.time", return_value=now + 3601.0):
                self.assertEqual(rotator.get_proxy(), proxy)
                self.assertEqual(rotator.get_next_available_wait_seconds(), 0.0)
                self.assertEqual(rotator.get_stats()["active"], 1)

    def test_quarantine_removes_proxy_and_persists_across_reloads(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            p1 = "socks5://192.0.2.60:1080"
            p1_alt = "http://192.0.2.60:1080"
            p2 = "https://192.0.2.61:8443"
            rotator = _create_isolated_rotator(tmpdir, working_proxies=[p1, p1_alt, p2])

            # Same IP:port across schemes shares bucket, so only p1 and p2 are loaded
            self.assertEqual(rotator.proxy_pool, [p1, p2])

            rotator.mark_failed(p1, reason="ProxyDeadException")
            self.assertIn(p1, rotator.quarantined)
            self.assertEqual(rotator.proxy_pool, [p2])
            self.assertTrue(os.path.isfile(rotator.quarantine_file))

            # Reloading from the same isolated files keeps p1 quarantined
            reloaded = _create_isolated_rotator(tmpdir, working_proxies=[p1, p1_alt, p2])
            self.assertEqual(reloaded.proxy_pool, [p2])
            self.assertIn(p1, reloaded.quarantined)

    def test_cooldown_wait_calculation_returns_soonest_expiring_slot(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            p1 = "socks5://192.0.2.70:1080"
            p2 = "socks5://192.0.2.71:1080"
            t0 = 1_700_000_000.0

            with mock.patch("proxy_rotator.time.time", return_value=t0):
                rotator = _create_isolated_rotator(tmpdir, working_proxies=[p1, p2])
                self.assertEqual(rotator.get_next_available_wait_seconds(), 0.0)
                self.assertEqual(rotator.get_cooldown_seconds(), 0.0)

            # p1 filled at t0+10, t0+20, t0+30 -> oldest active expires at t0+3610
            # p2 filled at t0+100, t0+200, t0+300 -> oldest active expires at t0+3700
            p1_times = [t0 + 10.0, t0 + 20.0, t0 + 30.0]
            p2_times = [t0 + 100.0, t0 + 200.0, t0 + 300.0]
            for ts in p1_times:
                with mock.patch("proxy_rotator.time.time", return_value=ts):
                    rotator.record_signup(p1)
            for ts in p2_times:
                with mock.patch("proxy_rotator.time.time", return_value=ts):
                    rotator.record_signup(p2)

            check_time = t0 + 500.0
            with mock.patch("proxy_rotator.time.time", return_value=check_time):
                # Soonest slot is p1's t0+10.0 -> remaining wait = 3600 - (500 - 10) = 3110.0s
                wait_s = rotator.get_next_available_wait_seconds()
                self.assertAlmostEqual(wait_s, 3110.0, places=3)
                self.assertAlmostEqual(rotator.get_cooldown_seconds(), 3110.0, places=3)


class TestAccountVault(unittest.TestCase):
    """Tests for account_vault collision detection and atomic CSV persistence."""

    def test_collision_checking_reads_active_and_banned_csvs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            active_csv = os.path.join(tmpdir, "simulated_accounts.csv")
            banned_csv = os.path.join(tmpdir, "banned_accounts.csv")

            with open(active_csv, "w", encoding="utf-8", newline="") as f:
                f.write("username,password,student_id,coins,created_at\nActiveUser1,pw,FLEET,100,2026-01-01\n")
            with open(banned_csv, "w", encoding="utf-8", newline="") as f:
                f.write("username,password,student_id,coins,created_at\nBannedUser2,pw,FLEET,100,2026-01-01\n")

            with mock.patch.object(account_vault, "SIMULATED_CSV", active_csv), \
                 mock.patch.object(account_vault, "BANNED_CSV", banned_csv):
                only_active = account_vault.get_existing_usernames(include_banned=False)
                combined = account_vault.get_existing_usernames(include_banned=True)

                self.assertEqual(only_active, {"activeuser1"})
                self.assertEqual(combined, {"activeuser1", "banneduser2"})

                # Force fallback path by ensuring candidate pools collide via mocked choice
                with mock.patch("account_vault._compose_candidate", return_value="BannedUser2"):
                    generated = account_vault.generate_username()

                self.assertRegex(generated, account_vault.USERNAME_PATTERN)
                self.assertNotIn(generated.lower(), combined)

    def test_generate_password_requirements(self):
        with self.assertRaises(ValueError):
            account_vault.generate_password(length=5)

        pw = account_vault.generate_password(length=14)
        self.assertEqual(len(pw), 14)
        self.assertTrue(any(c.isupper() for c in pw))
        self.assertTrue(any(c.islower() for c in pw))
        self.assertTrue(any(c.isdigit() for c in pw))

    def test_atomic_csv_persistence_preserves_headers_and_cleans_tmp_on_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target_csv = os.path.join(tmpdir, "simulated_accounts.csv")
            with mock.patch.object(account_vault, "SIMULATED_CSV", target_csv):
                account_vault.append_account_atomically(
                    username="NovaWolf1",
                    password="Password1A",
                    coins=150,
                    student_id="FLEET",
                    created_at="2026-11-11 10:00:00",
                )
                self.assertFalse(os.path.exists(f"{target_csv}.bak"))

                account_vault.append_account_atomically(
                    username="ApexFox2",
                    password="Password2B",
                    coins=225,
                    student_id="S123",
                    created_at="2026-11-11 10:05:00",
                )
                self.assertTrue(os.path.isfile(f"{target_csv}.bak"))

                with open(target_csv, "r", encoding="utf-8", newline="") as f:
                    rows = list(csv.DictReader(f))
                self.assertEqual(len(rows), 2)
                self.assertEqual(rows[0]["username"], "NovaWolf1")
                self.assertEqual(rows[1]["username"], "ApexFox2")
                self.assertEqual(rows[1]["coins"], "225")

                # Simulated os.replace error must clean up .tmp without losing existing rows
                with mock.patch("os.replace", side_effect=OSError("disk error")):
                    with self.assertRaises(OSError):
                        account_vault.append_account_atomically("FailUser", "Password3C")

                leftover_tmps = [fn for fn in os.listdir(tmpdir) if fn.endswith(".tmp")]
                self.assertEqual(leftover_tmps, [])
                with open(target_csv, "r", encoding="utf-8", newline="") as f:
                    rows_after = list(csv.DictReader(f))
                self.assertEqual(len(rows_after), 2)


class TestCreateHbuAccountsBatch(unittest.TestCase):
    """End-to-end mocked tests for create_hbu_accounts.run_batch_creation()."""

    def test_end_to_end_event_sequence_and_csv_persistence(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            sim_csv = os.path.join(tmpdir, "simulated_accounts.csv")
            ban_csv = os.path.join(tmpdir, "banned_accounts.csv")
            proxy = "socks5://192.0.2.100:1080"
            rotator = _create_isolated_rotator(tmpdir, working_proxies=[proxy])
            events: list[dict] = []

            with mock.patch.object(account_vault, "SIMULATED_CSV", sim_csv), \
                 mock.patch.object(account_vault, "BANNED_CSV", ban_csv), \
                 mock.patch.object(
                     create_hbu_accounts,
                     "fetch_image_challenge",
                     return_value={"id": "chal_1", "image": SAMPLE_SVG_B64},
                 ), \
                 mock.patch.object(
                     create_hbu_accounts,
                     "submit_signup",
                     return_value={"ok": True, "status": 200, "data": {"ok": True}, "error": None},
                 ), \
                 mock.patch.object(
                     create_hbu_accounts,
                     "claim_welcome_spin",
                     return_value=(True, 75, 175),
                 ):
                summary = create_hbu_accounts.run_batch_creation(
                    count=1,
                    concurrency=1,
                    use_proxies=True,
                    on_event=events.append,
                    student_id="QA_FLEET",
                    challenge_handler=lambda _b64: "ME4A48",
                    rotator=rotator,
                )

            self.assertEqual(summary["created"], 1)
            self.assertEqual(summary["failed"], 0)
            self.assertFalse(summary["stopped"])
            self.assertEqual(len(summary["accounts"]), 1)

            types = [e["type"] for e in events]
            self.assertEqual(types, ["status", "proxy", "created", "done"])
            self.assertEqual(events[1]["action"], "selected")
            self.assertEqual(events[1]["proxy"], proxy)
            self.assertEqual(events[2]["coins"], 175)
            self.assertEqual(events[2]["prize"], 75)
            self.assertTrue(events[2]["spin_ok"])
            self.assertEqual(events[2]["student_id"], "QA_FLEET")
            self.assertEqual(events[3]["created"], 1)
            self.assertEqual(events[3]["failed"], 0)
            self.assertFalse(events[3]["stopped"])

            with open(sim_csv, "r", encoding="utf-8", newline="") as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["username"], summary["accounts"][0]["username"])
            self.assertEqual(rows[0]["coins"], "175")
            self.assertEqual(len(rotator.rate_records[extract_ip_port_key(proxy)]), 1)

    def test_failover_on_proxy_dead_exception(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            sim_csv = os.path.join(tmpdir, "simulated_accounts.csv")
            ban_csv = os.path.join(tmpdir, "banned_accounts.csv")
            p_dead = "socks5://192.0.2.101:1080"
            p_live = "socks5://192.0.2.102:1080"
            rotator = _create_isolated_rotator(tmpdir, working_proxies=[p_dead, p_live])
            events: list[dict] = []

            def fake_fetch(proxy_url=None, timeout=8.0):
                if proxy_url == p_dead:
                    raise hbu_auth_client.ProxyDeadException("dead tunnel", proxy_url=proxy_url)
                return {"id": "chal_2", "image": SAMPLE_SVG_B64}

            with mock.patch.object(account_vault, "SIMULATED_CSV", sim_csv), \
                 mock.patch.object(account_vault, "BANNED_CSV", ban_csv), \
                 mock.patch.object(create_hbu_accounts, "fetch_image_challenge", side_effect=fake_fetch), \
                 mock.patch.object(
                     create_hbu_accounts,
                     "submit_signup",
                     return_value={"ok": True, "status": 200, "data": {}, "error": None},
                 ), \
                 mock.patch.object(
                     create_hbu_accounts,
                     "claim_welcome_spin",
                     return_value=(False, 0, 0),
                 ):
                summary = create_hbu_accounts.run_batch_creation(
                    count=1,
                    concurrency=1,
                    on_event=events.append,
                    challenge_handler=lambda _b64: "K9Z2NP",
                    rotator=rotator,
                    max_retries=3,
                )

            self.assertEqual(summary["created"], 1)
            self.assertEqual(summary["failed"], 0)
            self.assertIn(p_dead, rotator.quarantined)
            self.assertNotIn(p_dead, rotator.proxy_pool)
            self.assertIn(p_live, rotator.proxy_pool)

            proxy_events = [e for e in events if e["type"] == "proxy"]
            self.assertEqual(
                [(e["proxy"], e["action"]) for e in proxy_events],
                [(p_dead, "selected"), (p_dead, "failed"), (p_live, "selected")],
            )
            self.assertEqual(events[-1]["type"], "done")

    def test_cooldown_rotation_on_rate_limit_exception(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            sim_csv = os.path.join(tmpdir, "simulated_accounts.csv")
            ban_csv = os.path.join(tmpdir, "banned_accounts.csv")
            p1 = "socks5://192.0.2.103:1080"
            p2 = "socks5://192.0.2.104:1080"
            rotator = _create_isolated_rotator(tmpdir, working_proxies=[p1, p2])
            events: list[dict] = []

            def fake_fetch(proxy_url=None, timeout=8.0):
                if proxy_url == p1:
                    raise hbu_auth_client.RateLimitException("429 upstream", retry_after="60")
                return {"id": "chal_3", "image": SAMPLE_SVG_B64}

            with mock.patch.object(account_vault, "SIMULATED_CSV", sim_csv), \
                 mock.patch.object(account_vault, "BANNED_CSV", ban_csv), \
                 mock.patch.object(create_hbu_accounts, "fetch_image_challenge", side_effect=fake_fetch), \
                 mock.patch.object(
                     create_hbu_accounts,
                     "submit_signup",
                     return_value={"ok": True, "status": 200, "data": {}, "error": None},
                 ), \
                 mock.patch.object(
                     create_hbu_accounts,
                     "claim_welcome_spin",
                     return_value=(True, 25, 125),
                 ):
                summary = create_hbu_accounts.run_batch_creation(
                    count=1,
                    concurrency=1,
                    on_event=events.append,
                    challenge_handler=lambda _b64: "R4T5Y6",
                    rotator=rotator,
                    max_retries=3,
                )

            self.assertEqual(summary["created"], 1)
            self.assertEqual(summary["failed"], 0)
            self.assertNotIn(p1, rotator.quarantined)
            self.assertEqual(len(rotator.rate_records[extract_ip_port_key(p1)]), 1)
            self.assertEqual(len(rotator.rate_records[extract_ip_port_key(p2)]), 1)

            proxy_actions = [(e["proxy"], e["action"]) for e in events if e["type"] == "proxy"]
            self.assertEqual(
                proxy_actions,
                [(p1, "selected"), (p1, "rate_limited"), (p2, "selected")],
            )

    def test_failed_event_emitted_when_all_proxies_exhaust_rate_limit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            sim_csv = os.path.join(tmpdir, "simulated_accounts.csv")
            ban_csv = os.path.join(tmpdir, "banned_accounts.csv")
            p1 = "socks5://192.0.2.105:1080"
            rotator = _create_isolated_rotator(
                tmpdir,
                working_proxies=[p1],
                max_signups_per_hour=1,
            )
            events: list[dict] = []

            with mock.patch.object(account_vault, "SIMULATED_CSV", sim_csv), \
                 mock.patch.object(account_vault, "BANNED_CSV", ban_csv), \
                 mock.patch.object(
                     create_hbu_accounts,
                     "fetch_image_challenge",
                     side_effect=hbu_auth_client.RateLimitException("429"),
                 ):
                summary = create_hbu_accounts.run_batch_creation(
                    count=1,
                    concurrency=1,
                    on_event=events.append,
                    challenge_handler=lambda _b64: "R4T5Y6",
                    rotator=rotator,
                    max_retries=2,
                )

            self.assertEqual(summary["created"], 0)
            self.assertEqual(summary["failed"], 1)
            event_types = [e["type"] for e in events]
            self.assertIn("failed", event_types)
            self.assertEqual(events[-1]["type"], "done")
            self.assertEqual(events[-1]["failed"], 1)

    def test_stop_event_interruption_pre_set_and_mid_batch(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            sim_csv = os.path.join(tmpdir, "simulated_accounts.csv")
            ban_csv = os.path.join(tmpdir, "banned_accounts.csv")
            rotator = _create_isolated_rotator(tmpdir, use_proxies=False)

            # 1) Pre-set stop_event exits immediately without hitting fetch
            pre_stop = threading.Event()
            pre_stop.set()
            pre_events: list[dict] = []
            with mock.patch.object(account_vault, "SIMULATED_CSV", sim_csv), \
                 mock.patch.object(account_vault, "BANNED_CSV", ban_csv), \
                 mock.patch.object(create_hbu_accounts, "fetch_image_challenge") as fetch_mock:
                pre_summary = create_hbu_accounts.run_batch_creation(
                    count=3,
                    concurrency=1,
                    use_proxies=False,
                    on_event=pre_events.append,
                    stop_event=pre_stop,
                    rotator=rotator,
                )
            fetch_mock.assert_not_called()
            self.assertTrue(pre_summary["stopped"])
            self.assertEqual(pre_summary["created"], 0)
            self.assertEqual([e["type"] for e in pre_events], ["status", "done"])
            self.assertTrue(pre_events[-1]["stopped"])

            # 2) Mid-batch stop_event triggered after the first account completes
            mid_stop = threading.Event()
            mid_events: list[dict] = []

            def on_event_stop_after_first(event: dict) -> None:
                mid_events.append(event)
                if event.get("type") == "created":
                    mid_stop.set()

            with mock.patch.object(account_vault, "SIMULATED_CSV", sim_csv), \
                 mock.patch.object(account_vault, "BANNED_CSV", ban_csv), \
                 mock.patch.object(
                     create_hbu_accounts,
                     "fetch_image_challenge",
                     return_value={"id": "chal_mid", "image": SAMPLE_SVG_B64},
                 ), \
                 mock.patch.object(
                     create_hbu_accounts,
                     "submit_signup",
                     return_value={"ok": True, "status": 200, "data": {}, "error": None},
                 ), \
                 mock.patch.object(
                     create_hbu_accounts,
                     "claim_welcome_spin",
                     return_value=(True, 50, 150),
                 ):
                mid_summary = create_hbu_accounts.run_batch_creation(
                    count=3,
                    concurrency=1,
                    use_proxies=False,
                    on_event=on_event_stop_after_first,
                    stop_event=mid_stop,
                    challenge_handler=lambda _b64: "ME4A48",
                    rotator=rotator,
                )

            self.assertTrue(mid_summary["stopped"])
            self.assertEqual(mid_summary["created"], 1)
            self.assertEqual(mid_events[-1]["type"], "done")
            self.assertTrue(mid_events[-1]["stopped"])
            self.assertEqual(mid_events[-1]["created"], 1)


if __name__ == "__main__":
    unittest.main()
