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
                "Web search (DeepSeek/OpenAI have no built-in search)",
                "Image generation (backends + NSFW)",
                "Voice / TTS (ElevenLabs, Kokoro local, OpenAI, xAI)",
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
            _section_web_search()
        elif idx == 3:
            _section_image_backends()
        elif idx == 4:
            _section_tts()
        elif idx == 5:
            _section_persona()
        elif idx == 6:
            _section_phone_body(on_phone)
        elif idx == 7:
            _section_features(on_phone)
        elif idx == 8:
            _section_health_check()
        elif idx == 9:
            print()
            run_setup_wizard(phone=phone, checklist=True, do_auto=False)
        elif idx == 10:
            break
        if idx != 10:
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
        ("deepseek", "DeepSeek API key (cheap — V4 Flash)"),
        ("openai", "OpenAI API key"),
        ("compat", "OpenAI-compatible endpoint (LM Studio, etc.)"),
    ]
    labels = [label for _, label in options]
    default = next((i for i, (k, _) in enumerate(options) if k == current), 0)

    pick = radiolist(
        "Choose AI provider",
        labels,
        selected=default,
        description="Local-first: Ollama is free and works offline. DeepSeek V4 Flash is very cheap for cloud.",
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
        # Recognize GROK_API_KEY (alias used by some Discord bots) as the default.
        existing_key = read_env_key("XAI_API_KEY") or read_env_key("GROK_API_KEY")
        key = prompt_text("XAI_API_KEY", secret=True, default=existing_key)
        if key:
            updates["XAI_API_KEY"] = key
        elif not existing_key:
            print(
                "\n  [WARN] No API key set. xai mode uses the API key ONLY — it will NOT\n"
                "         fall back to your SuperGrok OAuth token (different tier, may\n"
                "         not access the same models). Set XAI_API_KEY in ~/.ophelia/.env\n"
                "         or switch to OPHELIA_PROVIDER=xai-oauth."
            )
    elif provider == "deepseek":
        key = prompt_text(
            "DEEPSEEK_API_KEY",
            secret=True,
            default=read_env_key("DEEPSEEK_API_KEY"),
            hint="Get a key at https://platform.deepseek.com",
        )
        if key:
            updates["DEEPSEEK_API_KEY"] = key
        ds_model = _pick_deepseek_model()
        if ds_model:
            updates["DEEPSEEK_MODEL"] = ds_model
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

    _maybe_configure_models(provider, on_phone=on_phone)
    _maybe_configure_fallback()


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
                "Get NEW token (Hermes browser login)",
                "Import / re-sync from ~/.hermes/auth.json",
                "Import / re-sync from Grok CLI (~/.grok)",
                "Verify OAuth connection",
                "Done — use xAI OAuth",
                "Back (don't change provider)",
            ],
            description=(
                status
                + "\n\nOAuth is from xAI, not Hermes-branded."
                + "\nStale copied tokens need a fresh login (option 1)."
            ),
        )
        if pick < 0 or pick == 5:
            return False
        if pick == 0:
            _oauth_browser_login(settings)
        elif pick == 1:
            _import_hermes_oauth(settings)
        elif pick == 2:
            _import_grok_oauth(settings)
        elif pick == 3:
            _verify_xai_oauth()
        elif pick == 4:
            return True
        pause()


def _oauth_browser_login(settings: Settings) -> bool:
    import shutil

    from ophelia.platform import is_termux
    from ophelia.providers.auth import (
        print_termux_oauth_login_help,
        run_hermes_xai_oauth_login,
        sync_oauth_from_hermes_home,
    )
    from ophelia.providers.oauth_refresh import access_token_usable, load_oauth_state

    print("\n  Fresh login: Hermes opens accounts.x.ai in your browser.")
    print("  Ophelia imports the new xAI token when done.\n")
    if is_termux():
        print_termux_oauth_login_help()
        print()
    hermes = shutil.which("hermes")
    if hermes:
        pause("Press Enter to start Hermes browser login...")
        if run_hermes_xai_oauth_login() != 0:
            print("  Login cancelled or failed.\n")
            return False
    else:
        print("  Hermes not installed. Run in another tab:")
        print("    hermes auth add xai-oauth --type oauth --no-browser")
        pause("Press Enter after browser login finishes...")
    ok, msg = sync_oauth_from_hermes_home(
        settings.hermes_home,
        ophelia_auth_path=settings.hermes_auth_path,
        ophelia_oauth_path=settings.xai_oauth_token_path,
    )
    print(f"  {'[OK]' if ok else '[FAIL]'} {msg}")
    state = load_oauth_state(settings.hermes_home / "auth.json")
    if ok and state and not access_token_usable(state["access_token"]):
        print("  [WARN] Token still expired — browser callback may have failed on Termux.\n")
        return False
    print()
    return ok


def _import_hermes_oauth(settings: Settings) -> bool:
    from ophelia.providers.auth import sync_oauth_from_hermes_home

    ok, msg = sync_oauth_from_hermes_home(
        settings.hermes_home,
        ophelia_auth_path=settings.hermes_auth_path,
        ophelia_oauth_path=settings.xai_oauth_token_path,
    )
    print(f"\n  {'[OK]' if ok else '[FAIL]'} {msg}")
    if not ok:
        print("  Get a fresh token: ophelia auth login\n")
    return ok


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


