"""Guided setup wizard — step-by-step, Hermes/OpenClaw style."""

from __future__ import annotations

import asyncio
import shutil
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ophelia.config import OPHELIA_HOME, Settings, ensure_dirs
from ophelia.platform import is_termux, is_windows, platform_summary, runtime_label

CheckFn = Callable[[], tuple[bool, str]]


@dataclass
class SetupStep:
    num: int
    title: str
    why: str
    commands: list[str]
    check: CheckFn
    optional: bool = False
    manual_note: str = ""


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _example_env() -> Path | None:
    for p in (_repo_root() / "config.example.env", Path.cwd() / "config.example.env"):
        if p.is_file():
            return p
    return None


def _read_env_key(key: str) -> str:
    env_path = OPHELIA_HOME / ".env"
    if not env_path.is_file():
        return ""
    for line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        if k.strip() == key:
            return v.strip().strip('"').strip("'")
    return ""


def _check_ophelia_installed() -> tuple[bool, str]:
    try:
        import ophelia  # noqa: F401

        return True, "ophelia package importable"
    except ImportError:
        return False, "run: pip install -e . (from project folder)"


def _check_ophelia_home() -> tuple[bool, str]:
    if OPHELIA_HOME.is_dir():
        return True, str(OPHELIA_HOME)
    return False, f"will create {OPHELIA_HOME}"


def _check_env_file() -> tuple[bool, str]:
    p = OPHELIA_HOME / ".env"
    if p.is_file():
        return True, str(p)
    return False, "copy config.example.env -> ~/.ophelia/.env"


def _check_soul() -> tuple[bool, str]:
    soul = OPHELIA_HOME / "SOUL.md"
    if soul.is_file() and soul.stat().st_size > 20:
        return True, str(soul)
    hermes = Path.home() / ".hermes" / "SOUL.md"
    if hermes.is_file():
        return False, "SOUL in ~/.hermes only - run: ophelia migrate hermes"
    return False, "add ~/.ophelia/SOUL.md or migrate from Hermes"


def _check_chat_provider() -> tuple[bool, str]:
    async def _probe() -> tuple[bool, str]:
        from ophelia.providers.router import build_provider_stack

        stack = build_provider_stack(Settings())
        ok, msg = await stack.check("chat")
        return ok, msg

    try:
        return asyncio.run(_probe())
    except Exception as e:
        return False, str(e)[:80]


def _check_telegram() -> tuple[bool, str]:
    token = _read_env_key("TELEGRAM_BOT_TOKEN") or Settings().telegram_bot_token
    users = _read_env_key("TELEGRAM_ALLOWED_USER_IDS") or Settings().telegram_allowed_user_ids
    if token and users.strip():
        return True, "bot token + allowed user IDs set"
    if token:
        return False, "set TELEGRAM_ALLOWED_USER_IDS in ~/.ophelia/.env"
    return False, "optional for PC - get token from @BotFather on Telegram"


def _check_ollama_models() -> tuple[bool, str]:
    import httpx

    async def _probe() -> tuple[bool, str]:
        from ophelia.providers.cookbook import list_ollama_models

        s = Settings()
        base = s.ollama_base_url.rstrip("/").removesuffix("/v1")
        try:
            r = httpx.get(f"{base}/api/tags", timeout=2.0)
            up = r.status_code == 200
        except httpx.HTTPError:
            up = False
        if not up:
            return False, "Ollama not running - install from ollama.com, then: ollama serve"
        installed = await list_ollama_models(s)
        if installed:
            return True, f"{len(installed)} model(s): {', '.join(installed[:3])}"
        return False, "run: ollama pull llama3.2:3b"

    try:
        return asyncio.run(_probe())
    except Exception as e:
        return False, str(e)[:80]


def _check_shizuku() -> tuple[bool, str]:
    rish = Path.home() / "rish"
    pc = Path.home() / "phone_control.sh"
    if is_termux():
        if rish.is_file() and pc.is_file():
            return True, "~/rish + ~/phone_control.sh"
        if rish.is_file():
            return False, "run: bash scripts/termux-shizuku-setup.sh"
        return False, "Shizuku not wired - see step commands"
    adb = shutil.which("adb")
    device = _read_env_key("OPHELIA_ADB_DEVICE")
    if adb and device:
        return True, f"adb + OPHELIA_ADB_DEVICE={device}"
    if adb:
        return False, "set OPHELIA_ADB_DEVICE for wireless/USB phone"
    return False, "optional on PC - install platform-tools for ADB body"


