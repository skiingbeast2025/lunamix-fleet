#!/usr/bin/env python3
"""
test_proxies.py - Single-file Concurrent Proxy Validator & Classifier

Reads proxies.json, tests each proxy concurrently against an HTTPS endpoint,
measures response latency, classifies each proxy as "working" or "dead", and
atomically updates proxies.json with the results and metadata.

Features:
  - Concurrent testing with configurable thread pool (default: 50 threads).
  - Native support for SOCKS5 (with remote DNS resolution), SOCKS4, HTTP, and HTTPS.
  - Periodic atomic checkpointing (saves every N tested proxies; work is never lost).
  - Graceful interrupt handling (Ctrl+C cleanly flushes current progress).
  - Generates working_proxies.txt alongside proxies.json for immediate use.
  - Optional '--supabase' mode to validate against the target Supabase instance.
"""

import argparse
import concurrent.futures
from datetime import datetime, timezone
import json
import os
import signal
import sys
import tempfile
import threading
import time
import requests

# Supabase constants if --supabase flag is enabled
DEFAULT_TARGET_URL = "https://1.1.1.1/cdn-cgi/trace"
SUPABASE_URL = "https://xvkjvuxhawlljakfkllu.supabase.co/rest/v1/"
SUPABASE_ANON_KEY = "sb_publishable_uyxd5lS77wdXpD56QF_lgg_zaujLjZZ"
SUPABASE_HEADERS = {
    "apikey": SUPABASE_ANON_KEY,
    "Authorization": f"Bearer {SUPABASE_ANON_KEY}",
    "Origin": "https://rockystudios.github.io",
    "Referer": "https://rockystudios.github.io/HBU2/",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}
DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

# Global state for signal handling & periodic saves (RLock prevents re-entrant checkpoint deadlock)
state_lock = threading.RLock()
is_interrupted = False
tested_in_session = 0
working_in_session = 0
dead_in_session = 0


def request_shutdown(signum=None, frame=None):
    """Signal-safe shutdown helper; sets interrupt flag without acquiring locks in signal frame."""
    global is_interrupted
    is_interrupted = True


def install_signal_handlers(handler=request_shutdown):
    """Registers SIGINT/SIGTERM handlers when executing on the main thread."""
    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGINT, handler)
        signal.signal(signal.SIGTERM, handler)


def load_proxies_json(json_path: str) -> dict:
    """Safely loads and validates proxies.json structure, raising ValueError on malformed files."""
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError(f"Failed to read valid JSON from {json_path}: {exc}") from exc

    if not isinstance(data, dict):
        raise ValueError(f"Invalid root JSON type in {json_path}: expected object, got {type(data).__name__}")
    proxies_list = data.get("proxies")
    if not isinstance(proxies_list, list):
        raise ValueError(f"Invalid or missing 'proxies' list in {json_path}")
    return data


def format_proxy_url(proto: str, ip: str, port: int) -> dict:
    """Builds appropriate requests proxies dictionary for the given protocol."""
    proto = (proto or "http").lower()
    if proto == "socks5":
        # socks5h:// forces remote DNS resolution through the proxy
        p_url = f"socks5h://{ip}:{port}"
        return {"http": p_url, "https": p_url}
    elif proto == "socks4":
        p_url = f"socks4://{ip}:{port}"
        return {"http": p_url, "https": p_url}
    elif proto == "https":
        p_url = f"https://{ip}:{port}"
        return {"http": f"http://{ip}:{port}", "https": p_url}
    else:
        p_url = f"http://{ip}:{port}"
        return {"http": p_url, "https": p_url}


