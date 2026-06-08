"""Interactive setup wizard — menus write ~/.ophelia/.env for you."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

from ophelia.config import OPHELIA_HOME, Settings, ensure_dirs
from ophelia.platform import is_termux, platform_summary
from ophelia.setup.env_io import read_env_key, write_env_updates
from ophelia.setup.tui import checkbox, pause, prompt_text, radiolist
from ophelia.setup.wizard import _auto_setup, run_setup_wizard


def run_interactive_setup(*, phone: bool | None = None) -> int:
    on_phone = is_termux() if phone is None else phone
    ensure_dirs(Settings())

    print()
    print("=" * 52)
    print("  The Ophelia Project — Interactive Setup")
    print(f"  Platform: {platform_summary()}")
    print(f"  Home:     {OPHELIA_HOME}")
    print("=" * 52)
    print()
    print("Arrow keys navigate | Space toggles | Enter confirms | Esc goes back")
    print()

    for action in _auto_setup():
        print(f"  + {action}")
    print()

    while True:
        idx = radiolist(
            "What do you want to configure?",
            [
                "AI provider (Ollama / cloud)",
                "Chat channels (Telegram / Discord)",
                "Persona (SOUL.md)",
                "Phone body (screen/tap)" if on_phone else "Phone body via ADB (optional)",
                "Features (consciousness, games, ...)",
                "Run health check",
                "Show checklist status",
                "Finish setup",
            ],
            description="No manual .env editing — pick options here.",
        )
        if idx < 0:
            continue
        if idx == 0:
            _section_provider(on_phone)
        elif idx == 1:
            _section_channels()
        elif idx == 2:
            _section_persona()
        elif idx == 3:
            _section_phone_body(on_phone)
        elif idx == 4:
            _section_features(on_phone)
        elif idx == 5:
            _section_health_check()
        elif idx == 6:
            print()
            run_setup_wizard(phone=phone, checklist=True, do_auto=False)
        elif idx == 7:
            break
        if idx != 7:
            pause()

    print()
    print("Setup complete. Next:")
    print("  ophelia check")
    if on_phone:
        print("  termux-wake-lock && tmux new -s ophelia && ophelia run")
    else:
        print("  ophelia ui   or   ophelia run")
    print()
    return run_setup_wizard(phone=phone, checklist=True, do_auto=False)


def _section_provider(on_phone: bool) -> None:
    current = read_env_key("OPHELIA_PROVIDER") or "ollama"
    options = [
        ("ollama", "Ollama (local — recommended)"),
        ("auto", "Auto (Ollama if up, else cloud)"),
        ("xai-oauth", "SuperGrok / xAI OAuth"),
        ("xai", "xAI API key"),
        ("openai", "OpenAI API key"),
        ("compat", "OpenAI-compatible endpoint (LM Studio, etc.)"),
    ]
    labels = [label for _, label in options]
    default = next((i for i, (k, _) in enumerate(options) if k == current), 0)

    pick = radiolist(
        "Choose AI provider",
        labels,
        selected=default,
        description="Local-first: Ollama is free and works offline.",
    )
    if pick < 0:
        return
    provider = options[pick][0]
    updates: dict[str, str | None] = {"OPHELIA_PROVIDER": provider}

    if provider in ("ollama", "auto"):
        model = _pick_ollama_model(on_phone)
        if model is None:
            return
        if model:
            updates["OLLAMA_MODEL"] = model
        if on_phone:
            updates.setdefault("OLLAMA_BASE_URL", "http://127.0.0.1:11434/v1")
    elif provider == "xai-oauth":
        if not _configure_xai_oauth():
            return
    elif provider == "xai":
        key = prompt_text("XAI_API_KEY", secret=True, default=read_env_key("XAI_API_KEY"))
        if key:
            updates["XAI_API_KEY"] = key
    elif provider == "openai":
        key = prompt_text("OPENAI_API_KEY", secret=True, default=read_env_key("OPENAI_API_KEY"))
        if key:
            updates["OPENAI_API_KEY"] = key
    elif provider == "compat":
        base = prompt_text(
            "OPHELIA_COMPAT_BASE_URL",
            default=read_env_key("OPHELIA_COMPAT_BASE_URL") or "http://127.0.0.1:1234/v1",
            hint="Example: http://127.0.0.1:1234/v1",
        )
        model = prompt_text(
            "OPHELIA_COMPAT_MODEL",
            default=read_env_key("OPHELIA_COMPAT_MODEL") or "local-model",
        )
        key = prompt_text(
            "OPHELIA_COMPAT_API_KEY",
            secret=True,
            default=read_env_key("OPHELIA_COMPAT_API_KEY") or "local",
        )
        if base:
            updates["OPHELIA_COMPAT_BASE_URL"] = base
        if model:
            updates["OPHELIA_COMPAT_MODEL"] = model
        if key:
            updates["OPHELIA_COMPAT_API_KEY"] = key

    touched = write_env_updates(updates)
    print(f"\n  Saved: {', '.join(touched)}")


def _oauth_status_lines() -> list[str]:
    from ophelia.providers.oauth_refresh import load_oauth_state, oauth_auth_paths

    settings = Settings()
    lines: list[str] = []
    for path in oauth_auth_paths(
        hermes_home=settings.hermes_home,
        hermes_auth_path=settings.hermes_auth_path,
        oauth_path=settings.xai_oauth_token_path,
    ):
        state = load_oauth_state(path)
        if state and state.get("access_token"):
            lines.append(f"[OK] OAuth token in {path}")
            return lines
    lines.append("No OAuth token found yet — import below.")
    hermes = settings.hermes_home / "auth.json"
    grok = settings.grok_cli_auth_path
    if hermes.is_file():
        lines.append(f"  Hermes auth available: {hermes}")
    if grok.is_file():
        lines.append(f"  Grok CLI auth available: {grok}")
    return lines


def _configure_xai_oauth() -> bool:
    """Sub-menu for SuperGrok OAuth. Loops until Done or Back."""
    settings = Settings()
    while True:
        status = "\n".join(_oauth_status_lines())
        pick = radiolist(
            "SuperGrok / xAI OAuth",
            [
                "Import / re-import from Hermes (~/.hermes/auth.json)",
                "Import / re-import from Grok CLI (~/.grok/auth.json)",
                "Verify OAuth connection",
                "Done — use xAI OAuth",
                "Back (don't change provider)",
            ],
            description=status + "\n\nPick an action, read the result, then Enter to continue.",
        )
        if pick < 0 or pick == 4:
            return False
        if pick == 0:
            _import_hermes_oauth(settings)
        elif pick == 1:
            _import_grok_oauth(settings)
        elif pick == 2:
            _verify_xai_oauth()
        elif pick == 3:
            return True
        pause()


def _import_hermes_oauth(settings: Settings) -> bool:
    from ophelia.providers.auth import import_hermes_auth_full, save_oauth_token
    from ophelia.providers.oauth_refresh import load_oauth_state

    auth = settings.hermes_home / "auth.json"
    if not auth.is_file():
        print(f"\n  No {auth}")
        print("  On this phone run: hermes auth add xai-oauth")
        print("  Or: ophelia auth import-hermes after copying auth.json\n")
        return False
    if not import_hermes_auth_full(auth, settings.hermes_auth_path):
        print("\n  auth.json found but no xai-oauth block inside.")
        print("  Re-login: hermes auth add xai-oauth\n")
        return False
    state = load_oauth_state(settings.hermes_auth_path)
    if state:
        save_oauth_token(
            settings.xai_oauth_token_path,
            state["access_token"],
            state.get("refresh_token"),
        )
    print(f"\n  [OK] Imported SuperGrok OAuth from Hermes")
    print(f"       {settings.hermes_auth_path}\n")
    return True


def _import_grok_oauth(settings: Settings) -> bool:
    from ophelia.providers.auth import save_oauth_token, token_from_grok_cli
    from ophelia.providers.oauth_refresh import load_oauth_state

    path = settings.grok_cli_auth_path
    token = token_from_grok_cli(path)
    if not token:
        print(f"\n  No token in {path}")
        print("  Run: grok login   (Grok CLI) then try again.\n")
        return False
    state = load_oauth_state(path) or {}
    save_oauth_token(
        settings.xai_oauth_token_path,
        token,
        state.get("refresh_token"),
    )
    print(f"\n  [OK] Imported OAuth from Grok CLI -> {settings.xai_oauth_token_path}\n")
    return True


def _verify_xai_oauth() -> bool:
    from ophelia.providers.router import build_provider_stack

    async def _probe() -> tuple[bool, str]:
        stack = build_provider_stack(Settings())
        return await stack.check("chat")

    try:
        ok, msg = asyncio.run(_probe())
    except Exception as e:
        print(f"\n  [FAIL] {e}\n")
        return False
    if ok:
        print(f"\n  [OK] xAI OAuth working — {msg}\n")
        return True
    print(f"\n  [FAIL] {msg}")
    print("  Try Import from Hermes or Grok CLI above.\n")
    return False


def _pick_ollama_model(on_phone: bool) -> str | None:
    models = _list_ollama_model_names()
    presets = [
        "llama3.2:3b",
        "llama3.2:1b",
        "llama3.2",
        "mistral",
        "qwen2.5:7b",
    ]
    choices = []
    for m in models[:12]:
        if m not in choices:
            choices.append(m)
    for p in presets:
        if p not in choices:
            choices.append(p)
    choices.append("Type a model name manually...")

    current = read_env_key("OLLAMA_MODEL") or "llama3.2:3b"
    default = next((i for i, c in enumerate(choices) if c == current), 0)

    hint = "Ollama not detected — you can still save a model name." if not models and not on_phone else ""
    pick = radiolist(
        "Ollama chat model",
        choices,
        selected=default,
        description=hint or "Pull models with: ollama pull llama3.2:3b",
    )
    if pick < 0:
        return None
    if choices[pick] == "Type a model name manually...":
        typed = prompt_text("Model name", default=current)
        return typed
    return choices[pick]


def _list_ollama_model_names() -> list[str]:
    import httpx

    async def _fetch() -> list[str]:
        from ophelia.providers.cookbook import list_ollama_models

        return await list_ollama_models(Settings())

    try:
        s = Settings()
        base = s.ollama_base_url.rstrip("/").removesuffix("/v1")
        httpx.get(f"{base}/api/tags", timeout=2.0)
        return asyncio.run(_fetch())
    except Exception:
        return []


def _section_channels() -> None:
    tg_on = bool(read_env_key("TELEGRAM_BOT_TOKEN"))
    dc_on = bool(read_env_key("DISCORD_BOT_TOKEN"))
    selected = set()
    if tg_on:
        selected.add(0)
    if dc_on:
        selected.add(1)

    picked = checkbox(
        "Enable chat channels",
        [
            "Telegram bot",
            "Discord bot",
        ],
        selected=selected,
        description="Get Telegram token from @BotFather. Your user ID from @userinfobot.",
    )

    updates: dict[str, str | None] = {}
    if 0 in picked:
        token = prompt_text(
            "TELEGRAM_BOT_TOKEN",
            secret=True,
            default=read_env_key("TELEGRAM_BOT_TOKEN"),
            hint="Telegram -> @BotFather -> /newbot",
        )
        user_id = prompt_text(
            "TELEGRAM_ALLOWED_USER_IDS (your numeric id)",
            default=read_env_key("TELEGRAM_ALLOWED_USER_IDS"),
            hint="Message @userinfobot to get your id",
        )
        if token:
            updates["TELEGRAM_BOT_TOKEN"] = token
        if user_id:
            updates["TELEGRAM_ALLOWED_USER_IDS"] = user_id
        updates["OPHELIA_TELEGRAM_ENABLED"] = "true"
    else:
        updates["OPHELIA_TELEGRAM_ENABLED"] = "false"

    if 1 in picked:
        token = prompt_text(
            "DISCORD_BOT_TOKEN",
            secret=True,
            default=read_env_key("DISCORD_BOT_TOKEN"),
            hint="Discord Developer Portal -> Bot -> Token",
        )
        user_id = prompt_text(
            "DISCORD_ALLOWED_USER_IDS (your numeric id)",
            default=read_env_key("DISCORD_ALLOWED_USER_IDS"),
        )
        if token:
            updates["DISCORD_BOT_TOKEN"] = token
        if user_id:
            updates["DISCORD_ALLOWED_USER_IDS"] = user_id
        updates["OPHELIA_DISCORD_ENABLED"] = "true"
    else:
        updates["OPHELIA_DISCORD_ENABLED"] = "false"

    touched = write_env_updates(updates)
    print(f"\n  Saved: {', '.join(touched)}")


def _section_persona() -> None:
    has_soul = (OPHELIA_HOME / "SOUL.md").is_file()
    has_hermes = (Path.home() / ".hermes" / "SOUL.md").is_file()
    labels = [
        "Import from Hermes (~/.hermes)",
        "Keep current SOUL.md" if has_soul else "Create empty SOUL.md stub",
        "Skip for now",
    ]
    pick = radiolist(
        "Persona (SOUL.md)",
        labels,
        description="Defines who Ophelia is — personality and boundaries.",
    )
    if pick < 0:
        return
    if pick == 0 and has_hermes:
        print("\n  Running: ophelia migrate hermes\n")
        try:
            from ophelia.migration.hermes import migrate_from_hermes

            report = migrate_from_hermes(Path.home() / ".hermes", dry_run=False)
            print(f"  Hermes import: {len(report.copied)} file(s) copied.\n")
        except Exception as e:
            print(f"  Import failed: {e}\n")
            print("  Try: ophelia migrate hermes\n")
    elif pick == 0 and not has_hermes:
        print("\n  No ~/.hermes/SOUL.md found. Skip or write SOUL.md later.\n")
    elif pick == 1 and not has_soul:
        soul = OPHELIA_HOME / "SOUL.md"
        soul.write_text(
            "# Ophelia\n\nYou are Ophelia — willful, curious, and genuine.\n",
            encoding="utf-8",
        )
        print(f"\n  Created {soul}\n")
    else:
        print("\n  Skipped persona step.\n")


def _section_phone_body(on_phone: bool) -> None:
    if on_phone:
        enabled = (read_env_key("OPHELIA_ANDROID_ENABLED") or "true").lower() == "true"
        pick = radiolist(
            "Phone body on this device",
            [
                "Enable — Shizuku screen/tap (games, vision)",
                "Disable — chat-only (no screen control)",
            ],
            selected=0 if enabled else 1,
            description="Optional. Ophelia works fine without a phone body.",
        )
        if pick < 0:
            return
        write_env_updates(
            {
                "OPHELIA_ANDROID_ENABLED": "true" if pick == 0 else "false",
                "OPHELIA_VISION_ENABLED": "true" if pick == 0 else "false",
            }
        )
        if pick == 0:
            print("\n  Next: install Shizuku app, then run:")
            print("  bash scripts/termux-shizuku-setup.sh\n")
    else:
        enabled = (read_env_key("OPHELIA_ANDROID_ENABLED") or "false").lower() == "true"
        pick = radiolist(
            "Phone body via ADB",
            [
                "Enable — control a separate Android over ADB",
                "Disable — software-only Ophelia (default)",
            ],
            selected=0 if enabled else 1,
        )
        if pick < 0:
            return
        updates: dict[str, str | None] = {
            "OPHELIA_ANDROID_ENABLED": "true" if pick == 0 else "false",
        }
        if pick == 0:
            device = prompt_text(
                "OPHELIA_ADB_DEVICE (ip:5555 or serial)",
                default=read_env_key("OPHELIA_ADB_DEVICE"),
                hint="adb connect PHONE_IP:5555 first",
            )
            if device:
                updates["OPHELIA_ADB_DEVICE"] = device
            if not shutil.which("adb"):
                print("\n  Install platform-tools / adb on this host.\n")
        write_env_updates(updates)
        print(f"\n  Saved phone body settings.\n")


def _section_features(on_phone: bool) -> None:
    def _on(key: str, default: bool = True) -> bool:
        val = read_env_key(key)
        if not val:
            return default
        return val.lower() in ("1", "true", "yes", "on")

    items = [
        "Continuous consciousness",
        "Inner log",
        "Memory curator",
        "Web search tools",
        "Mobile games layer" if on_phone else "Games layer (if phone body)",
    ]
    keys = [
        "OPHELIA_CONSCIOUSNESS",
        "OPHELIA_INNER_LOG",
        "OPHELIA_CURATOR",
        "OPHELIA_WEB_SEARCH",
        "OPHELIA_GAMES",
    ]
    selected = {i for i, k in enumerate(keys) if _on(k)}

    picked = checkbox("Enable features", items, selected=selected)
    updates = {keys[i]: "true" if i in picked else "false" for i in range(len(keys))}
    write_env_updates(updates)
    print(f"\n  Saved feature toggles.\n")


def _section_health_check() -> None:
    print("\n  Running ophelia check...\n")
    try:
        from ophelia.diagnostics.self_check import format_report, run_self_check

        report = asyncio.run(run_self_check(chat_only=False, quick=False))
        print(format_report(report))
        print()
        if report.ok:
            print("  Health check passed.\n")
        else:
            print("  Fix FAIL lines above, then re-run setup.\n")
    except Exception as e:
        print(f"  Check failed: {e}\n")
