from pathlib import Path
from typing import Self

from pydantic import AliasChoices, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from ophelia.platform import is_termux, platform_summary

OPHELIA_HOME = Path.home() / ".ophelia"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=OPHELIA_HOME / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Provider routing: auto | xai-oauth | xai | ollama | openai | compat
    provider: str = Field(default="ollama", alias="OPHELIA_PROVIDER")
    provider_chat: str | None = Field(default=None, alias="OPHELIA_PROVIDER_CHAT")
    provider_consciousness: str | None = Field(
        default=None, alias="OPHELIA_PROVIDER_CONSCIOUSNESS"
    )
    provider_vision: str | None = Field(default=None, alias="OPHELIA_PROVIDER_VISION")
    provider_curator: str | None = Field(default=None, alias="OPHELIA_PROVIDER_CURATOR")
    provider_image: str | None = Field(default=None, alias="OPHELIA_PROVIDER_IMAGE")
    provider_video: str | None = Field(default=None, alias="OPHELIA_PROVIDER_VIDEO")
    auto_local_consciousness: bool = Field(
        default=True,
        alias="OPHELIA_AUTO_LOCAL_CONSCIOUSNESS",
        description="When provider=auto, use Ollama for consciousness ticks if reachable",
    )

    # xAI / Grok
    xai_api_key: str | None = Field(default=None, alias="XAI_API_KEY")
    xai_base_url: str = Field(default="https://api.x.ai/v1", alias="XAI_BASE_URL")
    xai_model: str = Field(default="grok-4", alias="XAI_MODEL")
    xai_consciousness_model: str | None = Field(
        default=None, alias="XAI_CONSCIOUSNESS_MODEL"
    )
    xai_curator_model: str | None = Field(default=None, alias="XAI_CURATOR_MODEL")
    xai_image_model: str = Field(default="grok-imagine-image", alias="XAI_IMAGE_MODEL")
    xai_video_model: str = Field(default="grok-imagine-video", alias="XAI_VIDEO_MODEL")
    vision_model: str | None = Field(default=None, alias="XAI_VISION_MODEL")
    xai_oauth_token_path: Path = Field(
        default=OPHELIA_HOME / "xai_oauth.json",
        alias="XAI_OAUTH_TOKEN_PATH",
    )
    grok_cli_auth_path: Path = Field(
        default=Path.home() / ".grok" / "auth.json",
        alias="GROK_CLI_AUTH_PATH",
    )
    hermes_auth_path: Path = Field(
        default=OPHELIA_HOME / "hermes_auth.json",
        alias="HERMES_AUTH_PATH",
    )
    hermes_home: Path = Field(
        default=Path.home() / ".hermes",
        alias="HERMES_HOME",
    )

    # OpenAI
    openai_api_key: str | None = Field(default=None, alias="OPENAI_API_KEY")
    openai_base_url: str = Field(
        default="https://api.openai.com/v1", alias="OPENAI_BASE_URL"
    )
    openai_model: str = Field(default="gpt-4o-mini", alias="OPENAI_MODEL")
    openai_consciousness_model: str | None = Field(
        default=None, alias="OPENAI_CONSCIOUSNESS_MODEL"
    )
    openai_curator_model: str | None = Field(default=None, alias="OPENAI_CURATOR_MODEL")
    openai_image_model: str = Field(default="dall-e-3", alias="OPENAI_IMAGE_MODEL")
    openai_vision_model: str | None = Field(default=None, alias="OPENAI_VISION_MODEL")

    # Generic OpenAI-compatible (OpenRouter, LM Studio, vLLM, etc.)
    compat_api_key: str | None = Field(default=None, alias="OPHELIA_COMPAT_API_KEY")
    compat_base_url: str | None = Field(default=None, alias="OPHELIA_COMPAT_BASE_URL")
    compat_model: str | None = Field(default=None, alias="OPHELIA_COMPAT_MODEL")
    compat_consciousness_model: str | None = Field(
        default=None, alias="OPHELIA_COMPAT_CONSCIOUSNESS_MODEL"
    )
    compat_curator_model: str | None = Field(
        default=None, alias="OPHELIA_COMPAT_CURATOR_MODEL"
    )
    compat_vision_model: str | None = Field(
        default=None, alias="OPHELIA_COMPAT_VISION_MODEL"
    )

    # Ollama (local)
    ollama_base_url: str = Field(
        default="http://127.0.0.1:11434/v1",
        alias="OLLAMA_BASE_URL",
    )
    ollama_model: str = Field(default="llama3.2", alias="OLLAMA_MODEL")
    ollama_consciousness_model: str | None = Field(
        default=None, alias="OLLAMA_CONSCIOUSNESS_MODEL"
    )
    ollama_curator_model: str | None = Field(default=None, alias="OLLAMA_CURATOR_MODEL")
    ollama_vision_model: str | None = Field(default=None, alias="OLLAMA_VISION_MODEL")
    ollama_image_model: str | None = Field(default=None, alias="OLLAMA_IMAGE_MODEL")

    # Telegram
    telegram_bot_token: str | None = Field(default=None, alias="TELEGRAM_BOT_TOKEN")
    telegram_allowed_user_ids: str = Field(
        default="",
        validation_alias=AliasChoices(
            "TELEGRAM_ALLOWED_USER_IDS",
            "TELEGRAM_ALLOWED_USERS",  # Hermes .env name
        ),
    )
    telegram_enabled: bool | None = Field(default=None, alias="OPHELIA_TELEGRAM_ENABLED")

    # Discord
    discord_bot_token: str | None = Field(default=None, alias="DISCORD_BOT_TOKEN")
    discord_allowed_user_ids: str = Field(
        default="",
        alias="DISCORD_ALLOWED_USER_IDS",
    )
    discord_enabled: bool | None = Field(default=None, alias="OPHELIA_DISCORD_ENABLED")

    # Which channel consciousness mirrors into (e.g. telegram:123 or discord:456)
    primary_channel: str | None = Field(default=None, alias="OPHELIA_PRIMARY_CHANNEL")

    # Consciousness loop (Neuro-style; NOT isolated cron sessions)
    consciousness_enabled: bool = Field(default=True, alias="OPHELIA_CONSCIOUSNESS")
    consciousness_interval_seconds: int = Field(
        default=90,
        alias="OPHELIA_CONSCIOUSNESS_INTERVAL",
        description="Base seconds between inner ticks; arousal adjusts speed",
    )

    # Legacy alias
    autonomy_enabled: bool | None = Field(default=None, alias="OPHELIA_AUTONOMY")
    autonomy_interval_seconds: int | None = Field(
        default=None, alias="OPHELIA_AUTONOMY_INTERVAL"
    )

    # Honcho (optional; uses Hermes honcho.json if present)
    honcho_api_key: str | None = Field(default=None, alias="HONCHO_API_KEY")
    honcho_context_tokens: int = Field(default=2000, alias="HONCHO_CONTEXT_TOKENS")

    # Voice (Telegram)
    voice_reply_default: bool = Field(default=False, alias="OPHELIA_VOICE_REPLY")
    tts_voice_id: str = Field(default="eve", alias="XAI_TTS_VOICE")

    # Initiative / will (lower = more spontaneous)
    initiative_threshold: float = Field(default=0.55, alias="OPHELIA_INITIATIVE_THRESHOLD")
    greet_on_start: bool = Field(
        default=True,
        alias="OPHELIA_GREET_ON_START",
        description="Send a proactive hello to the user when Ophelia comes online",
    )
    max_spontaneous_per_hour: int = Field(default=4, alias="OPHELIA_MAX_SPONTANEOUS_PER_HOUR")
    quiet_hours: str = Field(
        default="",
        alias="OPHELIA_QUIET_HOURS",
        description="e.g. 23-08 = no outreach 11pm-8am",
    )
    vision_enabled: bool = Field(default=True, alias="OPHELIA_VISION_ENABLED")

    # Tier 2: inner log, listen, curator
    inner_log_enabled: bool = Field(default=True, alias="OPHELIA_INNER_LOG")
    inner_mirror_telegram: bool = Field(default=False, alias="OPHELIA_INNER_MIRROR_TELEGRAM")
    listen_enabled_default: bool | None = Field(default=None, alias="OPHELIA_LISTEN")
    listen_seconds: int = Field(default=5, alias="OPHELIA_LISTEN_SECONDS")
    listen_interval_seconds: int = Field(default=45, alias="OPHELIA_LISTEN_INTERVAL")
    curator_enabled: bool = Field(default=True, alias="OPHELIA_CURATOR")
    curator_interval_hours: float = Field(default=6.0, alias="OPHELIA_CURATOR_HOURS")
    dream_enabled: bool = Field(
        default=True,
        alias="OPHELIA_DREAM",
        description="Offline consolidation/dreaming loop that extracts lessons from recent experience",
    )
    dream_interval_hours: float = Field(
        default=4.0,
        alias="OPHELIA_DREAM_HOURS",
        description="Hours between dream/consolidation cycles",
    )

    # Games layer (vision + tap/swipe; turn-based / idle friendly)
    games_enabled: bool | None = Field(default=None, alias="OPHELIA_GAMES")
    game_session_minutes: float = Field(
        default=15.0, alias="OPHELIA_GAME_SESSION_MINUTES"
    )
    game_max_turns: int = Field(default=40, alias="OPHELIA_GAME_MAX_TURNS")

    # Optional phone body — Termux Shizuku OR ADB from PC/server (not required)
    android_enabled: bool | None = Field(default=None, alias="OPHELIA_ANDROID_ENABLED")
    adb_device: str | None = Field(
        default=None,
        alias="OPHELIA_ADB_DEVICE",
        description="PC wireless/USB target e.g. 192.168.1.50:5555",
    )
    adb_root: bool = Field(
        default=False,
        alias="OPHELIA_ADB_ROOT",
        description="Try adb root for elevated shell (rooted phones only)",
    )
    phone_control_path: Path = Field(
        default=Path.home() / "phone_control.sh",
        alias="OPHELIA_PHONE_CONTROL",
    )

    # Paths
    data_dir: Path = Field(default=OPHELIA_HOME / "data")
    memory_db: Path = Field(default=OPHELIA_HOME / "data" / "memory.db")

    # Workstation UI (PC)
    ui_host: str = Field(default="127.0.0.1", alias="OPHELIA_UI_HOST")
    ui_port: int = Field(default=8765, alias="OPHELIA_UI_PORT")
    ui_open_browser: bool = Field(default=True, alias="OPHELIA_UI_OPEN_BROWSER")

    web_search_enabled: bool = Field(default=True, alias="OPHELIA_WEB_SEARCH")
    mcp_config_path: Path = Field(default=OPHELIA_HOME / "mcp.json", alias="OPHELIA_MCP_CONFIG")

    @model_validator(mode="after")
    def apply_platform_defaults(self) -> Self:
        on_phone = is_termux()
        if self.android_enabled is None:
            self.android_enabled = on_phone or bool(self.adb_device)
        if self.games_enabled is None:
            self.games_enabled = bool(self.android_enabled)
        if self.listen_enabled_default is None:
            self.listen_enabled_default = on_phone
        if self.telegram_enabled is None:
            self.telegram_enabled = bool(self.telegram_bot_token)
        if self.discord_enabled is None:
            self.discord_enabled = bool(self.discord_bot_token)
        return self

    def consciousness_on(self) -> bool:
        if self.autonomy_enabled is not None:
            return self.autonomy_enabled
        return self.consciousness_enabled

    def consciousness_interval(self) -> int:
        if self.autonomy_interval_seconds is not None:
            return self.autonomy_interval_seconds
        return self.consciousness_interval_seconds

    def allowed_telegram_users(self) -> set[int] | None:
        raw = self.telegram_allowed_user_ids.strip()
        if not raw:
            return None
        return {int(x.strip()) for x in raw.split(",") if x.strip()}

    def allowed_discord_users(self) -> set[int] | None:
        raw = self.discord_allowed_user_ids.strip()
        if not raw:
            return None
        return {int(x.strip()) for x in raw.split(",") if x.strip()}

    def primary_user_channel(self) -> str | None:
        if self.primary_channel and self.primary_channel.strip():
            return self.primary_channel.strip()
        if self.telegram_enabled and self.allowed_telegram_users():
            uid = next(iter(self.allowed_telegram_users()))
            return f"telegram:{uid}"
        if self.discord_enabled and self.allowed_discord_users():
            uid = next(iter(self.allowed_discord_users()))
            return f"discord:{uid}"
        return None

    def outreach_configured(self) -> bool:
        tg = self.telegram_enabled and self.telegram_bot_token and self.allowed_telegram_users()
        dc = self.discord_enabled and self.discord_bot_token and self.allowed_discord_users()
        return bool(tg or dc)

    def runtime_line(self) -> str:
        host = "phone" if is_termux() else "pc"
        return f"Runtime: {platform_summary()} ({host} — Ophelia Project)"


def ensure_dirs(settings: Settings) -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    OPHELIA_HOME.mkdir(parents=True, exist_ok=True)
