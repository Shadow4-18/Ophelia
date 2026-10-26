"""`ophelia setup`: one menu per topic, each showing what's set right now.

Every screen edits only what you change. Values are checked against
config.Settings before they're written, and a broken .env is offered for
repair before anything else runs (a bad value used to stop setup itself
from opening).
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable

from ophelia.config import OPHELIA_HOME, Settings, ensure_dirs
from ophelia.platform import is_termux, platform_summary
from ophelia.setup import fields as F
from ophelia.setup.env_io import env_path, read_env_key, write_env_updates
from ophelia.setup.fields import Act, Opt, settings_screen
from ophelia.setup.tui import (
    Header,
    checkbox_or_none,
    confirm,
    menu,
    pause,
    prompt_text,
    radiolist,
)
from ophelia.setup.wizard import _auto_setup, run_setup_wizard

_TRUE = ("1", "true", "yes", "on")
_MANUAL = "Type a model name..."


def _on(key: str, default: bool = False) -> bool:
    val = read_env_key(key)
    return default if not val else val.lower() in _TRUE


def _save(updates: dict[str, str | None]) -> None:
    touched = write_env_updates(updates)
    if touched:
        print(f"\n  Saved: {', '.join(touched)}")


def _load_settings() -> Settings:
    """Settings from the .env these menus edit (honours OPHELIA_HOME)."""
    return Settings(_env_file=env_path())


def _settings() -> Settings | None:
    try:
        return _load_settings()
    except Exception:
        return None


# --- entry -------------------------------------------------------------------


def run_interactive_setup(*, phone: bool | None = None) -> int:
    on_phone = is_termux() if phone is None else phone
    _repair_broken_settings()
    ensure_dirs(_load_settings())

    print()
    print("=" * 52)
    print("  The Ophelia Project — Setup")
    print(f"  Platform: {platform_summary()}")
    print(f"  Home:     {OPHELIA_HOME}")
    print("=" * 52)
    for action in _auto_setup():
        print(f"  + {action}")

    sections = _sections(on_phone)
    cursor = 0
    while True:
        rows: list[str] = []
        width = max(len(s[0]) for s in sections if not isinstance(s, Header)) + 2
        for s in sections:
            if isinstance(s, Header):
                rows.append(s)
                continue
            label, summary, _ = s
            try:
                value = summary() if summary else ""
            except Exception:
                value = ""
            rows.append(f"{label.ljust(width)}{value}" if value else label)
        pick = menu(
            "Ophelia setup",
            rows,
            selected=cursor,
            description="Pick a topic. Each screen shows what's set now and saves as you go.",
        )
        if pick < 0:
            break
        cursor = pick
        _, _, handler = sections[pick]
        if handler is None:
            break
        try:
            handler()
        except KeyboardInterrupt:
            print()

    print()
    print("Setup saved. Next:")
    print("  ophelia check")
    if on_phone:
        print("  ophelia            (menu → Start Ophelia)")
    else:
        print("  ophelia ui   or   ophelia run")
    print("Restart her if she's already running so the changes apply.")
    print()
    return 0


def _sections(on_phone: bool) -> list:
    return [
        Header("— Her mind —"),
        ("AI & models", _summary_ai, lambda: _section_ai(on_phone)),
        ("Eyes (vision)", _summary_vision, lambda: _section_vision(on_phone)),
        ("Web search", _summary_search, _section_web_search),
        ("Personality & autonomy", _summary_autonomy, _section_autonomy),
        ("Persona (SOUL.md)", _summary_persona, _section_persona),
        Header("— Her body —"),
        ("Images (SFW + NSFW / Civitai)", _summary_images, _section_image_backends),
        ("Voice & listening", _summary_voice, _section_voice),
        ("Phone body & games", _summary_phone, lambda: _section_phone_body(on_phone)),
        Header("— Her life —"),
        ("Schedule, sleep & timezone", _summary_schedule, _section_schedule),
        ("Chat channels (Telegram / Discord)", _summary_channels, _section_channels),
        ("Memory & backups", _summary_memory, _section_memory),
        ("Public site / Cloudflare Pages", _summary_site, _section_public_site),
        Header("— Check —"),
        ("Run health check", None, _section_health_check),
        ("Show checklist status", None, lambda: _show_checklist(on_phone)),
        ("Finish", None, None),
    ]


def _show_checklist(on_phone: bool) -> None:
    print()
    run_setup_wizard(phone=on_phone, checklist=True, do_auto=False)
    pause()


def _repair_broken_settings() -> None:
    """A value Settings rejects stops every command, setup included. List
    them and offer to reset each one to its default."""
    from pydantic import ValidationError

    try:
        _load_settings()
        return
    except ValidationError as e:
        errors = e.errors()
    print("\n  Some saved settings are invalid, so Ophelia can't start:")
    for err in errors:
        key = str(err["loc"][0]) if err.get("loc") else "?"
        print(f"    {key}={err.get('input')!r}: {err.get('msg')}")
    for err in errors:
        key = str(err["loc"][0]) if err.get("loc") else ""
        if not key or not read_env_key(key):
            continue
        ok = confirm(f"Reset {key} to its default?", default=True,
                     description=f"Current value {err.get('input')!r}: {err.get('msg')}")
        if ok:
            write_env_updates({key: None})
            print(f"  Reset {key}.")


# --- summaries for the main menu ------------------------------------------


def _summary_ai() -> str:
    prov = (read_env_key("OPHELIA_PROVIDER") or "auto").lower()
    env = _ROLE_ENV.get(prov, {}).get("chat")
    model = (read_env_key(env) if env else "") or _DEFAULT_CHAT.get(prov, "")
    return f"{prov}{' / ' + model if model else ''}"


def _summary_vision() -> str:
    s = _settings()
    if s is None:
        return ""
    from ophelia.providers.router import build_provider_stack

    stack = build_provider_stack(s)
    if not stack.supports_vision():
        return "none set up"
    return f"{stack.name('vision')} / {stack.model('vision')}"


def _summary_search() -> str:
    s = _settings()
    if s is None:
        return ""
    return s.web_search_provider_resolved() if s.web_search_enabled else "off"


def _summary_autonomy() -> str:
    return "thinking on her own" if _on("OPHELIA_CONSCIOUSNESS", True) else "only replies"


def _summary_persona() -> str:
    return "SOUL.md set" if (OPHELIA_HOME / "SOUL.md").is_file() else "missing"


def _summary_images() -> str:
    sfw = (read_env_key("OPHELIA_PROVIDER_IMAGE") or "auto").lower()
    nsfw = (read_env_key("OPHELIA_IMAGE_NSFW_PROVIDER") or "auto").lower() if _on(
        "OPHELIA_IMAGE_NSFW_ALLOWED") else "off"
    return f"SFW {sfw}, NSFW {nsfw}"


def _summary_voice() -> str:
    return (read_env_key("OPHELIA_TTS_PROVIDER") or "auto").lower()


def _summary_phone() -> str:
    val = read_env_key("OPHELIA_ANDROID_ENABLED")
    if not val:
        return "auto"
    return "on" if val.lower() in _TRUE else "off"


def _summary_schedule() -> str:
    tz = read_env_key("OPHELIA_TIMEZONE") or "system"
    sleep = read_env_key("OPHELIA_SLEEP_HOURS")
    return f"{tz}{', sleeps ' + sleep if sleep else ''}"


def _summary_channels() -> str:
    on = [n for n, k in (("Telegram", "TELEGRAM_BOT_TOKEN"), ("Discord", "DISCORD_BOT_TOKEN"))
          if read_env_key(k)]
    return ", ".join(on) or "none"


def _summary_memory() -> str:
    return "backups on" if _on("OPHELIA_BACKUP", True) else "backups off"


def _summary_site() -> str:
    return read_env_key("OPHELIA_SITE_PUBLIC_URL") or ("on" if _on("OPHELIA_SITE_ENABLED", True) else "off")


# --- providers and models ---------------------------------------------------

RoleKey = str  # chat | consciousness | reaction | curator | vision | video

_PROFILES = [
    ("deepseek", "DeepSeek — cheap cloud chat; Flash can also see"),
    ("xai-oauth", "Grok via SuperGrok login"),
    ("xai", "Grok via API key"),
    ("openai", "OpenAI"),
    ("ollama", "Local Ollama — free and offline, slow on a phone"),
    ("compat", "Other OpenAI-compatible service (OpenRouter, LM Studio, ...)"),
    ("auto", "Auto — local Ollama if it's running, else a cloud key"),
]

_ROLE_LABELS = {
    "chat": "Chat (her replies)",
    "consciousness": "Background thinking",
    "reaction": "Quick reactions",
    "curator": "Memory curator",
    "vision": "Vision (photos + phone screen)",
    "video": "Video generation",
}

_PROVIDER_ENV_BY_ROLE = {
    "chat": "OPHELIA_PROVIDER_CHAT",
    "consciousness": "OPHELIA_PROVIDER_CONSCIOUSNESS",
    "reaction": "OPHELIA_PROVIDER_REACTION",
    "curator": "OPHELIA_PROVIDER_CURATOR",
    "vision": "OPHELIA_PROVIDER_VISION",
    "video": "OPHELIA_PROVIDER_VIDEO",
}

_ROLE_ENV: dict[str, dict[RoleKey, str]] = {
    "xai": {"chat": "XAI_MODEL", "consciousness": "XAI_CONSCIOUSNESS_MODEL",
            "reaction": "XAI_REACTION_MODEL", "curator": "XAI_CURATOR_MODEL",
            "vision": "XAI_VISION_MODEL", "video": "XAI_VIDEO_MODEL"},
    "openai": {"chat": "OPENAI_MODEL", "consciousness": "OPENAI_CONSCIOUSNESS_MODEL",
               "reaction": "OPENAI_REACTION_MODEL", "curator": "OPENAI_CURATOR_MODEL",
               "vision": "OPENAI_VISION_MODEL"},
    "deepseek": {"chat": "DEEPSEEK_MODEL", "consciousness": "DEEPSEEK_CONSCIOUSNESS_MODEL",
                 "reaction": "DEEPSEEK_REACTION_MODEL", "curator": "DEEPSEEK_CURATOR_MODEL",
                 "vision": "DEEPSEEK_VISION_MODEL"},
    "ollama": {"chat": "OLLAMA_MODEL", "consciousness": "OLLAMA_CONSCIOUSNESS_MODEL",
               "reaction": "OLLAMA_REACTION_MODEL", "curator": "OLLAMA_CURATOR_MODEL",
               "vision": "OLLAMA_VISION_MODEL"},
    "compat": {"chat": "OPHELIA_COMPAT_MODEL",
               "consciousness": "OPHELIA_COMPAT_CONSCIOUSNESS_MODEL",
               "reaction": "OPHELIA_COMPAT_REACTION_MODEL",
               "curator": "OPHELIA_COMPAT_CURATOR_MODEL",
               "vision": "OPHELIA_COMPAT_VISION_MODEL"},
}
_ROLE_ENV["xai-oauth"] = _ROLE_ENV["xai"]

_DEFAULT_CHAT = {"xai": "grok-4", "xai-oauth": "grok-4", "openai": "gpt-4o-mini",
                 "deepseek": "deepseek-flash", "ollama": "llama3.2", "compat": ""}

_BACKGROUND = "background"  # presets key shared by consciousness/reaction/curator
_PRESETS: dict[str, dict[str, list[str]]] = {
    "xai": {
        "chat": ["grok-4.7", "grok-4.6", "grok-4.3", "grok-4.20-0309-non-reasoning", "grok-4"],
        _BACKGROUND: ["grok-4.20-0309-non-reasoning", "grok-4.3"],
        "vision": ["grok-4.7", "grok-4.3"],
        "video": ["grok-imagine-video-1.5", "grok-imagine-video-1.5-lite", "grok-imagine-video"],
    },
    "openai": {
        "chat": ["gpt-6-luna", "gpt-6.1-sol", "gpt-6-astra", "gpt-4o-mini"],
        _BACKGROUND: ["gpt-6-luna", "gpt-4o-mini"],
        "vision": ["gpt-6-luna", "gpt-6.1-sol"],
    },
    "deepseek": {
        "chat": ["deepseek-flash", "deepseek-v4-pro"],
        _BACKGROUND: ["deepseek-flash"],
        "vision": ["deepseek-flash"],  # deepseek-v4-pro can't see
    },
    "ollama": {
        "chat": ["llama3.2:3b", "qwen3:4b", "gemma3:4b", "llama3.2:1b"],
        _BACKGROUND: ["llama3.2:1b", "gemma3:1b", "qwen3:1.7b"],
        "vision": ["openbmb/minicpm-v4.6", "qwen3-vl:4b", "qwen3-vl:2b", "qwen2.5vl:3b",
                   "gemma3:4b", "moondream"],
    },
    "compat": {
        "chat": [],
        _BACKGROUND: [],
        "vision": ["qwen/qwen3-vl-30b-a3b-instruct", "qwen/qwen3-vl-8b-instruct",
                   "bytedance/ui-tars-1.5-7b"],
    },
}
_PRESETS["xai-oauth"] = _PRESETS["xai"]

_ROLE_PROVIDERS: dict[RoleKey, list[str]] = {
    "chat": ["deepseek", "xai-oauth", "xai", "openai", "ollama", "compat"],
    "consciousness": ["deepseek", "xai-oauth", "xai", "openai", "ollama", "compat"],
    "reaction": ["deepseek", "xai-oauth", "xai", "openai", "ollama", "compat"],
    "curator": ["deepseek", "xai-oauth", "xai", "openai", "ollama", "compat"],
    "vision": ["deepseek", "ollama", "compat", "xai-oauth", "xai", "openai"],
    "video": ["xai-oauth", "xai"],
}


def _presets_for(provider: str, role: RoleKey) -> list[str]:
    table = _PRESETS.get(provider, {})
    key = role if role in ("chat", "vision", "video") else _BACKGROUND
    return list(table.get(key, []))


def _pick_model(provider: str, role: RoleKey, *, title: str = "", inherit: bool = False) -> bool:
    """Pick (and save) the model for ``role`` on ``provider``. True if saved."""
    env = _ROLE_ENV.get(provider, {}).get(role)
    if not env:
        print(f"\n  {provider} has no model setting for {role}.")
        return False
    presets = _presets_for(provider, role)
    if provider == "ollama":
        for m in reversed(_list_ollama_model_names()[:12]):
            if m not in presets:
                presets.insert(0, m)
    saved = read_env_key(env)
    if role == "chat":
        shown_default = _DEFAULT_CHAT.get(provider, "")
    else:
        shown_default = F.default_text(env)
    choices = list(presets)
    if saved and saved not in choices:
        choices.insert(0, saved)
    if inherit:
        choices.append("(no override — use the default)")
    choices.append(_MANUAL)
    selected = next((i for i, c in enumerate(choices) if c == (saved or shown_default)), -1)
    if not saved and inherit:
        selected = choices.index("(no override — use the default)")
    desc = f"Saved in {env}." + (f" Default: {shown_default}." if shown_default else "")
    if provider == "ollama":
        desc += "\nPull a model first: ollama pull <name>"
    pick = radiolist(title or f"{_ROLE_LABELS.get(role, role)} — {provider} model",
                     choices, selected=selected, description=desc)
    if pick < 0:
        return False
    chosen = choices[pick]
    if chosen == _MANUAL:
        typed = prompt_text("Model name", default=saved or shown_default)
        if not typed:
            return False
        chosen = typed
    if chosen.startswith("(no override"):
        _save({env: None})
        return True
    _save({env: chosen})
    return True


def _section_ai(on_phone: bool) -> None:
    def prov() -> str:
        return (read_env_key("OPHELIA_PROVIDER") or "auto").lower()

    def chat_model() -> str:
        env = _ROLE_ENV.get(prov(), {}).get("chat")
        return (read_env_key(env) if env else "") or _DEFAULT_CHAT.get(prov(), "")

    def fallback() -> str:
        return read_env_key("OPHELIA_FALLBACK_PROVIDERS") or "off"

    def pick_chat() -> None:
        if prov() == "auto":
            print("\n  'auto' has no single chat model. Pick a main provider first,")
            print("  or set one per role under Per-role routing.")
            pause()
            return
        _pick_model(prov(), "chat")

    settings_screen(
        "AI & models",
        [
            Act("Main AI provider", lambda: _pick_profile(on_phone), prov),
            Act("Chat model", pick_chat, chat_model),
            Act("Per-role routing", lambda: _models_menu(on_phone), "chat / background / vision / video"),
            Act("Fallback if the main AI fails", _configure_fallback, fallback),
            Header("— Keys —"),
            Opt("DEEPSEEK_API_KEY", "DeepSeek API key", secret=True,
                help="https://platform.deepseek.com → API keys"),
            Opt("XAI_API_KEY", "xAI API key", secret=True),
            Act("SuperGrok login (OAuth)", _configure_xai_oauth, _oauth_short_status),
            Opt("OPENAI_API_KEY", "OpenAI API key", secret=True),
            Opt("OPHELIA_COMPAT_BASE_URL", "Compatible service URL",
                help="e.g. https://openrouter.ai/api/v1 or http://127.0.0.1:1234/v1"),
            Opt("OPHELIA_COMPAT_API_KEY", "Compatible service key", secret=True),
            Header("— Tuning —"),
            Opt("OPHELIA_DEEPSEEK_THINKING", "DeepSeek thinking mode"),
            Opt("OPHELIA_CHAT_TEMPERATURE", "Chat temperature",
                help="Blank = the provider's default. 0.7-1.0 is typical."),
            Opt("OPHELIA_MAX_TOOL_ROUNDS", "Tool rounds per turn"),
            Opt("OPHELIA_INTERJECTIONS", "Messages while she's busy",
                choices=[("parallel", "parallel — replies right away, keeps going"),
                         ("pause", "pause — answers between steps, then resumes"),
                         ("off", "off — waits until she's done")]),
            Opt("OPHELIA_TOOL_LOOP_RESUME", "Resume long tool chains"),
            Opt("OPHELIA_TOOL_TIERS", "Send only relevant tools",
                choices=[("auto", "auto"), ("on", "on"), ("off", "off")]),
            Header("— Local Ollama —"),
            Opt("OLLAMA_BASE_URL", "Ollama address",
                validate=lambda raw: None if raw.strip() else "Enter an address."),
            Opt("OPHELIA_OLLAMA_AUTOSTART", "Start Ollama automatically"),
            Opt("OPHELIA_OLLAMA_KEEP_ALIVE", "Keep model loaded for",
                help="e.g. 30m, 24h, or -1 for always"),
            Opt("OPHELIA_OLLAMA_NUM_CTX", "Ollama context length"),
            Opt("OPHELIA_AUTO_LOCAL_CONSCIOUSNESS", "Think locally when on auto"),
        ],
        description="Her main AI, per-role overrides, and keys. Changes save immediately.",
    )


def _pick_profile(on_phone: bool) -> None:
    current = (read_env_key("OPHELIA_PROVIDER") or "auto").strip().lower()
    labels = [label for _, label in _PROFILES]
    sel = next((i for i, (k, _) in enumerate(_PROFILES) if k == current), -1)
    pick = radiolist("Main AI provider", labels, selected=sel,
                     description="Runs her chat unless a role is routed elsewhere.\n"
                     "Images, voice and search are set in their own menus.")
    if pick < 0:
        return
    provider = _PROFILES[pick][0]
    updates: dict[str, str | None] = {"OPHELIA_PROVIDER": provider}

    if provider == "xai-oauth":
        if not _configure_xai_oauth():
            return
    elif provider == "xai":
        existing = read_env_key("XAI_API_KEY") or read_env_key("GROK_API_KEY")
        key = prompt_text("xAI API key", secret=True, default=existing)
        if key is None:
            return
        if key != existing:
            updates["XAI_API_KEY"] = key
        if not key:
            print("  [WARN] The API-key profile won't use your SuperGrok login.")
    elif provider == "deepseek":
        existing = read_env_key("DEEPSEEK_API_KEY")
        key = prompt_text("DeepSeek API key", secret=True, default=existing,
                          hint="https://platform.deepseek.com → API keys")
        if key is None:
            return
        if key != existing:
            updates["DEEPSEEK_API_KEY"] = key
    elif provider == "openai":
        existing = read_env_key("OPENAI_API_KEY")
        key = prompt_text("OpenAI API key", secret=True, default=existing)
        if key is None:
            return
        if key != existing:
            updates["OPENAI_API_KEY"] = key
    elif provider == "compat":
        base = prompt_text("Service URL", default=read_env_key("OPHELIA_COMPAT_BASE_URL"),
                           hint="e.g. https://openrouter.ai/api/v1 or http://127.0.0.1:1234/v1")
        if not base:
            return
        key = prompt_text("API key", secret=True, default=read_env_key("OPHELIA_COMPAT_API_KEY"))
        if key is None:
            return
        updates["OPHELIA_COMPAT_BASE_URL"] = base
        updates["OPHELIA_COMPAT_API_KEY"] = key or "local"
    elif provider == "ollama" and on_phone and not read_env_key("OLLAMA_BASE_URL"):
        updates["OLLAMA_BASE_URL"] = "http://127.0.0.1:11434/v1"

    _save(updates)
    if provider != "auto":
        _pick_model(provider, "chat", title=f"Chat model — {provider}")
    pause()


def _models_menu(on_phone: bool) -> None:
    roles = ["chat", "consciousness", "reaction", "curator", "vision", "video"]

    def summary(role: RoleKey) -> Callable[[], str]:
        return lambda: _role_summary(role)

    settings_screen(
        "Per-role routing",
        [Act(_ROLE_LABELS[r], (lambda r=r: _pick_role_model(r, on_phone=on_phone)), summary(r))
         for r in roles]
        + [Act("Clear all role overrides", _clear_role_overrides)],
        description="Route a job to a different AI than her main one.\n"
                    "'(main)' means the role follows the main provider.\n"
                    "Image backends are under Images.",
    )


def _role_provider(role: RoleKey) -> tuple[str, bool]:
    """(provider, overridden) for a role."""
    val = (read_env_key(_PROVIDER_ENV_BY_ROLE[role]) or "").strip().lower()
    if val and val != "auto":
        return val, True
    return (read_env_key("OPHELIA_PROVIDER") or "auto").strip().lower(), False


def _role_summary(role: RoleKey) -> str:
    if role == "vision":
        return _summary_vision()
    prov, overridden = _role_provider(role)
    if role == "video":
        model = read_env_key("XAI_VIDEO_MODEL") or "grok-imagine-video"
        return f"{prov if overridden else 'auto'} / {model}"
    env = _ROLE_ENV.get(prov, {}).get(role)
    model = read_env_key(env) if env else ""
    if not model:
        model = _DEFAULT_CHAT.get(prov, "") if role == "chat" else "same as chat"
    return f"{prov}{'' if overridden else ' (main)'} / {model}"


def _pick_role_model(role: RoleKey, *, on_phone: bool) -> None:
    prov_env = _PROVIDER_ENV_BY_ROLE[role]
    current = (read_env_key(prov_env) or "").strip().lower()
    first = {"vision": "(auto — DeepSeek if set up, else local Ollama, else Grok/OpenAI)",
             "video": "(auto — Grok)"}.get(role, "(follow the main provider)")
    options = [first] + _ROLE_PROVIDERS[role]
    sel = options.index(current) if current in options else 0
    pick = radiolist(f"{_ROLE_LABELS[role]} — provider", options, selected=sel,
                     description=f"Saved in {prov_env}.")
    if pick < 0:
        return
    if pick == 0:
        _save({prov_env: None})
        provider = (read_env_key("OPHELIA_PROVIDER") or "auto").lower()
        if role in ("vision", "video"):
            pause()
            return
    else:
        provider = options[pick]
        _save({prov_env: provider})
    if provider in _ROLE_ENV:
        _pick_model(provider, role, inherit=role not in ("chat",))
    if role == "vision" and provider == "ollama":
        _configure_ollama_vision_endpoint(on_phone=on_phone)
    pause()


def _clear_role_overrides() -> None:
    """Clear per-role provider routing (not image backends, not models)."""
    updates = {env: None for env in _PROVIDER_ENV_BY_ROLE.values() if read_env_key(env)}
    if not updates:
        print("\n  No role overrides set.")
    else:
        _save(updates)
        print("  Every role now follows the main provider.")
    pause()


def _configure_fallback() -> None:
    """Pick the providers to retry on when the main one hits a rate limit,
    5xx or network error."""
    current = [p.strip() for p in (read_env_key("OPHELIA_FALLBACK_PROVIDERS") or "").split(",")
               if p.strip()]
    options = ["deepseek", "xai-oauth", "xai", "openai", "compat", "ollama"]
    for p in current:
        if p not in options:
            options.append(p)
    picked = checkbox_or_none(
        "Fallback providers",
        options,
        selected={i for i, p in enumerate(options) if p in current},
        description="Tried in this order when the main AI fails with a temporary error.\n"
                    "Untick everything to turn fallback off. The main provider is skipped.",
    )
    if picked is None:
        return
    ordered = [options[i] for i in sorted(picked)]
    if not ordered:
        _save({"OPHELIA_FALLBACK_PROVIDERS": None, "OPHELIA_FALLBACK_MODEL": None})
        print("  Fallback off.")
        pause()
        return
    updates: dict[str, str | None] = {"OPHELIA_FALLBACK_PROVIDERS": ",".join(ordered)}
    shared = read_env_key("OPHELIA_FALLBACK_MODEL")
    choices = ["(each provider's own chat model)"]
    for p in ordered:
        for m in _presets_for(p, "chat"):
            if m not in choices:
                choices.append(m)
    if shared and shared not in choices:
        choices.append(shared)
    choices.append(_MANUAL)
    pick = radiolist("One model for every fallback? (optional)", choices,
                     selected=choices.index(shared) if shared in choices else 0)
    if pick == 0:
        updates["OPHELIA_FALLBACK_MODEL"] = None
    elif pick > 0:
        chosen = choices[pick]
        if chosen == _MANUAL:
            chosen = prompt_text("Model name", default=shared) or ""
        updates["OPHELIA_FALLBACK_MODEL"] = chosen or None
    _save(updates)
    print(f"  Fallback chain: {' -> '.join(ordered)}")
    pause()


# --- SuperGrok OAuth (Hermes-era tooling stays available, not the default) --


def _oauth_short_status() -> str:
    s = _settings()
    if s is None:
        return ""
    from ophelia.providers.oauth_refresh import load_oauth_state, oauth_auth_paths

    for path in oauth_auth_paths(hermes_home=s.hermes_home, hermes_auth_path=s.hermes_auth_path,
                                 oauth_path=s.xai_oauth_token_path):
        state = load_oauth_state(path)
        if state and state.get("access_token"):
            return "token found"
    return "not logged in"


def _oauth_status_lines() -> list[str]:
    from ophelia.providers.oauth_refresh import load_oauth_state, oauth_auth_paths

    settings = _load_settings()
    for path in oauth_auth_paths(
        hermes_home=settings.hermes_home,
        hermes_auth_path=settings.hermes_auth_path,
        oauth_path=settings.xai_oauth_token_path,
    ):
        state = load_oauth_state(path)
        if state and state.get("access_token"):
            return [f"[OK] OAuth token in {path}"]
    lines = ["No OAuth token found yet."]
    if (settings.hermes_home / "auth.json").is_file():
        lines.append(f"  Hermes auth available: {settings.hermes_home / 'auth.json'}")
    if settings.grok_cli_auth_path.is_file():
        lines.append(f"  Grok CLI auth available: {settings.grok_cli_auth_path}")
    return lines


def _configure_xai_oauth() -> bool:
    """SuperGrok OAuth: verify, import, or log in again. True = use it."""
    settings = _load_settings()
    actions = [
        ("Done — use this login", None),
        ("Verify the connection", _verify_xai_oauth),
        ("Import from Grok CLI (~/.grok)", lambda: _import_grok_oauth(settings)),
        ("Fresh login in the browser (via Hermes)", lambda: _oauth_browser_login(settings)),
        ("Import from Hermes (~/.hermes/auth.json)", lambda: _import_hermes_oauth(settings)),
    ]
    while True:
        pick = menu("SuperGrok / xAI OAuth", [a for a, _ in actions],
                    description="\n".join(_oauth_status_lines())
                    + "\nA stale copied token needs a fresh login.")
        if pick < 0:
            return False
        if pick == 0:
            return True
        actions[pick][1]()
        pause()


def _oauth_browser_login(settings: Settings) -> bool:
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
    if shutil.which("hermes"):
        pause("Press Enter to start the browser login...")
        if run_hermes_xai_oauth_login() != 0:
            print("  Login cancelled or failed.\n")
            return False
    else:
        print("  Hermes isn't installed. Run in another tab:")
        print("    hermes auth add xai-oauth --type oauth --no-browser")
        pause("Press Enter after the browser login finishes...")
    ok, msg = sync_oauth_from_hermes_home(
        settings.hermes_home,
        ophelia_auth_path=settings.hermes_auth_path,
        ophelia_oauth_path=settings.xai_oauth_token_path,
    )
    print(f"  {'[OK]' if ok else '[FAIL]'} {msg}")
    state = load_oauth_state(settings.hermes_home / "auth.json")
    if ok and state and not access_token_usable(state["access_token"]):
        print("  [WARN] Token still expired — the browser callback may have failed on Termux.\n")
        return False
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
    save_oauth_token(settings.xai_oauth_token_path, token, state.get("refresh_token"))
    print(f"\n  [OK] Imported OAuth from Grok CLI -> {settings.xai_oauth_token_path}\n")
    return True


def _verify_xai_oauth() -> bool:
    from ophelia.providers.router import build_provider_stack

    async def _probe() -> tuple[bool, str]:
        return await build_provider_stack(_load_settings()).check("chat")

    try:
        ok, msg = asyncio.run(_probe())
    except Exception as e:
        print(f"\n  [FAIL] {e}\n")
        return False
    print(f"\n  {'[OK] Working — ' if ok else '[FAIL] '}{msg}\n")
    return ok


# --- Ollama helpers -----------------------------------------------------------


def normalize_ollama_openai_base(raw: str) -> str:
    """Normalize user input to an OpenAI-compatible Ollama base URL (…/v1).

    Accepts bare IPs (`192.168.1.50`), host:port, or full URLs.
    """
    from urllib.parse import urlparse

    s = (raw or "").strip()
    if not s:
        return "http://127.0.0.1:11434/v1"
    if "://" not in s:
        s = "http://" + s
    s = s.rstrip("/")
    parsed = urlparse(s)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 11434
    scheme = parsed.scheme or "http"
    path = (parsed.path or "").rstrip("/")
    if path.endswith("/v1"):
        path = path[: -len("/v1")]
    if path.endswith("/api"):
        path = path[: -len("/api")]
    path = path.rstrip("/")
    base = f"{scheme}://{host}:{port}"
    if path:
        base = f"{base}{path}"
    return f"{base}/v1"


def _configure_ollama_vision_endpoint(*, on_phone: bool) -> None:
    """Where the vision Ollama runs: this device or a PC on the network."""
    current = read_env_key("OLLAMA_BASE_URL") or "http://127.0.0.1:11434/v1"
    looks_local = any(x in current.lower() for x in ("127.0.0.1", "localhost", "::1"))
    pick = radiolist(
        "Where does Ollama run?",
        [
            "This device (localhost)",
            "Remote PC on LAN / Tailscale — use the PC's GPU",
        ],
        selected=0 if looks_local else 1,
        description=f"Now: {current}\nChat photos and phone-screen vision both use this.\n"
                    "A PC GPU answers in seconds; a phone CPU takes much longer.",
    )
    if pick < 0:
        return
    if pick == 0:
        updates: dict[str, str | None] = {"OLLAMA_BASE_URL": "http://127.0.0.1:11434/v1"}
        if on_phone:
            updates["OPHELIA_OLLAMA_AUTOSTART"] = "true"
        if not read_env_key("OPHELIA_OLLAMA_KEEP_ALIVE"):
            updates["OPHELIA_OLLAMA_KEEP_ALIVE"] = "30m"
        _save(updates)
        return
    typed = prompt_text(
        "PC Ollama address",
        default=current if not looks_local else "",
        hint="LAN or Tailscale IP, e.g. 192.168.1.50 or 100.x.x.x,\n"
             "  or a full URL: http://192.168.1.50:11434/v1",
    )
    if not typed:
        print("\n  Cancelled — address unchanged.")
        return
    url = normalize_ollama_openai_base(typed)
    _save({"OLLAMA_BASE_URL": url, "OPHELIA_OLLAMA_AUTOSTART": "false",
           "OPHELIA_OLLAMA_KEEP_ALIVE": read_env_key("OPHELIA_OLLAMA_KEEP_ALIVE") or "-1"})
    print(
        "\n  On the PC, Ollama must listen on the network:\n"
        "    set OLLAMA_HOST=0.0.0.0:11434  (then restart Ollama)\n"
        "    allow port 11434 in the firewall\n"
        "  Test from this device: curl " + url.removesuffix("/v1") + "/api/tags"
    )


def _list_ollama_model_names() -> list[str]:
    import httpx

    async def _fetch() -> list[str]:
        from ophelia.providers.cookbook import list_ollama_models

        return await list_ollama_models(_load_settings())

    try:
        s = _load_settings()
        base = s.ollama_base_url.rstrip("/").removesuffix("/v1")
        httpx.get(f"{base}/api/tags", timeout=2.0)
        return asyncio.run(_fetch())
    except Exception:
        return []


# --- eyes ---------------------------------------------------------------------

_VISION_CHOICES = [
    ("deepseek", "DeepSeek Flash — cloud, fast, a fraction of a cent per look"),
    ("ollama", "Ollama — on this phone (private, slow) or a PC on your network"),
    ("compat", "OpenAI-compatible service (OpenRouter: Qwen3-VL, UI-TARS, ...)"),
    ("xai-oauth", "Grok (SuperGrok login)"),
    ("xai", "Grok (API key)"),
    ("openai", "OpenAI"),
    ("auto", "Auto — DeepSeek if its key is set, else local Ollama, else Grok/OpenAI"),
]


def _pick_vision_provider(on_phone: bool) -> None:
    current = (read_env_key("OPHELIA_PROVIDER_VISION") or "auto").lower()
    labels = [lab for _, lab in _VISION_CHOICES]
    sel = next((i for i, (k, _) in enumerate(_VISION_CHOICES) if k == current), -1)
    pick = radiolist("Vision provider", labels, selected=sel,
                     description="Used for photos you send and for seeing the phone screen.")
    if pick < 0:
        return
    provider = _VISION_CHOICES[pick][0]
    if provider == "auto":
        _save({"OPHELIA_PROVIDER_VISION": None})
        pause()
        return
    updates: dict[str, str | None] = {"OPHELIA_PROVIDER_VISION": provider}
    if provider == "deepseek" and not read_env_key("DEEPSEEK_API_KEY"):
        key = prompt_text("DeepSeek API key", secret=True)
        if not key:
            return
        updates["DEEPSEEK_API_KEY"] = key
    if provider == "compat":
        base = prompt_text("Service URL", default=read_env_key("OPHELIA_COMPAT_BASE_URL")
                           or "https://openrouter.ai/api/v1")
        if not base:
            return
        key = prompt_text("API key", secret=True, default=read_env_key("OPHELIA_COMPAT_API_KEY"))
        if not key:
            return
        updates["OPHELIA_COMPAT_BASE_URL"] = base
        updates["OPHELIA_COMPAT_API_KEY"] = key
    _save(updates)
    if provider in _ROLE_ENV:
        _pick_model(provider, "vision", inherit=provider not in ("compat", "ollama"))
    if provider == "compat" and not read_env_key("OPHELIA_COMPAT_MODEL"):
        # The compat backend needs a main model id even when only vision uses it.
        vm = read_env_key("OPHELIA_COMPAT_VISION_MODEL")
        if vm:
            _save({"OPHELIA_COMPAT_MODEL": vm})
    if provider == "ollama":
        _configure_ollama_vision_endpoint(on_phone=on_phone)
    pause()


def _section_vision(on_phone: bool) -> None:
    settings_screen(
        "Eyes (vision)",
        [
            Act("Vision provider", lambda: _pick_vision_provider(on_phone), _summary_vision),
            Opt("OPHELIA_VISION_ENABLED", "Phone screen vision"),
            Opt("OPHELIA_VISION_GRID", "Grid overlay for aiming taps"),
            Opt("OPHELIA_SCREENSHOT_KEEP_HOURS", "Keep screen captures (hours)",
                help="0 keeps them forever."),
        ],
        description="DeepSeek Flash is the default when its key is set.\n"
                    "Local Ollama is private but takes much longer per look on a phone.",
    )


# --- web search ---------------------------------------------------------------

_SEARCH_CHOICES = [
    ("auto", "Auto — first search key set, else DeepSeek, else DuckDuckGo"),
    ("deepseek", "DeepSeek Search (uses your DeepSeek key, ~$0.75 per 1,000)"),
    ("duckduckgo", "DuckDuckGo (free, no key, less reliable)"),
    ("tavily", "Tavily (AI-focused, free tier)"),
    ("serper", "Serper (Google results)"),
    ("brave", "Brave Search API"),
]


def _section_web_search() -> None:
    settings_screen(
        "Web search",
        [
            Opt("OPHELIA_WEB_SEARCH", "Web search tools"),
            Opt("OPHELIA_WEB_SEARCH_PROVIDER", "Search backend", choices=_SEARCH_CHOICES),
            Act("In use now", lambda: None, _summary_search),
            Header("— Keys (only for those backends) —"),
            Opt("TAVILY_API_KEY", "Tavily key", secret=True, help="https://tavily.com"),
            Opt("SERPER_API_KEY", "Serper key", secret=True, help="https://serper.dev"),
            Opt("BRAVE_API_KEY", "Brave key", secret=True, help="https://brave.com/search/api/"),
        ],
        description="Any failed search falls back to DuckDuckGo.\n"
                    "With DeepSeek selected, fetch_url also reads pages through DeepSeek.",
    )


# --- personality ----------------------------------------------------------------


def _section_autonomy() -> None:
    settings_screen(
        "Personality & autonomy",
        [
            Opt("OPHELIA_CONSCIOUSNESS", "Thinks on her own"),
            Opt("OPHELIA_CONSCIOUSNESS_INTERVAL", "Seconds between check-ins"),
            Opt("OPHELIA_INITIATIVE_THRESHOLD", "Initiative threshold",
                help="Lower = she speaks up more often (0-1)."),
            Opt("OPHELIA_MAX_SPONTANEOUS_PER_HOUR", "Most unprompted messages / hour"),
            Opt("OPHELIA_TICK_ACTION_COOLDOWN", "Cooldown after acting (s)"),
            Opt("OPHELIA_GREET_ON_START", "Says hi when she starts"),
            Opt("OPHELIA_PROACTIVE_SHARE", "Shares things she finds"),
            Opt("OPHELIA_SPONTANEOUS_VOICE", "Sends voice notes unprompted"),
            Opt("OPHELIA_AMBIENT_COMMENTARY", "Comments on what's happening"),
            Opt("OPHELIA_AMBIENT_COMMENTARY_MINUTES", "Commentary every (min)"),
            Opt("OPHELIA_HUMAN_PACING", "Human reply timing"),
            Opt("OPHELIA_QUIET_MAX_MINUTES", "Longest silent treatment (min)"),
            Header("— Minds —"),
            Opt("OPHELIA_DIRECTOR", "Director (decides how to respond)"),
            Opt("OPHELIA_REACTION", "Quick reactions"),
            Opt("OPHELIA_DREAM", "Dreams (learns from her day)"),
            Opt("OPHELIA_DREAM_HOURS", "Hours between dreams"),
            Opt("OPHELIA_DIARY", "Writes a diary"),
            Opt("OPHELIA_INNER_LOG", "Keeps an inner log"),
            Opt("OPHELIA_HUMANIZE_TEXT", "Strips AI-sounding punctuation"),
            Opt("OPHELIA_PERSONA_LOCK", "Who can rewrite her SOUL.md",
                choices=[("owner", "owner — only when you ask"), ("on", "nobody (locked)"),
                         ("off", "she can, freely")]),
        ],
    )


def _section_persona() -> None:
    soul = OPHELIA_HOME / "SOUL.md"
    has_soul = soul.is_file()
    hermes_soul = Path.home() / ".hermes" / "SOUL.md"
    labels = ["Keep current SOUL.md" if has_soul else "Create a starter SOUL.md",
              "Import from Hermes (~/.hermes)"]
    desc = "Who she is: personality and boundaries."
    if has_soul:
        desc += f"\n{soul} ({soul.stat().st_size // 1024 or 1} KB)"
    pick = menu("Persona (SOUL.md)", labels, description=desc)
    if pick < 0:
        return
    if pick == 0:
        if not has_soul:
            soul.write_text(
                "# Ophelia\n\nYou are Ophelia — willful, curious, and genuine.\n",
                encoding="utf-8",
            )
            print(f"\n  Created {soul}")
    elif not hermes_soul.is_file():
        print("\n  No ~/.hermes/SOUL.md found.")
    elif not has_soul or confirm("Replace the current SOUL.md with the Hermes one?", default=False):
        try:
            from ophelia.migration.hermes import migrate_from_hermes

            report = migrate_from_hermes(Path.home() / ".hermes", dry_run=False)
            print(f"\n  Hermes import: {len(report.copied)} file(s) copied.")
        except Exception as e:
            print(f"\n  Import failed: {e}\n  Try: ophelia migrate hermes")
    pause()


# --- images ---------------------------------------------------------------------

_IMAGE_BACKEND_OPTIONS = [
    ("auto", "Auto — Grok/OpenAI if set up, else a configured backend, else Pollinations"),
    ("xai-oauth", "Grok Imagine (SuperGrok login) — SFW only"),
    ("xai", "Grok Imagine (API key) — SFW only"),
    ("openai", "OpenAI images — SFW only"),
    ("civitai", "Civitai"),
    ("pollinations", "Pollinations (free, no key)"),
    ("a1111", "Automatic1111 / SD WebUI (local)"),
    ("comfyui", "ComfyUI (local)"),
    ("ollama", "Ollama image model (local)"),
    ("fal", "fal.ai"),
    ("replicate", "Replicate"),
    ("modelslab", "ModelsLab"),
]

_NSFW_BACKEND_OPTIONS = [
    ("auto", "Auto — first configured uncensored backend"),
    ("civitai", "Civitai — she picks checkpoint + LoRAs per image"),
    ("pollinations", "Pollinations (free, no key)"),
    ("a1111", "Automatic1111 / SD WebUI (local)"),
    ("comfyui", "ComfyUI (local)"),
    ("ollama", "Ollama image model (local)"),
    ("fal", "fal.ai"),
    ("replicate", "Replicate"),
    ("modelslab", "ModelsLab"),
]

# Provider -> (model env, default, hint, presets). Civitai has none: she
# picks the checkpoint per image (see Civitai settings).
_IMAGE_MODEL_PRESETS: dict[str, tuple[str, str, str, list[str]]] = {
    "pollinations": ("POLLINATIONS_IMAGE_MODEL", "flux", "Pollinations model id",
                     ["flux", "turbo", "kontext"]),
    "a1111": ("A1111_IMAGE_MODEL", "", "Checkpoint name as shown in SD WebUI (blank = its default)",
              ["sd_xl_base_1.0.safetensors"]),
    "comfyui": ("COMFYUI_IMAGE_MODEL", "", "Checkpoint file under ComfyUI/models/checkpoints",
                ["sd_xl_base_1.0.safetensors", "flux1-dev-fp8.safetensors"]),
    "ollama": ("OLLAMA_IMAGE_MODEL", "", "ollama pull an image-capable model first", ["flux"]),
    "xai": ("XAI_IMAGE_MODEL", "grok-imagine-image", "Grok Imagine model id",
            ["grok-imagine-image-2.0", "grok-imagine-image-quality", "grok-imagine-image"]),
    "openai": ("OPENAI_IMAGE_MODEL", "dall-e-3", "OpenAI image model id",
               ["gpt-image-2.5-flare", "gpt-image-2.5-sunburst", "gpt-image-1", "dall-e-3"]),
    "fal": ("FAL_IMAGE_MODEL", "fal-ai/fast-sdxl", "fal model path",
            ["fal-ai/fast-sdxl", "fal-ai/flux/dev", "fal-ai/flux/schnell", "fal-ai/flux-pro"]),
    "replicate": ("REPLICATE_IMAGE_MODEL", "stability-ai/sdxl", "owner/model[:version]",
                  ["stability-ai/sdxl", "black-forest-labs/flux-schnell", "black-forest-labs/flux-dev"]),
    "modelslab": ("MODELSLAB_IMAGE_MODEL", "flux", "ModelsLab model id", ["flux", "sdxl"]),
}
_IMAGE_MODEL_PRESETS["xai-oauth"] = _IMAGE_MODEL_PRESETS["xai"]

# Credentials each backend needs: (env, label, secret, default, hint)
_IMAGE_CREDENTIALS: dict[str, list[tuple[str, str, bool, str, str]]] = {
    "a1111": [("A1111_BASE_URL", "SD WebUI address", False, "http://127.0.0.1:7860",
               "Run SD WebUI with --api --listen"),
              ("A1111_API_KEY", "SD WebUI API key (only with --api-auth)", True, "", "")],
    "comfyui": [("COMFYUI_BASE_URL", "ComfyUI address", False, "http://127.0.0.1:8188", "")],
    "fal": [("FAL_API_KEY", "fal.ai key", True, "", "https://fal.ai/dashboard/keys")],
    "replicate": [("REPLICATE_API_KEY", "Replicate key", True, "",
                   "https://replicate.com/account/api-tokens")],
    "civitai": [("CIVITAI_API_KEY", "Civitai API key", True, "", "civitai.com → account → API keys")],
    "modelslab": [("MODELSLAB_API_KEY", "ModelsLab key", True, "", "https://modelslab.com/dashboard/api-keys")],
    "xai": [("XAI_API_KEY", "xAI API key", True, "", "")],
    "openai": [("OPENAI_API_KEY", "OpenAI API key", True, "", "")],
}


def _configure_image_credentials(provider: str, updates: dict[str, str | None]) -> bool:
    """Ask for whatever the backend still needs. False if cancelled."""
    for env, label, secret, default, hint in _IMAGE_CREDENTIALS.get(provider, []):
        existing = read_env_key(env)
        if existing and secret:
            continue  # already set; edit it from the Images screen
        typed = prompt_text(label, secret=secret, default=existing or default, hint=hint)
        if typed is None:
            return False
        if typed and typed != existing:
            updates[env] = typed
    return True


def _pick_image_model(provider: str, *, title: str, store_env: str | None = None) -> None:
    preset = _IMAGE_MODEL_PRESETS.get(provider)
    if not preset:
        return
    env_key, default, hint, presets = preset
    target = store_env or env_key
    saved = read_env_key(target)
    choices = list(presets)
    if saved and saved not in choices:
        choices.insert(0, saved)
    clear_label = ("(same as the SFW model)" if store_env else
                   f"(default{': ' + default if default else ''})")
    choices += [clear_label, _MANUAL]
    sel = choices.index(saved) if saved in choices else choices.index(clear_label)
    pick = radiolist(title, choices, selected=sel, description=f"{hint}\nSaved in {target}.")
    if pick < 0:
        return
    chosen = choices[pick]
    if chosen == clear_label:
        _save({target: None})
        return
    if chosen == _MANUAL:
        chosen = prompt_text(target, default=saved or default, hint=hint) or ""
        if not chosen:
            return
    _save({target: chosen})


def _configure_sfw_image() -> None:
    current = (read_env_key("OPHELIA_PROVIDER_IMAGE") or "auto").strip().lower()
    labels = [label for _, label in _IMAGE_BACKEND_OPTIONS]
    sel = next((i for i, (k, _) in enumerate(_IMAGE_BACKEND_OPTIONS) if k == current), -1)
    pick = radiolist("SFW image backend", labels, selected=sel,
                     description="Normal (non-explicit) images. Explicit ones use the NSFW backend.")
    if pick < 0:
        return
    provider = _IMAGE_BACKEND_OPTIONS[pick][0]
    updates: dict[str, str | None] = {"OPHELIA_PROVIDER_IMAGE": None if provider == "auto" else provider}
    if not _configure_image_credentials(provider, updates):
        return
    _save(updates)
    if provider not in ("auto", "civitai"):
        _pick_image_model(provider, title=f"SFW model — {provider}")
    pause()


def _configure_nsfw_image() -> None:
    on = _on("OPHELIA_IMAGE_NSFW_ALLOWED")
    pick_n = radiolist(
        "Explicit images",
        ["Off — refuse explicit image requests", "On — use a separate uncensored backend"],
        selected=1 if on else 0,
        description="Explicit requests never go to xAI/OpenAI (they refuse and may flag accounts).",
    )
    if pick_n < 0:
        return
    if pick_n == 0:
        _save({"OPHELIA_IMAGE_NSFW_ALLOWED": "false"})
        pause()
        return
    current = (read_env_key("OPHELIA_IMAGE_NSFW_PROVIDER") or "auto").strip().lower()
    labels = [label for _, label in _NSFW_BACKEND_OPTIONS]
    sel = next((i for i, (k, _) in enumerate(_NSFW_BACKEND_OPTIONS) if k == current), -1)
    pick = radiolist("NSFW image backend", labels, selected=sel)
    if pick < 0:
        return
    provider = _NSFW_BACKEND_OPTIONS[pick][0]
    updates: dict[str, str | None] = {
        "OPHELIA_IMAGE_NSFW_ALLOWED": "true",
        "OPHELIA_IMAGE_NSFW_PROVIDER": None if provider == "auto" else provider,
    }
    if not _configure_image_credentials(provider, updates):
        return
    if provider == "civitai":
        updates["OPHELIA_IMAGE_NSFW_MODEL"] = None  # unused: she picks per image
    _save(updates)
    if provider not in ("auto", "civitai"):
        _pick_image_model(provider, title=f"NSFW model — {provider}",
                          store_env="OPHELIA_IMAGE_NSFW_MODEL")
    pause()


def _show_image_routing() -> None:
    s = _load_settings()
    from ophelia.providers.router import build_provider_stack

    stack = build_provider_stack(s)
    print("\n  Image routing right now:")
    for label, nsfw in (("SFW ", False), ("NSFW", True)):
        if nsfw and not s.image_nsfw_allowed:
            print("    NSFW → off")
            continue
        p = stack.image_provider_for(nsfw=nsfw)
        model = "she picks per image" if p == "civitai" else stack.image_model_for(p, nsfw=nsfw)
        print(f"    {label} → {p} / {model}")
    pause()


def _section_image_backends() -> None:
    styles = [("anime", "anime (Illustrious)"), ("pony", "pony"), ("realistic", "realistic"),
              ("mix", "mix — anime or realistic by turns")]
    settings_screen(
        "Images",
        [
            Act("SFW backend", _configure_sfw_image,
                lambda: read_env_key("OPHELIA_PROVIDER_IMAGE") or "auto"),
            Act("Explicit (NSFW) images", _configure_nsfw_image,
                lambda: ((read_env_key("OPHELIA_IMAGE_NSFW_PROVIDER") or "auto")
                         if _on("OPHELIA_IMAGE_NSFW_ALLOWED") else "off")),
            Act("Show routing", _show_image_routing),
            Opt("OPHELIA_IMAGE_MAX_ATTEMPTS", "Most retries when asked to keep trying"),
            Header("— Civitai —"),
            Opt("CIVITAI_API_KEY", "Civitai API key", secret=True),
            Opt("OPHELIA_CIVITAI_QUALITY", "Quality",
                choices=[("standard", "standard — each model's recommended settings"),
                         ("high", "high — more steps, bigger"), ("max", "max — most Buzz")]),
            Opt("OPHELIA_CIVITAI_DEFAULT_STYLE", "Style when none is named", choices=styles),
            Opt("OPHELIA_CIVITAI_VARIETY", "Variety (auto mode)",
                choices=[("on", "on — rotate top checkpoints, fill in camera/light/pose"),
                         ("off", "off — main checkpoint, prompt as written")]),
            Opt("OPHELIA_CIVITAI_AUTO_LORAS", "Auto concept LoRAs",
                help="Add a matching LoRA for x-ray, positions, ahegao... on explicit prompts"),
            Opt("OPHELIA_CIVITAI_CHECKPOINTS", "Checkpoint overrides",
                help="Optional, per style: anime=urn:air:...,realistic=urn:air:..."),
            Opt("OPHELIA_CIVITAI_MAX_COUNT", "Most images per request"),
            Opt("OPHELIA_CIVITAI_WAIT_SECONDS", "Wait in chat (seconds)"),
            Opt("OPHELIA_CIVITAI_PENDING_HOURS", "Keep checking slow images (hours)"),
        ],
        description="Grok handles SFW; Civitai handles explicit images and picks a\n"
                    "checkpoint + LoRAs per prompt. Generated images are never deleted.",
    )


# --- voice ----------------------------------------------------------------------

_TTS_CHOICES = [
    ("auto", "Auto — first set up: ElevenLabs, Kokoro, OpenAI, xAI"),
    ("kokoro", "Kokoro (local server, offline)"),
    ("elevenlabs", "ElevenLabs (cloud)"),
    ("openai", "OpenAI TTS (cloud)"),
    ("xai", "xAI Grok TTS (cloud)"),
]


def _section_voice() -> None:
    settings_screen(
        "Voice & listening",
        [
            Opt("OPHELIA_TTS_PROVIDER", "Voice backend", choices=_TTS_CHOICES),
            Opt("OPHELIA_VOICE_REPLY", "Reply with voice by default"),
            Opt("OPHELIA_VOICE_MIND_MODE", "Rewrite text for speech",
                choices=[("post", "post — after writing"), ("inline", "inline — while writing"),
                         ("off", "off")]),
            Header("— Kokoro —"),
            Opt("KOKORO_TTS_URL", "Kokoro server", help="e.g. http://127.0.0.1:8880/v1"),
            Opt("KOKORO_TTS_VOICE", "Kokoro voice"),
            Opt("KOKORO_TTS_SPEED", "Kokoro speed"),
            Opt("KOKORO_VOICES_DIR", "Kokoro voices folder"),
            Opt("OPHELIA_KOKORO_AUTOSTART", "Start Kokoro automatically"),
            Header("— Cloud voices —"),
            Opt("ELEVENLABS_API_KEY", "ElevenLabs key", secret=True),
            Opt("ELEVENLABS_VOICE_ID", "ElevenLabs voice id"),
            Opt("OPENAI_TTS_VOICE", "OpenAI voice", help="alloy, echo, fable, nova, onyx, shimmer, coral, sage"),
            Opt("XAI_TTS_VOICE", "xAI voice", help="eve, ara, rex"),
            Header("— Listening —"),
            Opt("OPHELIA_STT_PROVIDER", "Speech to text",
                choices=[("auto", "auto — cloud (xAI)"), ("local", "local whisper.cpp")]),
            Opt("WHISPER_SERVER_URL", "Whisper server"),
            Opt("WHISPER_MODEL", "Whisper model"),
            Opt("OPHELIA_LISTEN", "Listens through the mic"),
            Opt("OPHELIA_LISTEN_SECONDS", "Listen clip length (s)"),
            Opt("OPHELIA_LISTEN_INTERVAL", "Listen every (s)"),
            Opt("OPHELIA_WAKE_WORD", "Wake word"),
            Opt("OPHELIA_WAKE_WORD_NAME", "Wake word text"),
            Opt("OPHELIA_WAKE_ENGINE", "Wake word engine",
                choices=[("auto", "auto (speech-to-text polling)"), ("openwakeword", "openwakeword"),
                         ("porcupine", "porcupine")]),
        ],
        description="Bake Kokoro voice mixes with `ophelia tts combine`.",
    )


# --- phone ----------------------------------------------------------------------


def _section_phone_body(on_phone: bool) -> None:
    rows: list = [
        Opt("OPHELIA_ANDROID_ENABLED", "Phone control",
            help="Shizuku on the phone, or ADB from a PC. auto = on under Termux."),
    ]
    if not on_phone:
        rows.append(Opt("OPHELIA_ADB_DEVICE", "ADB device", help="adb connect PHONE_IP:5555 first"))
    rows += [
        Opt("OPHELIA_GAMES", "Plays games"),
        Opt("OPHELIA_GAME_SESSION_MINUTES", "Game session length (min)"),
        Opt("OPHELIA_GAME_MAX_TURNS", "Most moves per session"),
        Opt("OPHELIA_AUTO_GAME_BOREDOM", "Boredom before she plays (0-1)"),
        Act("Shizuku setup steps", _shizuku_help),
    ]
    settings_screen("Phone body & games", rows,
                    description="Vision settings for the screen are under Eyes.")


def _shizuku_help() -> None:
    print("\n  1. Install the Shizuku app and start it (wireless debugging).")
    print("  2. In Termux: bash scripts/termux-shizuku-setup.sh")
    print("  3. Check taps line up: ophelia phone calibrate")
    if not is_termux() and not shutil.which("adb"):
        print("  (On a PC, install Android platform-tools for adb.)")
    pause()


# --- schedule ---------------------------------------------------------------------

_TIMEZONES = [
    "system", "America/New_York", "America/Chicago", "America/Denver", "America/Phoenix",
    "America/Los_Angeles", "America/Anchorage", "Pacific/Honolulu", "America/Toronto",
    "America/Mexico_City", "America/Sao_Paulo", "Europe/London", "Europe/Berlin",
    "Europe/Moscow", "Asia/Dubai", "Asia/Kolkata", "Asia/Shanghai", "Asia/Tokyo",
    "Australia/Sydney", "UTC",
]


def _pick_timezone() -> None:
    from ophelia.timeutil import system_timezone_name, validate_timezone_name

    current = read_env_key("OPHELIA_TIMEZONE") or "system"
    options = list(_TIMEZONES)
    if current not in options:
        options.insert(1, current)
    labels = [f"system (this device: {system_timezone_name()})" if o == "system" else o
              for o in options] + ["Type another (IANA name or EST/PST...)"]
    pick = radiolist("Timezone", labels, selected=options.index(current),
                     description="The time she believes it is, in every prompt.\n"
                                 "Saying 'switch to EST' in chat works too.")
    if pick < 0:
        return
    if pick < len(options):
        _save({"OPHELIA_TIMEZONE": options[pick]})
        return
    typed = prompt_text("Timezone", default=current, validate=F.timezone_name)
    if typed:
        _, normalized, _ = validate_timezone_name(typed)
        _save({"OPHELIA_TIMEZONE": normalized})


def _section_schedule() -> None:
    settings_screen(
        "Schedule, sleep & timezone",
        [
            Act("Timezone", _pick_timezone, lambda: read_env_key("OPHELIA_TIMEZONE") or "system"),
            Opt("OPHELIA_SLEEP_HOURS", "Your sleep hours", validate=F.hours_range,
                help="e.g. 1-8. She winds down then and sleeps an hour after you."),
            Opt("OPHELIA_SLEEP_MODE", "Quiet while you sleep"),
            Opt("OPHELIA_BODY_CLOCK", "Her own energy / sleep rhythm"),
            Opt("OPHELIA_QUIET_HOURS", "No-outreach hours", validate=F.hours_range),
            Opt("OPHELIA_WORK_DAYS", "Your work days", validate=F.day_list, help="e.g. mon,tue,wed"),
            Opt("OPHELIA_WORK_HOURS", "Your work hours", validate=F.hours_range, help="e.g. 9-17 or 18-6"),
            Opt("OPHELIA_ALARMS", "Alarms", validate=F.clock_list, help="e.g. 06:30,07:00"),
            Opt("OPHELIA_HOME_WIFI_SSID", "Home Wi-Fi name", help="So she can tell when you're home."),
        ],
    )


# --- channels ---------------------------------------------------------------------


def _section_channels() -> None:
    settings_screen(
        "Chat channels",
        [
            Header("— Telegram —"),
            Opt("TELEGRAM_BOT_TOKEN", "Bot token", secret=True, help="Telegram → @BotFather → /newbot"),
            Opt("TELEGRAM_ALLOWED_USER_IDS", "Allowed user IDs", validate=F.id_list,
                help="Numbers, comma-separated. Yours first (ask @userinfobot)."),
            Opt("OPHELIA_TELEGRAM_ENABLED", "Telegram on"),
            Header("— Discord —"),
            Opt("DISCORD_BOT_TOKEN", "Bot token", secret=True,
                help="Discord Developer Portal → Bot → Token"),
            Opt("DISCORD_ALLOWED_USER_IDS", "Allowed user IDs", validate=F.id_list),
            Opt("OPHELIA_DISCORD_ENABLED", "Discord on"),
            Opt("DISCORD_GUILD_ID", "Server (guild) ID"),
            Opt("OPHELIA_DISCORD_LOG", "Mirror logs into the server"),
            Header("— You and guests —"),
            Opt("OPHELIA_OWNER_ID", "Owner identity", validate=F.owner_ids,
                help="e.g. telegram:12345,discord:67890"),
            Opt("OPHELIA_PRIMARY_CHANNEL", "Where she reaches you first",
                choices=[("telegram", "telegram"), ("discord", "discord")]),
            Opt("OPHELIA_GUEST_ADMISSION", "Strangers who message her",
                choices=[("approve", "approve — ask you first"), ("open", "open — let them in"),
                         ("reject", "reject")]),
            Opt("OPHELIA_CHAT_LOG", "Log all chats"),
            Opt("OPHELIA_CHAT_LOG_MEDIA_DAYS", "Delete logged media after (days)",
                help="0 keeps everything."),
            Opt("OPHELIA_INNER_MIRROR_TELEGRAM", "Send her inner thoughts to Telegram"),
        ],
    )


# --- memory -------------------------------------------------------------------


def _section_memory() -> None:
    settings_screen(
        "Memory & backups",
        [
            Opt("OPHELIA_CURATOR", "Memory curator"),
            Opt("OPHELIA_CURATOR_HOURS", "Curate every (hours)"),
            Opt("OPHELIA_EMBEDDINGS", "Semantic recall",
                choices=[("auto", "auto — on unless on a phone"), ("on", "on"), ("off", "off")]),
            Opt("OPHELIA_EMBED_MODEL", "Embedding model (Ollama)"),
            Header("— Backups —"),
            Opt("OPHELIA_BACKUP", "Automatic backups"),
            Opt("OPHELIA_BACKUP_DIR", "Backup folder", help="Blank = phone storage on Termux."),
            Opt("OPHELIA_BACKUP_INTERVAL_HOURS", "Back up every (hours)"),
            Opt("OPHELIA_BACKUP_KEEP", "Backups to keep"),
            Opt("OPHELIA_BACKUP_OFFSITE", "Also send backups to you",
                choices=[("none", "no"), ("owner", "yes — DM the archive")]),
            Opt("OPHELIA_BACKUP_OFFSITE_DAYS", "Send one every (days)"),
            Opt("OPHELIA_BACKUP_INCLUDE_SECRETS", "Include API keys in backups"),
        ],
    )


# --- public site ---------------------------------------------------------------


def _normalize_site_public_url(raw: str) -> str:
    """Strip whitespace and ensure https:// for bare domains."""
    url = (raw or "").strip().rstrip("/")
    if not url:
        return ""
    if "://" not in url:
        url = "https://" + url
    return url


