from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

import structlog

from ophelia.config import OPHELIA_HOME, Settings, ensure_dirs
from ophelia.core.orchestrator import Orchestrator
from ophelia.migration.hermes import format_report, migrate_from_hermes
from ophelia.providers.auth import (
    import_hermes_auth_full,
    save_oauth_token,
    token_from_grok_cli,
)
from ophelia.providers.oauth_refresh import load_oauth_state
from ophelia.providers.router import ROLE_ENV, build_provider_stack

structlog.configure(
    processors=[
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.dev.ConsoleRenderer(),
    ]
)


def cmd_ui(args: argparse.Namespace) -> int:
    settings = Settings()
    ensure_dirs(settings)
    from ophelia.ui.server import run_ui

    open_browser = not args.no_browser
    try:
        asyncio.run(run_ui(settings, open_browser=open_browser))
    except KeyboardInterrupt:
        return 0
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    settings = Settings()
    ensure_dirs(settings)

    # Single-instance guard: refuse to start a second `ophelia run` next to an
    # already-running one (e.g. a Termux:Boot/tmux instance). Two instances
    # fight over the Telegram bot token AND double-run consciousness/dream/
    # curator/mic. The lock auto-releases when this process exits.
    from ophelia.core.single_instance import acquire_run_lock

    lock = acquire_run_lock()
    if lock is None:
        print("Ophelia is already running.")
        print("  View the live session:  tmux attach -t ophelia")
        print("  Stop it:                 ophelia stop   (or: tmux kill-server; pkill -f ophelia)")
        print("  Then start fresh:        ophelia run")
        print()
        print("If you're sure nothing is running, remove the stale lock:")
        print(f"  rm {OPHELIA_HOME / 'ophelia.run.lock'}")
        return 0

    restart = getattr(args, "restart", False)
    max_restarts = 5 if restart else 0
    attempts = 0
    while True:
        orch = Orchestrator(settings)
        try:
            asyncio.run(orch.start())
            return 0
        except KeyboardInterrupt:
            return 0
        except Exception as e:
            attempts += 1
            log = structlog.get_logger()
            log.exception("ophelia.crashed", attempt=attempts, error=str(e))
            if attempts > max_restarts:
                print(f"ophelia crashed {attempts} times; giving up. Last error: {e}", file=sys.stderr)
                return 1
            print(f"ophelia crashed (attempt {attempts}): {e}. Restarting in 5s...", file=sys.stderr)
            import time as _t

            _t.sleep(5)


def cmd_auth_import_grok(_: argparse.Namespace) -> int:
    settings = Settings()
    token = token_from_grok_cli(settings.grok_cli_auth_path)
    if not token:
        print(f"No token in {settings.grok_cli_auth_path}. Run `grok login` first.")
        return 1
    save_oauth_token(settings.xai_oauth_token_path, token)
    print(f"Imported -> {settings.xai_oauth_token_path}")
    return 0


def cmd_auth_import_hermes(args: argparse.Namespace) -> int:
    from ophelia.providers.auth import sync_oauth_from_hermes_home

    settings = Settings()
    hermes = Path(args.hermes_home).expanduser()
    ok, msg = sync_oauth_from_hermes_home(
        hermes,
        ophelia_auth_path=settings.hermes_auth_path,
        ophelia_oauth_path=settings.xai_oauth_token_path,
    )
    print(msg)
    if ok:
        print(f"  {settings.hermes_auth_path}")
        return 0
    return 1


