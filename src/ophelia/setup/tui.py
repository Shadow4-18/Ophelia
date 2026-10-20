"""Curses menus for setup and the launcher.

Keys: Up/Down (or j/k) move, Enter picks, Space toggles a checkbox,
Esc / q / Left / Backspace goes back, digits jump to a numbered row,
PgUp/PgDn/Home/End page. Tapping a row (Termux sends touches as mouse
clicks) picks it. Keys a menu doesn't use are ignored: the old menus treated
them as "back", so the resize Termux sends when the on-screen keyboard
opens or closes made menus vanish.

``(o)`` marks the saved value, ``>`` marks the cursor. Rows given as
``Header("...")`` are section titles the cursor skips.

Without a TTY (or if curses fails) every menu falls back to numbered input.
"""

from __future__ import annotations

import sys
import time
from typing import Callable, Sequence

NAV_UP = "up"
NAV_DOWN = "down"
NAV_PGUP = "pgup"
NAV_PGDN = "pgdn"
NAV_HOME = "home"
NAV_END = "end"
NAV_SELECT = "select"
NAV_TOGGLE = "toggle"
NAV_CANCEL = "cancel"
NAV_NONE = "none"

_KEEP = object()


class Header(str):
    """A section title row: drawn, never selectable."""


def flush_stdin() -> None:
    """Drain stray keypresses after curses (Enter/arrow bytes confuse the next menu)."""
    try:
        if not sys.stdin.isatty():
            return
        import termios

        termios.tcflush(sys.stdin, termios.TCIFLUSH)
    except Exception:
        pass


def pause(message: str = "Press Enter to continue...") -> None:
    flush_stdin()
    try:
        input(f"\n{message}")
    except (KeyboardInterrupt, EOFError):
        print()


def prompt_text(
    label: str,
    *,
    secret: bool = False,
    default: str = "",
    hint: str = "",
    clearable: bool = False,
    validate: Callable[[str], str | None] | None = None,
) -> str | None:
    """Text input outside curses (safe for tokens).

    Returns None when cancelled (Ctrl+C / EOF), ``default`` on a blank line,
    and ``""`` when ``clearable`` and the user types ``-`` (clear the value).
    ``validate`` returns an error message to re-ask, or None when fine.
    """
    flush_stdin()
    if hint:
        print(hint)
    if clearable:
        print("  (Enter keeps the current value, '-' clears it)")
    while True:
        try:
            if secret:
                import getpass

                prompt = f"{label} [keep current]: " if default else f"{label}: "
                val = getpass.getpass(prompt).strip()
            else:
                suffix = f" [{default}]" if default else ""
                val = input(f"{label}{suffix}: ").strip()
        except (KeyboardInterrupt, EOFError):
            print()
            return None
        if clearable and val == "-":
            return ""
        if not val:
            return default or None
        if validate is not None:
            err = validate(val)
            if err:
                print(f"  {err}")
                continue
        return val


def confirm(question: str, *, default: bool = True, description: str = "") -> bool | None:
    """Yes/No menu. None when backed out."""
    idx = radiolist(question, ["Yes", "No"], selected=0 if default else 1,
                    description=description, marks=False)
    return None if idx < 0 else idx == 0


# --- drawing -------------------------------------------------------------


def _selectable(items: Sequence[str]) -> list[int]:
    return [i for i, it in enumerate(items) if not isinstance(it, Header)]


def _numbers(items: Sequence[str]) -> dict[int, int]:
    """Row index -> 1-based number shown next to selectable rows."""
    return {row: n for n, row in enumerate(_selectable(items), 1)}


def _init_colors() -> None:
    import curses

    if not curses.has_colors():
        return
    curses.start_color()
    try:
        curses.use_default_colors()
        bg = -1
    except curses.error:
        bg = curses.COLOR_BLACK
    curses.init_pair(1, curses.COLOR_GREEN, bg)   # rows
    curses.init_pair(2, curses.COLOR_YELLOW, bg)  # title
    curses.init_pair(3, curses.COLOR_CYAN, bg)    # section headers
    curses.init_pair(4, curses.COLOR_BLACK, curses.COLOR_GREEN)  # cursor bar


def _attr(pair: int, extra: int = 0) -> int:
    import curses

    return extra | (curses.color_pair(pair) if curses.has_colors() else 0)


