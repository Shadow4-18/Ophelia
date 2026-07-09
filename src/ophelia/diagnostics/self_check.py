"""Install, version, dependency, and runtime self-check."""

from __future__ import annotations

import asyncio
import importlib.metadata
import importlib.util
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from ophelia import __version__ as PACKAGE_VERSION
from ophelia.config import OPHELIA_HOME, Settings, ensure_dirs
from ophelia.platform import is_termux, platform_summary, runtime_label
from ophelia.providers.model_gate import get_model_gate
from ophelia.providers.router import ROLE_ENV, ProviderStack, build_provider_stack

# PyPI distribution names for importlib.metadata.version()
DIST_NAMES: dict[str, str] = {
    "httpx": "httpx",
    "openai": "openai",
    "telegram": "python-telegram-bot",
    "pydantic": "pydantic",
    "pydantic_settings": "pydantic-settings",
    "aiosqlite": "aiosqlite",
    "apscheduler": "APScheduler",
    "structlog": "structlog",
    "yaml": "PyYAML",
    "fastapi": "fastapi",
    "uvicorn": "uvicorn",
    "discord": "discord.py",
}

# Keep in sync with pyproject.toml [project.dependencies]
REQUIRED_PACKAGES: list[tuple[str, str]] = [
    ("httpx", "0.27"),
    ("openai", "1.68"),
    ("telegram", "21.10"),
    ("pydantic", "2.10"),
    ("pydantic_settings", "2.7"),
    ("aiosqlite", "0.20"),
    ("apscheduler", "3.11"),
    ("structlog", "24.4"),
    ("yaml", "6.0"),
    ("fastapi", "0.115"),
    ("uvicorn", "0.32"),
    ("discord", "2.4"),
]

MIN_PYTHON = (3, 11)


def _required_packages() -> list[tuple[str, str]]:
    """Platform-aware minimum versions (Termux uses capped openai without jiter)."""
    if not is_termux():
        return REQUIRED_PACKAGES
    return [
        (name, "1.35" if name == "openai" else min_ver)
        for name, min_ver in REQUIRED_PACKAGES
    ]


@dataclass
class CheckResult:
    category: str
    name: str
    ok: bool
    detail: str
    hint: str = ""
    required: bool = True


@dataclass
class SelfCheckReport:
    results: list[CheckResult] = field(default_factory=list)
    platform: str = ""
    ophelia_home: str = ""

    def add(self, **kwargs: Any) -> None:
        self.results.append(CheckResult(**kwargs))

    @property
    def ok(self) -> bool:
        return all(r.ok for r in self.results if r.required)

    def by_category(self) -> dict[str, list[CheckResult]]:
        out: dict[str, list[CheckResult]] = {}
        for r in self.results:
            out.setdefault(r.category, []).append(r)
        return out

    def failed(self) -> list[CheckResult]:
        return [r for r in self.results if not r.ok and r.required]


def installed_version() -> str:
    try:
        return importlib.metadata.version("ophelia")
    except importlib.metadata.PackageNotFoundError:
        return PACKAGE_VERSION


def _parse_version(v: str) -> tuple[int, ...]:
    parts: list[int] = []
    for piece in v.strip().split("."):
        digits = ""
        for ch in piece:
            if ch.isdigit():
                digits += ch
            else:
                break
        if digits:
            parts.append(int(digits))
    return tuple(parts) or (0,)


def _version_gte(installed: str, minimum: str) -> bool:
    return _parse_version(installed) >= _parse_version(minimum)


def _repo_root_from_package() -> Path | None:
    try:
        import ophelia

        root = Path(ophelia.__file__).resolve().parent
        for _ in range(4):
            if (root / "pyproject.toml").is_file():
                return root
            root = root.parent
    except (ImportError, OSError):
        pass
    return None


def _git_revision(root: Path | None) -> str | None:
    if not root or not (root / ".git").is_dir():
        return None
    try:
        import subprocess

        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if out.returncode == 0:
            return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def _check_python(report: SelfCheckReport) -> None:
    ver = sys.version_info
    ok = ver >= MIN_PYTHON
    report.add(
        category="version",
        name="Python",
        ok=ok,
        detail=f"{ver.major}.{ver.minor}.{ver.micro}",
        hint=f"Requires Python >={MIN_PYTHON[0]}.{MIN_PYTHON[1]}" if not ok else "",
    )