def cmd_auth_login(_: argparse.Namespace) -> int:
    """Fresh xAI browser login via Hermes CLI, then import into Ophelia."""
    import shutil

    from ophelia.platform import is_termux
    from ophelia.providers.auth import (
        print_termux_oauth_login_help,
        run_hermes_xai_oauth_login,
        sync_oauth_from_hermes_home,
    )
    from ophelia.providers.oauth_refresh import access_token_usable, load_oauth_state

    settings = Settings()
    print()
    print("SuperGrok OAuth comes from xAI (accounts.x.ai) — not Hermes-specific.")
    print("Hermes runs the browser login; Ophelia imports the same token.")
    print()
    if is_termux():
        print_termux_oauth_login_help()
        print()

    if shutil.which("hermes"):
        print("Starting Hermes xAI OAuth login...\n")
        if run_hermes_xai_oauth_login() != 0:
            print("Hermes login cancelled or failed.")
            return 1
    else:
        print("Hermes CLI not in PATH. In another Termux tab run:")
        print("  hermes auth add xai-oauth --type oauth --no-browser")
        print()
        try:
            input("Press Enter after browser login finishes... ")
        except (KeyboardInterrupt, EOFError):
            print()
            return 1

    ok, msg = sync_oauth_from_hermes_home(
        settings.hermes_home,
        ophelia_auth_path=settings.hermes_auth_path,
        ophelia_oauth_path=settings.xai_oauth_token_path,
    )
    print(msg)
    if not ok:
        return 1

    auth = settings.hermes_home / "auth.json"
    state = load_oauth_state(auth)
    if state and not access_token_usable(state["access_token"]):
        print()
        print("WARNING: Login ran but access token is still expired.")
        print("The browser callback probably did not reach Hermes.")
        print("  hermes auth logout xai-oauth")
        print("  hermes auth add xai-oauth --type oauth --no-browser")
        print("  ophelia auth import-hermes")
        return 1

    print()
    print("Next: ophelia auth refresh --force")
    print("       ophelia check")
    return 0


def cmd_auth_status(_: argparse.Namespace) -> int:
    from ophelia.providers.oauth_refresh import describe_oauth_paths

    settings = Settings()
    print()
    for line in describe_oauth_paths(
        hermes_home=settings.hermes_home,
        hermes_auth_path=settings.hermes_auth_path,
        oauth_path=settings.xai_oauth_token_path,
    ):
        print(line)
    print()
    print("HTTP 400 = dead refresh token, or Ophelia read a stale Hermes credential.")
    print("Fix: hermes auth logout xai-oauth  then  ophelia auth login")
    return 0


def cmd_auth_refresh(args: argparse.Namespace) -> int:
    settings = Settings()
    from ophelia.providers.oauth_refresh import (
        describe_oauth_paths,
        ensure_fresh_token,
        resolve_oauth_auth_path,
    )

    path = resolve_oauth_auth_path(
        hermes_home=settings.hermes_home,
        hermes_auth_path=settings.hermes_auth_path,
        oauth_path=settings.xai_oauth_token_path,
    )
    if not path:
        print("No OAuth auth file found. Run: ophelia auth login")
        return 1
    if args.verbose:
        print(f"Using auth file: {path}")
        for line in describe_oauth_paths(
            hermes_home=settings.hermes_home,
            hermes_auth_path=settings.hermes_auth_path,
            oauth_path=settings.xai_oauth_token_path,
        ):
            print(f"  {line}")
    try:
        token = asyncio.run(ensure_fresh_token(path, force=args.force))
        print(f"OAuth refreshed OK ({len(token)} char token)")
        return 0
    except Exception as e:
        print(f"Refresh failed: {e}")
        return 1


def cmd_auth_set_token(args: argparse.Namespace) -> int:
    settings = Settings()
    save_oauth_token(settings.xai_oauth_token_path, args.token)
    print(f"Saved -> {settings.xai_oauth_token_path}")
    return 0


def cmd_migrate_hermes(args: argparse.Namespace) -> int:
    settings = Settings()
    hermes = Path(args.source or settings.hermes_home)
    report = migrate_from_hermes(hermes, dry_run=args.dry_run)
    print(format_report(report))
    if not args.dry_run:
        print(f"\nReport: {OPHELIA_HOME / 'migration_report.json'}")
        print("Next: ophelia auth login  (or import-hermes if Hermes already logged in)")
        print("       merge from-hermes.env into ~/.ophelia/.env")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    from ophelia.diagnostics.self_check import format_report, run_self_check

    settings = Settings()
    ensure_dirs(settings)

    async def _run():
        return await run_self_check(
            settings,
            chat_only=getattr(args, "chat_only", False),
            quick=getattr(args, "quick", False),
        )

    report = asyncio.run(_run())
    print(format_report(report, verbose=getattr(args, "verbose", False)))
    return 0 if report.ok else 1


