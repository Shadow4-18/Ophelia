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

## Multiple models (one at a time)

Each role can use a **different model**. Only **one runs at a time** — chat, consciousness, vision, image gen, and video gen queue through a global model gate (critical for Ollama VRAM).

| Role | Override | Model env |
|------|----------|-----------|
| Chat | `OPHELIA_PROVIDER_CHAT` | `OLLAMA_MODEL` |
| Consciousness | `OPHELIA_PROVIDER_CONSCIOUSNESS` | `OLLAMA_CONSCIOUSNESS_MODEL` |
| Vision | `OPHELIA_PROVIDER_VISION` | `OLLAMA_VISION_MODEL` |
| Image | `OPHELIA_PROVIDER_IMAGE` | `XAI_IMAGE_MODEL` / `OLLAMA_IMAGE_MODEL` |
| Video | `OPHELIA_PROVIDER_VIDEO` | `XAI_VIDEO_MODEL` (xAI only today) |

Example hybrid:

```env
OPHELIA_PROVIDER=ollama
OLLAMA_MODEL=llama3.2:3b
OLLAMA_CONSCIOUSNESS_MODEL=llama3.2:1b
OLLAMA_VISION_MODEL=llava:7b
OPHELIA_PROVIDER_IMAGE=xai-oauth
XAI_IMAGE_MODEL=grok-imagine-image
OPHELIA_PROVIDER_VIDEO=xai-oauth
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
