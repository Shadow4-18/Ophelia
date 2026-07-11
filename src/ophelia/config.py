import os
from pathlib import Path
from typing import ClassVar, Self

from pydantic import AliasChoices, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from ophelia.platform import is_termux, platform_summary

OPHELIA_HOME = Path(os.environ.get("OPHELIA_HOME") or (Path.home() / ".ophelia"))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=OPHELIA_HOME / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Provider routing: auto | xai-oauth | xai | ollama | openai | deepseek | compat
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

    # Fallback: if the primary provider for a role fails (rate limit, 5xx,
    # network), retry on these fallback providers in order before giving up.
    # Comma-separated list of provider names. Empty = no fallback.
    fallback_providers: str = Field(
        default="",
        alias="OPHELIA_FALLBACK_PROVIDERS",
        description="Comma-separated providers tried in order when the primary fails",
    )
    fallback_model: str | None = Field(
        default=None,
        alias="OPHELIA_FALLBACK_MODEL",
        description="If set, use this model name on every fallback provider",
    )

    # xAI / Grok
    # GROK_API_KEY / GROK_API_URL / GROK_MODEL are accepted as aliases so a
    # working config from another Grok bot (e.g. a Discord bot) drops in
    # without renaming. Ophelia treats them identically to XAI_*.
    xai_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("XAI_API_KEY", "GROK_API_KEY"),
    )
    xai_base_url: str = Field(
        default="https://api.x.ai/v1",
        validation_alias=AliasChoices("XAI_BASE_URL", "GROK_API_URL"),
    )
    xai_model: str = Field(
        default="grok-4",
        validation_alias=AliasChoices("XAI_MODEL", "GROK_MODEL"),
    )
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

    # DeepSeek (OpenAI-compatible; cheap Flash model for cost-sensitive roles)
    deepseek_api_key: str | None = Field(default=None, alias="DEEPSEEK_API_KEY")
    deepseek_base_url: str = Field(
        default="https://api.deepseek.com/v1", alias="DEEPSEEK_BASE_URL"
    )
    deepseek_model: str = Field(default="deepseek-v4-flash", alias="DEEPSEEK_MODEL")
    deepseek_consciousness_model: str | None = Field(
        default=None, alias="DEEPSEEK_CONSCIOUSNESS_MODEL"
    )
    deepseek_curator_model: str | None = Field(
        default=None, alias="DEEPSEEK_CURATOR_MODEL"
    )
    deepseek_vision_model: str | None = Field(
        default=None, alias="DEEPSEEK_VISION_MODEL"
    )
    # DeepSeek V4 enables "thinking mode" by default, which returns a long
    # reasoning_content and tends to reason past tool calls (e.g. it talks
    # itself out of calling web_search). Default off for snappy tool-driven
    # turns; set OPHELIA_DEEPSEEK_THINKING=true to enable for harder prompts.
    deepseek_thinking: bool = Field(
        default=False,
        alias="OPHELIA_DEEPSEEK_THINKING",
        description="Enable DeepSeek V4 thinking mode (returns reasoning_content). "
        "Off by default — thinking mode often skips tool calls like web_search.",
    )

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
    # Auto-start `ollama serve` when Ophelia needs Ollama and it isn't running.
    # None = auto (on under Termux, off elsewhere); True/False to force.
    ollama_autostart: bool | None = Field(
        default=None,
        alias="OPHELIA_OLLAMA_AUTOSTART",
        description="Auto-start ollama serve if down (auto = on under Termux)",
    )
    # How long Ollama keeps a model in memory after each call. Default 5m is
    # too short for infrequent roles like vision — every photo would reload
    # ~1GB from flash (10-25s stall). 30m keeps it warm between uses; -1 keeps
    # it loaded indefinitely (more RAM, fastest). Passed to ollama serve as
    # OLLAMA_KEEP_ALIVE and per-request as keep_alive.
    ollama_keep_alive: str = Field(
        default="30m",
        alias="OPHELIA_OLLAMA_KEEP_ALIVE",
        description="Ollama model residency (e.g. 30m, 24h, -1 to always keep loaded)",
    )

    # ---- Image generation backends (media-only; selected via OPHELIA_PROVIDER_IMAGE) ----
    # These providers can't serve chat/vision — only the image role. Several are
    # NSFW-capable (pollinations, a1111, comfyui, fal, replicate, civitai,
    # modelslab, ollama). xAI/OpenAI are NOT — they refuse explicit prompts.
    image_nsfw_allowed: bool = Field(
        default=False,
        alias="OPHELIA_IMAGE_NSFW_ALLOWED",
        description=(
            "Content tier. When true, the agent may write explicit prompts and "
            "explicit requests are auto-routed to an uncensored backend (never "
            "xAI/OpenAI). When false, explicit requests are refused."
        ),
    )
    image_nsfw_provider: str = Field(
        default="auto",
        alias="OPHELIA_IMAGE_NSFW_PROVIDER",
        description=(
            "Provider used for explicit images. auto = first configured "
            "uncensored backend (pollinations > a1111 > comfyui > modelslab > "
            "civitai > fal > replicate > ollama)."
        ),
    )
    image_nsfw_model: str | None = Field(
        default=None,
        alias="OPHELIA_IMAGE_NSFW_MODEL",
        description=(
            "Optional model override for explicit images. When set, NSFW "
            "requests use this model on the NSFW provider instead of that "
            "provider's normal image-model env (e.g. POLLINATIONS_IMAGE_MODEL)."
        ),
    )

    # Pollinations — free, no API key, lax on NSFW (safe=false).
    pollinations_base_url: str = Field(
        default="https://image.pollinations.ai", alias="POLLINATIONS_BASE_URL"
    )
    pollinations_image_model: str = Field(
        default="flux", alias="POLLINATIONS_IMAGE_MODEL"
    )

    # Automatic1111 / SDWebUI with --api (local, uncensored, LoRAs/samplers).
    a1111_base_url: str = Field(default="http://127.0.0.1:7860", alias="A1111_BASE_URL")
    a1111_api_key: str | None = Field(default=None, alias="A1111_API_KEY")
    a1111_image_model: str | None = Field(
        default=None,
        alias="A1111_IMAGE_MODEL",
        description="Checkpoint name (optional override of webUI default)",
    )
    a1111_steps: int = Field(default=30, alias="A1111_STEPS")
    a1111_sampler: str = Field(default="DPM++ 2M Karras", alias="A1111_SAMPLER")
    a1111_cfg_scale: float = Field(default=7.0, alias="A1111_CFG_SCALE")

    # ComfyUI (local, uncensored). Uses a txt2img workflow graph; override the
    # graph by pointing COMFYUI_WORKFLOW_PATH at a workflow JSON export.
    comfyui_base_url: str = Field(default="http://127.0.0.1:8188", alias="COMFYUI_BASE_URL")
    comfyui_workflow_path: Path = Field(
        default=OPHELIA_HOME / "comfyui_workflow.json", alias="COMFYUI_WORKFLOW_PATH"
    )
    comfyui_image_model: str | None = Field(
        default=None,
        alias="COMFYUI_IMAGE_MODEL",
        description="Checkpoint filename in ComfyUI (optional)",
    )

    # fal.ai (fast cloud; flux/sdxl NSFW-tolerant variants).
    fal_api_key: str | None = Field(default=None, alias="FAL_API_KEY")
    fal_image_model: str = Field(default="fal-ai/fast-sdxl", alias="FAL_IMAGE_MODEL")

    # Replicate (cloud; many NSFW-allowed community models).
    replicate_api_key: str | None = Field(default=None, alias="REPLICATE_API_KEY")
    replicate_image_model: str = Field(
        default="stability-ai/sdxl", alias="REPLICATE_IMAGE_MODEL"
    )

    # Civitai Orchestration (hosts NSFW checkpoints/LoRAs; generation API).
    civitai_api_key: str | None = Field(default=None, alias="CIVITAI_API_KEY")
    civitai_image_model: str = Field(
        default="",
        alias="CIVITAI_IMAGE_MODEL",
        description=(
            "Optional FALLBACK checkpoint AIR URN if runtime auto-pick fails. "
            "Leave blank (recommended) — Ophelia picks checkpoint/LoRA per image "
            "from the prompt. Not a lock."
        ),
    )
    civitai_base_url: str = Field(
        default="https://orchestration.civitai.com", alias="CIVITAI_BASE_URL"
    )

    # ModelsLab (hosted SD APIs; explicit/adult models; safety_checker=false).
    modelslab_api_key: str | None = Field(default=None, alias="MODELSLAB_API_KEY")
    modelslab_image_model: str = Field(default="flux", alias="MODELSLAB_IMAGE_MODEL")
    modelslab_base_url: str = Field(
        default="https://modelslab.com/api/v6", alias="MODELSLAB_BASE_URL"
    )

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
    # Discord logging server — Ophelia auto-creates categories/channels here.
    discord_guild_id: int | None = Field(default=None, alias="DISCORD_GUILD_ID")
    discord_log_enabled: bool | None = Field(
        default=None,
        alias="OPHELIA_DISCORD_LOG",
        description=(
            "Mirror chat logs into Discord categories/channels in DISCORD_GUILD_ID. "
            "Default: on when DISCORD_GUILD_ID is set."
        ),
    )

    # Which channel consciousness mirrors into (e.g. telegram:123 or discord:456)
    primary_channel: str | None = Field(default=None, alias="OPHELIA_PRIMARY_CHANNEL")

    # Owner identity — the one user whose conversations shape her memory/personality/
    # soul/evolution. Everyone else is a sandboxed guest. Channel-style, e.g.
    # "telegram:12345" or comma-separated "telegram:111,discord:222". If unset,
    # primary_user_channel() is treated as the owner (backward compatible).
    owner_id: str = Field(
        default="",
        alias="OPHELIA_OWNER_ID",
        description=(
            "Channel-style owner id(s): 'telegram:12345' or comma-separated. "
            "Only this user shapes her identity; all others are sandboxed guests."
        ),
    )
    # Universal chat log: every inbound/outbound message + media, for oversight.
    chat_log_enabled: bool = Field(
        default=True,
        alias="OPHELIA_CHAT_LOG",
        description="Log every message sent to/from her (text + media) to ~/.ophelia/data/logs/",
    )
    # How unknown (non-owner, non-allowlisted) users are handled when they message her.
    #   approve — hold their message and prompt the owner to Accept/Decline (default).
    #              Accepting auto-adds their ID to the platform allowlist in ~/.ophelia/.env.
    #   open    — admit unknown users immediately as sandboxed guests (no prompt).
    #   reject  — refuse unknown users outright (strict; pre-this-feature behavior).
    guest_admission: str = Field(
        default="approve",
        alias="OPHELIA_GUEST_ADMISSION",
        description="approve | open | reject — how unknown users who message her are handled",
    )

    # Consciousness loop (Neuro-style; NOT isolated cron sessions)
    consciousness_enabled: bool = Field(default=True, alias="OPHELIA_CONSCIOUSNESS")
    consciousness_interval_seconds: int = Field(
        default=90,
        alias="OPHELIA_CONSCIOUSNESS_INTERVAL",
        description="Base seconds between inner ticks; arousal adjusts speed",
    )
    # After she acts (outreach / act / explore), suppress inner ticks for this
    # many seconds so she gets breathing room instead of an immediate next tick.
    # Her idea: "if I just sent a 🖤, don't tick again for 5 minutes." 0 = off.
    tick_action_cooldown_seconds: int = Field(
        default=300,
        alias="OPHELIA_TICK_ACTION_COOLDOWN",
        description="Suppress ticks for N seconds after she acts/outreaches (0 = off)",
    )
    # When fully idle with no due goal, rotate the nudge mode (reflect/create/
    # explore/social) so ticks aren't identical every time. Her "uniformity" fix.
    tick_idle_nudge_rotate: bool = Field(
        default=True,
        alias="OPHELIA_TICK_IDLE_NUDGE_ROTATE",
        description="Rotate idle nudge modes so ticks vary when nothing's due",
    )

    # Agentic tool loop (per turn)
    max_tool_rounds: int = Field(
        default=25,
        alias="OPHELIA_MAX_TOOL_ROUNDS",
        description="Hard cap on tool-call rounds in a single turn before bailing out",
    )
    tool_loop_resume: bool = Field(
        default=True,
        alias="OPHELIA_TOOL_LOOP_RESUME",
        description=(
            "If a turn hits the tool-round cap but isn't stuck repeating the same "
            "tool call, allow the next user turn to resume from the unfinished "
            "tool chain instead of starting fresh."
        ),
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
    # TTS backend: auto | xai | elevenlabs | openai | kokoro
    # auto = first configured of: elevenlabs -> kokoro -> openai -> xai
    tts_provider: str = Field(default="auto", alias="OPHELIA_TTS_PROVIDER")
    # ElevenLabs (cloud, best quality)
    elevenlabs_api_key: str | None = Field(default=None, alias="ELEVENLABS_API_KEY")
    elevenlabs_voice_id: str = Field(
        default="21m00Tcm4TlvDq8ikWAM", alias="ELEVENLABS_VOICE_ID"
    )
    elevenlabs_tts_model: str = Field(
        default="eleven_multilingual_v2", alias="ELEVENLABS_TTS_MODEL"
    )
    # OpenAI TTS (cloud; uses OPENAI_API_KEY)
    openai_tts_model: str = Field(default="gpt-4o-mini-tts", alias="OPENAI_TTS_MODEL")
    openai_tts_voice: str = Field(default="nova", alias="OPENAI_TTS_VOICE")
    # Kokoro-82M via a local OpenAI-compatible server (Kokoro-FastAPI on PC, or
    # the Rust `koko openai` server natively in Termux). Fully offline.
    kokoro_tts_url: str | None = Field(default=None, alias="KOKORO_TTS_URL")
    kokoro_tts_voice: str = Field(
        default="af_heart",
        alias="KOKORO_TTS_VOICE",
        description=(
            "Kokoro preset or mix expression, e.g. af_heart or "
            "af_bella(0.7)+bf_emma(0.3). Mixes should be baked via "
            "`ophelia tts combine` (or set KOKORO_VOICES_DIR) — raw inline "
            "mixes from Kokoro-FastAPI omit L2 renorm and sound muffled/peaky."
        ),
    )
    kokoro_tts_speed: float = Field(
        default=1.0,
        alias="KOKORO_TTS_SPEED",
        ge=0.5,
        le=2.0,
        description="Speech rate for Kokoro (0.85 thoughtful, 1.0 normal, 1.15 hyped)",
    )
    # Directory where Kokoro-FastAPI loads ``*.pt`` voice packs. When set,
    # Ophelia installs L2-renormalized baked mixes here so the speech API can
    # load them by a short name instead of doing a broken server-side blend.
    kokoro_voices_dir: str | None = Field(
        default=None,
        alias="KOKORO_VOICES_DIR",
        description=(
            "Path to Kokoro-FastAPI voices folder (e.g. Docker mount). "
            "Required for automatic install of baked voice mixes."
        ),
    )
    # Auto-start the local Kokoro server (`koko openai`) when configured and down.
    # None = auto (on under Termux, off elsewhere); True/False to force.
    kokoro_autostart: bool | None = Field(
        default=None,
        alias="OPHELIA_KOKORO_AUTOSTART",
        description="Auto-start koko openai if down (auto = on under Termux)",
    )
    kokoro_listen_port: int | None = Field(
        default=None,
        alias="KOKORO_LISTEN_PORT",
        description="Port for koko openai (default: parsed from KOKORO_TTS_URL or 8880)",
    )
    kokoro_koko_bin: str | None = Field(
        default=None,
        alias="KOKORO_KOKO_BIN",
        description="Path to koko binary for autostart",
    )
    kokoro_koko_cwd: str | None = Field(
        default=None,
        alias="KOKORO_KOKO_CWD",
        description="Working directory for koko (checkpoints/ and data/)",
    )
    kokoro_autostart_cmd: str | None = Field(
        default=None,
        alias="KOKORO_AUTOSTART_CMD",
        description="Shell command to start Kokoro when autostart is enabled",
    )

    # Tier A #4: voice mind — speech-first TTS prep layer.
    # inline = rewrite before TTS (better, +300-800ms latency).
    # post   = send raw reply fast, refine on next turn (default).
    # off    = disabled; raw text goes to TTS.
    voice_mind_mode: str = Field(
        default="post",
        alias="OPHELIA_VOICE_MIND_MODE",
        description="inline | post | off — how the voice mind rewrites text for speech",
    )

    # Initiative / will (lower = more spontaneous)
    initiative_threshold: float = Field(default=0.45, alias="OPHELIA_INITIATIVE_THRESHOLD")
    greet_on_start: bool = Field(
        default=True,
        alias="OPHELIA_GREET_ON_START",
        description="Send a proactive hello to the user when Ophelia comes online",
    )
    # Tier A #1: director — fast decision layer over the ensemble. Decides
    # whether to speak / react / defer / skip before the heavy chat call, and
    # sets urgency + pacing knobs. Off by default until you've tuned it.
    director_enabled: bool = Field(
        default=False,
        alias="OPHELIA_DIRECTOR",
        description=(
            "Enable the director mind — decides whether/when/how to respond "
            "before the chat LLM runs. Logs decisions to data/director_log.jsonl."
        ),
    )
    max_spontaneous_per_hour: int = Field(default=6, alias="OPHELIA_MAX_SPONTANEOUS_PER_HOUR")
    quiet_hours: str = Field(
        default="",
        alias="OPHELIA_QUIET_HOURS",
        description="e.g. 23-08 = no outreach 11pm-8am",
    )
    vision_enabled: bool = Field(default=True, alias="OPHELIA_VISION_ENABLED")
    # Draw a coordinate grid (with pixel labels) on screenshots sent to vision
    # so tap coordinates are read in native pixels, not the model's internally
    # resized image space. Fixes the classic "taps land off-target" problem.
    vision_grid_overlay: bool = Field(
        default=True,
        alias="OPHELIA_VISION_GRID",
        description="Annotate screenshots with a native-pixel grid for accurate taps",
    )

    # Tier 2: inner log, listen, curator
    inner_log_enabled: bool = Field(default=True, alias="OPHELIA_INNER_LOG")
    inner_mirror_telegram: bool = Field(default=False, alias="OPHELIA_INNER_MIRROR_TELEGRAM")
    listen_enabled_default: bool | None = Field(default=None, alias="OPHELIA_LISTEN")
    listen_seconds: int = Field(default=5, alias="OPHELIA_LISTEN_SECONDS")
    listen_interval_seconds: int = Field(default=45, alias="OPHELIA_LISTEN_INTERVAL")

    # Life context — authoritative time/schedule/presence (phone is stationary at home)
    timezone: str = Field(
        default="UTC",
        alias="OPHELIA_TIMEZONE",
        description="IANA timezone e.g. America/New_York",
    )
    work_days: str = Field(
        default="",
        alias="OPHELIA_WORK_DAYS",
        description="Owner work days e.g. Thu,Wed,Tue,Fri",
    )
    work_hours: str = Field(
        default="",
        alias="OPHELIA_WORK_HOURS",
        description="Owner work hours e.g. 18-06 (6pm-6am cross-midnight)",
    )
    home_wifi_ssid: str = Field(default="", alias="OPHELIA_HOME_WIFI_SSID")
    sleep_hours: str = Field(
        default="",
        alias="OPHELIA_SLEEP_HOURS",
        description="Owner sleep window e.g. 1-7 (overrides quiet_hours for sleep mode)",
    )
    sleep_mode_enabled: bool = Field(default=True, alias="OPHELIA_SLEEP_MODE")

    # Tier B #7: owner presence signals (BT / router / last-seen). Sharpen
    # "is he home?" beyond schedule + silence. Best-effort: missing binaries
    # or failed scans degrade to "unknown" and schedule-based inference runs.
    owner_bt_devices: str = Field(
        default="",
        alias="OPHELIA_OWNER_BT_DEVICES",
        description=(
            "Comma-separated BT MACs or device names to watch for "
            "(termux-bt-scan). e.g. '00:11:22:33:44:55,My-AirPods'"
        ),
    )
    owner_router_api_url: str = Field(
        default="",
        alias="OPHELIA_OWNER_ROUTER_API_URL",
        description="Router device-list endpoint (HTTP GET) for presence polling",
    )
    owner_router_api_header: str = Field(
        default="",
        alias="OPHELIA_OWNER_ROUTER_API_HEADER",
        description="Optional header for router API e.g. 'Authorization: Bearer ...'",
    )

    # Wake word (Termux mic — say her name to talk)
    wake_word_enabled: bool = Field(default=False, alias="OPHELIA_WAKE_WORD")
    wake_word: str = Field(default="ophelia", alias="OPHELIA_WAKE_WORD_NAME")
    wake_word_rms_threshold: int = Field(default=200, alias="OPHELIA_WAKE_RMS")

    # Tier A #2: local STT provider. auto = cloud (xAI) for backward compat;
    # local = whisper.cpp server / whisper-cli on-device. Falls back to cloud
    # if the local server/CLI is unreachable so a missing server never breaks
    # the listen loop.
    stt_provider: str = Field(
        default="auto",
        alias="OPHELIA_STT_PROVIDER",
        description="auto (cloud xAI) | local (whisper.cpp on-device)",
    )
    whisper_server_url: str = Field(
        default="",
        alias="WHISPER_SERVER_URL",
        description="OpenAI-compatible whisper.cpp server URL e.g. http://127.0.0.1:8080/v1",
    )
    whisper_model: str = Field(
        default="",
        alias="WHISPER_MODEL",
        description="Model name for the whisper.cpp server (e.g. tiny.en, base.en)",
    )

    # Tier A #3: real wake-word engine. auto = STT polling (legacy); openwakeword
    # or porcupine = dedicated keyword-spotting engine on the mic stream.
    wake_engine: str = Field(
        default="auto",
        alias="OPHELIA_WAKE_ENGINE",
        description="auto (STT polling) | openwakeword | porcupine",
    )
    wake_engine_sensitivity: float = Field(
        default=0.5,
        alias="OPHELIA_WAKE_ENGINE_SENSITIVITY",
        ge=0.0,
        le=1.0,
        description="Wake-engine keyword sensitivity (0 = strict, 1 = permissive)",
    )
    porcupine_access_key: str = Field(
        default="",
        alias="PORCUPINE_ACCESS_KEY",
        description="Picovoice access key for Porcupine wake-word engine",
    )
    porcupine_keyword_path: str = Field(
        default="",
        alias="PORCUPINE_KEYWORD_PATH",
        description="Path to a custom .ppn keyword file for Porcupine",
    )

    # Autonomous life (Neuro-style presence)
    spontaneous_voice_enabled: bool = Field(
        default=True, alias="OPHELIA_SPONTANEOUS_VOICE"
    )
    ambient_commentary_enabled: bool = Field(
        default=True, alias="OPHELIA_AMBIENT_COMMENTARY"
    )
    ambient_commentary_minutes: int = Field(
        default=45, alias="OPHELIA_AMBIENT_COMMENTARY_MINUTES"
    )
    alarms: str = Field(
        default="",
        alias="OPHELIA_ALARMS",
        description="Comma times HH:MM e.g. 06:30,07:00",
    )
    auto_game_boredom: float = Field(
        default=0.85, alias="OPHELIA_AUTO_GAME_BOREDOM"
    )
    proactive_share_enabled: bool = Field(
        default=True, alias="OPHELIA_PROACTIVE_SHARE"
    )
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
    # Web search backend. duckduckgo needs no key (free, less reliable);
    # tavily / serper / brave need an API key (reliable, AI-friendly).
    web_search_provider: str = Field(
        default="auto",
        alias="OPHELIA_WEB_SEARCH_PROVIDER",
        description="auto | duckduckgo | tavily | serper | brave (auto = first key set, else duckduckgo)",
    )
    tavily_api_key: str | None = Field(default=None, alias="TAVILY_API_KEY")
    serper_api_key: str | None = Field(default=None, alias="SERPER_API_KEY")
    brave_api_key: str | None = Field(default=None, alias="BRAVE_API_KEY")
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
        if self.discord_log_enabled is None:
            self.discord_log_enabled = bool(self.discord_guild_id)
        # Honor OPHELIA_HOME env var for path fields. The module-level
        # OPHELIA_HOME constant is read once at import, so if a user (or a
        # test) sets the env var after import, the field defaults won't
        # reflect it. Re-resolve here so `data_dir`, `memory_db`, and
        # `mcp_config_path` follow the live env var.
        env_home = os.environ.get("OPHELIA_HOME")
        if env_home:
            env_home_path = Path(env_home)
            # Only re-resolve fields that are still at their default (i.e. the
            # user didn't explicitly override them). We can't easily detect
            # "still default" with Pydantic, so instead always re-resolve to
            # env_home/<basename> — this matches the documented behavior that
            # OPHELIA_HOME controls the whole tree.
            self.data_dir = env_home_path / "data"
            self.memory_db = env_home_path / "data" / "memory.db"
            if not self.mcp_config_path or self.mcp_config_path == OPHELIA_HOME / "mcp.json":
                self.mcp_config_path = env_home_path / "mcp.json"
        return self

    def consciousness_on(self) -> bool:
        if self.autonomy_enabled is not None:
            return self.autonomy_enabled
        return self.consciousness_enabled

    def consciousness_interval(self) -> int:
        if self.autonomy_interval_seconds is not None:
            return self.autonomy_interval_seconds
        return self.consciousness_interval_seconds

    def fallback_provider_list(self) -> list[str]:
        """Ordered list of fallback provider names (excluding the primary)."""
        raw = self.fallback_providers.strip()
        if not raw:
            return []
        return [p.strip().lower() for p in raw.split(",") if p.strip()]

    def ollama_autostart_enabled(self) -> bool:
        """Effective ollama autostart decision: explicit override, else auto (Termux)."""
        if self.ollama_autostart is not None:
            return self.ollama_autostart
        from ophelia.platform import is_termux

        return is_termux()

    def kokoro_autostart_enabled(self) -> bool:
        """Effective Kokoro autostart decision: explicit override, else auto (Termux)."""
        if self.kokoro_autostart is not None:
            return self.kokoro_autostart
        from ophelia.platform import is_termux

        return is_termux()

    def web_search_provider_resolved(self) -> str:
        """Effective search backend: explicit choice, else first keyed backend,
        else duckduckgo. 'auto' picks the first available API key."""
        p = (self.web_search_provider or "auto").strip().lower()
        if p in ("tavily", "serper", "brave", "duckduckgo"):
            return p
        # auto: prefer keyed backends for reliability
        if self.tavily_api_key:
            return "tavily"
        if self.serper_api_key:
            return "serper"
        if self.brave_api_key:
            return "brave"
        return "duckduckgo"

    # Providers that can serve explicit/NSFW imagery (xAI/OpenAI are NOT here).
    # Prefer keyed/local quality backends over free Pollinations — otherwise
    # auto NSFW always lands on Pollinations because it needs no key.
    NSFW_CAPABLE_PROVIDERS: ClassVar[tuple[str, ...]] = (
        "civitai",
        "a1111",
        "comfyui",
        "modelslab",
        "fal",
        "replicate",
        "ollama",
        "pollinations",
    )

    def image_backend_configured(self, provider: str) -> bool:
        """True if the given image-only media provider has the creds it needs."""
        p = (provider or "").strip().lower()
        if p == "pollinations":
            return True  # free, no key
        if p == "a1111":
            return bool(self.a1111_base_url)
        if p == "comfyui":
            return bool(self.comfyui_base_url)
        if p == "modelslab":
            return bool(self.modelslab_api_key)
        if p == "civitai":
            return bool(self.civitai_api_key)
        if p == "fal":
            return bool(self.fal_api_key)
        if p == "replicate":
            return bool(self.replicate_api_key)
        if p == "ollama":
            return bool(self.ollama_image_model)
        return False

    def image_nsfw_provider_resolved(self) -> str:
        """Effective NSFW image provider: explicit choice if configured, else
        the first configured uncensored backend, else pollinations (free)."""
        p = (self.image_nsfw_provider or "auto").strip().lower()
        if p != "auto":
            return p
        for prov in self.NSFW_CAPABLE_PROVIDERS:
            if self.image_backend_configured(prov):
                return prov
        return "pollinations"  # zero-config free fallback

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

    def _allowed_telegram_users_ordered(self) -> list[int]:
        """Allowlist as an ordered list (owner first, guests appended after).

        The configured owner is always the first entry because the setup flow
        writes it first and guest approvals append. Preserving order matters
        for the owner-detection fallback — `next(iter(set))` is unordered and
        could pick a guest as the owner.
        """
        raw = self.telegram_allowed_user_ids.strip()
        if not raw:
            return []
        seen: set[int] = set()
        out: list[int] = []
        for x in raw.split(","):
            x = x.strip()
            if not x:
                continue
            try:
                uid = int(x)
            except ValueError:
                continue
            if uid not in seen:
                seen.add(uid)
                out.append(uid)
        return out

    def _allowed_discord_users_ordered(self) -> list[int]:
        raw = self.discord_allowed_user_ids.strip()
        if not raw:
            return []
        seen: set[int] = set()
        out: list[int] = []
        for x in raw.split(","):
            x = x.strip()
            if not x:
                continue
            try:
                uid = int(x)
            except ValueError:
                continue
            if uid not in seen:
                seen.add(uid)
                out.append(uid)
        return out

    def primary_user_channel(self) -> str | None:
        if self.primary_channel and self.primary_channel.strip():
            return self.primary_channel.strip()
        if self.telegram_enabled and self._allowed_telegram_users_ordered():
            return f"telegram:{self._allowed_telegram_users_ordered()[0]}"
        if self.discord_enabled and self._allowed_discord_users_ordered():
            return f"discord:{self._allowed_discord_users_ordered()[0]}"
        return None

    def owner_channels(self) -> set[str]:
        """The set of channel strings that count as the owner (shape her identity).
        From OPHELIA_OWNER_ID (channel-style, comma-separated); falls back to
        the first allowed user on EACH enabled platform when unset, so a user
        who is the owner on both Telegram and Discord is recognized on both
        (not just whichever platform happens to be checked first)."""
        raw = self.owner_id.strip()
        chans: set[str] = set()
        if raw:
            for tok in raw.split(","):
                tok = tok.strip()
                if not tok:
                    continue
                if ":" in tok:
                    chans.add(tok.lower())
                else:
                    # Bare numeric id — bind to whichever platforms are enabled.
                    if self.telegram_enabled is not False:
                        chans.add(f"telegram:{tok}")
                    if self.discord_enabled is not False:
                        chans.add(f"discord:{tok}")
            if chans:
                return chans
        # No explicit OPHELIA_OWNER_ID — the first allowed user on each
        # enabled platform is the configured owner (guests are appended
        # after, so they're never first). This keeps single-platform setups
        # working AND fixes the regression where adding Discord demoted the
        # Telegram owner to a guest.
        if self.telegram_enabled and self._allowed_telegram_users_ordered():
            chans.add(f"telegram:{self._allowed_telegram_users_ordered()[0]}")
        if self.discord_enabled and self._allowed_discord_users_ordered():
            chans.add(f"discord:{self._allowed_discord_users_ordered()[0]}")
        return chans

    def is_owner_channel(self, channel: str) -> bool:
        """True if this inbound channel is the owner (not a sandboxed guest)."""
        return bool(channel) and channel.lower() in self.owner_channels()

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
