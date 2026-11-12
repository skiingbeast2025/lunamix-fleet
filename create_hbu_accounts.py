#!/usr/bin/env python3
"""
create_hbu_accounts.py - Human-Verified Batch Account Creation Engine for HBU2

Integrates protocol client, human-only browser challenge UI, proxy rotator,
credential vault, and welcome wheel spin into a thread-safe orchestrator and CLI
that can also be embedded into a curses TUI via structured event callbacks.
"""

import argparse
import concurrent.futures
import threading
import time
from typing import Any, Callable, Optional

from account_vault import (
    append_account_atomically,
    claim_welcome_spin,
    generate_password,
    generate_username,
    get_existing_usernames,
)
from hbu_auth_client import (
    ProxyDeadException,
    RateLimitException,
    fetch_image_challenge,
    submit_signup,
)
from hbu_challenge_ui import present_challenge_to_human
from proxy_rotator import ProxyRotator

EventCallback = Callable[[dict[str, Any]], None]
ChallengeHandler = Callable[[str], Optional[str]]


class _BatchContext:
    """Thread-safe shared state, locks, and structured event emitter for a batch run."""

    def __init__(
        self,
        count: int,
        student_id: str,
        rotator: ProxyRotator,
        existing_users: set[str],
        stop_event: threading.Event,
        on_event: Optional[EventCallback],
        challenge_handler: ChallengeHandler,
        max_retries: int,
    ) -> None:
        self.count = count
        self.student_id = student_id
        self.rotator = rotator
        self.existing_users = existing_users
        self.stop_event = stop_event
        self.on_event = on_event
        self.challenge_handler = challenge_handler
        self.max_retries = max(1, int(max_retries))

        self.stats_lock = threading.Lock()
        self.users_lock = threading.Lock()
        # Serialize human browser/terminal prompts so concurrent workers never interleave.
        self.prompt_lock = threading.Lock()
        self.emit_lock = threading.Lock()

        self.created_total = 0
        self.failed_total = 0
        self.created_accounts: list[dict[str, Any]] = []

    def emit(self, event: dict[str, Any]) -> None:
        """Deliver a structured event to the callback under a lock."""
        if self.on_event is None:
            return
        with self.emit_lock:
            self.on_event(event)

    def emit_status(self, message: str, worker_id: Optional[int] = None) -> None:
        with self.stats_lock:
            created = self.created_total
            failed = self.failed_total
        self.emit({
            "type": "status",
            "message": message,
            "worker_id": worker_id,
            "created": created,
            "failed": failed,
            "target": self.count,
        })

    def emit_proxy(
        self,
        proxy_url: Optional[str],
        action: str,
        reason: Optional[str] = None,
    ) -> None:
        self.emit({
            "type": "proxy",
            "proxy": proxy_url or "Direct (Local IP)",
            "action": action,
            "reason": reason,
            "stats": self.rotator.get_stats(),
        })

    def reserve_username(self) -> str:
        """Generate a unique username while holding users_lock across threads."""
        with self.users_lock:
            username = generate_username(self.existing_users)
            self.existing_users.add(username.lower())
            return username

    def record_existing_username(self, username: str) -> None:
        with self.users_lock:
            self.existing_users.add(username.lower())

    def prompt_human(self, challenge_b64: str) -> Optional[str]:
        """Run the human challenge prompt serialized across workers."""
        with self.prompt_lock:
            if self.stop_event.is_set():
                return None
            return self.challenge_handler(challenge_b64)

    def record_success(
        self,
        account: dict[str, Any],
        prize: int,
        spin_ok: bool,
        proxy_desc: str,
    ) -> None:
        with self.stats_lock:
            self.created_total += 1
            curr_created = self.created_total
            self.created_accounts.append(account)
        self.emit({
            "type": "created",
            "username": account["username"],
            "password": account["password"],
            "coins": account["coins"],
            "prize": prize,
            "spin_ok": spin_ok,
            "student_id": account["student_id"],
            "proxy": proxy_desc,
            "created": curr_created,
            "target": self.count,
        })

    def record_failure(self, reason: str, worker_id: Optional[int] = None) -> None:
        with self.stats_lock:
            self.failed_total += 1
            curr_failed = self.failed_total
            curr_created = self.created_total
        self.emit({
            "type": "failed",
            "reason": reason,
            "worker_id": worker_id,
            "failed": curr_failed,
            "created": curr_created,
            "target": self.count,
        })