def test_single_proxy(proxy_entry: dict, target_url: str, headers: dict, timeout: float) -> dict:
    """Probes a single proxy and returns the outcome dict."""
    ip = proxy_entry.get("ip")
    port = proxy_entry.get("port")
    proto = proxy_entry.get("primary_protocol") or "http"
    proxies_dict = format_proxy_url(proto, ip, port)

    t0 = time.perf_counter()
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    try:
        with requests.Session() as session:
            session.trust_env = False  # Avoid inheriting system proxy env vars
            resp = session.get(
                target_url,
                headers=headers,
                proxies=proxies_dict,
                timeout=timeout,
                allow_redirects=False,
                verify=True
            )
        latency = (time.perf_counter() - t0) * 1000.0

        if resp.status_code in (200, 204, 301, 302, 307):
            return {
                "status": "working",
                "latency_ms": round(latency, 1),
                "last_checked": now_iso,
                "error": None
            }
        else:
            return {
                "status": "dead",
                "latency_ms": None,
                "last_checked": now_iso,
                "error": f"HTTP_{resp.status_code}"
            }
    except requests.exceptions.Timeout:
        return {
            "status": "dead",
            "latency_ms": None,
            "last_checked": now_iso,
            "error": "Timeout"
        }
    except requests.exceptions.SSLError:
        return {
            "status": "dead",
            "latency_ms": None,
            "last_checked": now_iso,
            "error": "SSLHandshakeError"
        }
    except requests.exceptions.ProxyError as e:
        err_msg = str(e)
        if "SOCKSHTTPError" in err_msg or "0x5b" in err_msg:
            code = "SOCKS_Rejected"
        elif "Cannot connect to proxy" in err_msg:
            code = "ProxyConnectFailed"
        else:
            code = "ProxyError"
        return {
            "status": "dead",
            "latency_ms": None,
            "last_checked": now_iso,
            "error": code
        }
    except requests.exceptions.ConnectionError:
        return {
            "status": "dead",
            "latency_ms": None,
            "last_checked": now_iso,
            "error": "ConnectionRefused"
        }
    except Exception as exc:
        return {
            "status": "dead",
            "latency_ms": None,
            "last_checked": now_iso,
            "error": exc.__class__.__name__
        }


def _atomic_write_file(target_path: str, write_cb) -> None:
    """Writes content via same-directory temp file, fsyncs, and atomically replaces target_path."""
    parent_dir = os.path.dirname(os.path.abspath(target_path))
    os.makedirs(parent_dir, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=f".{os.path.basename(target_path)}.",
        suffix=".tmp",
        dir=parent_dir,
    )
    file_obj = None
    try:
        file_obj = os.fdopen(fd, "w", encoding="utf-8")
        with file_obj as f:
            write_cb(f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, target_path)
    except Exception:
        if file_obj is None:
            try:
                os.close(fd)
            except OSError:
                pass
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        raise


def save_checkpoint(json_path: str, data: dict, output_txt: str = None):
    """Atomically writes updated JSON and optionally extracts working proxies to TXT."""
    with state_lock:
        proxies_list = data.get("proxies", [])
        if not isinstance(proxies_list, list):
            proxies_list = []

        # Recalculate metrics
        working_count = 0
        dead_count = 0
        untested_count = 0
        working_by_proto = {"http": 0, "https": 0, "socks4": 0, "socks5": 0}
        working_urls = []

        for p in proxies_list:
            if not isinstance(p, dict):
                continue
            status = p.get("status")
            proto = p.get("primary_protocol", "http")
            if status == "working":
                working_count += 1
                working_by_proto[proto] = working_by_proto.get(proto, 0) + 1
                if p.get("url"):
                    working_urls.append(p["url"])
                elif p.get("ip") and p.get("port"):
                    working_urls.append(f"{proto}://{p['ip']}:{p['port']}")
            elif status == "dead":
                dead_count += 1
            else:
                untested_count += 1

        if not isinstance(data.get("metadata"), dict):
            data["metadata"] = {}

        data["metadata"]["last_tested_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        data["metadata"]["tested_summary"] = {
            "total": len(proxies_list),
            "working": working_count,
            "dead": dead_count,
            "untested": untested_count,
            "working_by_protocol": working_by_proto
        }

        # Serialize snapshot while locked to avoid concurrent dict mutation during dump
        serialized_json = json.dumps(data, indent=2)

    # Atomic JSON save via temporary file
    try:
        _atomic_write_file(json_path, lambda f: f.write(serialized_json))
    except Exception as e:
        print(f"\n[!] Warning: Failed to save checkpoint to {json_path}: {e}")

    # Save working proxies text list if specified
    if output_txt and working_urls:
        try:
            _atomic_write_file(output_txt, lambda f: f.writelines(f"{u}\n" for u in working_urls))
        except Exception as e:
            print(f"\n[!] Warning: Failed to write working proxies to {output_txt}: {e}")


