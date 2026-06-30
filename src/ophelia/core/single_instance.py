"""Single-instance guard for `ophelia run`.

Prevents the duplicate-Telegram-poller problem at its source: if a second
`ophelia run` starts (e.g. a manual one next to a Termux:Boot/tmux one), it
would spin up a second consciousness/dream/curator/mic listener AND fight the
first over the bot token. We take an exclusive flock on
`~/.ophelia/ophelia.run.lock` before any background loop starts; a second
instance sees it held and exits with a helpful message instead.

The lock is held for the process lifetime and released automatically by the OS
on exit (even on crash), so a dead process can never leave it stuck.
"""

from __future__ import annotations

import os

from ophelia.config import OPHELIA_HOME

# fcntl is POSIX-only. On Windows there's typically no `ophelia run` always-on
# use; fail open (allow running) rather than blocking on a missing primitive.
try:
    import fcntl as _fcntl  # type: ignore
except Exception:  # pragma: no cover - Windows / unsupported
    _fcntl = None  # type: ignore

# Sentinel returned when locking is unavailable (fail-open). Not a real fd.
_NO_LOCK = -1


def acquire_run_lock():
    """Try to take the global single-instance lock.

    Returns an opaque token on success (an fd, or _NO_LOCK on platforms
    without fcntl) or ``None`` if another Ophelia instance already holds it.
    The caller should keep the token alive for the process lifetime; do not
    close it (closing releases the lock).
    """
    if _fcntl is None:
        return _NO_LOCK
    try:
        OPHELIA_HOME.mkdir(parents=True, exist_ok=True)
        fd = os.open(OPHELIA_HOME / "ophelia.run.lock", os.O_CREAT | os.O_RDWR, 0o644)
        try:
            _fcntl.flock(fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return None
        return fd
    except Exception:
        # Fail open: a lock hiccup must not prevent Ophelia from running.
        return _NO_LOCK