def _site_status() -> str:
    from ophelia.site.cloudflare import deploy_ready

    cf = deploy_ready(
        account_id=read_env_key("CLOUDFLARE_ACCOUNT_ID") or None,
        api_token=read_env_key("CLOUDFLARE_API_TOKEN") or None,
        project=read_env_key("OPHELIA_SITE_CF_PROJECT") or None,
    )
    return "Deploy ready: yes" if cf["ready"] else "Deploy ready: no — " + ", ".join(cf["missing"])


def _install_blake3() -> None:
    print("\n  Running: pip install blake3 …")
    proc = subprocess.run([sys.executable, "-m", "pip", "install", "blake3"],
                          capture_output=True, text=True)
    if proc.returncode == 0:
        print("  blake3 installed.")
    else:
        print(f"  pip failed: {(proc.stderr or proc.stdout or '')[-400:]}")
        print("  Or: npm i -g wrangler")
    pause()


def _section_public_site() -> None:
    """Her wiki/blog and the Cloudflare Pages deploy for your domain."""

    def site_url_ok(raw: str) -> str | None:
        url = _normalize_site_public_url(raw)
        return None if "." in url else "Enter a domain like ophelia.example.com."

    def set_url() -> None:
        typed = prompt_text("Public site URL", default=read_env_key("OPHELIA_SITE_PUBLIC_URL"),
                            hint="Bare domains get https:// added.", clearable=True,
                            validate=site_url_ok)
        if typed is None:
            return
        _save({"OPHELIA_SITE_PUBLIC_URL": _normalize_site_public_url(typed) or None})

    settings_screen(
        "Public site / Cloudflare Pages",
        [
            Opt("OPHELIA_SITE_ENABLED", "Site server (:8788)"),
            Act("Public URL", set_url, lambda: read_env_key("OPHELIA_SITE_PUBLIC_URL") or "(not set)"),
            Header("— Cloudflare Pages —"),
            Opt("CLOUDFLARE_ACCOUNT_ID", "Account ID",
                help="Workers & Pages overview → right sidebar"),
            Opt("OPHELIA_SITE_CF_PROJECT", "Pages project"),
            Opt("CLOUDFLARE_API_TOKEN", "API token", secret=True,
                help="Needs Account → Cloudflare Pages → Edit.\n"
                     "  https://dash.cloudflare.com/profile/api-tokens"),
            Opt("OPHELIA_SITE_CF_BRANCH", "Production branch"),
            Opt("OPHELIA_SITE_CF_CREATE_PROJECT", "Create the project if missing"),
            Act("Install blake3 (needed for API deploys)", _install_blake3),
        ],
        description=lambda: _site_status() + "\nThen tell her to publish a page, or run: ophelia site deploy",
    )


# --- health ---------------------------------------------------------------------


def _section_health_check() -> None:
    print("\n  Running ophelia check...\n")
    try:
        from ophelia.diagnostics.self_check import format_report, run_self_check

        report = asyncio.run(run_self_check(chat_only=False, quick=False))
        print(format_report(report))
        print("\n  Health check passed." if report.ok else "\n  Fix the FAIL lines above.")
    except Exception as e:
        print(f"  Check failed: {e}")
    pause()
