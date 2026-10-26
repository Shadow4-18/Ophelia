"""Battery-whitelist and wake-lock checks read the real Android state
through dumpsys, rish (Shizuku) or su."""

from __future__ import annotations

from types import SimpleNamespace

from ophelia.android import harden


def _fake(monkeypatch, *, have, outputs):
    calls = []
    monkeypatch.setattr(harden.shutil, "which", lambda name: name if name in have else None)

    def run(cmd, **kw):
        calls.append((cmd, kw.get("env")))
        out, err, rc = outputs.get(cmd[0], ("", "", 1))
        return SimpleNamespace(stdout=out, stderr=err, returncode=rc)

    monkeypatch.setattr(harden.subprocess, "run", run)
    return calls


def test_wake_lock_seen_through_rish_stderr(monkeypatch):
    calls = _fake(monkeypatch, have={"rish", "sh"}, outputs={
        "rish": ("", "Wake Locks: size=1\n PARTIAL_WAKE_LOCK 'termux:service-wakelock'", 0)})
    assert harden._check_wake_lock() == (True, "wake-lock held")
    cmd, env = calls[-1]
    assert cmd == ["rish", "-c", "dumpsys power"] and env["RISH_APPLICATION_ID"] == "com.termux"


def test_wake_lock_missing_and_no_shell(monkeypatch):
    _fake(monkeypatch, have={"su"}, outputs={"su": ("Wake Locks: size=0", "", 0)})
    ok, detail = harden._check_wake_lock()
    assert not ok and "termux-wake-lock" in detail
    _fake(monkeypatch, have=set(), outputs={})
    assert harden._check_wake_lock()[0] is True  # can't tell: don't cry wolf


def test_battery_whitelist(monkeypatch):
    _fake(monkeypatch, have={"dumpsys", "sh"},
          outputs={"sh": ("system-excidle,com.termux,10234", "", 0)})
    assert harden._check_battery_optimization()[0] is True
    _fake(monkeypatch, have={"rish"}, outputs={"rish": ("", "com.android.phone", 0)})
    assert harden._check_battery_optimization()[0] is False


async def _one_pass(loop, monkeypatch, checks):
    report = harden.HardenReport(checks=checks)
    monkeypatch.setattr(harden, "check_harden_status", lambda s: report)
    monkeypatch.setattr(harden, "is_termux", lambda: True)

    async def stop(_):
        loop._running = False

    monkeypatch.setattr(harden.asyncio, "sleep", stop)
    await loop.run()


async def test_unrepairable_checks_reported_once_even_across_restarts(tmp_path, monkeypatch):
    from unittest.mock import MagicMock

    repaired = MagicMock()
    monkeypatch.setattr(harden, "apply_harden", repaired)
    logged = []
    monkeypatch.setattr(harden.log, "info", lambda ev, **kw: logged.append(ev))
    monkeypatch.setattr(harden.log, "warning", lambda ev, **kw: logged.append(ev))
    settings = SimpleNamespace(data_dir=tmp_path)
    signals = SimpleNamespace(terminate=False)
    boot = harden.HardenCheck(name="termux_boot_addon", ok=False, detail="missing",
                              fix_hint="install it")
    await _one_pass(harden.HealthCheckLoop(settings, signals), monkeypatch, [boot])
    assert logged.count("health_check.manual") == 1
    assert "health_check.failed" not in logged and not repaired.called
    logged.clear()
    await _one_pass(harden.HealthCheckLoop(settings, signals), monkeypatch, [boot])  # restart
    assert "health_check.manual" not in logged
    lock = harden.HardenCheck(name="wake_lock", ok=False, detail="no lock")
    await _one_pass(harden.HealthCheckLoop(settings, signals), monkeypatch, [boot, lock])
    assert "health_check.failed" in logged and repaired.called  # fixable: still repaired
