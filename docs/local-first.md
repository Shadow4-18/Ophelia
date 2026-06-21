# Local-first setup (The Ophelia Project)

Ophelia is built for a **Neuro-sama-style streaming future**: continuous consciousness, drives, and personality — running on **whatever machine you pick** (PC, server, VPS, or phone). The **brain should run locally** so you are not burning cloud quota during dev or 24/7 ticks.

A physical phone is an **optional body** (screen/tap), not part of the default install.

PC and phone are **both supported hosts** — neither is required. Most people run Ophelia on a **PC or server** with no phone body at all.

## Why local-first

- SuperGrok / xAI OAuth has **usage limits** — fine for chat bursts, expensive for always-on consciousness.
- Local models let you **iterate on persona, drives, and tools** without per-token cost.
- When you go live streaming, you will already have **weights + training data** on your side.

## Quick start (PC)

```bash
# Install Ollama: https://ollama.com
ollama pull llama3.2:3b
ollama pull llava:7b          # optional: phone screen vision

pip install -e .
ophelia models                # RAM/GPU-aware recommendations
ophelia doctor --chat-only
ophelia ui                    # workstation
```

## Recommended `~/.ophelia/.env`

```env
OPHELIA_PROVIDER=ollama
OLLAMA_BASE_URL=http://127.0.0.1:11434/v1
OLLAMA_MODEL=llama3.2:3b

# Hybrid: fast local ticks, cloud chat when you want Grok
# OPHELIA_PROVIDER_CHAT=xai-oauth
# OPHELIA_PROVIDER_CONSCIOUSNESS=ollama
# OPHELIA_PROVIDER_VISION=ollama
# OLLAMA_VISION_MODEL=llava:7b

OPHELIA_CONSCIOUSNESS=true
OPHELIA_AUTO_LOCAL_CONSCIOUSNESS=true
```

## Provider routing

| Role | Env override | Typical local pick |
|------|----------------|-------------------|
| Chat | `OPHELIA_PROVIDER_CHAT` | `ollama` or `xai-oauth` |
| Consciousness | `OPHELIA_PROVIDER_CONSCIOUSNESS` | `ollama` (small model) |
| Vision | `OPHELIA_PROVIDER_VISION` | `ollama` + `llava` |
| Curator | `OPHELIA_PROVIDER_CURATOR` | `ollama` |

`OPHELIA_PROVIDER=auto` now prefers **Ollama when reachable**, then cloud fallbacks.

## Tools that work offline

- `web_search` / `fetch_url` — DuckDuckGo, no API key
- `save_skill` — writes to `~/.ophelia/skills/`
- `run_code` — local Python sandbox
- Phone body (optional) — see [remote-adb.md](remote-adb.md) when running on PC/server

## Multiple models (per role)

Each role can use a **different provider AND a different model**. Cloud providers (xAI, OpenAI, compat) run roles in parallel via per-role locks; local Ollama queues through a global model gate so it never loads two models at once (critical for VRAM).

| Role | Provider override | Model env (per provider) |
|------|-------------------|--------------------------|
| Chat | `OPHELIA_PROVIDER_CHAT` | `XAI_MODEL` · `OPENAI_MODEL` · `OLLAMA_MODEL` · `OPHELIA_COMPAT_MODEL` |
| Consciousness | `OPHELIA_PROVIDER_CONSCIOUSNESS` | `XAI_CONSCIOUSNESS_MODEL` · `OPENAI_CONSCIOUSNESS_MODEL` · `OLLAMA_CONSCIOUSNESS_MODEL` · `OPHELIA_COMPAT_CONSCIOUSNESS_MODEL` |
| Curator | `OPHELIA_PROVIDER_CURATOR` | `XAI_CURATOR_MODEL` · `OPENAI_CURATOR_MODEL` · `OLLAMA_CURATOR_MODEL` · `OPHELIA_COMPAT_CURATOR_MODEL` |
| Vision | `OPHELIA_PROVIDER_VISION` | `XAI_VISION_MODEL` · `OPENAI_VISION_MODEL` · `OLLAMA_VISION_MODEL` · `OPHELIA_COMPAT_VISION_MODEL` |
| Image | `OPHELIA_PROVIDER_IMAGE` | `XAI_IMAGE_MODEL` · `OPENAI_IMAGE_MODEL` · `OLLAMA_IMAGE_MODEL` |
| Video | `OPHELIA_PROVIDER_VIDEO` | `XAI_VIDEO_MODEL` (xAI only today) |

Optional roles (consciousness, curator, vision) **inherit the chat model when unset** — handy for running a cheap `grok-3-mini` for background ticks while keeping `grok-4` for real replies.

Configure interactively: `ophelia setup` → AI provider → "Configure specific models for each role?" — presets + manual entry per role, with live current values.

Example hybrid:

```env
OPHELIA_PROVIDER=ollama
OLLAMA_MODEL=llama3.2:3b
OLLAMA_CONSCIOUSNESS_MODEL=llama3.2:1b
OLLAMA_VISION_MODEL=llava:7b
OPHELIA_PROVIDER_IMAGE=xai-oauth
XAI_IMAGE_MODEL=grok-imagine-image
OPHELIA_PROVIDER_VIDEO=xai-oauth
XAI_VIDEO_MODEL=grok-imagine-video
```

Example all-xAI with a cheap background mind:

```env
OPHELIA_PROVIDER=xai-oauth
XAI_MODEL=grok-4
XAI_CONSCIOUSNESS_MODEL=grok-3-mini
XAI_CURATOR_MODEL=grok-3-mini
XAI_VISION_MODEL=grok-4
XAI_IMAGE_MODEL=grok-imagine-image
XAI_VIDEO_MODEL=grok-imagine-video
```

Cloud-only (need xAI unless noted): `generate_image` (also Ollama flux), `generate_video`, `text_to_speech`.

## MCP (optional)

Copy `mcp.example.json` → `~/.ophelia/mcp.json`, then:

```bash
pip install mcp
```

Tools appear as `mcp_<server>_<tool>` in chat.

## Training path (future)

1. Run local daily — consciousness + inner log build **behavior traces**.
2. Curator + `save_skill` build **structured memory**.
3. Export conversations from SQLite / Hermes archive for **fine-tune or LoRA** when you pick a base model.

See also: [pc-setup.md](pc-setup.md), [pc-ui.md](pc-ui.md), [remote-adb.md](remote-adb.md).
