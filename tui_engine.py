#!/usr/bin/env python3
"""
tui_engine.py — Zero-flicker double-buffered curses TUI core and async StatePoller.

Provides:
- Safe color & monochrome attribute initialization
- Crash-proof drawing primitives: safe_addstr, draw_box, draw_progress_bar, draw_table
- Non-blocking centered InlineModal (edit_text, confirm)
- Background daemon StatePoller with thread-safe get_snapshot()
- TuiApp runner with tabs, per-tab scroll state, toast notifications, and resize handling
"""

from __future__ import annotations

import curses
from dataclasses import dataclass, field
import json
import os
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple
import unicodedata


# Ensure instantaneous Esc key handling inside curses
os.environ.setdefault("ESCDELAY", "25")

MIN_TERM_COLS = 52
MIN_TERM_ROWS = 14


# ---------------------------------------------------------------------------
# Color & Attribute Management
# ---------------------------------------------------------------------------

def init_colors() -> Dict[str, int]:
    """Initialize curses color pairs safely, falling back to monochrome attributes."""
    palette: Dict[str, int] = {
        "default": 0,
        "header": curses.A_BOLD | curses.A_REVERSE,
        "accent": curses.A_BOLD,
        "ok": curses.A_BOLD,
        "warn": curses.A_BOLD,
        "err": curses.A_BOLD | curses.A_REVERSE,
        "muted": curses.A_DIM,
        "select": curses.A_REVERSE | curses.A_BOLD,
        "border": curses.A_DIM,
        "tab_active": curses.A_REVERSE | curses.A_BOLD,
        "tab_inactive": curses.A_DIM,
        "toast_info": curses.A_REVERSE,
        "toast_ok": curses.A_REVERSE | curses.A_BOLD,
        "toast_warn": curses.A_REVERSE | curses.A_BOLD,
        "toast_err": curses.A_REVERSE | curses.A_BOLD,
    }

    try:
        if not curses.has_colors():
            return palette
        curses.start_color()
        try:
            curses.use_default_colors()
            bg = -1
        except curses.error:
            bg = curses.COLOR_BLACK

        pair_defs = {
            "header": (curses.COLOR_BLACK, curses.COLOR_CYAN),
            "accent": (curses.COLOR_CYAN, bg),
            "ok": (curses.COLOR_GREEN, bg),
            "warn": (curses.COLOR_YELLOW, bg),
            "err": (curses.COLOR_RED, bg),
            "muted": (curses.COLOR_WHITE, bg),
            "select": (curses.COLOR_BLACK, curses.COLOR_WHITE),
            "border": (curses.COLOR_BLUE, bg),
            "tab_active": (curses.COLOR_BLACK, curses.COLOR_CYAN),
            "tab_inactive": (curses.COLOR_WHITE, bg),
            "toast_info": (curses.COLOR_BLACK, curses.COLOR_CYAN),
            "toast_ok": (curses.COLOR_BLACK, curses.COLOR_GREEN),
            "toast_warn": (curses.COLOR_BLACK, curses.COLOR_YELLOW),
            "toast_err": (curses.COLOR_WHITE, curses.COLOR_RED),
        }

        bold_keys = {"header", "accent", "ok", "warn", "err", "select", "tab_active", "toast_ok", "toast_err"}
        dim_keys = {"muted", "tab_inactive"}

        for idx, (name, (fg, pair_bg)) in enumerate(pair_defs.items(), start=1):
            if idx >= getattr(curses, "COLOR_PAIRS", 64):
                break
            try:
                curses.init_pair(idx, fg, pair_bg)
                attr = curses.color_pair(idx)
                if name in bold_keys:
                    attr |= curses.A_BOLD
                elif name in dim_keys:
                    attr |= curses.A_DIM
                palette[name] = attr
            except curses.error:
                pass
    except curses.error:
        pass

    return palette


# ---------------------------------------------------------------------------
# Crash-Proof Drawing Primitives
# ---------------------------------------------------------------------------

def _sanitize_text(text: Any) -> str:
    """Convert to string and replace control characters so curses never chokes."""
    s = str(text)
    out: List[str] = []
    for ch in s:
        code = ord(ch)
        if code < 32 or code == 127:
            out.append(" ")
        else:
            out.append(ch)
    return "".join(out)


def _char_width(ch: str) -> int:
    """Return terminal display column width (0, 1, or 2) for a single character."""
    if unicodedata.combining(ch):
        return 0
    if unicodedata.east_asian_width(ch) in ("W", "F"):
        return 2
    return 1


def _display_width(text: str) -> int:
    """Return total terminal display column width of a sanitized string."""
    return sum(_char_width(ch) for ch in text)