def _pick_xai_model() -> str | None:
    """Model picker for xAI (Grok) chat — presets plus manual entry."""
    return _pick_model_generic(
        title="xAI (Grok) chat model",
        env_key="XAI_MODEL",
        default="grok-4",
        presets=[
            "grok-4",
            "grok-4-fast",
            "grok-4-fast-reasoning",
            "grok-4-heavy",
            "grok-3",
            "grok-3-mini",
            "grok-2",
        ],
        description="Grok models available via your SuperGrok OAuth or API key.",
    )


def _pick_openai_model() -> str | None:
    """Model picker for OpenAI chat — presets plus manual entry."""
    return _pick_model_generic(
        title="OpenAI chat model",
        env_key="OPENAI_MODEL",
        default="gpt-4o-mini",
        presets=[
            "gpt-4o-mini",
            "gpt-4o",
            "gpt-4.1-mini",
            "gpt-4.1",
            "o4-mini",
            "gpt-4-turbo",
        ],
        description="Pick the model your API key has access to.",
    )


def _pick_deepseek_model() -> str | None:
    """Model picker for DeepSeek chat — presets plus manual entry."""
    return _pick_model_generic(
        title="DeepSeek chat model",
        env_key="DEEPSEEK_MODEL",
        default="deepseek-v4-flash",
        presets=[
            "deepseek-v4-flash",
            "deepseek-v4-pro",
            "deepseek-chat",
            "deepseek-reasoner",
        ],
        description=(
            "deepseek-v4-flash is very cheap (~$0.14/1M input) — great for\n"
            "background roles and cost saving. v4-pro is more capable."
        ),
    )


def _pick_model_generic(
    *,
    title: str,
    env_key: str,
    default: str,
    presets: list[str],
    description: str = "",
) -> str | None:
    """Shared model picker: presets + current value + manual entry. None = cancel."""
    current = read_env_key(env_key) or default
    choices = list(presets)
    if current not in choices:
        choices.insert(0, current)
    choices.append("Type a model name manually...")

    selected = next((i for i, c in enumerate(choices) if c == current), 0)
    pick = radiolist(title, choices, selected=selected, description=description)
    if pick < 0:
        return None
    if choices[pick] == "Type a model name manually...":
        return prompt_text("Model name", default=current)
    return choices[pick]


# Per-provider, per-role model configuration.
# Each role maps to an env var and a preset list (or None for free-form text).
RoleKey = str  # "chat" | "consciousness" | "curator" | "vision" | "image" | "video"

