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


def cmd_site(args: argparse.Namespace) -> int:
    """Serve / export / deploy Ophelia's public wiki/blog."""
    settings = Settings()
    ensure_dirs(settings)
    action = getattr(args, "site_cmd", None) or "serve"

    if action == "export":
        from ophelia.site.store import SiteStore

        async def _export() -> dict:
            store = SiteStore(settings.site_dir)
            await store.init()
            return await store.export_static()

        manifest = asyncio.run(_export())
        print(f"Exported {manifest.get('pages', 0)} pages → {manifest.get('path')}")
        return 0

    if action == "deploy":
        from ophelia.site.cloudflare import (
            CloudflarePagesError,
            deploy_directory,
            deploy_ready,
        )
        from ophelia.site.store import SiteStore

        cf = deploy_ready(
            account_id=settings.cloudflare_account_id,
            api_token=settings.cloudflare_api_token,
            project=settings.site_cf_project,
        )
        if not cf["ready"]:
            print("Cloudflare deploy not configured. Add to ~/.ophelia/.env:")
            for m in cf["missing"]:
                print(f"  {m}")
            print("Token: Cloudflare dashboard → My Profile → API Tokens →")
            print("  Create Token → template 'Edit Cloudflare Workers' or custom")
            print("  with Account → Cloudflare Pages → Edit.")
            return 1

        async def _export() -> tuple:
            store = SiteStore(settings.site_dir)
            await store.init()
            manifest = await store.export_static()
            return store, manifest

        store, manifest = asyncio.run(_export())
        print(f"Exported {manifest.get('pages', 0)} pages → {store.export_dir}")
        try:
            result = deploy_directory(
                store.export_dir,
                account_id=settings.cloudflare_account_id or "",
                api_token=settings.cloudflare_api_token or "",
                project=settings.site_cf_project or "",
                branch=settings.site_cf_branch or "main",
                create_project=bool(settings.site_cf_create_project),
                on_progress=print,
            )
        except CloudflarePagesError as e:
            print(f"Deploy failed: {e}", file=sys.stderr)
            return 1
        public = (settings.site_public_url or "").strip() or result.url
        print(f"Deployed via {result.method}: {result.url or 'ok'}")
        if public:
            print(f"Public URL: {public}")
        return 0

    from ophelia.site.server import run_site

    print(f"Ophelia site: http://{settings.site_host}:{settings.site_port}/")
    print(f"Content:      {settings.site_dir}")
    print("She edits this via site_* tools while `ophelia run` is up.")
    print("Deploy to Cloudflare: ophelia site deploy  (needs CF token in .env)")
    try:
        asyncio.run(run_site(settings))
    except KeyboardInterrupt:
        return 0
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    settings = Settings()
    ensure_dirs(settings)

    from ophelia.core.crashlog import install_all, write_crash

    install_all()

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
            path = write_crash(
                e,
                where="ophelia.run",
                attempt=attempts,
                extra={"restart": restart, "max_restarts": max_restarts},
            )
            log = structlog.get_logger()
            log.exception(
                "ophelia.crashed", attempt=attempts, error=str(e), crash_log=str(path)
            )
            if attempts > max_restarts:
                print(
                    f"ophelia crashed {attempts} times; giving up. Last error: {e}",
                    file=sys.stderr,
                )
                print(f"Crash log: {path}", file=sys.stderr)
                print("  View: ophelia crash", file=sys.stderr)
                return 1
            print(
                f"ophelia crashed (attempt {attempts}): {e}. Restarting in 5s...",
                file=sys.stderr,
            )
            print(f"Crash log: {path}  (ophelia crash)", file=sys.stderr)
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


