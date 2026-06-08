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
    import subprocess

    from ophelia.providers.auth import sync_oauth_from_hermes_home

    settings = Settings()
    print()
    print("SuperGrok OAuth comes from xAI (accounts.x.ai) — not Hermes-specific.")
    print("Hermes runs the browser login; Ophelia imports the same token.")
    print()

    hermes_bin = shutil.which("hermes")
    if hermes_bin:
        print("Opening Hermes browser login...")
        print("Sign in at accounts.x.ai, approve access, then return here.\n")
        result = subprocess.run([hermes_bin, "auth", "add", "xai-oauth"])
        if result.returncode != 0:
            print("Hermes login cancelled or failed.")
            return 1
    else:
        print("Hermes CLI not in PATH. In another Termux tab run:")
        print("  hermes auth add xai-oauth")
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
    print()
    print("Next: ophelia auth refresh   # optional test")
    print("       ophelia check")
    return 0


def cmd_auth_refresh(_: argparse.Namespace) -> int:
    settings = Settings()
    from ophelia.providers.oauth_refresh import ensure_fresh_token, resolve_oauth_auth_path

    path = resolve_oauth_auth_path(
        hermes_home=settings.hermes_home,
        hermes_auth_path=settings.hermes_auth_path,
        oauth_path=settings.xai_oauth_token_path,
    )
    if not path:
        print("No OAuth auth file found. Run: ophelia auth login")
        return 1
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ophelia")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("run").set_defaults(func=cmd_run)

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
    sub.add_parser("models", help="Local model cookbook (RAM/GPU → Ollama picks)").set_defaults(
        func=cmd_models
    )

    p_ui = sub.add_parser("ui", help="Launch PC workstation web UI")
    p_ui.add_argument(
        "--no-browser",
        action="store_true",
        help="Do not open browser automatically",
    )
    p_ui.set_defaults(func=cmd_ui)

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
    auth_sub.add_parser(
        "login",
        help="Fresh SuperGrok OAuth via Hermes browser login, import to Ophelia",
    ).set_defaults(func=cmd_auth_login)
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

    xfer = sub.add_parser("transfer", help="Move Hermes data phone ↔ PC")
    xfer_sub = xfer.add_subparsers(dest="transfer_cmd", required=True)

    p_recv = xfer_sub.add_parser("receive", help="PC: wait for phone upload (same Wi-Fi)")
    p_recv.add_argument("--host", default="0.0.0.0")
    p_recv.add_argument("--port", type=int, default=8777)
    p_recv.add_argument("--token", default=None)
    p_recv.add_argument("--dest", default=str(OPHELIA_HOME / "data"))
    p_recv.add_argument("--no-import", action="store_true", help="Save file only")
    p_recv.set_defaults(func=cmd_transfer_receive)

    p_send = xfer_sub.add_parser("send", help="Phone: upload bundle to PC URL")
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

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
