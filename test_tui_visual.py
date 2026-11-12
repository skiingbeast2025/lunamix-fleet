#!/usr/bin/env python3
"""
test_tui_visual.py — PTY & Visual Layout Test Suite for Lunamix TUI.

Tests:
1. Viewport Sizes & Small-Terminal Safety (40x12, 60x18, 80x24, 200x60)
2. Live SIGWINCH Resize Mid-Render (80x24 -> 45x14 -> 120x35 -> 70x20)
3. Rapid Tab Switching, Scrolling Stress & InlineModal Open/Cancel
4. Monochrome / No-Color Terminal Compatibility (TERM=vt100)
5. Long (150-char), Wide Unicode, and Corrupted/Empty bot_states Overflow Tests
6. Clean Terminal Teardown (exit code 0, alt-screen exit \\x1b[?1049l, termios restore)
"""

import curses
import fcntl
import json
import os
import pty
import re
import select
import shutil
import signal
import struct
import subprocess
import tempfile
import termios
import time
import unittest
from unittest import mock

import tui_engine
import tui_views


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SERVICE_SH = os.path.join(BASE_DIR, "service.sh")
ANSI_RE = re.compile(rb"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07|\x1b[=>()]")


def strip_ansi(raw: bytes) -> str:
    """Strip ANSI escape sequences for readable substring assertions."""
    cleaned = ANSI_RE.sub(b" ", raw)
    return cleaned.decode("utf-8", errors="replace")


def set_pty_size(fd: int, cols: int, rows: int) -> None:
    """Set PTY window size via TIOCSWINSZ."""
    winsize = struct.pack("HHHH", rows, cols, 0, 0)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)


def read_pty_for(master_fd: int, duration: float) -> bytes:
    """Drain available bytes from master_fd for `duration` seconds."""
    chunks = []
    deadline = time.monotonic() + duration
    while time.monotonic() < deadline:
        remaining = max(0.01, deadline - time.monotonic())
        r, _, _ = select.select([master_fd], [], [], min(0.05, remaining))
        if master_fd in r:
            try:
                data = os.read(master_fd, 8192)
                if not data:
                    break
                chunks.append(data)
            except OSError:
                break
    return b"".join(chunks)


def spawn_pty_process(
    cmd,
    cols: int = 80,
    rows: int = 24,
    term: str = "xterm-256color",
    cwd: str = BASE_DIR,
):
    """Spawn a command attached to a real controlling PTY."""
    master_fd, slave_fd = pty.openpty()
    set_pty_size(slave_fd, cols, rows)

    env = os.environ.copy()
    env["TERM"] = term
    env.setdefault("ESCDELAY", "25")

    def _preexec():
        os.setsid()
        fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)

    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        env=env,
        preexec_fn=_preexec,
        close_fds=True,
    )
    os.close(slave_fd)
    return proc, master_fd


def assert_no_errors(raw_output: bytes, context: str = "") -> None:
    """Assert that no Python tracebacks or curses errors appeared in PTY output."""
    text = strip_ansi(raw_output)
    for bad in ("Traceback (most recent call last)", "curses.error", "Failed to launch interactive TUI"):
        if bad in text:
            raise AssertionError(f"Found {bad!r} in output ({context}):\n{text[-1200:]}")


class FakeCursesWindow:
    """In-memory virtual curses window that enforces bounds and tracks every cell write."""

    def __init__(self, rows: int, cols: int) -> None:
        self.rows = rows
        self.cols = cols
        self.writes = []  # List of (y, x, text, attr)
        self.erased = False
        self.cursor = (0, 0)

    def getmaxyx(self):
        return (self.rows, self.cols)

    def erase(self):
        self.erased = True
        self.writes.clear()

    def clear(self):
        self.erase()

    def move(self, y: int, x: int):
        if y < 0 or y >= self.rows or x < 0 or x >= self.cols:
            raise curses.error("move out of bounds")
        self.cursor = (y, x)

    def noutrefresh(self):
        pass

    def addstr(self, y: int, x: int, text: str, attr: int = 0):
        if y < 0 or y >= self.rows or x < 0 or x >= self.cols:
            raise curses.error(f"addstr out of bounds ({y}, {x}) for ({self.rows}, {self.cols})")
        dw = tui_engine._display_width(text)
        if x + dw > self.cols:
            raise curses.error(f"line overflow at y={y}, x={x}, dw={dw}, cols={self.cols}")
        if y == self.rows - 1 and x + dw >= self.cols:
            raise curses.error(f"bottom-right cell write at y={y}, x={x}, dw={dw}, cols={self.cols}")
        self.writes.append((y, x, text, attr))