def cmd_whoami(_: argparse.Namespace) -> int:
    """Diagnose owner recognition — why you might be stuck in guest mode.

    Prints exactly which channels Ophelia treats as owner right now, what
    OPHELIA_OWNER_ID is set to (if anything), and the allowlists. Run this
    on the phone when Telegram suddenly shows you as a guest — the usual
    cause is OPHELIA_OWNER_ID being set to a single platform (e.g. just
    'discord:222'), which demotes the Telegram owner.
    """
    settings = Settings()
    print("== Owner recognition ==")
    print(f"  OPHELIA_OWNER_ID:         {settings.owner_id or '(unset — using fallback)'}")
    print(f"  OPHELIA_PRIMARY_CHANNEL:  {settings.primary_channel or '(unset)'}")
    print(f"  telegram_enabled:         {settings.telegram_enabled}")
    print(f"  discord_enabled:          {settings.discord_enabled}")
    print(f"  TELEGRAM_ALLOWED_USER_IDS: {settings.telegram_allowed_user_ids or '(empty)'}")
    print(f"  DISCORD_ALLOWED_USER_IDS:  {settings.discord_allowed_user_ids or '(empty)'}")
    print(f"  owner_channels (resolved): {sorted(settings.owner_channels()) or '(none!)'}")
    print()
    tg = settings._allowed_telegram_users_ordered()
    dc = settings._allowed_discord_users_ordered()
    print("  Interpretation:")
    if tg:
        print(f"    Telegram owner = telegram:{tg[0]}  (first entry; "
              f"{'also approved guests: ' + ','.join(map(str, tg[1:])) if len(tg) > 1 else 'no guests'})")
    if dc:
        print(f"    Discord owner  = discord:{dc[0]}  (first entry; "
              f"{'also approved guests: ' + ','.join(map(str, dc[1:])) if len(dc) > 1 else 'no guests'})")

    rc = 0
    if not settings.owner_channels():
        print("    WARNING: no owner is configured — everyone is a guest or rejected.")
        return 1

    explicit = (settings.owner_id or "").strip()
    if not explicit:
        rc = 1
        print()
        print("  !! OWNER ID IS NOT EXPLICITLY SET:")
        print("    You're relying on list-order fallback (first user in each platform's")
        print("    allowlist is treated as owner). This is fragile — adding a second")
        print("    platform or reordering the list can silently demote you to guest.")
        print("    Fix: set OPHELIA_OWNER_ID explicitly in ~/.ophelia/.env:")
        tg_id = tg[0] if tg else "YOUR_TG_ID"
        dc_id = dc[0] if dc else "YOUR_DC_ID"
        parts = []
        if settings.telegram_enabled:
            parts.append(f"telegram:{tg_id}")
        if settings.discord_enabled:
            parts.append(f"discord:{dc_id}")
        print(f"      OPHELIA_OWNER_ID={','.join(parts)}")

    # Detect the classic footgun: OPHELIA_OWNER_ID set to a subset of the
    # enabled platforms. e.g. OPHELIA_OWNER_ID=discord:222 when both telegram
    # and discord are enabled silently demotes the Telegram owner to a guest.
    if explicit:
        present_platforms = {c.split(":", 1)[0] for c in settings.owner_channels()}
        enabled_platforms: set[str] = set()
        if settings.telegram_enabled:
            enabled_platforms.add("telegram")
        if settings.discord_enabled:
            enabled_platforms.add("discord")
        missing = enabled_platforms - present_platforms
        if missing and present_platforms:
            rc = 1
            print()
            print("  !! FOOTGUN DETECTED:")
            print(f"    OPHELIA_OWNER_ID is set to {sorted(present_platforms)} but "
                  f"{sorted(missing)} is ALSO enabled.")
            print(f"    Your {sorted(missing)} identity is NOT recognized as owner — "
                  "you're a guest there.")
            print("    Fix: add the missing platform(s) to OPHELIA_OWNER_ID, e.g.:")
            tg_id = tg[0] if tg else "YOUR_TG_ID"
            dc_id = dc[0] if dc else "YOUR_DC_ID"
            print(f"      OPHELIA_OWNER_ID=telegram:{tg_id},discord:{dc_id}")
            print("    ...or delete the OPHELIA_OWNER_ID line entirely to let "
                  "auto-detection cover both.")

    # Also flag: an enabled platform's allowlist has a user who ISN'T in
    # owner_channels — that user would be a guest on a platform where the
    # owner is also configured. (Only relevant for the primary user.)
    print()
    print("  If your Telegram ID isn't in owner_channels above, you'll be a guest.")
    print("  Fix: remove OPHELIA_OWNER_ID from ~/.ophelia/.env (let it auto-detect")
    print("  the first allowed user on each platform), OR set it to BOTH platforms:")
    print('    OPHELIA_OWNER_ID=telegram:YOUR_TG_ID,discord:YOUR_DC_ID')
    return rc


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
    from ophelia.core.crashlog import crash_log_summary

    hb = OPHELIA_HOME / "data" / "heartbeat.json"
    if not hb.is_file():
        print("No heartbeat — Ophelia is not running (or hasn't written one yet).")
        summary = crash_log_summary()
        if summary:
            print(f"  crashlog: {summary}")
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
        f"  last_user_msg: {_ago_label(data.get('last_user_msg_ago'))}",
        f"  channels: {data.get('channels', [])}",
        f"  consciousness: {data.get('consciousness', False)}  dream: {data.get('dream', False)}",
    ]
    print("\n".join(lines))
    summary = crash_log_summary()
    if summary:
        print(f"  crashlog: {summary}")
    return 0