def _check_ophelia_version(report: SelfCheckReport) -> None:
    installed = installed_version()
    match = installed == PACKAGE_VERSION
    report.add(
        category="version",
        name="Ophelia package",
        ok=True,
        detail=f"v{installed} (code {PACKAGE_VERSION})",
        hint="" if match else "version mismatch - reinstall: pip install -e .",
        required=match,
    )

    root = _repo_root_from_package()
    rev = _git_revision(root)
    install_detail = "pip wheel"
    if root:
        install_detail = f"editable @ {root}"
    if rev:
        install_detail += f" (git {rev})"
    report.add(
        category="version",
        name="Install source",
        ok=True,
        detail=install_detail,
        required=False,
    )


def _check_dependencies(report: SelfCheckReport) -> None:
    import_map = {
        "telegram": "telegram",
        "yaml": "yaml",
        "pydantic_settings": "pydantic_settings",
        "discord": "discord",
    }
    for pkg, min_ver in _required_packages():
        import_name = import_map.get(pkg, pkg)
        dist = DIST_NAMES.get(pkg, pkg)
        try:
            ver = importlib.metadata.version(dist)
            ok = _version_gte(ver, min_ver)
            report.add(
                category="dependencies",
                name=pkg,
                ok=ok,
                detail=f"{ver} (need >={min_ver})",
                hint=_dep_install_hint(pkg) if not ok else "",
            )
        except importlib.metadata.PackageNotFoundError:
            spec = importlib.util.find_spec(import_name)
            report.add(
                category="dependencies",
                name=pkg,
                ok=spec is not None,
                detail="missing" if spec is None else "importable (version unknown)",
                hint=_dep_install_hint(pkg) if spec is None else "",
            )

    if is_termux():
        try:
            import pydantic_core  # noqa: F401

            report.add(
                category="dependencies",
                name="pydantic_core (Termux)",
                ok=True,
                detail="importable",
                required=False,
            )
        except ImportError:
            report.add(
                category="dependencies",
                name="pydantic_core (Termux)",
                ok=False,
                detail="missing — PyPI wheel incompatible on Termux",
                hint="bash scripts/termux-install.sh",
                required=True,
            )
        _check_termux_openai_httpx(report)


def _dep_install_hint(pkg: str) -> str:
    if is_termux():
        return "bash scripts/termux-install.sh"
    return "pip install -e ."


def _check_termux_openai_httpx(report: SelfCheckReport) -> None:
    """openai<1.40 + httpx>=0.28 breaks AsyncOpenAI (proxies kwarg removed)."""
    try:
        oa_ver = importlib.metadata.version("openai")
        hx_ver = importlib.metadata.version("httpx")
    except importlib.metadata.PackageNotFoundError:
        return
    oa_parts = [int(x) for x in oa_ver.split(".")[:2] if x.isdigit()]
    hx_parts = [int(x) for x in hx_ver.split(".")[:2] if x.isdigit()]
    bad = len(oa_parts) >= 2 and (oa_parts[0], oa_parts[1]) < (1, 40)
    bad = bad and len(hx_parts) >= 2 and (hx_parts[0], hx_parts[1]) >= (0, 28)
    report.add(
        category="dependencies",
        name="openai+httpx (Termux)",
        ok=not bad,
        detail=f"openai {oa_ver} + httpx {hx_ver}"
        + (" — incompatible" if bad else " — OK"),
        hint="python -m pip install 'httpx>=0.27,<0.28' -c scripts/termux-constraints.txt",
        required=bad,
    )


def _check_paths(report: SelfCheckReport, settings: Settings) -> None:
    ensure_dirs(settings)
    env_path = OPHELIA_HOME / ".env"
    report.add(
        category="config",
        name="Ophelia home",
        ok=OPHELIA_HOME.is_dir(),
        detail=str(OPHELIA_HOME),
    )
    report.add(
        category="config",
        name="Environment file",
        ok=env_path.is_file(),
        detail=str(env_path) if env_path.is_file() else "missing",
        hint="ophelia setup --do",
    )
    soul = OPHELIA_HOME / "SOUL.md"
    report.add(
        category="config",
        name="Persona (SOUL.md)",
        ok=soul.is_file() and soul.stat().st_size > 20,
        detail="present" if soul.is_file() else "missing",
        hint="ophelia migrate hermes or add SOUL.md",
        required=False,
    )