def _clip_to_width(text: str, max_w: int) -> str:
    """Clip a string so its terminal display width does not exceed max_w."""
    if max_w <= 0:
        return ""
    out: List[str] = []
    used = 0
    for ch in text:
        cw = _char_width(ch)
        if used + cw > max_w:
            break
        out.append(ch)
        used += cw
    return "".join(out)


def _format_cell(text: Any, width: int) -> str:
    """Format a table cell to exact terminal display width with ellipsis truncation."""
    raw = _sanitize_text(text)
    if width <= 0:
        return ""
    dw = _display_width(raw)
    if dw > width:
        if width > 1:
            clipped = _clip_to_width(raw, width - 1) + "…"
        else:
            clipped = _clip_to_width(raw, 1)
        pad = max(0, width - _display_width(clipped))
        return clipped + (" " * pad)
    return raw + (" " * (width - dw))


def _safe_int(val: Any, default: int = 0) -> int:
    try:
        if val is None or isinstance(val, bool):
            return int(val) if isinstance(val, bool) else default
        return int(val)
    except (ValueError, TypeError):
        return default


def _safe_float(val: Any, default: float = 0.0) -> float:
    try:
        if val is None or isinstance(val, bool):
            return float(val) if isinstance(val, bool) else default
        return float(val)
    except (ValueError, TypeError):
        return default


def safe_addstr(win: Any, y: int, x: int, text: Any, attr: int = 0) -> None:
    """Safely write text to a curses window, clamping to bounds and avoiding bottom-right error."""
    if win is None:
        return
    try:
        max_y, max_x = win.getmaxyx()
    except curses.error:
        return

    if y < 0 or y >= max_y or x >= max_x:
        return

    clean = _sanitize_text(text)
    if x < 0:
        clean = clean[-x:]
        x = 0

    if not clean:
        return

    # Avoid writing into the exact bottom-right cell (max_y - 1, max_x - 1)
    avail = (max_x - x - 1) if (y == max_y - 1) else (max_x - x)
    if avail <= 0:
        return

    clipped = _clip_to_width(clean, avail)
    if not clipped:
        return

    try:
        win.addstr(y, x, clipped, attr)
    except curses.error:
        pass


def draw_box(
    win: Any,
    title: str = "",
    attr: int = 0,
    y: int = 0,
    x: int = 0,
    height: Optional[int] = None,
    width: Optional[int] = None,
    title_attr: Optional[int] = None,
) -> None:
    """Draw a clean Unicode box with an optional title, clamped safely to window bounds."""
    if win is None:
        return
    try:
        max_y, max_x = win.getmaxyx()
    except curses.error:
        return

    h = max_y - y if height is None else height
    w = max_x - x if width is None else width

    if h < 2 or w < 2:
        return

    # Clamp to window dimensions
    if y < 0:
        h += y
        y = 0
    if x < 0:
        w += x
        x = 0
    h = min(h, max_y - y)
    w = min(w, max_x - x)
    if h < 2 or w < 2:
        return

    inner_w = w - 2
    top_bar = "┌" + ("─" * inner_w) + "┐"
    bot_bar = "└" + ("─" * inner_w) + "┘"

    safe_addstr(win, y, x, top_bar, attr)
    for row in range(y + 1, y + h - 1):
        safe_addstr(win, row, x, "│", attr)
        safe_addstr(win, row, x + w - 1, "│", attr)
    safe_addstr(win, y + h - 1, x, bot_bar, attr)

    if title and w >= 6:
        t_attr = attr if title_attr is None else title_attr
        max_title_len = w - 4
        t_str = f" {_sanitize_text(title)[:max_title_len - 2]} "
        safe_addstr(win, y, x + 2, t_str, t_attr)


def draw_progress_bar(
    win: Any,
    y: int,
    x: int,
    width: int,
    ratio: float,
    attr: int = 0,
    fill_char: str = "█",
    empty_char: str = "░",
    show_pct: bool = True,
) -> None:
    """Draw a bounded progress bar at (y, x) with ratio clamped to [0.0, 1.0]."""
    if width <= 0:
        return
    clamped = max(0.0, min(1.0, float(ratio)))
    pct_str = f" {int(round(clamped * 100)):3d}%" if show_pct else ""

    if show_pct and width <= len(pct_str) + 3:
        safe_addstr(win, y, x, pct_str.strip()[:width], attr)
        return

    bar_total = width - len(pct_str) - 2  # account for '[' and ']'
    if bar_total < 1:
        safe_addstr(win, y, x, pct_str.strip()[:width], attr)
        return

    filled = int(round(clamped * bar_total))
    empty = bar_total - filled
    bar = "[" + (fill_char * filled) + (empty_char * empty) + "]" + pct_str
    safe_addstr(win, y, x, bar[:width], attr)


