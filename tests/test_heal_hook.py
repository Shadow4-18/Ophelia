"""Her self-heal hook: knocks on her script after a check-in, detached,
at most every HEAL_KNOCK_S, never breaking the tick."""

from __future__ import annotations

import stat
import time

from ophelia.mind.consciousness import ConsciousnessLoop


def _loop(script):
    c = ConsciousnessLoop.__new__(ConsciousnessLoop)
    c._heal_script = lambda: script
    return c


def test_knocks_detached_and_not_too_often(tmp_path):
    out = tmp_path / "ran"
    script = tmp_path / "heal_loop.sh"
    script.write_text(f'#!/bin/sh\necho "$1" >> {out}\n')
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    c = _loop(script)
    c._heal_hook()
    c._heal_hook()  # right after: skipped
    for _ in range(50):
        if out.exists():
            break
        time.sleep(0.05)
    assert out.read_text().split() == ["--hook"]
    c._last_heal_knock -= c.HEAL_KNOCK_S + 1
    c._heal_hook()
    for _ in range(50):
        if len(out.read_text().split()) == 2:
            break
        time.sleep(0.05)
    assert out.read_text().split() == ["--hook", "--hook"]


def test_no_script_no_knock(tmp_path):
    c = _loop(tmp_path / "missing.sh")
    c._heal_hook()
    assert c._last_heal_knock == 0.0


def test_failure_is_a_log_line(tmp_path, monkeypatch):
    import subprocess

    script = tmp_path / "heal_loop.sh"
    script.write_text("#!/bin/sh\n")

    def boom(*a, **k):
        raise OSError("no bash")

    monkeypatch.setattr(subprocess, "Popen", boom)
    _loop(script)._heal_hook()  # doesn't raise