def _select_proxy(worker_id: int, ctx: _BatchContext) -> tuple[bool, Optional[str]]:
    """Select an available proxy or confirm direct mode; returns (ok, proxy_url)."""
    proxy_url = ctx.rotator.get_proxy()
    if ctx.rotator.use_proxies and proxy_url is None:
        if not ctx.rotator.proxy_pool:
            ctx.emit_proxy(None, "exhausted", "No active proxies remaining in pool")
        else:
            wait_s = ctx.rotator.get_next_available_wait_seconds()
            ctx.emit_proxy(
                None,
                "rate_limited",
                f"All proxies rate-limited (next slot in {wait_s:.0f}s)",
            )
        ctx.record_failure("No available proxy", worker_id=worker_id)
        return False, None

    ctx.emit_proxy(proxy_url, "selected" if proxy_url else "direct")
    return True, proxy_url


def _fetch_challenge_fast(
    worker_id: int,
    ctx: _BatchContext,
) -> tuple[str, Optional[str], Optional[dict[str, Any]]]:
    """
    Fetch an image challenge quickly. In proxy mode, races up to 4 proxies in
    parallel with a tight 2.8s timeout so dead proxies never stall the user.
    Returns (outcome, proxy_url, challenge_data) where outcome is 'ok', 'retry', or 'no_route'.
    """
    # For small pools (<= 4) or direct mode, use deterministic sequential selection with fast 3.0s timeout.
    if not ctx.rotator.use_proxies or len(ctx.rotator.proxy_pool) <= 4:
        has_route, proxy_url = _select_proxy(worker_id, ctx)
        if not has_route:
            return "no_route", None, None
        try:
            ch_data = fetch_image_challenge(proxy_url=proxy_url, timeout=3.0)
            return "ok", proxy_url, ch_data
        except ProxyDeadException as exc:
            ctx.rotator.mark_failed(proxy_url, reason=f"Fetch failed: {exc}")
            ctx.emit_proxy(proxy_url, "failed", str(exc))
            return "retry", proxy_url, None
        except RateLimitException as exc:
            ctx.rotator.record_signup(proxy_url)
            ctx.emit_proxy(proxy_url, "rate_limited", str(exc))
            return "retry", proxy_url, None
        except Exception as exc:
            ctx.emit_status(
                f"[W{worker_id}] Challenge fetch error via {proxy_url or 'Direct (Local IP)'}: {exc}",
                worker_id=worker_id,
            )
            if proxy_url:
                ctx.rotator.mark_failed(proxy_url, reason=f"Fetch error: {str(exc)[:60]}")
                ctx.emit_proxy(proxy_url, "failed", str(exc))
            return "retry", proxy_url, None

    # Gather up to 4 distinct candidate proxies to race concurrently
    candidates: list[str] = []
    seen: set[str] = set()
    race_width = min(4, max(1, len(ctx.rotator.proxy_pool)))
    for _ in range(race_width):
        p = ctx.rotator.get_proxy()
        if p and p not in seen:
            seen.add(p)
            candidates.append(p)

    if not candidates:
        _select_proxy(worker_id, ctx)
        return "no_route", None, None

    executor = concurrent.futures.ThreadPoolExecutor(max_workers=len(candidates))
    try:
        future_map = {
            executor.submit(fetch_image_challenge, proxy_url=p, timeout=2.8): p
            for p in candidates
        }
        for future in concurrent.futures.as_completed(future_map):
            p = future_map[future]
            try:
                ch_data = future.result()
                ctx.emit_proxy(p, "selected")
                return "ok", p, ch_data
            except ProxyDeadException as exc:
                ctx.rotator.mark_failed(p, reason=f"Fetch failed: {exc}")
                ctx.emit_proxy(p, "failed", str(exc))
            except RateLimitException as exc:
                ctx.rotator.record_signup(p)
                ctx.emit_proxy(p, "rate_limited", str(exc))
            except Exception as exc:
                ctx.rotator.mark_failed(p, reason=f"Fetch error: {str(exc)[:60]}")
                ctx.emit_proxy(p, "failed", str(exc))
    finally:
        executor.shutdown(wait=False, cancel_futures=True)

    return "retry", None, None