async def _check_memory_db(report: SelfCheckReport, settings: Settings) -> None:
    from ophelia.memory.store import MemoryStore

    db = settings.memory_db
    try:
        store = MemoryStore(db)
        await store.init()
        report.add(
            category="runtime",
            name="Memory database",
            ok=True,
            detail=str(db),
        )
    except Exception as e:
        report.add(
            category="runtime",
            name="Memory database",
            ok=False,
            detail=str(e)[:100],
            hint="check path and permissions",
        )


async def _check_providers(
    report: SelfCheckReport, stack: ProviderStack, *, chat_only: bool
) -> None:
    for role in ("chat", "consciousness", "vision", "curator", "image", "video"):
        required = role == "chat"
        good, msg = await stack.check(role)
        hint = ""
        if not good and role in ROLE_ENV:
            hint = f"set {ROLE_ENV[role]} or fix credentials"
        report.add(
            category="providers",
            name=f"Provider {role}",
            ok=good,
            detail=f"{stack.name(role)} -> {stack.model(role)} | {msg}",
            hint=hint,
            required=required,
        )
    _check_image_nsfw_routing(report, stack)


def _check_image_nsfw_routing(report: SelfCheckReport, stack: ProviderStack) -> None:
    """Surface SFW vs NSFW image backends so misroutes are obvious in check."""
    settings = stack.settings
    sfw = stack.image_provider_for(nsfw=False)
    nsfw_allowed = bool(settings.image_nsfw_allowed)
    if nsfw_allowed:
        nsfw = stack.image_provider_for(nsfw=True)
        nsfw_model = stack.image_model_for(nsfw, nsfw=True)
        detail = (
            f"SFW={sfw} | NSFW={nsfw}/{nsfw_model} (allowed=true, "
            f"OPHELIA_IMAGE_NSFW_PROVIDER={settings.image_nsfw_provider})"
        )
        censored = {"xai", "xai-oauth", "openai"}
        ok = nsfw not in censored
        hint = ""
        if not ok:
            hint = (
                "NSFW is allowed but resolved to a censored backend. "
                "Set OPHELIA_IMAGE_NSFW_PROVIDER=pollinations (or a1111/comfyui/…)."
            )
        report.add(
            category="providers",
            name="Image NSFW routing",
            ok=ok,
            detail=detail,
            hint=hint,
            required=False,
        )
    else:
        report.add(
            category="providers",
            name="Image NSFW routing",
            ok=True,
            detail=f"SFW={sfw} | NSFW disabled (OPHELIA_IMAGE_NSFW_ALLOWED=false)",
            hint="ophelia setup → Image generation to enable NSFW backends",
            required=False,
        )


async def _check_ollama_version(report: SelfCheckReport, settings: Settings) -> None:
    base = settings.ollama_base_url.rstrip("/").removesuffix("/v1")
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.get(f"{base}/api/version")
        if r.status_code != 200:
            report.add(
                category="services",
                name="Ollama daemon",
                ok=False,
                detail=f"HTTP {r.status_code}",
                hint="start Ollama: ollama serve",
                required=False,
            )
            return
        data = r.json()
        ver = data.get("version", "?")
        report.add(
            category="services",
            name="Ollama daemon",
            ok=True,
            detail=f"v{ver} @ {base}",
            required=False,
        )
    except httpx.HTTPError:
        report.add(
            category="services",
            name="Ollama daemon",
            ok=False,
            detail="not reachable",
            hint="install from ollama.com and run: ollama serve",
            required=False,
        )