def cmd_crash(args: argparse.Namespace) -> int:
    """Show the last framework crash (survives tmux/terminal close)."""
    from ophelia.core.crashlog import crash_log_path, last_crash_excerpt

    path = crash_log_path()
    if getattr(args, "path", False):
        print(path)
        return 0 if path.is_file() else 1
    if not path.is_file():
        print(f"No crash log yet at {path}")
        print("Crashes from `ophelia run` are appended here automatically.")
        return 0
    text = last_crash_excerpt(max_chars=int(getattr(args, "chars", 12000) or 12000))
    if not text.strip():
        print(f"Crash log is empty: {path}")
        return 0
    print(f"# {path}\n")
    print(text.rstrip())
    print()
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


def cmd_eval(args: argparse.Namespace) -> int:
    """Run behavioural scenarios against her real model (stubbed tools)."""
    from ophelia.evals.runner import run_suite
    from ophelia.evals.scenarios import load_scenarios

    import logging

    # Keep the PASS/FAIL table readable: only warnings (e.g. truncation).
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.DEBUG if args.verbose else logging.WARNING
        )
    )
    settings = Settings()
    ensure_dirs(settings)
    scenarios = load_scenarios(OPHELIA_HOME / "evals")
    if args.list:
        for s in scenarios:
            print(f"{s.id:32} {s.kind:5} {s.description}")
        return 0
    if args.only:
        wanted = set(args.only)
        scenarios = [s for s in scenarios if s.id in wanted or any(s.id.startswith(w) for w in wanted)]
        if not scenarios:
            print("No matching scenarios (see `ophelia eval --list`).")
            return 1

    def _show(r) -> None:
        mark = "PASS" if r.passed else "FAIL"
        tools = f" tools={','.join(r.tools)}" if r.tools else ""
        print(f"  {mark} {r.scenario} ({r.seconds:.1f}s){tools}")
        for f in r.failures:
            print(f"       - {f}")
        if not r.passed and r.output:
            print(f"       > {r.output[:200]!r}")

    print(f"Running {len(scenarios)} scenario(s) × {args.runs} run(s)…")
    report = asyncio.run(
        run_suite(
            settings,
            scenarios,
            runs=args.runs,
            judge=args.judge,
            out_dir=settings.data_dir / "evals",
            on_result=_show,
        )
    )
    total = sum(v["runs"] for v in report["summary"].values())
    passed = sum(v["passed"] for v in report["summary"].values())
    print(f"\n{passed}/{total} passed — chat {report['chat_model']}")
    print(f"Report: {report.get('path')}")
    return 0 if passed == total else 1


def cmd_persona(args: argparse.Namespace) -> int:
    """Inspect / undo personality drift (SOUL rewrites, wiped PROMPTER, generic lessons)."""
    from ophelia.mind import persona_report

    settings = Settings()
    if args.action == "restore":
        if len(args.args) != 2 or args.args[0] not in ("soul", "prompter"):
            print("Usage: ophelia persona restore soul|prompter <backup-file-name>")
            return 1
        print(persona_report.restore(args.args[0], args.args[1]))
        return 0
    if args.action == "forget":
        try:
            ids = [int(x.lstrip("#")) for x in args.args]
        except ValueError:
            print("Usage: ophelia persona forget [--generic] [lesson ids…]")
            return 1
        if not (ids or args.generic or args.all or args.matching):
            print("Usage: ophelia persona forget [ids…] [--generic] [--all] [--matching WORD …]")
            return 1
        print(persona_report.forget(
            settings.memory_db, ids, generic=args.generic, everything=args.all,
            matching=args.matching,
        ))
        return 0
    print(persona_report.report(settings.memory_db))
    return 0


def _ago_label(seconds) -> str:
    """'3m ago' style; 'never (since start)' when no message was seen (the
    heartbeat reports now - 0, i.e. ~56 years)."""
    try:
        s = float(seconds)
    except (TypeError, ValueError):
        return "?"
    if s < 0 or s > 10 * 365 * 86400:
        return "never (since start)"
    if s < 90:
        return f"{int(s)}s ago"
    if s < 5400:
        return f"{int(s / 60)}m ago"
    if s < 172800:
        return f"{s / 3600:.1f}h ago"
    return f"{s / 86400:.1f}d ago"


