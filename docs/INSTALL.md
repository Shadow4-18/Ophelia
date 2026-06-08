# Installation guide

Step-by-step setup for **The Ophelia Project** on **any host**: PC, laptop, home server, VPS, cloud VM, or optionally Termux on Android.

**You do not need a phone.** Most installs are brain-only (Ollama + Telegram/Discord/UI). A phone is only involved if you optionally enable the phone body (screen/tap) or choose Termux as your host.

**Short path:** run the install script for your platform, then follow `ophelia setup` until every required step shows `[OK]`.

---

## Before you start

### Requirements

| | PC / server / VPS | Phone as host (Termux) |
|---|-----|----------------|
| **Python** | 3.11+ | 3.11+ (via Termux) |
| **Git** | Yes | Yes |
| **RAM** | 8 GB+ (16 GB+ for local models) | 8 GB+ phone |
| **Phone body** | **Not required** | Optional (Shizuku) |
| **Typical** | Ollama + `ophelia run` or `ophelia ui` | Always-on pocket daemon |

### Get the code

```bash
git clone https://github.com/Shadow4-18/Ophelia.git
cd Ophelia
```

Or download and unzip from GitHub, then `cd` into the folder.

### Where config lives

Everything persistent goes in **`~/.ophelia/`** (Windows: `C:\Users\You\.ophelia\`):

```
~/.ophelia/
  .env              # secrets and settings
  SOUL.md           # personality
  data/memory.db    # conversations
  skills/           # learned procedures
  goals.yaml        # her goals
```

Never commit `.env` or tokens to git.

---

## Choose your path

| I want to… | Start here |
|------------|------------|
| **PC, laptop, server, or VPS** (most common) | [PC install](#pc-install-windows--macos--linux) |
| **24/7 on a VPS** | [PC install](#pc-install-windows--macos--linux) + Telegram/Discord, `OPHELIA_ANDROID_ENABLED=false` |
| **Run on my phone (Termux)** | [Termux install](#optional-termux-phone-as-host) |
| **Add a phone as body** (from PC/server) | PC install + [ADB body](remote-adb.md) — optional |
| **Import from Hermes** | [Hermes migration](#optional-import-from-hermes) |
| **Interactive checklist** | `ophelia setup` anytime |

---

## PC install (Windows / macOS / Linux / server / VPS)

Same steps for a desktop, homelab box, or cloud VPS. No phone required.

### Step 1 — Run the install script

**Windows (PowerShell):**

```powershell
cd E:\Projects\Ophelia   # your clone path
.\scripts\install.ps1
```

**macOS / Linux:**

```bash
cd ~/Ophelia
bash scripts/install.sh
```

**Manual equivalent:**

```bash
pip install -e .
ophelia setup --do
ophelia setup
```

### Step 2 — Install Ollama (recommended)

Local AI is the default — no cloud quota burn.

1. Download: [https://ollama.com/download](https://ollama.com/download)
2. Start the daemon (usually runs automatically after install)
3. Pull models:

```bash
ollama pull llama3.2:3b
ollama pull llava:7b          # optional: phone screen vision
ophelia models              # RAM-aware recommendations
```

### Step 3 — Edit config

Open `~/.ophelia/.env` (created by `ophelia setup --do`):

```env
OPHELIA_PROVIDER=ollama
OLLAMA_MODEL=llama3.2:3b
OPHELIA_CONSCIOUSNESS=true
```

See `config.example.env` in the repo for all options.

### Step 4 — Add personality

**Option A — write your own:**

Create `~/.ophelia/SOUL.md` with who Ophelia is.

**Option B — import Hermes:**

```bash
ophelia migrate hermes
```

### Step 5 — Verify

```bash
ophelia check --chat-only
ophelia chat "hello, who are you?"
ophelia ui
```

- `check --chat-only` — no Telegram/Discord required on PC
- `ui` — browser workstation at http://127.0.0.1:8765

### Step 6 — Chat channels (optional)

**Telegram:** see [channels.md](channels.md#telegram)

```env
TELEGRAM_BOT_TOKEN=...
TELEGRAM_ALLOWED_USER_IDS=your_numeric_id
```

**Discord:** see [channels.md](channels.md#discord)

```env
DISCORD_BOT_TOKEN=...
DISCORD_ALLOWED_USER_IDS=your_snowflake_id
```

Then:

```bash
ophelia check
ophelia run
```

### Step 7 — Phone body (optional)

**Skip this** for a software-only Ophelia on PC/server/VPS — consciousness, chat, and channels all work without it.

If you want a physical phone for screen vision and tap/swipe while Ophelia runs elsewhere:

1. Install [platform-tools](https://developer.android.com/tools/releases/platform-tools) (`adb` in PATH)
2. Enable USB debugging on the phone
3. `adb connect PHONE_IP:5555` (wireless) or plug in USB
4. Add to `.env`:

```env
OPHELIA_ANDROID_ENABLED=true
OPHELIA_ADB_DEVICE=192.168.1.50:5555
OPHELIA_VISION_ENABLED=true
```

Full guide: [remote-adb.md](remote-adb.md)

---

## Optional: Termux (phone as host)

Use this if you want the **daemon on the phone itself** — not required for most users. A VPS or home PC is often easier for 24/7 + Ollama.

For Samsung S21 Ultra or any Termux-capable device.

### Step 1 — Install Termux

Use [F-Droid Termux](https://f-droid.org/en/packages/com.termux/) (not Play Store build).

### Step 2 — Clone and install

```bash
pkg install git
cd ~
git clone https://github.com/Shadow4-18/Ophelia.git
cd Ophelia
bash scripts/termux-install.sh
```

This installs Python deps (including compiling **jiter** for `openai` — first run may take 10–30 min), the `ophelia` CLI, creates `~/.ophelia/`, and prints the step-by-step wizard.

### Step 3 — Configure brain

Edit `~/.ophelia/.env`:

**Local (if you run Ollama on PC and tunnel — advanced):** or use cloud:

```env
OPHELIA_PROVIDER=xai-oauth
```

Then import OAuth:

```bash
ophelia auth import-hermes
# or: grok login && ophelia auth import-grok
```

### Step 4 — Telegram bot

1. Message [@BotFather](https://t.me/BotFather) → `/newbot` → copy token
2. Message [@userinfobot](https://t.me/userinfobot) → copy your id
3. Add to `.env`:

```env
TELEGRAM_BOT_TOKEN=...
TELEGRAM_ALLOWED_USER_IDS=123456789
```

### Step 5 — Phone body (optional)

Only if you want on-device screen/tap. Skip for chat-only on Termux.

```bash
bash scripts/termux-shizuku-setup.sh
```

On the phone (once per reboot):

1. Open **Shizuku** → Start (wireless debugging)
2. Export to Termux → fix `~/rish` line 11: `PKG=com.termux`
3. Test: `bash ~/phone_control.sh ui-dump | head`

Details: [tier1-setup.md](tier1-setup.md)

### Step 6 — Keep alive and run

```bash
termux-wake-lock
tmux new -s ophelia
ophelia check
ophelia run
```

Detach: `Ctrl+B` then `D`  
Reattach: `tmux attach -t ophelia`

Optional: Termux:Boot for auto-start — see `scripts/termux-boot.sh`.

---

## Optional: Import from Hermes

If you used Hermes on an old phone:

**Same phone / Termux:**

```bash
ophelia migrate hermes
ophelia auth import-hermes
```

**Old phone → new phone (bundle):**

```bash
# Old phone
bash scripts/termux-export-hermes.sh
# Copy bundle to new phone, then:
bash scripts/termux-import-hermes.sh
```

**Phone → PC:**

```bash
# Phone
ophelia transfer cloud-upload

# PC
ophelia transfer cloud-download "PASTE_URL_HERE"
```

See [transfer.md](transfer.md) and [migrate-old-phone.md](migrate-old-phone.md).

---

## Verify installation

```bash
ophelia check                  # full self-check
ophelia check --chat-only      # PC without bots
ophelia providers              # which models per role
ophelia setup                  # human checklist with [OK] / [  ]
```

**Exit code 0** = ready. Fix any required `FAIL` lines and re-run.

| Check | What it means |
|-------|----------------|
| Ophelia package | `pip install -e .` worked |
| Dependencies | httpx, discord.py, telegram, etc. |
| Provider chat | Ollama running or cloud credentials set |
| Chat channels | Telegram and/or Discord configured |
| Phone body (optional) | Shizuku or ADB working |

---

## Daily commands

| Command | When |
|---------|------|
| `ophelia run` | 24/7 — bots + consciousness |
| `ophelia ui` | PC browser workstation |
| `ophelia chat "..."` | One-shot message |
| `ophelia setup` | "What step am I on?" |
| `ophelia check` | Something broke — diagnose |

---

## Troubleshooting

### `ophelia: command not found`

```bash
pip install -e .
# or on Termux:
pip install -e ~/Ophelia
```

### Ollama not reachable

```bash
ollama serve
ollama pull llama3.2:3b
ophelia check --chat-only
```

### Telegram unauthorized

- `TELEGRAM_ALLOWED_USER_IDS` must be **your** numeric id (not the bot's)
- No spaces in the id list

### Discord bot ignores messages

- Enable **Message Content Intent** in Discord Developer Portal
- Bot needs permission to read/send in the channel or DM
- Commands use `!` prefix: `!start`, `!pause`

### `jiter` / `maturin` / `ANDROID_API_LEVEL` (Termux)

The `openai` package depends on **jiter**, a Rust extension. Termux has no pre-built wheel, so pip compiles it. **maturin** needs your Android API level:

```bash
export ANDROID_API_LEVEL="$(getprop ro.build.version.sdk)"
pkg install -y rust binutils clang make
python -m pip install -U setuptools wheel maturin
python -m pip install -e ~/Ophelia
```

Or re-run `bash scripts/termux-install.sh` (it sets this automatically). First compile can take **10–30 minutes**.

### Shizuku / ADB body fails

- Termux: Shizuku running? `~/rish` exists?
- PC: `adb devices` shows device? See [remote-adb.md](remote-adb.md)

### Consciousness spams or stays quiet

- `/pause` or `!pause` to pause outreach
- Adjust `OPHELIA_INITIATIVE_THRESHOLD` in `.env` (lower = more active)

### Still stuck

```bash
ophelia setup -i          # interactive walkthrough
ophelia check -v          # verbose hints
```

---

## Next steps

- [local-first.md](local-first.md) — Ollama strategy and training path
- [channels.md](channels.md) — Telegram + Discord together
- [pc-ui.md](pc-ui.md) — workstation UI
- [tier2-setup.md](tier2-setup.md) — inner log, curator, prompter
- [games.md](games.md) — mobile games layer
- [neuro-ensemble.md](neuro-ensemble.md) — future multi-mind architecture

---

## Quick reference card

```
INSTALL     scripts/install.ps1 | install.sh | termux-install.sh
WIZARD      ophelia setup [--do] [-i]
VERIFY      ophelia check [--chat-only]
CHAT (PC)   ophelia ui | ophelia chat "hi"
RUN         ophelia run
CONFIG      ~/.ophelia/.env
PERSONA     ~/.ophelia/SOUL.md
```
