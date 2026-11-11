#!/usr/bin/env python3
"""
proxy_rotator.py - Thread-Safe Proxy Pool & Per-IP Rate Limiting Manager

Coordinates smart proxy pool loading (verified working_proxies.txt first, then
protocol-prioritized sampling from proxies.txt), round-robin rotation, per-IP:port
sliding-window rate tracking (max 3 signups / 3600s), TCP/HTTPS health probing,
quarantine persistence, and atomic JSON state saves for HBU2 account creation.
"""

import argparse
import concurrent.futures
import json
import logging
import math
import os
import random
import socket
import tempfile
import threading
import time
from typing import Any, Optional
from urllib.parse import urlparse

import requests

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOGS_DIR = os.path.join(BASE_DIR, "logs")
QUOTA_FILE = os.path.join(LOGS_DIR, "proxy_rate_limits.json")
QUARANTINE_FILE = os.path.join(LOGS_DIR, "quarantined_proxies.json")
WORKING_TXT = os.path.join(BASE_DIR, "working_proxies.txt")
PROXIES_TXT = os.path.join(BASE_DIR, "proxies.txt")

DEFAULT_WINDOW_SECONDS = 3600
DEFAULT_MAX_SIGNUPS_PER_HOUR = 3
DEFAULT_MAX_POOL_SIZE = 2000
DEFAULT_PROBE_URL = "https://1.1.1.1/cdn-cgi/trace"


def extract_ip_port_key(proxy_url: str) -> str:
    """
    Normalizes a proxy URL into a canonical 'IP:port' key so the same endpoint
    across protocols (http/https/socks4/socks5/socks5h) shares one quota bucket.
    """
    if not proxy_url or not isinstance(proxy_url, str):
        return ""
    raw = proxy_url.strip()
    if not raw:
        return ""

    candidate = raw if "://" in raw else f"http://{raw}"
    try:
        parsed = urlparse(candidate)
        host = parsed.hostname
        port = parsed.port
        if host and port:
            if ":" in host and not host.startswith("["):
                return f"[{host}]:{port}"
            return f"{host}:{port}"
        if parsed.netloc:
            return parsed.netloc.rsplit("@", 1)[-1]
    except ValueError:
        return ""
    return ""


def parse_proxy_host_port(proxy_url: str) -> Optional[tuple[str, int]]:
    """Extracts (host, port) from a proxy URL for TCP connectivity checks."""
    if not proxy_url or not isinstance(proxy_url, str):
        return None
    raw = proxy_url.strip()
    if not raw:
        return None

    candidate = raw if "://" in raw else f"http://{raw}"
    try:
        parsed = urlparse(candidate)
        if parsed.hostname and parsed.port:
            return parsed.hostname, int(parsed.port)
    except ValueError:
        return None
    return None


def _get_proxy_scheme(proxy_url: str) -> str:
    """Returns the lowercase scheme of a proxy URL, defaulting to 'http'."""
    if "://" not in proxy_url:
        return "http"
    return proxy_url.split("://", 1)[0].strip().lower() or "http"


def format_requests_proxies(proxy_url: str) -> Optional[dict[str, str]]:
    """Builds a requests-compatible proxies mapping (using socks5h:// for remote DNS)."""
    if not proxy_url or not isinstance(proxy_url, str):
        return None
    url = proxy_url.strip()
    if not url:
        return None
    if "://" not in url:
        url = f"http://{url}"
    if url.lower().startswith("socks5://"):
        url = "socks5h://" + url[len("socks5://"):]
    return {"http": url, "https": url}