def cmd_cleanup(args: argparse.Namespace) -> int:
    """Free space: old vision screenshots, and (if you choose) old chat-log media."""
    from ophelia.storage import housekeep, human_bytes

    settings = Settings()
    hours = args.screenshot_hours if args.screenshot_hours is not None else settings.screenshot_keep_hours
    days = args.chat_media_days if args.chat_media_days is not None else settings.chat_log_media_days
    result = housekeep(
        settings.data_dir, screenshot_hours=hours, chat_media_days=days, dry_run=args.dry_run
    )
    verb = "Would free" if args.dry_run else "Freed"
    for label, (files, size) in result.by_dir.items():
        print(f"  {label}: {files} file(s), {human_bytes(size)}")
    print(f"{verb} {human_bytes(result.bytes)} in {result.files} file(s).")
    if not days:
        print("Chat-log media kept (no retention set). e.g. "
              "`ophelia cleanup --chat-media-days 30 --dry-run` to preview.")
    return 0


def cmd_civitai_test(args: argparse.Namespace) -> int:
    """Three real Civitai generations (Illustrious auto-pick, pinned Pony,
    img2img at --strength), printing what was sent and what came back."""
    import asyncio
    import json

    from ophelia.providers import civitai as civ
    from ophelia.providers.media import _civitai_image

    settings = Settings()
    if not settings.civitai_api_key:
        print("CIVITAI_API_KEY is not set in ~/.ophelia/.env")
        return 1
    out_dir = settings.data_dir / "artifacts"
    ledger = settings.data_dir / "logs" / "image_generations.jsonl"
    print("Runs 3 generations on your Civitai key (uses Buzz).")

    async def run() -> int:
        runs = [
            ("txt2img, Illustrious auto-pick", dict(
                prompt="1girl, solo, goth vtuber, purple hair, neon city at night",
                model="", auto_pick=True)),
            ("txt2img, pinned Pony checkpoint", dict(
                prompt="source_anime, 1girl, solo, goth, purple hair, city at night",
                model=civ._FALLBACK_PONY_AIR, auto_pick=False)),
        ]
        failed = 0
        first_path = args.image
        for i, (label, kw) in enumerate(runs + [("img2img", None)]):
            if kw is None:
                if not first_path:
                    print(f"\n[{label}] skipped: no source image (first run failed?)")
                    failed += 1
                    break
                kw = dict(prompt="1girl, solo, same character, rainy night, glowing signs", model="",
                          auto_pick=True, image=str(first_path), strength=args.strength)
                label = f"img2img, strength {args.strength}"
            before = len(ledger.read_text(encoding="utf-8").splitlines()) if ledger.is_file() else 0
            try:
                result = await _civitai_image(
                    settings, kw.pop("prompt"), "1:1", kw.pop("model"), out_dir, **kw
                )
            except Exception as e:  # network errors etc.: show, keep going
                result = f"error: {type(e).__name__}: {e}"
            entry = {}
            lines = ledger.read_text(encoding="utf-8").splitlines() if ledger.is_file() else []
            if len(lines) > before:  # this run wrote its line
                entry = json.loads(lines[-1])
            print(f"\n[{label}]\n  result: {result}")
            for key in ("operation", "checkpoint", "loras", "strength", "seed", "path",
                        "bytes", "error"):
                if entry.get(key) not in (None, {}, ""):
                    print(f"  {key}: {entry[key]}")
            if result.startswith("Image saved to"):
                if i == 0 and not first_path:
                    first_path = entry.get("path")
            else:
                failed += 1
        print(f"\n{3 - failed}/3 succeeded. Full log: {ledger}")
        return 1 if failed else 0

    return asyncio.run(run())


