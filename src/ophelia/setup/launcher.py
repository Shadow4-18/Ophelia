"""The top-level `ophelia` menu.

Run with `ophelia` (no args) on a TTY, or `ophelia menu`. Groups start/stop,
setup, diagnostics, auth and migration into one menu with a live status
header. The item list is rebuilt after every action, so Start turns into
Stop as soon as she's running.
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

import structlog

from ophelia.config import OPHELIA_HOME, Settings, ensure_dirs
from ophelia.platform import is_termux, platform_summary
from ophelia.setup import tui

log = structlog.get_logger()

HEARTBEAT_PATH = OPHELIA_HOME / "data" / "heartbeat.json"
PAUSE_FLAG = OPHELIA_HOME / "data" / "pause.flag"


def _read_heartbeat() -> dict:
    if not HEARTBEAT_PATH.is_file():
        return {}
    try:
        return json.loads(HEARTBEAT_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _heartbeat_alive() -> bool:
    """True if the heartbeat file exists and was written within 120s."""
    hb = _read_heartbeat()
    if not hb:
        return False
    try:
        return (time.time() - float(hb.get("ts", 0))) < 120
    except (TypeError, ValueError):
        return False


def _is_running() -> bool:
    """True if Ophelia appears to be running right now.

    On Termux this requires BOTH a fresh heartbeat AND an alive tmux session —
    a stale-but-recent heartbeat (left over from a crash) must not trap the
    user out of the Start menu. On other platforms the heartbeat is the only
    signal (the process runs in the foreground).
    """
    if not _heartbeat_alive():
        return False
    if not is_termux():
        return True
    return _tmux_session_active()


def _stale_state() -> bool:
    """True when the heartbeat says running but the tmux session is dead.

    This is the "trapped" state: the menu would show Stop/Restart, but
    action_stop would no-op and Start isn't offered. The launcher exposes a
    cleanup action for this case.
    """
    if not is_termux():
        return False
    return _heartbeat_alive() and not _tmux_session_active()


def _status_line() -> str:
    """One-line live status for the launcher header."""
    hb = _read_heartbeat()
    running = _is_running()
    if not hb:
        return "Status: not running"
    mood = hb.get("mood", "?")
    pressure = hb.get("pressure", "?")
    paused = hb.get("paused", False)
    age = int(time.time() - float(hb.get("ts", 0)))
    if _stale_state():
        state = "stale (tmux dead, heartbeat recent — use Clear stale state)"
    elif paused:
        state = "paused"
    else:
        state = "running" if running else "stale"
    return (
        f"Status: {state} ({age}s ago) | Mood: {mood} | Pressure: {pressure} | "
        f"Channels: {','.join(hb.get('channels', [])) or 'none'}"
    )


def _run_blocking(cmd: list[str], *, cwd: str | None = None) -> tuple[int, str]:
    """Run a subprocess and capture output. Returns (returncode, combined_output)."""
    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=120,
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        return proc.returncode, out
    except Exception as e:
        return 1, str(e)


def _shell_out(script: str) -> None:
    """Print output of a shell command line for the user."""
    print(script)
    tui.pause("Press Enter to continue...")


def _ophelia_cmd(*args: str) -> int:
    """Run an `ophelia <args>` command in a blocking subprocess, streaming output."""
    cmd = [sys.executable, "-m", "ophelia", *args]
    try:
        proc = subprocess.run(cmd)
        return proc.returncode
    except KeyboardInterrupt:
        return 130
    except Exception as e:
        print(f"Error: {e}")
        tui.pause()
        return 1


def _tmux_session_active() -> bool:
    if not is_termux():
        return False
    rc, _ = _run_blocking(["tmux", "has-session", "-t", "ophelia"])
    return rc == 0


# ---------------------------------------------------------------------------
# Action handlers
# ---------------------------------------------------------------------------

def action_start() -> int:
    """Start Ophelia: wake-lock + tmux session + run."""
    if not is_termux():
        print("Start is a Termux convenience (wake-lock + tmux + run).")
        print("On PC, just run: ophelia run")
        tui.pause()
        return 0
    if _tmux_session_active():
        print("Ophelia tmux session already exists. Reattach with:")
        print("  tmux attach -t ophelia")
        tui.pause()
        return 0
    # Acquire wake lock.
    _run_blocking(["termux-wake-lock"])
    print("[ok] Wake lock acquired")
    # Launch in a detached tmux session.
    rc, _ = _run_blocking(
        [
            "tmux", "new-session", "-d", "-s", "ophelia",
            f"export PATH=$PREFIX/bin:$PATH; cd {shlex.quote(str(Path.cwd()))} && ophelia run --restart",
        ]
    )
    if rc == 0:
        print("[ok] Ophelia started in tmux session 'ophelia'")
        print("\nReattach to watch:  tmux attach -t ophelia")
        print("Detach (keep running):  Ctrl+B then D")
        print("Stop:  ophelia stop  (or tmux kill-session -t ophelia)")
    else:
        print("[fail] Failed to start tmux session")
    tui.pause()
    return rc


def action_stop(*, wait: bool = True) -> int:
    if not is_termux():
        print("Stop is a Termux convenience. On PC, Ctrl+C the running process.")
        if wait:
            tui.pause()
        return 0
    if not _tmux_session_active():
        print("No Ophelia tmux session is running.")
        if wait:
            tui.pause()
        return 0
    rc, _ = _run_blocking(["tmux", "kill-session", "-t", "ophelia"])
    _run_blocking(["termux-wake-unlock"])
    print("[ok] Ophelia stopped (tmux session killed, wake lock released)")
    if wait:
        tui.pause()
    return rc


def action_cleanup_stale() -> int:
    """Clear a stale heartbeat / zombie tmux state so Start becomes available.

    Reached from the menu when the heartbeat says running but the tmux session
    is dead — the 'trapped' state where Stop no-ops and Start isn't shown.
    """
    if not is_termux():
        return 0
    # Kill any zombie tmux session (silently — it may already be gone).
    if _tmux_session_active():
        _run_blocking(["tmux", "kill-session", "-t", "ophelia"])
        print("[ok] Killed stale tmux session 'ophelia'")
    # Remove the stale heartbeat so _is_running() returns False.
    if HEARTBEAT_PATH.is_file():
        try:
            HEARTBEAT_PATH.unlink()
            print(f"[ok] Removed stale heartbeat ({HEARTBEAT_PATH.name})")
        except OSError as e:
            print(f"[warn] Could not remove heartbeat: {e}")
    _run_blocking(["termux-wake-unlock"])
    print("[ok] State cleared — starting Ophelia.")
    print()
    return action_start()


def action_restart() -> int:
    action_stop(wait=False)
    print()
    return action_start()


def action_update() -> int:
    """Interactive menu entry: pull + reinstall (+ Termux restart by default)."""
    from ophelia.setup.update import run_update

    print("Updating Ophelia (git pull + pip install -e .)…")
    result = run_update(restart=is_termux(), allow_dirty=False)
    print(result.summary())
    tui.pause()
    return 0 if result.ok else 1


def action_reattach() -> int:
    if not is_termux():
        print("Reattach is a Termux/tmux feature.")
        tui.pause()
        return 0
    if not _tmux_session_active():
        print("No Ophelia tmux session. Start one first.")
        tui.pause()
        return 0
    # Exec into tmux attach — replaces this process.
    os.execvp("tmux", ["tmux", "attach", "-t", "ophelia"])
    return 0


def action_dashboard() -> int:
    from ophelia.setup.dashboard import run_dashboard

    run_dashboard()
    return 0


def action_setup_wizard() -> int:
    from ophelia.setup.interactive import run_interactive_setup

    return run_interactive_setup()


def action_health_check() -> int:
    settings = Settings()
    ensure_dirs(settings)
    from ophelia.diagnostics.self_check import format_report, run_self_check

    chat_only = not is_termux()
    report = asyncio.run(run_self_check(settings, chat_only=chat_only, quick=False))
    print(format_report(report))
    tui.pause()
    return 0


def action_tail_inner() -> int:
    path = OPHELIA_HOME / "data" / "inner_monologue.md"
    if not path.is_file():
        print("No inner monologue yet — Ophelia hasn't reflected.")
        tui.pause()
        return 0
    text = path.read_text(encoding="utf-8")
    lines = text.strip().split("\n")
    print("\n".join(lines[-40:]))
    tui.pause()
    return 0


def action_run_curator() -> int:
    return _ophelia_cmd("curator")


def action_reflect() -> int:
    focus = tui.prompt_text("Reflect on (optional, blank for general):", default="") or ""
    return _ophelia_cmd("reflect", focus) if focus else _ophelia_cmd("reflect")


def action_pause_resume() -> int:
    if _paused():
        PAUSE_FLAG.unlink(missing_ok=True)
        print("[ok] Resume requested — running Ophelia will pick it up.")
    else:
        PAUSE_FLAG.parent.mkdir(parents=True, exist_ok=True)
        PAUSE_FLAG.write_text("1", encoding="utf-8")
        print("[ok] Pause requested — running Ophelia will pick it up.")
    tui.pause()
    return 0


def _paused() -> bool:
    return PAUSE_FLAG.is_file()


def action_auth_status() -> int:
    return _ophelia_cmd("auth", "status")


def action_auth_login() -> int:
    return _ophelia_cmd("auth", "login")


def action_auth_refresh() -> int:
    return _ophelia_cmd("auth", "refresh", "--force")


def action_import_hermes() -> int:
    return _ophelia_cmd("migrate", "hermes")


def action_transfer() -> int:
    if is_termux():
        print("On Termux, you can upload a bundle to a temp cloud link,")
        print("then download on a PC with `ophelia transfer cloud-download <link>`.")
        print()
        return _ophelia_cmd("transfer", "cloud-upload")
    print("On PC, receive a phone upload over Wi-Fi:")
    return _ophelia_cmd("transfer", "receive")


def action_providers() -> int:
    return _ophelia_cmd("providers")


def action_models() -> int:
    return _ophelia_cmd("models")


def action_chat() -> int:
    msg = tui.prompt_text("Message Ophelia:")
    if not msg:
        return 0
    return _ophelia_cmd("chat", msg)


# ---------------------------------------------------------------------------
# Menu structure
# ---------------------------------------------------------------------------

Handler = Callable[[], object]


def _menu_items() -> list[tuple[str, str, Handler]]:
    """(label, group, handler) for the current state, in display order."""
    running = _is_running()
    termux = is_termux()
    items: list[tuple[str, str, Handler]] = []

    # --- Run ---
    if termux:
        if running:
            items.append(("Stop Ophelia", "Run", action_stop))
            items.append(("Restart Ophelia", "Run", action_restart))
            items.append(("Watch her live session (tmux)", "Run", action_reattach))
        elif _stale_state():
            # Heartbeat says running but tmux is dead: Stop would no-op and
            # Start would be hidden, so offer to clear it and start.
            items.append(("Clear stale state and Start (heartbeat/tmux out of sync)",
                          "Run", action_cleanup_stale))
            items.append(("Start Ophelia (wake-lock + tmux + run)", "Run", action_start))
        else:
            items.append(("Start Ophelia (wake-lock + tmux + run)", "Run", action_start))
    else:
        items.append(("Run Ophelia (foreground)", "Run", lambda: _ophelia_cmd("run", "--restart")))
    if running:
        items.append(("Resume her autonomy" if _paused() else "Pause her autonomy",
                      "Run", action_pause_resume))
    items.append(("Live status dashboard", "Run", action_dashboard))
    items.append(("Quick chat (one message)", "Run", action_chat))
    items.append(("Update Ophelia (git pull + reinstall)", "Run", action_update))

    # --- Set up ---
    items.append(("Setup (AI, eyes, images, voice, channels, schedule, ...)", "Set up", action_setup_wizard))
    items.append(("Show provider routing", "Set up", action_providers))
    items.append(("Local Ollama model suggestions", "Set up", action_models))

    # --- Diagnose ---
    items.append(("Health check (doctor)", "Diagnose", action_health_check))
    items.append(("Read her inner monologue", "Diagnose", action_tail_inner))
    items.append(("Run memory curator once", "Diagnose", action_run_curator))
    items.append(("Reflect now (self-reflection)", "Diagnose", action_reflect))

    # --- Auth ---
    items.append(("SuperGrok: show login status", "Auth", action_auth_status))
    items.append(("SuperGrok: fresh login", "Auth", action_auth_login))
    items.append(("SuperGrok: refresh token now", "Auth", action_auth_refresh))

    # --- Move / import ---
    items.append(("Transfer data (phone <-> PC)", "Move / import", action_transfer))
    items.append(("Import from Hermes", "Move / import", action_import_hermes))

    return items


def _render_menu_labels(items: list[tuple[str, str, Handler]]) -> tuple[list[str], list[int]]:
    """Menu rows with section headers, plus row -> item index (-1 = header)."""
    labels: list[str] = []
    index: list[int] = []
    last_group = None
    for i, (label, group, _) in enumerate(items):
        if group != last_group:
            labels.append(tui.Header(f"— {group} —"))
            index.append(-1)
            last_group = group
        labels.append(label)
        index.append(i)
    return labels, index


def _run_launcher() -> int:
    last_label = ""
    while True:
        items = _menu_items()
        labels, index = _render_menu_labels(items)
        selected = next((r for r, lab in enumerate(labels) if lab == last_label), 0)
        desc = f"{platform_summary()}\n{_status_line()}\nHome: {OPHELIA_HOME}"
        choice = tui.menu("Ophelia — what do you want to do?", labels,
                          selected=selected, description=desc)
        if choice < 0 or choice >= len(labels) or index[choice] < 0:
            return 0
        label, _, handler = items[index[choice]]
        last_label = label
        try:
            handler()
        except KeyboardInterrupt:
            print()
        except Exception as e:
            log.exception("launcher.action_failed")
            print(f"Action failed: {e}")
            tui.pause()


def run_launcher() -> int:
    """Entry point for `ophelia` (no args) or `ophelia menu`."""
    ensure_dirs(Settings())
    try:
        return _run_launcher()
    except KeyboardInterrupt:
        print()
        return 0