def _create_one_account(worker_id: int, ctx: _BatchContext) -> Optional[dict[str, Any]]:
    """Attempt to create one human-verified account with fast proxy failover."""
    for _attempt in range(ctx.max_retries):
        if ctx.stop_event.is_set():
            return None

        with ctx.stats_lock:
            if ctx.created_total >= ctx.count:
                return None

        outcome, proxy_url, ch_data = _fetch_challenge_fast(worker_id, ctx)
        if outcome == "no_route":
            return None
        if outcome != "ok" or ch_data is None:
            continue

        proxy_desc = proxy_url or "Direct (Local IP)"
        challenge_id = ch_data["id"]
        challenge_b64 = ch_data["image"]

        if ctx.stop_event.is_set():
            return None

        try:
            answer = ctx.prompt_human(challenge_b64)
        except ValueError as exc:
            ctx.emit_status(f"[W{worker_id}] Invalid challenge image: {exc}", worker_id=worker_id)
            continue

        if not answer:
            ctx.record_failure("Challenge skipped or invalid human input", worker_id=worker_id)
            return None

        username = ctx.reserve_username()
        password = generate_password()

        try:
            res = submit_signup(
                username=username,
                password=password,
                challenge_id=challenge_id,
                challenge_answer=answer,
                student_id=ctx.student_id,
                proxy_url=proxy_url,
                timeout=6.0,
            )
        except ProxyDeadException as exc:
            ctx.rotator.mark_failed(proxy_url, reason=f"Submit failed: {exc}")
            ctx.emit_proxy(proxy_url, "failed", str(exc))
            continue
        except Exception as exc:
            ctx.emit_status(
                f"[W{worker_id}] Registration transport error via {proxy_desc}: {exc}",
                worker_id=worker_id,
            )
            continue

        if res.get("ok"):
            ctx.rotator.record_signup(proxy_url)
            ctx.record_existing_username(username)

            spin_ok, prize, coins = claim_welcome_spin(username, password, proxy_url=proxy_url)
            starting_coins = coins if spin_ok else 100

            append_account_atomically(
                username=username,
                password=password,
                coins=starting_coins,
                student_id=ctx.student_id,
            )

            account = {
                "username": username,
                "password": password,
                "coins": starting_coins,
                "student_id": ctx.student_id,
            }
            ctx.record_success(account, prize=prize, spin_ok=spin_ok, proxy_desc=proxy_desc)
            return account

        status = res.get("status")
        err_msg = res.get("error") or f"HTTP {status}"
        if status == 429:
            ctx.rotator.record_signup(proxy_url)
            ctx.emit_proxy(proxy_url, "rate_limited", str(err_msg))
        elif "already exists" in str(err_msg).lower():
            ctx.record_existing_username(username)
            ctx.emit_status(
                f"[W{worker_id}] Username '{username}' already exists; retrying.",
                worker_id=worker_id,
            )
        else:
            ctx.emit_status(
                f"[W{worker_id}] Registration error ({status}): {err_msg}",
                worker_id=worker_id,
            )

    ctx.record_failure(f"Exhausted {ctx.max_retries} attempts", worker_id=worker_id)
    return None


def run_batch_creation(
    count: int = 5,
    concurrency: int = 1,
    use_proxies: bool = True,
    on_event: Optional[EventCallback] = None,
    stop_event: Optional[threading.Event] = None,
    *,
    student_id: str = "FLEET",
    challenge_handler: Optional[ChallengeHandler] = None,
    input_fn: Callable[[str], str] = input,
    rotator: Optional[ProxyRotator] = None,
    max_retries: int = 5,
) -> dict[str, Any]:
    """
    Run a batch of human-verified HBU2 account creations and emit structured events.

    Events delivered to on_event(event_dict) have a 'type' key in:
    'status', 'created', 'failed', 'proxy', 'done'.
    """
    target_count = max(0, int(count))
    workers = max(1, int(concurrency))
    stop = stop_event if stop_event is not None else threading.Event()
    active_rotator = rotator if rotator is not None else ProxyRotator(use_proxies=use_proxies)
    existing_users = get_existing_usernames()
    handler = challenge_handler or (lambda b64: present_challenge_to_human(b64, input_fn=input_fn))

    ctx = _BatchContext(
        count=target_count,
        student_id=student_id.strip() if isinstance(student_id, str) and student_id.strip() else "FLEET",
        rotator=active_rotator,
        existing_users=existing_users,
        stop_event=stop,
        on_event=on_event,
        challenge_handler=handler,
        max_retries=max_retries,
    )

    start_time = time.time()
    mode_label = "Proxy Pool Rotation" if active_rotator.use_proxies else "Direct Local IP"
    ctx.emit_status(
        f"Starting batch: target={target_count}, threads={workers}, "
        f"network={mode_label}, existing={len(existing_users)}"
    )

    if target_count > 0 and not stop.is_set():
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [
                executor.submit(_create_one_account, (idx % workers) + 1, ctx)
                for idx in range(target_count)
            ]
            try:
                for future in concurrent.futures.as_completed(futures):
                    future.result()
            except KeyboardInterrupt:
                stop.set()
                ctx.emit_status("Interrupted by user; stopping remaining workers...")
                for future in futures:
                    future.cancel()

    elapsed = time.time() - start_time
    summary = {
        "created": ctx.created_total,
        "failed": ctx.failed_total,
        "target": target_count,
        "elapsed": elapsed,
        "accounts": list(ctx.created_accounts),
        "stopped": stop.is_set(),
    }
    ctx.emit({
        "type": "done",
        "created": ctx.created_total,
        "failed": ctx.failed_total,
        "target": target_count,
        "elapsed": elapsed,
        "stopped": stop.is_set(),
    })
    return summary