def cmd_check(args: argparse.Namespace) -> int:
    """Alias for doctor — full install/runtime self-check."""
    return cmd_doctor(args)


def cmd_providers(_: argparse.Namespace) -> int:
    settings = Settings()
    stack = build_provider_stack(settings)
    print(settings.runtime_line())
    print()
    print(stack.describe())
    print()
    print("Supported providers: xai-oauth, xai, ollama, openai, compat, auto")
    print("Per-role overrides: OPHELIA_PROVIDER_CHAT, _CONSCIOUSNESS, _VISION, _CURATOR, _IMAGE, _VIDEO")
    return 0


def cmd_curator_run(_: argparse.Namespace) -> int:
    async def _once() -> None:
        settings = Settings()
        ensure_dirs(settings)
        from ophelia.memory.curator import MemoryCurator
        from ophelia.memory.store import MemoryStore

        mem = MemoryStore(settings.memory_db)
        await mem.init()
        c = MemoryCurator(settings, mem)
        n = await c.run_cycle()
        print(f"Curator added {n} memory fact(s) -> {OPHELIA_HOME / 'memories' / 'MEMORY.md'}")

    asyncio.run(_once())
    return 0


def _control_file(name: str) -> Path:
    return OPHELIA_HOME / "data" / f"{name}.flag"


def cmd_status(_: argparse.Namespace) -> int:
    """Show Ophelia's current autonomous state from the heartbeat file."""
    hb = OPHELIA_HOME / "data" / "heartbeat.json"
    if not hb.is_file():
        print("No heartbeat — Ophelia is not running (or hasn't written one yet).")
        return 1
    import json

    try:
        data = json.loads(hb.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"Could not read heartbeat: {e}")
        return 1
    import time as _t

    age = _t.time() - float(data.get("ts", 0))
    lines = [
        f"Ophelia status (heartbeat {age:.0f}s old):",
        f"  running: {age < 120}",
        f"  paused:  {data.get('paused', False)}",
        f"  mood:    {data.get('mood', '?')} (v={data.get('valence', '?')}, a={data.get('arousal', '?')})",
        f"  drives:  {data.get('drives', '?')}",
        f"  pressure: {data.get('pressure', '?')}",
        f"  last_user_msg: {data.get('last_user_msg_ago', '?')}s ago",
        f"  channels: {data.get('channels', [])}",
        f"  consciousness: {data.get('consciousness', False)}  dream: {data.get('dream', False)}",
    ]
    print("\n".join(lines))
    return 0


def cmd_pause(_: argparse.Namespace) -> int:
    _control_file("pause").parent.mkdir(parents=True, exist_ok=True)
    _control_file("pause").write_text("1", encoding="utf-8")
    print("Pause requested. Running Ophelia will pick it up within a tick.")
    return 0


def cmd_resume(_: argparse.Namespace) -> int:
    _control_file("pause").unlink(missing_ok=True)
    print("Resume requested. Running Ophelia will pick it up within a tick.")
    return 0


def cmd_reflect(args: argparse.Namespace) -> int:
    """Trigger one deliberate reflection cycle immediately."""
    settings = Settings()
    ensure_dirs(settings)

    async def _run() -> int:
        from ophelia.core.agent_loop import AgentLoop
        from ophelia.memory.store import MemoryStore
        from ophelia.mind.psyche import PsycheState
        from ophelia.mind.drives import DriveState
        from ophelia.mind.inner_log import InnerMonologue
        from ophelia.tools.registry import ToolRegistry

        mem = MemoryStore(settings.memory_db)
        await mem.init()
        artifacts = settings.data_dir / "artifacts"
        tools = ToolRegistry(settings, artifacts, memory=mem, psyche=PsycheState())
        drives = DriveState()
        agent = AgentLoop(settings, mem, tools, PsycheState(), drives=drives)
        inner = InnerMonologue() if settings.inner_log_enabled else None
        tools.inner = inner
        result = await tools._reflect(focus=getattr(args, "focus", "") or "")
        print(result)
        return 0

    return asyncio.run(_run())