def _draw_header(stdscr, title: str, desc_lines: list[str], hint: str, max_y: int, max_x: int) -> int:
    import curses

    row = 0
    try:
        stdscr.addnstr(row, 0, title, max_x - 1, _attr(2, curses.A_BOLD))
        row += 1
        for line in desc_lines:
            if row >= max_y - 3:
                break
            stdscr.addnstr(row, 0, line, max_x - 1, curses.A_NORMAL)
            row += 1
        stdscr.addnstr(row, 0, hint, max_x - 1, curses.A_DIM)
        row += 1
    except curses.error:
        pass
    return row + 1


def _draw_row(stdscr, y: int, text: str, *, header: bool, cursor: bool, max_x: int) -> None:
    import curses

    if header:
        attr = _attr(3, curses.A_BOLD)
    elif cursor:
        attr = _attr(4, curses.A_BOLD) if curses.has_colors() else curses.A_REVERSE
    else:
        attr = _attr(1)
    try:
        stdscr.addnstr(y, 0, text.ljust(max_x - 1) if cursor else text, max_x - 1, attr)
    except curses.error:
        pass


def _read_key(stdscr) -> tuple[str, int | None]:
    """(action, extra): extra is a digit for NAV_NONE+digit, or a screen row
    for a tap."""
    import curses

    key = stdscr.getch()
    if key in (curses.KEY_UP, ord("k")):
        return NAV_UP, None
    if key in (curses.KEY_DOWN, ord("j")):
        return NAV_DOWN, None
    if key == curses.KEY_PPAGE:
        return NAV_PGUP, None
    if key == curses.KEY_NPAGE:
        return NAV_PGDN, None
    if key == curses.KEY_HOME:
        return NAV_HOME, None
    if key == curses.KEY_END:
        return NAV_END, None
    if key in (curses.KEY_ENTER, 10, 13, curses.KEY_RIGHT):
        return NAV_SELECT, None
    if key == ord(" "):
        return NAV_TOGGLE, None
    if key in (ord("q"), curses.KEY_LEFT, curses.KEY_BACKSPACE, 127, 8):
        return NAV_CANCEL, None
    if ord("0") <= key <= ord("9"):
        return "digit", key - ord("0")
    if key == curses.KEY_MOUSE:
        try:
            _id, _x, y, _z, bstate = curses.getmouse()
        except curses.error:
            return NAV_NONE, None
        if bstate & getattr(curses, "BUTTON4_PRESSED", 0):
            return NAV_UP, None
        if bstate & getattr(curses, "BUTTON5_PRESSED", 0):
            return NAV_DOWN, None
        return "tap", y
    if key == 27:
        stdscr.timeout(60)
        try:
            nxt = stdscr.getch()
            if nxt == -1:
                return NAV_CANCEL, None
            if nxt in (ord("["), ord("O")):
                final = stdscr.getch()
                if final == ord("A"):
                    return NAV_UP, None
                if final == ord("B"):
                    return NAV_DOWN, None
                if final == ord("C"):
                    return NAV_SELECT, None
                if final == ord("D"):
                    return NAV_CANCEL, None
            return NAV_NONE, None
        finally:
            stdscr.timeout(-1)
    # KEY_RESIZE, stray letters, function keys: redraw and carry on.
    return NAV_NONE, None


