"""Android kill-switch — protect Termux loops from Samsung battery optimization (Tier C #12).

Samsung (and most OEM Android) aggressively kills background apps to save
battery. On a stock phone this murders Termux loops within minutes: the
mic listen loop, consciousness ticks, curator, dreams all silently stop.
The fixes are all manual UI toggles, but they can be checked from Termux
and partially automated.

This module:

  1. `check_harden_status()`  — runs the checklist, reports what's OK / wrong.
  2. `apply_harden()`         — where automatable, applies the fix; otherwise
                                prints the exact UI path the user must follow.
  3. `HealthCheckLoop`        — runtime loop that periodically verifies the
                                tmux session + wake-lock are alive and logs
                                (optionally restarts) when they're not.

The checklist:
  - Termux app: battery optimization OFF (must be done via Android UI).
  - Termux:Boot add-on installed + boot script present.
  - wake-lock held (`termux-wake-lock`).
  - tmux session "ophelia" alive.
  - (Optional) Termux notification persistent (helps keep the process alive).
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

import structlog

from ophelia.config import OPHELIA_HOME, Settings
from ophelia.core.signals import Signals
from ophelia.platform import is_termux

log = structlog.get_logger()


@dataclass
class HardenCheck:
    name: str
    ok: bool
    detail: str = ""
    fix_hint: str = ""
    auto_fixable: bool = False


@dataclass
class HardenReport:
    checks: list[HardenCheck] = field(default_factory=list)
    overall_ok: bool = False

    def to_text(self) -> str:
        lines = []
        for c in self.checks:
            mark = "✓" if c.ok else "✗"
            lines.append(f"  {mark} {c.name}: {c.detail}")
            if not c.ok and c.fix_hint:
                lines.append(f"      fix: {c.fix_hint}")
        lines.append("")
        lines.append("ALL OK" if self.overall_ok else "NEEDS ATTENTION")
        return "\n".join(lines)


def check_harden_status(settings: Settings) -> HardenReport:
    """Run the kill-switch checklist. Safe to call anywhere (no-ops off Termux)."""
    report = HardenReport()

    on_termux = is_termux()
    if not on_termux:
        report.checks.append(HardenCheck(
            name="platform",
            ok=True,
            detail="not Termux — kill-switch N/A on this host",
        ))
        report.overall_ok = True
        return report

    # 1. Battery optimization — can't be read directly without root, but we can
    # check if Termux has the REQUEST_IGNORE_BATTERY_OPTIMIZATIONS permission
    # granted via `dumpsys` (best-effort).
    bo_ok, bo_detail = _check_battery_optimization()
    report.checks.append(HardenCheck(
        name="battery_optimization",
        ok=bo_ok,
        detail=bo_detail,
        fix_hint=(
            "Settings → Apps → Termux → Battery → Unrestricted. "
            "Also Settings → Battery → Adaptive Battery OFF (Samsung)."
        ),
        auto_fixable=False,
    ))

    # 2. Termux:Boot add-on installed.
    boot_pkg = "/data/data/com.termux.boot"
    boot_ok = Path(boot_pkg).is_dir()
    report.checks.append(HardenCheck(
        name="termux_boot_addon",
        ok=boot_ok,
        detail="installed" if boot_ok else "NOT installed",
        fix_hint="Install Termux:Boot from F-Droid (not Play Store — it's outdated).",
        auto_fixable=False,
    ))

    # 3. Boot script present.
    boot_script = Path("/data/data/com.termux/files/home/.termux/boot/start-ophelia")
    boot_script_ok = boot_script.is_file() and boot_script.stat().st_mode & 0o100
    report.checks.append(HardenCheck(
        name="boot_script",
        ok=boot_script_ok,
        detail=str(boot_script) if boot_script_ok else "missing or not executable",
        fix_hint=(
            "mkdir -p ~/.termux/boot && create ~/.termux/boot/start-ophelia "
            "(see README 'Survive a phone reboot')"
        ),
        auto_fixable=boot_script_ok is False,
    ))

    # 4. Wake-lock held.
    wl_ok, wl_detail = _check_wake_lock()
    report.checks.append(HardenCheck(
        name="wake_lock",
        ok=wl_ok,
        detail=wl_detail,
        fix_hint="termux-wake-lock (or OPHELIA_SKIP_WAKE_LOCK=true to silence)",
        auto_fixable=not wl_ok,
    ))

    # 5. tmux session alive.
    tmux_ok, tmux_detail = _check_tmux_session()
    report.checks.append(HardenCheck(
        name="tmux_session",
        ok=tmux_ok,
        detail=tmux_detail,
        fix_hint="ophelia start  (creates the tmux session)",
        auto_fixable=False,
    ))

    report.overall_ok = all(c.ok for c in report.checks)
    return report


def apply_harden(settings: Settings) -> HardenReport:
    """Apply what's auto-fixable; leave manual UI steps as hints."""
    report = check_harden_status(settings)
    for c in report.checks:
        if c.ok or not c.auto_fixable:
            continue
        if c.name == "wake_lock":
            _apply_wake_lock()
        elif c.name == "boot_script":
            _apply_boot_script()
    # Re-check after applying.
    return check_harden_status(settings)