_PROVIDER_ROLE_DEFS: dict[str, dict[RoleKey, dict]] = {
    "xai": {
        "chat": {
            "env": "XAI_MODEL",
            "default": "grok-4",
            "presets": ["grok-4", "grok-4-fast", "grok-4-fast-reasoning", "grok-4-heavy",
                        "grok-3", "grok-3-mini", "grok-2"],
        },
        "consciousness": {
            "env": "XAI_CONSCIOUSNESS_MODEL",
            "default": "",
            "presets": ["grok-4-fast", "grok-3-mini", "grok-4", "grok-3"],
            "optional": True,
        },
        "curator": {
            "env": "XAI_CURATOR_MODEL",
            "default": "",
            "presets": ["grok-4-fast", "grok-3-mini", "grok-4", "grok-3"],
            "optional": True,
        },
        "vision": {
            "env": "XAI_VISION_MODEL",
            "default": "",
            "presets": ["grok-4", "grok-4-fast", "grok-4-vision", "grok-2-vision"],
            "optional": True,
        },
        "image": {
            "env": "XAI_IMAGE_MODEL",
            "default": "grok-imagine-image",
            "presets": ["grok-imagine-image", "grok-2-image", "grok-image"],
        },
        "video": {
            "env": "XAI_VIDEO_MODEL",
            "default": "grok-imagine-video",
            "presets": ["grok-imagine-video", "grok-2-video"],
        },
    },
    "xai-oauth": {},  # alias: filled in below
    "openai": {
        "chat": {
            "env": "OPENAI_MODEL",
            "default": "gpt-4o-mini",
            "presets": ["gpt-4o-mini", "gpt-4o", "gpt-4.1-mini", "gpt-4.1",
                        "o4-mini", "gpt-4-turbo"],
        },
        "consciousness": {
            "env": "OPENAI_CONSCIOUSNESS_MODEL",
            "default": "",
            "presets": ["gpt-4o-mini", "gpt-4.1-mini", "o4-mini"],
            "optional": True,
        },
        "curator": {
            "env": "OPENAI_CURATOR_MODEL",
            "default": "",
            "presets": ["gpt-4o-mini", "gpt-4.1-mini", "o4-mini"],
            "optional": True,
        },
        "vision": {
            "env": "OPENAI_VISION_MODEL",
            "default": "",
            "presets": ["gpt-4o", "gpt-4o-mini", "gpt-4.1", "gpt-4-turbo"],
            "optional": True,
        },
        "image": {
            "env": "OPENAI_IMAGE_MODEL",
            "default": "dall-e-3",
            "presets": ["dall-e-3", "dall-e-2", "gpt-image-1"],
        },
        "video": None,  # OpenAI has no video gen endpoint
    },
    "deepseek": {
        "chat": {
            "env": "DEEPSEEK_MODEL",
            "default": "deepseek-v4-flash",
            "presets": ["deepseek-v4-flash", "deepseek-v4-pro",
                        "deepseek-chat", "deepseek-reasoner"],
        },
        "consciousness": {
            "env": "DEEPSEEK_CONSCIOUSNESS_MODEL",
            "default": "",
            "presets": ["deepseek-v4-flash"],
            "optional": True,
        },
        "curator": {
            "env": "DEEPSEEK_CURATOR_MODEL",
            "default": "",
            "presets": ["deepseek-v4-flash"],
            "optional": True,
        },
        "vision": None,   # DeepSeek has no vision capability
        "image": None,   # DeepSeek has no image gen
        "video": None,   # DeepSeek has no video gen
    },
    "ollama": {
        "chat": {
            "env": "OLLAMA_MODEL",
            "default": "llama3.2:3b",
            "presets": ["llama3.2:3b", "llama3.2:1b", "llama3.2", "mistral",
                        "qwen2.5:7b", "phi3:mini"],
            "dynamic": True,
        },
        "consciousness": {
            "env": "OLLAMA_CONSCIOUSNESS_MODEL",
            "default": "",
            "presets": ["llama3.2:1b", "phi3:mini", "llama3.2:3b"],
            "optional": True,
            "dynamic": True,
        },
        "curator": {
            "env": "OLLAMA_CURATOR_MODEL",
            "default": "",
            "presets": ["llama3.2:3b", "phi3:mini", "llama3.2:1b"],
            "optional": True,
            "dynamic": True,
        },
        "vision": {
            "env": "OLLAMA_VISION_MODEL",
            "default": "",
            "presets": [
                "openbmb/minicpm-v4.6", "moondream", "openbmb/minicpm-v4",
                "qwen2.5vl:3b", "llava", "llama3.2-vision", "bakllava",
            ],
            "optional": True,
            "dynamic": True,
        },
        "image": {
            "env": "OLLAMA_IMAGE_MODEL",
            "default": "",
            "presets": ["flux", "stable-diffusion", "sd3"],
            "optional": True,
            "dynamic": True,
        },
        "video": None,
    },
    "compat": {
        "chat": {
            "env": "OPHELIA_COMPAT_MODEL",
            "default": "local-model",
            "presets": [],
        },
        "consciousness": {
            "env": "OPHELIA_COMPAT_CONSCIOUSNESS_MODEL",
            "default": "",
            "presets": [],
            "optional": True,
        },
        "curator": {
            "env": "OPHELIA_COMPAT_CURATOR_MODEL",
            "default": "",
            "presets": [],
            "optional": True,
        },
        "vision": {
            "env": "OPHELIA_COMPAT_VISION_MODEL",
            "default": "",
            "presets": [],
            "optional": True,
        },
        "image": None,
        "video": None,
    },
    "auto": {},  # no direct model config; inherits primary provider
}
_PROVIDER_ROLE_DEFS["xai-oauth"] = _PROVIDER_ROLE_DEFS["xai"]

_ROLE_LABELS = {
    "chat": "Chat / main replies",
    "consciousness": "Consciousness (background ticks)",
    "curator": "Memory curator",
    "vision": "Vision (photo understanding)",
    "image": "Image generation",
    "video": "Video generation",
}


_PROVIDER_ENV_BY_ROLE: dict[RoleKey, str] = {
    "chat": "OPHELIA_PROVIDER_CHAT",
    "consciousness": "OPHELIA_PROVIDER_CONSCIOUSNESS",
    "curator": "OPHELIA_PROVIDER_CURATOR",
    "vision": "OPHELIA_PROVIDER_VISION",
    "image": "OPHELIA_PROVIDER_IMAGE",
    "video": "OPHELIA_PROVIDER_VIDEO",
}

# Which providers can serve each role. "auto" = inherit primary provider.
_ROLE_PROVIDER_OPTIONS: dict[RoleKey, list[str]] = {
    "chat": ["auto", "ollama", "xai-oauth", "xai", "deepseek", "openai", "compat"],
    "consciousness": ["auto", "ollama", "xai-oauth", "xai", "deepseek", "openai", "compat"],
    "curator": ["auto", "ollama", "xai-oauth", "xai", "deepseek", "openai", "compat"],
    "vision": ["auto", "ollama", "xai-oauth", "xai", "openai", "compat"],
    "image": [
        "auto",
        "ollama",
        "xai-oauth",
        "xai",
        "openai",
        "pollinations",
        "a1111",
        "comfyui",
        "fal",
        "replicate",
        "civitai",
        "modelslab",
    ],
    "video": ["auto", "xai-oauth", "xai"],
}


def _role_summary(provider: str, role: RoleKey) -> str:
    """One-line current value for a role: provider + model."""
    role_def = _PROVIDER_ROLE_DEFS.get(provider, {}).get(role)
    if not role_def:
        return "(not available for this provider)"

    # Provider for this role (env override or inherited primary)
    prov_env = _PROVIDER_ENV_BY_ROLE[role]
    prov_val = read_env_key(prov_env)
    if prov_val and prov_val.lower() != "auto":
        prov_label = prov_val.lower()
    else:
        primary = read_env_key("OPHELIA_PROVIDER") or "auto"
        prov_label = f"{primary.lower()} (inherited)" if primary.lower() != "auto" else "auto"

    # Model for this role
    env_val = read_env_key(role_def["env"])
    if env_val:
        model_label = env_val
    else:
        default = role_def.get("default", "")
        if default:
            model_label = f"{default} (default)"
        elif role_def.get("optional"):
            model_label = "(inherits chat model)"
        else:
            model_label = "(not set)"

    return f"[{prov_label}] {model_label}"