def _check_wake_lock_hint() -> tuple[bool, str]:
    if not is_termux():
        return True, "N/A on PC"
    return False, "run once per session: termux-wake-lock"


def _pip_install_cmd() -> str:
    root = _repo_root()
    constraints = root / "constraints-termux.txt"
    if is_termux() and constraints.is_file():
        return (
            'export ANDROID_API_LEVEL="$(getprop ro.build.version.sdk)" && '
            f'pip install -e {root} -c {constraints}'
        )
    if (root / "pyproject.toml").is_file():
        return f"pip install -e {root}"
    return "pip install -e ."


def _copy_env_cmd() -> str:
    if is_windows():
        return (
            f'mkdir "{OPHELIA_HOME}" 2>nul & copy config.example.env "{OPHELIA_HOME}\\.env"'
        )
    return f'mkdir -p "{OPHELIA_HOME}" && cp config.example.env "{OPHELIA_HOME}/.env"'


def _steps_phone() -> list[SetupStep]:
    root = _repo_root()
    return [
        SetupStep(
            1,
            "Install Termux packages",
            "Python, git, tmux, and Termux:API for mic/listen.",
            [
                "pkg update -y",
                "pkg install -y python git tmux termux-api",
                f"cd {root}",
                "bash scripts/termux-install.sh",
            ],
            lambda: (shutil.which("python") is not None, "python in PATH"),
        ),
        SetupStep(
            2,
            "Install Ophelia (Python package)",
            "Editable install so `ophelia` CLI works.",
            [_pip_install_cmd()],
            _check_ophelia_installed,
        ),
        SetupStep(
            3,
            "Create config folder",
            "All secrets and persona live in ~/.ophelia/",
            [_copy_env_cmd(), f'# then edit: nano "{OPHELIA_HOME}/.env"'],
            _check_env_file,
        ),
        SetupStep(
            4,
            "Choose AI brain (provider)",
            "Local-first: Ollama on PC later; on phone xAI OAuth or Ollama if you run it.",
            [
                "# In ~/.ophelia/.env — pick ONE path:",
                "OPHELIA_PROVIDER=ollama",
                "OLLAMA_MODEL=llama3.2:3b",
                "# OR SuperGrok:",
                "OPHELIA_PROVIDER=xai-oauth",
                "ophelia auth import-grok    # after: grok login",
                "# OR from old Hermes phone:",
                "ophelia auth import-hermes",
                "ophelia models              # hardware tips if using Ollama",
            ],
            _check_chat_provider,
        ),
        SetupStep(
            5,
            "Persona (SOUL.md)",
            "Who Ophelia is — import from Hermes or write your own.",
            [
                "ophelia migrate hermes --dry-run",
                "ophelia migrate hermes",
                f'# or create: nano "{OPHELIA_HOME}/SOUL.md"',
            ],
            _check_soul,
            optional=True,
        ),
        SetupStep(
            6,
            "Telegram bot",
            "Optional — chat from anywhere (same on PC or Termux host).",
            [
                "# 1. Telegram -> @BotFather -> /newbot -> copy token",
                "# 2. Message @userinfobot -> copy your numeric ID",
                f'# 3. nano "{OPHELIA_HOME}/.env"',
                "TELEGRAM_BOT_TOKEN=123456:ABC...",
                "TELEGRAM_ALLOWED_USER_IDS=your_id",
            ],
            _check_telegram,
        ),
        SetupStep(
            7,
            "Keep Termux alive",
            "Stops Android killing Ophelia when the screen locks.",
            ["termux-wake-lock"],
            _check_wake_lock_hint,
            manual_note="Run each time you start a new Termux session (or use Termux:Boot).",
        ),
        SetupStep(
            8,
            "Phone body (Shizuku) — optional",
            "Only if you want screen/tap on this device. Skip for chat-only.",
            [
                "# On phone: install Shizuku app",
                "# Shizuku -> Start -> Export to Termux",
                "nano ~/rish   # line 11: PKG=com.termux",
                "chmod +x ~/rish",
                f"bash {root}/scripts/termux-shizuku-setup.sh",
                "bash ~/phone_control.sh ui-dump | head",
            ],
            _check_shizuku,
            optional=True,
        ),
        SetupStep(
            9,
            "Health check",
            "Confirms version, deps, providers, Telegram, and body.",
            ["ophelia check"],
            lambda: (False, "run ophelia check and fix any required FAIL lines"),
            manual_note="Re-run after fixing each FAIL. PC: ophelia check --chat-only",
        ),
        SetupStep(
            10,
            "Run Ophelia",
            "Long-lived session in tmux.",
            [
                "termux-wake-lock",
                "tmux new -s ophelia",
                "ophelia run",
                "# Detach: Ctrl+B then D. Reattach: tmux attach -t ophelia",
            ],
            lambda: (False, "start when steps 1-9 look good"),
            manual_note="Ctrl+C to stop. Use /pause in Telegram to quiet outreach.",
        ),
    ]


