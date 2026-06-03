from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

OPHELIA_HOME = Path.home() / ".ophelia"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=OPHELIA_HOME / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Provider: xai-oauth (SuperGrok, default) | xai (API key) | ollama (local)
    provider: str = Field(default="xai-oauth", alias="OPHELIA_PROVIDER")

    # xAI / Grok
    xai_api_key: str | None = Field(default=None, alias="XAI_API_KEY")
    xai_base_url: str = Field(default="https://api.x.ai/v1", alias="XAI_BASE_URL")
    xai_model: str = Field(default="grok-4", alias="XAI_MODEL")
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

    # Local (future training / tuning)
    ollama_base_url: str = Field(
        default="http://127.0.0.1:11434/v1",
        alias="OLLAMA_BASE_URL",
    )
    ollama_model: str = Field(default="llama3.2", alias="OLLAMA_MODEL")

    # Telegram
    telegram_bot_token: str | None = Field(default=None, alias="TELEGRAM_BOT_TOKEN")
    telegram_allowed_user_ids: str = Field(
        default="",
        alias="TELEGRAM_ALLOWED_USER_IDS",
    )

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
    listen_enabled_default: bool = Field(default=False, alias="OPHELIA_LISTEN")
    listen_seconds: int = Field(default=5, alias="OPHELIA_LISTEN_SECONDS")
    listen_interval_seconds: int = Field(default=45, alias="OPHELIA_LISTEN_INTERVAL")
    curator_enabled: bool = Field(default=True, alias="OPHELIA_CURATOR")
    curator_interval_hours: float = Field(default=6.0, alias="OPHELIA_CURATOR_HOURS")

    # Games layer (vision + tap/swipe; turn-based / idle friendly)
    games_enabled: bool = Field(default=True, alias="OPHELIA_GAMES")
    game_session_minutes: float = Field(
        default=15.0, alias="OPHELIA_GAME_SESSION_MINUTES"
    )
    game_max_turns: int = Field(default=40, alias="OPHELIA_GAME_MAX_TURNS")

    # Android body (Shizuku / phone_control.sh)
    android_enabled: bool = Field(default=True, alias="OPHELIA_ANDROID_ENABLED")
    phone_control_path: Path = Field(
        default=Path.home() / "phone_control.sh",
        alias="OPHELIA_PHONE_CONTROL",
    )

    # Paths
    data_dir: Path = Field(default=OPHELIA_HOME / "data")
    memory_db: Path = Field(default=OPHELIA_HOME / "data" / "memory.db")

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

    def primary_user_channel(self) -> str | None:
        users = self.allowed_telegram_users()
        if not users:
            return None
        uid = next(iter(users))
        return f"telegram:{uid}"


def ensure_dirs(settings: Settings) -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    OPHELIA_HOME.mkdir(parents=True, exist_ok=True)