def _maybe_configure_models(provider: str, *, on_phone: bool) -> None:
    """Offer the per-role Models sub-menu after a provider is selected."""
    role_defs = _PROVIDER_ROLE_DEFS.get(provider)
    if not role_defs:
        return  # auto / unknown — nothing to configure here

    pick = radiolist(
        "Configure specific providers/models for each role?",
        [
            "Yes — pick provider + model per role (chat, vision, image, video, ...)",
            "Skip — keep current / default settings",
        ],
        selected=1,
        description=(
            "Ophelia uses six roles, each with its own provider and model:\n"
            "  chat, consciousness, curator, vision, image, video.\n"
            "You can point each role at a different provider (e.g. image on xAI\n"
            "  API key while chat stays on OAuth) and pick its model independently."
        ),
    )
    if pick != 0:
        return

    _models_menu(provider, on_phone=on_phone)


def _maybe_configure_fallback() -> None:
    """Offer to set up a fallback provider chain for transient failures."""
    current = read_env_key("OPHELIA_FALLBACK_PROVIDERS")
    pick = radiolist(
        "Set up a fallback provider?",
        [
            "Yes — if the primary fails, retry on a cheaper backup (e.g. DeepSeek)",
            "Skip — no fallback (fail immediately on errors)",
        ],
        selected=1 if not current else 0,
        description=(
            "If the primary provider hits a rate limit, 5xx, or network error,\n"
            "Ophelia retries on each fallback provider in order before giving up.\n"
            "Great for cost: run Grok as primary, DeepSeek V4 Flash as a cheap\n"
            "  backup. Only transient errors trigger fallback (a 400 is not retried)."
        ),
    )
    if pick != 0:
        return

    # Which providers are available as fallbacks (have credentials set)?
    available = []
    if read_env_key("DEEPSEEK_API_KEY"):
        available.append("deepseek")
    if read_env_key("XAI_API_KEY") or read_env_key("GROK_API_KEY"):
        available.append("xai")
    # OAuth availability is harder to detect from env alone; offer it anyway.
    available.append("xai-oauth")
    if read_env_key("OPENAI_API_KEY"):
        available.append("openai")
    if read_env_key("OPHELIA_COMPAT_API_KEY") and read_env_key("OPHELIA_COMPAT_BASE_URL"):
        available.append("compat")
    available.append("ollama")

    if not available:
        print("\n  No providers with credentials detected. Configure one first.")
        return

    # Let the user toggle which fallbacks to enable, in order.
    chosen = checkbox(
        "Pick fallback providers (order matters — tried top to bottom)",
        available,
        selected={i for i, p in enumerate(available) if current and p in current},
        description=(
            "Toggle the providers to use as fallbacks. They're tried in the\n"
            "order listed. The primary provider is never re-tried as a fallback."
        ),
    )
    if not chosen:
        return
    ordered = [available[i] for i in sorted(chosen)]
    updates: dict[str, str | None] = {"OPHELIA_FALLBACK_PROVIDERS": ",".join(ordered)}

    # Optional: a single model to use on every fallback (e.g. always deepseek-v4-flash).
    fb_model = prompt_text(
        "OPHELIA_FALLBACK_MODEL (optional — same model on every fallback)",
        default=read_env_key("OPHELIA_FALLBACK_MODEL") or "",
        hint="Leave blank to use each fallback provider's own default model",
    )
    if fb_model:
        updates["OPHELIA_FALLBACK_MODEL"] = fb_model
    else:
        updates["OPHELIA_FALLBACK_MODEL"] = None

    touched = write_env_updates(updates)
    print(f"\n  Saved: {', '.join(touched)}")
    print(f"  Fallback chain: {' -> '.join(ordered)}")


def _models_menu(provider: str, *, on_phone: bool) -> None:
    role_defs = _PROVIDER_ROLE_DEFS[provider]
    role_order = ["chat", "consciousness", "curator", "vision", "image", "video"]
    available_roles = [r for r in role_order if role_defs.get(r) is not None]

    while True:
        items = [f"{_ROLE_LABELS[r]}  —  {_role_summary(provider, r)}" for r in available_roles]
        items.append("Clear all role overrides (use defaults)")
        items.append("Back")
        pick = radiolist(
            f"Roles — {provider}",
            items,
            selected=0,
            description=(
                "Pick a role to choose its provider AND model.\n"
                "Each role can use a different backend — e.g. image gen on xAI\n"
                "  API key while chat stays on OAuth. 'auto' inherits the primary."
            ),
        )
        if pick < 0 or pick == len(items) - 1:
            return  # back / cancel
        if pick == len(items) - 2:
            _clear_role_overrides(provider)
            pause()
            continue
        role = available_roles[pick]
        _pick_role_model(provider, role, on_phone=on_phone)
        pause()


