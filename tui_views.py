#!/usr/bin/env python3
"""
tui_views.py — 4-tab curses Dashboard & Control Views for Lunamix Fleet.

Built on top of tui_engine.py (TuiApp, TuiTab, StatePoller, InlineModal,
safe_addstr, draw_box, draw_progress_bar, draw_table).

Tabs:
  1. Overview (Control Center)
  2. Fleet    (Live Bot Fleet Table)
  3. Creator  (Human-Verified Account Creator)
  4. Logs     (Rolling Simulator Logs)
"""

from __future__ import annotations

import curses
from datetime import datetime
import os
import subprocess
import sys
import threading
import time
from typing import Any, Dict, List, Optional, Tuple
import webbrowser

from tui_engine import (
    InlineModal,
    StatePoller,
    TuiApp,
    TuiTab,
    _compute_col_widths,
    _format_cell,
    _safe_float,
    _safe_int,
    draw_box,
    draw_progress_bar,
    draw_table,
    safe_addstr,
)


# ---------------------------------------------------------------------------
# Shared Stateful Controllers for Tabs
# ---------------------------------------------------------------------------

class _OverviewController:
    """Manages non-blocking background actions and cached systemd autostart state."""

    def __init__(self, service_module: Any) -> None:
        self.svc = service_module
        self._lock = threading.Lock()
        self._busy_action: Optional[str] = None
        self._autostart_enabled: bool = self._safe_check_autostart()
        self._last_autostart_check: float = time.monotonic()

    def _safe_check_autostart(self) -> bool:
        try:
            return bool(self.svc.is_systemd_enabled())
        except Exception:
            return False

    def get_autostart(self) -> bool:
        now = time.monotonic()
        with self._lock:
            if now - self._last_autostart_check > 5.0 and self._busy_action is None:
                self._last_autostart_check = now
                threading.Thread(
                    target=self._refresh_autostart_worker,
                    name="AutostartCheck",
                    daemon=True,
                ).start()
            return self._autostart_enabled

    def _refresh_autostart_worker(self) -> None:
        val = self._safe_check_autostart()
        with self._lock:
            self._autostart_enabled = val

    def set_autostart_cached(self, val: bool) -> None:
        with self._lock:
            self._autostart_enabled = bool(val)
            self._last_autostart_check = time.monotonic()

    def is_busy(self) -> Optional[str]:
        with self._lock:
            return self._busy_action

    def run_async(
        self,
        app: TuiApp,
        label: str,
        fn: Any,
        on_done_toast: Optional[Any] = None,
    ) -> bool:
        with self._lock:
            if self._busy_action is not None:
                app.show_toast(f"Action '{self._busy_action}' already in progress...", "warn")
                return False
            self._busy_action = label

        app.show_toast(f"{label}...", "info", duration=4.0)

        def _worker() -> None:
            try:
                result = fn()
                if on_done_toast is not None:
                    msg, level = on_done_toast(result)
                elif isinstance(result, tuple) and len(result) == 2:
                    ok, msg = result
                    level = "ok" if ok else "warn"
                else:
                    msg = f"{label} completed."
                    level = "ok"
                app.show_toast(str(msg), level, duration=3.5)
            except Exception as exc:
                app.show_toast(f"{label} failed: {exc}", "err", duration=4.5)
            finally:
                with self._lock:
                    self._busy_action = None
                try:
                    app.poller.poll_once()
                except Exception:
                    pass

        threading.Thread(target=_worker, name=f"TuiAction-{label}", daemon=True).start()
        return True