def _run_menu(
    *,
    title: str,
    items: Sequence[str],
    description: str,
    hint: str,
    initial: int,
    render: Callable[[int, bool], str],
    on_action: Callable[[str, int], object],
    cancel_value,
    tap_action: str = NAV_SELECT,
):
    """Shared curses loop. ``render(i, is_cursor)`` gives a row's text;
    ``on_action(action, i)`` returns a result or _KEEP to stay open; a tap
    on a row moves there and sends ``tap_action``."""
    import curses

    selectable = _selectable(items)
    if not selectable:
        return cancel_value
    holder: list = [cancel_value]
    desc_lines = description.splitlines() if description else []
    numbers = {row: n for n, row in enumerate(selectable, 1)}
    by_number = {n: row for row, n in numbers.items()}

    def _loop(stdscr):
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        try:
            # curses waits ESCDELAY (1s) after Esc for an escape sequence;
            # arrow keys arrive in well under 25ms, so Esc can be snappy.
            curses.set_escdelay(25)
        except Exception:
            pass
        _init_colors()
        try:
            curses.mousemask(curses.ALL_MOUSE_EVENTS)
            curses.mouseinterval(0)
        except Exception:
            pass
        cursor = initial if initial in selectable else selectable[0]
        scroll = 0
        digits = ""
        digits_at = 0.0
        stdscr.clear()  # first frame repaints everything, not just changes
        while True:
            stdscr.erase()
            max_y, max_x = stdscr.getmaxyx()
            start = _draw_header(stdscr, title, desc_lines, hint, max_y, max_x)
            visible = max(1, max_y - start - 1)
            # Keep the cursor's section header in view when scrolling up.
            top = cursor - 1 if cursor > 0 and isinstance(items[cursor - 1], Header) else cursor
            if top < scroll:
                scroll = top
            elif cursor >= scroll + visible:
                scroll = cursor - visible + 1
            scroll = max(0, min(scroll, max(0, len(items) - visible)))
            shown = range(scroll, min(len(items), scroll + visible))
            for row_i, idx in enumerate(shown):
                _draw_row(stdscr, start + row_i, render(idx, idx == cursor),
                          header=isinstance(items[idx], Header), cursor=idx == cursor, max_x=max_x)
            if scroll > 0 or shown.stop < len(items):
                more = f" {shown.start + 1}-{shown.stop} of {len(items)} "
                try:
                    stdscr.addnstr(max_y - 1, max(0, max_x - len(more) - 1), more, max_x - 1, curses.A_DIM)
                except curses.error:
                    pass
            stdscr.refresh()

            action, extra = _read_key(stdscr)
            pos = selectable.index(cursor)
            if action == NAV_UP:
                cursor = selectable[(pos - 1) % len(selectable)]
            elif action == NAV_DOWN:
                cursor = selectable[(pos + 1) % len(selectable)]
            elif action in (NAV_PGUP, NAV_PGDN):
                step = max(1, visible - 2) * (-1 if action == NAV_PGUP else 1)
                target = min(max(cursor + step, 0), len(items) - 1)
                after = [i for i in selectable if (i >= target if step > 0 else i <= target)]
                cursor = (after[0] if step > 0 else after[-1]) if after else cursor
            elif action == NAV_HOME:
                cursor = selectable[0]
            elif action == NAV_END:
                cursor = selectable[-1]
            elif action == "digit":
                now = time.monotonic()
                digits = (digits if now - digits_at < 1.0 else "") + str(extra)
                digits_at = now
                if int(digits) in by_number:
                    cursor = by_number[int(digits)]
                elif int(str(extra)) in by_number:
                    digits = str(extra)
                    cursor = by_number[extra]
            elif action == "tap":
                idx = scroll + (extra - start) if extra is not None else -1
                if idx in shown and idx in numbers:
                    cursor = idx
                    out = on_action(tap_action, cursor)
                    if out is not _KEEP:
                        holder[0] = out
                        return
            elif action in (NAV_SELECT, NAV_TOGGLE, NAV_CANCEL):
                out = on_action(action, cursor)
                if out is not _KEEP:
                    holder[0] = out
                    return

    try:
        curses.wrapper(_loop)
    except KeyboardInterrupt:
        holder[0] = cancel_value
    flush_stdin()
    return holder[0]


def _use_curses() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


# --- public menus --------------------------------------------------------


def radiolist(
    title: str,
    items: list[str],
    *,
    selected: int = 0,
    description: str = "",
    cancel_returns: int | None = None,
    marks: bool = True,
) -> int:
    """Pick one row; returns its index, or ``cancel_returns`` (-1) on back.

    ``selected`` is the saved value: marked ``(o)`` and where the cursor
    starts. ``marks=False`` drops the radio column for action menus.
    """
    cancel = -1 if cancel_returns is None else cancel_returns
    if not items:
        return cancel
    flush_stdin()
    nums = _numbers(items)
    width = len(str(len(nums)))

    def _radio_row(i: int, is_cursor: bool) -> str:
        if isinstance(items[i], Header):
            return f" {items[i]}"
        arrow = ">" if is_cursor else " "
        radio = ("(o) " if i == selected else "( ) ") if marks else ""
        return f" {arrow} {str(nums[i]).rjust(width)}. {radio}{items[i]}"

    def on_action(action: str, cursor: int):
        if action == NAV_CANCEL:
            return cancel
        return cursor

    if not _use_curses():
        return _radio_numbered_fallback(title, items, selected, cancel, description)
    try:
        return _run_menu(
            title=title, items=items, description=description,
            hint=" Up/Down move | Enter pick | Esc back | tap or type a number",
            initial=selected, render=_radio_row, on_action=on_action, cancel_value=cancel,
        )
    except Exception:
        flush_stdin()
        return _radio_numbered_fallback(title, items, selected, cancel, description)