def _steps_pc() -> list[SetupStep]:
    root = _repo_root()
    ollama_url = "https://ollama.com/download"
    return [
        SetupStep(
            1,
            "Install Ophelia (Python package)",
            "Works on PC, laptop, home server, or VPS — no phone needed.",
            [
                f"cd {root}",
                _pip_install_cmd(),
            ],
            _check_ophelia_installed,
        ),
        SetupStep(
            2,
            "Create config folder",
            "Secrets and settings — never commit this folder.",
            [_copy_env_cmd(), f'# edit: notepad "{OPHELIA_HOME}\\.env"' if is_windows() else f'nano "{OPHELIA_HOME}/.env"'],
            _check_env_file,
        ),
        SetupStep(
            3,
            "Install Ollama (local brain)",
            "Recommended default — free, always-on consciousness later.",
            [
                f"# Download: {ollama_url}",
                "ollama serve",
                "ollama pull llama3.2:3b",
                "ollama pull llava:7b",
                "ophelia models",
            ],
            _check_ollama_models,
            optional=True,
        ),
        SetupStep(
            4,
            "Configure provider",
            "Local-first; cloud optional for image/video.",
            [
                f'# Edit "{OPHELIA_HOME}/.env":',
                "OPHELIA_PROVIDER=ollama",
                "OLLAMA_MODEL=llama3.2:3b",
                "OPHELIA_CONSCIOUSNESS=true",
                "# Optional cloud image/video:",
                "OPHELIA_PROVIDER_IMAGE=xai-oauth",
                "ophelia auth import-grok",
                "ophelia providers",
            ],
            _check_chat_provider,
        ),
        SetupStep(
            5,
            "Persona (SOUL.md)",
            "Personality file — or import Hermes history.",
            [
                "ophelia migrate hermes",
                "ophelia transfer cloud-download \"URL\"",
                f'# or: "{OPHELIA_HOME}/SOUL.md"',
            ],
            _check_soul,
            optional=True,
        ),
        SetupStep(
            6,
            "Verify chat works",
            "One-shot test without Telegram.",
            [
                "ophelia check --chat-only",
                'ophelia chat "hello, who are you?"',
                "ophelia ui",
            ],
            lambda: (False, "run ophelia check --chat-only until chat provider is OK"),
            manual_note="UI opens browser workstation at http://127.0.0.1:8765",
        ),
        SetupStep(
            7,
            "Telegram / Discord (optional)",
            "Run ophelia run on this host for 24/7 bots — no phone body required.",
            [
                "TELEGRAM_BOT_TOKEN=...",
                "TELEGRAM_ALLOWED_USER_IDS=your_id",
                "DISCORD_BOT_TOKEN=...",
                "DISCORD_ALLOWED_USER_IDS=your_id",
                "ophelia run",
            ],
            _check_telegram,
            optional=True,
        ),
        SetupStep(
            8,
            "Phone body via ADB (optional)",
            "Attach a separate Android for screen/tap — skip for software-only Ophelia.",
            [
                "# Enable USB debugging on phone",
                "adb devices",
                "adb connect PHONE_IP:5555",
                f'# "{OPHELIA_HOME}/.env":',
                "OPHELIA_ANDROID_ENABLED=true",
                "OPHELIA_ADB_DEVICE=192.168.x.x:5555",
                "ophelia doctor --chat-only",
            ],
            _check_shizuku,
            optional=True,
            manual_note="Full guide: docs/remote-adb.md",
        ),
        SetupStep(
            9,
            "Daily use",
            "You're ready.",
            [
                "ophelia ui",
                "ophelia chat \"...\"",
                "ophelia run",
                "ophelia setup",
            ],
            lambda: (True, "re-run ophelia setup anytime for a checklist"),
        ),
    ]