async def _check_discord(report: SelfCheckReport, settings: Settings, *, chat_only: bool) -> None:
    if not settings.discord_enabled:
        report.add(
            category="services",
            name="Discord bot",
            ok=True,
            detail="disabled",
            required=False,
        )
        return
    token = settings.discord_bot_token
    if not token:
        report.add(
            category="services",
            name="Discord bot",
            ok=chat_only,
            detail="DISCORD_BOT_TOKEN not set",
            hint="Discord Developer Portal -> Bot -> Token",
            required=not chat_only and settings.discord_enabled,
        )
        return
    users = settings.allowed_discord_users()
    try:
        import discord  # noqa: F401
    except ImportError:
        report.add(
            category="services",
            name="Discord bot",
            ok=False,
            detail="discord.py not installed",
            hint="pip install -e .",
            required=settings.discord_enabled,
        )
        return
    report.add(
        category="services",
        name="Discord bot",
        ok=bool(users),
        detail="token set" + (f" | {len(users)} allowed user(s)" if users else " | DISCORD_ALLOWED_USER_IDS missing"),
        hint="Discord: User Settings -> Advanced -> Developer Mode -> right-click profile -> Copy User ID",
        required=settings.discord_enabled and not chat_only,
    )
    if settings.discord_log_enabled:
        gid = settings.discord_guild_id
        report.add(
            category="services",
            name="Discord log channels",
            ok=bool(gid),
            detail=f"guild_id={gid}" if gid else "DISCORD_GUILD_ID not set",
            hint="Right-click logging server icon -> Copy Server ID; bot needs Manage Channels",
            required=False,
        )


async def _check_channels(report: SelfCheckReport, settings: Settings, *, chat_only: bool) -> None:
    names = []
    if settings.telegram_enabled and settings.telegram_bot_token:
        names.append("telegram")
    if settings.discord_enabled and settings.discord_bot_token:
        names.append("discord")
    ok = bool(names) or chat_only
    report.add(
        category="config",
        name="Chat channels",
        ok=ok,
        detail=", ".join(names) if names else "none (use ophelia ui / ophelia chat)",
        hint="set TELEGRAM_* and/or DISCORD_* in ~/.ophelia/.env",
        required=not chat_only and is_termux(),
    )


async def _check_telegram(report: SelfCheckReport, settings: Settings, *, chat_only: bool) -> None:
    if not settings.telegram_enabled:
        report.add(
            category="services",
            name="Telegram bot",
            ok=True,
            detail="disabled",
            required=False,
        )
        return
    token = settings.telegram_bot_token
    if not token:
        required = is_termux() and not chat_only
        report.add(
            category="services",
            name="Telegram bot",
            ok=not required,
            detail="TELEGRAM_BOT_TOKEN not set",
            hint="@BotFather -> /newbot, add to ~/.ophelia/.env",
            required=required,
        )
        return
    try:
        from ophelia.channels.telegram_util import fetch_webhook_info

        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(f"https://api.telegram.org/bot{token}/getMe")
        data = r.json()
        if data.get("ok"):
            user = data.get("result", {})
            name = user.get("username") or user.get("first_name") or "bot"
            users = settings.allowed_telegram_users()
            detail = f"@{name}" if not str(name).startswith("@") else name
            if users:
                detail += f" | allowed IDs: {len(users)}"
            else:
                detail += " | TELEGRAM_ALLOWED_USER_IDS missing"
            webhook_url = ""
            try:
                wh = await fetch_webhook_info(token)
                webhook_url = str(wh.get("url") or "").strip()
            except httpx.HTTPError:
                pass
            ok = bool(users) and not webhook_url
            if webhook_url:
                detail += f" | WEBHOOK SET: {webhook_url[:40]}"
            report.add(
                category="services",
                name="Telegram bot",
                ok=ok,
                detail=detail,
                hint=(
                    "Hermes webhook blocks polling — ophelia run clears it, or deleteWebhook"
                    if webhook_url
                    else ("message @userinfobot for your ID" if not users else "")
                ),
                required=is_termux() and not chat_only and settings.telegram_enabled,
            )
        else:
            report.add(
                category="services",
                name="Telegram bot",
                ok=False,
                detail=data.get("description", "invalid token")[:80],
                hint="fix TELEGRAM_BOT_TOKEN in ~/.ophelia/.env",
                required=not chat_only,
            )
    except httpx.HTTPError as e:
        report.add(
            category="services",
            name="Telegram bot",
            ok=False,
            detail=f"network error: {e}",
            required=False,
        )