def menu(title: str, items: list[str], *, selected: int = 0, description: str = "") -> int:
    """An action menu: no radio marks, -1 on back."""
    return radiolist(title, items, selected=selected, description=description, marks=False)


def checkbox(
    title: str,
    items: list[str],
    *,
    selected: set[int] | None = None,
    description: str = "",
    cancel_returns: set[int] | None = None,
) -> set[int]:
    """Toggle rows with Space (or a tap); Enter or the Done row saves. Back
    returns ``cancel_returns`` (default: the starting selection). Use
    :func:`checkbox_or_none` to tell "backed out" from "saved unchanged"."""
    chosen = set(selected or [])
    cancel = set(chosen) if cancel_returns is None else cancel_returns
    if not items:
        return cancel
    flush_stdin()
    nums = _numbers(items)
    width = len(str(len(nums)))
    confirm_row = len(items)
    rows = list(items) + ["Done"]

    def _check_row(i: int, is_cursor: bool) -> str:
        if i < len(items) and isinstance(items[i], Header):
            return f" {items[i]}"
        arrow = ">" if is_cursor else " "
        if i == confirm_row:
            return f" {arrow} {' ' * width}  -> Done (save)"
        mark = "[x]" if i in chosen else "[ ]"
        return f" {arrow} {str(nums[i]).rjust(width)}. {mark} {items[i]}"

    def on_action(action: str, cursor: int):
        if action == NAV_CANCEL:
            return cancel
        if action == NAV_SELECT or cursor == confirm_row:
            return set(chosen)
        chosen.symmetric_difference_update({cursor})
        return _KEEP

    if not _use_curses():
        return _checkbox_numbered_fallback(title, items, chosen, cancel, description)
    try:
        return _run_menu(
            title=title, items=rows, description=description,
            hint=" Space/tap toggle | Enter save | Esc back",
            initial=0, render=_check_row, on_action=on_action, cancel_value=cancel,
            tap_action=NAV_TOGGLE,
        )
    except Exception:
        flush_stdin()
        return _checkbox_numbered_fallback(title, items, chosen, cancel, description)


def checkbox_or_none(title: str, items: list[str], *, selected: set[int] | None = None,
                     description: str = "") -> set[int] | None:
    """Like :func:`checkbox` but returns None when the user backs out, so
    callers don't write anything."""
    sentinel: set[int] = {-999}
    got = checkbox(title, items, selected=selected, description=description, cancel_returns=sentinel)
    return None if got is sentinel else got


# --- numbered fallbacks (no TTY) ----------------------------------------


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
    nums = _numbers(items)
    for i, label in enumerate(items):
        if isinstance(label, Header):
            print(f"  {label}")
            continue
        mark = "*" if i == selected else " "
        print(f"  {mark} {nums[i]}. {label}")
    by_number = {n: row for row, n in nums.items()}
    default_n = nums.get(selected, 1)
    try:
        val = input(f"Choice [1-{len(nums)}, Enter={default_n}, q=back]: ").strip()
    except (KeyboardInterrupt, EOFError):
        return cancel_returns
    if val.lower() in ("q", "esc", "b"):
        return cancel_returns
    if not val:
        return selected if selected in nums else by_number[1]
    try:
        return by_number[int(val)]
    except (ValueError, KeyError):
        return cancel_returns


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
    nums = _numbers(items)
    by_number = {n: row for row, n in nums.items()}
    while True:
        for i, label in enumerate(items):
            if isinstance(label, Header):
                print(f"  {label}")
                continue
            mark = "[x]" if i in picked else "[ ]"
            print(f"  {mark} {nums[i]}. {label}")
        try:
            val = input("Toggle #, Enter=save, q=back: ").strip()
        except (KeyboardInterrupt, EOFError):
            return cancel_returns
        if val.lower() in ("q", "esc", "b"):
            return cancel_returns
        if not val:
            return picked
        try:
            picked.symmetric_difference_update({by_number[int(val)]})
        except (ValueError, KeyError):
            continue