def cmd_backup(args: argparse.Namespace) -> int:
    """Back up / list / restore her identity, memories and databases."""
    from ophelia import backup

    settings = Settings()
    where = backup.backup_dir(settings)
    if args.action == "list":
        items = backup.list_backups(where)
        if not items:
            print(f"No backups in {where} yet. Run `ophelia backup` to make one.")
        for p in items:
            print(f"{p.name}  {p.stat().st_size / 1_048_576:.1f} MB")
        return 0
    if args.action == "restore":
        if not args.file:
            print("Usage: ophelia backup restore <archive> (see `ophelia backup list`)")
            return 1
        import os

        from ophelia.core.single_instance import acquire_run_lock

        token = acquire_run_lock()
        if token is None:
            print("Ophelia is running — stop her first (`ophelia stop` or Ctrl+C), then restore.")
            return 1
        try:
            path = Path(args.file)
            if not path.is_file():
                path = where / args.file
            print(backup.restore_backup(path))
        finally:
            if isinstance(token, int) and token >= 0:
                os.close(token)
        return 0
    path = backup.create_backup(
        where,
        include_secrets=args.secrets or settings.backup_include_secrets,
        keep=settings.backup_keep,
    )
    print(f"Backup written: {path} ({path.stat().st_size / 1_048_576:.1f} MB)")
    if settings.backup_offsite != "owner":
        print("Tip: copy it off the phone, or set OPHELIA_BACKUP_OFFSITE=owner so "
              "she DMs you a copy weekly.")
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


def cmd_update(args: argparse.Namespace) -> int:
    """Pull latest Ophelia code, reinstall, optionally restart."""
    from ophelia.platform import is_termux
    from ophelia.setup.update import run_update

    branch = getattr(args, "branch", None) or None
    if getattr(args, "no_restart", False):
        restart = False
    elif getattr(args, "restart", False):
        restart = True
    else:
        # Termux always-on installs benefit from an automatic bounce.
        restart = is_termux()

    result = run_update(
        branch=branch,
        restart=restart,
        allow_dirty=bool(getattr(args, "allow_dirty", False)),
    )
    print(result.summary())
    return 0 if result.ok else 1


def cmd_dashboard(_: argparse.Namespace) -> int:
    from ophelia.setup.dashboard import run_dashboard

    run_dashboard()
    return 0


def cmd_menu(_: argparse.Namespace) -> int:
    from ophelia.setup.launcher import run_launcher

    return run_launcher()


def cmd_phone_harden(_: argparse.Namespace) -> int:
    """Tier C #12: check and apply the Android kill-switch.

    Runs the checklist (battery optimization, Termux:Boot, wake-lock, tmux
    session) and applies what's auto-fixable. Manual UI steps are printed
    as hints. Use this on Termux after install and after any phone reboot.
    """
    from ophelia.android.harden import apply_harden, check_harden_status

    settings = Settings()
    print("Checking Android kill-switch status...")
    before = check_harden_status(settings)
    print(before.to_text())
    if not before.overall_ok:
        print("\nApplying auto-fixable items...")
        after = apply_harden(settings)
        print("\nAfter applying fixes:")
        print(after.to_text())
    return 0 if before.overall_ok else 0  # always 0 — hints are the point


def cmd_phone_status(_: argparse.Namespace) -> int:
    """Diagnose why the phone body / Shizuku bridge isn't working.

    Prints exactly what Ophelia sees: whether android_enabled is set, whether
    rish / phone_control.sh / adb are present, the resolved mode, and the
    concrete next step. This is the first thing to run when the agent claims
    it has 'no termux api access' or 'shizuku isn't working'.
    """
    from pathlib import Path

    from ophelia.android.factory import build_android_body
    from ophelia.android.shizuku import AndroidBody
    from ophelia.platform import is_termux

    settings = Settings()
    print("== Phone body status ==")
    print(f"  is_termux:        {is_termux()}")
    print(f"  android_enabled:  {settings.android_enabled}")
    print(f"  adb_device:       {settings.adb_device or '(none)'}")
    print(f"  phone_control:    {settings.phone_control_path}"
          + ("  [exists]" if Path(settings.phone_control_path).is_file() else "  [missing]"))

    android = build_android_body(settings)
    if not android:
        print("\n  Phone body: NOT built (android_enabled is false).")
        print("  Fix: set OPHELIA_ANDROID_ENABLED=true in ~/.ophelia/.env")
        return 1

    rish = android.rish_path
    print(f"  rish_path:        {rish or '(none found)'}")
    print(f"  adb_path:         {android._adb_path or '(none)'}")
    print(f"  resolved mode:    {android.mode}")
    print(f"  status_line:      {android.status_line()}")

    if android.mode in ("termux_only", "none"):
        print("\n  Bridge is NOT wired — phone tools will all fail with 'not wired'.")
        print("  The Shizuku APP being running is not enough; Termux needs the bridge script.")
        print("  Fix on the phone:")
        print("    1. Shizuku app -> Start (already done)")
        print("    2. Shizuku -> 'Use in terminal apps' -> Export to Termux folder")
        print("    3. Fix ~/rish line 11: replace PKG with com.termux")
        print("    4. chmod +x ~/rish")
        print("  Then run: bash scripts/termux-shizuku-setup.sh")
        print("  Verify:  ~/rish -c whoami   (should print shell, not an error)")
        return 1

    print("\n  Bridge is wired. If tools still fail, Shizuku may have stopped since")
    print("  boot — restart it in the Shizuku app, then re-run this command.")
    return 0


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