def _compute_col_widths(
    total_width: int,
    num_cols: int,
    col_widths: Optional[Sequence[int]] = None,
) -> List[int]:
    """Compute column widths fitting inside total_width (with 1-space gaps)."""
    if num_cols <= 0 or total_width <= 0:
        return []
    gaps = max(0, num_cols - 1)
    usable = max(num_cols, total_width - gaps)

    if col_widths and len(col_widths) == num_cols:
        widths = [max(1, int(w)) for w in col_widths]
        total_req = sum(widths)
        if total_req <= usable:
            return widths
        # Scale down proportionally while keeping >= 1
        scaled = [max(1, int(w * usable / total_req)) for w in widths]
        diff = usable - sum(scaled)
        idx = 0
        while diff < 0 and idx < num_cols * 2:
            c = idx % num_cols
            if scaled[c] > 1:
                scaled[c] -= 1
                diff += 1
            idx += 1
        return scaled

    base = usable // num_cols
    rem = usable % num_cols
    return [base + (1 if i < rem else 0) for i in range(num_cols)]


def draw_table(
    win: Any,
    start_y: int,
    start_x: int,
    height: int,
    width: int,
    headers: Sequence[str],
    rows: Sequence[Sequence[Any]],
    scroll_offset: int = 0,
    selected_idx: Optional[int] = None,
    col_widths: Optional[Sequence[int]] = None,
    header_attr: Optional[int] = None,
    row_attr: int = 0,
    selected_attr: Optional[int] = None,
) -> int:
    """
    Render a scrollable table inside (start_y, start_x, height, width).
    Returns the clamped scroll_offset.
    """
    if height < 2 or width < 4 or not headers:
        return 0

    widths = _compute_col_widths(width, len(headers), col_widths)
    h_attr = (curses.A_BOLD | curses.A_UNDERLINE) if header_attr is None else header_attr
    s_attr = (curses.A_REVERSE | curses.A_BOLD) if selected_attr is None else selected_attr

    def format_row(cells: Sequence[Any]) -> str:
        parts: List[str] = []
        for idx, cw in enumerate(widths):
            val = cells[idx] if idx < len(cells) else ""
            parts.append(_format_cell(val, cw))
        joined = " ".join(parts)
        return _clip_to_width(joined, width)

    # Header
    header_line = format_row(headers)
    header_line += " " * max(0, width - _display_width(header_line))
    safe_addstr(win, start_y, start_x, header_line, h_attr)

    visible_rows = max(0, height - 1)
    max_scroll = max(0, len(rows) - visible_rows)
    clamped_scroll = max(0, min(scroll_offset, max_scroll))

    for i in range(visible_rows):
        row_idx = clamped_scroll + i
        y = start_y + 1 + i
        if row_idx < len(rows):
            line = format_row(rows[row_idx])
            line += " " * max(0, width - _display_width(line))
            attr = s_attr if (selected_idx is not None and row_idx == selected_idx) else row_attr
            safe_addstr(win, y, start_x, line, attr)
        else:
            safe_addstr(win, y, start_x, " " * width, row_attr)

    return clamped_scroll


# ---------------------------------------------------------------------------
# InlineModal (Non-blocking centered curses modals)
# ---------------------------------------------------------------------------

