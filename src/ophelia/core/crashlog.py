"""Persistent crash log — survives tmux/terminal death.

Writes to ``~/.ophelia/crash.log`` (follows ``OPHELIA_HOME``) so you can
still read why ``ophelia run`` died after the session pane is gone.
"""

from __future__ import annotations

import os
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ophelia.config import OPHELIA_HOME

CRASH_LOG = OPHELIA_HOME / "crash.log"
FAULT_LOG = OPHELIA_HOME / "crash.fault.log"
_MAX_BYTES = 2 * 1024 * 1024  # rotate when larger
_KEEP_BYTES = 1 * 1024 * 1024


def crash_log_path() -> Path:
    return Path(os.environ.get("OPHELIA_HOME") or OPHELIA_HOME) / "crash.log"


def fault_log_path() -> Path:
    return Path(os.environ.get("OPHELIA_HOME") or OPHELIA_HOME) / "crash.fault.log"


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _rotate_if_needed(path: Path) -> None:
    try:
        if not path.is_file() or path.stat().st_size <= _MAX_BYTES:
            return
        data = path.read_bytes()
        path.write_bytes(data[-_KEEP_BYTES:])
    except OSError:
        pass


def write_crash(
    exc: BaseException | None = None,
    *,
    where: str = "unknown",
    attempt: int | None = None,
    argv: list[str] | None = None,
    extra: dict[str, Any] | None = None,
    tb_text: str | None = None,
) -> Path:
    """Append one crash record. Never raises (logging must not take down exit)."""
    path = crash_log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _rotate_if_needed(path)
        lines: list[str] = [
            "",
            "=" * 72,
            f"time:     {_utc_now()}",
            f"where:    {where}",
            f"pid:      {os.getpid()}",
            f"python:   {sys.version.split()[0]} {sys.platform}",
            f"argv:     {argv if argv is not None else sys.argv!r}",
        ]
        if attempt is not None:
            lines.append(f"attempt:  {attempt}")
        if extra:
            for k, v in extra.items():
                lines.append(f"{k}: {v}")
        if exc is not None:
            lines.append(f"error:    {type(exc).__name__}: {exc}")
        lines.append("-" * 72)
        if tb_text:
            lines.append(tb_text.rstrip() + "\n")
        elif exc is not None:
            lines.append("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
        else:
            lines.append("(no traceback)\n")
        with path.open("a", encoding="utf-8") as f:
            f.write("\n".join(lines))
            if not lines[-1].endswith("\n"):
                f.write("\n")
    except Exception:
        pass
    return path


def install_excepthook() -> None:
    """Capture any uncaught exception outside cmd_run's try block."""
    previous = sys.excepthook

    def _hook(exc_type: type[BaseException], exc: BaseException, tb: Any) -> None:
        try:
            write_crash(
                exc,
                where="sys.excepthook",
                tb_text="".join(traceback.format_exception(exc_type, exc, tb)),
            )
        finally:
            previous(exc_type, exc, tb)

    sys.excepthook = _hook


def install_threading_excepthook() -> None:
    try:
        import threading
    except ImportError:
        return
    if not hasattr(threading, "excepthook"):
        return
    previous = threading.excepthook

    def _hook(args: Any) -> None:
        try:
            write_crash(
                args.exc_value,
                where=f"threading:{getattr(args.thread, 'name', '?')}",
                tb_text="".join(
                    traceback.format_exception(
                        args.exc_type, args.exc_value, args.exc_traceback
                    )
                ),
            )
        finally:
            previous(args)

    threading.excepthook = _hook


def install_faulthandler() -> None:
    """Dump hard crashes (segfault, abort) to crash.fault.log if possible."""
    try:
        import faulthandler
    except ImportError:
        return
    try:
        path = fault_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        # Keep file handle open for the process lifetime (faulthandler requirement)
        fh = path.open("a", encoding="utf-8")
        fh.write(f"\n===== faulthandler enabled {_utc_now()} pid={os.getpid()} =====\n")
        fh.flush()
        faulthandler.enable(file=fh, all_threads=True)
        # Prevent GC closing the handle
        install_faulthandler._fh = fh  # type: ignore[attr-defined]
    except Exception:
        pass


def install_asyncio_handler(loop: Any | None = None) -> None:
    """Log unhandled asyncio task exceptions (won't always kill the process)."""
    import asyncio

    def _handler(loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
        exc = context.get("exception")
        msg = context.get("message", "asyncio error")
        if isinstance(exc, BaseException):
            write_crash(
                exc,
                where="asyncio",
                extra={"asyncio_message": msg},
            )
        else:
            write_crash(
                where="asyncio",
                extra={"asyncio_message": msg, "context": repr(context)[:2000]},
                tb_text=context.get("traceback") or "(no traceback)\n",
            )
        # Still use default logging so console shows it when available
        loop.default_exception_handler(context)

    try:
        target = loop or asyncio.get_running_loop()
        target.set_exception_handler(_handler)
    except RuntimeError:
        # No running loop yet — caller can pass one or call again after create
        pass


def install_all() -> None:
    """Install process-wide crash capture (safe to call multiple times)."""
    install_excepthook()
    install_threading_excepthook()
    install_faulthandler()


def last_crash_excerpt(*, max_chars: int = 8000) -> str:
    """Return the tail of crash.log (last record-ish chunk) for CLI display."""
    path = crash_log_path()
    if not path.is_file():
        return ""
    try:
        data = path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return f"(could not read {path}: {e})"
    if not data.strip():
        return ""
    if len(data) <= max_chars:
        return data
    # Prefer starting at the last record separator
    tail = data[-max_chars:]
    marker = "\n" + ("=" * 72)
    idx = tail.rfind(marker)
    if idx > 0:
        tail = tail[idx + 1 :]
    return tail


def crash_log_summary() -> str | None:
    """One-line summary if a crash log exists (for status)."""
    path = crash_log_path()
    if not path.is_file():
        return None
    try:
        st = path.stat()
        if st.st_size <= 0:
            return None
        age_s = max(0, int(datetime.now(timezone.utc).timestamp() - st.st_mtime))
        if age_s < 120:
            age = f"{age_s}s ago"
        elif age_s < 3600:
            age = f"{age_s // 60}m ago"
        elif age_s < 86400:
            age = f"{age_s // 3600}h ago"
        else:
            age = f"{age_s // 86400}d ago"
        return f"{path} ({st.st_size} bytes, updated {age}) — run: ophelia crash"
    except OSError:
        return None
