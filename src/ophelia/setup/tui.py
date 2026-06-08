"""Curses menus for setup — Hermes-style arrow / space / enter navigation."""

from __future__ import annotations

import sys
from typing import Callable

_KEEP = object()

NAV_UP = "up"
NAV_DOWN = "down"
NAV_SELECT = "select"
NAV_TOGGLE = "toggle"
NAV_CANCEL = "cancel"
NAV_NONE = "none"


def flush_stdin() -> None:
    try:
        if not sys.stdin.isatty():
            return
        import termios

        termios.tcflush(sys.stdin, termios.TCIFLUSH)
    except Exception:
        pass


def prompt_text(
    label: str,
    *,
    secret: bool = False,
    default: str = "",
    hint: str = "",
) -> str | None:
    """Text input outside curses (safe for tokens). None = cancelled."""
    flush_stdin()
    if hint:
        print(hint)
    try:
        if secret:
            import getpass

            prompt = f"{label}: "
            if default:
                prompt = f"{label} [keep current]: "
            val = getpass.getpass(prompt)
        else:
            suffix = f" [{default}]" if default else ""
            val = input(f"{label}{suffix}: ").strip()
        if not val and default:
            return default
        return val or None
    except (KeyboardInterrupt, EOFError):
        print()
        return None


def radiolist(
    title: str,
    items: list[str],
    *,
    selected: int = 0,
    description: str = "",
    cancel_returns: int | None = None,
) -> int:
    if cancel_returns is None:
        cancel_returns = selected
    if not items:
        return cancel_returns
    if not sys.stdin.isatty():
        return _radio_numbered_fallback(title, items, selected, cancel_returns, description)

    desc_lines = description.splitlines() if description else []

    def draw_header(stdscr, max_y, max_x):
        import curses

        row = 0
        try:
            attr = curses.A_BOLD
            if curses.has_colors():
                curses.init_pair(1, curses.COLOR_GREEN, -1)
                curses.init_pair(2, curses.COLOR_YELLOW, -1)
                attr |= curses.color_pair(2)
            stdscr.addnstr(row, 0, title, max_x - 1, attr)
            row += 1
            for line in desc_lines:
                if row >= max_y - 2:
                    break
                stdscr.addnstr(row, 0, line, max_x - 1, curses.A_NORMAL)
                row += 1
            stdscr.addnstr(
                row,
                0,
                " Up/Down move | Enter/Space select | Esc back",
                max_x - 1,
                curses.A_DIM,
            )
            row += 1
        except curses.error:
            pass
        return row + 1

    def draw_row(stdscr, y, i, is_cursor, max_x):
        import curses

        radio = "(o)" if is_cursor else "( )"
        arrow = ">" if is_cursor else " "
        line = f" {arrow} {radio} {items[i]}"
        attr = curses.A_BOLD if is_cursor else curses.A_NORMAL
        if curses.has_colors():
            attr |= curses.color_pair(1)
        try:
            stdscr.addnstr(y, 0, line, max_x - 1, attr)
        except curses.error:
            pass

    def on_action(action, cursor):
        if action in (NAV_SELECT, NAV_TOGGLE):
            return cursor
        return cancel_returns

    return _run_menu(
        initial=cursor_min(selected, len(items) - 1),
        count=len(items),
        draw_header=draw_header,
        draw_row=draw_row,
        on_action=on_action,
        cancel_value=cancel_returns,
        fallback=lambda: _radio_numbered_fallback(
            title, items, selected, cancel_returns, description
        ),
    )


def checkbox(
    title: str,
    items: list[str],
    *,
    selected: set[int] | None = None,
    description: str = "",
    cancel_returns: set[int] | None = None,
) -> set[int]:
    chosen = set(selected or [])
    if cancel_returns is None:
        cancel_returns = set(chosen)
    if not items:
        return cancel_returns
    if not sys.stdin.isatty():
        return _checkbox_numbered_fallback(title, items, chosen, cancel_returns, description)

    desc_lines = description.splitlines() if description else []

    def draw_header(stdscr, max_y, max_x):
        import curses

        row = 0
        try:
            attr = curses.A_BOLD
            if curses.has_colors():
                curses.init_pair(1, curses.COLOR_GREEN, -1)
                curses.init_pair(2, curses.COLOR_YELLOW, -1)
                attr |= curses.color_pair(2)
            stdscr.addnstr(row, 0, title, max_x - 1, attr)
            row += 1
            for line in desc_lines:
                if row >= max_y - 2:
                    break
                stdscr.addnstr(row, 0, line, max_x - 1, curses.A_NORMAL)
                row += 1
            stdscr.addnstr(
                row,
                0,
                " Up/Down move | Space toggle | Enter confirm | Esc back",
                max_x - 1,
                curses.A_DIM,
            )
            row += 1
        except curses.error:
            pass
        return row + 1

    def draw_row(stdscr, y, i, is_cursor, max_x):
        import curses

        mark = "[x]" if i in chosen else "[ ]"
        arrow = ">" if is_cursor else " "
        line = f" {arrow} {mark} {items[i]}"
        attr = curses.A_BOLD if is_cursor else curses.A_NORMAL
        if curses.has_colors():
            attr |= curses.color_pair(1)
        try:
            stdscr.addnstr(y, 0, line, max_x - 1, attr)
        except curses.error:
            pass

    def on_action(action, cursor):
        if action == NAV_TOGGLE:
            chosen.symmetric_difference_update({cursor})
            return _KEEP
        if action == NAV_SELECT:
            return set(chosen)
        return cancel_returns

    result = _run_menu(
        initial=0,
        count=len(items),
        draw_header=draw_header,
        draw_row=draw_row,
        on_action=on_action,
        cancel_value=cancel_returns,
        fallback=lambda: _checkbox_numbered_fallback(
            title, items, chosen, cancel_returns, description
        ),
    )
    if result is _KEEP:
        return cancel_returns
    return result