def _pick_role_model(provider: str, role: RoleKey, *, on_phone: bool) -> None:
    """Pick the PROVIDER and MODEL for a single role."""
    # --- Step 1: provider for this role ---
    prov_env = _PROVIDER_ENV_BY_ROLE[role]
    current_prov = read_env_key(prov_env) or "auto"
    prov_options = _ROLE_PROVIDER_OPTIONS.get(role, ["auto"])
    prov_labels = []
    for p in prov_options:
        if p == current_prov:
            prov_labels.append(f"{p} (current)")
        else:
            prov_labels.append(p)
    # Map label -> provider value
    prov_pick = radiolist(
        f"{_ROLE_LABELS[role]} — choose provider",
        prov_labels,
        selected=next(
            (i for i, p in enumerate(prov_options) if p == current_prov), 0
        ),
        description=(
            "Which backend should run this role?\n"
            "auto = inherit the primary provider set on the main menu."
        ),
    )
    if prov_pick < 0:
        return
    chosen_prov = prov_options[prov_pick]
    if chosen_prov == "auto":
        write_env_updates({prov_env: None})
        print(f"\n  Cleared {prov_env} — role will inherit primary provider.")
    else:
        write_env_updates({prov_env: chosen_prov})
        print(f"\n  Saved {prov_env}={chosen_prov}")
    # Use the chosen provider for the model presets that follow.
    effective_provider = chosen_prov if chosen_prov != "auto" else provider

    # --- Step 2: model for this role, under the chosen provider ---
    role_def = _PROVIDER_ROLE_DEFS.get(effective_provider, {}).get(role)
    if not role_def:
        print(f"\n  No model presets for {effective_provider}/{role} — set via .env if needed.")
        pause()
        return

    env_key = role_def["env"]
    default = role_def.get("default", "")
    presets = list(role_def.get("presets", []))
    optional = role_def.get("optional", False)

    # For Ollama chat/vision, augment presets with what's actually pulled.
    if role_def.get("dynamic") and effective_provider == "ollama":
        pulled = _list_ollama_model_names()
        for m in pulled[:12]:
            if m not in presets:
                presets.insert(0, m)

    current = read_env_key(env_key) or default
    choices: list[str] = []
    if current and current not in choices:
        choices.append(current)
    for p in presets:
        if p not in choices:
            choices.append(p)
    if optional:
        choices.append("(inherit chat model / clear override)")
    choices.append("Type a model name manually...")

    selected = next((i for i, c in enumerate(choices) if c == current), 0)
    title = f"{_ROLE_LABELS[role]} — model ({effective_provider})"
    desc = "Optional: leave unset to inherit the chat model." if optional else ""
    pick = radiolist(title, choices, selected=selected, description=desc)
    if pick < 0:
        return

    chosen = choices[pick]
    if chosen == "Type a model name manually...":
        typed = prompt_text("Model name", default=current)
        if typed:
            write_env_updates({env_key: typed})
            print(f"\n  Saved {env_key}={typed}")
    elif chosen == "(inherit chat model / clear override)":
        write_env_updates({env_key: None})
        print(f"\n  Cleared {env_key} — will inherit chat model.")
    else:
        write_env_updates({env_key: chosen})
        print(f"\n  Saved {env_key}={chosen}")


def _clear_role_overrides(provider: str) -> None:
    """Clear all per-role provider AND model overrides."""
    updates: dict[str, str | None] = {}
    role_order = ["chat", "consciousness", "curator", "vision", "image", "video"]
    for role in role_order:
        # Clear provider override for every role.
        prov_env = _PROVIDER_ENV_BY_ROLE[role]
        if read_env_key(prov_env):
            updates[prov_env] = None
        # Clear model override for roles of the current provider.
        role_def = _PROVIDER_ROLE_DEFS.get(provider, {}).get(role)
        if role_def and role != "chat":
            env_key = role_def["env"]
            if read_env_key(env_key):
                updates[env_key] = None
    if not updates:
        print("\n  No role overrides to clear.")
        return
    touched = write_env_updates(updates)
    print(f"\n  Cleared: {', '.join(touched)}")


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

    # Explicit owner identity — declare it now so owner status doesn't depend
    # on list-ordering of the allowlist (a footgun that demotes the owner to
    # guest when a second platform is added). Default to channel form built
    # from the IDs just entered, on whichever platforms are enabled.
    default_owner = read_env_key("OPHELIA_OWNER_ID") or ""
    if not default_owner:
        parts: list[str] = []
        tg = updates.get("TELEGRAM_ALLOWED_USER_IDS") or read_env_key("TELEGRAM_ALLOWED_USER_IDS")
        dc = updates.get("DISCORD_ALLOWED_USER_IDS") or read_env_key("DISCORD_ALLOWED_USER_IDS")
        # Use only the first id on each platform (owner is position 0).
        if tg and updates.get("OPHELIA_TELEGRAM_ENABLED") != "false":
            parts.append(f"telegram:{tg.split(',')[0].strip()}")
        if dc and updates.get("OPHELIA_DISCORD_ENABLED") != "false":
            parts.append(f"discord:{dc.split(',')[0].strip()}")
        default_owner = ",".join(parts)
    owner_id = prompt_text(
        "OPHELIA_OWNER_ID (channel:id — your identity as owner)",
        default=default_owner,
        hint="Comma-separated, e.g. telegram:111,discord:222 — sets who shapes her. "
             "Leave as default unless you know better.",
    )
    if owner_id:
        updates["OPHELIA_OWNER_ID"] = owner_id

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


