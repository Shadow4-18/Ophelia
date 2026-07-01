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
| `image` | Image generation | `XAI_IMAGE_MODEL`, `OPENAI_IMAGE_MODEL`, `OLLAMA_IMAGE_MODEL`, `POLLINATIONS_IMAGE_MODEL`, `A1111_*`, `COMFYUI_*`, `FAL_*`, `REPLICATE_*`, `CIVITAI_*`, `MODELSLAB_*` (see [Image generation](#image-generation)) |
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

## Image generation

Ophelia can generate images through several backends, picked per-role via
`OPHELIA_PROVIDER_IMAGE` (or `ophelia setup` → **Image generation**):

| Backend | Env | Notes |
|---------|-----|-------|
| `pollinations` | none | **Free, no API key**, lax on NSFW — zero-config image gen |
| `a1111` | `A1111_BASE_URL` | Automatic1111/SDWebUI local (`--api`), uncensored, LoRAs — best local quality |
| `comfyui` | `COMFYUI_BASE_URL` | ComfyUI local, uncensored, most control (custom workflow JSON) |
| `ollama` | `OLLAMA_IMAGE_MODEL` | Local (`ollama pull flux`), uncensored |
| `xai-oauth` / `xai` | `XAI_API_KEY` | Grok Imagine — **censored** |
| `openai` | `OPENAI_API_KEY` | DALL-E — **censored** |
| `fal` | `FAL_API_KEY` | Fast cloud, NSFW-tolerant flux/sdxl |
| `replicate` | `REPLICATE_API_KEY` | Cloud, many NSFW-allowed community models |
| `civitai` | `CIVITAI_API_KEY` | NSFW checkpoints/LoRAs, generation API |
| `modelslab` | `MODELSLAB_API_KEY` | Hosted SD, explicit/adult models |
| `auto` | *(default)* | Ollama if a model is pulled → cloud → Pollinations free |

All generated images are downloaded and saved under `~/.ophelia/data/artifacts`
(even cloud URLs, which expire) and auto-sent to Telegram/Discord.

### NSFW content tier

`OPHELIA_IMAGE_NSFW_ALLOWED=true` enables the NSFW tier. When on:

- The `generate_image` tool exposes an `nsfw` flag the model **only** sets when
  you explicitly ask for explicit content (it's `false` for everything else).
- Explicit requests are **auto-routed to an uncensored backend** — never xAI or
  OpenAI, which would refuse them and risk flagging your account.
- When off, explicit image requests are refused with a clear message.

`OPHELIA_IMAGE_NSFW_PROVIDER=auto` picks the first configured uncensored
backend (pollinations → a1111 → comfyui → modelslab → civitai → fal →
replicate → ollama). Pollinations is the zero-config default.

```bash
OPHELIA_PROVIDER_IMAGE=pollinations     # free, no key, NSFW-capable
OPHELIA_IMAGE_NSFW_ALLOWED=true
OPHELIA_IMAGE_NSFW_PROVIDER=auto        # route explicit prompts here
```

> **Local NSFW (recommended for privacy):** run Automatic1111 with `--api
> --listen` and set `OPHELIA_PROVIDER_IMAGE=a1111`. Nothing leaves your machine,
> you get LoRAs/samplers, and quality is far above the cloud free tier.



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

### Touch accuracy & calibration

`input tap X Y` expects **native display pixels** (e.g. 1440×3200 on an S21U).
Taps miss when coordinates come back in the wrong space — usually because the
vision model sees an internally-resized image and guesses x,y in *that* space.
Ophelia handles this three ways:

- **Coordinate grid overlay** (`OPHELIA_VISION_GRID=true`, default): screenshots
  sent to vision are annotated with a yellow grid + native-pixel labels, so she
  reads exact coordinates off the image instead of guessing.
- **Native-size prompt injection**: the vision prompt is told the native
  resolution and to prefer the accessibility tree (`phone_ui_dump`) bounds
  center for taps — those are already native pixels and pixel-exact.
- **Auto-scale + guard**: `phone_tap`/`phone_swipe` accept normalized fractions
  (0.0–1.0) and scale them to native; coordinates outside the native bounds are
  clamped and flagged so she re-reads the screen instead of silently mis-tapping.

Calibrate with one command:

```bash
ophelia phone calibrate
```

It reports `wm size` vs the screenshot's real pixel size (and the scale factor
between them), saves a grid-annotated screenshot to
`~/.ophelia/data/screenshots/calibrate_grid.png`, and taps the four corners +
center so — with **Developer Options → Pointer location** enabled on the phone —
you can see exactly where each tap lands. If the crosshair matches the label,
calibration is correct.

## Neuro-like inner life

Continuous consciousness (not Hermes cron isolation):

- **Mood**, **feelings**, **urges**, **internal thought**
- **Drives** build while idle → initiative to message / explore
- Outbound messages land in **your Telegram thread** (same SQLite memory)

### Adaptive ticks (attention system)

The inner loop is state-aware, not a fixed pulse:

- **Backs off when busy** — ticks are skipped while she's mid-turn (`agent_thinking`) or a model call is in flight, so she's never interrupted mid-sentence or mid-creation.
- **Action cooldown** (`OPHELIA_TICK_ACTION_COOLDOWN`, default 300s) — after she acts or reaches out, the next tick backs off for N seconds instead of knocking immediately.
- **Contextual nudges** — when a goal is due, the tick tells her *what she was last doing* and *how overdue* the goal is, not just "tick."
- **Rotating idle nudge** (`OPHELIA_TICK_IDLE_NUDGE_ROTATE`, default on) — when nothing's due and she's been idle a while, the suggested mode rotates (reflect → create → explore → social) so ticks aren't identical every time.
- **Silence is the baseline** — the prompt frames a no-action tick as correct, not something she must justify; drives/initiative pressure still surface real urges.

### Telegram chat commands (remote control)

Ophelia registers its own `/`-command menu with BotFather on startup, so any
stale commands left over from a previous bot on the same token (e.g. Hermes)
are replaced. Available from your phone while away from the terminal:

| Command | What it does |
|---------|--------------|
| `/status` | Autonomy/thinking/listen/voice state, pending resume, chat provider+model, Ollama reachability |
| `/pause` · `/resume` | Pause / resume autonomous outreach |
| `/continue` | Resume an unfinished tool chain that hit the round cap |
| `/voice on\|off` | Toggle voice replies |
| `/listen on\|off` | Toggle local mic listening (Termux:API) |
| `/inner on\|off\|tail` | Inner-monologue mirror |
| `/game list\|play <id>\|stop\|look` | Android game sessions |
| `/models` | Per-role provider/model routing |
| `/help` | List commands |

**Continue button.** When a turn runs out of tool rounds mid-task, the reply
includes a `▶ Continue` inline button — one tap resumes exactly where she left
off. `/continue` does the same thing as text.

**Tool-round cap.** `OPHELIA_MAX_TOOL_ROUNDS` (default `25`) bounds how many
tool calls one turn can make. Raise it (e.g. `40`) if she's building long
from-scratch artifacts like a math-based synth engine.

**"terminated by other getUpdates request" / Telegram spam.** That error means
two processes are polling the same bot token at once (Telegram allows only one
`getUpdates` poller per token). The usual cause is a **second `ophelia run`** —
most often a detached tmux session left by the Termux:Boot auto-start script
that you can't see, next to the `ophelia run` you started manually in a fresh
terminal. Ophelia now guards against this at three levels:

- **Single-instance lock** (`~/.ophelia/ophelia.run.lock`): `ophelia run` takes
  an exclusive flock *before any background loop starts*. A second `ophelia run`
  prints "Ophelia is already running" and exits instead of double-running
  consciousness/dream/curator/mic and fighting over the token. Covers all launch
  paths (`ophelia run`, `ophelia start`, Termux:Boot) since they all run `ophelia run`.
- **Poller lock + conflict filter** in the Telegram gateway as a backstop, and
  `phone_shell` refuses commands that would spawn another Ophelia or kill her
  own runtime (a common cause — she'd run a "system check" via `phone_shell`
  and accidentally start a second instance).
- **One-shot diagnostic**: on conflict, runs `ps -ef` and logs the culprit
  processes with PIDs (`telegram.polling_conflict_processes`).

**Pick one launch method.** You can't have Termux:Boot auto-start *and* manual
`ophelia run` without one blocking the other. Either:

- Let Termux:Boot run her (survives reboots) and use `tmux attach -t ophelia` to
  view / `ophelia stop` to stop — never run `ophelia run` manually; **or**
- Disable the boot script (`rm ~/.termux/boot/start-ophelia`) and always start
  manually with `ophelia run`.

Recover from a current duplicate:

```bash
tmux kill-server          # kills any tmux-held ophelia from Termux:Boot
pkill -f ophelia          # kills any foreground/old instance
ps -ef | grep -E 'ophelia|hermes|tmux'   # confirm only nothing is running
ophelia run               # then start exactly one
```

Note: a leftover from *before* the lock was added doesn't hold the new lock, so
the first time you upgrade you must kill it manually as above; after that the
lock prevents recurrence.

## Owner vs guests (multi-user sandboxing)

By default Ophelia treats every allowed user equally — anyone in your
`TELEGRAM_ALLOWED_USER_IDS` / `DISCORD_ALLOWED_USER_IDS` can talk to her and
those conversations shape her. If you want to **let other people message her
without affecting her memory, personality, soul, or evolution**, mark yourself
as the owner:

```env
OPHELIA_OWNER_ID=telegram:123456789          # channel-style; comma-separate for multiple
```

Then add other people's IDs to the allowlist as usual. Anyone in the allowlist
who **isn't** the owner becomes a **sandboxed guest**:

- **She's still herself** — guests get her full SOUL personality in replies.
- **Her identity is untouched** — guest turns are stored in a separate
  quarantined table that her curator, dream, and reflection loops never read,
  so guest content never becomes memory, lessons, mood, or goals.
- **Private life stays private** — guests don't see her inner thoughts,
  long-term `MEMORY`, the `USER` profile, or her mood/psyche.
- **Identity-shaping & costly tools are disabled** for guests: `edit_soul`,
  `edit_prompter`, `save_lesson`, `reflect`, goals, drives, skills, `sqlite_*`,
  `run_code`, `recall_memory`, media generation, phone control, and MCP tools.
  Guests get conversation only.
- **Drives aren't bumped** by guest messages — only yours move her social will.

If `OPHELIA_OWNER_ID` is unset, the first allowed user is treated as the owner
(backward compatible with single-user setups).

### Approving guests without knowing their IDs

You don't have to collect your friends' Telegram/Discord IDs ahead of time.
Set the admission mode to `approve` (the default):

```env
OPHELIA_GUEST_ADMISSION=approve
```

When a stranger messages her, she holds their message and asks **you** to
approve them:

- **Telegram** — you get a message with their name, ID, and first message, plus
  **✅ Accept / ❌ Decline** buttons. Tap Accept and she automatically appends
  their ID to `TELEGRAM_ALLOWED_USER_IDS` in `~/.ophelia/.env` (no restart
  needed) and replies to them — they're in as a sandboxed guest.
- **Discord** — you get a DM with their details; reply `!approve <id>` or
  `!deny <id>`.

While they wait they get a gentle "I've asked my owner to OK our chat" note, and
they won't re-prompt you on every message. Other modes: `open` (anyone chats as
a guest immediately, no prompt) and `reject` (refuse strangers outright).

## Chat log (oversight)

Every message sent to her and every reply she sends back is logged — text and
media (photos sent to her, images/audio/video she sends back) — to
`~/.ophelia/data/logs/` (a SQLite index + an organized `media/` folder with
stable filenames). Logging covers **both owner and guests**, so you can see
everything anyone says to her and everything she says back.

```bash
ophelia logs                                  # recent entries (oldest→newest)
ophelia logs --channel telegram:12345          # one user's conversation
ophelia logs --media                          # only entries with attached media
ophelia logs --direction in --since 2026-07-01 # inbound since a date
ophelia logs --limit 200
```

Each line shows timestamp, direction (`<-` to her / `->` from her), channel,
owner/guest, the message text, and the path to any attached media. Disable with
`OPHELIA_CHAT_LOG=false`.

## New tools

| Tool | Notes |
|------|-------|
| `web_search` / `fetch_url` | Pluggable backends — DuckDuckGo (free), Tavily, Serper, Brave (see Web search section) |
| `generate_image` / `generate_video` | xAI / OpenAI / Ollama / Pollinations / A1111 / ComfyUI / fal / Replicate / Civitai / ModelsLab — saved to artifacts, auto-sent to chat (image supports an `nsfw` flag) |
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

**If she keeps calling herself Hermes** after migrating: `ophelia migrate hermes`
copies the old `~/.hermes/SOUL.md` and memories into `~/.ophelia/`, so they still
say "You are Hermes" and she reads them as her persona every turn. Ophelia now
has an identity guard in her base prompt (she's told Hermes is a prior
incarnation, not her identity) and the memory-search tool is renamed
`recall_past_sessions` (no more "Hermes" in tool names/results). To finish the
cleanup, rewrite the soul and drop Hermes-identity memory lines:

```bash
cat ~/.ophelia/SOUL.md                      # see what it currently says
grep -rli hermes ~/.ophelia/memories/       # find tainted memory files
# either edit ~/.ophelia/SOUL.md by hand, or just tell her in chat:
#   "Rewrite your SOUL.md — your name is Ophelia, not Hermes."
# (she has the edit_soul tool and will back up the old version first)
```

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
| `ophelia phone calibrate` | Diagnose + calibrate touch input (grid + live tap test) |
| `ophelia logs` | View the universal chat log (messages + media, with filters) |

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

**Why the first photo each time feels slow — and the fix.** Ollama unloads a
model from memory after 5 minutes of inactivity by default. Vision is called
rarely (only when you send a photo), so the model almost always gets unloaded
between calls — every photo then cold-loads ~1GB from flash, a 10–25s stall on
a phone. Ophelia avoids this two ways:

- It launches `ollama serve` with `OLLAMA_KEEP_ALIVE=30m` (configurable via
  `OPHELIA_OLLAMA_KEEP_ALIVE`), so the model stays resident between uses.
  Set `-1` to keep it loaded forever (fastest, more RAM).
- On startup it sends a 1×1 warmup image so the model is already in memory
  before the first real photo arrives.

So expect a warm first-token delay of ~2–6s on an S21U (Snapdragon 888, CPU
only — MiniCPM-V 4.6's "2s" headline number is for an A18 Pro iPhone). Decode
runs ~3–8 tok/s. If you run `ollama serve` yourself instead of letting Ophelia
auto-start it, export `OLLAMA_KEEP_ALIVE=30m` before launching it for the same
benefit.

**Survive a phone reboot** — Ophelia can't start itself after a reboot, so use
the Termux:Boot add-on. Install it, then drop a script in `~/.termux/boot/`:

```bash
mkdir -p ~/.termux/boot
cat > ~/.termux/boot/start-ophelia <<'EOF'
#!/data/data/com.termux/files/usr/bin/sh
termux-wake-lock
OLLAMA_KEEP_ALIVE=30m ollama serve >/dev/null 2>&1 &
sleep 2 && tmux new -d -s ophelia 'ophelia run'
EOF
chmod +x ~/.termux/boot/start-ophelia
```

`scripts/install.ps1` · `scripts/install.sh` · `scripts/termux-install.sh` · `scripts/termux-shizuku-setup.sh`

## License

MIT