def _run_menu(
    *,
    initial: int,
    count: int,
    draw_header: Callable,
    draw_row: Callable,
    on_action: Callable,
    cancel_value,
    fallback: Callable,
):
    if not sys.stdin.isatty():
        return cancel_value
    try:
        import curses

        holder: list = [cancel_value]

        def _loop(stdscr):
            curses.curs_set(0)
            if curses.has_colors():
                curses.start_color()
                curses.use_default_colors()
            cursor = initial
            scroll = 0
            while True:
                stdscr.clear()
                max_y, max_x = stdscr.getmaxyx()
                start = draw_header(stdscr, max_y, max_x)
                visible = max(1, max_y - start - 1)
                if cursor < scroll:
                    scroll = cursor
                elif cursor >= scroll + visible:
                    scroll = cursor - visible + 1
                scroll = max(0, min(scroll, max(0, count - visible)))
                for row_i, idx in enumerate(range(scroll, min(count, scroll + visible))):
                    draw_row(stdscr, start + row_i, idx, idx == cursor, max_x)
                stdscr.refresh()
                action = _read_key(stdscr)
                if action == NAV_UP:
                    cursor = (cursor - 1) % count
                    continue
                if action == NAV_DOWN:
                    cursor = (cursor + 1) % count
                    continue
                out = on_action(action, cursor)
                if out is not _KEEP:
                    holder[0] = out
                    return

        curses.wrapper(_loop)
        flush_stdin()
        return holder[0]
    except Exception:
        flush_stdin()
        return fallback()


def _read_key(stdscr) -> str:
    import curses

    key = stdscr.getch()
    if key in (curses.KEY_UP, ord("k")):
        return NAV_UP
    if key in (curses.KEY_DOWN, ord("j")):
        return NAV_DOWN
    if key in (curses.KEY_ENTER, 10, 13):
        return NAV_SELECT
    if key == ord(" "):
        return NAV_TOGGLE
    if key == ord("q"):
        return NAV_CANCEL
    if key == 27:
        try:
            stdscr.timeout(60)
            nxt = stdscr.getch()
        finally:
            stdscr.timeout(-1)
        if nxt == -1:
            return NAV_CANCEL
        if nxt in (ord("["), ord("O")):
            final = stdscr.getch()
            if final in (ord("A"), ord("k")):
                return NAV_UP
            if final in (ord("B"), ord("j")):
                return NAV_DOWN
        return NAV_CANCEL
    return NAV_NONE


def _radio_numbered_fallback(
    title: str,
    items: list[str],
    selected: int,
    cancel_returns: int,
    description: str,
) -> int:
    print(f"\n{title}")
    if description:
        print(description)
    for i, label in enumerate(items):
        mark = "*" if i == selected else " "
        print(f"  {mark} {i + 1}. {label}")
    try:
        val = input(f"Choice [1-{len(items)}, Enter={selected + 1}, q=cancel]: ").strip()
        if val.lower() in ("q", "esc"):
            return cancel_returns
        if not val:
            return selected
        idx = int(val) - 1
        if 0 <= idx < len(items):
            return idx
    except (ValueError, KeyboardInterrupt, EOFError):
        return cancel_returns
    return selected


def _checkbox_numbered_fallback(
    title: str,
    items: list[str],
    chosen: set[int],
    cancel_returns: set[int],
    description: str,
) -> set[int]:
    picked = set(chosen)
    print(f"\n{title}")
    if description:
        print(description)
    while True:
        for i, label in enumerate(items):
            mark = "[x]" if i in picked else "[ ]"
            print(f"  {mark} {i + 1}. {label}")
        try:
            val = input("Toggle #, Enter=done, q=cancel: ").strip()
            if val.lower() in ("q", "esc"):
                return cancel_returns
            if not val:
                return picked
            idx = int(val) - 1
            if 0 <= idx < len(items):
                picked.symmetric_difference_update({idx})
        except (ValueError, KeyboardInterrupt, EOFError):
            return cancel_returns


def cursor_min(a: int, b: int) -> int:
    return min(a, b)
