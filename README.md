# Ophelia

A **willful, phone-bodied AI** for **Termux** + **Telegram** — built because Hermes is excellent but **reactive and lifeless** for a Neuro-like vision.

**SuperGrok OAuth** · **Hermes soul import** · **continuous consciousness + drives** · **Shizuku/ADB body** (OpenClaw-style)

Read why Hermes isn't enough: [docs/why-not-hermes.md](docs/why-not-hermes.md)

## Why Ophelia vs Hermes cron

Hermes cron jobs run in **fresh, isolated sessions**. Deliveries are **not mirrored** into your live Telegram chat, so you cannot naturally continue ("what did you just say?"). Ophelia uses one **continuous consciousness loop** that shares the **same SQLite conversation** as your Telegram thread.

| Hermes cron | Ophelia consciousness |
|-------------|----------------------|
| New session every tick | Same memory + user channel |
| Self-contained prompt only | Sees recent chat + inner thoughts |
| Broadcast-only delivery | Outbound messages stored as `assistant` in your thread |
| Schedule-driven | **Feeling-driven** (mood, urges, boredom, curiosity) |

## SuperGrok OAuth (your subscription)

```bash
# On PC or phone where Hermes is already logged in:
ophelia migrate hermes          # SOUL, MEMORY, USER, skills, state.db archive
ophelia auth import-hermes      # copies ~/.hermes/auth.json → OAuth bearer

# Or from Grok CLI:
grok login
ophelia auth import-grok
```

Set `OPHELIA_PROVIDER=xai-oauth` (default). API key is fallback only (`OPHELIA_PROVIDER=xai`).

## Neuro-like inner life

Each consciousness tick updates:

- **Mood** (valence, arousal, label)
- **Feelings** and **urges**
- **Internal thought** (logged to `consciousness` channel)
- **Action**: `silent` | `message` | `reflect`

When she **chooses** to message (not on a fixed spam schedule), it is written to **your** `telegram:{id}` channel so the next reply has full context.

Higher arousal → faster ticks (restless). Low arousal → slower (calm).

## Provider roadmap

| Provider | Use |
|----------|-----|
| `xai-oauth` | SuperGrok today (default) |
| `xai` | Paid API fallback |
| `ollama` | Local fine-tunes / training later |

## Migrate from Hermes (old phone only)

**Hermes on old Termux phone → S21:** see [docs/migrate-old-phone.md](docs/migrate-old-phone.md)

```bash
# Old phone
bash scripts/termux-export-hermes.sh
# Copy ophelia-hermes-bundle.tar.gz to S21 Download/

# S21
bash scripts/termux-import-hermes.sh
```

Imports:

- `SOUL.md` → `~/.ophelia/SOUL.md`
- `memories/MEMORY.md`, `USER.md` (§ entries)
- `auth.json` (SuperGrok OAuth)
- `skills/` → `~/.ophelia/skills/hermes-import/`
- `state.db` → archive for future session search
- `.env` / config hints

```bash
ophelia migrate hermes --dry-run   # preview
ophelia migrate hermes
ophelia auth import-hermes
# Merge ~/.ophelia/from-hermes.env into ~/.ophelia/.env
ophelia doctor
ophelia run
```

## Termux (S21 Ultra)

```bash
termux-wake-lock
tmux new -s ophelia
ophelia run
```

See `scripts/termux-install.sh` and `scripts/termux-boot.sh`.

## Commands

| Command | Purpose |
|---------|---------|
| `ophelia run` | Telegram + consciousness |
| `ophelia migrate hermes` | Import personality & memories |
| `ophelia auth import-hermes` | SuperGrok OAuth + refresh token from Hermes |
| `ophelia auth refresh` | Refresh OAuth now |
| `/pause` `/resume` | Pause spontaneous consciousness outreach |
| `/voice on` | Reply with TTS voice notes |
| Voice message | Telegram → xAI STT → Ophelia |

## Mobile games

[docs/games.md](docs/games.md) — `games.yaml`, `/game play`, `phone_game_look`, swipe/tap, bounded sessions.

## Tier 2 (inner log, listen, prompter, curator)

[docs/tier2-setup.md](docs/tier2-setup.md) — inner monologue file, `/listen` mic loop, `PROMPTER.md`, auto MEMORY curation.

## Tier 1 (vision, survival, goals, initiative)

See [docs/tier1-setup.md](docs/tier1-setup.md) — run on S21:

```bash
bash scripts/termux-survival.sh
bash scripts/termux-shizuku-setup.sh
# edit ~/.ophelia/goals.yaml
ophelia run
```

## Will + body (vs Hermes)

| Layer | What |
|-------|------|
| **Drives** | social, boredom, curiosity, agency — build while idle |
| **Initiative** | `OPHELIA_INITIATIVE_THRESHOLD` — when pressure is high, she *must* consider acting |
| **Shizuku** | `phone_ui_dump`, `phone_tap`, `phone_shell` via `~/phone_control.sh` + `rish` |
| **One mind** | No cron isolation — Telegram thread is her continuous self |

```bash
bash scripts/termux-shizuku-setup.sh   # after Shizuku export to Termux
bash ~/phone_control.sh ui-dump | head
```

## Features

- **OAuth refresh** — SuperGrok, Hermes-compatible `auth.json`
- **Hermes `state.db` search** — past conversations
- **Honcho** — optional
- **Voice** — Telegram STT/TTS
- **Consciousness** — message / act / **explore** (screen via Shizuku)

## License

MIT