async def _check_android_body(report: SelfCheckReport, settings: Settings) -> None:
    if not settings.android_enabled:
        report.add(
            category="services",
            name="Phone body (optional)",
            ok=True,
            detail="disabled (OK — not required on PC/server/VPS)",
            required=False,
        )
        return
    from ophelia.android.factory import build_android_body

    body = build_android_body(settings)
    if not body:
        report.add(
            category="services",
            name="Phone body (optional)",
            ok=False,
            detail="enabled but failed to build",
            hint="check OPHELIA_ANDROID_ENABLED and paths",
            required=False,
        )
        return
    detail = body.status_line()
    mode = body.mode
    ok = mode not in ("termux_only", "none")
    if settings.adb_device and mode.startswith("adb"):
        await body.ensure_ready()
        ok = mode in ("adb", "adb_root")
        if not ok:
            detail += " | adb not connected"
    elif is_termux() and mode == "termux_only":
        ok = False
        detail += " | run termux-shizuku-setup.sh"
    report.add(
        category="services",
        name="Phone body (optional)",
        ok=ok,
        detail=detail,
        hint="docs/remote-adb.md or scripts/termux-shizuku-setup.sh",
        required=False,
    )


def _check_model_gate(report: SelfCheckReport) -> None:
    st = get_model_gate().status()
    busy = st.get("busy")
    active = st.get("active") or "idle"
    report.add(
        category="runtime",
        name="Model gate",
        ok=True,
        detail=f"{'busy' if busy else 'idle'} ({active})",
        required=False,
    )


def _check_optional_tools(report: SelfCheckReport) -> None:
    adb = shutil.which("adb")
    report.add(
        category="tools",
        name="adb (PC phone control)",
        ok=adb is not None,
        detail=adb or "not in PATH",
        hint="winget install Google.PlatformTools",
        required=False,
    )
    mcp_ok = importlib.util.find_spec("mcp") is not None
    report.add(
        category="tools",
        name="MCP bridge (optional)",
        ok=mcp_ok,
        detail="installed" if mcp_ok else "not installed",
        hint="pip install -e \".[mcp]\"",
        required=False,
    )


def _check_life_subsystems(report: SelfCheckReport, settings: Settings) -> None:
    """Tier A/B/C follow-up: verify the 'alive' subsystems are configured.

    These are the pieces that make her feel like one continuous person rather
    than a chat bot — director, voice mind, local ears, wake word, memory
    reconciliation. None are required (she runs fine with them off), but a
    misconfiguration here is the difference between 'feels alive' and 'just
    answers messages', so the doctor surfaces the state for tuning.
    """
    # IANA tz database — required for LifeContext on Termux/minimal installs.
    try:
        from zoneinfo import ZoneInfo

        ZoneInfo("UTC")
        tz_ok, tz_detail = True, "tzdata available"
    except Exception:
        tz_ok, tz_detail = False, "missing — LifeContext/chat will fail without it"
    report.add(
        category="life",
        name="Timezone data (tzdata)",
        ok=tz_ok,
        detail=tz_detail,
        hint="pip install tzdata  (included in pip install -e .)",
        required=False,
    )

    # Director (Tier A #1)
    if settings.director_enabled:
        report.add(
            category="life",
            name="Director",
            ok=True,
            detail="enabled — speaks/reacts/defers routing active",
            required=False,
        )
    else:
        report.add(
            category="life",
            name="Director",
            ok=True,
            detail="disabled (default) — all ticks go through unchanged",
            hint="set OPHELIA_DIRECTOR=true once you've tuned the prompt",
            required=False,
        )

    # Voice mind (Tier A #4)
    mode = (settings.voice_mind_mode or "post").strip().lower()
    report.add(
        category="life",
        name="Voice mind",
        ok=True,
        detail=f"mode={mode}",
        hint="set OPHELIA_VOICE_MIND_MODE=off if TTS sounds too rewritten" if mode == "off" else "",
        required=False,
    )

    # Local STT (Tier A #2)
    from ophelia.media.stt_local import local_stt_configured

    stt_on = local_stt_configured(settings)
    detail = "local whisper" if stt_on else f"provider={settings.stt_provider or 'auto'} (cloud/fallback)"
    report.add(
        category="life",
        name="Local STT (ears)",
        ok=True,
        detail=detail,
        hint="set OPHELIA_STT_PROVIDER=local + OPHELIA_WHISPER_SERVER_URL for offline ears" if not stt_on else "",
        required=False,
    )

    # Wake engine (Tier A #3)
    from ophelia.media.wake_engine import WakeEngine

    eng = WakeEngine(settings)
    wake_on = eng.available()
    if settings.wake_engine and settings.wake_engine.strip().lower() not in ("", "auto"):
        report.add(
            category="life",
            name="Wake word engine",
            ok=wake_on,
            detail=f"engine={settings.wake_engine}" + ("" if wake_on else " — configured but unavailable"),
            hint=eng._import_hint() if not wake_on else "",
            required=False,
        )
    else:
        report.add(
            category="life",
            name="Wake word engine",
            ok=True,
            detail="auto (STT polling fallback)",
            hint="set OPHELIA_WAKE_ENGINE=openwakeword or porcupine for always-on ears",
            required=False,
        )

    # Curator reconciliation (Tier C #13)
    if settings.curator_enabled:
        report.add(
            category="life",
            name="Memory curator",
            ok=True,
            detail=f"enabled — reconciles facts every {settings.curator_interval_hours:g}h",
            required=False,
        )
    else:
        report.add(
            category="life",
            name="Memory curator",
            ok=True,
            detail="disabled — stored facts never reconciled against context",
            hint="set OPHELIA_CURATOR=true to enable",
            required=False,
        )

    # Android kill-switch (Tier C #12) — only meaningful on Termux.
    if is_termux():
        from ophelia.android.harden import check_harden_status

        try:
            hs = check_harden_status(settings)
            report.add(
                category="life",
                name="Android kill-switch",
                ok=hs.overall_ok,
                detail=", ".join(c.name for c in hs.checks if not c.ok) or "all checks pass",
                hint="run: ophelia phone harden",
                required=False,
            )
        except Exception as e:
            report.add(
                category="life",
                name="Android kill-switch",
                ok=False,
                detail=f"check failed: {e}",
                hint="run: ophelia phone harden",
                required=False,
            )