class InlineModal:
    """Centered modal dialogs inside curses that never exit curses or block background threads."""

    @staticmethod
    def edit_text(
        stdscr: Any,
        title: str,
        prompt: str,
        initial: str = "",
        colors: Optional[Dict[str, int]] = None,
    ) -> Optional[str]:
        """
        Show a centered text input modal.
        Returns the entered string on Enter, or None on Esc.
        """
        pal = colors or {}
        border_attr = pal.get("accent", curses.A_BOLD)
        text_attr = pal.get("default", 0)
        muted_attr = pal.get("muted", curses.A_DIM)
        input_attr = pal.get("select", curses.A_REVERSE)

        buf = list(initial)
        cursor_pos = len(buf)

        try:
            curses.curs_set(1)
        except curses.error:
            pass

        try:
            while True:
                max_y, max_x = stdscr.getmaxyx()
                box_w = min(max(36, len(prompt) + 6, len(title) + 8), max(20, max_x - 4))
                box_h = 7
                if max_y < box_h or max_x < 20:
                    return None

                top_y = (max_y - box_h) // 2
                left_x = (max_x - box_w) // 2

                # Fill modal background
                for r in range(top_y, top_y + box_h):
                    safe_addstr(stdscr, r, left_x, " " * box_w, text_attr)

                draw_box(stdscr, title=title, attr=border_attr, y=top_y, x=left_x, height=box_h, width=box_w)
                safe_addstr(stdscr, top_y + 2, left_x + 2, prompt[: box_w - 4], text_attr)

                field_w = max(8, box_w - 4)
                full_str = "".join(buf)
                # Scroll input horizontally if longer than field_w - 1
                scroll_x = max(0, cursor_pos - field_w + 1)
                visible_str = full_str[scroll_x : scroll_x + field_w]
                safe_addstr(stdscr, top_y + 3, left_x + 2, visible_str.ljust(field_w), input_attr)
                safe_addstr(stdscr, top_y + 5, left_x + 2, "[Enter] Save   [Esc] Cancel"[: box_w - 4], muted_attr)

                try:
                    stdscr.move(top_y + 3, min(left_x + 2 + (cursor_pos - scroll_x), max_x - 2))
                except curses.error:
                    pass

                stdscr.noutrefresh()
                curses.doupdate()

                ch = stdscr.getch()
                if ch == -1:
                    continue
                if ch == curses.KEY_RESIZE:
                    if hasattr(curses, "update_lines_cols"):
                        curses.update_lines_cols()
                    stdscr.erase()
                    continue
                if ch == 27:  # Esc
                    return None
                if ch in (curses.KEY_ENTER, 10, 13):
                    return "".join(buf)
                if ch in (curses.KEY_BACKSPACE, 127, 8):
                    if cursor_pos > 0:
                        buf.pop(cursor_pos - 1)
                        cursor_pos -= 1
                elif ch == curses.KEY_DC:
                    if cursor_pos < len(buf):
                        buf.pop(cursor_pos)
                elif ch == curses.KEY_LEFT:
                    cursor_pos = max(0, cursor_pos - 1)
                elif ch == curses.KEY_RIGHT:
                    cursor_pos = min(len(buf), cursor_pos + 1)
                elif ch == curses.KEY_HOME:
                    cursor_pos = 0
                elif ch == curses.KEY_END:
                    cursor_pos = len(buf)
                elif 32 <= ch <= 126:
                    buf.insert(cursor_pos, chr(ch))
                    cursor_pos += 1
        finally:
            try:
                curses.curs_set(0)
            except curses.error:
                pass

    @staticmethod
    def confirm(
        stdscr: Any,
        title: str,
        message: str,
        colors: Optional[Dict[str, int]] = None,
        default_yes: bool = False,
    ) -> bool:
        """
        Show a centered Yes/No confirmation modal.
        Returns True if confirmed, False if cancelled/declined.
        """
        pal = colors or {}
        border_attr = pal.get("warn", curses.A_BOLD)
        text_attr = pal.get("default", 0)
        sel_attr = pal.get("select", curses.A_REVERSE | curses.A_BOLD)
        muted_attr = pal.get("muted", curses.A_DIM)

        selected_yes = default_yes

        while True:
            max_y, max_x = stdscr.getmaxyx()
            box_w = min(max(36, len(message) + 6, len(title) + 8), max(22, max_x - 4))
            box_h = 6
            if max_y < box_h or max_x < 22:
                return False

            top_y = (max_y - box_h) // 2
            left_x = (max_x - box_w) // 2

            for r in range(top_y, top_y + box_h):
                safe_addstr(stdscr, r, left_x, " " * box_w, text_attr)

            draw_box(stdscr, title=title, attr=border_attr, y=top_y, x=left_x, height=box_h, width=box_w)
            safe_addstr(stdscr, top_y + 2, left_x + 2, message[: box_w - 4], text_attr)

            yes_btn = " [ Yes ] "
            no_btn = " [ No ] "
            btn_y = top_y + 4
            center_x = left_x + box_w // 2
            safe_addstr(stdscr, btn_y, center_x - len(yes_btn) - 1, yes_btn, sel_attr if selected_yes else muted_attr)
            safe_addstr(stdscr, btn_y, center_x + 1, no_btn, muted_attr if selected_yes else sel_attr)

            stdscr.noutrefresh()
            curses.doupdate()

            ch = stdscr.getch()
            if ch == -1:
                continue
            if ch == curses.KEY_RESIZE:
                if hasattr(curses, "update_lines_cols"):
                    curses.update_lines_cols()
                stdscr.erase()
                continue
            if ch == 27 or ch in (ord("n"), ord("N")):
                return False
            if ch in (ord("y"), ord("Y")):
                return True
            if ch in (curses.KEY_LEFT, curses.KEY_RIGHT, 9):  # Tab / Arrows
                selected_yes = not selected_yes
            elif ch in (curses.KEY_ENTER, 10, 13):
                return selected_yes