def cmd_inner_tail(args: argparse.Namespace) -> int:
    from ophelia.mind.inner_log import InnerMonologue

    n = int(args.lines)
    print(InnerMonologue().tail(n))
    return 0


def cmd_chat(args: argparse.Namespace) -> int:
    async def _once() -> None:
        settings = Settings()
        ensure_dirs(settings)
        from ophelia.core.agent_loop import AgentLoop
        from ophelia.memory.store import MemoryStore
        from ophelia.mind.psyche import PsycheState
        from ophelia.providers.router import build_provider_stack
        from ophelia.tools.registry import ToolRegistry

        mem = MemoryStore(settings.memory_db)
        await mem.init()
        psyche = await mem.load_psyche()
        stack = build_provider_stack(settings)
        from ophelia.android.factory import build_android_body

        android = build_android_body(settings)
        tools = ToolRegistry(
            settings, settings.data_dir / "artifacts", stack=stack, android=android
        )
        agent = AgentLoop(
            settings,
            mem,
            tools,
            psyche,
            stack=stack,
            drives=await mem.load_drives(),
        )
        print(await agent.run_turn("cli", args.message))

    asyncio.run(_once())
    return 0


def cmd_models(_: argparse.Namespace) -> int:
    from ophelia.providers.cookbook import detect_system, format_cookbook, list_ollama_models

    settings = Settings()

    async def _once() -> None:
        profile = detect_system()
        installed = await list_ollama_models(settings)
        print(format_cookbook(settings, profile, installed))

    asyncio.run(_once())
    return 0


def cmd_setup(args: argparse.Namespace) -> int:
    from ophelia.setup.wizard import run_setup_wizard

    phone = True if args.phone else False if args.pc else None
    return run_setup_wizard(
        phone=phone,
        interactive=args.interactive,
        checklist=args.checklist,
        do_auto=args.do_auto,
        step_num=args.step,
    )


def cmd_transfer_receive(args: argparse.Namespace) -> int:
    from ophelia.transfer.receive import run_receive_server

    dest = Path(args.dest).expanduser()
    try:
        asyncio.run(
            run_receive_server(
                host=args.host,
                port=args.port,
                token=args.token,
                dest_dir=dest,
                auto_import=not args.no_import,
            )
        )
    except KeyboardInterrupt:
        pass
    return 0


def cmd_transfer_send(args: argparse.Namespace) -> int:
    from ophelia.transfer.send import send_bundle

    hermes = Path(args.hermes_home).expanduser()
    asyncio.run(
        send_bundle(
            args.url,
            hermes_home=hermes,
            token=args.token,
        )
    )
    return 0


def cmd_transfer_cloud_upload(args: argparse.Namespace) -> int:
    from ophelia.transfer.bundle import create_hermes_bundle
    from ophelia.transfer.cloud import cloud_upload

    hermes = Path(args.hermes_home).expanduser()

    async def _once() -> None:
        bundle = create_hermes_bundle(hermes)
        print(f"Packing {bundle} ({bundle.stat().st_size // 1024} KB)...")
        link = await cloud_upload(bundle)
        print()
        print("Free temp cloud link (~14 days, one HTTPS URL):")
        print(link)
        print()
        print("On PC:")
        print(f"  ophelia transfer cloud-download \"{link}\"")

    asyncio.run(_once())
    return 0


def cmd_transfer_cloud_download(args: argparse.Namespace) -> int:
    from ophelia.transfer.cloud import cloud_download
    from ophelia.transfer.import_bundle import import_bundle

    dest = Path(args.output or OPHELIA_HOME / "data" / "hermes-download.tar.gz")

    async def _once() -> None:
        print(f"Downloading to {dest}...")
        await cloud_download(args.url, dest)
        if args.no_import:
            print(f"Saved: {dest}")
        else:
            print(import_bundle(dest))

    asyncio.run(_once())
    return 0


