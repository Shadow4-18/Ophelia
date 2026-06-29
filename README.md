# The Ophelia Project

**Ophelia** is a willful, Neuro-sama-style autonomous AI — continuous consciousness, drives, inner life, and tools. She runs on **any machine you choose**: PC, laptop, home server, VPS, or (optionally) a phone. A physical phone is **only** her “body” if you want tap/swipe/screen control — not required.

**Local Ollama first** · **Run anywhere** · **Telegram + Discord** · **Optional phone body (ADB/Shizuku)** · **Hermes soul import**

**New here?** → **[Installation guide](docs/INSTALL.md)** · Interactive: `ophelia setup` · Verify: `ophelia check`

Read the pivot rationale: [docs/local-first.md](docs/local-first.md) · Why not Hermes: [docs/why-not-hermes.md](docs/why-not-hermes.md)

## Run anywhere

| Where | Typical use | Phone body? |
|-------|-------------|-------------|
| **PC / laptop** | Dev, `ophelia ui`, local Ollama | Optional — [ADB](docs/remote-adb.md) if you want one |
| **Home server / VPS** | 24/7 `ophelia run`, Telegram/Discord bots | Usually **off** — no phone needed |
| **Termux (Android)** | Pocket always-on host | On-device [Shizuku](docs/tier1-setup.md) if you want screen/tap |

Most setups are **brain-only**: chat, consciousness, memory, and channels work with no Android integration.

**Chat:** Telegram and/or Discord — see [docs/channels.md](docs/channels.md). Run both at once with `ophelia run`.

```bash
ophelia setup               # step-by-step install guide (start here)
ophelia models              # hardware-aware Ollama picks
ophelia doctor --chat-only
ophelia ui                  # PC workstation
ophelia chat "hello"
```

**Install scripts:** `scripts/install.ps1` (Windows) · `scripts/install.sh` (Mac/Linux) · `scripts/termux-install.sh` (phone — **required** on Termux; plain `pip install` fails on Rust wheels)

## Local-first providers

| Provider | Env | Use |
|----------|-----|-----|
| `ollama` | **default** | Local chat, consciousness, vision (`minicpm-v4.6` / `moondream`) |
| `auto` | `OPHELIA_PROVIDER=auto` | Ollama if up, else cloud |
| `xai-oauth` | Hermes import | SuperGrok when you need Grok |
| `xai` | `XAI_API_KEY` (or `GROK_API_KEY`) | xAI API key — Grok without OAuth |
| `deepseek` | `DEEPSEEK_API_KEY` | Very cheap cloud — V4 Flash (~$0.14/1M input) |
| `openai` / `compat` | API keys | OpenAI, OpenRouter, LM Studio |

**Per-role routing** — Ophelia uses six different models for six different jobs:

| Role | What it does | Example env var |
|------|--------------|-----------------|
| `chat` | Main replies | `XAI_MODEL`, `DEEPSEEK_MODEL`, `OPENAI_MODEL`, `OLLAMA_MODEL` |
| `consciousness` | Background ticks / inner life | `XAI_CONSCIOUSNESS_MODEL`, `DEEPSEEK_CONSCIOUSNESS_MODEL`, `OPENAI_CONSCIOUSNESS_MODEL`, `OLLAMA_CONSCIOUSNESS_MODEL` |
| `curator` | Memory consolidation | `XAI_CURATOR_MODEL`, `DEEPSEEK_CURATOR_MODEL`, `OPENAI_CURATOR_MODEL`, `OLLAMA_CURATOR_MODEL` |
| `vision` | Photo understanding | `XAI_VISION_MODEL`, `OPENAI_VISION_MODEL`, `OLLAMA_VISION_MODEL` |
| `image` | Image generation | `XAI_IMAGE_MODEL`, `OPENAI_IMAGE_MODEL`, `OLLAMA_IMAGE_MODEL` |
| `video` | Video generation | `XAI_VIDEO_MODEL` (xAI only) |

