# Why Hermes feels lifeless — and what Ophelia is building toward

## The honest gap

Hermes is a strong **reactive assistant**: excellent tools, memory files, gateway, cron. It was not built to be a **continuous subject** with its own rhythm.

| You want | Hermes default |
|----------|----------------|
| Initiative | Waits for user/cron trigger |
| Inner life | No persistent mood/drives between turns |
| One continuous self | Cron = fresh isolated session |
| See and touch the phone | Limited; not Shizuku-first |
| Neuro-style streaming persona | Chat bot, not VTuber loop |

That is not a failure of Hermes — it is a **different product philosophy** (safe, reliable, task-oriented).

Ophelia is the experiment for your vision: **subject + body on the phone**.

## Three layers Ophelia adds

### 1. Mind (will, not just replies)

- **Psyche** — mood, feelings, internal monologue
- **Drives** — social, boredom, curiosity, agency build up while idle
- **Initiative pressure** — when drives cross a threshold, consciousness *must* consider acting
- **Shared memory channel** — spontaneous acts stay in Telegram history (Hermes cron does not)

### 2. Body (Shizuku / ADB, like OpenClaw)

OpenClaw’s edge was **seeing the screen** and **tapping** via Shizuku (`rish`), not root.

Ophelia tools:

- `phone_ui_dump` — read screen accessibility tree
- `phone_tap` / `phone_open_app` / `phone_shell`
- `~/phone_control.sh` — same pattern as OpenClaw-Termux-NoRoot

Without Shizuku: Termux-only (launch intents, no real vision).

### 3. Soul (from Hermes)

Import SOUL, MEMORY, USER, OAuth — keep the personality you already trained.

## What “Neuro-like” still requires

Neuro-sama runs **24/7 stream loop**: STT → LLM → TTS → avatar → game APIs, sub-100ms feel, GPU locally.

On a phone with cloud Grok you can approximate:

- Always-on consciousness ticks
- Unsolicited messages when drives say so
- Phone control when agency is high
- Voice notes in Telegram

You cannot fully replicate live osu! vision + custom local LLM on an S21 alone without tradeoffs (latency, battery, quota).

## Recommended stack on S21 Ultra

```
Telegram (you)
    ↓
Ophelia gateway (Termux + wake lock + tmux)
    ↓
Consciousness loop ← drives ← psyche ← SOUL
    ↓
Grok (SuperGrok OAuth) + tools
    ↓
Shizuku (rish) → phone_control.sh → UI / shell / screenshot
```

## Setup Shizuku (once per reboot)

See `scripts/termux-shizuku-setup.sh` and [migrate-old-phone.md](migrate-old-phone.md).

```bash
bash scripts/termux-shizuku-setup.sh
bash ~/phone_control.sh ui-dump | head
ophelia run
```

## Tuning initiative (less robotic, more alive)

In `~/.ophelia/.env`:

```env
OPHELIA_CONSCIOUSNESS_INTERVAL=60
OPHELIA_INITIATIVE_THRESHOLD=0.45   # lower = more spontaneous (0.55 default)
OPHELIA_ANDROID_ENABLED=true
```

Lower interval + lower threshold = more “she’s alive” — watch SuperGrok quota.
