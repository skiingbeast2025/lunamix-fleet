#!/usr/bin/env python3
"""
test_proxy_rotator.py - Unit & Concurrency Verification Suite for proxy_rotator.py
"""

import concurrent.futures
import json
import os
import socket
import tempfile
import threading
import time
import unittest
from unittest import mock

from proxy_rotator import ProxyRotator, extract_ip_port_key, probe_proxy


class TestProxyRotator(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.base = self.tmpdir.name
        self.quota_file = os.path.join(self.base, "logs", "proxy_rate_limits.json")
        self.quarantine_file = os.path.join(self.base, "logs", "quarantined_proxies.json")
        self.working_txt = os.path.join(self.base, "working_proxies.txt")
        self.proxies_json = os.path.join(self.base, "proxies.json")
        self.proxies_txt = os.path.join(self.base, "proxies.txt")

    def tearDown(self):
        self.tmpdir.cleanup()

    def _make_rotator(self, **kwargs) -> ProxyRotator:
        defaults = dict(
            use_proxies=True,
            max_signups_per_hour=3,
            window_seconds=3600,
            quota_file=self.quota_file,
            quarantine_file=self.quarantine_file,
            working_txt=self.working_txt,
            proxies_json=self.proxies_json,
            proxies_txt=self.proxies_txt,
        )
        defaults.update(kwargs)
        return ProxyRotator(**defaults)

    def test_extract_ip_port_key(self):
        self.assertEqual(extract_ip_port_key("socks5://101.36.104.239:10808"), "101.36.104.239:10808")
        self.assertEqual(extract_ip_port_key("socks5h://101.36.104.239:10808"), "101.36.104.239:10808")
        self.assertEqual(extract_ip_port_key("http://user:pass@101.36.104.239:10808"), "101.36.104.239:10808")
        self.assertEqual(extract_ip_port_key("101.36.104.239:10808"), "101.36.104.239:10808")
        self.assertEqual(extract_ip_port_key("socks5://45.74.31.40:7316"), "45.74.31.40:7316")
        self.assertEqual(extract_ip_port_key("socks5://45.74.31.40:7610"), "45.74.31.40:7610")

    def test_ingestion_priority_and_backup_limit(self):
        with open(self.working_txt, "w", encoding="utf-8") as f:
            f.write("# comment\nsocks5://10.0.0.1:1080\nhttp://10.0.0.2:8080\n")

        with open(self.proxies_json, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "proxies": [
                        {"url": "http://10.0.0.1:1080", "status": "working"},  # duplicate IP:port
                        {"url": "http://10.0.0.3:8080", "status": "dead"},     # skipped
                        {"url": "socks4://10.0.0.4:1080", "status": None},     # untested
                        {"url": "https://10.0.0.5:8443", "status": "working"}, # working first
                    ]
                },
                f,
            )

        with open(self.proxies_txt, "w", encoding="utf-8") as f:
            f.write("http://10.0.0.6:3128\nhttp://10.0.0.7:3128\n")

        # Default: working_proxies.txt only (no backups)
        rot_no_backup = self._make_rotator()
        self.assertEqual(rot_no_backup.proxy_pool, ["socks5://10.0.0.1:1080", "http://10.0.0.2:8080"])
        self.assertEqual(
            rot_no_backup.get_stats(),
            {"active": 2, "working_verified": 2, "quarantined": 0, "rate_limited": 0},
        )

        # With backup_limit=3: working_txt (2) + json working (10.0.0.5) + json untested (10.0.0.4) + proxies.txt (10.0.0.6)
        rot_backups = self._make_rotator(include_backups=True, backup_limit=3)
        self.assertEqual(
            rot_backups.proxy_pool,
            [
                "socks5://10.0.0.1:1080",
                "http://10.0.0.2:8080",
                "https://10.0.0.5:8443",
                "socks4://10.0.0.4:1080",
                "http://10.0.0.6:3128",
            ],
        )
        self.assertEqual(
            rot_backups.get_stats(),
            {"active": 5, "working_verified": 3, "quarantined": 0, "rate_limited": 0},
        )

    def test_sliding_window_rate_limit_and_protocol_dedup(self):
        with open(self.working_txt, "w", encoding="utf-8") as f:
            f.write("socks5://192.0.2.10:1080\n")

        rot = self._make_rotator(max_signups_per_hour=3, window_seconds=3600)

        for _ in range(3):
            p = rot.get_proxy()
            self.assertEqual(p, "socks5://192.0.2.10:1080")
            rot.record_signup(p)

        # Same IP:port via http:// shares the exact same rate bucket
        self.assertEqual(len(rot.rate_records["192.0.2.10:1080"]), 3)
        self.assertIsNone(rot.get_proxy())
        self.assertEqual(rot.get_proxy(allow_rate_limited=True), "socks5://192.0.2.10:1080")
        self.assertGreater(rot.get_cooldown_seconds(), 3500)
        self.assertEqual(
            rot.get_stats(),
            {"active": 0, "working_verified": 1, "quarantined": 0, "rate_limited": 1},
        )

        # Verify atomic persistence & reload
        rot2 = self._make_rotator()
        self.assertIsNone(rot2.get_proxy())
        self.assertEqual(len(rot2.rate_records["192.0.2.10:1080"]), 3)

        # Expire timestamps in file and reload
        with open(self.quota_file, "w", encoding="utf-8") as f:
            json.dump({"http://192.0.2.10:1080": [time.time() - 3605]}, f)
        rot3 = self._make_rotator()
        self.assertEqual(rot3.get_proxy(), "socks5://192.0.2.10:1080")

    def test_quarantine_persistence(self):
        with open(self.working_txt, "w", encoding="utf-8") as f:
            f.write("socks5://192.0.2.20:1080\nhttp://192.0.2.21:8080\n")

        rot = self._make_rotator()
        rot.mark_failed("socks5://192.0.2.20:1080", reason="ConnectionRefused")
        self.assertEqual(rot.proxy_pool, ["http://192.0.2.21:8080"])
        self.assertEqual(
            rot.get_stats(),
            {"active": 1, "working_verified": 1, "quarantined": 1, "rate_limited": 0},
        )

        rot2 = self._make_rotator()
        self.assertEqual(rot2.proxy_pool, ["http://192.0.2.21:8080"])
        self.assertEqual(rot2.get_stats()["quarantined"], 1)

    def test_probe_proxy_and_warm_pool(self):
        # Spin up an ephemeral local TCP listener for deterministic TCP probe testing
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.bind(("127.0.0.1", 0))
        srv.listen(5)
        live_port = srv.getsockname()[1]

        # Find an unused port for dead proxy
        dead_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        dead_sock.bind(("127.0.0.1", 0))
        dead_port = dead_sock.getsockname()[1]
        dead_sock.close()

        stop_flag = threading.Event()

        def accept_loop():
            srv.settimeout(0.2)
            while not stop_flag.is_set():
                try:
                    conn, _ = srv.accept()
                    conn.close()
                except socket.timeout:
                    continue
                except OSError:
                    break

        t = threading.Thread(target=accept_loop, daemon=True)
        t.start()

        try:
            live_url = f"http://127.0.0.1:{live_port}"
            dead_url = f"http://127.0.0.1:{dead_port}"

            self.assertTrue(probe_proxy(live_url, timeout=1.0, tcp_only=True))
            self.assertFalse(probe_proxy(dead_url, timeout=0.5, tcp_only=True))

            with open(self.working_txt, "w", encoding="utf-8") as f:
                f.write(f"{live_url}\n{dead_url}\n")

            rot = self._make_rotator()
            # Test full probe_proxy with mocked requests.Session.get
            with mock.patch("proxy_rotator.requests.Session.get") as mock_get:
                mock_resp = mock.Mock(status_code=200)
                mock_get.return_value = mock_resp
                live_list = rot.warm_pool(target_size=1, timeout=1.0, max_workers=2)

            self.assertEqual(live_list, [live_url])
            self.assertEqual(rot.proxy_pool, [live_url])
            self.assertIn(dead_url, rot.quarantined)
        finally:
            stop_flag.set()
            srv.close()
            t.join(timeout=2.0)

    def test_concurrent_thread_safety(self):
        with open(self.working_txt, "w", encoding="utf-8") as f:
            for i in range(1, 21):
                f.write(f"socks5://198.51.100.{i}:1080\n")

        rot = self._make_rotator(max_signups_per_hour=3)

        def worker(idx: int):
            for step in range(5):
                proxy = rot.get_proxy()
                if proxy:
                    if idx % 7 == 0 and step == 4:
                        rot.mark_failed(proxy, reason="TestFail")
                    else:
                        rot.record_signup(proxy)
                rot.get_stats()

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
            futures = [pool.submit(worker, i) for i in range(20)]
            for f in futures:
                f.result(timeout=5)

        stats = rot.get_stats()
        self.assertEqual(stats["active"] + stats["rate_limited"], len(rot.proxy_pool))
        self.assertGreaterEqual(stats["quarantined"], 0)


if __name__ == "__main__":
    unittest.main()