class _CreatorController:
    """Manages batch account creation state, live event log, and thread-safe modal prompts."""

    def __init__(self, base_dir: str) -> None:
        self.base_dir = base_dir
        self.lock = threading.Lock()
        self.count: int = 5
        self.concurrency: int = 1
        self.use_proxies: bool = True

        self.running: bool = False
        self.created: int = 0
        self.failed: int = 0
        self.target: int = 0
        self.status_text: str = "IDLE"
        self.logs: List[str] = []
        self.stop_event: Optional[threading.Event] = None

        # Thread-safe modal prompt handoff (worker -> main curses thread)
        self._pending_prompt: Optional[Dict[str, Any]] = None
        self._modal_active: bool = False

    def append_log(self, line: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        with self.lock:
            self.logs.append(f"[{ts}] {line}")
            if len(self.logs) > 400:
                self.logs = self.logs[-400:]

    def snapshot(self) -> Dict[str, Any]:
        with self.lock:
            return {
                "count": self.count,
                "concurrency": self.concurrency,
                "use_proxies": self.use_proxies,
                "running": self.running,
                "created": self.created,
                "failed": self.failed,
                "target": self.target,
                "status_text": self.status_text,
                "logs": list(self.logs),
                "has_pending_prompt": self._pending_prompt is not None,
            }

    def tui_input_fn(self, prompt_text: str) -> str:
        """Called from worker thread; blocks until main curses thread shows InlineModal."""
        evt = threading.Event()
        req: Dict[str, Any] = {
            "prompt": prompt_text,
            "event": evt,
            "result": "skip",
        }
        with self.lock:
            self._pending_prompt = req

        stop_ev = self.stop_event
        while not evt.is_set():
            if stop_ev is not None and stop_ev.is_set():
                with self.lock:
                    if self._pending_prompt is req:
                        self._pending_prompt = None
                return "skip"
            evt.wait(0.1)

        return str(req.get("result") or "skip")

    def process_pending_prompt(self, stdscr: Any, app: TuiApp) -> None:
        """Called on the main curses thread during render to display any queued prompt."""
        with self.lock:
            req = self._pending_prompt
            if req is None or self._modal_active:
                return
            self._modal_active = True

        try:
            answer = InlineModal.edit_text(
                stdscr,
                title="Human Verification Challenge",
                prompt=str(req.get("prompt") or "Enter 6-char code (or 'skip'):"),
                initial="",
                colors=app.colors,
            )
            req["result"] = answer.strip() if answer is not None and answer.strip() else "skip"
        finally:
            with self.lock:
                if self._pending_prompt is req:
                    self._pending_prompt = None
                self._modal_active = False
            req["event"].set()

    def start_batch(self, app: TuiApp) -> None:
        with self.lock:
            if self.running:
                app.show_toast("Batch creation is already running.", "warn")
                return
            self.running = True
            self.created = 0
            self.failed = 0
            self.target = self.count
            self.status_text = "RUNNING"
            self.stop_event = threading.Event()
            count = self.count
            concurrency = self.concurrency
            use_proxies = self.use_proxies
            stop_ev = self.stop_event

        self.append_log(
            f"Starting batch: target={count}, mode={'Proxies' if use_proxies else 'Direct'}, threads={concurrency}"
        )
        app.show_toast(f"Started batch creation ({count} accounts)", "info")

        def _on_event(ev: Dict[str, Any]) -> None:
            etype = ev.get("type")
            if etype == "status":
                with self.lock:
                    self.created = int(ev.get("created", self.created))
                    self.failed = int(ev.get("failed", self.failed))
                self.append_log(f"[*] {ev.get('message', '')}")
            elif etype == "proxy":
                action = ev.get("action", "")
                proxy = ev.get("proxy", "Direct")
                reason = ev.get("reason")
                if action in ("rate_limited", "failed", "exhausted"):
                    self.append_log(f"[Proxy:{action}] {proxy} - {reason or ''}")
                elif action in ("selected", "direct"):
                    self.append_log(f"[Proxy:{action}] {proxy}")
            elif etype == "created":
                with self.lock:
                    self.created = int(ev.get("created", self.created + 1))
                user = ev.get("username", "?")
                coins = ev.get("coins", 0)
                proxy = ev.get("proxy", "?")
                self.append_log(
                    f"[+] CREATED {user} | {coins} coins | via {proxy} ({self.created}/{self.target})"
                )
            elif etype == "failed":
                with self.lock:
                    self.failed = int(ev.get("failed", self.failed + 1))
                self.append_log(f"[!] FAILED: {ev.get('reason', 'Unknown error')}")
            elif etype == "done":
                with self.lock:
                    self.created = int(ev.get("created", self.created))
                    self.failed = int(ev.get("failed", self.failed))
                    self.status_text = "STOPPED" if ev.get("stopped") else "DONE"
                elapsed = float(ev.get("elapsed", 0.0))
                self.append_log(
                    f"[=] Batch finished in {elapsed:.1f}s: {self.created} created, {self.failed} failed."
                )

        def _worker() -> None:
            try:
                import create_hbu_accounts

                create_hbu_accounts.run_batch_creation(
                    count=count,
                    concurrency=concurrency,
                    use_proxies=use_proxies,
                    on_event=_on_event,
                    stop_event=stop_ev,
                    input_fn=self.tui_input_fn,
                )
                app.show_toast(
                    f"Batch complete: {self.created} created, {self.failed} failed",
                    "ok" if self.created > 0 else "warn",
                )
            except Exception as exc:
                self.append_log(f"[!] Batch crashed: {exc}")
                with self.lock:
                    self.status_text = "ERROR"
                app.show_toast(f"Batch error: {exc}", "err", duration=4.5)
            finally:
                with self.lock:
                    self.running = False
                try:
                    app.poller.poll_once()
                except Exception:
                    pass

        threading.Thread(target=_worker, name="TuiBatchCreator", daemon=True).start()

    def stop_batch(self, app: TuiApp) -> None:
        with self.lock:
            if not self.running or self.stop_event is None:
                app.show_toast("No batch creation is currently running.", "info")
                return
            self.stop_event.set()
            self.status_text = "STOPPING"
        self.append_log("[*] Stop requested by operator...")
        app.show_toast("Stopping batch creation...", "warn")


class _LogsController:
    """Tracks follow-tail preference for the Rolling Logs tab."""

    def __init__(self) -> None:
        self.follow_tail: bool = True


# ---------------------------------------------------------------------------
# Tab 1: Overview (Control Center)
# ---------------------------------------------------------------------------

def _build_overview_tab(svc: Any, ctrl: _OverviewController) -> TuiTab:
    def render(stdscr: Any, app: TuiApp, state: Dict[str, Any], rect: Tuple[int, int, int, int]) -> None:
        top, left, height, width = rect
        pal = app.colors
        border_attr = pal.get("border", curses.A_DIM)
        title_attr = pal.get("accent", curses.A_BOLD)
        ok_attr = pal.get("ok", curses.A_BOLD)
        warn_attr = pal.get("warn", curses.A_BOLD)
        err_attr = pal.get("err", curses.A_BOLD)
        muted_attr = pal.get("muted", curses.A_DIM)
        def_attr = pal.get("default", 0)

        daemon_up = bool(state.get("daemon_running", False))
        paused = bool(state.get("paused", False))
        pid = state.get("daemon_pid")
        uptime = state.get("uptime_str", "00:00:00")
        sent = _safe_int(state.get("messages_sent", 0), 0)
        failed = _safe_int(state.get("messages_failed", 0), 0)
        rate = _safe_float(state.get("msgs_per_min", 0.0), 0.0)
        active_bots = _safe_int(state.get("active_bots", 0), 0)
        banned = _safe_int(state.get("banned_count", 0), 0)
        latency = _safe_int(state.get("last_latency_ms", 0), 0)

        cfg = state.get("config", {})
        if not isinstance(cfg, dict):
            cfg = {}
        bot_count = cfg.get("bot_count", "all")
        bot_cfg_str = "ALL" if str(bot_count).lower() == "all" else str(bot_count)
        min_delay = _safe_float(cfg.get("min_delay_per_bot", 5.1), 5.1) or 5.1
        max_delay = _safe_float(cfg.get("max_delay_per_bot", min_delay + 0.15), min_delay + 0.15) or (min_delay + 0.15)
        dash_port = cfg.get("web_dashboard_port", 8088)
        csv_file = cfg.get("csv_file", "simulated_accounts.csv")
        csv_count = _safe_int(state.get("account_count", 0), 0)

        working_proxies = _safe_int(state.get("working_proxies_count", 0), 0)
        rate_limited = _safe_int(state.get("proxy_rate_limits_count", 0), 0)
        quarantined = _safe_int(state.get("quarantined_proxies_count", 0), 0)

        autostart = ctrl.get_autostart()
        busy = ctrl.is_busy()

        if not daemon_up:
            status_badge, status_attr = "STOPPED", err_attr
        elif paused:
            status_badge, status_attr = "PAUSED", warn_attr
        else:
            status_badge, status_attr = "RUNNING", ok_attr

        # Clean, minimal two-column layout when wide enough, else single column
        if width >= 84 and height >= 13:
            col_w = width // 2
            lx = left + 2
            rx = left + col_w + 2
            cw = max(12, col_w - 4)

            safe_addstr(stdscr, top + 1, lx, "DAEMON & TELEMETRY"[:cw], title_attr)
            safe_addstr(stdscr, top + 1, rx, "CONFIGURATION & POOLS"[:cw], title_attr)
            safe_addstr(stdscr, top + 2, lx, ("-" * min(cw, 36)), muted_attr)
            safe_addstr(stdscr, top + 2, rx, ("-" * min(cw, 36)), muted_attr)

            safe_addstr(stdscr, top + 3, lx, "Status       : ", muted_attr)
            safe_addstr(stdscr, top + 3, lx + 15, f"{status_badge} (PID: {pid or 'None'})"[: max(1, cw - 15)], status_attr)
            safe_addstr(stdscr, top + 4, lx, f"Uptime       : {uptime}"[:cw], def_attr)
            safe_addstr(stdscr, top + 5, lx, f"Messages     : {sent:,} sent, {failed} failed"[:cw], def_attr)
            safe_addstr(stdscr, top + 6, lx, f"Throughput   : {rate:.1f} msgs/min"[:cw], def_attr)
            safe_addstr(stdscr, top + 7, lx, f"Active Bots  : {active_bots}  (Muted/Banned: {banned})"[:cw], def_attr)
            safe_addstr(stdscr, top + 8, lx, f"Ping Latency : {latency} ms"[:cw], def_attr)
            if busy:
                safe_addstr(stdscr, top + 9, lx, f"Task         : {busy}..."[:cw], warn_attr)

            safe_addstr(stdscr, top + 3, rx, f"Bot Fleet    : {bot_cfg_str} configured"[:cw], def_attr)
            safe_addstr(stdscr, top + 4, rx, f"CSV Accounts : {csv_count:,} ({csv_file})"[:cw], def_attr)
            safe_addstr(stdscr, top + 5, rx, f"Pacing Delay : {min_delay:.2f}s - {max_delay:.2f}s"[:cw], def_attr if min_delay >= 5.1 else warn_attr)
            safe_addstr(stdscr, top + 6, rx, f"Autostart    : {'ENABLED' if autostart else 'DISABLED'}"[:cw], def_attr)
            safe_addstr(stdscr, top + 7, rx, f"Dashboard    : http://localhost:{dash_port}"[:cw], def_attr)
            safe_addstr(
                stdscr,
                top + 8,
                rx,
                f"Proxies      : {working_proxies} active, {rate_limited} rate-ltd, {quarantined} quar."[:cw],
                def_attr,
            )

            ctrl_y = top + 11
            if ctrl_y + 2 < top + height:
                safe_addstr(stdscr, ctrl_y, lx, ("-" * min(width - 4, 74)), muted_attr)
                safe_addstr(stdscr, ctrl_y + 1, lx, "[S] Start   [X] Stop    [P] Pause/Resume   [T] Autostart"[: width - 4], def_attr)
                safe_addstr(stdscr, ctrl_y + 2, lx, "[B] Bots    [D] Delay   [G] GitHub Sync    [W] Web Dashboard"[: width - 4], def_attr)
        else:
            cx = left + 2
            cw = max(10, width - 4)
            lines: List[Tuple[str, int]] = [
                (f"Status    : {status_badge} (PID: {pid or 'None'})  Up: {uptime}", status_attr),
                (f"Sent/Rate : {sent:,} sent ({failed} fail) | {rate:.1f}/min | {latency}ms", def_attr),
                (f"Fleet     : {active_bots} active / {bot_cfg_str} cfg | CSV: {csv_count:,} ({csv_file})", def_attr),
                (f"Delay/Boot: {min_delay:.2f}s-{max_delay:.2f}s | Autostart: {'ON' if autostart else 'OFF'}", def_attr),
                (f"Proxies   : {working_proxies} active | {rate_limited} rate-ltd | {quarantined} quar.", def_attr),
                (f"Dashboard : http://localhost:{dash_port}", def_attr),
                ("", def_attr),
                ("[S] Start  [X] Stop  [P] Pause  [T] Autostart", muted_attr),
                ("[B] Bots   [D] Delay [G] Sync   [W] Web", muted_attr),
            ]
            if busy:
                lines.insert(6, (f"Task      : {busy}...", warn_attr))
            for idx, (txt, attr) in enumerate(lines):
                y = top + 1 + idx
                if y >= top + height:
                    break
                safe_addstr(stdscr, y, cx, txt[:cw], attr)

    def handle_key(ch: int, app: TuiApp, state: Dict[str, Any]) -> bool:
        if ch in (ord("s"), ord("S")):
            ctrl.run_async(app, "Starting daemon", lambda: svc.start_service())
            return True

        if ch in (ord("x"), ord("X")):
            ctrl.run_async(app, "Stopping daemon", lambda: svc.stop_service())
            return True

        if ch in (ord("p"), ord("P")):
            try:
                ok, msg = svc.pause_service()
                app.show_toast(msg, "ok" if ok else "warn")
                app.poller.poll_once()
            except Exception as exc:
                app.show_toast(f"Pause failed: {exc}", "err")
            return True

        if ch in (ord("t"), ord("T")):
            try:
                new_state = svc.toggle_autostart()
                ctrl.set_autostart_cached(new_state)
                app.show_toast(
                    f"Autostart on boot: {'ENABLED' if new_state else 'DISABLED'}",
                    "ok" if new_state else "info",
                )
            except Exception as exc:
                app.show_toast(f"Autostart toggle failed: {exc}", "err")
            return True

        if ch in (ord("b"), ord("B")):
            cfg = svc.load_config()
            cur = str(cfg.get("bot_count", "all"))
            val = InlineModal.edit_text(
                app.stdscr,
                "Configure Bot Count",
                "Enter number of bots or 'all':",
                initial=cur,
                colors=app.colors,
            )
            if val is not None:
                cleaned = val.strip()
                if cleaned.lower() in ("a", "all"):
                    cfg["bot_count"] = "all"
                    svc.save_config(cfg)
                    app.poller.poll_once()
                    app.show_toast("Bot count set to ALL", "ok")
                else:
                    try:
                        n = int(cleaned)
                        if n > 0:
                            cfg["bot_count"] = n
                            svc.save_config(cfg)
                            app.poller.poll_once()
                            app.show_toast(f"Bot count set to {n}", "ok")
                        else:
                            app.show_toast("Bot count must be > 0", "err")
                    except ValueError:
                        app.show_toast(f"Invalid bot count: {cleaned}", "err")
            return True

        if ch in (ord("d"), ord("D")):
            cfg = svc.load_config()
            cur = f"{float(cfg.get('min_delay_per_bot', 5.1)):.2f}"
            val = InlineModal.edit_text(
                app.stdscr,
                "Anti-Mute Pacing Delay",
                "Enter min delay in seconds (>= 5.1):",
                initial=cur,
                colors=app.colors,
            )
            if val is not None:
                try:
                    delay = float(val.strip())
                    if delay >= 5.1:
                        cfg["min_delay_per_bot"] = round(delay, 2)
                        cfg["max_delay_per_bot"] = round(delay + 0.15, 2)
                        svc.save_config(cfg)
                        app.poller.poll_once()
                        app.show_toast(f"Delay updated to {delay:.2f}s", "ok")
                    else:
                        app.show_toast("Delay must be >= 5.1s to avoid auto-mutes!", "err")
                except ValueError:
                    app.show_toast(f"Invalid delay: {val.strip()}", "err")
            return True

        if ch in (ord("w"), ord("W")):
            port = state.get("config", {}).get("web_dashboard_port", 8088)
            url = f"http://localhost:{port}"
            try:
                threading.Thread(target=webbrowser.open, args=(url,), daemon=True).start()
                app.show_toast(f"Opening {url}", "info")
            except Exception as exc:
                app.show_toast(f"Could not open browser: {exc}", "err")
            return True

        if ch in (ord("g"), ord("G")):
            msg = InlineModal.edit_text(
                app.stdscr,
                "GitHub Sync",
                "Commit message (Esc to cancel):",
                initial="feat: update lunamix fleet codebase and tools",
                colors=app.colors,
            )
            if msg is not None:
                commit_msg = msg.strip() or "feat: update lunamix fleet codebase and tools"
                base_dir = getattr(svc, "BASE_DIR", os.path.dirname(os.path.abspath(__file__)))
                sync_script = os.path.join(base_dir, "sync_to_github.py")

                def _do_sync() -> Tuple[bool, str]:
                    res = subprocess.run(
                        [sys.executable, sync_script, commit_msg],
                        cwd=base_dir,
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    if res.returncode == 0:
                        return True, "GitHub sync completed successfully!"
                    err_line = (res.stderr or res.stdout or "Unknown error").strip().splitlines()[-1]
                    return False, f"Sync failed: {err_line[:80]}"

                ctrl.run_async(app, "Syncing to GitHub", _do_sync)
            return True

        return False

    return TuiTab(
        tab_id="overview",
        title="Overview",
        render_fn=render,
        handle_key_fn=handle_key,
        legend="[S]Start [X]Stop [P]Pause [T]Boot [B]Bots [D]Delay [G]Sync [W]Web",
    )


# ---------------------------------------------------------------------------
# Tab 2: Live Bot Fleet
# ---------------------------------------------------------------------------

def _build_fleet_tab() -> TuiTab:
    def render(stdscr: Any, app: TuiApp, state: Dict[str, Any], rect: Tuple[int, int, int, int]) -> None:
        top, left, height, width = rect
        pal = app.colors
        border_attr = pal.get("border", curses.A_DIM)
        title_attr = pal.get("accent", curses.A_BOLD)
        ok_attr = pal.get("ok", curses.A_BOLD)
        warn_attr = pal.get("warn", curses.A_BOLD)
        err_attr = pal.get("err", curses.A_BOLD)
        muted_attr = pal.get("muted", curses.A_DIM)
        def_attr = pal.get("default", 0)

        raw_bots = state.get("bot_states")
        bots: List[Any] = raw_bots if isinstance(raw_bots, list) else []
        active = _safe_int(state.get("active_bots", 0), 0)
        sent = _safe_int(state.get("messages_sent", 0), 0)
        banned = _safe_int(state.get("banned_count", 0), 0)

        inner_x = left + 2
        inner_w = max(10, width - 4)
        summary = f"Fleet ({len(bots)} tracked)  |  Active: {active}  |  Total Sent: {sent:,}  |  Muted/Banned: {banned}"
        safe_addstr(stdscr, top + 1, inner_x, summary[:inner_w], title_attr)

        table_y = top + 3
        table_h = height - 3
        if table_h < 2:
            return

        if not bots:
            safe_addstr(stdscr, table_y + 1, inner_x, "(No active bot telemetry recorded yet)", muted_attr)
            return

        headers = ["Bot Name", "Status", "Sent", "Last Update / Error"]
        col_w = [
            max(12, min(22, inner_w // 4)),
            max(8, min(14, inner_w // 6)),
            max(6, min(8, inner_w // 10)),
            max(12, inner_w - max(12, min(22, inner_w // 4)) - max(8, min(14, inner_w // 6)) - max(6, min(8, inner_w // 10)) - 3),
        ]
        computed_w = _compute_col_widths(inner_w, len(headers), col_w)

        rows: List[List[Any]] = []
        status_list: List[str] = []
        for b in bots:
            if not isinstance(b, dict):
                rows.append(["<corrupt>", "ERROR", 0, str(b) if b is not None else "None"])
                status_list.append("ERROR")
                continue
            b_name = str(b.get("bot") or b.get("username") or "-")
            b_status = str(b.get("status") or "UNKNOWN")
            b_sent = _safe_int(b.get("sent", 0), 0)
            b_detail = str(b.get("error") or b.get("last_seen") or b.get("last_msg") or "-")
            rows.append([b_name, b_status, b_sent, b_detail])
            status_list.append(b_status.upper())

        scroll = app.get_scroll("fleet")
        clamped = draw_table(
            stdscr,
            start_y=table_y,
            start_x=inner_x,
            height=table_h,
            width=inner_w,
            headers=headers,
            rows=rows,
            scroll_offset=scroll,
            col_widths=computed_w,
            header_attr=pal.get("accent", curses.A_BOLD),
            row_attr=def_attr,
        )
        if clamped != scroll:
            app.set_scroll(clamped, "fleet")

        # Overlay status-based color highlighting on the Status cell for each visible row
        visible_rows = max(0, table_h - 1)
        status_x = inner_x + computed_w[0] + 1
        status_w = computed_w[1]
        for i in range(visible_rows):
            idx = clamped + i
            if idx >= len(status_list):
                break
            st = status_list[idx]
            if st in ("OK", "RUNNING", "ACTIVE", "SENT"):
                s_attr = ok_attr
            elif st in ("PAUSED", "WAIT", "RATE_LIMITED", "SLEEPING"):
                s_attr = warn_attr
            elif any(k in st for k in ("ERR", "FAIL", "BAN", "MUTE", "DEAD", "STOP")):
                s_attr = err_attr
            else:
                s_attr = def_attr
            cell_txt = _format_cell(rows[idx][1], status_w)
            safe_addstr(stdscr, table_y + 1 + i, status_x, cell_txt, s_attr)

    return TuiTab(
        tab_id="fleet",
        title="Fleet",
        render_fn=render,
        handle_key_fn=None,
        legend="[↑/↓/PgUp/PgDn/Home] Scroll Fleet",
    )


# ---------------------------------------------------------------------------
# Tab 3: Account Creator
# ---------------------------------------------------------------------------

def _build_creator_tab(ctrl: _CreatorController) -> TuiTab:
    def render(stdscr: Any, app: TuiApp, state: Dict[str, Any], rect: Tuple[int, int, int, int]) -> None:
        # Handle any worker-thread verification prompt safely on the main curses thread
        ctrl.process_pending_prompt(stdscr, app)

        top, left, height, width = rect
        pal = app.colors
        border_attr = pal.get("border", curses.A_DIM)
        title_attr = pal.get("accent", curses.A_BOLD)
        ok_attr = pal.get("ok", curses.A_BOLD)
        warn_attr = pal.get("warn", curses.A_BOLD)
        err_attr = pal.get("err", curses.A_BOLD)
        muted_attr = pal.get("muted", curses.A_DIM)
        def_attr = pal.get("default", 0)

        snap = ctrl.snapshot()
        working_proxies = _safe_int(state.get("working_proxies_count", 0), 0)
        rate_limited = _safe_int(state.get("proxy_rate_limits_count", 0), 0)
        quarantined = _safe_int(state.get("quarantined_proxies_count", 0), 0)
        csv_count = _safe_int(state.get("account_count", 0), 0)

        header_h = 6
        ix = left + 2
        iw = max(10, width - 4)
        net_mode = "Proxy Pool" if snap["use_proxies"] else "Direct Local IP"
        st_label = snap["status_text"]
        st_attr = ok_attr if st_label == "DONE" else (warn_attr if st_label in ("RUNNING", "STOPPING") else (err_attr if st_label == "ERROR" else muted_attr))

        safe_addstr(
            stdscr,
            top + 1,
            ix,
            f"Proxies: {working_proxies} active, {rate_limited} rate-ltd, {quarantined} quar.  |  Fleet CSV: {csv_count:,}"[:iw],
            def_attr,
        )
        label_prefix = f"Target: {snap['count']}  |  Network: {net_mode}  |  State: "
        safe_addstr(stdscr, top + 2, ix, label_prefix[:iw], def_attr)
        if len(label_prefix) < iw:
            safe_addstr(stdscr, top + 2, ix + len(label_prefix), st_label[: iw - len(label_prefix)], st_attr)

        target = max(1, int(snap["target"] or snap["count"] or 1))
        ratio = float(snap["created"]) / float(target) if snap["target"] > 0 else 0.0
        safe_addstr(
            stdscr,
            top + 3,
            ix,
            f"Progress ({snap['created']}/{snap['target']} created, {snap['failed']} failed):"[:iw],
            muted_attr,
        )
        draw_progress_bar(stdscr, top + 4, ix, min(iw, 52), ratio, ok_attr if snap["created"] > 0 else def_attr)
        safe_addstr(stdscr, top + 5, ix, ("-" * min(iw, 74)), muted_attr)

        log_top = top + header_h
        log_h = height - header_h
        if log_h < 2:
            return

        logs = snap["logs"]
        vis_h = max(1, log_h)
        if not logs:
            safe_addstr(stdscr, log_top, ix, "(Press [R] to start account creation batch)"[:iw], muted_attr)
            return

        max_scroll = max(0, len(logs) - vis_h)
        scroll = max_scroll if snap["running"] else min(app.get_scroll("creator"), max_scroll)
        app.set_scroll(scroll, "creator")

        for i in range(vis_h):
            idx = scroll + i
            if idx >= len(logs):
                break
            line = logs[idx]
            l_attr = def_attr
            if "[+] CREATED" in line:
                l_attr = ok_attr
            elif "[!] FAILED" in line or "crashed" in line:
                l_attr = err_attr
            safe_addstr(stdscr, log_top + i, ix, line[:iw], l_attr)

    def handle_key(ch: int, app: TuiApp, state: Dict[str, Any]) -> bool:
        if ch in (ord("n"), ord("N")):
            if ctrl.snapshot()["running"]:
                app.show_toast("Cannot change count while batch is running.", "warn")
                return True
            val = InlineModal.edit_text(
                app.stdscr,
                "Batch Account Count",
                "Enter number of accounts to create:",
                initial=str(ctrl.count),
                colors=app.colors,
            )
            if val is not None:
                try:
                    n = int(val.strip())
                    if n > 0:
                        with ctrl.lock:
                            ctrl.count = n
                        app.show_toast(f"Batch count set to {n}", "ok")
                    else:
                        app.show_toast("Count must be a positive integer", "err")
                except ValueError:
                    app.show_toast(f"Invalid count: {val.strip()}", "err")
            return True

        if ch in (ord("m"), ord("M")):
            if ctrl.snapshot()["running"]:
                app.show_toast("Cannot switch network mode while batch is running.", "warn")
                return True
            with ctrl.lock:
                ctrl.use_proxies = not ctrl.use_proxies
                mode = "Proxy Pool" if ctrl.use_proxies else "Direct Local IP"
            app.show_toast(f"Network mode: {mode}", "info")
            return True

        if ch in (ord("r"), ord("R")):
            ctrl.start_batch(app)
            return True

        if ch in (ord("x"), ord("X")):
            ctrl.stop_batch(app)
            return True

        return False

    return TuiTab(
        tab_id="creator",
        title="Creator",
        render_fn=render,
        handle_key_fn=handle_key,
        legend="[N]Count [M]Mode [R]Run [X]Stop",
    )


# ---------------------------------------------------------------------------
# Tab 4: Rolling Logs
# ---------------------------------------------------------------------------

def _build_logs_tab(ctrl: _LogsController) -> TuiTab:
    def render(stdscr: Any, app: TuiApp, state: Dict[str, Any], rect: Tuple[int, int, int, int]) -> None:
        top, left, height, width = rect
        pal = app.colors
        border_attr = pal.get("border", curses.A_DIM)
        title_attr = pal.get("accent", curses.A_BOLD)
        ok_attr = pal.get("ok", curses.A_BOLD)
        warn_attr = pal.get("warn", curses.A_BOLD)
        err_attr = pal.get("err", curses.A_BOLD)
        muted_attr = pal.get("muted", curses.A_DIM)
        def_attr = pal.get("default", 0)

        lines: List[str] = state.get("log_lines", []) or []
        follow_badge = "ON" if ctrl.follow_tail else "OFF"

        ix = left + 2
        iw = max(10, width - 4)
        safe_addstr(stdscr, top + 1, ix, f"logs/simulator.log ({len(lines)} lines)  |  Follow-Tail: {follow_badge}"[:iw], title_attr)
        safe_addstr(stdscr, top + 2, ix, ("-" * min(iw, 74)), muted_attr)

        vis_h = max(1, height - 3)
        if not lines:
            safe_addstr(stdscr, top + 3, ix, "(No log entries in logs/simulator.log yet)"[:iw], muted_attr)
            return

        max_scroll = max(0, len(lines) - vis_h)
        if ctrl.follow_tail:
            scroll = max_scroll
            app.set_scroll(scroll, "logs")
        else:
            scroll = min(app.get_scroll("logs"), max_scroll)
            app.set_scroll(scroll, "logs")

        for i in range(vis_h):
            idx = scroll + i
            if idx >= len(lines):
                break
            line = lines[idx]
            upper = line.upper()
            l_attr = err_attr if ("ERROR" in upper or "FAIL" in upper or "EXCEPTION" in upper) else def_attr
            safe_addstr(stdscr, top + 3 + i, ix, line[:iw], l_attr)

    def handle_key(ch: int, app: TuiApp, state: Dict[str, Any]) -> bool:
        if ch in (ord("f"), ord("F")):
            ctrl.follow_tail = not ctrl.follow_tail
            app.show_toast(
                f"Log follow-tail: {'ON' if ctrl.follow_tail else 'OFF'}",
                "ok" if ctrl.follow_tail else "info",
            )
            return True

        # Manual upward scroll disables follow-tail so user can inspect history
        if ch in (curses.KEY_UP, ord("k"), curses.KEY_PPAGE, curses.KEY_HOME, ord("g")):
            ctrl.follow_tail = False
            return False  # Let TuiApp apply the scroll offset

        return False

    return TuiTab(
        tab_id="logs",
        title="Logs",
        render_fn=render,
        handle_key_fn=handle_key,
        legend="[F] Follow-Tail [↑/↓/PgUp/PgDn] Scroll",
    )


# ---------------------------------------------------------------------------
# Public Entry Point
# ---------------------------------------------------------------------------

def launch_tui(service_module: Any, start_tab: int = 0) -> None:
    """Build and run the 4-tab Lunamix Fleet curses TUI."""
    base_dir = getattr(service_module, "BASE_DIR", os.path.dirname(os.path.abspath(__file__)))
    poller = StatePoller(base_dir=base_dir, poll_interval=0.35, log_tail_lines=200)
    app = TuiApp(title="Lunamix Fleet Control Center", poller=poller, fps_ms=50)

    overview_ctrl = _OverviewController(service_module)
    creator_ctrl = _CreatorController(base_dir)
    logs_ctrl = _LogsController()

    tabs = [
        _build_overview_tab(service_module, overview_ctrl),
        _build_fleet_tab(),
        _build_creator_tab(creator_ctrl),
        _build_logs_tab(logs_ctrl),
    ]
    app.register_tabs(tabs)
    if tabs:
        app.active_tab_idx = max(0, min(int(start_tab), len(tabs) - 1))

    app.run()