def _build_cli_event_printer(print_lock: threading.Lock) -> EventCallback:
    """Create a thread-safe CLI event printer for structured batch events."""

    def _on_event(event: dict[str, Any]) -> None:
        etype = event.get("type")
        with print_lock:
            if etype == "created":
                spin_msg = (
                    f"+{event['prize']} coins (Total: {event['coins']})"
                    if event.get("spin_ok")
                    else f"{event['coins']} base coins"
                )
                print(
                    f"  ✓ [{event['created']}/{event['target']}] "
                    f"Created: {event['username']:<18} | {spin_msg} via {event['proxy']}"
                )
            elif etype == "failed":
                wid = event.get("worker_id")
                prefix = f"[W{wid}] " if wid is not None else ""
                print(f"  ✗ {prefix}Attempt failed: {event.get('reason')}")
            elif etype == "proxy":
                action = event.get("action")
                if action in ("rate_limited", "failed", "exhausted"):
                    print(
                        f"  [Proxy:{action}] {event.get('proxy')} - {event.get('reason')}"
                    )
            elif etype == "status":
                print(f"  [*] {event.get('message')}")

    return _on_event


def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="HBU2 Human-Verified Batch Account Creator"
    )
    parser.add_argument(
        "--count",
        "-n",
        type=int,
        default=5,
        help="Number of accounts to create (default: 5)",
    )
    parser.add_argument(
        "--threads",
        "-c",
        type=int,
        default=1,
        help="Concurrent worker threads; human prompts are serialized (default: 1)",
    )
    parser.add_argument(
        "--direct",
        action="store_true",
        help="Force direct connection without proxies (subject to 3/hr limit)",
    )
    parser.add_argument(
        "--student-id",
        type=str,
        default="FLEET",
        help="Student ID tag attached to created accounts (default: FLEET)",
    )
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> None:
    args = _parse_args(argv)
    target_count = max(0, args.count)
    threads = max(1, args.threads)
    use_proxies = not args.direct

    rotator = ProxyRotator(use_proxies=use_proxies)
    existing_users = get_existing_usernames()

    print("=" * 72)
    print("        HBU2 FLEET HUMAN-VERIFIED BATCH ACCOUNT CREATOR        ")
    print("=" * 72)
    print(f"[*] Target Fleet Size : {target_count} accounts")
    print(f"[*] Concurrency       : {threads} worker thread(s) (prompts serialized)")
    print(f"[*] Network Mode      : {'Proxy Pool Rotation' if use_proxies else 'Direct Local IP'}")
    if use_proxies:
        print(f"[*] Loaded Proxies    : {len(rotator.proxy_pool)}")
    print(f"[*] Student ID Tag    : {args.student_id}")
    print(f"[*] Existing Fleet    : {len(existing_users)} accounts")
    print("=" * 72)

    stop_event = threading.Event()
    print_lock = threading.Lock()
    cli_printer = _build_cli_event_printer(print_lock)

    try:
        summary = run_batch_creation(
            count=target_count,
            concurrency=threads,
            use_proxies=use_proxies,
            on_event=cli_printer,
            stop_event=stop_event,
            student_id=args.student_id,
            rotator=rotator,
        )
    except KeyboardInterrupt:
        stop_event.set()
        print("\n[!] Process interrupted by user.")
        return

    elapsed = summary["elapsed"]
    created = summary["created"]
    print("\n" + "=" * 72)
    print("                       BATCH SUMMARY                       ")
    print("=" * 72)
    print(f"  • Successfully Created : {created} / {target_count}")
    print(f"  • Failed Attempts      : {summary['failed']}")
    print(f"  • Total Time Elapsed   : {elapsed:.2f}s")
    if created > 0 and elapsed > 0:
        print(f"  • Average Creation Rate: {(created / elapsed * 60):.1f} accounts/min")
    print("  • Updated Fleet File   : simulated_accounts.csv")
    print("=" * 72)


if __name__ == "__main__":
    main()