def probe_proxy(
    proxy_url: str,
    timeout: float = 3.0,
    target_url: str = DEFAULT_PROBE_URL,
    tcp_only: bool = False,
) -> bool:
    """
    Validates proxy liveness with a fast TCP pre-check followed by an optional
    lightweight HTTPS GET through the tunnel.
    """
    host_port = parse_proxy_host_port(proxy_url)
    if not host_port:
        return False

    host, port = host_port
    tcp_timeout = min(1.5, max(0.2, float(timeout)))
    try:
        with socket.create_connection((host, port), timeout=tcp_timeout):
            pass
    except OSError:
        return False

    if tcp_only:
        return True

    proxies = format_requests_proxies(proxy_url)
    if not proxies:
        return False

    try:
        with requests.Session() as session:
            session.trust_env = False
            resp = session.get(
                target_url,
                proxies=proxies,
                timeout=timeout,
                allow_redirects=False,
                verify=True,
            )
            return resp.status_code in (200, 204, 301, 302, 307)
    except requests.RequestException:
        return False


def _is_valid_timestamp(ts: Any) -> bool:
    """Validates numeric epoch timestamps while rejecting booleans and NaN/Inf."""
    return (
        isinstance(ts, (int, float))
        and not isinstance(ts, bool)
        and math.isfinite(ts)
    )


def _reservoir_insert(
    buf: list[str],
    seen_count: int,
    item: str,
    limit: int,
    rng: random.Random,
) -> None:
    """Maintains a uniform reservoir sample of up to `limit` items (1-indexed `seen_count`)."""
    if limit <= 0:
        return
    if len(buf) < limit:
        buf.append(item)
        return
    idx = rng.randrange(seen_count)
    if idx < limit:
        buf[idx] = item


def _load_json_safe(file_path: str, expected_type: type) -> Optional[Any]:
    """
    Safely loads JSON from disk, returning None if the file is missing, empty,
    malformed, unreadable, or does not match `expected_type`.
    """
    if not file_path or not os.path.exists(file_path):
        return None
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            raw = f.read().strip()
        if not raw:
            return None
        data = json.loads(raw)
        if not isinstance(data, expected_type):
            logger.warning(
                "Unexpected JSON root type in %s (expected %s, got %s)",
                file_path,
                expected_type.__name__,
                type(data).__name__,
            )
            return None
        return data
    except (OSError, json.JSONDecodeError, ValueError, TypeError) as exc:
        logger.warning("Failed to load %s; ignoring corrupt/unreadable state: %s", file_path, exc)
        return None


def _atomic_write_json(file_path: str, payload: Any) -> None:
    """Atomically persists JSON state via a same-directory temporary file + os.replace."""
    parent_dir = os.path.dirname(os.path.abspath(file_path))
    os.makedirs(parent_dir, exist_ok=True)
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=parent_dir,
            prefix=f".{os.path.basename(file_path)}.",
            suffix=".tmp",
            delete=False,
        ) as tmp_file:
            tmp_path = tmp_file.name
            json.dump(payload, tmp_file, indent=2, sort_keys=True)
            tmp_file.flush()
            os.fsync(tmp_file.fileno())
        os.replace(tmp_path, file_path)
    except (OSError, TypeError, ValueError) as exc:
        logger.warning("Atomic JSON write failed for %s: %s", file_path, exc)
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