# ---------------------------------------------------------------------------
# Async StatePoller
# ---------------------------------------------------------------------------

def _read_json_safe(path: str, default: Any) -> Tuple[Any, Optional[str]]:
    """Read and parse JSON safely without raising on missing/corrupt files."""
    if not os.path.exists(path):
        return default, None
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return json.load(fh), None
    except Exception as exc:
        return default, f"{os.path.basename(path)}: {exc}"


def _tail_lines_fast(path: str, max_lines: int = 120, chunk_size: int = 8192) -> List[str]:
    """Read the last `max_lines` lines from a potentially large file by seeking from EOF."""
    if not os.path.exists(path) or max_lines <= 0:
        return []
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            file_size = fh.tell()
            if file_size == 0:
                return []

            pos = file_size
            buffer = b""
            newline_count = 0

            while pos > 0 and newline_count <= max_lines:
                read_len = min(chunk_size, pos)
                pos -= read_len
                fh.seek(pos, os.SEEK_SET)
                chunk = fh.read(read_len)
                buffer = chunk + buffer
                newline_count = buffer.count(b"\n")

            text = buffer.decode("utf-8", errors="replace")
            lines = text.splitlines()
            return lines[-max_lines:]
    except Exception:
        return []


def _count_nonempty_lines(path: str) -> int:
    """Count non-empty lines in a text file safely."""
    if not os.path.exists(path):
        return 0
    try:
        count = 0
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if line.strip():
                    count += 1
        return count
    except Exception:
        return 0


def _check_pid_running(pid_path: str) -> Tuple[bool, Optional[int]]:
    """Check if PID file exists and the corresponding process is alive."""
    if not os.path.exists(pid_path):
        return False, None
    try:
        with open(pid_path, "r", encoding="utf-8", errors="replace") as fh:
            raw = fh.read().strip()
        pid = int(raw)
        if pid <= 0:
            return False, None
        os.kill(pid, 0)
        return True, pid
    except PermissionError:
        # Process exists, owned by another user
        return True, pid
    except (ValueError, ProcessLookupError, OSError):
        return False, None


