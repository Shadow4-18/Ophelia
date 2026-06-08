# PC setup (Windows / macOS / Linux / server / VPS)

See the full **[installation guide](INSTALL.md)** first. This page is host-specific detail after install.

Ophelia runs on any of these for **development**, **24/7 hosting**, **Telegram/Discord bots**, and **chat without any phone**. No Android device required.

## Install

```powershell
cd e:\Projects\Ophelia
.\scripts\install.ps1
# or: pip install -e . ; ophelia setup --do ; ophelia setup
```

Copy config:

```powershell
mkdir $env:USERPROFILE\.ophelia
copy config.example.env $env:USERPROFILE\.ophelia\.env
```

Add `SOUL.md` to `~/.ophelia/` (or `ophelia migrate hermes`).

## Pick a provider

| Method | Env | Notes |
|--------|-----|-------|
| **Auto** | `OPHELIA_PROVIDER=auto` | Picks OAuth → API → Ollama → OpenAI |
| **SuperGrok OAuth** | `xai-oauth` | `ophelia auth import-grok` after `grok login` |
| **xAI API** | `xai` + `XAI_API_KEY` | Paid API |
| **Ollama** | `ollama` | Local, free; run `ollama serve` |
| **OpenAI** | `openai` + `OPENAI_API_KEY` | GPT-4o etc. |
| **Compatible** | `compat` + `OPHELIA_COMPAT_*` | LM Studio, OpenRouter, vLLM |

### Hybrid (recommended on PC)

Use cheap/local for inner ticks, cloud for chat:

```env
OPHELIA_PROVIDER=auto
OPHELIA_PROVIDER_CHAT=xai-oauth
OPHELIA_PROVIDER_CONSCIOUSNESS=ollama
OPHELIA_AUTO_LOCAL_CONSCIOUSNESS=true
OLLAMA_MODEL=llama3.2
```

## Verify

```powershell
ophelia providers
ophelia doctor --chat-only
ophelia chat "hello, who are you?"
ophelia ui
```

`ophelia ui` opens the workstation in your browser — see [pc-ui.md](pc-ui.md).

`doctor --chat-only` does not require Telegram.

## Full stack on PC

You can run `ophelia run` on PC/server/VPS with Telegram or Discord — no phone body required:

```env
TELEGRAM_BOT_TOKEN=...
TELEGRAM_ALLOWED_USER_IDS=123456789
OPHELIA_ANDROID_ENABLED=false
```

Consciousness, inner log, curator, and games config all work; phone tools return "Phone body disabled" when Android integration is off.

## What is auto-disabled on PC

| Feature | PC default |
|---------|------------|
| Android / Shizuku | off |
| Games | off |
| Listen loop (mic) | off |
| Vision | off (no body) |

Override with `OPHELIA_ANDROID_ENABLED=true` if using ADB to a device from PC.

## OAuth on PC

```powershell
grok login
ophelia auth import-grok
ophelia auth refresh
```

Or import Hermes bundle from old phone: `ophelia auth import-hermes`.