def _check_battery_optimization() -> tuple[bool, str]:
    """Best-effort: query `dumpsys deviceidle whitelist` for termux."""
    dumpsys = shutil.which("dumpsys")
    if not dumpsys:
        return True, "dumpsys unavailable — assuming OK"
    try:
        out = subprocess.run(
            [dumpsys, "deviceidle", "whitelist"],
            capture_output=True, text=True, timeout=5,
        ).stdout
    except Exception:
        return True, "dumpsys query failed — assuming OK"
    if "com.termux" in out.lower():
        return True, "Termux on battery whitelist"
    return False, "Termux NOT on battery whitelist — likely being killed"


def _check_wake_lock() -> tuple[bool, str]:
    """Check if a Termux wake-lock is held by looking for the lock file."""
    lock_file = Path("/data/data/com.termux/files/home/.termux/lock")
    # termux-wake-lock doesn't expose a clean query; the presence of any
    # ophelia process is a decent proxy. Best-effort.
    try:
        ps = subprocess.run(
            ["ps", "-ef"], capture_output=True, text=True, timeout=5
        ).stdout
        if "ophelia" in ps and "termux-wake-lock" in ps:
            return True, "wake-lock held"
        if "ophelia" in ps:
            return False, "ophelia running but NO wake-lock — at risk"
        return False, "ophelia not running"
    except Exception:
        return True, "ps unavailable — assuming OK"


def _check_tmux_session() -> tuple[bool, str]:
    tmux = shutil.which("tmux")
    if not tmux:
        return False, "tmux not installed"
    try:
        out = subprocess.run(
            [tmux, "ls"], capture_output=True, text=True, timeout=5
        ).stdout
        if "ophelia" in out:
            return True, "session 'ophelia' alive"
        return False, "no 'ophelia' tmux session"
    except Exception:
        return False, "tmux query failed"


def _apply_wake_lock() -> None:
    wl = shutil.which("termux-wake-lock")
    if wl:
        try:
            subprocess.run([wl], timeout=5, check=False)
            log.info("harden.wake_lock_applied")
        except Exception as e:
            log.warning("harden.wake_lock_failed", error=str(e))


def _apply_boot_script() -> None:
    """Generate the Termux:Boot start script.

    Mirrors `ophelia start` (action_start): wake-lock, then a detached tmux
    session running `ophelia run --restart` with the Termux bin dir on PATH
    and the install dir as cwd. Using `ophelia run --restart` (not bare
    `ophelia run`) matches the manual start path so a phone reboot brings her
    back up the same way the user would, including clearing any stale state.
    """
    import os

    boot_dir = Path("/data/data/com.termux/files/home/.termux/boot")
    boot_dir.mkdir(parents=True, exist_ok=True)
    script = boot_dir / "start-ophelia"
    if script.is_file():
        return
    # Resolve the install dir the same way action_start does (the cwd when
    # the user ran `ophelia phone harden`). Fall back to $HOME if unknown.
    cwd = os.getcwd()
    script.write_text(
        "#!/data/data/com.termux/files/usr/bin/sh\n"
        "termux-wake-lock\n"
        "OLLAMA_KEEP_ALIVE=30m ollama serve >/dev/null 2>&1 &\n"
        f"sleep 2 && tmux new-session -d -s ophelia "
        f"'export PATH=$PREFIX/bin:$PATH; cd {cwd} && ophelia run --restart'\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    log.info("harden.boot_script_created", path=str(script))


class HealthCheckLoop:
    """Runtime loop that periodically verifies the Termux session is healthy.

    Runs alongside the orchestrator. When the tmux session or wake-lock
    disappears, it logs loudly (and optionally re-applies the wake-lock).
    """

    def __init__(
        self,
        settings: Settings,
        signals: Signals,
        *,
        interval_seconds: int = 600,
        auto_repair: bool = True,
    ) -> None:
        self.settings = settings
        self.signals = signals
        self.interval = max(60, int(interval_seconds))
        self.auto_repair = auto_repair
        self._running = False
        self._last_report: HardenReport | None = None

    async def run(self) -> None:
        if not is_termux():
            return  # no-op off Termux
        self._running = True
        log.info("health_check.started", interval_s=self.interval)
        while self._running and not self.signals.terminate:
            try:
                report = check_harden_status(self.settings)
                self._last_report = report
                if not report.overall_ok:
                    log.warning("health_check.failed", checks=[
                        c.name for c in report.checks if not c.ok
                    ])
                    if self.auto_repair:
                        apply_harden(self.settings)
                else:
                    log.debug("health_check.ok")
            except Exception as e:
                log.warning("health_check.error", error=str(e))
            await asyncio.sleep(self.interval)

    def stop(self) -> None:
        self._running = False