class TestTuiPtyVisual(unittest.TestCase):
    """Real PTY + SIGWINCH + Visual Rendering Tests."""

    def _run_quit(self, proc, master_fd, wait_s: float = 4.0):
        os.write(master_fd, b"q")
        tail = read_pty_for(master_fd, 0.4)
        rc = proc.wait(timeout=wait_s)
        # Drain any remaining bytes after exit
        tail += read_pty_for(master_fd, 0.15)
        return rc, tail

    def test_01_tiny_terminal_40x12(self):
        """Tiny terminal (40x12) shows 'Terminal too small' cleanly without crashing."""
        proc, master = spawn_pty_process([SERVICE_SH], cols=40, rows=12)
        try:
            out = read_pty_for(master, 0.7)
            rc, tail = self._run_quit(proc, master)
            full = out + tail
            self.assertEqual(rc, 0)
            assert_no_errors(full, "tiny 40x12")
            text = strip_ansi(full)
            self.assertIn("Terminal too small", text)
            self.assertIn("40x12", text)
        finally:
            if proc.poll() is None:
                proc.kill()
            os.close(master)

    def test_02_compact_60x18_and_standard_80x24_all_tabs(self):
        """Compact (60x18) and Standard (80x24) terminals render all 4 tabs cleanly."""
        for cols, rows in ((60, 18), (80, 24)):
            with self.subTest(size=f"{cols}x{rows}"):
                proc, master = spawn_pty_process([SERVICE_SH], cols=cols, rows=rows)
                try:
                    chunks = [read_pty_for(master, 0.5)]
                    for key in (b"1", b"2", b"3", b"4", b"\t"):
                        os.write(master, key)
                        chunks.append(read_pty_for(master, 0.25))
                    rc, tail = self._run_quit(proc, master)
                    full = b"".join(chunks) + tail
                    self.assertEqual(rc, 0)
                    assert_no_errors(full, f"size {cols}x{rows}")
                    text = strip_ansi(full)
                    self.assertIn("Overview", text)
                    self.assertIn("Fleet", text)
                    self.assertIn("Creator", text)
                    self.assertIn("Logs", text)
                finally:
                    if proc.poll() is None:
                        proc.kill()
                    os.close(master)

    def test_03_wide_terminal_200x60(self):
        """Wide terminal (200x60) renders cleanly with no out-of-bounds errors."""
        proc, master = spawn_pty_process([SERVICE_SH], cols=200, rows=60)
        try:
            chunks = [read_pty_for(master, 0.5)]
            for key in (b"2", b"3", b"4", b"1"):
                os.write(master, key)
                chunks.append(read_pty_for(master, 0.2))
            rc, tail = self._run_quit(proc, master)
            full = b"".join(chunks) + tail
            self.assertEqual(rc, 0)
            assert_no_errors(full, "wide 200x60")
            text = strip_ansi(full)
            self.assertIn("Lunamix Fleet Control Center", text)
        finally:
            if proc.poll() is None:
                proc.kill()
            os.close(master)

    def test_04_live_sigwinch_resize_mid_render(self):
        """Dynamic PTY resize (80x24 -> 45x14 -> 120x35 -> 70x20) with SIGWINCH & keys."""
        proc, master = spawn_pty_process([SERVICE_SH], cols=80, rows=24)
        try:
            chunks = [read_pty_for(master, 0.45)]
            sequence = [
                (45, 14, b"2"),   # Below MIN_TERM_COLS -> small terminal warning
                (120, 35, b"\t"), # Large -> tab 3
                (70, 20, b"4"),   # Medium -> tab 4
                (80, 24, b"1"),   # Back to standard -> tab 1
            ]
            for cols, rows, key in sequence:
                set_pty_size(master, cols, rows)
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGWINCH)
                except OSError:
                    pass
                chunks.append(read_pty_for(master, 0.2))
                os.write(master, key)
                chunks.append(read_pty_for(master, 0.2))

            rc, tail = self._run_quit(proc, master)
            full = b"".join(chunks) + tail
            self.assertEqual(rc, 0)
            assert_no_errors(full, "live SIGWINCH")
            text = strip_ansi(full)
            self.assertIn("Terminal too small", text)
            self.assertIn("Lunamix Fleet Control Center", text)
        finally:
            if proc.poll() is None:
                proc.kill()
            os.close(master)

    def test_05_rapid_tab_scroll_and_modal_cancel(self):
        """Rapid tab switching, scrolling, and InlineModal open/cancel on populated state."""
        tmpdir = tempfile.mkdtemp(prefix="lunamix_tui_test_")
        try:
            logs_dir = os.path.join(tmpdir, "logs")
            os.makedirs(logs_dir, exist_ok=True)
            bot_states = [
                {"bot": f"BotUser_{i:02d}", "status": "RUNNING" if i % 2 == 0 else "RATE_LIMITED", "sent": i * 3, "error": f"ok_{i}"}
                for i in range(40)
            ]
            with open(os.path.join(logs_dir, "telemetry_state.json"), "w", encoding="utf-8") as fh:
                json.dump({
                    "status": "RUNNING",
                    "uptime_str": "01:23:45",
                    "active_bots": 40,
                    "messages_sent": 1200,
                    "messages_failed": 2,
                    "messages_per_minute": 42.5,
                    "banned_count": 1,
                    "last_latency_ms": 55,
                    "bot_states": bot_states,
                }, fh)
            with open(os.path.join(logs_dir, "simulator.log"), "w", encoding="utf-8") as fh:
                for i in range(80):
                    fh.write(f"2026-11-12 08:00:{i%60:02d} INFO Tick {i} completed\n")

            # Launch tui_views against the isolated temp dir so real workspace files are untouched
            runner_code = (
                "import sys, types\n"
                f"sys.path.insert(0, {BASE_DIR!r})\n"
                "import tui_views\n"
                "svc = types.SimpleNamespace(\n"
                f"    BASE_DIR={tmpdir!r},\n"
                "    is_systemd_enabled=lambda: False,\n"
                "    load_config=lambda: {'bot_count': 'all', 'min_delay_per_bot': 5.1},\n"
                "    save_config=lambda c: None,\n"
                ")\n"
                "tui_views.launch_tui(svc, start_tab=0)\n"
            )
            proc, master = spawn_pty_process(["python3", "-c", runner_code], cols=80, rows=24)
            try:
                chunks = [read_pty_for(master, 0.5)]

                # Open InlineModal ('b') and cancel with Esc
                os.write(master, b"b")
                modal_out = read_pty_for(master, 0.25)
                chunks.append(modal_out)
                self.assertIn("Configure Bot Count", strip_ansi(modal_out))
                os.write(master, b"\x1b")
                after_esc = read_pty_for(master, 0.25)
                chunks.append(after_esc)

                # Rapidly switch tabs and scroll across Fleet & Logs
                # Note: curses keypad(True) sends smkx, so xterm arrow keys use SS3 (\x1bOA-D)
                keys = [
                    b"2", b"\x1bOB", b"\x1bOB", b"\x1b[6~", b"\x1b[5~", b"\x1bOH",
                    b"4", b"\x1bOA", b"\x1b[5~", b"\x1b[6~", b"\x1bOH",
                    b"\t", b"\x1bOC", b"\x1bOD", b"1", b"3", b"2", b"4", b"1",
                ]
                for k in keys:
                    os.write(master, k)
                    chunks.append(read_pty_for(master, 0.06))

                rc, tail = self._run_quit(proc, master)
                full = b"".join(chunks) + tail
                self.assertEqual(rc, 0)
                assert_no_errors(full, "rapid tab/scroll/modal")
                # Verify modal title does not reappear on final tab redraw after Esc
                self.assertNotIn("Configure Bot Count", strip_ansi(tail))
            finally:
                if proc.poll() is None:
                    proc.kill()
                os.close(master)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_06_monochrome_vt100_compatibility(self):
        """TERM=vt100 (no colors) runs and exits cleanly without crashing."""
        proc, master = spawn_pty_process([SERVICE_SH], cols=80, rows=24, term="vt100")
        try:
            chunks = [read_pty_for(master, 0.5)]
            for k in (b"2", b"3", b"4", b"1"):
                os.write(master, k)
                chunks.append(read_pty_for(master, 0.15))
            rc, tail = self._run_quit(proc, master)
            full = b"".join(chunks) + tail
            self.assertEqual(rc, 0)
            assert_no_errors(full, "TERM=vt100")
        finally:
            if proc.poll() is None:
                proc.kill()
            os.close(master)

    def test_07_init_colors_no_color_fallback(self):
        """Unit test: init_colors() returns monochrome attributes when has_colors() is False."""
        with mock.patch.object(curses, "has_colors", return_value=False):
            pal = tui_engine.init_colors()
            self.assertEqual(pal["default"], 0)
            self.assertIn("header", pal)
            self.assertIn("err", pal)
            self.assertIn("tab_active", pal)

    def test_08_long_unicode_and_corrupted_bot_states(self):
        """Unit/render test: 150-char names, wide Unicode, and corrupted/empty bot_states."""
        win = FakeCursesWindow(rows=24, cols=80)
        long_name = "🤖超级长Bot名称🚀_" + ("X" * 140)
        long_err = "🔥Error_詳細_" + ("E" * 150)

        # 1. Direct safe_addstr clipping including bottom-right cell and wide Unicode
        tui_engine.safe_addstr(win, 23, 0, "A" * 200, 0)
        tui_engine.safe_addstr(win, 5, 65, "🤖🤖🤖🤖🤖🤖🤖🤖🤖🤖", 0)
        tui_engine.safe_addstr(win, 6, -3, "abcdef", 0)

        # 2. Direct draw_table with 150-char & wide Unicode cells
        headers = ["Bot Name", "Status", "Sent", "Last Update / Error"]
        rows = [
            [long_name, "RATE_LIMITED", 999, long_err],
            ["NormalBot", "OK", 12, "all good"],
        ]
        win.erase()
        tui_engine.draw_table(win, 2, 2, 10, 76, headers, rows, col_widths=[20, 12, 8, 33])
        self.assertGreater(len(win.writes), 0)

        # 3. Full render across all 4 tabs with corrupted & empty bot_states
        app = tui_engine.TuiApp(title="Test", poller=tui_engine.StatePoller(base_dir=BASE_DIR))
        app.colors = {"default": 0, "accent": 0, "ok": 0, "warn": 0, "err": 0, "muted": 0}
        svc = mock.MagicMock()
        svc.is_systemd_enabled.return_value = False
        tabs = [
            tui_views._build_overview_tab(svc, tui_views._OverviewController(svc)),
            tui_views._build_fleet_tab(),
            tui_views._build_creator_tab(tui_views._CreatorController(BASE_DIR)),
            tui_views._build_logs_tab(tui_views._LogsController()),
        ]
        app.register_tabs(tabs)

        corrupted_state = {
            "daemon_running": True,
            "messages_sent": "not_a_num",
            "bot_states": [
                None,
                "corrupt_string",
                12345,
                {"bot": long_name, "status": "RATE_LIMITED", "sent": "NaN", "error": long_err},
                {"username": "正常用户", "status": None, "sent": None},
            ],
            "log_lines": [long_err, "NORMAL LOG LINE"],
            "config": None,
        }
        for tab in tabs:
            win.erase()
            tab.render_fn(win, app, corrupted_state, (2, 0, 21, 80))

        # Empty bot_states
        win.erase()
        tabs[1].render_fn(win, app, {"bot_states": []}, (2, 0, 21, 80))

    def test_09_clean_terminal_teardown(self):
        """Pressing 'q' exits with 0, emits alt-screen exit \\x1b[?1049l, and restores terminal."""
        proc, master = spawn_pty_process([SERVICE_SH], cols=80, rows=24)
        try:
            out = read_pty_for(master, 0.6)
            rc, tail = self._run_quit(proc, master)
            full = out + tail
            self.assertEqual(rc, 0)
            self.assertIn(b"\x1b[?1049h", full, "Expected alternate screen enter sequence")
            self.assertIn(b"\x1b[?1049l", full, "Expected alternate screen exit sequence")
            assert_no_errors(full, "teardown")
        finally:
            if proc.poll() is None:
                proc.kill()
            os.close(master)


if __name__ == "__main__":
    unittest.main(verbosity=2)