def _banner(mode: str) -> None:
    line = "=" * 52
    print(line)
    print("  The Ophelia Project - Setup Guide")
    print(f"  Platform: {platform_summary()}")
    print(f"  Mode:     {mode}")
    print(f"  Home:     {OPHELIA_HOME}")
    print(line)
    print()


def _status_mark(done: bool) -> str:
    return "[OK]" if done else "[  ]"


def _print_step(step: SetupStep, *, verbose: bool = True) -> bool:
    done, detail = step.check()
    opt = " (optional)" if step.optional else ""
    print(f"Step {step.num}{opt}: {step.title} {_status_mark(done)}")
    if verbose:
        print(f"  Why: {step.why}")
        if detail:
            print(f"  Status: {detail}")
        if not done or verbose:
            print("  Do this:")
            for cmd in step.commands:
                print(f"    {cmd}")
        if step.manual_note:
            print(f"  Note: {step.manual_note}")
        print()
    return done


def _auto_setup() -> list[str]:
    """Safe automated steps only (dirs + example .env)."""
    actions: list[str] = []
    ensure_dirs(Settings())
    actions.append(f"Created {OPHELIA_HOME}")

    env_dest = OPHELIA_HOME / ".env"
    if not env_dest.is_file():
        src = _example_env()
        if src:
            shutil.copy2(src, env_dest)
            actions.append(f"Copied {src.name} -> {env_dest}")
        else:
            actions.append("config.example.env not found — create .env manually")

    for name, example in (
        ("goals.yaml", "goals.example.yaml"),
        ("PROMPTER.md", "PROMPTER.example.md"),
    ):
        dest = OPHELIA_HOME / name
        if not dest.is_file():
            src = _repo_root() / example
            if src.is_file():
                shutil.copy2(src, dest)
                actions.append(f"Copied {example} -> {dest}")

    return actions


def run_setup_wizard(
    *,
    phone: bool | None = None,
    interactive: bool = False,
    do_auto: bool = False,
    step_num: int | None = None,
) -> int:
    on_phone = is_termux() if phone is None else phone
    mode = "phone host (Termux)" if on_phone else "PC / server / VPS"
    steps = _steps_phone() if on_phone else _steps_pc()

    _banner(mode)

    if do_auto:
        print("Running safe auto-setup...")
        for a in _auto_setup():
            print(f"  + {a}")
        print()

    if step_num is not None:
        match = [s for s in steps if s.num == step_num]
        if not match:
            print(f"No step {step_num}. Steps 1-{len(steps)}.")
            return 1
        _print_step(match[0])
        return 0

    done_count = 0
    required_total = sum(1 for s in steps if not s.optional)

    for step in steps:
        done = _print_step(step)
        if done:
            done_count += 1
        if interactive and not done:
            try:
                input("  Press Enter when this step is done (or Ctrl+C to exit)... ")
            except (KeyboardInterrupt, EOFError):
                print("\nSetup paused. Re-run: ophelia setup")
                return 0

    print("-" * 52)
    print(f"Progress: {done_count}/{len(steps)} steps passing checks")
    print()
    print("Quick commands:")
    print("  ophelia setup --do          # create ~/.ophelia + .env again")
    print("  ophelia setup --step N      # one step only")
    print("  ophelia setup -i            # interactive (pause between steps)")
    print("  ophelia check               # full self-check (version, deps, runtime)")
    print("  ophelia doctor --chat-only  # same, PC mode (no Telegram required)")
    print()

    incomplete_required = [
        s for s in steps if not s.optional and not s.check()[0]
    ]
    # Steps 9-10 on phone are manual by design — don't fail install
    if on_phone:
        incomplete_required = [
            s for s in incomplete_required if s.num <= 8
        ]
    if incomplete_required and done_count < required_total:
        print("Next: fix the first [  ] step above, then re-run ophelia setup")
        return 1
    print("Looking good - start Ophelia when you're ready.")
    return 0
