# The Ophelia Project

**Ophelia** is a willful, Neuro-sama-style autonomous AI — continuous consciousness, drives, inner life, and an optional **phone body**. Built to evolve from Hermes toward **local models + streaming**, not another reactive chatbot.

**Local Ollama first** · **PC + phone parity** · **Telegram + Discord** · **ADB remote body** · **Hermes soul import**

Read the pivot rationale: [docs/local-first.md](docs/local-first.md) · Why not Hermes: [docs/why-not-hermes.md](docs/why-not-hermes.md)

## PC and phone — equal footing

| Host | Brain | Body |
|------|-------|------|
| **PC** | Ollama / cloud via `ophelia ui` or `ophelia run` | Phone over **ADB** ([remote-adb.md](docs/remote-adb.md)) |
| **Phone (Termux)** | Ollama or xAI OAuth | **Shizuku** on-device |

Cloud (SuperGrok OAuth) is optional fallback — not the default anymore.

**Chat:** Telegram and/or Discord — see [docs/channels.md](docs/channels.md). Run both at once with `ophelia run`.

```bash
ophelia setup               # step-by-step install guide (start here)
ophelia models              # hardware-aware Ollama picks
ophelia doctor --chat-only
ophelia ui                  # PC workstation
ophelia chat "hello"
```

**Install scripts:** `scripts/install.ps1` (Windows) · `scripts/install.sh` (Mac/Linux) · `scripts/termux-install.sh` (phone)

## Local-first providers

| Provider | Env | Use |
|----------|-----|-----|
| `ollama` | **default** | Local chat, consciousness, vision (`llava`) |
| `auto` | `OPHELIA_PROVIDER=auto` | Ollama if up, else cloud |
| `xai-oauth` | Hermes import | SuperGrok when you need Grok |
| `openai` / `compat` | API keys | OpenAI, OpenRouter, LM Studio |

Per-role: `OPHELIA_PROVIDER_CHAT`, `_CONSCIOUSNESS`, `_VISION`, `_CURATOR`, `_IMAGE`, `_VIDEO`.

**One model at a time** — all inference (chat, consciousness, vision, image, video) queues through a model gate so Ollama never loads two models simultaneously.

**Future:** [Neuro-style ensemble](docs/neuro-ensemble.md) — multiple specialized minds (director, filter, reaction, voice, avatar) coordinated into one character on stream. Today's per-role routing is ensemble v0.

## PC controls the phone (ADB)

From your PC, Ophelia can tap, swipe, screenshot, and shell — **with or without root**:

```env
OPHELIA_ANDROID_ENABLED=true
OPHELIA_ADB_DEVICE=192.168.1.50:5555
OPHELIA_ADB_ROOT=false
```

Full setup: [docs/remote-adb.md](docs/remote-adb.md)

## Neuro-like inner life

Continuous consciousness (not Hermes cron isolation):

- **Mood**, **feelings**, **urges**, **internal thought**
- **Drives** build while idle → initiative to message / explore
- Outbound messages land in **your Telegram thread** (same SQLite memory)

## New tools

| Tool | Notes |
|------|-------|
| `web_search` / `fetch_url` | DuckDuckGo, no API key |
| `save_skill` | Learn procedures → `~/.ophelia/skills/` |
| MCP bridge | `~/.ophelia/mcp.json` + `pip install mcp` |
| Phone body | Shizuku (on-phone) or ADB (from PC) |

## Migrate from Hermes

```bash
ophelia migrate hermes
ophelia auth import-hermes      # optional SuperGrok OAuth
ophelia transfer cloud-upload   # phone → PC bundle
```

See [docs/transfer.md](docs/transfer.md), [docs/migrate-old-phone.md](docs/migrate-old-phone.md).

## Commands

| Command | Purpose |
|---------|---------|
| `ophelia setup` | **Step-by-step install guide** (idiot-proof checklist) |
| `ophelia ui` | PC workstation (browser) |
| `ophelia run` | Telegram + Discord + consciousness |
| `ophelia chat` | One-shot message |
| `ophelia models` | Local model cookbook |
| `ophelia providers` | Show AI routing |
| `ophelia check` / `ophelia doctor` | **Self-check** — version, deps, providers, services |
| `ophelia transfer *` | Phone ↔ PC data move |

**Docs:** [channels](docs/channels.md) · [setup wizard](docs/setup.md) · [local-first](docs/local-first.md) · [PC setup](docs/pc-setup.md) · [UI](docs/pc-ui.md) · [Neuro ensemble (future)](docs/neuro-ensemble.md) · [games](docs/games.md) · [tier 1/2](docs/tier1-setup.md)

## Termux (S21)

```bash
termux-wake-lock
tmux new -s ophelia
ophelia run
```

`scripts/install.ps1` · `scripts/install.sh` · `scripts/termux-install.sh` · `scripts/termux-shizuku-setup.sh`

## License

MIT