def cmd_start(_: argparse.Namespace) -> int:
    from ophelia.setup.launcher import action_start

    return action_start()


def cmd_stop(_: argparse.Namespace) -> int:
    from ophelia.setup.launcher import action_stop

    return action_stop()


def cmd_restart(_: argparse.Namespace) -> int:
    from ophelia.setup.launcher import action_restart

    return action_restart()


def cmd_dashboard(_: argparse.Namespace) -> int:
    from ophelia.setup.dashboard import run_dashboard

    run_dashboard()
    return 0


def cmd_menu(_: argparse.Namespace) -> int:
    from ophelia.setup.launcher import run_launcher

    return run_launcher()


def cmd_phone_calibrate(_: argparse.Namespace) -> int:
    """Diagnose + calibrate touch input: reports native display size vs
    screenshot pixel size, saves a grid-annotated screenshot, and taps the four
    corners + center so the user can verify (with Pointer Location on) where
    taps actually land."""
    import asyncio

    from ophelia.android.factory import build_android_body
    from ophelia.android.vision import annotate_screenshot_file, png_size

    settings = Settings()
    ensure_dirs(settings)
    android = build_android_body(settings)
    if not android:
        print("Phone body disabled. Set OPHELIA_ANDROID_ENABLED=true (Termux) or")
        print("OPHELIA_ADB_DEVICE=ip:5555 (PC -> phone wireless debugging).")
        return 1
    if android.mode == "termux_only":
        print("No Shizuku/ADB path available. Start Shizuku on the phone or connect ADB.")
        print(f"  mode: {android.mode}")
        return 1

    async def _run() -> int:
        await android.ensure_ready() if hasattr(android, "ensure_ready") else None
        native = await android.display_size()
        print()
        print("== Touch calibration ==")
        if native:
            print(f"Native display (wm size): {native[0]} x {native[1]} px")
        else:
            print("Native display: unknown (wm size + dumpsys display both failed).")
            print("  Check Shizuku is running / ADB is connected.")

        shots = settings.data_dir / "screenshots"
        shots.mkdir(parents=True, exist_ok=True)
        raw = shots / f"calibrate_{int(__import__('time').time())}.png"
        res = await android.screenshot_path(raw)
        if not raw.is_file():
            print(f"\nScreenshot failed: {res}")
            return 1
        shot_px = png_size(raw.read_bytes())
        if shot_px:
            print(f"Screenshot pixels:        {shot_px[0]} x {shot_px[1]} px")
        if native and shot_px:
            sx, sy = shot_px[0] / native[0], shot_px[1] / native[1]
            print(f"Screenshot/native scale:  {sx:.3f} x {sy:.3f}"
                  + ("  (== 1.0, screencap is native)" if abs(sx - 1) < 1e-3 and abs(sy - 1) < 1e-3 else "  (differs!)"))
            if abs(sx - 1) > 1e-3 or abs(sy - 1) > 1e-3:
                print("  NOTE: screencap is not native. Tap coords from the screenshot")
                print("  must be scaled by the above factor — the grid overlay handles this.")
        else:
            print("Screenshot pixels:        unreadable")

        # If both shell queries failed, fall back to the screenshot size so the
        # live tap test can still run. On most phones screencap == native, so
        # this is a reasonable stand-in and unblocks calibration diagnosis.
        used_fallback = False
        if not native and shot_px:
            native = shot_px
            used_fallback = True
            print(f"Native display: using screenshot size as stand-in ({native[0]}x{native[1]}).")
            print("  ASSUMES screencap == native. If taps below are off, this assumption is wrong.")

        annotated = raw.with_name("calibrate_grid.png")
        if annotate_screenshot_file(raw, annotated, native or shot_px):
            print(f"\nGrid-annotated screenshot saved: {annotated}")
            print("Open it to see the native-pixel coordinate labels she reads.")

        print()
        print("== Live tap test ==")
        print("Tip: enable Developer Options -> Pointer location on the phone to see")
        print("     exactly where each tap lands (a crosshair + x,y shows on touch).")
        if not native:
            print("\n(skipping taps — native size unknown)")
            return 0
        nw, nh = native
        targets = [
            ("top-left",     10, 10),
            ("top-right",    nw - 10, 10),
            ("center",       nw // 2, nh // 2),
            ("bottom-left",  10, nh - 10),
            ("bottom-right", nw - 10, nh - 10),
        ]
        for name, tx, ty in targets:
            print(f"  tapping {name:12} at ({tx:4d}, {ty:4d}) ... ", end="", flush=True)
            try:
                r = await android.tap(tx, ty)
                print(r.splitlines()[0][:60] if r else "ok")
            except Exception as e:
                print(f"error: {e}")
            await asyncio.sleep(1.5)
        print()
        print("If the crosshair landed where the label said, calibration is correct.")
        print("If taps are consistently offset/scaled, that points to a device-level")
        print("issue (density/letterboxing) — report it so a correction can be added.")
        return 0

    return asyncio.run(_run())


def cmd_logs(args: argparse.Namespace) -> int:
    """View the universal chat log: every message sent to/from Ophelia, with
    media. Filters by user/channel, direction, media-only, date, and limit."""
    import asyncio
    import datetime as _dt

    from ophelia.channels.chat_log import ChatLogger

    settings = Settings()
    ensure_dirs(settings)
    logger = ChatLogger.from_settings(settings)

    def _parse_date(s: str | None) -> float | None:
        if not s:
            return None
        try:
            return _dt.datetime.fromisoformat(s).timestamp()
        except ValueError:
            try:
                return _dt.datetime.strptime(s, "%Y-%m-%d").timestamp()
            except ValueError:
                print(f"Unrecognized date: {s} (use YYYY-MM-DD or ISO datetime)")
                raise SystemExit(2)

    since = _parse_date(args.since)
    until = _parse_date(args.until)

    async def _run() -> int:
        rows = await logger.query(
            channel=args.channel,
            direction=args.direction,
            media_only=args.media,
            since=since,
            until=until,
            limit=args.limit,
        )
        if not rows:
            print("(no log entries match)")
            return 0
        rows = list(reversed(rows))  # oldest -> newest for reading
        for r in rows:
            ts = _dt.datetime.fromtimestamp(r["ts"]).strftime("%Y-%m-%d %H:%M:%S")
            arrow = "->" if r["direction"] == "out" else "<-"
            who = "owner" if r["is_owner"] else "guest"
            chan = r["channel"]
            body = (r["text"] or "").replace("\n", " ")[:160]
            media = ""
            if r["media_path"]:
                media = f"  [media:{r['media_kind'] or '?'} {r['media_path']}]"
            print(f"{ts}  {arrow} {chan} ({who})  {body}{media}")
        print(f"\n{len(rows)} entries.")
        return 0

    return asyncio.run(_run())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ophelia",
        description="Ophelia - willful autonomous AI. Run with no args for the menu.",
    )
    sub = parser.add_subparsers(dest="command")

    p_run = sub.add_parser("run", help="Run Ophelia always-on (foreground)")
    p_run.add_argument(
        "--restart",
        action="store_true",
        help="Auto-restart on crash (up to 5 times)",
    )
    p_run.set_defaults(func=cmd_run)

    sub.add_parser("start", help="Start Ophelia in tmux (Termux: wake-lock + tmux + run)").set_defaults(
        func=cmd_start
    )
    sub.add_parser("stop", help="Stop the Ophelia tmux session (Termux)").set_defaults(func=cmd_stop)
    sub.add_parser("restart", help="Restart the Ophelia tmux session (Termux)").set_defaults(
        func=cmd_restart
    )
    sub.add_parser("menu", help="Open the interactive launcher menu").set_defaults(func=cmd_menu)
    sub.add_parser(
        "dashboard", help="Live status dashboard (mood, drives, pressure, channels)"
    ).set_defaults(func=cmd_dashboard)

    p_setup = sub.add_parser(
        "setup",
        help="Step-by-step install guide (idiot-proof checklist)",
    )
    p_setup.add_argument(
        "--do",
        dest="do_auto",
        action="store_true",
        help="Auto-create ~/.ophelia, copy .env and example files",
    )
    p_setup.add_argument(
        "-i",
        "--interactive",
        action="store_true",
        help="Interactive menus (default when terminal supports it)",
    )
    p_setup.add_argument(
        "--checklist",
        action="store_true",
        help="Text checklist only (no arrow-key menus)",
    )
    p_setup.add_argument("--step", type=int, default=None, help="Show one step only")
    p_setup.add_argument("--pc", action="store_true", help="Force PC guide")
    p_setup.add_argument("--phone", action="store_true", help="Force Termux guide")
    p_setup.set_defaults(func=cmd_setup)

    p_doc = sub.add_parser(
        "doctor",
        help="Self-check: version, deps, providers, services",
    )
    p_doc.add_argument(
        "--chat-only",
        action="store_true",
        help="Do not require Telegram (PC dev mode)",
    )
    p_doc.add_argument(
        "--quick",
        action="store_true",
        help="Skip network probes (providers, Telegram, Ollama, ADB)",
    )
    p_doc.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Show hints even for passing checks",
    )
    p_doc.set_defaults(func=cmd_doctor)

    p_check = sub.add_parser(
        "check",
        help="Same as doctor — verify install, version, and runtime",
    )
    p_check.add_argument("--chat-only", action="store_true")
    p_check.add_argument("--quick", action="store_true")
    p_check.add_argument("-v", "--verbose", action="store_true")
    p_check.set_defaults(func=cmd_check)
    sub.add_parser("providers", help="Show resolved AI provider routing").set_defaults(
        func=cmd_providers
    )
    sub.add_parser("models", help="Local model cookbook (RAM/GPU -> Ollama picks)").set_defaults(
        func=cmd_models
    )

    p_ui = sub.add_parser("ui", help="Launch PC workstation web UI")
    p_ui.add_argument(
        "--no-browser",
        action="store_true",
        help="Do not open browser automatically",
    )
    p_ui.set_defaults(func=cmd_ui)

    p_chat = sub.add_parser("chat", help="One-shot chat message (no always-on loop)")
    p_chat.add_argument("message")
    p_chat.set_defaults(func=cmd_chat)

    sub.add_parser("curator", help="Run memory curator once").set_defaults(func=cmd_curator_run)

    sub.add_parser("status", help="Show live autonomy state (mood, drives, pressure)").set_defaults(
        func=cmd_status
    )
    sub.add_parser("pause", help="Pause autonomy outreach on a running Ophelia").set_defaults(
        func=cmd_pause
    )
    sub.add_parser("resume", help="Resume autonomy outreach on a running Ophelia").set_defaults(
        func=cmd_resume
    )
    p_reflect = sub.add_parser("reflect", help="Run one self-reflection cycle now")
    p_reflect.add_argument("focus", nargs="?", default="", help="Optional topic to reflect on")
    p_reflect.set_defaults(func=cmd_reflect)

    p_inner = sub.add_parser("inner", help="Inner monologue log")
    p_inner.add_argument("lines", nargs="?", type=int, default=40)
    p_inner.set_defaults(func=cmd_inner_tail)

    p_mig = sub.add_parser("migrate", help="Import from Hermes")
    mig_sub = p_mig.add_subparsers(dest="migrate_cmd", required=True)
    p_h = mig_sub.add_parser("hermes")
    p_h.add_argument("--source", default=None, help="~/.hermes")
    p_h.add_argument("--dry-run", action="store_true")
    p_h.set_defaults(func=cmd_migrate_hermes)

    auth = sub.add_parser("auth")
    auth_sub = auth.add_subparsers(dest="auth_cmd", required=True)
    auth_sub.add_parser(
        "login",
        help="Fresh SuperGrok OAuth via Hermes browser login, import to Ophelia",
    ).set_defaults(func=cmd_auth_login)
    auth_sub.add_parser("import-grok", help="Import OAuth from Grok CLI (~/.grok)").set_defaults(
        func=cmd_auth_import_grok
    )
    p_ih = auth_sub.add_parser("import-hermes", help="Import OAuth from Hermes (~/.hermes)")
    p_ih.add_argument("--hermes-home", default=str(Path.home() / ".hermes"))
    p_ih.set_defaults(func=cmd_auth_import_hermes)
    p_st = auth_sub.add_parser("set-token", help="Write a raw xAI API token")
    p_st.add_argument("token")
    p_st.set_defaults(func=cmd_auth_set_token)
    p_ar = auth_sub.add_parser("status", help="Show OAuth files, expiry, refresh token")
    p_ar.set_defaults(func=cmd_auth_status)
    p_rf = auth_sub.add_parser("refresh", help="Refresh SuperGrok OAuth now")
    p_rf.add_argument(
        "--force",
        action="store_true",
        help="Always hit the refresh endpoint (test even if access token still valid)",
    )
    p_rf.add_argument("-v", "--verbose", action="store_true", help="Show auth file details")
    p_rf.set_defaults(func=cmd_auth_refresh)

    xfer = sub.add_parser("transfer", help="Move Hermes data phone <-> PC")
    xfer_sub = xfer.add_subparsers(dest="transfer_cmd", required=True)

    p_recv = xfer_sub.add_parser(
        "receive", help="PC: receive a phone upload over Wi-Fi (not for Termux)"
    )
    p_recv.add_argument("--host", default="0.0.0.0")
    p_recv.add_argument("--port", type=int, default=8777)
    p_recv.add_argument("--token", default=None)
    p_recv.add_argument("--dest", default=str(OPHELIA_HOME / "data"))
    p_recv.add_argument("--no-import", action="store_true", help="Save file only")
    p_recv.set_defaults(func=cmd_transfer_receive)

    p_send = xfer_sub.add_parser(
        "send", help="Termux: upload bundle to a PC URL (from `transfer receive`)"
    )
    p_send.add_argument("url", help="http://PC_IP:8777 from transfer receive")
    p_send.add_argument("--token", default=None)
    p_send.add_argument("--hermes-home", default=str(Path.home() / ".hermes"))
    p_send.set_defaults(func=cmd_transfer_send)

    p_up = xfer_sub.add_parser(
        "cloud-upload", help="Phone: upload to free temp cloud (any network)"
    )
    p_up.add_argument("--hermes-home", default=str(Path.home() / ".hermes"))
    p_up.set_defaults(func=cmd_transfer_cloud_upload)

    p_down = xfer_sub.add_parser(
        "cloud-download", help="PC: download from cloud link and import"
    )
    p_down.add_argument("url")
    p_down.add_argument("--output", default=None)
    p_down.add_argument("--no-import", action="store_true")
    p_down.set_defaults(func=cmd_transfer_cloud_download)

    phone = sub.add_parser("phone", help="Phone body tools (touch calibration)")
    phone_sub = phone.add_subparsers(dest="phone_cmd", required=True)
    phone_sub.add_parser(
        "calibrate",
        help="Diagnose + calibrate touch: native size, screenshot scale, grid save, tap corners.",
    ).set_defaults(func=cmd_phone_calibrate)

    p_logs = sub.add_parser(
        "logs", help="View the universal chat log (messages + media sent to/from her)"
    )
    p_logs.add_argument(
        "--channel", default=None, help="Filter by channel, e.g. telegram:12345"
    )
    p_logs.add_argument(
        "--direction", default=None, choices=["in", "out"], help="in=to her, out=from her"
    )
    p_logs.add_argument(
        "--media", action="store_true", help="Only show entries with attached media"
    )
    p_logs.add_argument("--since", default=None, help="From date (YYYY-MM-DD or ISO)")
    p_logs.add_argument("--until", default=None, help="To date (YYYY-MM-DD or ISO)")
    p_logs.add_argument(
        "--limit", type=int, default=80, help="Max entries (most recent first)"
    )
    p_logs.set_defaults(func=cmd_logs)

    args = parser.parse_args(argv)
    if not args.command:
        # No subcommand → launch the interactive menu on a TTY, else print help.
        import sys as _sys

        if _sys.stdin.isatty():
            return cmd_menu(None)
        parser.print_help()
        return 0
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