def _section_web_search() -> None:
    """Configure web search backend.

    Grok has built-in live search, but DeepSeek / OpenAI / Ollama do not — so
    when running on those providers Ophelia relies on the web_search tool.
    DuckDuckGo works with no key (less reliable); Tavily / Serper / Brave need
    an API key and are far more reliable for AI use.
    """
    current = (read_env_key("OPHELIA_WEB_SEARCH_PROVIDER") or "auto").strip().lower()
    has_tavily = bool(read_env_key("TAVILY_API_KEY"))
    has_serper = bool(read_env_key("SERPER_API_KEY"))
    has_brave = bool(read_env_key("BRAVE_API_KEY"))

    options = [
        ("auto", "Auto (use the first API key I have, else DuckDuckGo)"),
        ("duckduckgo", "DuckDuckGo (free — no API key, less reliable)"),
        ("tavily", "Tavily (AI-focused — free tier, reliable)" + ("  [key set]" if has_tavily else "")),
        ("serper", "Serper / Google results (reliable)" + ("  [key set]" if has_serper else "")),
        ("brave", "Brave Search API (reliable)" + ("  [key set]" if has_brave else "")),
    ]
    labels = [label for _, label in options]
    default = next((i for i, (k, _) in enumerate(options) if k == current), 0)

    pick = radiolist(
        "Choose web search backend",
        labels,
        selected=default,
        description=(
            "Grok has built-in web search; DeepSeek/OpenAI/Ollama do not.\n"
            "Pick a keyed backend (Tavily/Serper/Brave) for reliable results,\n"
            "  or DuckDuckGo for a free no-key option. 'Auto' uses the first\n"
            "  API key you've set, falling back to DuckDuckGo."
        ),
    )
    if pick < 0:
        return
    provider = options[pick][0]
    updates: dict[str, str | None] = {
        "OPHELIA_WEB_SEARCH": "true",
        "OPHELIA_WEB_SEARCH_PROVIDER": provider,
    }

    if provider in ("tavily", "auto"):
        key = prompt_text(
            "TAVILY_API_KEY (optional — get one at https://tavily.com)",
            secret=True,
            default=read_env_key("TAVILY_API_KEY") or "",
        )
        if key:
            updates["TAVILY_API_KEY"] = key
    if provider in ("serper", "auto"):
        key = prompt_text(
            "SERPER_API_KEY (optional — get one at https://serper.dev)",
            secret=True,
            default=read_env_key("SERPER_API_KEY") or "",
        )
        if key:
            updates["SERPER_API_KEY"] = key
    if provider in ("brave", "auto"):
        key = prompt_text(
            "BRAVE_API_KEY (optional — get one at https://brave.com/search/api/)",
            secret=True,
            default=read_env_key("BRAVE_API_KEY") or "",
        )
        if key:
            updates["BRAVE_API_KEY"] = key

    touched = write_env_updates(updates)
    print(f"\n  Saved: {', '.join(touched)}")
    resolved = provider
    if resolved == "auto":
        if updates.get("TAVILY_API_KEY") or has_tavily:
            resolved = "tavily"
        elif updates.get("SERPER_API_KEY") or has_serper:
            resolved = "serper"
        elif updates.get("BRAVE_API_KEY") or has_brave:
            resolved = "brave"
        else:
            resolved = "duckduckgo"
    print(f"  Active web search backend: {resolved}")


_IMAGE_BACKEND_OPTIONS = [
    ("auto", "Auto (local Ollama if a model is pulled, else cloud, else Pollinations free)"),
    ("pollinations", "Pollinations (free, no API key — lax on NSFW)"),
    ("a1111", "Automatic1111 / SDWebUI (local, uncensored, LoRAs — best local quality)"),
    ("comfyui", "ComfyUI (local, uncensored — most control)"),
    ("ollama", "Ollama image model (local)"),
    ("xai-oauth", "xAI Grok Imagine (OAuth) — censored"),
    ("xai", "xAI Grok Imagine (API key) — censored"),
    ("openai", "OpenAI DALL-E — censored"),
    ("fal", "fal.ai (fast cloud — NSFW-tolerant flux/sdxl)"),
    ("replicate", "Replicate (cloud — many NSFW-allowed models)"),
    ("civitai", "Civitai (NSFW checkpoints/LoRAs, generation API)"),
    ("modelslab", "ModelsLab (hosted SD — explicit/adult models)"),
]