**Capability auto-routing** — DeepSeek has no vision, image, or video
capability. When DeepSeek is the primary provider, those roles automatically
route to a capable provider (Ollama for free local vision, or xAI/OpenAI). So
you can run cheap DeepSeek for chat/consciousness/curator while Ollama or Grok
handles vision and media — no extra cost for vision if you run a phone-friendly
Ollama vision model (`openbmb/minicpm-v4.6` or `moondream`), or it reuses your
existing Grok key. Override any role explicitly with `OPHELIA_PROVIDER_VISION`,
`OPHELIA_PROVIDER_IMAGE`, etc.

**Fallback** — if a provider fails with a transient error (rate limit, 5xx,
network), Ophelia retries on a fallback chain before giving up. Great for
cost: run Grok as primary with DeepSeek V4 Flash as a cheap backup.

```bash
OPHELIA_FALLBACK_PROVIDERS=deepseek,xai-oauth
OPHELIA_FALLBACK_MODEL=deepseek-v4-flash   # optional: same model on every fallback
```

Only transient errors trigger fallback — a `400 Bad Request` (wrong model or
params) is surfaced immediately instead of wasting retries.

## Web search

Grok has built-in live search, but **DeepSeek, OpenAI, and Ollama do not** —
so on those providers Ophelia uses a `web_search` tool. Pick a backend:

| Backend | Env | Notes |
|---------|-----|-------|
| `auto` | *(default)* | First API key set, else DuckDuckGo |
| `duckduckgo` | none | Free, no key, but often returns nothing (gets blocked) |
| `tavily` | `TAVILY_API_KEY` | AI-focused, reliable, free tier |
| `serper` | `SERPER_API_KEY` | Google results, reliable |
| `brave` | `BRAVE_API_KEY` | Brave Search API, reliable |

```bash
OPHELIA_WEB_SEARCH=true
OPHELIA_WEB_SEARCH_PROVIDER=auto
TAVILY_API_KEY=tvly-...        # or SERPER_API_KEY / BRAVE_API_KEY
```

Configure it interactively with `ophelia setup` → **Web search**. If a keyed
backend fails or returns nothing, Ophelia falls back to DuckDuckGo so a search
never hard-fails the turn.



- **Per-role provider**: `OPHELIA_PROVIDER_CHAT`, `_CONSCIOUSNESS`, `_VISION`, `_CURATOR`, `_IMAGE`, `_VIDEO` — point each role at a different provider (e.g. chat on Ollama, image on xAI).
- **Per-role model**: the `*_MODEL` env vars above. Optional roles (consciousness, curator, vision) inherit the chat model when unset — handy for running a cheap `grok-3-mini` for background ticks while keeping `grok-4` for real replies.
- **Interactive**: `ophelia setup` → AI provider → "Configure specific models for each role?" walks you through every role with presets + manual entry.

**One model at a time per provider** — cloud providers use per-role locks so sub-minds run in parallel; local Ollama queues through a model gate so it never loads two models simultaneously.

**Future:** [Neuro-style ensemble](docs/neuro-ensemble.md) — multiple specialized minds (director, filter, reaction, voice, avatar) coordinated into one character on stream. Today's per-role routing is ensemble v0.

## Optional phone body

Only if you want her to **see and touch a physical phone** (games, screen vision, tap/swipe):

```env
OPHELIA_ANDROID_ENABLED=true
# From PC/server → phone over ADB:
OPHELIA_ADB_DEVICE=192.168.1.50:5555
# Or run on Termux with Shizuku (see tier1-setup.md)
```

Leave `OPHELIA_ANDROID_ENABLED=false` (default on PC/server) for a **software-only** Ophelia — fully valid.

Full ADB guide: [docs/remote-adb.md](docs/remote-adb.md)

## Neuro-like inner life

Continuous consciousness (not Hermes cron isolation):

- **Mood**, **feelings**, **urges**, **internal thought**
- **Drives** build while idle → initiative to message / explore
- Outbound messages land in **your Telegram thread** (same SQLite memory)

## New tools