async def run_self_check(
    settings: Settings | None = None,
    *,
    chat_only: bool = False,
    quick: bool = False,
) -> SelfCheckReport:
    settings = settings or Settings()
    report = SelfCheckReport(
        platform=platform_summary(),
        ophelia_home=str(OPHELIA_HOME),
    )

    _check_python(report)
    _check_ophelia_version(report)
    _check_dependencies(report)
    _check_paths(report, settings)
    _check_model_gate(report)
    _check_optional_tools(report)
    _check_life_subsystems(report, settings)

    if not quick:
        await _check_memory_db(report, settings)
        stack = build_provider_stack(settings)
        await _check_providers(report, stack, chat_only=chat_only)
        await _check_ollama_version(report, settings)
        await _check_channels(report, settings, chat_only=chat_only)
        await _check_telegram(report, settings, chat_only=chat_only)
        await _check_discord(report, settings, chat_only=chat_only)
        await _check_android_body(report, settings)

    return report


def format_report(report: SelfCheckReport, *, verbose: bool = False) -> str:
    lines = [
        "=" * 54,
        "  Ophelia self-check",
        f"  Platform: {report.platform}",
        f"  Home:     {report.ophelia_home}",
        f"  Version:  v{installed_version()}",
        "=" * 54,
        "",
    ]

    for category, items in report.by_category().items():
        lines.append(f"[{category.upper()}]")
        for r in items:
            mark = "OK" if r.ok else "FAIL"
            req = "" if r.required else " (optional)"
            lines.append(f"  {r.name:22} [{mark}]{req} {r.detail}")
            if (not r.ok or verbose) and r.hint:
                lines.append(f"                         -> {r.hint}")
        lines.append("")

    failed = report.failed()
    if report.ok:
        lines.append("RESULT: All required checks passed.")
        if runtime_label() == "windows" or runtime_label() in ("macos", "linux"):
            lines.append('Next: ophelia chat "hello"  or  ophelia ui')
        else:
            lines.append("Next: ophelia run")
    else:
        lines.append(f"RESULT: {len(failed)} required check(s) failed.")
        lines.append("Fix FAIL items above, then: ophelia check")
    return "\n".join(lines)