def main():
    global is_interrupted, tested_in_session, working_in_session, dead_in_session
    is_interrupted = False
    tested_in_session = 0
    working_in_session = 0
    dead_in_session = 0

    parser = argparse.ArgumentParser(
        description="Lunamix Concurrent Proxy Validator: Test & Classify Proxies in proxies.json"
    )
    parser.add_argument("--file", "-f", default="proxies.json", help="Path to proxies.json (default: proxies.json)")
    parser.add_argument("--limit", "-l", type=int, default=None, help="Maximum number of proxies to test (default: all)")
    parser.add_argument("--threads", "-c", type=int, default=50, help="Number of concurrent worker threads (default: 50)")
    parser.add_argument("--timeout", "-t", type=float, default=3.5, help="Socket timeout per check in seconds (default: 3.5)")
    parser.add_argument("--target", default=None, help=f"Custom target URL to test against (default: {DEFAULT_TARGET_URL})")
    parser.add_argument("--supabase", action="store_true", help="Test specifically against HBU2 Supabase REST API")
    parser.add_argument("--protocol", "-p", choices=["http", "https", "socks4", "socks5"], default=None, help="Filter: test only proxies matching this protocol")
    parser.add_argument("--retest", action="store_true", help="Retest already classified proxies (default: test untested only)")
    parser.add_argument("--checkpoint", type=int, default=200, help="Save progress to proxies.json every N proxies (default: 200)")
    parser.add_argument("--output-txt", default="working_proxies.txt", help="Output text file for working proxies (default: working_proxies.txt)")

    args = parser.parse_args()

    json_path = os.path.abspath(args.file)
    if not os.path.exists(json_path):
        print(f"[-] Error: File not found: {json_path}")
        sys.exit(1)

    print("=" * 72)
    print("        LUNAMIX PROXY VALIDATOR & CLASSIFIER        ")
    print("=" * 72)
    print(f"[*] Ingesting: {json_path}...")

    t_load = time.time()
    try:
        data = load_proxies_json(json_path)
    except ValueError as e:
        print(f"[-] Error parsing JSON file: {e}")
        sys.exit(1)
    print(f"[*] Loaded JSON in {time.time() - t_load:.2f}s.")

    proxies_list = data.get("proxies", [])
    if not proxies_list:
        print("[-] No proxies array found in JSON.")
        sys.exit(1)

    # Configure target endpoint
    if args.supabase:
        target_url = SUPABASE_URL
        target_headers = SUPABASE_HEADERS
        target_desc = f"HBU2 Supabase Endpoint ({SUPABASE_URL})"
    elif args.target:
        target_url = args.target
        target_headers = DEFAULT_HEADERS
        target_desc = f"Custom URL ({target_url})"
    else:
        target_url = DEFAULT_TARGET_URL
        target_headers = DEFAULT_HEADERS
        target_desc = f"Cloudflare Trace ({DEFAULT_TARGET_URL})"

    # Select candidates to test
    candidates = []
    for idx, p in enumerate(proxies_list):
        if not isinstance(p, dict):
            continue
        if args.protocol and p.get("primary_protocol") != args.protocol:
            continue
        if not args.retest and p.get("status") in ("working", "dead"):
            continue
        candidates.append((idx, p))

    if args.limit is not None and args.limit >= 0 and len(candidates) > args.limit:
        candidates = candidates[:args.limit]

    total_to_test = len(candidates)
    if total_to_test == 0:
        print("✓ All selected proxies have already been tested! Use --retest to force retesting.")
        sys.exit(0)

    print(f"[*] Target       : {target_desc}")
    print(f"[*] Concurrency  : {args.threads} worker threads")
    print(f"[*] Timeout      : {args.timeout}s per test")
    print(f"[*] Queue Size   : {total_to_test:,} proxies queued for testing")
    print(f"[*] Checkpoint   : Saving every {args.checkpoint} checks & on Ctrl+C")
    print("=" * 72)

    install_signal_handlers()

    start_time = time.time()
    last_checkpoint_count = 0
    checkpoint_interval = max(1, int(args.checkpoint))

    def worker_task(item):
        if is_interrupted:
            return None
        idx, p = item
        res = test_single_proxy(p, target_url, target_headers, args.timeout)
        return idx, p, res

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, int(args.threads))) as executor:
            future_to_item = {executor.submit(worker_task, item): item for item in candidates}

            for future in concurrent.futures.as_completed(future_to_item):
                if is_interrupted:
                    for pending in future_to_item:
                        pending.cancel()
                    break

                res_tuple = future.result()
                if not res_tuple:
                    continue

                idx, proxy_obj, test_result = res_tuple

                should_checkpoint = False
                with state_lock:
                    # Update proxy in-place
                    proxy_obj["status"] = test_result["status"]
                    proxy_obj["latency_ms"] = test_result["latency_ms"]
                    proxy_obj["last_checked"] = test_result["last_checked"]
                    proxy_obj["error"] = test_result["error"]

                    tested_in_session += 1
                    if test_result["status"] == "working":
                        working_in_session += 1
                        proto_tag = proxy_obj.get("primary_protocol", "http").upper()
                        print(f"  ✓ [{proto_tag:6}] {proxy_obj.get('ip', ''):<15}:{proxy_obj.get('port', ''):<5} "
                              f"({test_result['latency_ms']}ms)")
                    else:
                        dead_in_session += 1

                    # Live progress summary
                    pct = (tested_in_session / total_to_test) * 100
                    elapsed = time.time() - start_time
                    rate = tested_in_session / elapsed if elapsed > 0 else 0
                    success_rate = (working_in_session / tested_in_session * 100) if tested_in_session else 0

                    if tested_in_session % 25 == 0 or tested_in_session == total_to_test:
                        sys.stdout.write(
                            f"\r[*] Progress: {tested_in_session}/{total_to_test} ({pct:.1f}%) | "
                            f"Live Working: {working_in_session} ({success_rate:.1f}%) | "
                            f"Speed: {rate:.1f} checks/s   "
                        )
                        sys.stdout.flush()

                    if tested_in_session - last_checkpoint_count >= checkpoint_interval:
                        last_checkpoint_count = tested_in_session
                        should_checkpoint = True

                # Run checkpoint outside state_lock so lock ordering stays clean
                if should_checkpoint:
                    save_checkpoint(json_path, data, args.output_txt)

    except KeyboardInterrupt:
        request_shutdown()

    # Final save
    print("\n\n[*] Test run finished. Performing final atomic save...")
    save_checkpoint(json_path, data, args.output_txt)

    total_time = time.time() - start_time
    rate_str = f"{tested_in_session / total_time:.1f}" if total_time > 0 else "0.0"
    print("=" * 72)
    print("                    VALIDATION SUMMARY                    ")
    print("=" * 72)
    print(f"  • Tested in this session : {tested_in_session:,}")
    print(f"  • Confirmed Working      : {working_in_session:,}")
    print(f"  • Dead / Unresponsive    : {dead_in_session:,}")
    if tested_in_session:
        print(f"  • Success Rate           : {(working_in_session / tested_in_session * 100):.2f}%")
        print(f"  • Total Duration         : {total_time:.1f}s ({rate_str} checks/s)")
    print(f"  • Updated JSON File      : {json_path}")
    if os.path.exists(args.output_txt):
        print(f"  • Working List Output    : {os.path.abspath(args.output_txt)}")
    print("=" * 72)


if __name__ == "__main__":
    main()