def _section_image_backends() -> None:
    """Pick the image-generation backend and configure its credentials.

    Several backends are NSFW-capable (pollinations, a1111, comfyui, fal,
    replicate, civitai, modelslab, ollama). xAI/OpenAI are NOT — explicit
    prompts are auto-routed away from them when the NSFW tier is enabled.
    """
    current = (read_env_key("OPHELIA_PROVIDER_IMAGE") or "auto").strip().lower()
    labels = [label for _, label in _IMAGE_BACKEND_OPTIONS]
    default = next(
        (i for i, (k, _) in enumerate(_IMAGE_BACKEND_OPTIONS) if k == current), 0
    )
    pick = radiolist(
        "Choose image generation backend",
        labels,
        selected=default,
        description=(
            "Backends marked 'censored' (xAI/OpenAI) refuse explicit prompts.\n"
            "  For NSFW, pick an uncensored backend AND enable the NSFW tier below.\n"
            "  Pollinations is free and needs no key — image gen works out of the box."
        ),
    )
    if pick < 0:
        return
    provider = _IMAGE_BACKEND_OPTIONS[pick][0]
    updates: dict[str, str | None] = {"OPHELIA_PROVIDER_IMAGE": provider}

    if provider == "pollinations":
        m = prompt_text(
            "POLLINATIONS_IMAGE_MODEL",
            default=read_env_key("POLLINATIONS_IMAGE_MODEL") or "flux",
            hint="flux, turbo, sdxl, flux-realism, ...",
        )
        if m:
            updates["POLLINATIONS_IMAGE_MODEL"] = m
    elif provider == "a1111":
        base = prompt_text(
            "A1111_BASE_URL",
            default=read_env_key("A1111_BASE_URL") or "http://127.0.0.1:7860",
            hint="Run SDWebUI with --api --listen --port 7860",
        )
        updates["A1111_BASE_URL"] = base
        key = prompt_text(
            "A1111_API_KEY (optional — only if --api-auth is set)",
            secret=True,
            default=read_env_key("A1111_API_KEY") or "",
        )
        if key:
            updates["A1111_API_KEY"] = key
        ckpt = prompt_text(
            "A1111_IMAGE_MODEL (optional checkpoint name, blank = webUI default)",
            default=read_env_key("A1111_IMAGE_MODEL") or "",
        )
        updates["A1111_IMAGE_MODEL"] = ckpt or None
    elif provider == "comfyui":
        base = prompt_text(
            "COMFYUI_BASE_URL",
            default=read_env_key("COMFYUI_BASE_URL") or "http://127.0.0.1:8188",
        )
        updates["COMFYUI_BASE_URL"] = base
        ckpt = prompt_text(
            "COMFYUI_IMAGE_MODEL (optional checkpoint filename, blank = default graph)",
            default=read_env_key("COMFYUI_IMAGE_MODEL") or "",
        )
        updates["COMFYUI_IMAGE_MODEL"] = ckpt or None
    elif provider == "fal":
        key = prompt_text(
            "FAL_API_KEY (get one at https://fal.ai/dashboard/keys)",
            secret=True,
            default=read_env_key("FAL_API_KEY") or "",
        )
        if key:
            updates["FAL_API_KEY"] = key
        m = prompt_text(
            "FAL_IMAGE_MODEL",
            default=read_env_key("FAL_IMAGE_MODEL") or "fal-ai/fast-sdxl",
            hint="fal-ai/fast-sdxl, fal-ai/flux/dev, fal-ai/flux/schnell, ...",
        )
        if m:
            updates["FAL_IMAGE_MODEL"] = m
    elif provider == "replicate":
        key = prompt_text(
            "REPLICATE_API_KEY (get one at https://replicate.com/account/api-tokens)",
            secret=True,
            default=read_env_key("REPLICATE_API_KEY") or "",
        )
        if key:
            updates["REPLICATE_API_KEY"] = key
        m = prompt_text(
            "REPLICATE_IMAGE_MODEL",
            default=read_env_key("REPLICATE_IMAGE_MODEL") or "stability-ai/sdxl",
            hint="owner/model or owner/model:version — e.g. stability-ai/sdxl",
        )
        if m:
            updates["REPLICATE_IMAGE_MODEL"] = m
    elif provider == "civitai":
        key = prompt_text(
            "CIVITAI_API_KEY (account -> API keys)",
            secret=True,
            default=read_env_key("CIVITAI_API_KEY") or "",
        )
        if key:
            updates["CIVITAI_API_KEY"] = key
        m = prompt_text(
            "CIVITAI_IMAGE_MODEL (optional URN, blank = engine default flux)",
            default=read_env_key("CIVITAI_IMAGE_MODEL") or "",
            hint="urn:air:sdxl:checkpoint:civitai:101055@128078",
        )
        updates["CIVITAI_IMAGE_MODEL"] = m or None
    elif provider == "modelslab":
        key = prompt_text(
            "MODELSLAB_API_KEY (https://modelslab.com/dashboard/api-keys)",
            secret=True,
            default=read_env_key("MODELSLAB_API_KEY") or "",
        )
        if key:
            updates["MODELSLAB_API_KEY"] = key
        m = prompt_text(
            "MODELSLAB_IMAGE_MODEL",
            default=read_env_key("MODELSLAB_IMAGE_MODEL") or "flux",
            hint="flux, sdxl, ...",
        )
        if m:
            updates["MODELSLAB_IMAGE_MODEL"] = m
    elif provider == "ollama":
        m = prompt_text(
            "OLLAMA_IMAGE_MODEL",
            default=read_env_key("OLLAMA_IMAGE_MODEL") or "",
            hint="ollama pull flux (or another image-capable model)",
        )
        updates["OLLAMA_IMAGE_MODEL"] = m or None

    # NSFW content tier
    nsfw_now = (read_env_key("OPHELIA_IMAGE_NSFW_ALLOWED") or "").lower() in (
        "1", "true", "yes", "on",
    )
    pick_n = radiolist(
        "Enable NSFW content tier? (allows explicit prompts; routes them to an "
        "uncensored backend — never xAI/OpenAI)",
        ["No (refuse explicit image requests)", "Yes (allow, route to uncensored)"],
        selected=1 if nsfw_now else 0,
    )
    want_nsfw = pick_n == 1
    updates["OPHELIA_IMAGE_NSFW_ALLOWED"] = "true" if want_nsfw else "false"
    if want_nsfw:
        nsfw_prov = prompt_text(
            "OPHELIA_IMAGE_NSFW_PROVIDER (auto = first configured uncensored)",
            default=read_env_key("OPHELIA_IMAGE_NSFW_PROVIDER") or "auto",
        )
        updates["OPHELIA_IMAGE_NSFW_PROVIDER"] = nsfw_prov or "auto"

    touched = write_env_updates(updates)
    print(f"\n  Saved: {', '.join(touched)}")
    print(f"  Active image backend: {provider}")
    if want_nsfw:
        s = Settings()
        print(f"  NSFW tier: ON -> explicit requests route to "
              f"{s.image_nsfw_provider_resolved()}")
    else:
        print("  NSFW tier: OFF (explicit image requests will be refused)")