class ProxyRotator:
    """Thread-safe proxy pool manager with per-IP:port sliding-window rate limits."""

    def __init__(
        self,
        use_proxies: bool = True,
        max_signups_per_hour: int = DEFAULT_MAX_SIGNUPS_PER_HOUR,
        window_seconds: int = DEFAULT_WINDOW_SECONDS,
        max_pool_size: int = DEFAULT_MAX_POOL_SIZE,
        include_backups: bool = True,
        backup_limit: Optional[int] = None,
        quota_file: str = QUOTA_FILE,
        quarantine_file: str = QUARANTINE_FILE,
        working_txt: str = WORKING_TXT,
        proxies_txt: str = PROXIES_TXT,
        proxies_json: Optional[str] = None,
        seed: Optional[int] = None,
    ):
        self.use_proxies = bool(use_proxies)
        self.max_signups_per_hour = max(1, int(max_signups_per_hour))
        self.window_seconds = max(1, int(window_seconds))
        self.max_pool_size = max(0, int(max_pool_size))
        self.include_backups = bool(include_backups)
        self.backup_limit = None if backup_limit is None else max(0, int(backup_limit))

        self.quota_file = quota_file
        self.quarantine_file = quarantine_file
        self.working_txt = working_txt
        self.proxies_txt = proxies_txt
        self.proxies_json = proxies_json
        self._rng = random.Random(seed)

        self.lock = threading.RLock()
        self.proxy_pool: list[str] = []
        self.verified_keys: set[str] = set()
        self.quarantined: set[str] = set()
        self._quarantined_keys: set[str] = set()
        self.rate_records: dict[str, list[float]] = {}  # { "ip:port": [ts1, ts2, ...] }
        self.index = 0

        os.makedirs(os.path.dirname(os.path.abspath(self.quota_file)), exist_ok=True)
        os.makedirs(os.path.dirname(os.path.abspath(self.quarantine_file)), exist_ok=True)

        self._load_state()
        if self.use_proxies:
            self._load_proxies()

    def _clean_old_timestamps(self, timestamps: list[Any], now: Optional[float] = None) -> list[float]:
        """Filters timestamps to valid values within the active sliding window."""
        current = time.time() if now is None else now
        cutoff = current - self.window_seconds
        return [
            float(ts)
            for ts in timestamps
            if _is_valid_timestamp(ts) and float(ts) > cutoff
        ]

    def _load_state(self) -> None:
        """Loads persistent rate-limit timestamps and quarantined proxy list safely."""
        now = time.time()
        loaded_rates: dict[str, list[float]] = {}
        raw_rates = _load_json_safe(self.quota_file, dict)
        if raw_rates is not None:
            for raw_key, ts_list in raw_rates.items():
                if not isinstance(ts_list, list):
                    continue
                ip_port_key = extract_ip_port_key(str(raw_key))
                if not ip_port_key:
                    continue
                cleaned = self._clean_old_timestamps(ts_list, now=now)
                if cleaned:
                    existing = loaded_rates.setdefault(ip_port_key, [])
                    existing.extend(cleaned)
                    existing.sort()

        loaded_quarantine: set[str] = set()
        loaded_quarantine_keys: set[str] = set()
        raw_quar = _load_json_safe(self.quarantine_file, list)
        if raw_quar is not None:
            for item in raw_quar:
                if isinstance(item, str) and item.strip():
                    proxy_url = item.strip()
                    loaded_quarantine.add(proxy_url)
                    key = extract_ip_port_key(proxy_url)
                    if key:
                        loaded_quarantine_keys.add(key)

        with self.lock:
            self.rate_records = loaded_rates
            self.quarantined = loaded_quarantine
            self._quarantined_keys = loaded_quarantine_keys

    def _save_state(self) -> None:
        """Persists pruned rate limits and quarantined proxies atomically."""
        with self.lock:
            now = time.time()
            pruned_rates: dict[str, list[float]] = {}
            for key, timestamps in self.rate_records.items():
                cleaned = self._clean_old_timestamps(timestamps, now=now)
                if cleaned:
                    cleaned.sort()
                    pruned_rates[key] = cleaned
            self.rate_records = pruned_rates
            quarantine_snapshot = sorted(self.quarantined)

            _atomic_write_json(self.quota_file, pruned_rates)
            _atomic_write_json(self.quarantine_file, quarantine_snapshot)

    def _is_quarantined(self, proxy_url: str, key: str) -> bool:
        return proxy_url in self.quarantined or (bool(key) and key in self._quarantined_keys)

    def _add_candidate(
        self,
        pool: list[str],
        seen_keys: set[str],
        proxy_url: str,
        verified: bool = False,
    ) -> bool:
        """Adds a deduplicated, non-quarantined proxy URL to the candidate pool."""
        key = extract_ip_port_key(proxy_url)
        if not key or key in seen_keys or self._is_quarantined(proxy_url, key):
            return False
        seen_keys.add(key)
        pool.append(proxy_url)
        if verified:
            self.verified_keys.add(key)
        return True

    def _sample_proxies_txt(self, limit: int, seen_keys: set[str]) -> list[str]:
        """
        Streams proxies.txt in a single pass and prioritizes protocols in order:
        socks5/socks5h -> https -> socks4 -> http (uniform reservoir-sampled).
        """
        if limit <= 0 or not self.proxies_txt or not os.path.exists(self.proxies_txt):
            return []

        buckets: dict[str, list[str]] = {
            "socks5": [],
            "https": [],
            "socks4": [],
            "http": [],
        }
        counts: dict[str, int] = {k: 0 for k in buckets}

        try:
            with open(self.proxies_txt, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    url = line.strip()
                    if not url or url.startswith("#"):
                        continue
                    key = extract_ip_port_key(url)
                    if not key or key in seen_keys or self._is_quarantined(url, key):
                        continue

                    scheme = _get_proxy_scheme(url)
                    if scheme in ("socks5", "socks5h"):
                        tier = "socks5"
                    elif scheme in buckets:
                        tier = scheme
                    else:
                        tier = "http"

                    counts[tier] += 1
                    _reservoir_insert(buckets[tier], counts[tier], url, limit, self._rng)
        except OSError as exc:
            logger.warning("Failed to read proxy list %s: %s", self.proxies_txt, exc)
            return []

        ordered: list[str] = []
        for tier in ("socks5", "https", "socks4", "http"):
            ordered.extend(buckets[tier])
        return ordered

    def _load_proxies(self) -> None:
        """
        Loads verified proxies from working_proxies.txt first, then fills remaining
        capacity up to max_pool_size from proxies.txt prioritizing socks5:// and
        https:// entries before sampling http:// entries.
        """
        with self.lock:
            pool: list[str] = []
            seen_keys: set[str] = set()
            self.verified_keys.clear()

            if self.max_pool_size <= 0:
                self.proxy_pool = []
                self.index = 0
                return

            # Priority 1: Verified live proxies in working_proxies.txt
            if self.working_txt and os.path.exists(self.working_txt):
                try:
                    with open(self.working_txt, "r", encoding="utf-8", errors="replace") as f:
                        for line in f:
                            if len(pool) >= self.max_pool_size:
                                break
                            url = line.strip()
                            if url and not url.startswith("#"):
                                self._add_candidate(pool, seen_keys, url, verified=True)
                except OSError as exc:
                    logger.warning("Failed to read %s: %s", self.working_txt, exc)

            # Priority 2: Protocol-prioritized sampling from proxies.txt
            remaining = self.max_pool_size - len(pool)
            if self.backup_limit is not None:
                remaining = min(remaining, self.backup_limit)

            if self.include_backups and remaining > 0:
                added = 0
                for url in self._sample_proxies_txt(remaining, seen_keys):
                    if added >= remaining or len(pool) >= self.max_pool_size:
                        break
                    if self._add_candidate(pool, seen_keys, url, verified=False):
                        added += 1

            self.proxy_pool = pool
            self.index = 0

    def _active_timestamps_for_key(self, key: str, now: float) -> list[float]:
        """Prunes and returns active window timestamps for a canonical IP:port key."""
        active_ts = self._clean_old_timestamps(self.rate_records.get(key, []), now=now)
        if active_ts:
            self.rate_records[key] = active_ts
        else:
            self.rate_records.pop(key, None)
        return active_ts

    def get_proxy(self, allow_rate_limited: bool = False) -> Optional[str]:
        """
        Returns the next proxy URL under the per-IP:port sliding-window limit.
        Returns None if proxies are disabled, pool is empty/exhausted, or all
        proxies are currently rate-limited (unless allow_rate_limited=True).
        """
        if not self.use_proxies:
            return None

        with self.lock:
            pool_size = len(self.proxy_pool)
            if pool_size == 0:
                return None

            now = time.time()
            for _ in range(pool_size):
                proxy = self.proxy_pool[self.index % pool_size]
                self.index = (self.index + 1) % pool_size

                key = extract_ip_port_key(proxy)
                active_ts = self._active_timestamps_for_key(key, now=now)
                if len(active_ts) < self.max_signups_per_hour:
                    return proxy

            if not allow_rate_limited:
                return None

            # Opt-in fallback: pick the proxy whose quota slot expires soonest
            best_proxy = None
            min_wait = float("inf")
            for proxy in self.proxy_pool:
                key = extract_ip_port_key(proxy)
                active_ts = self._active_timestamps_for_key(key, now=now)
                if len(active_ts) < self.max_signups_per_hour:
                    return proxy
                expiring_ts = sorted(active_ts)[-self.max_signups_per_hour]
                wait = max(0.0, self.window_seconds - (now - expiring_ts))
                if wait < min_wait:
                    min_wait = wait
                    best_proxy = proxy
            return best_proxy

    def get_next_available_wait_seconds(self) -> float:
        """
        Returns seconds until at least one proxy in the pool has available quota.
        Returns 0.0 if a proxy is available now, proxies are disabled, or pool is empty.
        """
        if not self.use_proxies:
            return 0.0

        with self.lock:
            if not self.proxy_pool:
                return 0.0

            now = time.time()
            min_wait = float("inf")
            for proxy in self.proxy_pool:
                key = extract_ip_port_key(proxy)
                active_ts = self._active_timestamps_for_key(key, now=now)
                if len(active_ts) < self.max_signups_per_hour:
                    return 0.0
                expiring_ts = sorted(active_ts)[-self.max_signups_per_hour]
                wait = max(0.0, self.window_seconds - (now - expiring_ts))
                if wait < min_wait:
                    min_wait = wait
            return 0.0 if min_wait == float("inf") else min_wait

    def get_cooldown_seconds(self) -> float:
        """Alias for get_next_available_wait_seconds()."""
        return self.get_next_available_wait_seconds()

    def record_signup(self, proxy_url: Optional[str]) -> None:
        """Records a signup timestamp against the proxy's IP:port quota bucket."""
        if not proxy_url:
            return
        key = extract_ip_port_key(proxy_url)
        if not key:
            return

        with self.lock:
            now = time.time()
            active_ts = self._clean_old_timestamps(self.rate_records.get(key, []), now=now)
            active_ts.append(now)
            active_ts.sort()
            self.rate_records[key] = active_ts
            self._save_state()

    def mark_failed(self, proxy_url: Optional[str], reason: str = "Dead") -> None:
        """Quarantines a dead or blocked proxy and persists state atomically."""
        if not proxy_url:
            return
        key = extract_ip_port_key(proxy_url)
        with self.lock:
            self.quarantined.add(proxy_url)
            if key:
                self._quarantined_keys.add(key)
                self.verified_keys.discard(key)
            self.proxy_pool = [
                p for p in self.proxy_pool
                if p != proxy_url and (not key or extract_ip_port_key(p) != key)
            ]
            self.index = (self.index % len(self.proxy_pool)) if self.proxy_pool else 0
            logger.debug("Quarantined proxy %s (%s)", proxy_url, reason)
            self._save_state()

    def probe_proxy(
        self,
        proxy_url: str,
        timeout: float = 3.0,
        target_url: str = DEFAULT_PROBE_URL,
        tcp_only: bool = False,
    ) -> bool:
        """Probes a single proxy; marks its IP:port verified if reachable."""
        ok = probe_proxy(proxy_url, timeout=timeout, target_url=target_url, tcp_only=tcp_only)
        if ok:
            key = extract_ip_port_key(proxy_url)
            if key:
                with self.lock:
                    self.verified_keys.add(key)
        return ok

    def warm_pool(
        self,
        target_size: int = 15,
        timeout: float = 3.0,
        max_workers: int = 10,
        tcp_only: bool = False,
    ) -> list[str]:
        """
        Concurrently pre-probes candidate proxies until target_size live proxies are
        verified. Failed proxies are quarantined; returns the list of live proxies.
        """
        if not self.use_proxies or target_size <= 0:
            return []

        with self.lock:
            now = time.time()
            candidates = []
            for proxy in self.proxy_pool:
                key = extract_ip_port_key(proxy)
                active_ts = self._active_timestamps_for_key(key, now=now)
                if len(active_ts) < self.max_signups_per_hour:
                    candidates.append(proxy)

        if not candidates:
            return []

        live: list[str] = []
        failed: list[str] = []
        worker_count = max(1, min(int(max_workers), len(candidates)))
        batch_size = max(target_size, worker_count)

        with concurrent.futures.ThreadPoolExecutor(max_workers=worker_count) as executor:
            for offset in range(0, len(candidates), batch_size):
                if len(live) >= target_size:
                    break
                batch = candidates[offset:offset + batch_size]
                future_map = {
                    executor.submit(probe_proxy, proxy, timeout, DEFAULT_PROBE_URL, tcp_only): proxy
                    for proxy in batch
                }
                for future in concurrent.futures.as_completed(future_map):
                    proxy = future_map[future]
                    try:
                        is_live = bool(future.result())
                    except Exception:
                        is_live = False
                    if is_live:
                        live.append(proxy)
                    else:
                        failed.append(proxy)

        with self.lock:
            for proxy in live:
                key = extract_ip_port_key(proxy)
                if key:
                    self.verified_keys.add(key)
            if failed:
                failed_keys = {extract_ip_port_key(p) for p in failed}
                failed_keys.discard("")
                self.quarantined.update(failed)
                self._quarantined_keys.update(failed_keys)
                self.verified_keys.difference_update(failed_keys)
                self.proxy_pool = [
                    p for p in self.proxy_pool
                    if p not in self.quarantined and extract_ip_port_key(p) not in self._quarantined_keys
                ]
                self.index = (self.index % len(self.proxy_pool)) if self.proxy_pool else 0
                self._save_state()

        return live[:target_size]

    def get_stats(self) -> dict[str, int]:
        """Returns pool telemetry: {active, working_verified, quarantined, rate_limited}."""
        with self.lock:
            now = time.time()
            active = 0
            rate_limited = 0
            working_verified = 0

            for proxy in self.proxy_pool:
                key = extract_ip_port_key(proxy)
                active_ts = self._active_timestamps_for_key(key, now=now)
                if len(active_ts) >= self.max_signups_per_hour:
                    rate_limited += 1
                else:
                    active += 1

                if key in self.verified_keys:
                    working_verified += 1

            return {
                "active": active,
                "working_verified": working_verified,
                "quarantined": len(self.quarantined),
                "rate_limited": rate_limited,
            }


def main() -> None:
    parser = argparse.ArgumentParser(description="HBU2 Proxy Rotator & Rate-Limit Manager")
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_MAX_POOL_SIZE,
        help=f"Maximum proxy pool size (default: {DEFAULT_MAX_POOL_SIZE})",
    )
    parser.add_argument(
        "--no-backups",
        action="store_true",
        help="Load only verified proxies from working_proxies.txt",
    )
    parser.add_argument("--warm", type=int, default=0, help="Pre-probe N live proxies before printing stats")
    parser.add_argument("--timeout", type=float, default=3.0, help="Probe timeout in seconds (default: 3.0)")
    args = parser.parse_args()

    rotator = ProxyRotator(
        use_proxies=True,
        max_pool_size=args.limit,
        include_backups=not args.no_backups,
    )
    if args.warm > 0:
        live = rotator.warm_pool(target_size=args.warm, timeout=args.timeout)
        print(f"[*] Warmed pool: {len(live)} live proxies verified.")

    next_proxy = rotator.get_proxy()
    stats = rotator.get_stats()
    wait_s = rotator.get_next_available_wait_seconds()

    scheme_counts: dict[str, int] = {}
    for proxy in rotator.proxy_pool:
        scheme = _get_proxy_scheme(proxy)
        scheme_counts[scheme] = scheme_counts.get(scheme, 0) + 1

    print(f"[*] Rotator initialized with {len(rotator.proxy_pool)} proxies.")
    print(f"[*] Protocol breakdown: {dict(sorted(scheme_counts.items()))}")
    print(f"[*] Next available proxy: {next_proxy} (key: {extract_ip_port_key(next_proxy or '')})")
    print(f"[*] Next available wait: {wait_s:.2f}s")
    print(f"[*] Pool stats: {stats}")


if __name__ == "__main__":
    main()
