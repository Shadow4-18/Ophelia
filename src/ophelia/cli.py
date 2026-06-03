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
from ophelia.providers.router import XAIBackend, build_backend

structlog.configure(
    processors=[
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.dev.ConsoleRenderer(),
    ]
)


def cmd_run(_: argparse.Namespace) -> int:
    settings = Settings()
    ensure_dirs(settings)
    orch = Orchestrator(settings)
    try:
        asyncio.run(orch.start())
    except KeyboardInterrupt:
        return 0
    return 0


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
    settings = Settings()
    hermes = Path(args.hermes_home).expanduser()
    auth = hermes / "auth.json"
    if not auth.is_file():
        print(f"No {auth}. On old phone: hermes auth add xai-oauth")
        return 1
    if import_hermes_auth_full(auth, settings.hermes_auth_path):
        state = load_oauth_state(settings.hermes_auth_path)
        if state:
            save_oauth_token(
                settings.xai_oauth_token_path,
                state["access_token"],
                state.get("refresh_token"),
            )
        print("SuperGrok OAuth imported (with refresh support).")
        print(f"  {settings.hermes_auth_path}")
        return 0
    print("auth.json copied but no xai-oauth token found.")
    return 1


def cmd_auth_refresh(_: argparse.Namespace) -> int:
    settings = Settings()
    from ophelia.providers.oauth_refresh import ensure_fresh_token

    path = settings.hermes_auth_path if settings.hermes_auth_path.is_file() else settings.xai_oauth_token_path
    try:
        token = asyncio.run(ensure_fresh_token(path))
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
        print("Next: ophelia auth import-hermes  (if not already)")
        print("       merge from-hermes.env into ~/.ophelia/.env")
    return 0


def cmd_doctor(_: argparse.Namespace) -> int:
    settings = Settings()
    ensure_dirs(settings)
    backend = build_backend(settings)
    ok = True
    print(f"Ophelia home: {OPHELIA_HOME}")
    print(f"Provider:     {settings.provider} ({backend.label()})")
    if isinstance(backend, XAIBackend):
        if backend.bearer():
            print("xAI auth:     OK (OAuth/API)")
        else:
            print("xAI auth:     MISSING — ophelia auth import-hermes")
            ok = False
    if (OPHELIA_HOME / "SOUL.md").is_file():
        print("Persona:      SOUL.md loaded")
    elif (settings.hermes_home / "SOUL.md").is_file():
        print("Persona:      in Hermes only — run: ophelia migrate hermes")
    if settings.telegram_bot_token:
        print("Telegram:     token set")
    else:
        print("Telegram:     TELEGRAM_BOT_TOKEN missing")
        ok = False
    ch = settings.primary_user_channel()
    print(f"Consciousness: {'on' if settings.consciousness_on() else 'off'} -> {ch or 'set TELEGRAM_ALLOWED_USER_IDS'}")
    print(f"Initiative:    threshold={settings.initiative_threshold} max/h={settings.max_spontaneous_per_hour} quiet={settings.quiet_hours or 'off'}")
    goals_path = OPHELIA_HOME / "goals.yaml"
    print(f"Goals:         {goals_path} ({'exists' if goals_path.is_file() else 'missing'})")
    print(f"Vision:        {'on' if settings.vision_enabled else 'off'}")
    if settings.android_enabled:
        from ophelia.android.shizuku import AndroidBody

        body = AndroidBody(Path(str(settings.phone_control_path)).expanduser())
        print(f"Android body:  {body.status_line()}")
    print(f"Inner log:    {'on' if settings.inner_log_enabled else 'off'} -> {OPHELIA_HOME / 'data' / 'inner_monologue.md'}")
    print(f"Listen loop:  default={'on' if settings.listen_enabled_default else 'off'} (Termux:API)")
    print(f"Curator:      {'on' if settings.curator_enabled else 'off'} every {settings.curator_interval_hours}h")
    print(f"Prompter:     {(OPHELIA_HOME / 'PROMPTER.md').is_file()}")
    games_path = OPHELIA_HOME / "games.yaml"
    print(
        f"Games:        {'on' if settings.games_enabled else 'off'} "
        f"({games_path.name} {'exists' if games_path.is_file() else 'missing'})"
    )
    return 0 if ok else 1


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
        from ophelia.providers.router import build_backend
        from ophelia.tools.registry import ToolRegistry

        mem = MemoryStore(settings.memory_db)
        await mem.init()
        psyche = await mem.load_psyche()
        backend = build_backend(settings)
        from ophelia.android.shizuku import AndroidBody

        android = AndroidBody() if settings.android_enabled else None
        tools = ToolRegistry(settings, settings.data_dir / "artifacts", android=android)
        agent = AgentLoop(backend, settings, mem, tools, psyche, drives=await mem.load_drives())
        print(await agent.run_turn("cli", args.message))

    asyncio.run(_once())
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ophelia")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("run").set_defaults(func=cmd_run)
    sub.add_parser("doctor").set_defaults(func=cmd_doctor)

    p_chat = sub.add_parser("chat")
    p_chat.add_argument("message")
    p_chat.set_defaults(func=cmd_chat)

    sub.add_parser("curator", help="Run memory curator once").set_defaults(func=cmd_curator_run)

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
    auth_sub.add_parser("import-grok").set_defaults(func=cmd_auth_import_grok)
    p_ih = auth_sub.add_parser("import-hermes")
    p_ih.add_argument("--hermes-home", default=str(Path.home() / ".hermes"))
    p_ih.set_defaults(func=cmd_auth_import_hermes)
    p_st = auth_sub.add_parser("set-token")
    p_st.add_argument("token")
    p_st.set_defaults(func=cmd_auth_set_token)
    auth_sub.add_parser("refresh", help="Refresh SuperGrok OAuth now").set_defaults(
        func=cmd_auth_refresh
    )

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
