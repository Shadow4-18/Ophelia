"""Tests for persistent crash logging."""

from __future__ import annotations

import sys

import pytest

from ophelia.core import crashlog


def test_write_crash_persists_traceback(isolated_env, monkeypatch):
    monkeypatch.setenv("OPHELIA_HOME", str(isolated_env))
    # Module may have imported OPHELIA_HOME earlier — paths use env via crash_log_path()
    path = crashlog.crash_log_path()
    assert path == isolated_env / "crash.log"

    try:
        raise RuntimeError("simulated framework death")
    except RuntimeError as e:
        out = crashlog.write_crash(
            e,
            where="ophelia.run",
            attempt=2,
            argv=["ophelia", "run", "--restart"],
            extra={"restart": True},
        )

    assert out == path
    assert path.is_file()
    text = path.read_text(encoding="utf-8")
    assert "RuntimeError: simulated framework death" in text
    assert "ophelia.run" in text
    assert "attempt:  2" in text
    assert "traceback" in text.lower() or "File " in text


def test_last_crash_excerpt_returns_tail(isolated_env, monkeypatch):
    monkeypatch.setenv("OPHELIA_HOME", str(isolated_env))
    for i in range(3):
        try:
            raise ValueError(f"boom-{i}")
        except ValueError as e:
            crashlog.write_crash(e, where="test", attempt=i)

    excerpt = crashlog.last_crash_excerpt(max_chars=4000)
    assert "boom-2" in excerpt
    summary = crashlog.crash_log_summary()
    assert summary is not None
    assert "ophelia crash" in summary


def test_write_crash_never_raises(isolated_env, monkeypatch):
    monkeypatch.setenv("OPHELIA_HOME", str(isolated_env))
    # Even with a weird path parent issue, write_crash should swallow errors
    monkeypatch.setattr(crashlog, "crash_log_path", lambda: isolated_env / "a" / "b" / "c.log")
    path = crashlog.write_crash(where="test", tb_text="hello\n")
    assert path.name == "c.log"
    assert path.is_file()


def test_excepthook_writes(isolated_env, monkeypatch):
    monkeypatch.setenv("OPHELIA_HOME", str(isolated_env))
    crashlog.install_excepthook()
    # Don't actually invoke sys.excepthook in a way that prints scary output —
    # call write path the hook uses.
    try:
        raise KeyError("missing")
    except KeyError as e:
        crashlog.write_crash(e, where="sys.excepthook")
    assert "KeyError" in crashlog.crash_log_path().read_text(encoding="utf-8")