def _section_tts() -> None:
    """Pick the TTS backend used for voice replies / text_to_speech."""
    current = (read_env_key("OPHELIA_TTS_PROVIDER") or "auto").strip().lower()
    options = [
        ("auto", "Auto (first configured: ElevenLabs -> Kokoro -> OpenAI -> xAI)"),
        ("elevenlabs", "ElevenLabs (cloud — best quality, free tier available)"),
        ("kokoro", "Kokoro-82M (LOCAL server — offline, natural voices)"),
        ("openai", "OpenAI TTS (cloud — gpt-4o-mini-tts)"),
        ("xai", "xAI Grok TTS (cloud — legacy default)"),
    ]
    labels = [label for _, label in options]
    default = next((i for i, (k, _) in enumerate(options) if k == current), 0)
    pick = radiolist(
        "Choose TTS backend (voice replies + text_to_speech tool)",
        labels,
        selected=default,
        description=(
            "All backends work from Termux — cloud ones are just HTTP calls.\n"
            "  Kokoro runs fully offline via a small local server (see README\n"
            "  'Better voices' for the Termux install steps)."
        ),
    )
    if pick < 0:
        return
    provider = options[pick][0]
    updates: dict[str, str | None] = {"OPHELIA_TTS_PROVIDER": provider}

    if provider == "elevenlabs":
        key = prompt_text(
            "ELEVENLABS_API_KEY (https://elevenlabs.io -> profile -> API keys)",
            secret=True,
            default=read_env_key("ELEVENLABS_API_KEY") or "",
        )
        if key:
            updates["ELEVENLABS_API_KEY"] = key
        voice = prompt_text(
            "ELEVENLABS_VOICE_ID",
            default=read_env_key("ELEVENLABS_VOICE_ID") or "21m00Tcm4TlvDq8ikWAM",
            hint="Voice ID from the ElevenLabs voice library (default = Rachel)",
        )
        if voice:
            updates["ELEVENLABS_VOICE_ID"] = voice
    elif provider == "kokoro":
        base = prompt_text(
            "KOKORO_TTS_URL",
            default=read_env_key("KOKORO_TTS_URL") or "http://127.0.0.1:8880/v1",
            hint="Kokoro-FastAPI or `koko openai` server base URL",
        )
        updates["KOKORO_TTS_URL"] = base
        voice = prompt_text(
            "KOKORO_TTS_VOICE",
            default=read_env_key("KOKORO_TTS_VOICE")
            or "af_heart(0.45)+af_bella(0.35)+bf_emma(0.2)",
            hint="preset or mix: af_heart, af_bella(0.7)+bf_emma(0.3), ...",
        )
        if voice:
            updates["KOKORO_TTS_VOICE"] = voice
        spd = prompt_text(
            "KOKORO_TTS_SPEED",
            default=read_env_key("KOKORO_TTS_SPEED") or "1.0",
            hint="0.85 thoughtful, 1.0 normal, 1.15 hyped",
        )
        if spd:
            updates["KOKORO_TTS_SPEED"] = spd
    elif provider == "openai":
        if not read_env_key("OPENAI_API_KEY"):
            key = prompt_text(
                "OPENAI_API_KEY",
                secret=True,
                default="",
            )
            if key:
                updates["OPENAI_API_KEY"] = key
        voice = prompt_text(
            "OPENAI_TTS_VOICE",
            default=read_env_key("OPENAI_TTS_VOICE") or "nova",
            hint="alloy, echo, fable, nova, onyx, shimmer, coral, sage",
        )
        if voice:
            updates["OPENAI_TTS_VOICE"] = voice
    elif provider == "xai":
        voice = prompt_text(
            "XAI_TTS_VOICE",
            default=read_env_key("XAI_TTS_VOICE") or "eve",
            hint="eve, ara, rex",
        )
        if voice:
            updates["XAI_TTS_VOICE"] = voice

    touched = write_env_updates(updates)
    print(f"\n  Saved: {', '.join(touched)}")
    print(f"  Active TTS backend: {provider}")
    if provider == "kokoro":
        print("  Reminder: Kokoro-FastAPI on PC gives voice mixing + [pause:1s] expressions.")
        print("  Termux Kokoros gives preset voices only. See README.")


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
