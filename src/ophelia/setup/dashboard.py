"""Live status dashboard — a curses panel that auto-refreshes from the heartbeat.

Run with `ophelia dashboard` or pick "Live status dashboard" from the launcher.
Shows mood, drives, pressure, channels, autonomy state, and runtime info.
Hotkeys: r=refresh now, p=pause, s=resume, i=inner log, q=back.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from ophelia.config import OPHELIA_HOME
from ophelia.setup import tui

HEARTBEAT_PATH = OPHELIA_HOME / "data" / "heartbeat.json"
PAUSE_FLAG = OPHELIA_HOME / "data" / "pause.flag"


def _read_heartbeat() -> dict:
    if not HEARTBEAT_PATH.is_file():
        return {}
    try:
        return json.loads(HEARTBEAT_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _format_dashboard(hb: dict) -> list[str]:
    """Render the heartbeat as a list of display lines."""
    if not hb:
        return [
            "",
            "  Ophelia is not running (no heartbeat).",
            "  Start her with: ophelia start   (Termux)   or   ophelia run",
            "",
        ]
    age = int(time.time() - float(hb.get("ts", 0)))
    running = age < 120
    paused = hb.get("paused", False)
    state = "paused" if paused else ("running" if running else "stale")

    drives = hb.get("drives", {}) or {}
    drives_str = "  ".join(f"{k}={v:.2f}" for k, v in drives.items()) if drives else "n/a"

    channels = hb.get("channels", []) or []
    chan_str = ", ".join(channels) if channels else "none"

    lines = [
        f"  State:        {'*' if running else 'o'} {state}  (heartbeat {age}s old)",
        f"  Paused:       {'yes' if paused else 'no'}",
        f"  Mood:         {hb.get('mood', '?')}  (valence={hb.get('valence', '?')}, arousal={hb.get('arousal', '?')})",
        f"  Drives:       {drives_str}",
        f"  Pressure:     {hb.get('pressure', '?')}",
        f"  Last user:    {hb.get('last_user_msg_ago', '?')}s ago",
        f"  Channels:     {chan_str}",
        f"  Consciousness: {'on' if hb.get('consciousness') else 'off'}   "
        f"Dream: {'on' if hb.get('dream') else 'off'}",
        "",
    ]
    return lines


def _dashboard_numbered() -> None:
    """Fallback for non-TTY: print once and return."""
    hb = _read_heartbeat()
    print("\n=== Ophelia dashboard ===")
    for line in _format_dashboard(hb):
        print(line)
    print("[r] refresh  [p] pause  [s] resume  [i] inner log  [q] back")


def run_dashboard() -> None:
    """Live curses dashboard that refreshes every 2s."""
    import sys

    if not sys.stdin.isatty():
        _dashboard_numbered()
        tui.pause()
        return

    try:
        import curses
    except Exception:
        _dashboard_numbered()
        tui.pause()
        return

    def _loop(stdscr):
        curses.curs_set(0)
        if curses.has_colors():
            curses.start_color()
            curses.use_default_colors()
            curses.init_pair(1, curses.COLOR_GREEN, -1)
            curses.init_pair(2, curses.COLOR_YELLOW, -1)
            curses.init_pair(3, curses.COLOR_RED, -1)
            curses.init_pair(4, curses.COLOR_CYAN, -1)

        while True:
            stdscr.clear()
            max_y, max_x = stdscr.getmaxyx()
            row = 0
            try:
                stdscr.addnstr(row, 0, "Ophelia — live dashboard", max_x - 1, curses.A_BOLD)
                row += 1
                stdscr.addnstr(
                    row, 0,
                    " [r] refresh  [p] pause  [s] resume  [i] inner log  [q] back",
                    max_x - 1, curses.A_DIM,
                )
                row += 1
                stdscr.addnstr(row, 0, "─" * min(max_x - 1, 60), max_x - 1, curses.A_DIM)
                row += 1
            except curses.error:
                pass

            hb = _read_heartbeat()
            lines = _format_dashboard(hb)
            for line in lines:
                if row >= max_y - 1:
                    break
                attr = curses.A_NORMAL
                stripped = line.strip()
                if stripped.startswith("*"):
                    attr = curses.color_pair(1)
                elif stripped.startswith("o") and "stale" in line:
                    attr = curses.color_pair(2)
                try:
                    stdscr.addnstr(row, 0, line, max_x - 1, attr)
                except curses.error:
                    pass
                row += 1

            stdscr.refresh()

            # Wait for a key with 2s timeout.
            stdscr.timeout(2000)
            key = stdscr.getch()
            if key == ord("q") or key == 27:
                return
            if key == ord("r"):
                continue
            if key == ord("p"):
                PAUSE_FLAG.parent.mkdir(parents=True, exist_ok=True)
                PAUSE_FLAG.write_text("1", encoding="utf-8")
                continue
            if key == ord("s"):
                PAUSE_FLAG.unlink(missing_ok=True)
                continue
            if key == ord("i"):
                _show_inner_popup(stdscr)

    try:
        curses.wrapper(_loop)
    except Exception:
        _dashboard_numbered()
        tui.pause()
    tui.flush_stdin()


def _show_inner_popup(stdscr) -> None:
    """Briefly show the tail of the inner monologue."""
    import curses

    path = OPHELIA_HOME / "data" / "inner_monologue.md"
    text = ""
    if path.is_file():
        text = path.read_text(encoding="utf-8")
    lines = text.strip().split("\n")[-15:] if text else ["(no inner monologue yet)"]

    stdscr.clear()
    max_y, max_x = stdscr.getmaxyx()
    try:
        stdscr.addnstr(0, 0, "Inner monologue (last 15 lines) — any key to close", max_x - 1, curses.A_BOLD)
    except curses.error:
        pass
    for i, line in enumerate(lines):
        if i + 2 >= max_y:
            break
        try:
            stdscr.addnstr(i + 2, 0, line[: max_x - 1], max_x - 1, curses.A_NORMAL)
        except curses.error:
            pass
    stdscr.refresh()
    stdscr.timeout(-1)
    stdscr.getch()