def cmd_tts_voices(_: argparse.Namespace) -> int:
    """List Kokoro voices from the configured local server."""
    import asyncio

    from ophelia.media.voice import kokoro_list_voices, resolve_tts_provider

    settings = Settings()
    if resolve_tts_provider(settings) != "kokoro" and not settings.kokoro_tts_url:
        print("Set KOKORO_TTS_URL (and OPHELIA_TTS_PROVIDER=kokoro) first.")
        return 1

    async def _run() -> int:
        try:
            voices = await kokoro_list_voices(settings)
        except Exception as e:
            print(f"Could not reach Kokoro server: {e}")
            return 1
        if not voices:
            print("(no voices returned — is Kokoro-FastAPI running?)")
            return 1
        print(f"{'id':<24} name")
        print("-" * 40)
        for v in voices:
            if isinstance(v, dict):
                vid = v.get("id") or v.get("name") or str(v)
                name = v.get("name") or vid
                print(f"{vid:<24} {name}")
            else:
                print(v)
        print(f"\n{len(voices)} voices. Mix example: af_bella(0.7)+bf_emma(0.3)")
        return 0

    return asyncio.run(_run())


def cmd_tts_combine(args: argparse.Namespace) -> int:
    """Blend Kokoro voice packs (L2-renormalized) and save a .pt tensor locally."""
    import asyncio

    from ophelia.config import OPHELIA_HOME, ensure_dirs
    from ophelia.media.kokoro_mix import mix_cache_name
    from ophelia.media.voice import kokoro_combine_voices

    settings = Settings()
    ensure_dirs(settings)
    out = Path(args.output).expanduser()
    if out.suffix != ".pt":
        out = out.with_suffix(".pt")
    if not out.is_absolute():
        # Default name from mix formula when user kept the placeholder
        if args.output == "ophelia_mix.pt":
            try:
                out = OPHELIA_HOME / "voices" / f"{mix_cache_name(args.expression)}.pt"
            except ValueError:
                out = OPHELIA_HOME / "voices" / out.name
        else:
            out = OPHELIA_HOME / "voices" / out.name

    async def _run() -> int:
        try:
            path = await kokoro_combine_voices(args.expression, out, settings=settings)
        except Exception as e:
            print(f"Combine failed: {e}")
            return 1
        print(f"Saved L2-renormalized combined voice: {path}")
        voice_name = path.stem
        voices_dir = (settings.kokoro_voices_dir or "").strip()
        if voices_dir:
            dest = Path(voices_dir).expanduser() / path.name
            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(path.read_bytes())
                print(f"Installed into KOKORO_VOICES_DIR: {dest}")
                print(f"Set KOKORO_TTS_VOICE={voice_name}")
            except OSError as e:
                print(f"Could not install into {voices_dir}: {e}")
                print(f"Copy {path} into your Kokoro-FastAPI voices folder, then:")
                print(f"  KOKORO_TTS_VOICE={voice_name}")
        else:
            print(
                "Copy this .pt into your Kokoro-FastAPI voices folder "
                "(or set KOKORO_VOICES_DIR), then:"
            )
            print(f"  KOKORO_TTS_VOICE={voice_name}")
            print(
                "Do NOT put the raw mix string in KOKORO_TTS_VOICE — "
                "Kokoro-FastAPI's inline blend skips L2 renorm and sounds "
                "muffled / peaky."
            )
        return 0

    return asyncio.run(_run())