| Tool | Notes |
|------|-------|
| `web_search` / `fetch_url` | Pluggable backends — DuckDuckGo (free), Tavily, Serper, Brave (see Web search section) |
| `generate_image` / `generate_video` | xAI / OpenAI / Ollama — saved to artifacts, auto-sent to chat |
| `text_to_speech` | xAI TTS — saved mp3, auto-sent to chat |
| `send_file` | Explicitly send any saved file (audio/video/image/doc) to the chat mid-turn |
| `save_skill` | Learn procedures → `~/.ophelia/skills/` |
| MCP bridge | `~/.ophelia/mcp.json` + `pip install mcp` |
| Phone body | Shizuku (on-phone) or ADB (from PC) |

**Sending media** — generated images, videos, and audio are detected in the
reply ("Image saved to …", "TTS saved to …") and automatically uploaded to
Telegram/Discord. The `send_file` tool lets Ophelia explicitly deliver any
saved file (audio, video, screenshots, documents) on demand — so she won't
claim she can't send audio. On Telegram each type is sent as the right media
kind (photo/video/audio/document); on Discord everything is sent as a file
attachment.

## Migrate from Hermes

```bash
ophelia migrate hermes
ophelia auth login              # fresh SuperGrok OAuth (browser)
ophelia auth import-hermes      # re-sync if Hermes already logged in
ophelia transfer cloud-upload   # phone → PC bundle
```

See [docs/transfer.md](docs/transfer.md), [docs/migrate-old-phone.md](docs/migrate-old-phone.md).

## Commands

| Command | Purpose |
|---------|---------|
| `ophelia` | **Interactive menu** — start/stop, configure, diagnose, live dashboard |
| `ophelia setup` | Step-by-step install guide (idiot-proof checklist) |
| `ophelia start` | One command: wake-lock + tmux + run (Termux) |
| `ophelia dashboard` | Live status panel (mood, drives, pressure, channels) |
| `ophelia ui` | PC workstation (browser) |
| `ophelia run` | Telegram + Discord + consciousness |
| `ophelia chat` | One-shot message |
| `ophelia models` | Local model cookbook |
| `ophelia providers` | Show AI routing |
| `ophelia check` / `ophelia doctor` | **Self-check** — version, deps, providers, services |
| `ophelia transfer *` | Phone ↔ PC data move |

**Docs:** **[INSTALL](docs/INSTALL.md)** · [channels](docs/channels.md) · [setup wizard](docs/setup.md) · [local-first](docs/local-first.md) · [PC setup](docs/pc-setup.md) · [UI](docs/pc-ui.md) · [Neuro ensemble](docs/neuro-ensemble.md) · [games](docs/games.md) · [tier 1/2](docs/tier1-setup.md)

## Termux (optional pocket host)

Run on a phone **as the host** — not the same as using a phone as a body from a VPS.

Just type `ophelia` with no args to get the full interactive menu (start/stop, configure, diagnose, migrate, auth, live dashboard):

```bash
ophelia            # interactive launcher menu (start here)
ophelia start      # one command: wake-lock + tmux + run
ophelia dashboard  # live mood/drives/pressure panel
```

Or the classic manual way:

```bash
termux-wake-lock
tmux new -s ophelia
ophelia run
```

**Ollama auto-starts** — on Termux you no longer need to run `ollama serve`
separately. When `ophelia run` needs Ollama (e.g. for local vision with
`minicpm-v4.6`) and it isn't already running, Ophelia spawns it for you and
waits for it to come up. Disable with `OPHELIA_OLLAMA_AUTOSTART=false`.

**Survive a phone reboot** — Ophelia can't start itself after a reboot, so use
the Termux:Boot add-on. Install it, then drop a script in `~/.termux/boot/`:

```bash
mkdir -p ~/.termux/boot
cat > ~/.termux/boot/start-ophelia <<'EOF'
#!/data/data/com.termux/files/usr/bin/sh
termux-wake-lock
ollama serve >/dev/null 2>&1 &
sleep 2 && tmux new -d -s ophelia 'ophelia run'
EOF
chmod +x ~/.termux/boot/start-ophelia
```

`scripts/install.ps1` · `scripts/install.sh` · `scripts/termux-install.sh` · `scripts/termux-shizuku-setup.sh`

## License

MIT