class StatePoller:
    """Background daemon thread polling Lunamix service state every ~0.35s."""

    def __init__(
        self,
        base_dir: Optional[str] = None,
        poll_interval: float = 0.35,
        log_tail_lines: int = 120,
    ) -> None:
        self.base_dir = base_dir or os.path.dirname(os.path.abspath(__file__))
        self.poll_interval = max(0.05, float(poll_interval))
        self.log_tail_lines = max(1, int(log_tail_lines))

        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._snapshot: Dict[str, Any] = self._empty_snapshot()

    @staticmethod
    def _empty_snapshot() -> Dict[str, Any]:
        return {
            "daemon_running": False,
            "daemon_pid": None,
            "status": "STOPPED",
            "total_sent": 0,
            "messages_sent": 0,
            "messages_failed": 0,
            "msgs_per_min": 0.0,
            "uptime_str": "00:00:00",
            "uptime_seconds": 0,
            "paused": False,
            "active_bots": 0,
            "total_bots_configured": 0,
            "banned_count": 0,
            "last_latency_ms": 0,
            "bot_states": [],
            "config": {
                "bot_count": "all",
                "min_delay_per_bot": 0.0,
                "max_delay_per_bot": 0.0,
                "web_dashboard_port": 8088,
                "csv_file": "simulated_accounts.csv",
            },
            "proxy_rate_limits": {},
            "proxy_rate_limits_count": 0,
            "quarantined_proxies": {},
            "quarantined_proxies_count": 0,
            "working_proxies_count": 0,
            "account_count": 0,
            "log_lines": [],
            "poll_count": 0,
            "last_poll_ts": 0.0,
            "last_poll_duration_ms": 0.0,
            "errors": [],
        }

    def poll_once(self) -> Dict[str, Any]:
        """Perform a single synchronous poll and update the internal snapshot."""
        t0 = time.monotonic()
        errors: List[str] = []

        logs_dir = os.path.join(self.base_dir, "logs")
        pid_path = os.path.join(logs_dir, "daemon.pid")
        telemetry_path = os.path.join(logs_dir, "telemetry_state.json")
        config_path = os.path.join(self.base_dir, "hbu_service_config.json")
        rate_limits_path = os.path.join(logs_dir, "proxy_rate_limits.json")
        quarantine_path = os.path.join(logs_dir, "quarantined_proxies.json")
        working_proxies_path = os.path.join(self.base_dir, "working_proxies.txt")
        sim_log_path = os.path.join(logs_dir, "simulator.log")

        daemon_running, daemon_pid = _check_pid_running(pid_path)

        telemetry, err = _read_json_safe(telemetry_path, {})
        if err:
            errors.append(err)
        if not isinstance(telemetry, dict):
            telemetry = {}

        config_raw, err = _read_json_safe(config_path, {})
        if err:
            errors.append(err)
        if not isinstance(config_raw, dict):
            config_raw = {}

        rate_limits, err = _read_json_safe(rate_limits_path, {})
        if err:
            errors.append(err)
        quarantined, err = _read_json_safe(quarantine_path, {})
        if err:
            errors.append(err)

        working_proxies_count = _count_nonempty_lines(working_proxies_path)

        csv_rel = str(config_raw.get("csv_file") or "simulated_accounts.csv")
        csv_path = csv_rel if os.path.isabs(csv_rel) else os.path.join(self.base_dir, csv_rel)
        csv_lines = _count_nonempty_lines(csv_path)
        account_count = max(0, csv_lines - 1) if csv_lines > 0 else 0

        log_lines = _tail_lines_fast(sim_log_path, self.log_tail_lines)

        bot_states = telemetry.get("bot_states")
        if not isinstance(bot_states, list):
            bot_states = []

        messages_sent = _safe_int(telemetry.get("messages_sent"), 0)
        msgs_per_min = _safe_float(telemetry.get("messages_per_minute"), 0.0)
        paused = bool(telemetry.get("paused", config_raw.get("paused", False)))

        rate_count = len(rate_limits) if isinstance(rate_limits, (dict, list)) else 0
        quar_count = len(quarantined) if isinstance(quarantined, (dict, list)) else 0

        duration_ms = (time.monotonic() - t0) * 1000.0

        with self._lock:
            prev_polls = _safe_int(self._snapshot.get("poll_count", 0), 0)
            new_snap = {
                "daemon_running": daemon_running,
                "daemon_pid": daemon_pid,
                "status": str(telemetry.get("status") or ("RUNNING" if daemon_running else "STOPPED")),
                "total_sent": messages_sent,
                "messages_sent": messages_sent,
                "messages_failed": _safe_int(telemetry.get("messages_failed"), 0),
                "msgs_per_min": msgs_per_min,
                "uptime_str": str(telemetry.get("uptime_str") or "00:00:00"),
                "uptime_seconds": _safe_int(telemetry.get("uptime_seconds"), 0),
                "paused": paused,
                "active_bots": _safe_int(telemetry.get("active_bots"), 0),
                "total_bots_configured": telemetry.get("total_bots_configured", config_raw.get("bot_count", "all")),
                "banned_count": _safe_int(telemetry.get("banned_count"), 0),
                "last_latency_ms": _safe_int(telemetry.get("last_latency_ms"), 0),
                "bot_states": bot_states,
                "config": {
                    "bot_count": config_raw.get("bot_count", "all"),
                    "min_delay_per_bot": config_raw.get("min_delay_per_bot", 0.0),
                    "max_delay_per_bot": config_raw.get("max_delay_per_bot", 0.0),
                    "web_dashboard_port": config_raw.get("web_dashboard_port", 8088),
                    "csv_file": csv_rel,
                    **config_raw,
                },
                "proxy_rate_limits": rate_limits if isinstance(rate_limits, (dict, list)) else {},
                "proxy_rate_limits_count": rate_count,
                "quarantined_proxies": quarantined if isinstance(quarantined, (dict, list)) else {},
                "quarantined_proxies_count": quar_count,
                "working_proxies_count": working_proxies_count,
                "account_count": account_count,
                "log_lines": log_lines,
                "poll_count": prev_polls + 1,
                "last_poll_ts": time.time(),
                "last_poll_duration_ms": round(duration_ms, 2),
                "errors": errors,
            }
            self._snapshot = new_snap
            return dict(new_snap)

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            self.poll_once()
            self._stop_event.wait(self.poll_interval)

    def start(self) -> None:
        """Start the background polling daemon thread."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self.poll_once()
        self._thread = threading.Thread(target=self._run_loop, name="TuiStatePoller", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        """Signal the polling thread to stop and wait briefly."""
        self._stop_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=timeout)
        self._thread = None

    def get_snapshot(self) -> Dict[str, Any]:
        """Return a thread-safe shallow copy of the latest polled state."""
        with self._lock:
            return dict(self._snapshot)

    def __enter__(self) -> "StatePoller":
        self.start()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.stop()


# ---------------------------------------------------------------------------
# TuiApp Runner
# ---------------------------------------------------------------------------

@dataclass
class TuiTab:
    """Definition of a single tab view in TuiApp."""
    tab_id: str
    title: str
    render_fn: Callable[[Any, "TuiApp", Dict[str, Any], Tuple[int, int, int, int]], None]
    handle_key_fn: Optional[Callable[[int, "TuiApp", Dict[str, Any]], bool]] = None
    legend: str = ""


class TuiApp:
    """Zero-flicker double-buffered curses TUI application runner."""

    def __init__(
        self,
        title: str = "Lunamix Fleet TUI",
        poller: Optional[StatePoller] = None,
        fps_ms: int = 50,
    ) -> None:
        self.title = title
        self.poller = poller or StatePoller()
        self.fps_ms = max(16, int(fps_ms))
        self.tabs: List[TuiTab] = []
        self.active_tab_idx: int = 0
        self.scroll_offsets: Dict[str, int] = {}
        self.selected_indices: Dict[str, int] = {}
        self.colors: Dict[str, int] = {}
        self.running: bool = False
        self.stdscr: Any = None

        self._toast_msg: str = ""
        self._toast_level: str = "info"
        self._toast_expires_at: float = 0.0

    def register_tabs(self, tabs: Sequence[TuiTab]) -> None:
        """Register a sequence of TuiTab views."""
        self.tabs = list(tabs)
        for tab in self.tabs:
            self.scroll_offsets.setdefault(tab.tab_id, 0)
            self.selected_indices.setdefault(tab.tab_id, 0)
        self.active_tab_idx = 0

    def register_tab(self, tab: TuiTab) -> None:
        """Append a single TuiTab view."""
        self.tabs.append(tab)
        self.scroll_offsets.setdefault(tab.tab_id, 0)
        self.selected_indices.setdefault(tab.tab_id, 0)

    def show_toast(self, message: str, level: str = "info", duration: float = 3.0) -> None:
        """Display an auto-expiring status toast notification."""
        self._toast_msg = _sanitize_text(message)
        self._toast_level = level if level in ("info", "ok", "warn", "err") else "info"
        self._toast_expires_at = time.monotonic() + max(0.5, float(duration))

    def get_scroll(self, tab_id: Optional[str] = None) -> int:
        tid = tab_id or (self.tabs[self.active_tab_idx].tab_id if self.tabs else "default")
        return self.scroll_offsets.get(tid, 0)

    def set_scroll(self, value: int, tab_id: Optional[str] = None) -> None:
        tid = tab_id or (self.tabs[self.active_tab_idx].tab_id if self.tabs else "default")
        self.scroll_offsets[tid] = max(0, int(value))

    def get_selected(self, tab_id: Optional[str] = None) -> int:
        tid = tab_id or (self.tabs[self.active_tab_idx].tab_id if self.tabs else "default")
        return self.selected_indices.get(tid, 0)

    def set_selected(self, value: int, tab_id: Optional[str] = None) -> None:
        tid = tab_id or (self.tabs[self.active_tab_idx].tab_id if self.tabs else "default")
        self.selected_indices[tid] = max(0, int(value))

    def _draw_header(self, stdscr: Any, max_x: int, state: Dict[str, Any]) -> None:
        header_attr = self.colors.get("header", curses.A_REVERSE | curses.A_BOLD)
        safe_addstr(stdscr, 0, 0, " " * max_x, header_attr)

        daemon_up = state.get("daemon_running", False)
        paused = state.get("paused", False)
        if not daemon_up:
            badge = "STOPPED"
        elif paused:
            badge = "PAUSED"
        else:
            badge = "RUNNING"

        sent = state.get("total_sent", 0)
        rate = state.get("msgs_per_min", 0.0)
        bots = state.get("active_bots", 0)
        uptime = state.get("uptime_str", "00:00:00")

        left = f" {self.title} │ {badge} "
        right = f"Bots:{bots} │ Sent:{sent} ({rate:.1f}/m) │ Up:{uptime} "

        safe_addstr(stdscr, 0, 0, left, header_attr)
        if max_x > len(left) + len(right) + 1:
            safe_addstr(stdscr, 0, max_x - len(right), right, header_attr)

    def _draw_tab_bar(self, stdscr: Any, max_x: int) -> None:
        muted = self.colors.get("muted", curses.A_DIM)
        active_attr = self.colors.get("tab_active", curses.A_REVERSE | curses.A_BOLD)
        inactive_attr = self.colors.get("tab_inactive", curses.A_DIM)

        safe_addstr(stdscr, 1, 0, "─" * max_x, muted)
        x = 1
        for idx, tab in enumerate(self.tabs):
            label = f" [{idx + 1}] {tab.title} "
            attr = active_attr if idx == self.active_tab_idx else inactive_attr
            if x + len(label) <= max_x:
                safe_addstr(stdscr, 1, x, label, attr)
                x += len(label) + 1

    def _draw_footer(self, stdscr: Any, max_y: int, max_x: int) -> None:
        now = time.monotonic()
        if self._toast_msg and now < self._toast_expires_at:
            toast_key = f"toast_{self._toast_level}"
            toast_attr = self.colors.get(toast_key, curses.A_REVERSE)
            safe_addstr(stdscr, max_y - 2, 0, f" {self._toast_msg} ".ljust(max_x), toast_attr)

        footer_attr = self.colors.get("muted", curses.A_DIM)
        base_legend = "[Tab/←/→/1-4] Tabs  [↑/↓/PgUp/PgDn] Scroll  [q] Quit"
        if self.tabs and self.tabs[self.active_tab_idx].legend:
            legend = f"{self.tabs[self.active_tab_idx].legend} │ {base_legend}"
        else:
            legend = base_legend
        safe_addstr(stdscr, max_y - 1, 0, legend.ljust(max_x), footer_attr)

    def _handle_navigation_key(self, ch: int, state: Dict[str, Any]) -> bool:
        if ch in (ord("q"), ord("Q")):
            self.running = False
            return True

        if not self.tabs:
            return False

        cur_tab = self.tabs[self.active_tab_idx]

        # Let active tab handle custom keys first
        if cur_tab.handle_key_fn is not None:
            if cur_tab.handle_key_fn(ch, self, state):
                return True

        # Tab switching
        if ch in (9, curses.KEY_RIGHT):  # Tab or Right
            self.active_tab_idx = (self.active_tab_idx + 1) % len(self.tabs)
            return True
        if ch in (curses.KEY_BTAB, curses.KEY_LEFT):  # Shift-Tab or Left
            self.active_tab_idx = (self.active_tab_idx - 1) % len(self.tabs)
            return True
        if ord("1") <= ch <= ord("9"):
            target = ch - ord("1")
            if target < len(self.tabs):
                self.active_tab_idx = target
                return True

        # Per-tab scrolling
        tid = cur_tab.tab_id
        cur_scroll = self.scroll_offsets.get(tid, 0)
        if ch in (curses.KEY_UP, ord("k")):
            self.scroll_offsets[tid] = max(0, cur_scroll - 1)
            return True
        if ch in (curses.KEY_DOWN, ord("j")):
            self.scroll_offsets[tid] = cur_scroll + 1
            return True
        if ch == curses.KEY_PPAGE:
            self.scroll_offsets[tid] = max(0, cur_scroll - 10)
            return True
        if ch == curses.KEY_NPAGE:
            self.scroll_offsets[tid] = cur_scroll + 10
            return True
        if ch in (curses.KEY_HOME, ord("g")):
            self.scroll_offsets[tid] = 0
            return True

        return False

    def _main_loop(self, stdscr: Any) -> None:
        self.stdscr = stdscr
        self.colors = init_colors()
        self.running = True

        try:
            curses.curs_set(0)
        except curses.error:
            pass

        stdscr.keypad(True)
        stdscr.timeout(self.fps_ms)

        while self.running:
            max_y, max_x = stdscr.getmaxyx()
            stdscr.erase()

            state = self.poller.get_snapshot()

            if max_x < MIN_TERM_COLS or max_y < MIN_TERM_ROWS:
                msg = f"Terminal too small ({max_x}x{max_y})"
                sub = f"Need >= {MIN_TERM_COLS}x{MIN_TERM_ROWS}"
                warn_attr = self.colors.get("warn", curses.A_BOLD)
                safe_addstr(stdscr, max(0, max_y // 2), max(0, (max_x - len(msg)) // 2), msg, warn_attr)
                if max_y >= 3:
                    safe_addstr(stdscr, max(0, max_y // 2 + 1), max(0, (max_x - len(sub)) // 2), sub)
            else:
                self._draw_header(stdscr, max_x, state)
                self._draw_tab_bar(stdscr, max_x)

                content_y = 2
                content_h = max(1, max_y - 3)
                if self.tabs:
                    active_tab = self.tabs[self.active_tab_idx]
                    active_tab.render_fn(stdscr, self, state, (content_y, 0, content_h, max_x))

                self._draw_footer(stdscr, max_y, max_x)

            stdscr.noutrefresh()
            curses.doupdate()

            ch = stdscr.getch()
            if ch == -1:
                continue
            if ch == curses.KEY_RESIZE:
                if hasattr(curses, "update_lines_cols"):
                    curses.update_lines_cols()
                stdscr.erase()
                continue

            self._handle_navigation_key(ch, state)

    def run(self) -> None:
        """Start the background StatePoller and enter the curses TUI loop."""
        self.poller.start()
        try:
            curses.wrapper(self._main_loop)
        finally:
            self.poller.stop()