def cmd_tts_speak(args: argparse.Namespace) -> int:
    """Test TTS synthesis (expressions, speed, voice mix)."""
    import asyncio

    from ophelia.media.voice import resolve_tts_provider, synthesize

    settings = Settings()
    ensure_dirs(settings)
    out = Path(args.output).expanduser() if args.output else settings.data_dir / "tts_test.mp3"
    voice = args.voice or None
    speed = args.speed

    async def _run() -> int:
        provider = resolve_tts_provider(settings)
        bearer = None
        if provider == "xai":
            stack = build_provider_stack(settings)
            xai = stack.xai_backend()
            if not xai:
                print("No xAI credentials for TTS.")
                return 1
            bearer = await xai.bearer_fresh()
        try:
            path = await synthesize(
                args.text,
                out,
                settings=settings,
                xai_bearer=bearer,
                voice=voice,
                speed=speed,
            )
        except Exception as e:
            print(f"TTS failed ({provider}): {e}")
            return 1
        print(f"Provider: {provider}")
        print(f"Saved: {path}")
        if args.play:
            import shutil

            player = shutil.which("termux-media-player") or shutil.which("mpv")
            if player:
                proc = await asyncio.create_subprocess_exec(player, str(path))
                await proc.wait()
            else:
                print("(no player found — open the file manually)")
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
    from ophelia.core.crashlog import install_all

    install_all()

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
    p_update = sub.add_parser(
        "update",
        help="git pull + pip install -e . (optional restart) — remote-friendly self-update",
    )
    p_update.add_argument(
        "--branch",
        "-b",
        default=None,
        help="Branch to checkout/pull (default: current branch)",
    )
    p_update.add_argument(
        "--restart",
        action="store_true",
        help="Schedule a restart after a successful update",
    )
    p_update.add_argument(
        "--no-restart",
        action="store_true",
        help="Never restart (overrides Termux default restart)",
    )
    p_update.add_argument(
        "--allow-dirty",
        action="store_true",
        help="Allow update with local uncommitted changes (autostash rebase)",
    )
    p_update.set_defaults(func=cmd_update)
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
    sub.add_parser(
        "whoami",
        help="Diagnose owner recognition — why you might be stuck in guest mode.",
    ).set_defaults(func=cmd_whoami)
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

    p_site = sub.add_parser(
        "site",
        help="Ophelia's public wiki/blog (serve locally, export, or deploy to Cloudflare)",
    )
    site_sub = p_site.add_subparsers(dest="site_cmd")
    site_sub.add_parser("serve", help="Serve the site locally (default)").set_defaults(
        func=cmd_site, site_cmd="serve"
    )
    site_sub.add_parser(
        "export", help="Export published pages to ~/.ophelia/site/export/"
    ).set_defaults(func=cmd_site, site_cmd="export")
    site_sub.add_parser(
        "deploy",
        help="Export + upload to Cloudflare Pages (needs CLOUDFLARE_* in .env)",
    ).set_defaults(func=cmd_site, site_cmd="deploy")
    p_site.set_defaults(func=cmd_site, site_cmd="serve")

    p_chat = sub.add_parser("chat", help="One-shot chat message (no always-on loop)")
    p_chat.add_argument("message")
    p_chat.set_defaults(func=cmd_chat)

    p_eval = sub.add_parser(
        "eval",
        help="Behavioural evals: scripted situations vs her real model (tools stubbed)",
    )
    p_eval.add_argument("only", nargs="*", help="Scenario ids (or prefixes) to run")
    p_eval.add_argument("--runs", type=int, default=1, help="Runs per scenario (default 1)")
    p_eval.add_argument(
        "--judge", action="store_true", help="Also grade soft rubrics with the curator model"
    )
    p_eval.add_argument("--list", action="store_true", help="List scenarios and exit")
    p_eval.add_argument("-v", "--verbose", action="store_true", help="Show debug logs")
    p_eval.set_defaults(func=cmd_eval)

    p_cleanup = sub.add_parser(
        "cleanup", help="Free space: old vision screenshots and (optionally) old chat-log media"
    )
    p_cleanup.add_argument("--dry-run", action="store_true", help="Show what would be deleted")
    p_cleanup.add_argument(
        "--chat-media-days", type=float, default=None,
        help="Delete chat-log media copies older than N days (default: OPHELIA_CHAT_LOG_MEDIA_DAYS)",
    )
    p_cleanup.add_argument(
        "--screenshot-hours", type=float, default=None,
        help="Delete screen captures older than N hours (default: OPHELIA_SCREENSHOT_KEEP_HOURS)",
    )
    p_cleanup.set_defaults(func=cmd_cleanup)

    p_civ = sub.add_parser(
        "civitai-test", help="Run 3 real Civitai generations and print what was sent/returned"
    )
    p_civ.add_argument("--image", help="Source image for the img2img run (default: run 1's output)")
    p_civ.add_argument("--strength", type=float, default=0.7, help="img2img strength (default 0.7)")
    p_civ.set_defaults(func=cmd_civitai_test)

    p_backup = sub.add_parser(
        "backup", help="Back up / list / restore her identity, memories and databases"
    )
    p_backup.add_argument("action", nargs="?", default="now", choices=["now", "list", "restore"])
    p_backup.add_argument("file", nargs="?", help="restore: archive path or name")
    p_backup.add_argument(
        "--secrets", action="store_true", help="Include .env and OAuth tokens in this backup"
    )
    p_backup.set_defaults(func=cmd_backup)

    p_persona = sub.add_parser(
        "persona",
        help="See/undo personality drift: SOUL rewrites, wiped PROMPTER, generic lessons",
    )
    p_persona.add_argument(
        "action", nargs="?", default="report", choices=["report", "restore", "forget"]
    )
    p_persona.add_argument("args", nargs="*", help="restore: soul|prompter <file>; forget: ids")
    p_persona.add_argument(
        "--generic", action="store_true", help="forget: remove all assistant-policy lessons"
    )
    p_persona.add_argument("--all", action="store_true", help="forget: remove every lesson")
    p_persona.add_argument(
        "--matching", nargs="+", metavar="WORD",
        help="forget: remove lessons containing any of these words",
    )
    p_persona.set_defaults(func=cmd_persona)

    sub.add_parser("curator", help="Run memory curator once").set_defaults(func=cmd_curator_run)

    sub.add_parser("status", help="Show live autonomy state (mood, drives, pressure)").set_defaults(
        func=cmd_status
    )
    p_crash = sub.add_parser(
        "crash",
        help="Show last framework crash (from ~/.ophelia/crash.log — survives terminal close)",
    )
    p_crash.add_argument(
        "--path",
        action="store_true",
        help="Print crash log path only",
    )
    p_crash.add_argument(
        "--chars",
        type=int,
        default=12000,
        help="Max characters of the log tail to print (default 12000)",
    )
    p_crash.set_defaults(func=cmd_crash)
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

    phone = sub.add_parser("phone", help="Phone body tools (touch calibration, kill-switch)")
    phone_sub = phone.add_subparsers(dest="phone_cmd", required=True)
    phone_sub.add_parser(
        "status",
        help="Diagnose why Shizuku / phone body isn't working and print the fix.",
    ).set_defaults(func=cmd_phone_status)
    phone_sub.add_parser(
        "calibrate",
        help="Diagnose + calibrate touch: native size, screenshot scale, grid save, tap corners.",
    ).set_defaults(func=cmd_phone_calibrate)
    phone_sub.add_parser(
        "harden",
        help="Tier C #12: check/apply Android kill-switch (battery opt, boot, wake-lock, tmux).",
    ).set_defaults(func=cmd_phone_harden)

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

    p_tts = sub.add_parser("tts", help="TTS tools (Kokoro voices, mixing, test speak)")
    tts_sub = p_tts.add_subparsers(dest="tts_cmd", required=True)
    tts_sub.add_parser("voices", help="List voices from Kokoro-FastAPI server").set_defaults(
        func=cmd_tts_voices
    )
    p_combine = tts_sub.add_parser(
        "combine",
        help="Bake L2-renormalized voice mix → ~/.ophelia/voices/*.pt",
    )
    p_combine.add_argument(
        "expression",
        help='Mix expression, e.g. "af_bella(0.7)+bf_emma(0.3)"',
    )
    p_combine.add_argument(
        "-o", "--output", default="ophelia_mix.pt", help="Output filename"
    )
    p_combine.set_defaults(func=cmd_tts_combine)
    p_speak = tts_sub.add_parser("speak", help="Test TTS with expressions / speed")
    p_speak.add_argument("text", help='Text to speak (Kokoro: embed [pause:1s] pauses)')
    p_speak.add_argument(
        "--voice",
        default=None,
        help="Voice override (preset or baked mix name)",
    )
    p_speak.add_argument("--speed", type=float, default=None, help="Speech rate")
    p_speak.add_argument("-o", "--output", default=None, help="Output audio path")
    p_speak.add_argument(
        "--play", action="store_true", help="Play via termux-media-player / mpv"
    )
    p_speak.set_defaults(func=cmd_tts_speak)

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
